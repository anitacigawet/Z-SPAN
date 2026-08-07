"""North Las Vegas — PrimeGov meeting parser."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from html import unescape
import json
import logging
import re
import ssl
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import HTTPSHandler, Request, build_opener


logger = logging.getLogger(__name__)

DEFAULT_URL = "https://cityofnorthlasvegas.primegov.com/public/portal"
PRIMEGOV_HOST = "cityofnorthlasvegas.primegov.com"
API_PREFIX = f"https://{PRIMEGOV_HOST}/api/v2/PublicPortal"
ARCHIVED_YEARS_URL = f"{API_PREFIX}/GetArchivedMeetingYears"
ARCHIVED_MEETINGS_URL = f"{API_PREFIX}/ListArchivedMeetings"
UPCOMING_MEETINGS_URL = f"{API_PREFIX}/ListUpcomingMeetings"
COMPILED_FILE_URL = f"https://{PRIMEGOV_HOST}/api/Meeting/getcompiledfiledownloadurl"
ALLOWED_FETCH_HOSTS = {PRIMEGOV_HOST}
ALLOWED_EMIT_HOSTS = {
    PRIMEGOV_HOST,
    "northlasvegasnv.new.swagit.com",
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
MAX_RESPONSE_BYTES = 8_000_000
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
TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*([AP])\.?M\.?(?=\s|$)", re.IGNORECASE)
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

__all__ = ["scrape_calendar"]


@dataclass
class ScrapeStats:
    years_seen: int = 0
    years_empty: list[int] = field(default_factory=list)
    years_failed: list[int] = field(default_factory=list)
    rows_seen: int = 0
    rows_accepted: int = 0
    rows_dropped: int = 0
    drop_reasons: Counter[str] = field(default_factory=Counter)
    field_absences: Counter[str] = field(default_factory=Counter)
    document_templates: Counter[str] = field(default_factory=Counter)
    document_output_types: Counter[str] = field(default_factory=Counter)
    dropped_documents: Counter[str] = field(default_factory=Counter)
    dropped_document_samples: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    vendor_states: Counter[str] = field(default_factory=Counter)
    emitted_statuses: Counter[str] = field(default_factory=Counter)
    cancellation_notice_without_title: list[str] = field(default_factory=list)
    duplicate_ids: list[str] = field(default_factory=list)


def scrape_calendar(url: str) -> list[dict[str, str]]:
    """Scrape North Las Vegas PrimeGov archived and upcoming meetings."""
    _validate_input_url(url)
    stats = ScrapeStats()
    _validate_primegov_surface(url)
    years = _fetch_archived_years()
    _validate_years(years)

    meetings_by_id: dict[str, dict[str, str]] = {}
    for year in years:
        stats.years_seen += 1
        try:
            rows = _fetch_json(f"{ARCHIVED_MEETINGS_URL}?{urlencode({'year': str(year)})}")
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            stats.years_failed.append(year)
            logger.warning("dropped archived year %s: fetch failed: %s", year, exc)
            continue
        if not isinstance(rows, list):
            raise ValueError(f"PrimeGov archived year {year} returned {type(rows).__name__}")
        if not rows:
            stats.years_empty.append(year)
            logger.warning(
                "archived year %s returned zero rows from ListArchivedMeetings; keeping loop trail",
                year,
            )
            continue
        logger.info("fetched PrimeGov archived year=%s rows=%d", year, len(rows))
        _merge_rows(rows, meetings_by_id, stats, source=f"archived:{year}")

    try:
        upcoming_rows = _fetch_json(UPCOMING_MEETINGS_URL)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        logger.warning("dropped upcoming endpoint: fetch failed: %s", exc)
        upcoming_rows = []
    if not isinstance(upcoming_rows, list):
        raise ValueError(f"PrimeGov upcoming endpoint returned {type(upcoming_rows).__name__}")
    logger.info("fetched PrimeGov upcoming rows=%d", len(upcoming_rows))
    _merge_rows(upcoming_rows, meetings_by_id, stats, source="upcoming")

    meetings = sorted(
        meetings_by_id.values(),
        key=lambda row: (row["meeting_date"], _sort_time(row["meeting_time"]), row["meeting_id"]),
    )
    _log_stats(stats, len(meetings))
    return meetings


def _merge_rows(
    rows: list[object],
    meetings_by_id: dict[str, dict[str, str]],
    stats: ScrapeStats,
    *,
    source: str,
) -> None:
    for index, row in enumerate(rows, start=1):
        stats.rows_seen += 1
        row_key = f"{source}#{index}"
        if not isinstance(row, dict):
            stats.rows_dropped += 1
            stats.drop_reasons["non_dict_row"] += 1
            logger.warning("dropped row %s: expected object, got %s", row_key, type(row).__name__)
            continue

        meeting = _build_meeting(row, row_key, stats)
        meeting_id = meeting["meeting_id"]
        if not meeting_id:
            stats.rows_dropped += 1
            stats.drop_reasons["missing_meeting_id"] += 1
            logger.warning("dropped row %s: missing PrimeGov id title=%r", row_key, meeting["meeting_title"])
            continue

        if meeting_id in meetings_by_id:
            stats.duplicate_ids.append(f"{meeting_id} from {source}")
            logger.warning("dropped duplicate PrimeGov meeting id=%s from %s", meeting_id, source)
            continue

        _validate_schema(meeting)
        meetings_by_id[meeting_id] = meeting
        stats.rows_accepted += 1
        logger.info(
            "emitted PrimeGov meeting id=%s source=%s date=%s time=%r title=%r status=%s agenda=%r packet=%r minutes=%r video=%r",
            meeting_id,
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


def _build_meeting(row: dict, row_key: str, stats: ScrapeStats) -> dict[str, str]:
    meeting_id = _clean_text(str(row.get("id") or ""))
    title = _clean_text(str(row.get("title") or ""))
    if not title:
        stats.field_absences["meeting_title"] += 1
        logger.warning("row %s has empty meeting_title from PrimeGov title field", row_key)

    meeting_date = _extract_date(row, row_key)
    meeting_time = _extract_time(row, row_key, stats)
    meeting_location = _clean_text(str(row.get("location") or ""))
    if not meeting_location:
        stats.field_absences["meeting_location"] += 1

    urls = {
        "agenda_url": "",
        "minutes_url": "",
        "video_url": "",
        "agenda_packet_url": "",
        "ecomment_url": "",
    }
    has_cancellation_notice = _assign_document_urls(row, meeting_id or row_key, urls, stats)
    video_url = _emit_url(str(row.get("videoUrl") or ""), DEFAULT_URL, "video_url", meeting_id or row_key)
    if video_url:
        urls["video_url"] = video_url
    else:
        stats.field_absences["video_url"] += 1

    if row.get("allowPublicComment"):
        stats.field_absences["ecomment_url"] += 1
        logger.warning(
            "row %s allows public comment but PrimeGov API exposes modal-only comment action, not a stable ecomment_url",
            meeting_id or row_key,
        )
    else:
        stats.field_absences["ecomment_url"] += 1

    if row.get("allowPublicSpeaker"):
        logger.warning(
            "row %s allows speaker requests but canonical schema has no speaker_url field; value dropped",
            meeting_id or row_key,
        )

    zoom_link = str(row.get("zoomMeetingLink") or "").strip()
    if zoom_link:
        logger.warning(
            "row %s has zoomMeetingLink=%r; not emitted as video_url because it is attendance access, not a recording",
            meeting_id or row_key,
            zoom_link,
        )

    vendor_state = _clean_text(str(row.get("meetingState") or ""))
    if vendor_state:
        stats.vendor_states[vendor_state] += 1
    status = _status_from_evidence(title, urls, has_cancellation_notice, meeting_id or row_key, stats)
    stats.emitted_statuses[status] += 1

    return {
        "meeting_title": title,
        "meeting_date": meeting_date,
        "meeting_time": meeting_time,
        "meeting_location": meeting_location,
        "meeting_status": status,
        "agenda_url": urls["agenda_url"],
        "minutes_url": urls["minutes_url"],
        "video_url": urls["video_url"],
        "agenda_packet_url": urls["agenda_packet_url"],
        "ecomment_url": urls["ecomment_url"],
        "meeting_id": meeting_id,
    }


def _assign_document_urls(
    row: dict,
    row_id: str,
    urls: dict[str, str],
    stats: ScrapeStats,
) -> bool:
    documents = row.get("documentList") or []
    if not isinstance(documents, list):
        logger.warning("row %s documentList is %s; dropped document evidence", row_id, type(documents).__name__)
        return False

    has_cancellation_notice = False
    for document in documents:
        if not isinstance(document, dict):
            _record_dropped_document(stats, "non_dict_document", row_id, repr(document)[:120])
            continue

        template_name = _clean_text(str(document.get("templateName") or ""))
        output_type = _clean_text(str(document.get("compileOutputType") or ""))
        document_id = _clean_text(str(document.get("id") or ""))
        stats.document_templates[template_name or "<empty>"] += 1
        stats.document_output_types[output_type or "<empty>"] += 1
        field = _document_field(template_name, output_type, row_id, document_id, stats)
        if "cancellation notice" in template_name.strip().lower():
            has_cancellation_notice = True
        if not field:
            continue
        if not document_id:
            _record_dropped_document(stats, "missing_document_id", row_id, template_name)
            continue

        href = f"{COMPILED_FILE_URL}?{urlencode({'compiledFileId': document_id})}"
        emitted = _emit_url(href, DEFAULT_URL, field, row_id)
        if not emitted:
            continue
        if urls[field]:
            _record_dropped_document(
                stats,
                f"duplicate_{field}",
                row_id,
                f"kept={urls[field]} dropped={emitted}",
            )
            continue
        urls[field] = emitted

    return has_cancellation_notice


def _document_field(
    template_name: str,
    output_type: str,
    row_id: str,
    document_id: str,
    stats: ScrapeStats,
) -> str:
    normalized = re.sub(r"\s+", " ", template_name.strip().lower())
    if output_type == "3":
        _record_dropped_document(
            stats,
            "html_document_not_emitted",
            row_id,
            f"id={document_id} template={template_name!r} outputType=3",
        )
        return ""
    if normalized == "agenda":
        return "agenda_url"
    if normalized == "minutes":
        return "minutes_url"
    if normalized in {"packet", "agenda packet"}:
        return "agenda_packet_url"
    if normalized in {"cancellation notice", "action report", "action summary"}:
        _record_dropped_document(
            stats,
            f"noncanonical_document:{normalized}",
            row_id,
            f"id={document_id} template={template_name!r}",
        )
        return ""

    _record_dropped_document(
        stats,
        f"unknown_document_template:{normalized or '<empty>'}",
        row_id,
        f"id={document_id} template={template_name!r} outputType={output_type!r}",
    )
    return ""


def _record_dropped_document(stats: ScrapeStats, reason: str, row_id: str, value: str) -> None:
    stats.dropped_documents[reason] += 1
    samples = stats.dropped_document_samples[reason]
    if len(samples) < 10:
        samples.append(f"row={row_id} value={value}")


def _status_from_evidence(
    title: str,
    urls: dict[str, str],
    has_cancellation_notice: bool,
    row_id: str,
    stats: ScrapeStats,
) -> str:
    if CANCELLED_RE.search(title[:300]):
        return "Cancelled"
    if has_cancellation_notice:
        stats.cancellation_notice_without_title.append(row_id)
        return "Cancelled"
    if urls["minutes_url"]:
        return "Minutes Available"
    if urls["agenda_url"] or urls["agenda_packet_url"]:
        return "Agenda Available"
    return "Scheduled"


def _extract_date(row: dict, row_key: str) -> str:
    date_time = _clean_text(str(row.get("dateTime") or ""))
    if date_time:
        try:
            date_value = datetime.fromisoformat(date_time).date().isoformat()
        except ValueError as exc:
            raise ValueError(f"row {row_key} has unparseable dateTime={date_time!r}") from exc
        display_date = _clean_text(str(row.get("date") or ""))
        if display_date:
            try:
                parsed_display = datetime.strptime(display_date, "%b %d, %Y").date().isoformat()
            except ValueError:
                logger.warning("row %s has unparseable PrimeGov display date=%r", row_key, display_date)
            else:
                if parsed_display != date_value:
                    logger.warning(
                        "row %s date conflict: dateTime=%s display_date=%s; emitted dateTime date",
                        row_key,
                        date_value,
                        parsed_display,
                    )
        return date_value
    raise ValueError(f"row {row_key} missing PrimeGov dateTime")


def _extract_time(row: dict, row_key: str, stats: ScrapeStats) -> str:
    display_time = _clean_text(str(row.get("time") or ""))
    if display_time:
        normalized = _normalize_time(display_time)
        if not normalized:
            stats.field_absences["meeting_time_unparsed"] += 1
            logger.warning("row %s has unparseable PrimeGov time=%r", row_key, display_time)
            return ""
        date_time = _clean_text(str(row.get("dateTime") or ""))
        if date_time:
            try:
                from_datetime = _normalize_time(datetime.fromisoformat(date_time).strftime("%I:%M %p"))
            except ValueError:
                from_datetime = ""
            if from_datetime and from_datetime != normalized:
                logger.warning(
                    "row %s time conflict: time=%s dateTime_time=%s; emitted display time",
                    row_key,
                    normalized,
                    from_datetime,
                )
        return normalized

    date_time = _clean_text(str(row.get("dateTime") or ""))
    if date_time:
        try:
            normalized = _normalize_time(datetime.fromisoformat(date_time).strftime("%I:%M %p"))
        except ValueError:
            normalized = ""
        if normalized:
            logger.warning(
                "row %s missing PrimeGov display time; emitted time extracted from dateTime=%r",
                row_key,
                date_time,
            )
            return normalized

    stats.field_absences["meeting_time"] += 1
    logger.warning("row %s has no per-row meeting_time signal", row_key)
    return ""


def _normalize_time(value: str) -> str:
    match = TIME_RE.search(value.strip())
    if not match:
        return ""
    hour = int(match.group(1))
    minute = match.group(2)
    suffix = match.group(3).upper() + "M"
    if hour < 1 or hour > 12:
        return ""
    return f"{hour}:{minute} {suffix}"


def _fetch_archived_years() -> list[int]:
    data = _fetch_json(ARCHIVED_YEARS_URL)
    if not isinstance(data, list):
        raise ValueError(f"PrimeGov archived years returned {type(data).__name__}")
    years: list[int] = []
    for raw_year in data:
        try:
            years.append(int(raw_year))
        except (TypeError, ValueError):
            logger.warning("dropped non-integer PrimeGov archive year value=%r", raw_year)
    return sorted(set(years))


def _validate_years(years: list[int]) -> None:
    if not years:
        raise ValueError("PrimeGov GetArchivedMeetingYears returned no usable years")
    current_year = datetime.now().year
    if max(years) < current_year - 1:
        logger.warning(
            "PrimeGov latest archived year %s is older than current year %s; upcoming endpoint may carry current rows",
            max(years),
            current_year,
        )
    logger.info("PrimeGov archive years discovered: %s through %s (%d years)", min(years), max(years), len(years))


def _validate_primegov_surface(url: str) -> None:
    html = _fetch_text_bounded(url, accept="text/html,application/xhtml+xml")
    markers = (
        "PrimeGov Portal",
        "/Scripts/Custom/Public/_Archived.js",
        "/Scripts/Custom/Public/_Upcoming.js",
        "primegov_logo",
    )
    missing = [marker for marker in markers if marker not in html]
    if missing:
        raise ValueError(f"North Las Vegas portal no longer matches PrimeGov public portal markers: {missing}")
    logger.info("witnessed PrimeGov portal markers: %s", ", ".join(markers))


def _fetch_json(url: str) -> object:
    text = _fetch_text_bounded(url, accept="application/json")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Expected JSON from {url}, got {text[:200]!r}") from exc


def _fetch_text_bounded(url: str, *, accept: str) -> str:
    _validate_fetch_url(url)
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": accept,
        },
    )
    with _http_opener().open(request, timeout=30) as response:
        _validate_fetch_url(response.geturl())
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


def _http_opener():
    verify_paths = ssl.get_default_verify_paths()
    if verify_paths.cafile or not Path("/etc/ssl/cert.pem").exists():
        return build_opener()
    context = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    return build_opener(HTTPSHandler(context=context))


def _validate_fetch_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in ALLOWED_FETCH_HOSTS:
        raise ValueError(f"disallowed PrimeGov fetch URL: {url}")


def _validate_input_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host != PRIMEGOV_HOST:
        raise ValueError(f"Expected North Las Vegas PrimeGov URL, got {url!r}")


def _emit_url(href: str, base_url: str, field: str, row_id: str) -> str:
    if not href:
        return ""
    stripped = href.strip()
    lowered = stripped.lower()
    if lowered.startswith(BAD_URL_PREFIXES) or lowered in {"#", ""}:
        logger.warning("dropped %s for row %s: rejected non-http href %r", field, row_id, href)
        return ""

    absolute = urljoin(base_url, stripped)
    parsed = urlparse(absolute)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        logger.warning("dropped %s for row %s: disallowed scheme in %r", field, row_id, absolute)
        return ""
    if host not in ALLOWED_EMIT_HOSTS:
        logger.warning("dropped %s for row %s: disallowed host %r in %r", field, row_id, host, absolute)
        return ""
    return absolute


def _clean_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", unescape(value))
    return re.sub(r"\s+", " ", text).strip()


def _validate_schema(meeting: dict[str, str]) -> None:
    keys = tuple(meeting.keys())
    if keys != CANONICAL_FIELDS:
        raise ValueError(f"Schema mismatch: expected {CANONICAL_FIELDS}, got {keys}")
    for key, value in meeting.items():
        if not isinstance(value, str):
            raise TypeError(f"{key} must be str, got {type(value).__name__}")
    if meeting["meeting_date"] and not ISO_DATE_RE.match(meeting["meeting_date"]):
        raise ValueError(f"meeting_date is not ISO YYYY-MM-DD: {meeting['meeting_date']!r}")
    if meeting["meeting_status"] not in {
        "Scheduled",
        "Agenda Available",
        "Minutes Available",
        "Cancelled",
    }:
        raise ValueError(f"invalid meeting_status: {meeting['meeting_status']!r}")
    for field_name in (
        "agenda_url",
        "minutes_url",
        "video_url",
        "agenda_packet_url",
        "ecomment_url",
    ):
        value = meeting[field_name]
        if value and not value.startswith(("http://", "https://")):
            raise ValueError(f"{field_name} must be absolute http(s) URL or empty: {value!r}")


def _sort_time(value: str) -> str:
    if not value:
        return "99:99"
    try:
        return datetime.strptime(value, "%I:%M %p").strftime("%H:%M")
    except ValueError:
        return "99:99"


def _log_stats(stats: ScrapeStats, emitted_count: int) -> None:
    if stats.years_empty:
        logger.warning("PrimeGov archived years with zero rows: %s", stats.years_empty)
    if stats.years_failed:
        logger.warning("PrimeGov archived years failed and were omitted: %s", stats.years_failed)
    if stats.duplicate_ids:
        logger.warning("PrimeGov duplicate meeting ids dropped: %s", stats.duplicate_ids[:20])
    if stats.cancellation_notice_without_title:
        logger.warning(
            "emitted Cancelled for %d rows from Cancellation Notice document although title lacked cancellation regex; samples=%s",
            len(stats.cancellation_notice_without_title),
            stats.cancellation_notice_without_title[:20],
        )
    for reason, count in sorted(stats.dropped_documents.items()):
        logger.warning(
            "dropped %d PrimeGov documents for reason=%s; samples=%s",
            count,
            reason,
            stats.dropped_document_samples.get(reason, []),
        )
    logger.warning(
        "PrimeGov field absences: %s",
        dict(sorted(stats.field_absences.items())),
    )
    logger.info(
        "PrimeGov document templates seen: %s output_types=%s",
        dict(sorted(stats.document_templates.items())),
        dict(sorted(stats.document_output_types.items())),
    )
    logger.info("PrimeGov vendor meetingState values seen: %s", dict(sorted(stats.vendor_states.items())))
    logger.info("PrimeGov emitted statuses: %s", dict(sorted(stats.emitted_statuses.items())))
    logger.info(
        "north_las_vegas_parser summary: years_seen=%d rows_seen=%d rows_accepted=%d rows_dropped=%d emitted=%d drop_reasons=%s",
        stats.years_seen,
        stats.rows_seen,
        stats.rows_accepted,
        stats.rows_dropped,
        emitted_count,
        dict(sorted(stats.drop_reasons.items())),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    rows = scrape_calendar(DEFAULT_URL)
    print(f"North Las Vegas meetings scraped: {len(rows)}")
