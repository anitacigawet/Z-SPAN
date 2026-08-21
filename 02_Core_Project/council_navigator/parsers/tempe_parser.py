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

BASE_URL = "https://tempe.hylandcloud.com/AgendaOnline/"
DEFAULT_URL = urljoin(BASE_URL, "Meetings/Search?dropid=4")
ALLOWED_HOSTS = {"tempe.hylandcloud.com"}
MAX_RESPONSE_BYTES = 3_000_000
CANONICAL_FIELDS = (
    "meeting_title", "meeting_date", "meeting_time", "meeting_location",
    "meeting_status", "agenda_url", "minutes_url", "video_url",
    "agenda_packet_url", "ecomment_url", "meeting_id",
)
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
COUNCIL_RE = re.compile(r"\bcity\s+council\b", re.IGNORECASE)


def scrape_calendar(url: str | None = None) -> list[dict[str, str]]:
    """Return current-month-forward Tempe City Council OnBase rows."""
    target = _search_url(url or DEFAULT_URL)
    with make_session() as session:
        html = _fetch_text_bounded(session, target)
    soup = BeautifulSoup(html, "html.parser")
    page_title = _clean_text(soup.title)
    meeting_rows = soup.select("tr.meeting-row")
    if not meeting_rows:
        meeting_rows = [
            row for row in soup.find_all("tr")
            if row.get("data-meeting-id") or row.find("a", id=re.compile(r"^lnkMeetingAgenda_"))
        ]
    page_text = _clean_text(soup)
    if "Meeting Search Results" not in page_title:
        logger.warning("vendor_fingerprint_failed expected=OnBase_Meeting_Search_Results title=%r", page_title)
        raise RuntimeError("Tempe OnBase fingerprint drifted")
    if not meeting_rows and not re.search(r"Showing\s+0\s+Meeting", page_text, re.IGNORECASE):
        logger.warning("vendor_fingerprint_failed reason=no_meeting_rows_without_zero_state")
        raise RuntimeError("Tempe OnBase row structure drifted")
    logger.info(
        "vendor_fingerprint witness=OnBase_Meeting_Search_Results_plus_meeting_rows rows=%d",
        len(meeting_rows),
    )

    floor = date.today().replace(day=1).isoformat()
    meetings: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    rows_seen = rows_dropped = historical = non_council = 0
    for row in meeting_rows:
        rows_seen += 1
        title_cell = row.find("td", attrs={"data-sortable-type": "mtgName"})
        time_cell = row.find("td", attrs={"data-sortable-type": "mtgTime"})
        location_cell = row.find("td", attrs={"data-sortable-type": "mtgLocation"})
        title = _clean_text(title_cell)
        if not title:
            rows_dropped += 1
            logger.warning("drop_row reason=meeting_title_absent row_id=%r", row.get("data-meeting-id"))
            continue
        if not COUNCIL_RE.search(title):
            non_council += 1
            logger.info("drop_row reason=non_city_council title=%r", title)
            continue
        dt = _parse_datetime(_clean_text(time_cell), title)
        if dt is None:
            rows_dropped += 1
            continue
        meeting_date = dt.date().isoformat()
        if meeting_date < floor:
            historical += 1
            continue
        meeting_id = str(row.get("data-meeting-id") or _id_from_links(row) or "")
        if not meeting_id:
            logger.warning("meeting_id_absent date=%s title=%r", meeting_date, title)
        agenda_url = _row_url(row, ("lnkMeetingAgenda_",), target, "agenda_url", title)
        packet_url = _row_url(row, ("lnkAgendaPacket_", "lnkMeetingAgendaDoc_"), target, "agenda_packet_url", title)
        minutes_url = _row_url(
            row,
            ("lnkMinutesPacket_", "lnkMeetingSummary_", "lnkMinutes_"),
            target,
            "minutes_url",
            title,
        )
        video_url = _row_url(row, ("lnkMeetingVideo_",), target, "video_url", title)
        ecomment_url = _row_url(row, ("lnkEComment_",), target, "ecomment_url", title)
        meeting_time = f"{dt.hour % 12 or 12}:{dt.minute:02d} {dt.strftime('%p')}"
        location = _clean_text(location_cell)
        if not location:
            logger.info("field_absent field=meeting_location date=%s title=%r reason=no_row_signal", meeting_date, title)
        status = _status(title, agenda_url, packet_url, minutes_url)
        key = (meeting_date, meeting_time, title.casefold())
        if key in seen:
            rows_dropped += 1
            logger.warning("drop_row reason=duplicate date=%s time=%s title=%r", meeting_date, meeting_time, title)
            continue
        seen.add(key)
        meeting = {
            "meeting_title": title,
            "meeting_date": meeting_date,
            "meeting_time": meeting_time,
            "meeting_location": location,
            "meeting_status": status,
            "agenda_url": agenda_url,
            "minutes_url": minutes_url,
            "video_url": video_url,
            "agenda_packet_url": packet_url,
            "ecomment_url": ecomment_url,
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


def _search_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or _host(url) not in ALLOWED_HOSTS:
        raise ValueError("Tempe source URL must use HTTPS on its official OnBase host")
    if "/meetings/search" in parsed.path.casefold():
        return url
    return DEFAULT_URL


def _fetch_text_bounded(session: requests.Session, url: str) -> str:
    with session.get(url, timeout=30, stream=True, allow_redirects=True) as response:
        if _host(response.url) not in ALLOWED_HOSTS:
            raise ValueError(f"Tempe redirect reached disallowed host: {_host(response.url)}")
        body = bytearray()
        for chunk in response.iter_content(64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Tempe response exceeded {MAX_RESPONSE_BYTES} bytes")
        if response.status_code in {401, 403, 429}:
            logger.warning("health_empty_kind=source_blocked")
        response.raise_for_status()
        return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _parse_datetime(text: str, title: str) -> datetime | None:
    normalized = " ".join(text.split())
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p"):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    logger.warning("drop_row reason=meeting_datetime_unparseable title=%r value=%r", title, text)
    return None


def _row_url(row, prefixes: tuple[str, ...], base_url: str, field: str, title: str) -> str:
    anchor = row.find("a", id=lambda value: isinstance(value, str) and value.startswith(prefixes))
    if anchor is None or not anchor.get("href"):
        logger.info("field_absent field=%s title=%r reason=no_same_row_link", field, title)
        return ""
    absolute = urljoin(base_url, str(anchor.get("href")).strip())
    if urlparse(absolute).scheme not in {"http", "https"} or _host(absolute) not in ALLOWED_HOSTS:
        logger.warning(
            "drop_url field=%s title=%r href=%r reason=scheme_or_host_not_allowlisted",
            field, title, anchor.get("href"),
        )
        return ""
    return absolute


def _id_from_links(row) -> str:
    for anchor in row.find_all("a", id=True):
        match = re.search(r"_([0-9]+)$", str(anchor.get("id")))
        if match:
            return match.group(1)
    return ""


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
            raise ValueError(f"Tempe row {index} violates canonical schema")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    print(json.dumps(scrape_calendar(DEFAULT_URL), indent=2))
