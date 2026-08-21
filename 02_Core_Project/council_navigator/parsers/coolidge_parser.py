"""Coolidge, AZ IQM2 calendar parser."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import logging
import re
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from polite_http import make_session


DEFAULT_URL = "https://coolidgecityaz.iqm2.com/Citizens/Calendar.aspx"
BASE_URL = DEFAULT_URL
ALLOWED_HOSTS = {"coolidgecityaz.iqm2.com"}
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
DOCUMENT_TYPE_TO_FIELD = {
    "1": "agenda_packet_url",
    "12": "minutes_url",
    "14": "agenda_url",
    "15": "minutes_url",
}
DOCUMENT_LABEL_TO_FIELD = {
    "agenda outline": "agenda_url",
    "agenda packet": "agenda_packet_url",
    "action minutes": "minutes_url",
    "minutes packet": "minutes_url",
    "video": "video_url",
}
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
DATE_TEXT_RE = re.compile(
    r"^[A-Z]+,\s+([A-Z]+)\s+(\d{1,2}),\s+(\d{4})\s+(\d{1,2}:\d{2}\s+[AP]M)",
    re.IGNORECASE,
)
SUMMARY_COUNT_RE = re.compile(r"Displaying\s+(?:ALL\s+)?([\d,]+)\s+meetings?", re.IGNORECASE)

logger = logging.getLogger(__name__)


class ScrapeStats:
    def __init__(self) -> None:
        self.rejected_urls: Counter[str] = Counter()
        self.placeholder_links: Counter[str] = Counter()
        self.vendor_statuses: Counter[str] = Counter()
        self.emitted_statuses: Counter[str] = Counter()
        self.date_conflicts: list[str] = []
        self.unknown_document_types: Counter[str] = Counter()
        self.document_type_mismatches: Counter[str] = Counter()
        self.body_values: Counter[str] = Counter()
        self.row_decisions: Counter[str] = Counter()


def scrape_calendar(url: str, *, today: date | None = None) -> list[dict]:
    """Scrape current-month-forward Coolidge City Council meetings."""
    if url != DEFAULT_URL:
        raise ValueError(f"Expected exact Coolidge IQM2 source URL, got {url!r}")
    stats = ScrapeStats()
    month_floor = (today or date.today()).replace(day=1)
    with make_session() as session:
        try:
            initial_soup = _fetch_soup(session, url)
        except requests.exceptions.ConnectionError as exc:
            if isinstance(
                exc,
                (
                    requests.exceptions.SSLError,
                    requests.exceptions.ProxyError,
                ),
            ):
                raise
            logger.warning("health_empty_kind=source_blocked")
            logger.warning(
                "Coolidge official IQM2 source connection blocked: url=%s "
                "missing_scope=current_month_forward_city_council floor=%s error=%s",
                url,
                month_floor.isoformat(),
                exc,
            )
            return []
        _validate_iqm2_surface(initial_soup, url)

        meetings = _scrape_soup(initial_soup, url, stats, month_floor=month_floor)
        expected = _expected_count(initial_soup)
        if expected is not None and expected != stats.row_decisions["rows_seen"]:
            raise ValueError(
                f"IQM2 current calendar says {expected} meetings, "
                f"but exposed {stats.row_decisions['rows_seen']} rows"
            )
        logger.info(
            "Scraped %d current-month-forward Coolidge City Council meetings; "
            "month_floor=%s",
            len(meetings),
            month_floor.isoformat(),
        )
        _log_stats(stats)
        if not meetings:
            logger.warning("health_empty_kind=confirmed_empty")
            logger.warning(
                "Coolidge IQM2 current view witnessed zero current-month-forward City Council rows"
            )
        return meetings


def _fetch_soup(session: requests.Session, url: str) -> BeautifulSoup:
    text = _fetch_text_bounded(session, "GET", url)
    return BeautifulSoup(text, "html.parser")


def _fetch_text_bounded(
    session: requests.Session,
    method: str,
    url: str,
    data: dict[str, str] | None = None,
    max_bytes: int = 12_000_000,
) -> str:
    response_context = session.request(
        method,
        url,
        data=data,
        timeout=(10, 30),
        stream=True,
        allow_redirects=True,
    )
    with response_context as response:
        if getattr(response, "status_code", None) in {401, 403, 429}:
            logger.warning("health_empty_kind=source_blocked")
        response.raise_for_status()
        _validate_response_host(response.url)
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > max_bytes:
                raise ValueError(f"Response from {url} exceeded {max_bytes} bytes")
            chunks.append(chunk)
        encoding = response.encoding or "utf-8"
        return b"".join(chunks).decode(encoding, errors="replace")


def _validate_response_host(url: str) -> None:
    host = (urlparse(url).hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise ValueError(f"IQM2 redirect to disallowed host: {host!r}")


def _validate_iqm2_surface(soup: BeautifulSoup, url: str) -> None:
    parsed = urlparse(url)
    if parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError(f"Expected Coolidge IQM2 host, got {parsed.hostname!r}")
    if "Calendar.aspx" not in parsed.path:
        raise ValueError(f"Expected IQM2 Calendar.aspx URL, got {parsed.path!r}")
    if soup.select_one("#aspnetForm") is None:
        raise ValueError("Expected ASP.NET form on IQM2 calendar page")
    if soup.select_one("input#__VIEWSTATE") is None:
        raise ValueError("Expected ASP.NET __VIEWSTATE on IQM2 calendar page")
    if soup.select_one("input#__EVENTVALIDATION") is None:
        raise ValueError("Expected ASP.NET __EVENTVALIDATION on IQM2 calendar page")
    if soup.select_one("#ContentPlaceholder1_lblCalendarRange") is None:
        raise ValueError("Expected IQM2 calendar range controls")
    if soup.select_one("#ContentPlaceholder1_pnlMeetings") is None:
        raise ValueError("Expected IQM2 meetings panel")


def _scrape_soup(
    soup: BeautifulSoup,
    base_url: str,
    stats: ScrapeStats,
    *,
    month_floor: date | None = None,
) -> list[dict]:
    rows = soup.select("#ContentPlaceholder1_pnlMeetings div.Row.MeetingRow")
    expected = _expected_count(soup)
    if expected and not rows:
        raise ValueError(f"IQM2 summary says {expected} meetings, but no meeting rows were found")

    meetings: list[dict] = []
    effective_floor = month_floor or date.today().replace(day=1)
    for row_number, row in enumerate(rows, start=1):
        stats.row_decisions["rows_seen"] += 1
        detail_link = row.select_one(".RowLink a[href*='Detail_Meeting.aspx']")
        if detail_link is None:
            raise ValueError("IQM2 meeting row missing Detail_Meeting link")
        meeting_id = _meeting_id_from_href(str(detail_link.get("href") or ""))
        meeting_date, _meeting_time = _date_time_from_row(detail_link, meeting_id, stats)
        if not meeting_date:
            stats.row_decisions["rows_dropped_unverified_date"] += 1
            logger.warning(
                "IQM2 row dropped because its date cannot be verified: "
                "row=%d meeting_id=%r",
                row_number,
                meeting_id,
            )
            continue
        if date.fromisoformat(meeting_date) < effective_floor:
            stats.row_decisions["rows_dropped_before_month"] += 1
            logger.warning(
                "IQM2 row dropped before month floor: row=%d meeting_id=%s "
                "date=%s floor=%s",
                row_number,
                meeting_id,
                meeting_date,
                effective_floor.isoformat(),
            )
            continue

        stats.row_decisions["current_window_rows"] += 1
        tooltip = _parse_tooltip(str(detail_link.get("title") or ""))
        board = tooltip.get("board", "").strip()
        meeting_type = tooltip.get("type", "").strip()
        if not board:
            raise ValueError(
                "IQM2 current-window row lacks a trustworthy Board signal: "
                f"meeting_id={meeting_id!r} type={meeting_type!r}"
            )
        stats.body_values[board] += 1
        if board.casefold() != "city council":
            stats.row_decisions["rows_dropped_non_council"] += 1
            logger.warning(
                "IQM2 current-window row dropped for non-council body: "
                "row=%d meeting_id=%s board=%r type=%r",
                row_number,
                meeting_id,
                board,
                meeting_type,
            )
            continue
        stats.row_decisions["council_body_rows"] += 1
        meeting = _parse_meeting_row(row, base_url, stats)
        _validate_schema(meeting)
        meetings.append(meeting)
        stats.row_decisions["rows_emitted"] += 1
    if (
        stats.row_decisions["current_window_rows"]
        and not stats.row_decisions["council_body_rows"]
    ):
        raise ValueError(
            "IQM2 current-month-forward rows were present but none carried "
            "the observed Board: City Council signal"
        )
    return meetings


def _parse_meeting_row(row: Tag, base_url: str, stats: ScrapeStats) -> dict:
    detail_link = row.select_one(".RowLink a[href*='Detail_Meeting.aspx']")
    if detail_link is None:
        raise ValueError("IQM2 meeting row missing Detail_Meeting link")

    detail_href = detail_link.get("href", "")
    meeting_id = _meeting_id_from_href(detail_href)
    if not meeting_id:
        raise ValueError(f"IQM2 meeting detail link missing ID: {detail_href!r}")

    detail_url = _emit_url(detail_href, base_url, "meeting_detail", meeting_id)
    if not detail_url:
        raise ValueError(f"IQM2 meeting detail URL was rejected for meeting {meeting_id}: {detail_href!r}")

    title_text = _clean_text(row.select_one(".RowDetails")) or _title_from_tooltip(detail_link)
    meeting_date, meeting_time = _date_time_from_row(detail_link, meeting_id, stats)
    tooltip = _parse_tooltip(detail_link.get("title", ""))
    meeting_location = tooltip.get("location", "")

    urls = {
        "agenda_url": "",
        "minutes_url": "",
        "video_url": "",
        "agenda_packet_url": "",
    }
    _assign_document_urls(row, base_url, meeting_id, urls, stats)

    vendor_status = tooltip.get("status", "")
    if vendor_status:
        stats.vendor_statuses[vendor_status] += 1
    title_text = _normalize_cancellation_title(
        title_text,
        vendor_status,
        meeting_id,
        stats,
    )
    meeting_status = _canonical_status(title_text, urls)
    stats.emitted_statuses[meeting_status] += 1

    return {
        "meeting_title": title_text,
        "meeting_date": meeting_date,
        "meeting_time": meeting_time,
        "meeting_location": meeting_location,
        "meeting_status": meeting_status,
        "agenda_url": urls["agenda_url"],
        "minutes_url": urls["minutes_url"],
        "video_url": urls["video_url"],
        "agenda_packet_url": urls["agenda_packet_url"],
        "ecomment_url": "",
        "meeting_id": meeting_id,
    }


def _assign_document_urls(
    row: Tag,
    base_url: str,
    meeting_id: str,
    urls: dict[str, str],
    stats: ScrapeStats,
) -> None:
    for link in row.select(".MeetingLinks a"):
        label = _clean_text(link)
        label_key = label.lower()
        href = (link.get("href") or "").strip()
        field = DOCUMENT_LABEL_TO_FIELD.get(label_key)

        if field is None:
            stats.placeholder_links[f"unknown_label:{label}"] += 1
            continue

        if href in ("", "#"):
            if "HiddenDocumentLink" in (link.get("class") or []):
                stats.placeholder_links[f"{field}:{href or 'blank'}"] += 1
                continue
            stats.rejected_urls[f"{field}:empty_href"] += 1
            continue

        document_type = _query_value(href, "Type")
        if document_type:
            type_field = DOCUMENT_TYPE_TO_FIELD.get(document_type)
            if type_field is None:
                stats.unknown_document_types[document_type] += 1
            elif type_field != field:
                stats.document_type_mismatches[f"Type={document_type} label={label}"] += 1
                field = type_field

        emitted = _emit_url(href, base_url, field, meeting_id)
        if not emitted:
            stats.rejected_urls[f"{field}:rejected"] += 1
            continue

        if field == "minutes_url" and urls[field]:
            # Prefer Action Minutes over Minutes Packet when both are present.
            if label_key == "action minutes":
                urls[field] = emitted
            continue
        urls[field] = emitted


def _normalize_cancellation_title(
    meeting_title: str,
    vendor_status: str,
    meeting_id: str,
    stats: ScrapeStats,
) -> str:
    """Carry IQM2's structured cancellation signal into canonical title evidence."""
    if vendor_status.casefold() != "cancelled" or CANCELLED_RE.search(meeting_title):
        return meeting_title

    normalized = f"{meeting_title} — Cancelled" if meeting_title else "Cancelled"
    stats.row_decisions["titles_normalized_from_vendor_cancellation"] += 1
    logger.warning(
        "IQM2 cancellation surfaced in the official tooltip outside the title; "
        "normalized title for canonical evidence: meeting_id=%s raw_title=%r "
        "vendor_status=%r normalized_title=%r",
        meeting_id,
        meeting_title,
        vendor_status,
        normalized,
    )
    return normalized


