"""Boulder City — PrimeGov meeting parser."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
import json
import logging
from pathlib import Path
import re
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import HTTPSHandler, Request, build_opener


logger = logging.getLogger(__name__)

DEFAULT_URL = "https://bcnv.primegov.com/public/portal"
BASE_URL = "https://bcnv.primegov.com"
PRIMEGOV_HOST = "bcnv.primegov.com"
ARCHIVED_MEETINGS_PATH = "/api/v2/PublicPortal/ListArchivedMeetings"
UPCOMING_MEETINGS_PATH = "/api/v2/PublicPortal/ListUpcomingMeetings"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
MAX_RESPONSE_BYTES = 8_000_000
REQUEST_TIMEOUT = 30
MIN_POPULATED_ARCHIVE_YEARS = 4
MAX_ARCHIVE_YEAR_SPAN = 12
ALLOWED_FETCH_HOSTS = {PRIMEGOV_HOST}
ALLOWED_EMIT_HOSTS = {
    PRIMEGOV_HOST,
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
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
VACATED_RE = re.compile(r"\bvacat(?:e|ed|ion)\b", re.IGNORECASE)
TIME_RE = re.compile(
    r"^\s*(\d{1,2})(?::(\d{2}))?\s*([AP])\.?M\.?\s*$",
    re.IGNORECASE,
)
EXPECTED_MEETING_KEYS = {
    "allowPublicComment",
    "allowPublicSpeaker",
    "committeeId",
    "date",
    "dateTime",
    "documentList",
    "endDateTime",
    "endTime",
    "externalProviderMeetingId",
    "id",
    "isMediaManagerVideo",
    "isShowVideoIcon",
    "isZoomMeeting",
    "location",
    "meetingOnline",
    "meetingState",
    "meetingTypeId",
    "publishDate",
    "streamCompleted",
    "swagitId",
    "time",
    "title",
    "videoUrl",
    "zoomMeetingLink",
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
    source_counts: Counter[str] = field(default_factory=Counter)
    rows_seen: int = 0
    rows_accepted: int = 0
    rows_dropped: int = 0
    drop_reasons: Counter[str] = field(default_factory=Counter)
    duplicate_ids: Counter[str] = field(default_factory=Counter)
    field_absences: Counter[str] = field(default_factory=Counter)
    field_absence_samples: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    rejected_urls: Counter[str] = field(default_factory=Counter)
    meeting_states: Counter[str] = field(default_factory=Counter)
    emitted_statuses: Counter[str] = field(default_factory=Counter)
    document_labels: Counter[str] = field(default_factory=Counter)
    compile_output_types: Counter[str] = field(default_factory=Counter)
    unsupported_documents: Counter[str] = field(default_factory=Counter)
    public_comment_flags: int = 0
    public_speaker_flags: int = 0
    zoom_flags: int = 0


def scrape_calendar(url: str = DEFAULT_URL) -> list[dict[str, str]]:
    """Scrape Boulder City public meetings from PrimeGov archive and upcoming APIs."""
    _validate_input_url(url)
    stats = ScrapeStats()
    logger.warning(
        "primegov_public_portal_no_standalone_ecomment_url: public comment/speaker controls are modal/API flags; ecomment_url empties are counted per run"
    )

    portal_html = _fetch_text_bounded(_portal_url(url), accept="text/html")
    script_url = _validate_primegov_portal_surface(portal_html)
    script_text = _fetch_text_bounded(script_url, accept="application/javascript,*/*")
    _validate_document_route_pattern(script_text, script_url)

    raw_rows, years = _fetch_all_meeting_rows(stats)

    meetings_by_id: dict[str, dict[str, str]] = {}
    for source, raw_row in raw_rows:
        stats.rows_seen += 1
        if not isinstance(raw_row, dict):
            stats.rows_dropped += 1
            stats.drop_reasons[f"{source}:non_object_row"] += 1
            logger.warning(
                "dropped non-object PrimeGov row from source=%s type=%s",
                source,
                type(raw_row).__name__,
            )
            continue

        meeting = _build_meeting(raw_row, source, stats)
        meeting_id = meeting["meeting_id"]
        if not meeting_id:
            stats.rows_dropped += 1
            stats.drop_reasons["missing_meeting_id"] += 1
            logger.warning(
                "dropped PrimeGov row with missing id: source=%s title=%r dateTime=%r",
                source,
                raw_row.get("title"),
                raw_row.get("dateTime"),
            )
            continue
        if not meeting["meeting_date"]:
            stats.rows_dropped += 1
            stats.drop_reasons["missing_meeting_date"] += 1
            logger.warning(
                "dropped PrimeGov row id=%s source=%s: no valid same-row date evidence",
                meeting_id,
                source,
            )
            continue
        if meeting_id in meetings_by_id:
            stats.rows_dropped += 1
            stats.drop_reasons["duplicate_meeting_id"] += 1
            stats.duplicate_ids[meeting_id] += 1
            meetings_by_id[meeting_id] = _prefer_more_complete_row(
                meetings_by_id[meeting_id],
                meeting,
            )
            logger.warning(
                "deduped PrimeGov meeting id=%s from source=%s title=%r",
                meeting_id,
                source,
                meeting["meeting_title"],
            )
            continue

        _validate_schema(meeting)
        meetings_by_id[meeting_id] = meeting
        stats.rows_accepted += 1
        logger.info(
            "emitted meeting id=%s source=%s date=%s time=%r title=%r status=%s agenda=%r packet=%r minutes=%r video=%r",
            meeting["meeting_id"],
            source,
            meeting["meeting_date"],
            meeting["meeting_time"],
            meeting["meeting_title"],
            meeting["meeting_status"],
            meeting["agenda_url"],
            meeting["agenda_packet_url"],
            meeting["minutes_url"],
            meeting["video_url"],
        )

    meetings = sorted(
        meetings_by_id.values(),
        key=lambda row: (row["meeting_date"], row["meeting_time"], row["meeting_id"]),
    )
    _log_stats(stats, len(meetings), years)
    return meetings


def _validate_input_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.hostname != PRIMEGOV_HOST:
        raise ValueError(f"Expected Boulder City PrimeGov host {PRIMEGOV_HOST!r}, got {parsed.hostname!r}")
    if parsed.scheme != "https":
        raise ValueError(f"Expected https PrimeGov URL, got {url!r}")


def _portal_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{PRIMEGOV_HOST}/public/portal"


def _fetch_all_meeting_rows(stats: ScrapeStats) -> tuple[list[tuple[str, object]], list[int]]:
    rows: list[tuple[str, object]] = []
    populated_years: list[int] = []
    current_year = datetime.now().year

    for year in range(current_year, current_year - MAX_ARCHIVE_YEAR_SPAN - 1, -1):
        api_url = urljoin(BASE_URL, ARCHIVED_MEETINGS_PATH) + f"?{urlencode({'year': str(year)})}"
        try:
            year_rows = _fetch_meeting_list(api_url, f"archived:{year}")
        except (HTTPError, RuntimeError, TimeoutError, URLError) as exc:
            logger.warning("PrimeGov archive fetch failed for year=%s url=%s: %s", year, api_url, exc)
            continue
        if not year_rows:
            logger.warning(
                "PrimeGov archive year returned zero rows: year=%s url=%s state=%s",
                year,
                api_url,
                "post-populated-empty" if populated_years else "pre-populated-empty",
            )
            if populated_years:
                break
            continue
        populated_years.append(year)
        stats.source_counts[f"archived:{year}"] = len(year_rows)
        logger.info("fetched PrimeGov archive year=%s rows=%d", year, len(year_rows))
        rows.extend((f"archived:{year}", row) for row in year_rows)

    if len(populated_years) < MIN_POPULATED_ARCHIVE_YEARS:
        raise ValueError(
            "PrimeGov archive sweep found fewer than "
            f"{MIN_POPULATED_ARCHIVE_YEARS} populated years via {ARCHIVED_MEETINGS_PATH}: "
            f"{populated_years}"
        )
    if populated_years and min(populated_years) == current_year - MAX_ARCHIVE_YEAR_SPAN:
        logger.warning(
            "PrimeGov archive sweep hit MAX_ARCHIVE_YEAR_SPAN=%d without a post-populated empty year; oldest_year=%s",
            MAX_ARCHIVE_YEAR_SPAN,
            min(populated_years),
        )

    upcoming_url = urljoin(BASE_URL, UPCOMING_MEETINGS_PATH)
    try:
        upcoming_rows = _fetch_meeting_list(upcoming_url, "upcoming")
    except (HTTPError, RuntimeError, TimeoutError, URLError) as exc:
        logger.warning("PrimeGov upcoming fetch failed url=%s: %s", upcoming_url, exc)
        upcoming_rows = []
    stats.source_counts["upcoming"] = len(upcoming_rows)
    logger.info("fetched PrimeGov upcoming rows=%d", len(upcoming_rows))
    rows.extend(("upcoming", row) for row in upcoming_rows)
    return rows, populated_years


def _fetch_meeting_list(api_url: str, source: str) -> list[object]:
    data = _fetch_json(api_url, source)
    if not isinstance(data, list):
        raise ValueError(f"Unexpected PrimeGov {source} payload: {type(data).__name__}")
    _validate_primegov_api_shape(data, source)
    return data


def _fetch_json(url: str, source: str):
    text = _fetch_text_bounded(url, accept="application/json")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"PrimeGov {source} response was not valid JSON from {url}") from exc


def _fetch_text_bounded(url: str, *, accept: str) -> str:
    _validate_fetch_host(url)
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": accept,
        },
    )
    try:
        with _http_opener().open(request, timeout=REQUEST_TIMEOUT) as response:
            final_url = response.geturl()
            _validate_fetch_host(final_url)
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


def _validate_primegov_portal_surface(html: str) -> str:
    required_markers = (
        'id="upcomingMeetingsTable"',
        'id="archivedMeetingsTable"',
        "primegov_logo",
        "/Scripts/Custom/Public/_Archived.js",
    )
    missing = [marker for marker in required_markers if marker not in html]
    if missing:
        raise ValueError(f"Boulder City portal no longer matches PrimeGov public portal markers: {missing}")

    parser = ScriptSrcParser()
    parser.feed(html)
    script_src = next((src for src in parser.script_sources if "_Upcoming.js" in src), "")
    if not script_src:
        raise ValueError("Boulder City PrimeGov portal missing _Upcoming.js script reference")
    script_url = _emit_url(script_src, BASE_URL, "vendor_script", "portal", None)
    if not script_url:
        raise ValueError(f"Rejected PrimeGov _Upcoming.js script URL: {script_src!r}")
    logger.info(
        "validated PrimeGov portal fingerprint: upcoming/archived tables, primegov_logo, script=%s",
        script_url,
    )
    return script_url


def _validate_document_route_pattern(script_text: str, script_url: str) -> None:
    required_markers = (
        "function getHref(doc)",
        "/Public/CompiledDocument?",
        "/Portal/Meeting?",
        "meetingTemplateId=",
        "compiledMeetingDocumentFileId=",
    )
    missing = [marker for marker in required_markers if marker not in script_text]
    if missing:
        raise ValueError(f"PrimeGov document-route helper changed in {script_url}: {missing}")
    logger.info(
        "validated PrimeGov document route pattern from %s: stable same-host compiled document routes are present",
        script_url,
    )


def _validate_primegov_api_shape(rows: list[object], source: str) -> None:
    if not rows:
        logger.info("PrimeGov %s payload was an empty list", source)
        return
    first_object = next((row for row in rows if isinstance(row, dict)), None)
    if first_object is None:
        logger.warning("PrimeGov %s payload had %d rows but no object rows", source, len(rows))
        return
    missing_keys = sorted(EXPECTED_MEETING_KEYS - set(first_object))
    if missing_keys:
        raise ValueError(f"PrimeGov {source} row missing expected keys: {missing_keys}")
    logger.info(
        "validated PrimeGov %s API fingerprint: rows=%d keys include id/dateTime/documentList/templateName shape",
        source,
        len(rows),
    )


def _build_meeting(raw_row: dict, source: str, stats: ScrapeStats) -> dict[str, str]:
    meeting_id = _clean_text(str(raw_row.get("id") or ""))
    title = _clean_text(str(raw_row.get("title") or ""))
    if not title:
        _record_field_absence(stats, "meeting_title", meeting_id, "empty PrimeGov title")
        logger.warning("PrimeGov row id=%s source=%s has empty title", meeting_id, source)

    meeting_date, meeting_time = _extract_date_time(raw_row, meeting_id, stats)
    location = _clean_text(str(raw_row.get("location") or ""))
    if not location:
        _record_field_absence(stats, "meeting_location", meeting_id, "empty PrimeGov location")

    urls = {
        "agenda_url": "",
        "minutes_url": "",
        "video_url": "",
        "agenda_packet_url": "",
        "ecomment_url": "",
    }
    flags = _assign_document_urls(raw_row, meeting_id, urls, stats)
    _assign_video_url(raw_row, meeting_id, urls, stats)
    _inspect_public_controls(raw_row, meeting_id, stats)
    status = _status_from_evidence(title, urls, flags, meeting_id, stats)
    stats.emitted_statuses[status] += 1

    meeting_state = _clean_text(str(raw_row.get("meetingState") or ""))
    if meeting_state:
        stats.meeting_states[meeting_state] += 1

    for field_name, field_value in urls.items():
        if not field_value:
            _record_field_absence(stats, field_name, meeting_id, "no same-row URL evidence emitted")

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


def _extract_date_time(raw_row: dict, meeting_id: str, stats: ScrapeStats) -> tuple[str, str]:
    datetime_value = _clean_text(str(raw_row.get("dateTime") or ""))
    date_value = _clean_text(str(raw_row.get("date") or ""))
    time_value = _clean_text(str(raw_row.get("time") or ""))

    parsed_dt: datetime | None = None
    if datetime_value:
        try:
            parsed_dt = datetime.fromisoformat(datetime_value)
        except ValueError:
            logger.warning("meeting id=%s has unparsable dateTime=%r", meeting_id, datetime_value)
    else:
        logger.warning("meeting id=%s has empty dateTime field", meeting_id)

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
                _record_field_absence(stats, "meeting_date", meeting_id, "date/dateTime conflict")
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
        _record_field_absence(stats, "meeting_time", meeting_id, "time/dateTime conflict")
        return iso_date, ""
    if api_time:
        return iso_date, api_time
    if datetime_time:
        return iso_date, datetime_time

    _record_field_absence(stats, "meeting_time", meeting_id, "no PrimeGov time/dateTime signal")
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
        _record_field_absence(stats, "meeting_time", meeting_id, f"unparsable {source_field}")
        return ""

    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    suffix = match.group(3).upper()
    if hour < 1 or hour > 12 or minute > 59:
        logger.warning("meeting id=%s has out-of-range %s value=%r", meeting_id, source_field, value)
        _record_field_absence(stats, "meeting_time", meeting_id, f"out-of-range {source_field}")
        return ""
    return f"{hour}:{minute:02d} {suffix}M"


def _format_time(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%I:%M %p").lstrip("0")


def _assign_document_urls(
    raw_row: dict,
    meeting_id: str,
    urls: dict[str, str],
    stats: ScrapeStats,
) -> dict[str, bool]:
    document_list = raw_row.get("documentList")
    if not isinstance(document_list, list):
        logger.warning(
            "meeting id=%s documentList was %s, expected list; document URLs left empty",
            meeting_id,
            type(document_list).__name__,
        )
        return {"cancellation_document": False, "vacate_document": False}
    if not document_list:
        _record_field_absence(stats, "documents", meeting_id, "empty PrimeGov documentList")

    flags = {"cancellation_document": False, "vacate_document": False}
    candidates: dict[str, list[tuple[int, str, str]]] = {
        "agenda_url": [],
        "minutes_url": [],
        "agenda_packet_url": [],
    }
    for document in document_list:
        if not isinstance(document, dict):
            logger.warning("meeting id=%s dropped non-object document entry=%r", meeting_id, document)
            continue

        label = _clean_text(str(document.get("templateName") or ""))
        stats.document_labels[label or "<empty>"] += 1
        output_type = _to_int(document.get("compileOutputType"))
        stats.compile_output_types[str(output_type) if output_type is not None else "<missing>"] += 1
        label_lower = label.lower()
        if "cancellation" in label_lower:
            flags["cancellation_document"] = True
        if "vacate" in label_lower:
            flags["vacate_document"] = True

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

        field_name = _document_field(label)
        if not field_name:
            stats.unsupported_documents[label or "<empty>"] += 1
            logger.warning(
                "dropped unsupported PrimeGov document row=%s label=%r doc_id=%r reason=no canonical URL field",
                meeting_id,
                label,
                document.get("id"),
            )
            continue

        href = _document_href(document, output_type, meeting_id)
        emitted = _emit_url(href, BASE_URL, field_name, meeting_id, stats)
        if not emitted:
            continue
        candidates[field_name].append((_document_priority(label, output_type), emitted, label))

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
    return flags


def _document_field(label: str) -> str:
    normalized = label.strip().lower()
    if "minute" in normalized:
        return "minutes_url"
    if "packet" in normalized:
        return "agenda_packet_url"
    if "agenda" in normalized:
        return "agenda_url"
    return ""


def _document_href(document: dict, output_type: int, meeting_id: str) -> str:
    direct_link = _clean_text(str(document.get("link") or ""))
    if direct_link:
        return direct_link

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
    if "amended" in lowered:
        priority -= 1
    if "spanish" in lowered:
        priority += 5
    if "draft" in lowered:
        priority += 5
    return priority


def _assign_video_url(
    raw_row: dict,
    meeting_id: str,
    urls: dict[str, str],
    stats: ScrapeStats,
) -> None:
    raw_video = _clean_text(str(raw_row.get("videoUrl") or ""))
    if raw_video:
        urls["video_url"] = _emit_url(raw_video, BASE_URL, "video_url", meeting_id, stats)
        return
    if raw_row.get("isShowVideoIcon"):
        logger.warning(
            "meeting id=%s has isShowVideoIcon=true but empty videoUrl; video_url left empty",
            meeting_id,
        )


def _inspect_public_controls(raw_row: dict, meeting_id: str, stats: ScrapeStats) -> None:
    if raw_row.get("allowPublicComment"):
        stats.public_comment_flags += 1
        logger.warning(
            "meeting id=%s has allowPublicComment=true but PrimeGov exposes comments through modal/API state, not a stable ecomment URL",
            meeting_id,
        )
    if raw_row.get("allowPublicSpeaker"):
        stats.public_speaker_flags += 1
        logger.warning(
            "meeting id=%s has allowPublicSpeaker=true but canonical schema has no stable speaker URL field",
            meeting_id,
        )
    if raw_row.get("isZoomMeeting") or raw_row.get("zoomMeetingLink"):
        stats.zoom_flags += 1
        logger.warning(
            "meeting id=%s has Zoom meeting signal; zoomMeetingLink is not emitted as video_url",
            meeting_id,
        )


def _status_from_evidence(
    title: str,
    urls: dict[str, str],
    flags: dict[str, bool],
    meeting_id: str,
    stats: ScrapeStats,
) -> str:
    title_slice = title[:500]
    if CANCELLED_RE.search(title_slice):
        return "Cancelled"
    if VACATED_RE.search(title_slice):
        logger.warning(
            "meeting id=%s title uses vendor vocabulary %r; mapping to canonical Cancelled",
            meeting_id,
            "vacated",
        )
        return "Cancelled"
    if flags.get("cancellation_document") or flags.get("vacate_document"):
        reason = "Notice of Cancellation" if flags.get("cancellation_document") else "Notice of Vacate"
        logger.warning(
            "meeting id=%s has PrimeGov document vocabulary %r but title lacks cancellation regex; mapping to canonical Cancelled from same-row document evidence",
            meeting_id,
            reason,
        )
        return "Cancelled"
    if urls["minutes_url"]:
        return "Minutes Available"
    if urls["agenda_url"] or urls["agenda_packet_url"]:
        return "Agenda Available"
    stats.drop_reasons["status_default_scheduled"] += 1
    return "Scheduled"


def _prefer_more_complete_row(
    existing: dict[str, str],
    candidate: dict[str, str],
) -> dict[str, str]:
    existing_score = sum(1 for value in existing.values() if value)
    candidate_score = sum(1 for value in candidate.values() if value)
    return candidate if candidate_score > existing_score else existing


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
        logger.warning("dropped %s URL for row %s: disallowed scheme in %r", field, row_id, absolute)
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


def _record_field_absence(stats: ScrapeStats, field: str, meeting_id: str, reason: str) -> None:
    stats.field_absences[f"{field}:{reason}"] += 1
    samples = stats.field_absence_samples[field]
    if len(samples) < 10:
        samples.append(meeting_id or "<missing-id>")


def _log_stats(stats: ScrapeStats, emitted_count: int, years: list[int]) -> None:
    logger.warning(
        "boulder_city_primegov_summary emitted=%d rows_seen=%d rows_accepted=%d rows_dropped=%d years=%s source_counts=%s drop_reasons=%s",
        emitted_count,
        stats.rows_seen,
        stats.rows_accepted,
        stats.rows_dropped,
        years,
        dict(stats.source_counts),
        dict(stats.drop_reasons),
    )
    logger.warning(
        "boulder_city_primegov_field_absences=%s samples=%s",
        dict(stats.field_absences),
        {key: value for key, value in stats.field_absence_samples.items()},
    )
    logger.warning(
        "boulder_city_primegov_documents labels=%s compile_output_types=%s unsupported=%s rejected_urls=%s",
        dict(stats.document_labels),
        dict(stats.compile_output_types),
        dict(stats.unsupported_documents),
        dict(stats.rejected_urls),
    )
    logger.warning(
        "boulder_city_primegov_status meeting_states=%s emitted_statuses=%s public_comment_flags=%d public_speaker_flags=%d zoom_flags=%d duplicates=%s",
        dict(stats.meeting_states),
        dict(stats.emitted_statuses),
        stats.public_comment_flags,
        stats.public_speaker_flags,
        stats.zoom_flags,
        dict(stats.duplicate_ids),
    )


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
        raise ValueError(f"Invalid meeting_status: {meeting['meeting_status']!r}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s:%(name)s:%(message)s")
    rows = scrape_calendar(DEFAULT_URL)
    print(f"scraped {len(rows)} Boulder City PrimeGov meetings")
