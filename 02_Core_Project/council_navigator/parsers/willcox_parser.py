"""Willcox City Council meetings from the city's official accordion archive."""

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

DEFAULT_URL = "https://willcox.az.gov/city-council-meetings-agendas-resolutions-1"
ALLOWED_HOSTS = {"willcox.az.gov", "www.willcox.az.gov"}
MAX_RESPONSE_BYTES = 5_000_000
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
DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?[,]?\s+(20\d{2})\b",
    re.IGNORECASE,
)
# Test against: 5:30 a.m. / 5:30 p.m. / 5:30am / 5:30 AM. Do not put \b after optional '.'.
TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*([ap])\.?m\.?(?=\s|$)", re.IGNORECASE)
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)


def scrape_calendar(url: str = DEFAULT_URL) -> list[dict[str, str]]:
    """Return official Willcox City Council rows from this calendar month forward."""
    _validate_input_url(url)
    status, final_url, body = _fetch_bounded(make_session(), url)
    if status in {401, 403}:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Willcox official council page blocked the neutral paced request: "
            "status=%d final_url=%s missing_data_scope=all_current_city_council_meetings",
            status,
            final_url,
        )
        return []
    if status != 200:
        raise RuntimeError(f"Willcox official council page returned HTTP {status}: {final_url}")

    soup = BeautifulSoup(body, "html.parser")
    page_title = _clean(soup.title.get_text(" ", strip=True) if soup.title else "")
    items = soup.select("li.accordion-item")
    if "city council meetings" not in page_title.casefold() or not items:
        raise RuntimeError(
            f"Willcox official Squarespace council-accordion fingerprint drifted: "
            f"title={page_title!r} accordion_items={len(items)}"
        )
    logger.info(
        "Willcox official Squarespace council-accordion fingerprint witnessed: items=%d",
        len(items),
    )

    cutoff = date.today().replace(day=1)
    stats: Counter[str] = Counter()
    meetings: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for position, item in enumerate(items, start=1):
        stats["rows_seen"] += 1
        button = item.find("button")
        if not isinstance(button, Tag):
            stats["missing_button"] += 1
            logger.warning("Willcox row dropped: reason=missing_button position=%d", position)
            continue
        label = _clean(button.get_text(" ", strip=True))
        if "quorum notice" in label.casefold():
            stats["quorum_notice"] += 1
            logger.info("Willcox row dropped: reason=quorum_notice position=%d label=%r", position, label)
            continue
        if "city council" not in label.casefold():
            stats["other_body_or_document"] += 1
            logger.info("Willcox row dropped: reason=not_city_council position=%d label=%r", position, label)
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
            logger.warning("Willcox row dropped: reason=duplicate position=%d key=%r", position, key)
            continue
        seen.add(key)
        documents = _documents(item, final_url, position, stats)
        meeting = {
            "meeting_title": title,
            "meeting_date": meeting_date,
            "meeting_time": _time(label, position, stats),
            "meeting_location": _location(label),
            "meeting_status": _status(title, documents),
            "agenda_url": documents["agenda_url"],
            "minutes_url": documents["minutes_url"],
            "video_url": "",
            "agenda_packet_url": documents["agenda_packet_url"],
            "ecomment_url": "",
            "meeting_id": "",
        }
        _validate_meeting(meeting)
        meetings.append(meeting)
        stats["rows_accepted"] += 1
        logger.info("Willcox meeting emitted: date=%s title=%r documents=%s", meeting_date, title, documents)

    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Willcox witnessed official council accordion has no City Council rows from cutoff=%s: stats=%s",
            cutoff.isoformat(),
            dict(stats),
        )
    logger.warning(
        "Willcox scrape summary: rows_seen=%d accepted=%d drop_reasons=%s "
        "fields_absent_by_construction=%s",
        stats["rows_seen"],
        stats["rows_accepted"],
        {key: value for key, value in stats.items() if key not in {"rows_seen", "rows_accepted"}},
        {
            "video_url": stats["rows_accepted"],
            "ecomment_url": stats["rows_accepted"],
            "meeting_id": stats["rows_accepted"],
        },
    )
    return meetings


def _validate_input_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        raise ValueError(f"Willcox parser called with disallowed URL: {url!r}")
    if not parsed.path.casefold().rstrip("/").endswith("/city-council-meetings-agendas-resolutions-1"):
        raise ValueError(f"Willcox parser called with unexpected path: {url!r}")


def _fetch_bounded(session: Any, url: str) -> tuple[int, str, str]:
    with session.get(url, timeout=30, stream=True, allow_redirects=True) as response:
        final_host = (urlparse(response.url).hostname or "").lower()
        if final_host not in ALLOWED_HOSTS:
            raise ValueError(f"Willcox redirect reached disallowed host: {final_host}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Willcox response exceeded {MAX_RESPONSE_BYTES} bytes")
        return response.status_code, response.url, bytes(body).decode(response.encoding or "utf-8", "replace")


