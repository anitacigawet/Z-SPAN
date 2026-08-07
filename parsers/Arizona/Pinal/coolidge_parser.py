"""Coolidge — IQM2 meeting parser."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import logging
import re
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag


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


@dataclass(frozen=True)
class CalendarRange:
    label: str
    url: str
    event_target: str = ""
    event_argument: str = ""


class ScrapeStats:
    def __init__(self) -> None:
        self.rejected_urls: Counter[str] = Counter()
        self.placeholder_links: Counter[str] = Counter()
        self.vendor_statuses: Counter[str] = Counter()
        self.emitted_statuses: Counter[str] = Counter()
        self.date_conflicts: list[str] = []
        self.unknown_document_types: Counter[str] = Counter()
        self.document_type_mismatches: Counter[str] = Counter()


def scrape_calendar(url: str) -> list[dict]:
    """Scrape the Coolidge IQM2 calendar into the canonical parser schema."""
    stats = ScrapeStats()
    with requests.Session() as session:
        session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"})
        initial_soup = _fetch_soup(session, url)
        _validate_iqm2_surface(initial_soup, url)

        ranges = _discover_ranges(initial_soup, url)
        all_range = next((item for item in ranges if item.label.lower() == "all"), None)
        if all_range is not None:
            try:
                meetings = _scrape_range(session, all_range, stats)
                _log_stats(stats)
                return meetings
            except requests.RequestException as exc:
                logger.warning("IQM2 all-range fetch failed; falling back to year ranges: %s", exc)

        meetings_by_id: dict[str, dict] = {}
        year_ranges = [item for item in ranges if item.label.isdigit()]
        if not year_ranges:
            return _scrape_soup(initial_soup, url, stats)

        for item in sorted(year_ranges, key=lambda candidate: int(candidate.label)):
            try:
                for meeting in _scrape_range(session, item, stats):
                    meeting_id = meeting["meeting_id"]
                    if meeting_id in meetings_by_id:
                        logger.warning("Duplicate IQM2 meeting id %s from range %s", meeting_id, item.label)
                    meetings_by_id[meeting_id] = meeting
            except requests.RequestException as exc:
                logger.warning("IQM2 range %s failed; keeping prior results: %s", item.label, exc)

        meetings = sorted(
            meetings_by_id.values(),
            key=lambda meeting: (meeting["meeting_date"], meeting["meeting_time"], meeting["meeting_id"]),
        )
        _log_stats(stats)
        return meetings


def _scrape_range(session: requests.Session, item: CalendarRange, stats: ScrapeStats) -> list[dict]:
    soup = _fetch_postback_soup(session, item) if item.event_target else _fetch_soup(session, item.url)
    meetings = _scrape_soup(soup, item.url, stats)
    expected = _expected_count(soup)
    if expected is not None and expected != len(meetings):
        raise ValueError(
            f"IQM2 summary for {item.label!r} says {expected} meetings, "
            f"but parsed {len(meetings)} rows"
        )
    logger.info("Scraped %d Coolidge IQM2 meetings from range %s", len(meetings), item.label)
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
    with session.request(
        method,
        url,
        data=data,
        timeout=(10, 30),
        stream=True,
        allow_redirects=True,
    ) as response:
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


def _fetch_postback_soup(session: requests.Session, item: CalendarRange) -> BeautifulSoup:
    initial_soup = _fetch_soup(session, item.url)
    form_data = _extract_form_data(initial_soup)
    form_data["__EVENTTARGET"] = item.event_target
    form_data["__EVENTARGUMENT"] = item.event_argument
    text = _fetch_text_bounded(session, "POST", item.url, data=form_data)
    return BeautifulSoup(text, "html.parser")


def _extract_form_data(soup: BeautifulSoup) -> dict[str, str]:
    form_data: dict[str, str] = {}
    form = soup.select_one("form#aspnetForm")
    if form is None:
        raise ValueError("Cannot post back IQM2 page without ASP.NET form")

    for input_tag in form.select("input[name]"):
        name = input_tag.get("name", "")
        input_type = (input_tag.get("type") or "").lower()
        if input_type in {"checkbox", "radio"} and not input_tag.has_attr("checked"):
            continue
        form_data[name] = input_tag.get("value", "")

    for select in form.select("select[name]"):
        name = select.get("name", "")
        selected = select.select_one("option[selected]")
        if selected is None:
            selected = select.select_one("option")
        form_data[name] = selected.get("value", "") if selected is not None else ""

    return form_data


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


def _discover_ranges(soup: BeautifulSoup, base_url: str) -> list[CalendarRange]:
    range_box = soup.select_one("#ContentPlaceholder1_lblCalendarRange")
    if range_box is None:
        return []

    ranges: dict[str, CalendarRange] = {}
    for link in range_box.select("a[href]"):
        label = _clean_text(link)
        if not label or label.lower() == "see more...":
            continue
        href = link.get("href", "")
        postback = _postback_target(href) or _postback_target(link.get("onclick", ""))
        if postback:
            event_target, event_argument = postback
            ranges[label] = CalendarRange(
                label=label,
                url=base_url,
                event_target=event_target,
                event_argument=event_argument,
            )
            continue
        if "Calendar.aspx" in href:
            full_url = _emit_url(href, base_url, "calendar_range", label)
            if full_url:
                ranges[label] = CalendarRange(label=label, url=full_url)

    return list(ranges.values())


def _postback_target(value: str) -> tuple[str, str] | None:
    match = re.search(r"__doPostBack\('([^']*)','([^']*)'\)", value)
    if not match:
        return None
    return match.group(1), match.group(2)


def _scrape_soup(soup: BeautifulSoup, base_url: str, stats: ScrapeStats) -> list[dict]:
    rows = soup.select("#ContentPlaceholder1_pnlMeetings div.Row.MeetingRow")
    expected = _expected_count(soup)
    if expected and not rows:
        raise ValueError(f"IQM2 summary says {expected} meetings, but no meeting rows were found")

    meetings: list[dict] = []
    for row in rows:
        meeting = _parse_meeting_row(row, base_url, stats)
        _validate_schema(meeting)
        meetings.append(meeting)
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
    meeting_status = _canonical_status(title_text, vendor_status, urls, stats)
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


def _canonical_status(
    meeting_title: str,
    vendor_status: str,
    urls: dict[str, str],
    stats: ScrapeStats,
) -> str:
    if CANCELLED_RE.search(meeting_title):
        return "Cancelled"
    if vendor_status.lower() == "cancelled":
        stats.placeholder_links["status:cancelled_from_tooltip_not_title"] += 1
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
    return parsed.strftime("%Y-%m-%d"), parsed.strftime("%-I:%M %p")


def _parse_tooltip_date(value: str) -> tuple[str, str]:
    match = DATE_TEXT_RE.search(value.strip())
    if not match:
        return "", ""
    month, day, year, time_text = match.groups()
    parsed = datetime.strptime(f"{month.title()} {day}, {year} {time_text.upper()}", "%B %d, %Y %I:%M %p")
    return parsed.strftime("%Y-%m-%d"), parsed.strftime("%-I:%M %p")


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
    logger.info("IQM2 emitted canonical statuses: %s", dict(sorted(stats.emitted_statuses.items())))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    scraped = scrape_calendar(DEFAULT_URL)
    print(f"Scraped {len(scraped)} Coolidge meetings")
    for meeting in scraped[:5]:
        print(meeting)
