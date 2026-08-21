"""Taylor Town Council agendas and minutes from the official annual table."""

from __future__ import annotations

from collections import Counter
from datetime import date
from html import unescape
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from polite_http import make_session

__all__ = ["scrape_calendar"]

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://www.tayloraz.gov/town-hall/town-council-meetings/agenda-minutes/"
ALLOWED_HOSTS = {"tayloraz.gov", "www.tayloraz.gov"}
MAX_RESPONSE_BYTES = 8_000_000
CHUNK_SIZE = 65_536
FIELDS = (
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
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
DATE_RE = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th|h)?$", re.IGNORECASE)
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
COUNCIL_TYPES = {"regular meeting", "special meeting", "work session"}
BROWSER_CHALLENGE_TITLES = {"just a moment...", "one moment, please..."}
BROWSER_CHALLENGE_MARKERS = (
    "please wait while your request is being verified",
    "performing security verification",
)


def scrape_calendar(url: str = DEFAULT_URL) -> list[dict[str, str]]:
    """Return Taylor Town Council meetings from this calendar month forward."""
    _validate_input_url(url)
    status, final_url, body = _fetch_bounded(make_session(), url)
    if status in {401, 403, 429}:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Taylor official agenda page blocked the neutral paced request: "
            "status=%d final_url=%s missing_data_scope=all_current_meetings",
            status,
            final_url,
        )
        return []
    if status != 200:
        raise RuntimeError(f"Taylor official agenda page returned HTTP {status}: {final_url}")

    soup = BeautifulSoup(body, "html.parser")
    title = _clean(soup.title.get_text(" ", strip=True) if soup.title else "")
    page_text = _clean(soup.get_text(" ", strip=True))[:20_000]
    if _is_browser_challenge(title, page_text):
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Taylor official agenda page returned a browser-verification interstitial: "
            "title=%r final_url=%s missing_data_scope=all_current_meetings",
            title,
            final_url,
        )
        return []
    current_year = date.today().year
    heading_text = f"{current_year} Town Council Meetings"
    heading = soup.find(
        lambda tag: tag.name in {"h2", "h3", "h4"}
        and _clean(tag.get_text(" ", strip=True)).casefold() == heading_text.casefold()
    )
    if not isinstance(heading, Tag):
        raise RuntimeError(f"Taylor official page lacks expected current-year heading: {heading_text!r}")
    table = heading.find_next("table")
    if not isinstance(table, Tag):
        raise RuntimeError("Taylor current-year Town Council heading is not followed by a table")
    next_heading = heading.find_next(lambda tag: tag.name in {"h2", "h3", "h4"})
    if isinstance(next_heading, Tag) and table.sourceline and next_heading.sourceline:
        if table.sourceline > next_heading.sourceline:
            raise RuntimeError("Taylor current-year table boundary drifted")
    logger.info("Taylor official Town Council table fingerprint witnessed: heading=%r", heading_text)

    cutoff = date.today().replace(day=1)
    stats: Counter[str] = Counter()
    meetings: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for position, row in enumerate(table.find_all("tr"), start=1):
        stats["rows_seen"] += 1
        cells = row.find_all(["td", "th"], recursive=False)
        values = [_clean(cell.get_text(" ", strip=True)) for cell in cells]
        if not any(values):
            stats["blank_row"] += 1
            logger.info("Taylor row dropped: reason=blank_row position=%d", position)
            continue
        if len(cells) < 2:
            raise RuntimeError(f"Taylor current-year row width drifted: position={position} cells={values!r}")
        meeting_date = _parse_date(values[0], current_year, position)
        if date.fromisoformat(meeting_date) < cutoff:
            stats["before_current_month"] += 1
            continue
        meeting_type = values[1].casefold()
        if meeting_type not in COUNCIL_TYPES:
            raise RuntimeError(
                f"Taylor Town Council meeting-type vocabulary drifted: position={position} value={values[1]!r}"
            )
        key = (meeting_date, meeting_type)
        if key in seen:
            stats["duplicate_meeting"] += 1
            logger.warning("Taylor row dropped: reason=duplicate_meeting position=%d key=%r", position, key)
            continue
        seen.add(key)
        documents = _documents(cells, final_url, position, stats)
        title = f"Taylor Town Council {values[1]}"
        if any(CANCELLED_RE.search(value) for value in values):
            if not CANCELLED_RE.search(title):
                title = f"{title} - Cancelled"
        meeting = {
            "meeting_title": title,
            "meeting_date": meeting_date,
            "meeting_time": "",
            "meeting_location": "",
            "meeting_status": _status(title, documents),
            "agenda_url": documents["agenda_url"],
            "minutes_url": documents["minutes_url"],
            "video_url": "",
            "agenda_packet_url": "",
            "ecomment_url": "",
            "meeting_id": "",
        }
        _validate_meeting(meeting)
        meetings.append(meeting)
        stats["rows_accepted"] += 1
        logger.info("Taylor meeting emitted: date=%s title=%r documents=%s", meeting_date, title, documents)

    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Taylor official current-year Town Council table has no rows from cutoff=%s: stats=%s",
            cutoff.isoformat(),
            dict(stats),
        )
    logger.warning(
        "Taylor scrape summary: rows_seen=%d accepted=%d drop_reasons=%s "
        "fields_absent_by_construction=%s",
        stats["rows_seen"],
        stats["rows_accepted"],
        {key: value for key, value in stats.items() if key not in {"rows_seen", "rows_accepted"}},
        {
            "meeting_time": stats["rows_accepted"],
            "meeting_location": stats["rows_accepted"],
            "video_url": stats["rows_accepted"],
            "agenda_packet_url": stats["rows_accepted"],
            "ecomment_url": stats["rows_accepted"],
            "meeting_id": stats["rows_accepted"],
        },
    )
    return meetings


