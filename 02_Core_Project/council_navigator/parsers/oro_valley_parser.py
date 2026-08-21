"""Oro Valley Town Council upcoming-meeting parser (official Swagit view)."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import logging
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from polite_http import make_session

__all__ = ["scrape_calendar"]

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://orovalleyaz.new.swagit.com/views/52"
ALLOWED_HOSTS = {"orovalleyaz.new.swagit.com"}
MAX_RESPONSE_BYTES = 8_000_000
CHUNK_SIZE = 65_536
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
EVENT_ID_RE = re.compile(r"^/events/(\d+)(?:/|$)", re.IGNORECASE)
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


def scrape_calendar(url: str = DEFAULT_URL) -> list[dict]:
    """Return Oro Valley Town Council rows from the current month forward."""
    logger.warning(
        "Oro Valley Swagit upcoming table does not expose meeting_location; emitted rows use an honest empty value"
    )
    session = make_session()
    try:
        status, final_url, html = _fetch_bounded(session, url)
    except requests.exceptions.SSLError:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning("Oro Valley official Swagit source failed verified TLS")
        return []
    if status in {401, 403, 429}:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Oro Valley official Swagit source blocked the neutral paced request: "
            "failure_shape=honest-empty missing_data_scope=all_current_and_future_meetings "
            "status=%d final_url=%s",
            status,
            final_url,
        )
        return []
    if status != 200:
        raise RuntimeError(f"Oro Valley Swagit source returned HTTP {status}: {final_url}")

    soup = BeautifulSoup(html, "html.parser")
    page_title = _clean(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "oro valley" not in page_title.casefold() or "video archive" not in page_title.casefold():
        raise RuntimeError(f"Oro Valley Swagit fingerprint drifted: title={page_title!r}")
    table, headers = _upcoming_table(soup)
    if table is None:
        raise RuntimeError("Oro Valley Swagit fingerprint drifted: Upcoming Events Title/Date/Links table missing")
    logger.info("Oro Valley Swagit fingerprint witnessed: title=%r headers=%s", page_title, headers)

    cutoff = date.today().replace(day=1)
    rows_seen = 0
    accepted = 0
    drops: Counter[str] = Counter()
    meetings: list[dict] = []
    seen_ids: set[str] = set()
    for row_index, row in enumerate(table.select("tbody tr"), start=1):
        rows_seen += 1
        cells = row.find_all("td", recursive=False)
        if len(cells) != len(headers):
            raise RuntimeError(
                f"Oro Valley Swagit row/header drift: row={row_index} cells={len(cells)} headers={len(headers)}"
            )
        by_header = {headers[index]: cells[index] for index in range(len(headers))}
        title_cell = by_header["title"]
        meeting_title = _clean(title_cell.get_text(" ", strip=True))
        if not _is_town_council_title(meeting_title):
            drops["not_town_council"] += 1
            continue

        meeting_date, meeting_time = _parse_datetime(
            _clean(by_header["date"].get_text(" ", strip=True)), row_index
        )
        if date.fromisoformat(meeting_date) < cutoff:
            drops["before_current_calendar_month"] += 1
            logger.info(
                "Oro Valley row dropped: reason=before_current_calendar_month row=%d date=%s cutoff=%s",
                row_index,
                meeting_date,
                cutoff.isoformat(),
            )
            continue

        title_anchor = title_cell.find("a", href=True)
        meeting_id = _event_id(title_anchor.get("href", "") if title_anchor else "", row_index)
        if meeting_id in seen_ids:
            drops["duplicate_event_id"] += 1
            logger.warning("Oro Valley row dropped: reason=duplicate_event_id row=%d id=%s", row_index, meeting_id)
            continue
        seen_ids.add(meeting_id)

        urls = _row_urls(by_header["links"], final_url, row_index)
        status_value = _status(meeting_title, urls)
        meeting = {
            "meeting_title": meeting_title,
            "meeting_date": meeting_date,
            "meeting_time": meeting_time,
            "meeting_location": "",
            "meeting_status": status_value,
            "agenda_url": urls["agenda_url"],
            "minutes_url": urls["minutes_url"],
            "video_url": urls["video_url"],
            "agenda_packet_url": urls["agenda_packet_url"],
            "ecomment_url": "",
            "meeting_id": meeting_id,
        }
        _validate(meeting)
        meetings.append(meeting)
        accepted += 1
        logger.info("Oro Valley meeting emitted: row=%d fields=%s", row_index, meeting)

    logger.info(
        "Oro Valley scrape summary: rows_seen=%d rows_accepted=%d rows_dropped=%d drop_reasons=%s "
        "field_absences=%s",
        rows_seen,
        accepted,
        rows_seen - accepted,
        dict(drops),
        {"meeting_location": accepted, "ecomment_url": accepted},
    )
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Oro Valley witnessed zero current-month-forward Town Council rows in the official table"
        )
    return meetings


def _fetch_bounded(session, url: str) -> tuple[int, str, str]:
    if (urlparse(url).hostname or "").lower() not in ALLOWED_HOSTS:
        raise ValueError(f"Oro Valley parser called with disallowed host: {url!r}")
    with session.get(url, timeout=30, stream=True, allow_redirects=True) as response:
        final_host = (urlparse(response.url).hostname or "").lower()
        if final_host not in ALLOWED_HOSTS:
            raise ValueError(f"Oro Valley redirect reached disallowed host: {final_host}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Oro Valley response exceeded {MAX_RESPONSE_BYTES} bytes")
        return response.status_code, response.url, bytes(body).decode(response.encoding or "utf-8", "replace")


def _is_access_block(html: str) -> bool:
    text = _clean(BeautifulSoup(html[:20_000], "html.parser").get_text(" ", strip=True)).casefold()
    return "access denied" in text or "permission to access" in text or "captcha" in text


def _upcoming_table(soup: BeautifulSoup) -> tuple[Tag | None, list[str]]:
    for table in soup.find_all("table"):
        headers = [_clean(th.get_text(" ", strip=True)).casefold() for th in table.select("thead th")]
        if headers == ["title", "date", "links"]:
            return table, headers
    return None, []


def _is_town_council_title(title: str) -> bool:
    words = re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()
    return "town council" in words and ("session" in words or "meeting" in words)


def _parse_datetime(value: str, row_index: int) -> tuple[str, str]:
    try:
        parsed = datetime.strptime(value, "%b %d, %Y %I:%M %p")
    except ValueError as exc:
        raise RuntimeError(f"Oro Valley row {row_index} has unparsable date/time: {value!r}") from exc
    return parsed.date().isoformat(), parsed.strftime("%I:%M %p").lstrip("0")


def _event_id(href: str, row_index: int) -> str:
    match = EVENT_ID_RE.match(href)
    if not match:
        raise RuntimeError(f"Oro Valley row {row_index} event URL drifted: {href!r}")
    return match.group(1)


def _row_urls(cell: Tag, base_url: str, row_index: int) -> dict[str, str]:
    urls = {field: "" for field in ("agenda_url", "minutes_url", "video_url", "agenda_packet_url")}
    mapping = {
        "agenda": "agenda_url",
        "minutes": "minutes_url",
        "video": "video_url",
        "packet": "agenda_packet_url",
        "agenda packet": "agenda_packet_url",
    }
    for anchor in cell.find_all("a", href=True):
        label = _clean(anchor.get_text(" ", strip=True)).casefold()
        field = mapping.get(label)
        if field is None:
            logger.warning(
                "Oro Valley link dropped: row=%d reason=unmapped_link_label label=%r href=%r",
                row_index,
                label,
                anchor.get("href"),
            )
            continue
        emitted = _safe_url(anchor.get("href", ""), base_url, row_index, field)
        if urls[field]:
            logger.warning(
                "Oro Valley duplicate link dropped: row=%d field=%s kept=%s dropped=%s",
                row_index,
                field,
                urls[field],
                emitted,
            )
            continue
        urls[field] = emitted
    return urls


def _safe_url(raw: str, base_url: str, row_index: int, field: str) -> str:
    lowered = raw.strip().casefold()
    if not lowered or lowered.startswith(("//", "javascript:", "data:", "file:", "mailto:")):
        logger.warning(
            "Oro Valley URL dropped: row=%d field=%s reason=empty_or_disallowed_scheme rejected=%r",
            row_index,
            field,
            raw,
        )
        return ""
    absolute = urljoin(base_url, raw)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        logger.warning(
            "Oro Valley URL dropped: row=%d field=%s reason=scheme_or_host_not_allowed rejected=%r",
            row_index,
            field,
            raw,
        )
        return ""
    return absolute


def _status(title: str, urls: dict[str, str]) -> str:
    if CANCELLED_RE.search(title):
        return "Cancelled"
    if urls["minutes_url"]:
        return "Minutes Available"
    if urls["agenda_url"] or urls["agenda_packet_url"]:
        return "Agenda Available"
    return "Scheduled"


def _validate(meeting: dict[str, str]) -> None:
    if tuple(meeting) != FIELDS:
        raise RuntimeError(f"Oro Valley canonical schema drifted: {tuple(meeting)}")
    if any(not isinstance(value, str) for value in meeting.values()):
        raise RuntimeError(f"Oro Valley canonical values must be strings: {meeting}")


def _clean(value: str) -> str:
    return " ".join(value.split()) if value else ""
