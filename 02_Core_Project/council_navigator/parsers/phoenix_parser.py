"""Phoenix City Council meetings from the official current Legistar calendar."""

from __future__ import annotations

from collections import Counter
from datetime import date
from html import unescape
import logging
import re
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from polite_http import make_session

__all__ = ["scrape_calendar"]

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://phoenix.legistar.com/Calendar.aspx"
ALLOWED_HOST = "phoenix.legistar.com"
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
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
COUNCIL_RE = re.compile(
    r"\bcity council\b.*\b(?:formal|policy|special|work study)\b.*\bmeeting\b",
    re.IGNORECASE,
)
DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
TIME_RE = re.compile(r"^(1[0-2]|0?[1-9]):([0-5]\d)\s+([AP])M$", re.IGNORECASE)


def scrape_calendar(url: str = DEFAULT_URL) -> list[dict[str, str]]:
    """Return Phoenix council meetings visible from this calendar month forward."""
    _validate_input_url(url)
    status, final_url, body = _fetch_bounded(make_session(), url)
    if status in {401, 403}:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Phoenix official Legistar calendar blocked the neutral paced request: "
            "status=%d final_url=%s missing_data_scope=all_current_meetings",
            status,
            final_url,
        )
        return []
    if status != 200:
        raise RuntimeError(f"Phoenix Legistar calendar returned HTTP {status}: {final_url}")

    soup = BeautifulSoup(body, "html.parser")
    table = soup.select_one("table.rgMasterTable")
    if not isinstance(table, Tag):
        raise RuntimeError("Phoenix Legistar fingerprint drifted: table.rgMasterTable missing")

    headers = [_clean(th.get_text(" ", strip=True)) for th in table.select("thead th")]
    required = {"Name", "Date", "Time", "Details", "Agenda", "Agenda Packet", "Minutes", "Video"}
    if not required.issubset(set(headers)):
        raise RuntimeError(f"Phoenix Legistar headers drifted: {headers!r}")
    logger.info("Phoenix Legistar fingerprint witnessed: headers=%s", headers)

    rows = table.select("tbody > tr")
    if not rows:
        page_text = _clean(table.get_text(" ", strip=True)).casefold()
        if "no records to display" not in page_text and not table.select_one(".rgNoRecords"):
            raise RuntimeError("Phoenix Legistar table had no rows and no witnessed empty-state marker")
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning("Phoenix official current-month Legistar table explicitly reports no records")
        return []

    cutoff = date.today().replace(day=1)
    stats: Counter[str] = Counter()
    meetings: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for position, row in enumerate(rows, start=1):
        stats["rows_seen"] += 1
        cells = row.find_all("td", recursive=False)
        if len(cells) != len(headers):
            raise RuntimeError(
                f"Phoenix Legistar row width drifted: position={position} cells={len(cells)} headers={len(headers)}"
            )
        values = {header: cells[index] for index, header in enumerate(headers) if header}
        title = _clean(values["Name"].get_text(" ", strip=True))
        if not COUNCIL_RE.search(title):
            stats["not_city_council"] += 1
            logger.info("Phoenix row dropped: reason=not_city_council position=%d title=%r", position, title)
            continue

        meeting_date = _parse_date(_clean(values["Date"].get_text(" ", strip=True)), position)
        if date.fromisoformat(meeting_date) < cutoff:
            stats["before_current_month"] += 1
            logger.warning(
                "Phoenix row dropped: reason=before_current_month position=%d date=%s cutoff=%s",
                position,
                meeting_date,
                cutoff.isoformat(),
            )
            continue
        meeting_time = _parse_time(_clean(values["Time"].get_text(" ", strip=True)), position)
        links = {
            "agenda_url": _cell_url(values["Agenda"], final_url, position, "agenda_url"),
            "agenda_packet_url": _cell_url(
                values["Agenda Packet"], final_url, position, "agenda_packet_url"
            ),
            "minutes_url": _cell_url(values["Minutes"], final_url, position, "minutes_url"),
            "video_url": _cell_url(values["Video"], final_url, position, "video_url"),
        }
        ecomment = _cell_url(values.get("eComment"), final_url, position, "ecomment_url")
        meeting_id = _meeting_id(row, position)
        if meeting_id in seen_ids:
            stats["duplicate_meeting_id"] += 1
            logger.warning("Phoenix row dropped: reason=duplicate_meeting_id id=%s", meeting_id)
            continue
        seen_ids.add(meeting_id)
        meeting = {
            "meeting_title": title,
            "meeting_date": meeting_date,
            "meeting_time": meeting_time,
            "meeting_location": "",
            "meeting_status": _status(title, links),
            "agenda_url": links["agenda_url"],
            "minutes_url": links["minutes_url"],
            "video_url": links["video_url"],
            "agenda_packet_url": links["agenda_packet_url"],
            "ecomment_url": ecomment,
            "meeting_id": meeting_id,
        }
        _validate_meeting(meeting)
        meetings.append(meeting)
        stats["rows_accepted"] += 1
        logger.info("Phoenix meeting emitted: id=%s date=%s title=%r", meeting_id, meeting_date, title)

    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Phoenix official current-month table contained no qualifying City Council rows: stats=%s",
            dict(stats),
        )
    logger.warning(
        "Phoenix scrape summary: rows_seen=%d rows_accepted=%d drop_reasons=%s "
        "meeting_location_absent_by_construction=%d",
        stats["rows_seen"],
        stats["rows_accepted"],
        {key: value for key, value in stats.items() if key not in {"rows_seen", "rows_accepted"}},
        stats["rows_accepted"],
    )
    return meetings


