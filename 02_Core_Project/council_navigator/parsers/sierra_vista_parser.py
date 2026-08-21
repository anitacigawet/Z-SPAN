"""Current-month-forward Sierra Vista City Council calendar parser."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import logging
import re
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from polite_http import make_session

__all__ = ["scrape_calendar"]

logger = logging.getLogger(__name__)

_DEFAULT_URL = (
    "https://www.sierravistaaz.gov/our-city/advanced-components/list-detail-pages/"
    "calendar-list/-toggle-allupcoming/-sortn-EDate/-sortd-asc"
)
_ALLOWED_HOSTS = {"sierravistaaz.gov", "www.sierravistaaz.gov"}
_MAX_RESPONSE_BYTES = 8_000_000
_CHUNK_SIZE = 65_536
_DATE_RE = re.compile(r"(?<!\d)(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])/(20\d{2})(?!\d)")
_TIME_RE = re.compile(
    r"(?<!\d)(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([AP])\.?\s*M\.?(?=\s|$|[^\w.])",
    re.IGNORECASE,
)
_CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
_EVENT_ID_RE = re.compile(r"/Calendar/Event/(\d+)(?:/|$)", re.IGNORECASE)

_FIELDS = (
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


def scrape_calendar(url: str = _DEFAULT_URL) -> list[dict]:
    """Return official Sierra Vista council/work-session rows from this month forward.

    The official Granicus/GovAccess endpoint intermittently returns an Akamai
    403 to neutral scripted clients.  A witnessed access-denial response is an
    architectural blocker and therefore returns an explicitly logged honest
    empty result.  A 200 response whose expected calendar table has drifted
    fails loudly instead.
    """
    logger.warning(
        "Sierra Vista calendar list does not expose meeting_location; all emitted rows use an honest empty value"
    )
    session = make_session()
    try:
        status, final_url, html = _fetch_one_bounded(session, url)
    except requests.exceptions.SSLError:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning("Sierra Vista official calendar failed verified TLS")
        return []

    if status in {401, 403, 429}:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Sierra Vista official calendar blocked the neutral paced request: "
            "failure_shape=honest-empty missing_data_scope=all_current_and_future_meetings "
            "status=403 final_url=%s blocker=akamai_access_denied",
            final_url,
        )
        return []
    if status != 200:
        raise RuntimeError(f"Sierra Vista official calendar returned HTTP {status}: {final_url}")

    soup = BeautifulSoup(html, "html.parser")
    table, headers = _calendar_table(soup)
    if table is None:
        title = _clean(soup.title.get_text(" ", strip=True) if soup.title else "")
        raise RuntimeError(
            "Sierra Vista calendar surface drifted: expected Event and Date/Time table headers "
            f"were not witnessed (title={title!r}, final_url={final_url!r})"
        )

    title = _clean(soup.title.get_text(" ", strip=True) if soup.title else "")
    page_text = _clean(soup.get_text(" ", strip=True))[:10_000]
    if "sierra vista" not in f"{title} {page_text}".lower():
        raise RuntimeError("Sierra Vista vendor fingerprint failed: city identity not witnessed in calendar markup")
    logger.info(
        "Sierra Vista Granicus/GovAccess fingerprint witnessed: final_url=%s headers=%s title=%r",
        final_url,
        headers,
        title,
    )

    rows_seen = 0
    accepted = 0
    drop_reasons: Counter[str] = Counter()
    meetings: list[dict] = []
    seen: set[tuple[str, str]] = set()
    cutoff = date.today().replace(day=1)

    for row_index, row in enumerate(table.select("tbody tr"), start=1):
        rows_seen += 1
        cells = row.find_all("td", recursive=False)
        if not cells:
            cells = row.find_all("td")
        if len(cells) < len(headers):
            drop_reasons["short_row"] += 1
            logger.warning(
                "Sierra Vista calendar row dropped: reason=short_row row_index=%d cells=%d headers=%d text=%r",
                row_index,
                len(cells),
                len(headers),
                _clean(row.get_text(" ", strip=True))[:300],
            )
            continue

        by_header = {headers[index]: cells[index] for index in range(min(len(headers), len(cells)))}
        event_cell = by_header.get("event")
        date_cell = by_header.get("date/time") or by_header.get("date")
        if event_cell is None or date_cell is None:
            raise RuntimeError(f"Sierra Vista semantic calendar columns drifted: {headers}")

        meeting_title = _clean(event_cell.get_text(" ", strip=True))
        if not _is_council_title(meeting_title):
            drop_reasons["not_city_council_meeting"] += 1
            continue

        raw_date_time = _clean(date_cell.get_text(" ", strip=True))
        meeting_date = _extract_date(raw_date_time, row_index)
        parsed_date = date.fromisoformat(meeting_date)
        if parsed_date < cutoff:
            drop_reasons["before_current_calendar_month"] += 1
            logger.info(
                "Sierra Vista row dropped: reason=before_current_calendar_month row_index=%d date=%s cutoff=%s",
                row_index,
                meeting_date,
                cutoff.isoformat(),
            )
            continue

        meeting_time = _extract_time(raw_date_time, row_index)
        links = _document_links(by_header, final_url, row_index)
        event_link = event_cell.find("a", href=True)
        meeting_id = _meeting_id(event_link.get("href", "") if event_link else "", row_index)
        meeting_status = _status(
            meeting_title,
            links["agenda_url"],
            links["minutes_url"],
            links["agenda_packet_url"],
        )
        key = (meeting_date, meeting_title.casefold())
        if key in seen:
            drop_reasons["duplicate_date_title"] += 1
            logger.warning(
                "Sierra Vista row dropped: reason=duplicate_date_title row_index=%d key=%r",
                row_index,
                key,
            )
            continue
        seen.add(key)

        meeting = {
            "meeting_title": meeting_title,
            "meeting_date": meeting_date,
            "meeting_time": meeting_time,
            "meeting_location": "",
            "meeting_status": meeting_status,
            "agenda_url": links["agenda_url"],
            "minutes_url": links["minutes_url"],
            "video_url": links["video_url"],
            "agenda_packet_url": links["agenda_packet_url"],
            "ecomment_url": links["ecomment_url"],
            "meeting_id": meeting_id,
        }
        _validate_meeting(meeting)
        meetings.append(meeting)
        accepted += 1
        logger.info("Sierra Vista meeting emitted: row_index=%d fields=%s", row_index, meeting)

    logger.info(
        "Sierra Vista scrape summary: rows_seen=%d rows_accepted=%d rows_dropped=%d drop_reasons=%s "
        "field_absences=%s",
        rows_seen,
        accepted,
        rows_seen - accepted,
        dict(drop_reasons),
        {"meeting_location": accepted},
    )
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Sierra Vista witnessed zero current-month-forward City Council rows in the official table"
        )
    return meetings


def _fetch_one_bounded(session, url: str) -> tuple[int, str, str]:
    start_host = (urlparse(url).hostname or "").lower()
    if start_host not in _ALLOWED_HOSTS:
        raise ValueError(f"Sierra Vista parser called with disallowed source host: {start_host}")
    with session.get(url, timeout=30, stream=True, allow_redirects=True) as response:
        final_host = (urlparse(response.url).hostname or "").lower()
        if final_host not in _ALLOWED_HOSTS:
            raise ValueError(f"Sierra Vista redirect reached disallowed host: {final_host}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
            body.extend(chunk)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise ValueError(f"Sierra Vista response exceeded {_MAX_RESPONSE_BYTES} bytes")
        return response.status_code, response.url, bytes(body).decode(response.encoding or "utf-8", "replace")


def _is_access_denied(html: str) -> bool:
    sample = _clean(BeautifulSoup(html[:20_000], "html.parser").get_text(" ", strip=True)).lower()
    return "access denied" in sample and ("reference #" in sample or "permission to access" in sample)


def _calendar_table(soup: BeautifulSoup) -> tuple[Tag | None, list[str]]:
    for table in soup.find_all("table"):
        headers = [_clean(cell.get_text(" ", strip=True)).casefold() for cell in table.select("thead th")]
        if not headers:
            first = table.find("tr")
            headers = [_clean(cell.get_text(" ", strip=True)).casefold() for cell in first.find_all(["th", "td"])] if first else []
        if "event" in headers and any(header in {"date", "date/time"} for header in headers):
            return table, headers
    return None, []


def _is_council_title(title: str) -> bool:
    words = re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()
    return "city council" in words and ("meeting" in words or "session" in words)


def _extract_date(text: str, row_index: int) -> str:
    matches = _DATE_RE.findall(text[:500])
    if not matches:
        raise RuntimeError(f"Sierra Vista council row {row_index} has no parseable date: {text!r}")
    month, day, year = matches[0]
    try:
        value = date(int(year), int(month), int(day)).isoformat()
    except ValueError as exc:
        raise RuntimeError(f"Sierra Vista council row {row_index} has invalid date: {text!r}") from exc
    logger.info("Sierra Vista meeting_date emitted: row_index=%d value=%s source=%r", row_index, value, text)
    return value


def _extract_time(text: str, row_index: int) -> str:
    match = _TIME_RE.search(text[:500])
    if not match:
        logger.warning(
            "Sierra Vista meeting_time empty: row_index=%d reason=no_row_level_time_signal source=%r",
            row_index,
            text,
        )
        return ""
    value = f"{int(match.group(1))}:{match.group(2) or '00'} {match.group(3).upper()}M"
    logger.info("Sierra Vista meeting_time emitted: row_index=%d value=%s source=%r", row_index, value, text)
    return value


def _document_links(cells: dict[str, Tag], base_url: str, row_index: int) -> dict[str, str]:
    result = {field: "" for field in ("agenda_url", "minutes_url", "video_url", "agenda_packet_url", "ecomment_url")}
    mapping = {
        "agenda": "agenda_url",
        "minutes": "minutes_url",
        "video": "video_url",
        "other": "agenda_packet_url",
        "agenda packet": "agenda_packet_url",
        "comment": "ecomment_url",
    }
    for header, cell in cells.items():
        field = mapping.get(header)
        if field is None:
            continue
        for anchor in cell.find_all("a", href=True):
            raw = anchor.get("href", "")
            emitted = _safe_url(raw, base_url, field, row_index)
            if emitted:
                result[field] = emitted
                break
    return result


def _safe_url(raw: str, base_url: str, field: str, row_index: int) -> str:
    lowered = raw.strip().lower()
    if not lowered or lowered.startswith(("javascript:", "data:", "file:", "mailto:", "//")):
        logger.warning(
            "Sierra Vista URL dropped: row_index=%d field=%s reason=empty_or_disallowed_scheme rejected=%r",
            row_index,
            field,
            raw,
        )
        return ""
    absolute = urljoin(base_url, raw)
    parsed = urlparse(absolute)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in _ALLOWED_HOSTS:
        logger.warning(
            "Sierra Vista URL dropped: row_index=%d field=%s reason=scheme_or_host_not_allowed rejected=%r",
            row_index,
            field,
            raw,
        )
        return ""
    return absolute


def _meeting_id(href: str, row_index: int) -> str:
    match = _EVENT_ID_RE.search(href)
    if match:
        return match.group(1)
    query = parse_qs(urlparse(href).query)
    for key in ("eventid", "EventID", "id", "ID"):
        if query.get(key):
            return query[key][0]
    logger.warning(
        "Sierra Vista meeting_id empty: row_index=%d reason=no_vendor_event_id source_href=%r",
        row_index,
        href,
    )
    return ""


def _status(title: str, agenda: str, minutes: str, packet: str) -> str:
    if _CANCELLED_RE.search(title):
        return "Cancelled"
    if minutes:
        return "Minutes Available"
    if agenda or packet:
        return "Agenda Available"
    return "Scheduled"


def _validate_meeting(meeting: dict) -> None:
    if tuple(meeting) != _FIELDS:
        raise RuntimeError(f"Sierra Vista canonical schema drifted: {tuple(meeting)}")
    if any(not isinstance(value, str) for value in meeting.values()):
        raise RuntimeError(f"Sierra Vista canonical values must be strings: {meeting}")
    datetime.strptime(meeting["meeting_date"], "%Y-%m-%d")


def _clean(value: str) -> str:
    return " ".join(value.split()) if value else ""