def _canonical_status(
    meeting_title: str,
    urls: dict[str, str],
) -> str:
    if CANCELLED_RE.search(meeting_title):
        return "Cancelled"
    if urls["minutes_url"]:
        return "Minutes Available"
    if urls["agenda_url"] or urls["agenda_packet_url"]:
        return "Agenda Available"
    return "Scheduled"


def _date_time_from_row(link: Tag, meeting_id: str, stats: ScrapeStats) -> tuple[str, str]:
    link_date, link_time = _parse_link_date(_clean_text(link))
    tooltip_date, tooltip_time = _parse_tooltip_date(link.get("title", ""))

    if link_date and tooltip_date and link_date != tooltip_date:
        stats.date_conflicts.append(meeting_id)
        return "", ""
    if link_time and tooltip_time and link_time != tooltip_time:
        stats.date_conflicts.append(meeting_id)
        return link_date or tooltip_date, ""
    return link_date or tooltip_date, link_time or tooltip_time


def _parse_link_date(value: str) -> tuple[str, str]:
    if not value:
        return "", ""
    parsed = datetime.strptime(value, "%b %d, %Y %I:%M %p")
    return parsed.strftime("%Y-%m-%d"), _format_time(parsed)


def _parse_tooltip_date(value: str) -> tuple[str, str]:
    match = DATE_TEXT_RE.search(value.strip())
    if not match:
        return "", ""
    month, day, year, time_text = match.groups()
    parsed = datetime.strptime(f"{month.title()} {day}, {year} {time_text.upper()}", "%B %d, %Y %I:%M %p")
    return parsed.strftime("%Y-%m-%d"), _format_time(parsed)


