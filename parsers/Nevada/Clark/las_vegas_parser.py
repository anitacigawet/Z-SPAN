"""Las Vegas — PrimeGov meeting parser."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from html import unescape
import json
import logging
from pathlib import Path
import re
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import HTTPSHandler, Request, build_opener, urlopen


DEFAULT_URL = "https://lasvegas.primegov.com/public/portal"
BASE_URL = "https://lasvegas.primegov.com"
ARCHIVED_YEARS_PATH = "/api/v2/PublicPortal/GetArchivedMeetingYears"
ARCHIVED_MEETINGS_PATH = "/api/v2/PublicPortal/ListArchivedMeetings"
UPCOMING_MEETINGS_PATH = "/api/v2/PublicPortal/ListUpcomingMeetings"
ALLOWED_HOSTS = {
    "lasvegas.primegov.com",
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
}
PRIMEGOV_HOST = "lasvegas.primegov.com"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
MAX_RESPONSE_BYTES = 5_000_000
REQUEST_TIMEOUT = 30
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
BAD_URL_PREFIXES = (
    "javascript:",
    "data:",
    "vbscript:",
    "file:",
    "mailto:",
    "ftp:",
    "gopher:",
)
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)

logger = logging.getLogger(__name__)


@dataclass
class ScrapeStats:
    source_counts: Counter[str] = field(default_factory=Counter)
    rows_seen: int = 0
    rows_accepted: int = 0
    duplicate_meetings: Counter[str] = field(default_factory=Counter)
    field_absences: Counter[str] = field(default_factory=Counter)
    field_absence_samples: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    rejected_urls: Counter[str] = field(default_factory=Counter)
    vendor_states: Counter[str] = field(default_factory=Counter)
    document_names: Counter[str] = field(default_factory=Counter)
    unsupported_documents: Counter[str] = field(default_factory=Counter)
    unknown_document_output_types: Counter[str] = field(default_factory=Counter)
    dropped_controls: Counter[str] = field(default_factory=Counter)


def scrape_calendar(url: str = DEFAULT_URL) -> list[dict[str, str]]:
    """Scrape Las Vegas public meetings from PrimeGov archive and upcoming APIs."""
    _validate_input_host(url)
    stats = ScrapeStats()

    portal_html = _fetch_text_bounded(_portal_url(url), accept="text/html")
    _validate_primegov_portal_surface(portal_html)

    years = _fetch_archive_years(stats)
    raw_rows = _fetch_all_meeting_rows(years, stats)

    meetings_by_id: dict[str, dict[str, str]] = {}
    for source, raw_row in raw_rows:
        stats.rows_seen += 1
        if not isinstance(raw_row, dict):
            logger.warning(
                "dropped non-object PrimeGov row from source=%s type=%s",
                source,
                type(raw_row).__name__,
            )
            continue

        meeting = _build_meeting(raw_row, source, stats)
        meeting_id = meeting["meeting_id"]
        if not meeting_id:
            raise ValueError(f"PrimeGov row missing id after parsing: {raw_row!r}")

        if meeting_id in meetings_by_id:
            stats.duplicate_meetings[meeting_id] += 1
            meetings_by_id[meeting_id] = _prefer_more_complete_row(meetings_by_id[meeting_id], meeting)
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
            "emitted meeting id=%s source=%s date=%s time=%s title=%r status=%s agenda=%r packet=%r minutes=%r video=%r",
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
    _log_stats(stats, len(meetings))
    return meetings


def _validate_input_host(url: str) -> None:
    host = (urlparse(url).hostname or "").lower()
    if host != PRIMEGOV_HOST:
        raise ValueError(f"Expected Las Vegas PrimeGov host {PRIMEGOV_HOST!r}, got {host!r}")


def _portal_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme or 'https'}://{PRIMEGOV_HOST}/public/portal"


def _fetch_archive_years(stats: ScrapeStats) -> list[int]:
    api_url = urljoin(BASE_URL, ARCHIVED_YEARS_PATH)
    text = _fetch_text_bounded(api_url, accept="application/json")
    data = json.loads(text)
    if not isinstance(data, list) or not all(isinstance(year, int) for year in data):
        raise ValueError(f"Unexpected PrimeGov archived-years payload: {data!r}")
    if not data:
        raise ValueError("PrimeGov archived-years payload was empty")

    years = sorted(set(data), reverse=True)
    stats.source_counts["archive_years_returned"] = len(years)
    logger.info("PrimeGov archived years discovered: %s", years)
    return years


def _fetch_all_meeting_rows(
    years: list[int],
    stats: ScrapeStats,
) -> list[tuple[str, dict]]:
    rows: list[tuple[str, dict]] = []
    for year in years:
        params = urlencode({"year": str(year)})
        api_url = urljoin(BASE_URL, ARCHIVED_MEETINGS_PATH) + f"?{params}"
        try:
            year_rows = _fetch_meeting_list(api_url, f"archived:{year}")
        except (HTTPError, TimeoutError, URLError) as exc:
            logger.warning("PrimeGov archive fetch failed for year=%s url=%s: %s", year, api_url, exc)
            continue
        if not year_rows:
            logger.warning(
                "PrimeGov archive year returned zero rows despite year-list discovery: year=%s url=%s",
                year,
                api_url,
            )
        stats.source_counts[f"archived:{year}"] = len(year_rows)
        logger.info("fetched PrimeGov archive year=%s rows=%d", year, len(year_rows))
        rows.extend((f"archived:{year}", row) for row in year_rows)

    upcoming_url = urljoin(BASE_URL, UPCOMING_MEETINGS_PATH)
    try:
        upcoming_rows = _fetch_meeting_list(upcoming_url, "upcoming")
    except (HTTPError, TimeoutError, URLError) as exc:
        logger.warning("PrimeGov upcoming fetch failed url=%s: %s", upcoming_url, exc)
        upcoming_rows = []
    stats.source_counts["upcoming"] = len(upcoming_rows)
    logger.info("fetched PrimeGov upcoming rows=%d", len(upcoming_rows))
    rows.extend(("upcoming", row) for row in upcoming_rows)
    return rows


def _fetch_meeting_list(api_url: str, source: str) -> list[dict]:
    text = _fetch_text_bounded(api_url, accept="application/json")
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError(f"Unexpected PrimeGov {source} payload: {type(data).__name__}")
    _validate_primegov_api_shape(data, source)
    return [row for row in data if isinstance(row, dict)]


def _fetch_text_bounded(url: str, *, accept: str) -> str:
    last_exc: HTTPError | TimeoutError | URLError | None = None
    for attempt in range(1, 3):
        try:
            request = Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": accept,
                },
            )
            with _open_verified(request) as response:
                _validate_response_host(response.geturl(), url)
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
                return b"".join(chunks).decode(encoding, errors="replace")
        except (HTTPError, TimeoutError, URLError) as exc:
            last_exc = exc
            logger.warning("PrimeGov fetch attempt=%d failed url=%s: %s", attempt, url, exc)
            if attempt == 1:
                time.sleep(1)
    assert last_exc is not None
    raise last_exc


def _open_verified(request: Request):
    if Path("/etc/ssl/cert.pem").exists():
        context = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
        return build_opener(HTTPSHandler(context=context)).open(request, timeout=REQUEST_TIMEOUT)
    return urlopen(request, timeout=REQUEST_TIMEOUT)


def _validate_response_host(final_url: str, started_url: str) -> None:
    host = (urlparse(final_url).hostname or "").lower()
    if host != PRIMEGOV_HOST:
        raise ValueError(f"PrimeGov redirect to disallowed host: {host!r} (started from {started_url})")


def _validate_primegov_portal_surface(html: str) -> None:
    markers = (
        "upcomingMeetingsTable",
        "archivedMeetingsTable",
        "/Scripts/Custom/Public/_Archived.js",
        "/Scripts/Custom/Public/_Upcoming.js",
    )
    missing = [marker for marker in markers if marker not in html]
    if missing:
        raise ValueError(f"Las Vegas portal no longer matches witnessed PrimeGov surface: {missing}")
    logger.info("validated PrimeGov portal surface markers=%s", markers)


def _validate_primegov_api_shape(rows: list[object], source: str) -> None:
    if not rows:
        logger.info("PrimeGov API source=%s returned an empty list", source)
        return
    first = rows[0]
    if not isinstance(first, dict):
        raise ValueError(f"PrimeGov API source={source} first row is {type(first).__name__}")
    required = {"id", "dateTime", "date", "time", "documentList", "title", "meetingState"}
    missing = sorted(required - set(first))
    if missing:
        raise ValueError(f"PrimeGov API source={source} missing expected fields: {missing}")
    if not isinstance(first.get("documentList"), list):
        raise ValueError(f"PrimeGov API source={source} documentList was not a list")


def _build_meeting(raw_row: dict, source: str, stats: ScrapeStats) -> dict[str, str]:
    meeting_id = _clean_text(str(raw_row.get("id") or ""))
    title = _clean_text(str(raw_row.get("title") or ""))
    if not title:
        raise ValueError(f"PrimeGov row id={meeting_id!r} source={source} missing title")

    meeting_date, meeting_time = _date_and_time(raw_row, meeting_id)
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
    document_flags = _assign_document_urls(raw_row, meeting_id, urls, stats)
    _assign_video_url(raw_row, meeting_id, urls, stats)
    _inspect_public_comment_controls(raw_row, meeting_id, urls, stats)

    vendor_state = _clean_text(str(raw_row.get("meetingState") or ""))
    if vendor_state:
        stats.vendor_states[vendor_state] += 1
        if vendor_state != "3":
            logger.warning(
                "unmapped PrimeGov meetingState row=%s state=%r; status will use document/title evidence",
                meeting_id,
                vendor_state,
            )

    status = _status_from_evidence(title, urls, document_flags, meeting_id)
    meeting = {
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
    _validate_schema(meeting)
    return meeting


def _date_and_time(raw_row: dict, meeting_id: str) -> tuple[str, str]:
    raw_datetime = _clean_text(str(raw_row.get("dateTime") or ""))
    if not raw_datetime:
        raise ValueError(f"PrimeGov row id={meeting_id} missing dateTime")

    parsed = datetime.fromisoformat(raw_datetime)
    date_from_datetime = parsed.strftime("%Y-%m-%d")
    time_from_datetime = _format_time(parsed)

    raw_date = _clean_text(str(raw_row.get("date") or ""))
    if raw_date:
        parsed_date = datetime.strptime(raw_date, "%b %d, %Y").strftime("%Y-%m-%d")
        if parsed_date != date_from_datetime:
            logger.warning(
                "PrimeGov date mismatch row=%s dateTime=%r date=%r parsed_date=%s",
                meeting_id,
                raw_datetime,
                raw_date,
                parsed_date,
            )

    raw_time = _clean_text(str(raw_row.get("time") or ""))
    if raw_time:
        parsed_time = _normalize_time(raw_time, meeting_id)
        if parsed_time != time_from_datetime:
            logger.warning(
                "PrimeGov time mismatch row=%s dateTime=%r time=%r parsed_time=%s",
                meeting_id,
                raw_datetime,
                raw_time,
                parsed_time,
            )
    else:
        logger.warning("PrimeGov row=%s missing visible time field; using dateTime time signal", meeting_id)

    return date_from_datetime, time_from_datetime


def _format_time(value: datetime) -> str:
    return value.strftime("%I:%M %p").lstrip("0")


def _normalize_time(value: str, meeting_id: str) -> str:
    try:
        return datetime.strptime(value, "%I:%M %p").strftime("%I:%M %p").lstrip("0")
    except ValueError as exc:
        raise ValueError(f"PrimeGov row {meeting_id} had unparsable time {value!r}") from exc


def _assign_document_urls(
    raw_row: dict,
    meeting_id: str,
    urls: dict[str, str],
    stats: ScrapeStats,
) -> dict[str, bool]:
    document_list = raw_row.get("documentList")
    if not isinstance(document_list, list):
        raise ValueError(f"PrimeGov row id={meeting_id} documentList is not a list")
    if not document_list:
        _record_field_absence(stats, "documents", meeting_id, "empty PrimeGov documentList")

    flags = {"cancellation_document": False}
    for document in document_list:
        if not isinstance(document, dict):
            logger.warning("dropped non-object document row=%s type=%s", meeting_id, type(document).__name__)
            continue
        name = _clean_text(str(document.get("templateName") or ""))
        stats.document_names[name or "<empty>"] += 1
        if not name:
            logger.warning("dropped PrimeGov document with empty templateName row=%s doc=%r", meeting_id, document)
            continue

        if "cancellation" in name.lower():
            flags["cancellation_document"] = True

        output_type = document.get("compileOutputType")
        if output_type not in {1, 2, 3}:
            stats.unknown_document_output_types[str(output_type)] += 1
            logger.warning(
                "PrimeGov document has unknown compileOutputType row=%s name=%r output_type=%r",
                meeting_id,
                name,
                output_type,
            )

        field_name = _document_field(name)
        if not field_name:
            stats.unsupported_documents[name] += 1
            logger.warning(
                "dropped unsupported PrimeGov document row=%s name=%r doc_id=%r reason=no canonical URL field",
                meeting_id,
                name,
                document.get("id"),
            )
            continue

        href = _document_href(document)
        emitted = _emit_url(href, BASE_URL, field_name, meeting_id, stats)
        if not emitted:
            continue

        existing = urls[field_name]
        if not existing:
            urls[field_name] = emitted
            continue

        if _document_priority(name) > _url_priority(existing):
            logger.info(
                "replaced lower-priority %s row=%s kept=%r dropped=%r name=%r",
                field_name,
                meeting_id,
                emitted,
                existing,
                name,
            )
            urls[field_name] = emitted
        else:
            logger.info(
                "dropped alternate %s row=%s kept=%r dropped=%r name=%r",
                field_name,
                meeting_id,
                existing,
                emitted,
                name,
            )
    return flags


def _document_field(name: str) -> str:
    normalized = name.strip().lower()
    if normalized in {"agenda", "html agenda"}:
        return "agenda_url"
    if normalized in {"packet", "agenda packet"}:
        return "agenda_packet_url"
    if normalized in {"minutes", "action minutes", "minutes packet"}:
        return "minutes_url"
    return ""


def _document_priority(name: str) -> int:
    normalized = name.strip().lower()
    if normalized in {"agenda", "packet", "agenda packet", "minutes", "action minutes", "minutes packet"}:
        return 2
    if normalized.startswith("html "):
        return 1
    return 0


def _url_priority(url: str) -> int:
    parsed = urlparse(url)
    if parsed.path.lower().startswith("/public/compileddocument"):
        return 2
    if parsed.path.lower().startswith("/portal/meeting"):
        return 1
    return 0


def _document_href(document: dict) -> str:
    direct_link = document.get("link")
    if isinstance(direct_link, str) and direct_link.strip():
        return direct_link.strip()

    template_id = _to_int(document.get("templateId"))
    document_id = _to_int(document.get("id"))
    output_type = _to_int(document.get("compileOutputType"))
    if template_id and template_id > 0:
        meeting_param = urlencode({"meetingTemplateId": str(template_id)})
    elif document_id and document_id > 0:
        meeting_param = urlencode({"compiledMeetingDocumentFileId": str(document_id)})
    else:
        return ""

    if output_type == 3:
        return f"/Portal/Meeting?{meeting_param}"
    return f"/Public/CompiledDocument?{meeting_param}&{urlencode({'compileOutputType': str(output_type)})}"


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
            "PrimeGov row=%s exposed video icon but videoUrl was empty; emitted empty video_url",
            meeting_id,
        )


def _inspect_public_comment_controls(
    raw_row: dict,
    meeting_id: str,
    urls: dict[str, str],
    stats: ScrapeStats,
) -> None:
    if raw_row.get("allowPublicComment"):
        stats.dropped_controls["allowPublicComment"] += 1
        logger.warning(
            "PrimeGov row=%s has allowPublicComment=true but API exposes modal control, not stable ecomment URL",
            meeting_id,
        )
    if raw_row.get("allowPublicSpeaker"):
        stats.dropped_controls["allowPublicSpeaker"] += 1
        logger.warning(
            "PrimeGov row=%s has allowPublicSpeaker=true but API exposes modal control, not canonical URL",
            meeting_id,
        )
    if raw_row.get("zoomMeetingLink") and not urls["video_url"]:
        stats.dropped_controls["zoomMeetingLink"] += 1
        logger.warning(
            "PrimeGov row=%s has zoomMeetingLink=%r; not emitted as video_url because it is a live access link",
            meeting_id,
            raw_row.get("zoomMeetingLink"),
        )


def _status_from_evidence(
    title: str,
    urls: dict[str, str],
    document_flags: dict[str, bool],
    meeting_id: str,
) -> str:
    if CANCELLED_RE.search(title[:300]):
        return "Cancelled"
    if document_flags.get("cancellation_document"):
        logger.warning(
            "emitting Cancelled for row=%s from PrimeGov Notice of Cancellation document; title lacks cancellation text",
            meeting_id,
        )
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
    stats: ScrapeStats,
) -> str:
    if not href:
        return ""
    stripped = href.strip()
    lowered = stripped.lower()
    if lowered in {"#", ""} or lowered.startswith(BAD_URL_PREFIXES):
        stats.rejected_urls[f"{field}:bad_scheme_or_placeholder"] += 1
        logger.warning("dropped %s row=%s href=%r reason=bad_scheme_or_placeholder", field, row_id, href)
        return ""

    absolute = urljoin(base_url, stripped)
    parsed = urlparse(absolute)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        stats.rejected_urls[f"{field}:bad_scheme"] += 1
        logger.warning("dropped %s row=%s href=%r reason=bad_scheme", field, row_id, absolute)
        return ""
    if host not in ALLOWED_HOSTS:
        stats.rejected_urls[f"{field}:disallowed_host:{host}"] += 1
        logger.warning(
            "dropped %s row=%s href=%r reason=disallowed_host host=%r",
            field,
            row_id,
            absolute,
            host,
        )
        return ""
    return absolute


def _prefer_more_complete_row(existing: dict[str, str], candidate: dict[str, str]) -> dict[str, str]:
    existing_score = sum(1 for value in existing.values() if value)
    candidate_score = sum(1 for value in candidate.values() if value)
    return candidate if candidate_score > existing_score else existing


def _record_field_absence(stats: ScrapeStats, field_name: str, meeting_id: str, reason: str) -> None:
    stats.field_absences[f"{field_name}:{reason}"] += 1
    samples = stats.field_absence_samples[field_name]
    if len(samples) < 10:
        samples.append(meeting_id)


def _clean_text(value: object) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value))
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _to_int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _validate_schema(meeting: dict[str, str]) -> None:
    keys = tuple(meeting)
    if keys != CANONICAL_FIELDS:
        raise ValueError(f"Schema mismatch: expected {CANONICAL_FIELDS}, got {keys}")
    for key, value in meeting.items():
        if not isinstance(value, str):
            raise TypeError(f"{key} must be str, got {type(value).__name__}")
    for key in ("agenda_url", "minutes_url", "video_url", "agenda_packet_url", "ecomment_url"):
        value = meeting[key]
        if value and urlparse(value).scheme not in {"http", "https"}:
            raise ValueError(f"{key} is not an absolute http(s) URL: {value!r}")


def _log_stats(stats: ScrapeStats, emitted_count: int) -> None:
    logger.info(
        "las_vegas_parser summary emitted=%d rows_seen=%d rows_accepted=%d source_counts=%s vendor_states=%s",
        emitted_count,
        stats.rows_seen,
        stats.rows_accepted,
        dict(stats.source_counts),
        dict(stats.vendor_states),
    )
    if stats.field_absences:
        logger.warning(
            "PrimeGov field absences summary=%s samples=%s",
            dict(stats.field_absences),
            dict(stats.field_absence_samples),
        )
    if stats.rejected_urls:
        logger.warning("PrimeGov rejected URL summary=%s", dict(stats.rejected_urls))
    if stats.unsupported_documents:
        logger.warning("PrimeGov unsupported document summary=%s", dict(stats.unsupported_documents))
    if stats.unknown_document_output_types:
        logger.warning(
            "PrimeGov unknown document output type summary=%s",
            dict(stats.unknown_document_output_types),
        )
    if stats.duplicate_meetings:
        logger.warning("PrimeGov duplicate meeting summary=%s", dict(stats.duplicate_meetings))
    if stats.dropped_controls:
        logger.warning("PrimeGov dropped control summary=%s", dict(stats.dropped_controls))
    logger.info("PrimeGov document name summary=%s", dict(stats.document_names))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    rows = scrape_calendar(DEFAULT_URL)
    print(f"Las Vegas PrimeGov meetings extracted: {len(rows)}")
