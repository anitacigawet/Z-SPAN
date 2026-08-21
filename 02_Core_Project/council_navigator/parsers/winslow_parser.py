from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from polite_http import make_session


logger = logging.getLogger(__name__)

DEFAULT_URL = "https://winslowaz.suiteonemedia.com/"
SOURCE_HOSTS = {"winslowaz.suiteonemedia.com"}
VIDEO_HOSTS = SOURCE_HOSTS | {
    "youtube.com", "www.youtube.com", "youtu.be", "vimeo.com", "www.vimeo.com", "player.vimeo.com",
}
MAX_RESPONSE_BYTES = 3_000_000
CANONICAL_FIELDS = (
    "meeting_title", "meeting_date", "meeting_time", "meeting_location",
    "meeting_status", "agenda_url", "minutes_url", "video_url",
    "agenda_packet_url", "ecomment_url", "meeting_id",
)
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
COUNCIL_RE = re.compile(r"\bcity\s+council\b", re.IGNORECASE)


def scrape_calendar(url: str | None = None) -> list[dict[str, str]]:
    """Return current-month-forward Winslow City Council rows."""
    target = _source_url(url or DEFAULT_URL)
    with make_session() as session:
        html = _fetch_text_bounded(session, target)
    soup = BeautifulSoup(html, "html.parser")
    page_title = _clean_text(soup.title)
    tables = soup.find_all(
        "table",
        class_=["upcomingEventsTable", "recentEventsTable", "eventTable"],
    )
    if "WINSLOWAZ Meeting Management" not in page_title or not tables:
        logger.warning(
            "vendor_fingerprint_failed expected=WINSLOWAZ_Meeting_Management_plus_event_tables "
            "title=%r tables=%d",
            page_title, len(tables),
        )
        raise RuntimeError("Winslow SuiteOne fingerprint drifted")
    logger.info(
        "vendor_fingerprint witness=WINSLOWAZ_Meeting_Management_plus_event_tables tables=%d",
        len(tables),
    )
    logger.warning(
        "field_absence fields=meeting_location,ecomment_url "
        "reason=suiteone_table_exposes_no_same_row_signal"
    )

    floor = date.today().replace(day=1).isoformat()
    meetings: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    rows_seen = rows_dropped = historical = non_council = 0
    for table in tables:
        for row in table.select("tbody tr"):
            cells = row.find_all("td")
            if len(cells) < 7:
                rows_dropped += 1
                logger.warning("drop_row reason=column_count expected_at_least=7 actual=%d", len(cells))
                continue
            rows_seen += 1
            title_anchor = cells[0].find("a", href=True)
            title = _clean_text(title_anchor or cells[0])
            title = re.sub(r"\s*\(opens in new window\)\s*", " ", title, flags=re.IGNORECASE).strip()
            dt = _parse_datetime(_clean_text(cells[1]), title)
            if dt is None:
                rows_dropped += 1
                continue
            meeting_date = dt.date().isoformat()
            if meeting_date < floor:
                historical += 1
                continue
            if not COUNCIL_RE.search(title):
                non_council += 1
                logger.info(
                    "drop_row reason=non_city_council date=%s title=%r",
                    meeting_date,
                    title,
                )
                continue
            meeting_time = f"{dt.hour % 12 or 12}:{dt.minute:02d} {dt.strftime('%p')}"
            agenda_url = _cell_url(cells[2], target, "agenda_url", title, SOURCE_HOSTS)
            packet_url = _cell_url(cells[3], target, "agenda_packet_url", title, SOURCE_HOSTS)
            minutes_url = _cell_url(cells[4], target, "minutes_url", title, SOURCE_HOSTS)
            documents_url = _cell_url(cells[5], target, "agenda_packet_fallback_url", title, SOURCE_HOSTS)
            video_url = _cell_url(cells[6], target, "video_url", title, VIDEO_HOSTS)
            if not packet_url and documents_url:
                packet_url = documents_url
            meeting_id = _meeting_id(title_anchor.get("href", "") if title_anchor else "")
            if not meeting_id:
                logger.warning("meeting_id_absent date=%s title=%r", meeting_date, title)
            key = (meeting_date, meeting_time, title.casefold())
            if key in seen:
                rows_dropped += 1
                logger.warning("drop_row reason=duplicate date=%s time=%s title=%r", meeting_date, meeting_time, title)
                continue
            seen.add(key)
            status = _status(title, agenda_url, packet_url, minutes_url)
            meeting = {
                "meeting_title": title,
                "meeting_date": meeting_date,
                "meeting_time": meeting_time,
                "meeting_location": "",
                "meeting_status": status,
                "agenda_url": agenda_url,
                "minutes_url": minutes_url,
                "video_url": video_url,
                "agenda_packet_url": packet_url,
                "ecomment_url": "",
                "meeting_id": meeting_id,
            }
            meetings.append({field: meeting[field] for field in CANONICAL_FIELDS})

    _assert_schema(meetings)
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
    logger.info(
        "scrape_summary rows_seen=%d rows_accepted=%d rows_dropped=%d "
        "historical_ignored=%d non_council_ignored=%d current_floor=%s",
        rows_seen, len(meetings), rows_dropped, historical, non_council, floor,
    )
    return meetings