def _format_time(value: datetime) -> str:
    """Format a clock time portably on Windows and POSIX."""
    return value.strftime("%I:%M %p").lstrip("0")


def _parse_tooltip(value: str) -> dict[str, str]:
    result = {"board": "", "type": "", "status": "", "location": ""}
    lines = [_clean_text_fragment(line) for line in re.split(r"[\r\n]+", value)]
    lines = [line for line in lines if line]

    for line in lines:
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip().lower()
        if key in result:
            result[key] = raw_value.strip()

    status_index = next(
        (index for index, line in enumerate(lines) if line.lower().startswith("status:")),
        None,
    )
    if status_index is not None:
        for line in lines[status_index + 1 :]:
            if ":" not in line:
                result["location"] = line
                break

    return result


def _title_from_tooltip(link: Tag) -> str:
    tooltip = _parse_tooltip(link.get("title", ""))
    parts = [part for part in (tooltip.get("board", ""), tooltip.get("type", "")) if part]
    return " - ".join(parts)


def _expected_count(soup: BeautifulSoup) -> int | None:
    summary = _clean_text(soup.select_one("#ContentPlaceholder1_lblFilterSummary"))
    if not summary:
        return None
    match = SUMMARY_COUNT_RE.search(summary)
    if not match:
        logger.warning("Could not parse IQM2 meeting-count summary: %r", summary)
        return None
    return int(match.group(1).replace(",", ""))