def _validate_input_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        raise ValueError(f"Taylor parser called with disallowed URL: {url!r}")
    if not parsed.path.casefold().rstrip("/").endswith("/town-council-meetings/agenda-minutes"):
        raise ValueError(f"Taylor parser called with unexpected path: {url!r}")


def _fetch_bounded(session: Any, url: str) -> tuple[int, str, str]:
    with session.get(url, timeout=30, stream=True, allow_redirects=True) as response:
        final_host = (urlparse(response.url).hostname or "").lower()
        if final_host not in ALLOWED_HOSTS:
            raise ValueError(f"Taylor redirect reached disallowed host: {final_host}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Taylor response exceeded {MAX_RESPONSE_BYTES} bytes")
        return response.status_code, response.url, bytes(body).decode(response.encoding or "utf-8", "replace")


def _parse_date(raw: str, year: int, position: int) -> str:
    match = DATE_RE.fullmatch(raw)
    if not match:
        raise RuntimeError(f"Taylor row has unparseable date: position={position} value={raw!r}")
    month = MONTHS.get(match.group(1).casefold().rstrip("."))
    if month is None:
        raise RuntimeError(f"Taylor row has unknown month: position={position} value={raw!r}")
    try:
        return date(year, month, int(match.group(2))).isoformat()
    except ValueError as exc:
        raise RuntimeError(f"Taylor row has invalid date: position={position} value={raw!r}") from exc


def _documents(
    cells: list[Tag],
    base_url: str,
    row_position: int,
    stats: Counter[str],
) -> dict[str, str]:
    result = {"agenda_url": "", "minutes_url": ""}
    for cell_position, cell in enumerate(cells, start=1):
        for anchor in cell.find_all("a"):
            label = _clean(anchor.get_text(" ", strip=True)).casefold()
            raw_href = _clean(anchor.get("href"))
            if re.search(r"\bminutes?\b", label):
                field = "minutes_url"
            elif re.search(r"\bagendas?\b", label):
                field = "agenda_url"
            else:
                stats[f"unsupported_document:{label or 'empty'}"] += 1
                logger.warning(
                    "Taylor document dropped: row=%d cell=%d reason=unsupported_document label=%r href=%r",
                    row_position,
                    cell_position,
                    label,
                    raw_href,
                )
                continue
            emitted = _safe_url(raw_href, base_url, row_position, field)
            if not emitted:
                continue
            if result[field]:
                logger.warning(
                    "Taylor duplicate document dropped: row=%d field=%s kept=%s dropped=%s",
                    row_position,
                    field,
                    result[field],
                    emitted,
                )
                continue
            result[field] = emitted
    for field, emitted in result.items():
        if not emitted:
            stats[f"{field}:not_exposed"] += 1
    return result


def _safe_url(raw: str, base_url: str, row_position: int, field: str) -> str:
    if not raw or raw.casefold().startswith(("//", "javascript:", "data:", "file:", "mailto:", "ftp:")):
        logger.warning(
            "Taylor URL dropped: row=%d field=%s reason=empty_or_disallowed_scheme value=%r",
            row_position,
            field,
            raw,
        )
        return ""
    absolute = urljoin(base_url, raw)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        logger.warning(
            "Taylor URL dropped: row=%d field=%s reason=disallowed_host value=%r",
            row_position,
            field,
            raw,
        )
        return ""
    return absolute


def _status(title: str, documents: dict[str, str]) -> str:
    if CANCELLED_RE.search(title):
        return "Cancelled"
    if documents["minutes_url"]:
        return "Minutes Available"
    if documents["agenda_url"]:
        return "Agenda Available"
    return "Scheduled"


def _validate_meeting(meeting: dict[str, str]) -> None:
    if tuple(meeting) != FIELDS:
        raise RuntimeError(f"Taylor canonical schema drifted: {tuple(meeting)}")
    if any(not isinstance(value, str) for value in meeting.values()):
        raise RuntimeError(f"Taylor canonical values must be strings: {meeting!r}")


def _clean(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = unescape(str(value))
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return " ".join(text.split())


def _is_browser_challenge(title: str, page_text: str) -> bool:
    normalized_title = title.casefold()
    normalized_text = page_text.casefold()
    return normalized_title in BROWSER_CHALLENGE_TITLES and any(
        marker in normalized_text for marker in BROWSER_CHALLENGE_MARKERS
    )
