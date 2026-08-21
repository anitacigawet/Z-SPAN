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

DEFAULT_URL = "https://www.williamsaz.gov/government/meetings/city_council"
ALLOWED_HOSTS = {"williamsaz.gov", "www.williamsaz.gov"}
MAX_RESPONSE_BYTES = 2_000_000
CANONICAL_FIELDS = (
    "meeting_title", "meeting_date", "meeting_time", "meeting_location",
    "meeting_status", "agenda_url", "minutes_url", "video_url",
    "agenda_packet_url", "ecomment_url", "meeting_id",
)
DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"([0-9]{1,2}),?\s+([0-9]{4})",
    re.IGNORECASE,
)
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)


def scrape_calendar(url: str | None = None) -> list[dict[str, str]]:
    """Return current-month-forward Williams City Council rows."""
    target = _validated_source_url(url or DEFAULT_URL)
    with make_session() as session:
        html = _fetch_text_bounded(session, target)
    soup = BeautifulSoup(html, "html.parser")
    page_title = _clean_text(soup.title)
    table, headers = _council_table(soup)
    if "City Council" not in page_title or table is None:
        logger.warning(
            "vendor_fingerprint_failed title=%r expected=City_Council_plus_Date_Agenda_Packets_Minutes_table",
            page_title,
        )
        raise RuntimeError("Williams City Council table fingerprint drifted")
    logger.info(
        "vendor_fingerprint witness=City_Council_title_plus_headers headers=%r",
        headers,
    )
    logger.warning(
        "field_absence fields=meeting_time,meeting_location,video_url,ecomment_url "
        "reason=city_council_table_exposes_no_per_row_signal"
    )

    floor = date.today().replace(day=1).isoformat()
    meetings: list[dict[str, str]] = []
    rows_seen = rows_dropped = historical = 0
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 4:
            logger.warning("drop_row reason=column_count expected_at_least=4 actual=%d", len(cells))
            rows_dropped += 1
            continue
        date_label = _clean_text(cells[0])
        meeting_date = _extract_date(date_label)
        if not meeting_date:
            if date_label:
                logger.warning("drop_row reason=meeting_date_unparseable label=%r", date_label)
                rows_dropped += 1
            continue
        rows_seen += 1
        if meeting_date < floor:
            historical += 1
            continue
        if re.search(r"\bno\s+meeting\b", date_label, re.IGNORECASE):
            rows_dropped += 1
            logger.warning("drop_row reason=explicit_no_meeting date=%s label=%r", meeting_date, date_label)
            continue

        descriptor = DATE_RE.sub("", date_label, count=1)
        descriptor = re.sub(r"\([^)]*\)", " ", descriptor)
        descriptor = " ".join(descriptor.strip(" -\u2013\u2014").split())
        title = "City Council" if not descriptor else f"City Council {descriptor}"
        links_by_header = {
            headers[index]: cells[index]
            for index in range(min(len(headers), len(cells)))
        }
        agenda_url = _first_url(links_by_header.get("agenda"), target, "agenda_url", date_label)
        packet_url = _first_url(links_by_header.get("packets"), target, "agenda_packet_url", date_label)
        minutes_url = _first_url(links_by_header.get("minutes"), target, "minutes_url", date_label)
        status = _status(title, agenda_url, packet_url, minutes_url)
        meeting = {
            "meeting_title": title,
            "meeting_date": meeting_date,
            "meeting_time": "",
            "meeting_location": "",
            "meeting_status": status,
            "agenda_url": agenda_url,
            "minutes_url": minutes_url,
            "video_url": "",
            "agenda_packet_url": packet_url,
            "ecomment_url": "",
            "meeting_id": "",
        }
        meetings.append({field: meeting[field] for field in CANONICAL_FIELDS})

    _assert_schema(meetings)
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
    logger.info(
        "scrape_summary rows_seen=%d rows_accepted=%d rows_dropped=%d historical_ignored=%d current_floor=%s",
        rows_seen, len(meetings), rows_dropped, historical, floor,
    )
    return meetings


def _validated_source_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or _host(url) not in ALLOWED_HOSTS:
        raise ValueError("Williams source URL must use HTTPS on the official city host")
    return url


def _fetch_text_bounded(session: requests.Session, url: str) -> str:
    with session.get(url, timeout=30, stream=True, allow_redirects=True) as response:
        if _host(response.url) not in ALLOWED_HOSTS:
            raise ValueError(f"Williams redirect reached disallowed host: {_host(response.url)}")
        body = bytearray()
        for chunk in response.iter_content(64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Williams response exceeded {MAX_RESPONSE_BYTES} bytes")
        if response.status_code in {401, 403, 429}:
            logger.warning("health_empty_kind=source_blocked")
        response.raise_for_status()
        return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _council_table(soup: BeautifulSoup):
    for table in soup.find_all("table"):
        first_row = table.find("tr")
        if first_row is None:
            continue
        headers = [_clean_text(cell).casefold() for cell in first_row.find_all(["th", "td"])]
        if all(required in headers for required in ("date", "agenda", "packets", "minutes")):
            return table, headers
    return None, []


def _extract_date(text: str) -> str:
    match = DATE_RE.search(text[:300])
    if not match:
        logger.warning("meeting_date_absent_or_unparseable label=%r", text[:240])
        return ""
    try:
        return datetime.strptime(" ".join(match.groups()), "%B %d %Y").date().isoformat()
    except ValueError:
        logger.warning("meeting_date_invalid label=%r raw=%r", text[:240], match.group(0))
        return ""


def _first_url(cell, base_url: str, field: str, row_label: str) -> str:
    if cell is None:
        logger.warning("field_absent field=%s row=%r reason=expected_column_missing", field, row_label)
        return ""
    anchor = cell.find("a", href=True)
    if anchor is None:
        logger.info("field_absent field=%s row=%r reason=no_same_row_link", field, row_label)
        return ""
    absolute = urljoin(base_url, str(anchor.get("href") or "").strip())
    if urlparse(absolute).scheme not in {"http", "https"} or _host(absolute) not in ALLOWED_HOSTS:
        logger.warning(
            "drop_url field=%s row=%r href=%r reason=scheme_or_host_not_allowlisted",
            field, row_label, anchor.get("href"),
        )
        return ""
    return absolute


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
            raise ValueError(f"Williams row {index} violates canonical schema")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    print(json.dumps(scrape_calendar(DEFAULT_URL), indent=2))