def _meeting_id_from_href(href: str) -> str:
    return _query_value(href, "ID")


def _query_value(href: str, key: str) -> str:
    parsed = urlparse(href)
    values = parse_qs(parsed.query).get(key)
    return values[0] if values else ""


def _emit_url(href: str, base_url: str, field: str, row_id: str) -> str:
    raw = href.strip()
    if not raw:
        return ""
    lowered = raw.lower().lstrip()
    if lowered.startswith(BAD_URL_PREFIXES):
        logger.warning("Rejected IQM2 %s URL for %s due to bad scheme: %r", field, row_id, href)
        return ""
    absolute = urljoin(base_url, raw)
    parsed = urlparse(absolute)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        logger.warning("Rejected IQM2 %s URL for %s due to scheme %r", field, row_id, parsed.scheme)
        return ""
    if host not in ALLOWED_HOSTS:
        logger.warning("Rejected IQM2 %s URL for %s due to host %r", field, row_id, host)
        return ""
    return absolute


def _clean_text(node: Tag | None) -> str:
    if node is None:
        return ""
    return _clean_text_fragment(node.get_text(" ", strip=True))


def _clean_text_fragment(value: str) -> str:
    return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)


def _validate_schema(meeting: dict) -> None:
    if tuple(meeting.keys()) != CANONICAL_FIELDS:
        raise ValueError(f"Coolidge parser emitted non-canonical fields: {tuple(meeting.keys())!r}")
    for key, value in meeting.items():
        if not isinstance(value, str):
            raise TypeError(f"Coolidge parser emitted non-string {key}: {value!r}")
    for key in ("agenda_url", "minutes_url", "video_url", "agenda_packet_url", "ecomment_url"):
        value = meeting[key]
        if value and not value.startswith(("http://", "https://")):
            raise ValueError(f"Coolidge parser emitted non-absolute URL for {key}: {value!r}")