def _validate_input_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != ALLOWED_HOST:
        raise ValueError(f"Phoenix parser called with disallowed URL: {url!r}")
    if parsed.path.casefold().rstrip("/") != "/calendar.aspx":
        raise ValueError(f"Phoenix parser called with unexpected path: {url!r}")


def _fetch_bounded(session: Any, url: str) -> tuple[int, str, str]:
    with session.get(url, timeout=30, stream=True, allow_redirects=True) as response:
        final_host = (urlparse(response.url).hostname or "").lower()
        if final_host != ALLOWED_HOST:
            raise ValueError(f"Phoenix redirect reached disallowed host: {final_host}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Phoenix response exceeded {MAX_RESPONSE_BYTES} bytes")
        return response.status_code, response.url, bytes(body).decode(response.encoding or "utf-8", "replace")


def _parse_date(raw: str, position: int) -> str:
    match = DATE_RE.fullmatch(raw)
    if not match:
        raise RuntimeError(f"Phoenix row has unparseable date: position={position} value={raw!r}")
    try:
        return date(int(match.group(3)), int(match.group(1)), int(match.group(2))).isoformat()
    except ValueError as exc:
        raise RuntimeError(f"Phoenix row has invalid date: position={position} value={raw!r}") from exc


def _parse_time(raw: str, position: int) -> str:
    if not raw:
        logger.warning("Phoenix meeting_time honest-empty: position=%d reason=source_cell_empty", position)
        return ""
    match = TIME_RE.fullmatch(raw)
    if not match:
        raise RuntimeError(f"Phoenix row has unparseable time: position={position} value={raw!r}")
    return f"{int(match.group(1))}:{match.group(2)} {match.group(3).upper()}M"


def _cell_url(cell: Tag | None, base_url: str, position: int, field: str) -> str:
    if not isinstance(cell, Tag):
        logger.info("Phoenix %s honest-empty: position=%d reason=column_not_present", field, position)
        return ""
    anchor = cell.find("a")
    href = _clean(anchor.get("href")) if isinstance(anchor, Tag) else ""
    label = _clean(cell.get_text(" ", strip=True))
    if not href:
        if label and label.casefold() not in {"not available", ""}:
            logger.warning(
                "Phoenix URL dropped: position=%d field=%s reason=label_without_href label=%r",
                position,
                field,
                label,
            )
        return ""
    return _safe_url(href, base_url, position, field)


def _safe_url(raw: str, base_url: str, position: int, field: str) -> str:
    if raw.casefold().startswith(("//", "javascript:", "data:", "file:", "mailto:", "ftp:")):
        logger.warning(
            "Phoenix URL dropped: position=%d field=%s reason=disallowed_scheme value=%r",
            position,
            field,
            raw,
        )
        return ""
    absolute = urljoin(base_url, raw)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower() != ALLOWED_HOST:
        logger.warning(
            "Phoenix URL dropped: position=%d field=%s reason=disallowed_host value=%r",
            position,
            field,
            raw,
        )
        return ""
    return absolute


def _meeting_id(row: Tag, position: int) -> str:
    for anchor in row.find_all("a", href=True):
        parsed = urlparse(str(anchor.get("href")))
        query = parse_qs(parsed.query)
        candidate = (query.get("ID") or query.get("id") or [""])[0]
        if str(candidate).isdigit():
            return str(candidate)
    for anchor in row.find_all("a"):
        candidate = _clean(anchor.get("data-event-id"))
        if candidate:
            logger.warning(
                "Phoenix numeric vendor ID absent; using witnessed data-event-id: position=%d id=%s",
                position,
                candidate,
            )
            return candidate
    raise RuntimeError(f"Phoenix row lacks a witnessed vendor ID: position={position}")


def _status(title: str, links: dict[str, str]) -> str:
    if CANCELLED_RE.search(title):
        return "Cancelled"
    if links["minutes_url"]:
        return "Minutes Available"
    if links["agenda_url"] or links["agenda_packet_url"]:
        return "Agenda Available"
    return "Scheduled"


def _validate_meeting(meeting: dict[str, str]) -> None:
    if tuple(meeting) != FIELDS:
        raise RuntimeError(f"Phoenix canonical schema drifted: {tuple(meeting)}")
    if any(not isinstance(value, str) for value in meeting.values()):
        raise RuntimeError(f"Phoenix canonical values must be strings: {meeting!r}")


def _clean(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = unescape(str(value))
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return " ".join(text.split())
