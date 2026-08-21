"""City of Maricopa, Arizona Legistar calendar parser."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import logging
import re
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from polite_http import make_session


DEFAULT_URL = "https://maricopa.legistar.com/Calendar.aspx"
ALLOWED_HOSTS = {"maricopa.legistar.com"}
MAX_RESPONSE_BYTES = 4_000_000
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
VIDEO_ONCLICK_RE = re.compile(r"window\.open\(\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
REQUIRED_HEADERS = {
    "Name",
    "Meeting Date",
    "Meeting Time",
    "Meeting Location",
    "Meeting Details",
    "Agenda",
    "Minutes",
    "Video",
}
COUNCIL_TITLE_VOCABULARY = {
    "city council regular meeting",
    "city council special meeting",
    "city council work session",
}

logger = logging.getLogger(__name__)


def scrape_calendar(
    url: str = DEFAULT_URL,
    *,
    today: date | None = None,
) -> list[dict]:
    """Read current-month-forward City Council rows from the default view."""
    month_floor = (today or date.today()).replace(day=1)
    with make_session() as session:
        html = _fetch_html(session, url)

    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.rgMasterTable")
    if table is None:
        raise ValueError("Maricopa Legistar fingerprint missing table.rgMasterTable")
    headers = [_clean_text(cell) for cell in table.select("thead th")]
    header_index = {header: index for index, header in enumerate(headers) if header}
    missing = sorted(REQUIRED_HEADERS - set(header_index))
    if missing:
        raise ValueError(f"Maricopa Legistar headers changed; missing={missing} observed={headers}")

    stats: Counter[str] = Counter()
    meetings: list[dict] = []
    for row_number, row in enumerate(table.select("tbody > tr"), start=1):
        stats["rows_seen"] += 1
        cells = row.find_all("td", recursive=False)
        if len(cells) < len(headers):
            stats["rows_dropped_short"] += 1
            logger.warning(
                "Maricopa row %d dropped: cells=%d headers=%d",
                row_number,
                len(cells),
                len(headers),
            )
            continue
        meeting = _parse_row(
            cells,
            header_index,
            url,
            row_number,
            stats,
            month_floor=month_floor,
        )
        if meeting is None:
            continue
        _validate_meeting(meeting)
        meetings.append(meeting)
        stats["rows_emitted"] += 1

    if stats["rows_current_month_forward"] and not stats["rows_council_signal"]:
        raise ValueError(
            "Maricopa Legistar exposed current-month-forward rows but none "
            "matched the observed City Council title vocabulary"
        )
    logger.info(
        "Maricopa current-month-forward council scrape summary floor=%s stats=%s",
        month_floor.isoformat(),
        dict(stats),
    )
    if not meetings:
        if stats["rows_dropped_short"]:
            raise ValueError(
                "Maricopa Legistar contained malformed short rows, so an official zero cannot be witnessed"
            )
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Maricopa Legistar table witnessed zero current-month-forward City Council rows"
        )
    return meetings


def _fetch_html(session, url: str) -> str:
    _validate_source_url(url, "request")
    try:
        response_context = session.get(
            url, timeout=(10, 30), stream=True, allow_redirects=True
        )
    except requests.exceptions.SSLError:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning("Maricopa official Legistar source failed verified TLS")
        raise
    with response_context as response:
        if getattr(response, "status_code", None) in {401, 403, 429}:
            logger.warning("health_empty_kind=source_blocked")
        response.raise_for_status()
        _validate_source_url(response.url, "redirect")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Maricopa calendar exceeded {MAX_RESPONSE_BYTES} bytes")
        return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _parse_row(
    cells: list[Tag],
    header_index: dict[str, int],
    base_url: str,
    row_number: int,
    stats: Counter[str],
    *,
    month_floor: date | None = None,
) -> dict | None:
    title = _clean_text(cells[header_index["Name"]])
    raw_date = _clean_text(cells[header_index["Meeting Date"]])
    if not title or not raw_date:
        stats["rows_dropped_missing_identity"] += 1
        logger.warning(
            "Maricopa row %d dropped: title=%r date=%r",
            row_number,
            title,
            raw_date,
        )
        return None
    try:
        meeting_day = datetime.strptime(raw_date, "%m/%d/%Y").date()
        meeting_date = meeting_day.isoformat()
    except ValueError:
        stats["rows_dropped_bad_date"] += 1
        logger.warning("Maricopa row %d dropped: unparsed date=%r", row_number, raw_date)
        return None

    effective_floor = month_floor or date.today().replace(day=1)
    if meeting_day < effective_floor:
        stats["rows_dropped_before_month"] += 1
        logger.warning(
            "Maricopa row %d dropped before month floor: date=%s floor=%s title=%r",
            row_number,
            meeting_date,
            effective_floor.isoformat(),
            title,
        )
        return None

    stats["rows_current_month_forward"] += 1
    normalized_title = " ".join(title.casefold().split())
    if normalized_title not in COUNCIL_TITLE_VOCABULARY:
        if normalized_title.startswith("city council"):
            raise ValueError(
                "Maricopa current-window row uses an unreviewed City Council "
                f"title: {title!r}"
            )
        stats["rows_dropped_non_council"] += 1
        logger.warning(
            "Maricopa current-window row dropped for non-council body: "
            "row=%d date=%s title=%r",
            row_number,
            meeting_date,
            title,
        )
        return None
    stats["rows_council_signal"] += 1

    raw_time = _clean_text(cells[header_index["Meeting Time"]])
    meeting_time = _normalize_time(raw_time, row_number, stats)
    location = _clean_text(cells[header_index["Meeting Location"]])
    meeting_id = _meeting_id(cells[header_index["Meeting Details"]], row_number)
    if not meeting_id:
        stats["rows_dropped_missing_id"] += 1
        return None

    agenda_url = _labeled_link(
        cells[header_index["Agenda"]], "Agenda", base_url, "agenda_url", meeting_id, stats
    )
    minutes_url = _labeled_link(
        cells[header_index["Minutes"]], "Minutes", base_url, "minutes_url", meeting_id, stats
    )
    video_url = _video_link(
        cells[header_index["Video"]], base_url, meeting_id, stats
    )
    status = _canonical_status(title, location, agenda_url, minutes_url, stats)
    stats[f"status_{status.lower().replace(' ', '_')}"] += 1

    return {
        "meeting_title": title,
        "meeting_date": meeting_date,
        "meeting_time": meeting_time,
        "meeting_location": location,
        "meeting_status": status,
        "agenda_url": agenda_url,
        "minutes_url": minutes_url,
        "video_url": video_url,
        "agenda_packet_url": "",
        "ecomment_url": "",
        "meeting_id": meeting_id,
    }


def _normalize_time(raw_time: str, row_number: int, stats: Counter[str]) -> str:
    if not raw_time:
        stats["time_absent"] += 1
        return ""
    try:
        return datetime.strptime(raw_time.upper(), "%I:%M %p").strftime("%I:%M %p").lstrip("0")
    except ValueError:
        stats["time_unparsed"] += 1
        logger.warning("Maricopa row %d left time empty: unparsed value=%r", row_number, raw_time)
        return ""


def _meeting_id(cell: Tag, row_number: int) -> str:
    link = cell.select_one("a[href*='MeetingDetail.aspx']")
    href = str(link.get("href") or "") if link else ""
    values = parse_qs(urlparse(href).query).get("ID")
    meeting_id = values[0] if values else ""
    if not meeting_id or not meeting_id.isdigit():
        logger.warning(
            "Maricopa row %d missing numeric meeting ID: href=%r",
            row_number,
            href,
        )
        return ""
    return meeting_id


def _labeled_link(
    cell: Tag,
    label: str,
    base_url: str,
    field: str,
    meeting_id: str,
    stats: Counter[str],
) -> str:
    link = next(
        (candidate for candidate in cell.select("a[href]") if _clean_text(candidate) == label),
        None,
    )
    if link is None:
        stats[f"{field}_absent"] += 1
        return ""
    emitted = _emit_url(str(link.get("href") or ""), base_url, field, meeting_id)
    if emitted:
        stats[f"{field}_emitted"] += 1
    else:
        stats[f"{field}_rejected"] += 1
    return emitted


def _video_link(
    cell: Tag,
    base_url: str,
    meeting_id: str,
    stats: Counter[str],
) -> str:
    link = next(
        (candidate for candidate in cell.select("a") if _clean_text(candidate) == "Video"),
        None,
    )
    if link is None:
        stats["video_url_absent"] += 1
        return ""
    href = str(link.get("href") or "").strip()
    if href in {"", "#"}:
        onclick = str(link.get("onclick") or "")
        match = VIDEO_ONCLICK_RE.search(onclick)
        if match:
            href = match.group(1)
        else:
            stats["video_url_placeholder_without_fallback"] += 1
            logger.warning(
                "Maricopa meeting %s left video empty: placeholder href=%r onclick=%r",
                meeting_id,
                str(link.get("href") or ""),
                onclick,
            )
            return ""
    emitted = _emit_url(href, base_url, "video_url", meeting_id)
    if emitted:
        stats["video_url_emitted"] += 1
    else:
        stats["video_url_rejected"] += 1
    return emitted


def _emit_url(href: str, base_url: str, field: str, meeting_id: str) -> str:
    raw = href.strip()
    if not raw or raw.lower().startswith(
        ("javascript:", "data:", "vbscript:", "file:", "mailto:", "ftp:", "gopher:")
    ):
        logger.warning("Maricopa meeting %s rejected %s href=%r", meeting_id, field, href)
        return ""
    absolute = urljoin(base_url, raw)
    parsed = urlparse(absolute)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        logger.warning("Maricopa meeting %s rejected %s URL=%r", meeting_id, field, absolute)
        return ""
    return absolute


def _canonical_status(
    title: str,
    location: str,
    agenda_url: str,
    minutes_url: str,
    stats: Counter[str],
) -> str:
    if CANCELLED_RE.search(title):
        return "Cancelled"
    if CANCELLED_RE.search(location):
        stats["unsupported_location_cancellation"] += 1
        logger.warning(
            "Maricopa cancellation signal appears in location but not title; "
            "canonical status remains document-evidence based: title=%r location=%r",
            title,
            location,
        )
    if minutes_url:
        return "Minutes Available"
    if agenda_url:
        return "Agenda Available"
    return "Scheduled"


def _validate_source_url(url: str, context: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        raise ValueError(f"Maricopa {context} URL is not allowlisted: {url!r}")


def _clean_text(node: Tag) -> str:
    return BeautifulSoup(node.decode_contents(), "html.parser").get_text(" ", strip=True)


def _validate_meeting(meeting: dict) -> None:
    expected = {
        "meeting_title", "meeting_date", "meeting_time", "meeting_location",
        "meeting_status", "agenda_url", "minutes_url", "video_url",
        "agenda_packet_url", "ecomment_url", "meeting_id",
    }
    if set(meeting) != expected:
        raise ValueError(f"Maricopa parser emitted wrong fields: {sorted(meeting)}")
    if any(not isinstance(value, str) for value in meeting.values()):
        raise TypeError("Maricopa parser emitted a non-string field")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    scraped = scrape_calendar()
    print(f"Scraped {len(scraped)} Maricopa meetings from the bounded current view")
