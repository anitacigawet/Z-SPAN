"""Town of Miami Town Council meetings from the official public-meetings table."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from html import unescape
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from polite_http import make_session

__all__ = ["scrape_calendar"]

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://miamiaz.gov/departments/town-clerk/public-meetings/"
ALLOWED_HOSTS = {"miamiaz.gov", "www.miamiaz.gov"}
MAX_RESPONSE_BYTES = 4_000_000
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
DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b")
MONTH_DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?[,]?\s+(20\d{2})\b",
    re.IGNORECASE,
)
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)


def scrape_calendar(url: str = DEFAULT_URL) -> list[dict[str, str]]:
    """Return official Miami Town Council rows from this calendar month forward."""
    _validate_input_url(url)
    status, final_url, body = _fetch_bounded(make_session(), url)
    if status in {401, 403}:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Miami official public-meetings page blocked the neutral paced request: "
            "status=%d final_url=%s missing_data_scope=all_current_town_council_meetings",
            status,
            final_url,
        )
        return []
    if status != 200:
        raise RuntimeError(f"Miami official public-meetings page returned HTTP {status}: {final_url}")

    soup = BeautifulSoup(body, "html.parser")
    table, headers = _find_table(soup)
    logger.info("Miami official meeting-table fingerprint witnessed: headers=%s", headers)
    indexes = {name: headers.index(name) for name in headers}
    cutoff = date.today().replace(day=1)
    stats: Counter[str] = Counter()
    meetings: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for position, row in enumerate(table.find_all("tr"), start=1):
        cells = row.find_all(["td", "th"], recursive=False)
        if not cells or all(cell.name == "th" for cell in cells):
            continue
        stats["rows_seen"] += 1
        if len(cells) != len(headers):
            raise RuntimeError(
                f"Miami meeting-table row width drifted: position={position} "
                f"expected={len(headers)} actual={len(cells)}"
            )
        label = _clean(cells[indexes["Meeting"]].get_text(" ", strip=True))
        if "town council" not in label.casefold():
            stats["other_body_or_notice"] += 1
            logger.info("Miami row dropped: reason=not_town_council position=%d label=%r", position, label)
            continue
        meeting_date = _parse_date(label, position)
        if not meeting_date:
            stats["unparseable_historical_date"] += 1
            continue
        if date.fromisoformat(meeting_date) < cutoff:
            stats["before_current_month"] += 1
            continue
        title = _title(label, position)
        key = (meeting_date, title.casefold())
        if key in seen:
            stats["duplicate"] += 1
            logger.warning("Miami row dropped: reason=duplicate position=%d key=%r", position, key)
            continue
        seen.add(key)
        documents = _documents(cells, indexes, final_url, position, stats)
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
        logger.info("Miami meeting emitted: date=%s title=%r documents=%s", meeting_date, title, documents)

    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Miami witnessed official meeting table has no Town Council rows from cutoff=%s: stats=%s",
            cutoff.isoformat(),
            dict(stats),
        )
    logger.warning(
        "Miami scrape summary: rows_seen=%d accepted=%d drop_reasons=%s "
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
        raise ValueError(f"Miami parser called with disallowed URL: {url!r}")
    if not parsed.path.casefold().rstrip("/").endswith("/departments/town-clerk/public-meetings"):
        raise ValueError(f"Miami parser called with unexpected path: {url!r}")


def _fetch_bounded(session: Any, url: str) -> tuple[int, str, str]:
    with session.get(url, timeout=30, stream=True, allow_redirects=True) as response:
        final_host = (urlparse(response.url).hostname or "").lower()
        if final_host not in ALLOWED_HOSTS:
            raise ValueError(f"Miami redirect reached disallowed host: {final_host}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Miami response exceeded {MAX_RESPONSE_BYTES} bytes")
        return response.status_code, response.url, bytes(body).decode(response.encoding or "utf-8", "replace")


def _find_table(soup: BeautifulSoup) -> tuple[Tag, list[str]]:
    required = {"Meeting", "Notice & Agenda", "Draft Minutes", "Approved Minutes"}
    for table in soup.find_all("table"):
        headers = [_clean(cell.get_text(" ", strip=True)) for cell in table.find_all("th")]
        if set(headers) == required and len(headers) == len(required):
            return table, headers
    raise RuntimeError("Miami official meeting-table fingerprint drifted")


def _parse_date(label: str, position: int) -> str:
    slash_matches = DATE_RE.findall(label)
    month_matches = list(MONTH_DATE_RE.finditer(label))
    if len(slash_matches) + len(month_matches) != 1:
        logger.warning(
            "Miami Town Council row dropped: reason=unparseable_or_ambiguous_date "
            "position=%d label=%r slash_matches=%r month_matches=%d",
            position,
            label,
            slash_matches,
            len(month_matches),
        )
        return ""
    if slash_matches:
        raw = slash_matches[0]
        for fmt in ("%m/%d/%y", "%m/%d/%Y"):
            try:
                return datetime.strptime(raw, fmt).date().isoformat()
            except ValueError:
                continue
        logger.warning(
            "Miami Town Council row dropped: reason=invalid_date position=%d value=%r",
            position,
            raw,
        )
        return ""
    match = month_matches[0]
    raw = f"{match.group(1)} {match.group(2)} {match.group(3)}"
    try:
        return datetime.strptime(raw, "%B %d %Y").date().isoformat()
    except ValueError:
        logger.warning(
            "Miami Town Council row dropped: reason=invalid_date position=%d value=%r",
            position,
            raw,
        )
        return ""


def _title(label: str, position: int) -> str:
    without_date = MONTH_DATE_RE.sub("", DATE_RE.sub("", label)).strip(" -–—")
    normalized = " ".join(without_date.split())
    allowed = {
        "town council regular meeting": "Miami Town Council Regular Meeting",
        "town council special meeting": "Miami Town Council Special Meeting",
        "town council work session meeting": "Miami Town Council Work Session",
        "town council work session": "Miami Town Council Work Session",
    }
    title = allowed.get(normalized.casefold())
    if title is None:
        raise RuntimeError(
            f"Miami Town Council meeting vocabulary drifted: position={position} label={label!r}"
        )
    if CANCELLED_RE.search(label):
        title = f"{title} - Cancelled"
    return title


def _documents(
    cells: list[Tag],
    indexes: dict[str, int],
    base_url: str,
    position: int,
    stats: Counter[str],
) -> dict[str, str]:
    result = {"agenda_url": "", "minutes_url": ""}
    column_fields = {
        "Notice & Agenda": "agenda_url",
        "Draft Minutes": "minutes_url",
        "Approved Minutes": "minutes_url",
    }
    for column, field in column_fields.items():
        for anchor in cells[indexes[column]].find_all("a", href=True):
            raw = _clean(anchor.get("href"))
            emitted = _safe_url(raw, base_url, position, field)
            if not emitted:
                continue
            if result[field]:
                logger.warning(
                    "Miami document dropped: row=%d reason=duplicate field=%s kept=%s dropped=%s",
                    position,
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


def _safe_url(raw: str, base_url: str, position: int, field: str) -> str:
    if not raw or raw.casefold().startswith(("//", "javascript:", "data:", "file:", "mailto:", "ftp:")):
        logger.warning(
            "Miami URL dropped: row=%d field=%s reason=empty_or_disallowed_scheme value=%r",
            position,
            field,
            raw,
        )
        return ""
    absolute = urljoin(base_url, raw)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        logger.warning(
            "Miami URL dropped: row=%d field=%s reason=disallowed_host value=%r",
            position,
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
        raise RuntimeError(f"Miami canonical schema drifted: {tuple(meeting)}")
    if any(not isinstance(value, str) for value in meeting.values()):
        raise RuntimeError(f"Miami canonical values must be strings: {meeting!r}")


def _clean(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = unescape(str(value))
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return " ".join(text.split())
