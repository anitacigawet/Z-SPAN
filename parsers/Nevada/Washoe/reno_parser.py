"""Reno — PrimeGov meeting parser."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
import json
import logging
import re
import ssl
from html import unescape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import HTTPSHandler, Request, build_opener


logger = logging.getLogger(__name__)

DEFAULT_URL = "https://reno.primegov.com/public/portal"
BASE_URL = "https://reno.primegov.com"
ARCHIVE_DEPTH_YEARS = 3
MAX_RESPONSE_BYTES = 8_000_000
REQUEST_TIMEOUT = (10, 30)
ALLOWED_FETCH_HOSTS = {"reno.primegov.com"}
ALLOWED_EMIT_HOSTS = {
    "reno.primegov.com",
    "youtube.com",
    "www.youtube.com",
}
BAD_URL_PREFIXES = (
    "javascript:",
    "data:",
    "vbscript:",
    "file:",
    "mailto:",
    "ftp:",
    "gopher:",
)
CANONICAL_FIELDS = (
    "meeting_title",
    "meeting_date",
    "meeting_time",
    "meeting_location",
    "meeting_status",
    "agenda_url",
    "minutes_url",
    "video_url",
    "agenda_packet_url",
    "ecomment_url",
    "meeting_id",
)
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
TIME_RE = re.compile(
    r"^\s*(\d{1,2})(?::(\d{2}))?\s*([AP])\.?M\.?\s*$",
    re.IGNORECASE,
)
EXPECTED_MEETING_KEYS = {
    "id",
    "meetingTypeId",
    "committeeId",
    "dateTime",
    "endDateTime",
    "date",
    "time",
    "endTime",
    "documentList",
    "allowPublicSpeaker",
    "allowPublicComment",
    "isZoomMeeting",
    "videoUrl",
    "swagitId",
    "isShowVideoIcon",
    "isMediaManagerVideo",
    "externalProviderMeetingId",
    "zoomMeetingLink",
    "meetingOnline",
    "streamCompleted",
    "meetingState",
    "publishDate",
    "title",
    "location",
}
KNOWN_COMPILE_OUTPUT_TYPES = {1, 2, 3}


class ScriptSrcParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.script_sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attr_map = {name.lower(): value or "" for name, value in attrs}
        src = attr_map.get("src", "")
        if src:
            self.script_sources.append(src)


@dataclass
class ScrapeStats:
    rows_seen: int = 0
    rows_accepted: int = 0
    rows_dropped: int = 0
    drop_reasons: Counter[str] = field(default_factory=Counter)
    api_counts: dict[str, int] = field(default_factory=dict)
    field_absences: Counter[str] = field(default_factory=Counter)
    meeting_states: Counter[str] = field(default_factory=Counter)
    document_labels: Counter[str] = field(default_factory=Counter)
    compile_output_types: Counter[str] = field(default_factory=Counter)
    unclassified_documents: list[str] = field(default_factory=list)
    rejected_urls: Counter[str] = field(default_factory=Counter)
    duplicate_ids: list[str] = field(default_factory=list)
    public_comment_flags: int = 0
    public_speaker_flags: int = 0
    zoom_flags: int = 0


def scrape_calendar(url: str = DEFAULT_URL) -> list[dict[str, str]]:
    """Scrape Reno meetings from the PrimeGov public portal API."""
    stats = ScrapeStats()
    logger.warning(
        "primegov_public_portal_no_standalone_ecomment_url: comment/speaker flags are modal/API signals; ecomment_url empties are counted per run"
    )

    portal_html = _fetch_text_bounded(url)
    upcoming_script_url = _validate_primegov_surface(portal_html, url)
    upcoming_script = _fetch_text_bounded(upcoming_script_url)
    _validate_document_route_pattern(upcoming_script, upcoming_script_url)

    years = _latest_archive_years()
    meetings_by_id: dict[str, dict[str, str]] = {}
    for year in years:
        api_url = f"{BASE_URL}/api/v2/PublicPortal/ListArchivedMeetings?{urlencode({'year': year})}"
        rows = _fetch_meeting_rows(api_url, f"archive:{year}", stats)
        if not rows:
            logger.warning(
                "archive year %s returned zero rows from %s; keeping other fetched years",
                year,
                api_url,
            )
        _merge_rows(rows, api_url, meetings_by_id, stats)

    upcoming_url = f"{BASE_URL}/api/v2/PublicPortal/ListUpcomingMeetings"
    upcoming_rows = _fetch_meeting_rows(upcoming_url, "upcoming", stats)
    _merge_rows(upcoming_rows, upcoming_url, meetings_by_id, stats)

    meetings = sorted(
        meetings_by_id.values(),
        key=lambda row: (row["meeting_date"], row["meeting_time"], row["meeting_id"]),
    )
    _log_stats(stats, len(meetings), years)
    return meetings


def _fetch_text_bounded(url: str) -> str:
    _validate_fetch_host(url)
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"})
    try:
        with _http_opener().open(request, timeout=REQUEST_TIMEOUT[1]) as response:
            _validate_fetch_host(response.geturl())
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ValueError(f"Response from {url} exceeded {MAX_RESPONSE_BYTES} bytes")
                chunks.append(chunk)
            encoding = response.headers.get_content_charset() or "utf-8"
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"Failed to fetch {url}: {exc}") from exc

    return b"".join(chunks).decode(encoding, errors="replace")


def _http_opener():
    verify_paths = ssl.get_default_verify_paths()
    if verify_paths.cafile or not Path("/etc/ssl/cert.pem").exists():
        return build_opener()
    context = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    return build_opener(HTTPSHandler(context=context))


def _validate_fetch_host(url: str) -> None:
    host = (urlparse(url).hostname or "").lower()
    if host not in ALLOWED_FETCH_HOSTS:
        raise ValueError(f"PrimeGov fetch redirected to disallowed host {host!r}: {url}")


def _validate_primegov_surface(html: str, url: str) -> str:
    parsed = urlparse(url)
    if parsed.hostname != "reno.primegov.com":
        raise ValueError(f"Expected Reno PrimeGov host, got {parsed.hostname!r}")

    required_markers = (
        'id="upcomingMeetingsTable"',
        'id="archivedMeetingsTable"',
        "/Scripts/Custom/Public/_Archived.js",
    )
    missing = [marker for marker in required_markers if marker not in html]
    if missing:
        raise ValueError(f"Reno portal no longer matches PrimeGov public portal markers: {missing}")

    parser = ScriptSrcParser()
    parser.feed(html)
    script_src = next((src for src in parser.script_sources if "_Upcoming.js" in src), "")
    if not script_src:
        raise ValueError("Reno PrimeGov portal missing _Upcoming.js script reference")
    script_url = _emit_url(script_src, url, "vendor_script", "portal", None)
    if not script_url:
        raise ValueError(f"Rejected PrimeGov _Upcoming.js script URL: {script_src!r}")
    logger.info(
        "validated PrimeGov portal fingerprint: upcoming/archived tables + public portal script=%s",
        script_url,
    )
    return script_url


def _validate_document_route_pattern(script_text: str, script_url: str) -> None:
    required = (
        "function getHref(doc)",
        "/Public/CompiledDocument?",
        "/Portal/Meeting?",
        "meetingTemplateId=",
        "compiledMeetingDocumentFileId=",
    )
    missing = [marker for marker in required if marker not in script_text]
    if missing:
        raise ValueError(f"PrimeGov document-route helper changed in {script_url}: {missing}")
    logger.info(
        "validated PrimeGov document route pattern from %s: getHref uses stable same-host compiled document routes",
        script_url,
    )


def _latest_archive_years() -> list[str]:
    url = f"{BASE_URL}/api/v2/PublicPortal/GetArchivedMeetingYears"
    data = _fetch_json(url)
    if not isinstance(data, list):
        raise ValueError(f"Unexpected PrimeGov archived years payload: {type(data).__name__}")

    years: list[int] = []
    for raw_year in data:
        try:
            years.append(int(raw_year))
        except (TypeError, ValueError):
            logger.warning("dropped archived year value %r: not an integer", raw_year)

    if len(years) < ARCHIVE_DEPTH_YEARS:
        raise ValueError(
            f"PrimeGov returned only {len(years)} archived years, expected {ARCHIVE_DEPTH_YEARS}: {years}"
        )

    sorted_years = sorted(set(years), reverse=True)
    if years != sorted(years, reverse=True):
        logger.warning("archived years were not descending as emitted by vendor: %s", years)

    selected = [str(year) for year in sorted_years[:ARCHIVE_DEPTH_YEARS]]
    logger.info(
        "PrimeGov archived years observed=%s; selected latest %d years=%s using configured archive depth",
        years,
        ARCHIVE_DEPTH_YEARS,
        selected,
    )
    return selected


def _fetch_json(url: str):
    text = _fetch_text_bounded(url)
    return json.loads(text)


def _fetch_meeting_rows(
    api_url: str,
    source_label: str,
    stats: ScrapeStats,
) -> list[dict]:
    data = _fetch_json(api_url)
    if not isinstance(data, list):
        raise ValueError(f"Unexpected PrimeGov {source_label} payload: {type(data).__name__}")

    rows = [row for row in data if isinstance(row, dict)]
    non_dict_count = len(data) - len(rows)
    if non_dict_count:
        stats.rows_dropped += non_dict_count
        stats.drop_reasons[f"{source_label}:non_dict_row"] += non_dict_count
        logger.warning(
            "dropped %d %s rows: payload entries were not objects",
            non_dict_count,
            source_label,
        )

    stats.api_counts[source_label] = len(rows)
    if rows:
        missing_keys = sorted(EXPECTED_MEETING_KEYS - set(rows[0]))
        if missing_keys:
            raise ValueError(f"PrimeGov {source_label} row missing expected keys: {missing_keys}")
    logger.info("fetched PrimeGov %s rows=%d from %s", source_label, len(rows), api_url)
    return rows


def _merge_rows(
    rows: list[dict],
    base_url: str,
    meetings_by_id: dict[str, dict[str, str]],
    stats: ScrapeStats,
) -> None:
    for row in rows:
        stats.rows_seen += 1
        meeting_id = _clean_text(str(row.get("id") or ""))
        if not meeting_id:
            stats.rows_dropped += 1
            stats.drop_reasons["missing_meeting_id"] += 1
            logger.warning("dropped PrimeGov row without id: title=%r date=%r", row.get("title"), row.get("date"))
            continue

        meeting = _build_meeting(row, base_url, stats)
        if not meeting["meeting_date"]:
            stats.rows_dropped += 1
            stats.drop_reasons["missing_meeting_date"] += 1
            logger.warning("dropped meeting id=%s: no valid per-row date evidence", meeting_id)
            continue

        if meeting_id in meetings_by_id:
            stats.rows_dropped += 1
            stats.drop_reasons["duplicate_meeting_id"] += 1
            stats.duplicate_ids.append(meeting_id)
            logger.warning(
                "dropped duplicate meeting id=%s title=%r from %s",
                meeting_id,
                meeting["meeting_title"],
                base_url,
            )
            continue

        _validate_schema(meeting)
        meetings_by_id[meeting_id] = meeting
        stats.rows_accepted += 1
        logger.info(
            "emitted meeting id=%s date=%s time=%r title=%r status=%s agenda=%r packet=%r minutes=%r video=%r",
            meeting["meeting_id"],
            meeting["meeting_date"],
            meeting["meeting_time"],
            meeting["meeting_title"],
            meeting["meeting_status"],
            meeting["agenda_url"],
            meeting["agenda_packet_url"],
            meeting["minutes_url"],
            meeting["video_url"],
        )


def _build_meeting(row: dict, base_url: str, stats: ScrapeStats) -> dict[str, str]:
    meeting_id = _clean_text(str(row.get("id") or ""))
    title = _clean_text(str(row.get("title") or ""))
    if not title:
        stats.field_absences["meeting_title"] += 1
        logger.warning("meeting id=%s has empty title field", meeting_id)

    meeting_date, meeting_time = _extract_date_time(row, meeting_id, stats)
    location = _clean_text(str(row.get("location") or ""))
    if not location:
        stats.field_absences["meeting_location"] += 1

    urls = {
        "agenda_url": "",
        "minutes_url": "",
        "video_url": "",
        "agenda_packet_url": "",
        "ecomment_url": "",
    }
    _assign_document_urls(row, base_url, meeting_id, urls, stats)
    _assign_video_url(row, base_url, meeting_id, urls, stats)
    _track_public_option_flags(row, meeting_id, stats)

    status = _status_from_evidence(title, urls)
    meeting_state = _clean_text(str(row.get("meetingState") or ""))
    if meeting_state:
        stats.meeting_states[meeting_state] += 1

    for field_name, field_value in urls.items():
        if not field_value:
            stats.field_absences[field_name] += 1

    return {
        "meeting_title": title,
        "meeting_date": meeting_date,
        "meeting_time": meeting_time,
        "meeting_location": location,
        "meeting_status": status,
        "agenda_url": urls["agenda_url"],
        "minutes_url": urls["minutes_url"],
        "video_url": urls["video_url"],
        "agenda_packet_url": urls["agenda_packet_url"],
        "ecomment_url": urls["ecomment_url"],
        "meeting_id": meeting_id,
    }


def _extract_date_time(row: dict, meeting_id: str, stats: ScrapeStats) -> tuple[str, str]:
    datetime_value = _clean_text(str(row.get("dateTime") or ""))
    date_value = _clean_text(str(row.get("date") or ""))
    time_value = _clean_text(str(row.get("time") or ""))

    parsed_dt: datetime | None = None
    if datetime_value:
        try:
            parsed_dt = datetime.fromisoformat(datetime_value)
        except ValueError:
            logger.warning(
                "meeting id=%s has unparsable dateTime=%r",
                meeting_id,
                datetime_value,
            )

    iso_date = parsed_dt.date().isoformat() if parsed_dt else ""
    if date_value and parsed_dt:
        try:
            vendor_date = datetime.strptime(date_value, "%b %d, %Y").date().isoformat()
            if vendor_date != iso_date:
                logger.warning(
                    "meeting id=%s date conflict: dateTime=%r date=%r; dropping date as ambiguous",
                    meeting_id,
                    datetime_value,
                    date_value,
                )
                return "", ""
        except ValueError:
            logger.warning(
                "meeting id=%s has unparsable date field=%r; using dateTime date if available",
                meeting_id,
                date_value,
            )
    elif date_value and not parsed_dt:
        try:
            iso_date = datetime.strptime(date_value, "%b %d, %Y").date().isoformat()
        except ValueError:
            logger.warning("meeting id=%s has no parseable date evidence: %r", meeting_id, date_value)

    api_time = _normalize_time(time_value, meeting_id, "time", stats) if time_value else ""
    datetime_time = _format_time(parsed_dt) if parsed_dt else ""
    if api_time and datetime_time and api_time != datetime_time:
        logger.warning(
            "meeting id=%s time conflict: dateTime=%r gives %r but time field gives %r; emitting empty meeting_time",
            meeting_id,
            datetime_value,
            datetime_time,
            api_time,
        )
        stats.field_absences["meeting_time"] += 1
        return iso_date, ""
    if api_time:
        return iso_date, api_time
    if datetime_time:
        return iso_date, datetime_time

    stats.field_absences["meeting_time"] += 1
    logger.warning("meeting id=%s has no per-row time signal in PrimeGov dateTime/time fields", meeting_id)
    return iso_date, ""


def _normalize_time(value: str, meeting_id: str, source_field: str, stats: ScrapeStats) -> str:
    match = TIME_RE.match(value[:32])
    if not match:
        logger.warning(
            "meeting id=%s has unparsable %s value=%r; emitting empty meeting_time if no fallback agrees",
            meeting_id,
            source_field,
            value,
        )
        stats.field_absences["meeting_time"] += 1
        return ""

    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    suffix = match.group(3).upper()
    if hour < 1 or hour > 12 or minute > 59:
        logger.warning(
            "meeting id=%s has out-of-range %s value=%r",
            meeting_id,
            source_field,
            value,
        )
        stats.field_absences["meeting_time"] += 1
        return ""
    return f"{hour}:{minute:02d} {suffix}M"


def _format_time(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%I:%M %p").lstrip("0")


def _assign_document_urls(
    row: dict,
    base_url: str,
    meeting_id: str,
    urls: dict[str, str],
    stats: ScrapeStats,
) -> None:
    documents = row.get("documentList") or []
    if not isinstance(documents, list):
        logger.warning(
            "meeting id=%s documentList was %s, expected list; document URLs left empty",
            meeting_id,
            type(documents).__name__,
        )
        return

    candidates: dict[str, list[tuple[int, str, str]]] = {
        "agenda_url": [],
        "minutes_url": [],
        "agenda_packet_url": [],
    }
    for document in documents:
        if not isinstance(document, dict):
            logger.warning("meeting id=%s dropped non-object document entry=%r", meeting_id, document)
            continue

        label = _clean_text(str(document.get("templateName") or ""))
        stats.document_labels[label or "<empty>"] += 1
        output_type = _to_int(document.get("compileOutputType"))
        stats.compile_output_types[str(output_type) if output_type is not None else "<missing>"] += 1
        if output_type not in KNOWN_COMPILE_OUTPUT_TYPES:
            logger.warning(
                "meeting id=%s document label=%r has unknown compileOutputType=%r; URL dropped",
                meeting_id,
                label,
                document.get("compileOutputType"),
            )
            continue

        publish_status = _clean_text(str(document.get("publishStatus") or ""))
        if publish_status and publish_status != "1":
            logger.warning(
                "meeting id=%s document label=%r has non-public publishStatus=%r; browser may hide this",
                meeting_id,
                label,
                publish_status,
            )

        field_name = _classify_document_label(label, meeting_id, stats)
        if not field_name:
            continue

        href = _document_href(document, output_type, meeting_id)
        document_url = _emit_url(href, base_url, field_name, meeting_id, stats)
        if not document_url:
            continue
        priority = _document_priority(label, output_type)
        candidates[field_name].append((priority, document_url, label))

    for field_name, field_candidates in candidates.items():
        if not field_candidates:
            continue
        field_candidates.sort(key=lambda item: (item[0], item[1]))
        urls[field_name] = field_candidates[0][1]
        if len(field_candidates) > 1:
            skipped = [label for _, _, label in field_candidates[1:]]
            logger.info(
                "meeting id=%s selected %s label=%r and skipped lower-priority labels=%s",
                meeting_id,
                field_name,
                field_candidates[0][2],
                skipped,
            )


def _classify_document_label(label: str, meeting_id: str, stats: ScrapeStats) -> str:
    lowered = label.lower()
    if "minute" in lowered:
        return "minutes_url"
    if "packet" in lowered:
        return "agenda_packet_url"
    if "agenda" in lowered:
        return "agenda_url"

    stats.unclassified_documents.append(f"{meeting_id}:{label}")
    logger.warning(
        "meeting id=%s document label=%r did not map to agenda/minutes/packet; URL dropped",
        meeting_id,
        label,
    )
    return ""


def _document_href(document: dict, output_type: int, meeting_id: str) -> str:
    external_link = _clean_text(str(document.get("link") or ""))
    if external_link:
        return external_link

    template_id = _to_int(document.get("templateId"))
    document_id = _to_int(document.get("id"))
    if template_id is not None and template_id > 0:
        meeting_param = urlencode({"meetingTemplateId": str(template_id)})
    elif document_id is not None and document_id > 0:
        meeting_param = urlencode({"compiledMeetingDocumentFileId": str(document_id)})
    else:
        logger.warning(
            "meeting id=%s document missing templateId/id required by PrimeGov getHref route",
            meeting_id,
        )
        return ""

    if output_type == 3:
        return f"/Portal/Meeting?{meeting_param}"
    return f"/Public/CompiledDocument?{meeting_param}&{urlencode({'compileOutputType': str(output_type)})}"


def _document_priority(label: str, output_type: int) -> int:
    priority = {1: 0, 2: 10, 3: 20}.get(output_type, 50)
    lowered = label.lower()
    if "spanish" in lowered:
        priority += 5
    if "draft" in lowered:
        priority += 3
    return priority


def _assign_video_url(
    row: dict,
    base_url: str,
    meeting_id: str,
    urls: dict[str, str],
    stats: ScrapeStats,
) -> None:
    video_value = _clean_text(str(row.get("videoUrl") or ""))
    if video_value:
        urls["video_url"] = _emit_url(video_value, base_url, "video_url", meeting_id, stats)
        return

    if row.get("isShowVideoIcon"):
        logger.warning(
            "meeting id=%s has isShowVideoIcon=true but empty videoUrl; video_url left empty",
            meeting_id,
        )


def _track_public_option_flags(row: dict, meeting_id: str, stats: ScrapeStats) -> None:
    if row.get("allowPublicComment"):
        stats.public_comment_flags += 1
        logger.warning(
            "meeting id=%s has allowPublicComment=true but portal exposes comments through a modal, not a stable URL; ecomment_url left empty",
            meeting_id,
        )
    if row.get("allowPublicSpeaker"):
        stats.public_speaker_flags += 1
        logger.warning(
            "meeting id=%s has allowPublicSpeaker=true but canonical schema has no speaker URL field; no URL emitted",
            meeting_id,
        )
    if row.get("isZoomMeeting") or row.get("zoomMeetingLink"):
        stats.zoom_flags += 1
        logger.warning(
            "meeting id=%s has Zoom meeting signal; zoomMeetingLink is not emitted as video_url",
            meeting_id,
        )


def _status_from_evidence(title: str, urls: dict[str, str]) -> str:
    if CANCELLED_RE.search(title[:500]):
        return "Cancelled"
    if urls["minutes_url"]:
        return "Minutes Available"
    if urls["agenda_url"] or urls["agenda_packet_url"]:
        return "Agenda Available"
    return "Scheduled"


def _emit_url(
    href: str,
    base_url: str,
    field: str,
    row_id: str,
    stats: ScrapeStats | None,
) -> str:
    if not href:
        return ""
    stripped = href.strip()
    lowered = stripped.lower()
    if lowered in {"#", ""} or lowered.startswith(BAD_URL_PREFIXES):
        _record_rejected_url(stats, field, "non_http_or_placeholder")
        logger.warning(
            "dropped %s URL for row %s: rejected non-http/placeholder href=%r",
            field,
            row_id,
            href,
        )
        return ""

    absolute = urljoin(base_url, stripped)
    parsed = urlparse(absolute)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        _record_rejected_url(stats, field, "bad_scheme")
        logger.warning(
            "dropped %s URL for row %s: disallowed scheme in %r",
            field,
            row_id,
            absolute,
        )
        return ""
    if host not in ALLOWED_EMIT_HOSTS:
        _record_rejected_url(stats, field, f"bad_host:{host}")
        logger.warning(
            "dropped %s URL for row %s: disallowed host %r in %r",
            field,
            row_id,
            host,
            absolute,
        )
        return ""
    return absolute


def _record_rejected_url(stats: ScrapeStats | None, field: str, reason: str) -> None:
    if stats is not None:
        stats.rejected_urls[f"{field}:{reason}"] += 1


def _clean_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", unescape(value))
    return re.sub(r"\s+", " ", text).strip()


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _validate_schema(meeting: dict[str, str]) -> None:
    keys = tuple(meeting.keys())
    if keys != CANONICAL_FIELDS:
        raise ValueError(f"Schema keys mismatch: {keys}")
    for field_name, value in meeting.items():
        if not isinstance(value, str):
            raise TypeError(f"{field_name} must be str, got {type(value).__name__}")
    for field_name in (
        "agenda_url",
        "minutes_url",
        "video_url",
        "agenda_packet_url",
        "ecomment_url",
    ):
        value = meeting[field_name]
        if value and not value.startswith(("http://", "https://")):
            raise ValueError(f"{field_name} is not absolute http(s): {value!r}")
    if meeting["meeting_status"] not in {
        "Scheduled",
        "Agenda Available",
        "Minutes Available",
        "Cancelled",
    }:
        raise ValueError(f"Unexpected meeting_status={meeting['meeting_status']!r}")
    if meeting["meeting_date"] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", meeting["meeting_date"]):
        raise ValueError(f"meeting_date is not ISO: {meeting['meeting_date']!r}")


def _log_stats(stats: ScrapeStats, emitted_count: int, years: list[str]) -> None:
    logger.warning(
        "PrimeGov meetingState vendor vocabulary observed=%s; canonical meeting_status derived from title/document evidence, not meetingState",
        dict(stats.meeting_states),
    )
    if stats.unclassified_documents:
        logger.warning(
            "unclassified PrimeGov document labels dropped count=%d first_10=%s",
            len(stats.unclassified_documents),
            stats.unclassified_documents[:10],
        )
    if stats.rejected_urls:
        logger.warning("rejected URL summary=%s", dict(stats.rejected_urls))
    if stats.public_comment_flags or stats.public_speaker_flags or stats.zoom_flags:
        logger.warning(
            "public option/zoom signals without emitted URL: comments=%d speakers=%d zoom=%d",
            stats.public_comment_flags,
            stats.public_speaker_flags,
            stats.zoom_flags,
        )
    logger.info(
        "reno_parser summary emitted=%d rows_seen=%d rows_accepted=%d rows_dropped=%d years=%s api_counts=%s drop_reasons=%s field_absences=%s document_labels=%s compile_output_types=%s duplicate_ids=%s",
        emitted_count,
        stats.rows_seen,
        stats.rows_accepted,
        stats.rows_dropped,
        years,
        stats.api_counts,
        dict(stats.drop_reasons),
        dict(stats.field_absences),
        dict(stats.document_labels),
        dict(stats.compile_output_types),
        stats.duplicate_ids[:20],
    )


__all__ = ["scrape_calendar"]


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s:%(name)s:%(message)s")
    rows = scrape_calendar(DEFAULT_URL)
    print(f"Reno meetings extracted: {len(rows)}")