def _log_stats(stats: ScrapeStats) -> None:
    unsupported_statuses = {
        status: count
        for status, count in stats.vendor_statuses.items()
        if status not in {"Cancelled", "Scheduled"}
    }
    if unsupported_statuses:
        logger.warning(
            "IQM2 vendor statuses not in canonical enum were remapped by row evidence: %s",
            dict(sorted(unsupported_statuses.items())),
        )
    if stats.placeholder_links:
        logger.warning("IQM2 hidden/placeholder fields left empty: %s", dict(sorted(stats.placeholder_links.items())))
    if stats.rejected_urls:
        logger.warning("IQM2 rejected URL inputs: %s", dict(sorted(stats.rejected_urls.items())))
    if stats.unknown_document_types:
        logger.warning("IQM2 unknown document Type values: %s", dict(sorted(stats.unknown_document_types.items())))
    if stats.document_type_mismatches:
        logger.warning("IQM2 document label/type mismatches: %s", dict(sorted(stats.document_type_mismatches.items())))
    if stats.date_conflicts:
        logger.warning("IQM2 date/time conflicts blanked for meeting IDs: %s", stats.date_conflicts[:20])
    if stats.body_values:
        logger.info("IQM2 current-window Board vocabulary: %s", dict(sorted(stats.body_values.items())))
    logger.info("IQM2 row decisions: %s", dict(sorted(stats.row_decisions.items())))
    logger.info("IQM2 emitted canonical statuses: %s", dict(sorted(stats.emitted_statuses.items())))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    scraped = scrape_calendar(DEFAULT_URL)
    print(f"Scraped {len(scraped)} Coolidge meetings")
    for meeting in scraped[:5]:
        print(meeting)
