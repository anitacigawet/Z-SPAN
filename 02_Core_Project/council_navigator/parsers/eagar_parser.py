from __future__ import annotations

import json
import logging
import re
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from polite_http import make_session

AGENDAS_PAGE = "https://www.eagaraz.gov/o/tofe/page/agendas-minutes"


logger = logging.getLogger(__name__)

FETCH_HOSTS = {"eagaraz.gov", "www.eagaraz.gov"}
EMIT_HOSTS = FETCH_HOSTS | {
    "5il.co",
    "core-docs.s3.amazonaws.com",
    "core-docs.s3.us-east-1.amazonaws.com",
    "files-backend.assets.thrillshare.com",
}
MAX_RESPONSE_BYTES = 4_000_000
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
MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2,
    "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5,
    "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
DATE_RE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+([0-9]{1,2}),?\s+([0-9]{4})\b",
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r"(?<!\d)([0-9]{1,2})(?::([0-9]{2}))?\s*([AP])\.?M\.?(?=\s|$|[^\w.])",
    re.IGNORECASE,
)
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
NON_MEETING_RE = re.compile(r"\bpossible quorum\b|\bno meeting\b", re.IGNORECASE)
COUNCIL_RE = re.compile(r"\b(?:town|city)\s+council\b", re.IGNORECASE)
NON_COUNCIL_BODY_RE = re.compile(
    r"\b(?:planning\s+(?:and\s+zoning\s+)?commission|board\s+of\s+adjustment|"
    r"parks?\s+(?:and\s+recreation\s+)?board|library\s+board)\b",
    re.IGNORECASE,
)


def scrape_calendar(url: str | None = None) -> list[dict[str, str]]:
    """Read Eagar's current table, failing loudly when its challenge blocks it."""
    target = url or AGENDAS_PAGE
    with make_session() as session:
        html = _fetch_text_bounded(session, target)
    soup = BeautifulSoup(html, "html.parser")
    title = _clean_text(soup.title)
    visible_text = _clean_text(soup)
    if title == "Client Challenge" or "JavaScript is disabled in your browser" in visible_text:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "architectural_blocker source=%s failure=client_challenge result=raised_not_empty",
            target,
        )
        raise RuntimeError("Eagar official agenda page returned a client challenge, not meeting data")

    table = _find_meetings_table(soup)
    if table is None:
        logger.warning(
            "vendor_fingerprint_failed expected=table_headers_Date_Meeting_Agenda title=%r body_sample=%r",
            title,
            visible_text[:300],
        )
        raise ValueError("Eagar agendas-and-minutes table surface drifted")
    logger.info("vendor_fingerprint witness=table_headers_Date_Meeting_Agenda")
    logger.warning(
        "field_absence fields=meeting_location,ecomment_url "
        "reason=agendas_minutes_table_exposes_no_per_row_signal"
    )

    current_floor = date.today().replace(day=1).isoformat()
    meetings: list[dict[str, str]] = []
    latest_observed = ""
    rows_seen = rows_dropped = historical = 0
    for row in table.find_all("tr"):
        cells = row.find_all(["td", "th"], recursive=False)
        if len(cells) < 2 or all(cell.name == "th" for cell in cells):
            continue
        rows_seen += 1
        date_text = _clean_text(cells[0])
        meeting_date = _extract_date(date_text)
        if not meeting_date:
            rows_dropped += 1
            logger.warning("drop_row reason=date_unparseable date_cell=%r", date_text[:240])
            continue
        latest_observed = max(latest_observed, meeting_date)
        if meeting_date < current_floor:
            historical += 1
            continue
        agenda_cell = cells[1]
        agenda_text = _clean_text(agenda_cell)
        row_text = _clean_text(row)
        if NON_MEETING_RE.search(agenda_text):
            rows_dropped += 1
            logger.warning(
                "drop_row reason=notice_or_no_meeting date=%s agenda_text=%r",
                meeting_date,
                agenda_text,
            )
            continue
        if not COUNCIL_RE.search(row_text):
            if NON_COUNCIL_BODY_RE.search(row_text):
                rows_dropped += 1
                logger.warning(
                    "drop_row reason=explicit_non_council_body date=%s row_text=%r",
                    meeting_date,
                    row_text[:300],
                )
                continue
            raise RuntimeError(
                "Eagar current meeting row lacks explicit Town/City Council evidence: "
                f"date={meeting_date} row={row_text[:300]!r}"
            )
        agenda_anchor = agenda_cell.find("a", href=True)
        if agenda_anchor is None:
            rows_dropped += 1
            logger.warning(
                "drop_row reason=agenda_link_missing date=%s agenda_text=%r",
                meeting_date,
                agenda_text,
            )
            continue
        agenda_url = _emit_url(agenda_anchor.get("href", ""), target, "agenda_url", agenda_text)
        if not agenda_url:
            rows_dropped += 1
            continue

        packet_url = _first_cell_url(cells, 2, target, "agenda_packet_url", agenda_text)
        minutes_url = _first_cell_url(cells, 3, target, "minutes_url", agenda_text)
        title_text = _clean_title(_clean_text(agenda_anchor) or agenda_text)
        status = (
            "Cancelled"
            if CANCELLED_RE.search(f"{date_text} {title_text}")
            else "Minutes Available"
            if minutes_url
            else "Agenda Available"
        )
        meeting_id = urlparse(agenda_url).path.rstrip("/").split("/")[-1]
        if not meeting_id:
            logger.warning("meeting_id_absent date=%s agenda_url=%s", meeting_date, agenda_url)
        meeting = {
            "meeting_title": title_text,
            "meeting_date": meeting_date,
            "meeting_time": _extract_time(date_text),
            "meeting_location": "",
            "meeting_status": status,
            "agenda_url": agenda_url,
            "minutes_url": minutes_url,
            "video_url": "",
            "agenda_packet_url": packet_url,
            "ecomment_url": "",
            "meeting_id": meeting_id,
        }
        meetings.append({field: meeting[field] for field in CANONICAL_FIELDS})

    _assert_schema(meetings)
    logger.info(
        "scrape_summary rows_seen=%d rows_accepted=%d rows_dropped=%d historical_ignored=%d "
        "current_floor=%s latest_observed=%s",
        rows_seen,
        len(meetings),
        rows_dropped,
        historical,
        current_floor,
        latest_observed,
    )
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "honest_empty evidence=verified_meetings_table latest_observed=%s current_floor=%s",
            latest_observed,
            current_floor,
        )
    return meetings