def _source_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("Winslow source URL must use HTTPS")
    if _host(url) in SOURCE_HOSTS:
        return url
    # The catalog carries the official city wrapper; its witnessed embedded
    # SuiteOne source is the only network surface this parser requests.
    if _host(url) in {"winslowaz.gov", "www.winslowaz.gov"}:
        return DEFAULT_URL
    raise ValueError("Winslow source URL must be the official city or SuiteOne host")


def _fetch_text_bounded(session: requests.Session, url: str) -> str:
    with session.get(url, timeout=30, stream=True, allow_redirects=True) as response:
        if _host(response.url) not in SOURCE_HOSTS:
            raise ValueError(f"Winslow redirect reached disallowed host: {_host(response.url)}")
        body = bytearray()
        for chunk in response.iter_content(64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Winslow response exceeded {MAX_RESPONSE_BYTES} bytes")
        if response.status_code in {401, 403, 429}:
            logger.warning("health_empty_kind=source_blocked")
        response.raise_for_status()
        return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _parse_datetime(text: str, title: str) -> datetime | None:
    normalized = " ".join(text.split())
    for fmt in ("%b %d, %Y | %I:%M %p", "%B %d, %Y | %I:%M %p"):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    logger.warning("drop_row reason=meeting_datetime_unparseable title=%r value=%r", title, text)
    return None


def _cell_url(cell, base_url: str, field: str, title: str, allowed_hosts: set[str]) -> str:
    anchor = cell.find("a", href=True)
    if anchor is None:
        logger.info("field_absent field=%s title=%r reason=no_same_row_link", field, title)
        return ""
    absolute = urljoin(base_url, str(anchor.get("href") or "").strip())
    if urlparse(absolute).scheme not in {"http", "https"} or _host(absolute) not in allowed_hosts:
        logger.warning(
            "drop_url field=%s title=%r href=%r reason=scheme_or_host_not_allowlisted",
            field, title, anchor.get("href"),
        )
        return ""
    return absolute


def _meeting_id(href: str) -> str:
    path = urlparse(urljoin(DEFAULT_URL, href)).path.rstrip("/")
    match = re.search(r"(?:meeting|event|id)[/-]([0-9]+)(?:/|$)", path, re.IGNORECASE)
    return match.group(1) if match else ""


def _status(title: str, agenda: str, packet: str, minutes: str) -> str:
    if CANCELLED_RE.search(title):
        return "Cancelled"
    if minutes:
        return "Minutes Available"
    if agenda or packet:
        return "Agenda Available"
    return "Scheduled"


def _clean_text(value: object) -> str:
    return " ".join(BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True).split())


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def _assert_schema(rows: list[dict[str, str]]) -> None:
    for index, row in enumerate(rows):
        if tuple(row) != CANONICAL_FIELDS or any(not isinstance(value, str) for value in row.values()):
            raise ValueError(f"Winslow row {index} violates canonical schema")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    print(json.dumps(scrape_calendar(DEFAULT_URL), indent=2))