def _parse_date(label: str, position: int) -> str:
    matches = list(DATE_RE.finditer(label))
    if len(matches) != 1:
        logger.warning(
            "Willcox City Council row dropped: reason=unparseable_or_ambiguous_date "
            "position=%d label=%r matches=%d",
            position,
            label,
            len(matches),
        )
        return ""
    match = matches[0]
    raw = f"{match.group(1)} {match.group(2)} {match.group(3)}"
    try:
        return datetime.strptime(raw, "%B %d %Y").date().isoformat()
    except ValueError:
        logger.warning(
            "Willcox City Council row dropped: reason=invalid_date position=%d value=%r",
            position,
            raw,
        )
        return ""


def _title(label: str, position: int) -> str:
    lower = label.casefold()
    if "special city council meeting" in lower:
        title = "Willcox City Council Special Meeting"
    elif "regular city council meeting" in lower:
        title = "Willcox City Council Regular Meeting"
    elif "city council work session" in lower:
        title = "Willcox City Council Work Session"
    else:
        raise RuntimeError(
            f"Willcox City Council meeting vocabulary drifted: position={position} label={label!r}"
        )
    if CANCELLED_RE.search(label):
        title = f"{title} - Cancelled"
    return title


def _time(label: str, position: int, stats: Counter[str]) -> str:
    matches = list(TIME_RE.finditer(label))
    if not matches:
        stats["meeting_time:not_exposed"] += 1
        logger.warning(
            "Willcox meeting time absent: position=%d reason=no_per_row_time_signal label=%r",
            position,
            label,
        )
        return ""
    if len(matches) != 1:
        raise RuntimeError(f"Willcox row has ambiguous time: position={position} label={label!r}")
    hour = int(matches[0].group(1))
    minute = int(matches[0].group(2))
    if not 1 <= hour <= 12 or not 0 <= minute <= 59:
        raise RuntimeError(f"Willcox row has invalid time: position={position} label={label!r}")
    return f"{hour}:{minute:02d} {matches[0].group(3).upper()}M"


def _location(label: str) -> str:
    return "City Hall" if re.search(r"\bat City Hall\b", label, re.IGNORECASE) else ""


def _documents(item: Tag, base_url: str, position: int, stats: Counter[str]) -> dict[str, str]:
    result = {"agenda_url": "", "minutes_url": "", "agenda_packet_url": ""}
    dropdown = item.select_one("div.accordion-item__dropdown")
    if not isinstance(dropdown, Tag):
        stats["missing_dropdown"] += 1
        logger.warning("Willcox row has no document dropdown: position=%d", position)
        return result
    for anchor in dropdown.find_all("a", href=True):
        label = _clean(anchor.get_text(" ", strip=True))
        lowered = label.casefold()
        if re.fullmatch(r"(?:council |complete )?packet", lowered):
            field = "agenda_packet_url"
        elif re.fullmatch(r"(?:meeting )?minutes", lowered):
            field = "minutes_url"
        elif re.fullmatch(r"(?:amended )?agenda", lowered):
            field = "agenda_url"
        else:
            stats[f"unsupported_document:{lowered or 'empty'}"] += 1
            logger.warning(
                "Willcox document dropped: row=%d reason=unsupported_document label=%r href=%r",
                position,
                label,
                _clean(anchor.get("href")),
            )
            continue
        emitted = _safe_url(_clean(anchor.get("href")), base_url, position, field)
        if not emitted:
            continue
        if result[field]:
            logger.warning(
                "Willcox document dropped: row=%d reason=duplicate field=%s kept=%s dropped=%s",
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
            "Willcox URL dropped: row=%d field=%s reason=empty_or_disallowed_scheme value=%r",
            position,
            field,
            raw,
        )
        return ""
    absolute = urljoin(base_url, raw)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        logger.warning(
            "Willcox URL dropped: row=%d field=%s reason=disallowed_host value=%r",
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
    if documents["agenda_url"] or documents["agenda_packet_url"]:
        return "Agenda Available"
    return "Scheduled"


def _validate_meeting(meeting: dict[str, str]) -> None:
    if tuple(meeting) != FIELDS:
        raise RuntimeError(f"Willcox canonical schema drifted: {tuple(meeting)}")
    if any(not isinstance(value, str) for value in meeting.values()):
        raise RuntimeError(f"Willcox canonical values must be strings: {meeting!r}")


def _clean(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = unescape(str(value))
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return " ".join(text.split())