def _fetch_text_bounded(session: requests.Session, url: str) -> str:
    with session.get(url, timeout=35, stream=True, allow_redirects=True) as response:
        if _host(response.url) not in FETCH_HOSTS:
            raise ValueError(f"Eagar redirect reached disallowed host: {_host(response.url)}")
        body = bytearray()
        for chunk in response.iter_content(64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Eagar response exceeded {MAX_RESPONSE_BYTES} bytes")
        if response.status_code in {401, 403, 429}:
            logger.warning("health_empty_kind=source_blocked")
        response.raise_for_status()
        return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _find_meetings_table(soup: BeautifulSoup) -> object | None:
    for table in soup.find_all("table"):
        headers = [_clean_text(cell).casefold() for cell in table.find_all("th")]
        if any(header == "date" for header in headers) and any(
            "meeting agenda" in header for header in headers
        ):
            return table
    return None


def _first_cell_url(
    cells: list[object],
    index: int,
    base_url: str,
    field: str,
    row_label: str,
) -> str:
    if index >= len(cells):
        return ""
    anchor = cells[index].find("a", href=True)
    if anchor is None:
        return ""
    return _emit_url(anchor.get("href", ""), base_url, field, row_label)


def _emit_url(href: str, base_url: str, field: str, row_label: str) -> str:
    absolute = urljoin(base_url, str(href or "").strip())
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or _host(absolute) not in EMIT_HOSTS:
        logger.warning(
            "drop_url field=%s row=%r href=%r host=%r reason=scheme_or_host_not_allowlisted",
            field,
            row_label,
            href,
            _host(absolute),
        )
        return ""
    return absolute


def _extract_date(text: str) -> str:
    match = DATE_RE.search(text[:500])
    if not match:
        logger.warning("meeting_date_unparseable reason=no_date_pattern text=%r", text[:240])
        return ""
    month = MONTHS.get(match.group(1).casefold())
    if not month:
        logger.warning("meeting_date_unparseable reason=unknown_month text=%r", text[:240])
        return ""
    try:
        return date(int(match.group(3)), month, int(match.group(2))).isoformat()
    except ValueError:
        logger.warning("meeting_date_unparseable reason=invalid_calendar_date text=%r", text[:240])
        return ""


def _extract_time(text: str) -> str:
    match = TIME_RE.search(text[:500])
    if not match:
        logger.info("meeting_time_absent date_cell=%r", text[:240])
        return ""
    hour = int(match.group(1))
    if not 1 <= hour <= 12:
        logger.warning("meeting_time_invalid raw=%r date_cell=%r", match.group(0), text[:240])
        return ""
    return f"{hour}:{match.group(2) or '00'} {match.group(3).upper()}M"


def _clean_title(text: str) -> str:
    title = re.sub(r"\s*-?\s*Click Here\s*$", "", text, flags=re.IGNORECASE)
    return " ".join(title.split()) or "Meeting"


def _clean_text(value: object) -> str:
    return " ".join(BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True).split())


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _assert_schema(meetings: list[dict[str, str]]) -> None:
    for index, meeting in enumerate(meetings):
        if tuple(meeting) != CANONICAL_FIELDS:
            raise ValueError(f"Eagar row {index} schema mismatch: {tuple(meeting)}")
        if any(not isinstance(value, str) for value in meeting.values()):
            raise ValueError(f"Eagar row {index} contains a non-string value")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    print(json.dumps(scrape_calendar(AGENDAS_PAGE), indent=2))
