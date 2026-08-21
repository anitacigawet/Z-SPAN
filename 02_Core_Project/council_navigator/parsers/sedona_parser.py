"""Sedona City Council parser for the official server-rendered calendar list."""

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

DEFAULT_URL = (
    "https://www.sedonaaz.gov/i-want-to/advanced-components-not-displayed/"
    "custom-documents-images-calendar/-selcat-10/-toggle-all/-folder-5440"
)
ALLOWED_HOSTS = {"sedonaaz.gov", "www.sedonaaz.gov"}
MAX_RESPONSE_BYTES = 8_000_000
CHUNK_SIZE = 65_536
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")
# The lookahead deliberately follows the optional final dot; \b after "p.m." fails.
TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*([ap])\.?m\.?(?=\s|$|[-\u2013\u2014])", re.IGNORECASE)
EVENT_ID_PATTERNS = (
    re.compile(r"/Home/Components/Calendar/Event/(\d+)(?:/|$)", re.IGNORECASE),
    re.compile(r"/-item-(\d+)(?:/|$)", re.IGNORECASE),
)
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
    """Return Sedona City Council meetings from the current month forward."""
    logger.warning(
        "Sedona official council list does not expose meeting_location or ecomment_url; "
        "emitted rows use honest empty values"
    )
    session = make_session()
    try:
        status, final_url, html = _fetch_bounded(session, url)
    except requests.exceptions.SSLError:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning("Sedona official calendar failed verified TLS")
        return []
    if status in {401, 403, 429}:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Sedona official calendar edge blocked the neutral paced request: "
            "failure_shape=honest-empty missing_data_scope=all_current_and_future_meetings "
            "status=%d final_url=%s",
            status,
            final_url,
        )
        return []
    if status != 200:
        raise RuntimeError(f"Sedona official calendar returned HTTP {status}: {final_url}")

    soup = BeautifulSoup(html, "html.parser")
    page_title = _clean(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "city of sedona" not in page_title.casefold() or "calendar" not in page_title.casefold():
        raise RuntimeError(f"Sedona official calendar fingerprint drifted: title={page_title!r}")
    table, headers = _calendar_table(soup)
    if table is None:
        raise RuntimeError("Sedona official calendar fingerprint drifted: Event/Date-Time table missing")
    tbody = table.find("tbody")
    if tbody is None:
        raise RuntimeError("Sedona official calendar fingerprint drifted: calendar table tbody missing")
    logger.info("Sedona Granicus/GovAccess fingerprint witnessed: title=%r headers=%s", page_title, headers)

    cutoff = date.today().replace(day=1)
    rows_seen = 0
    accepted = 0
    drops: Counter[str] = Counter()
    field_absences: Counter[str] = Counter()
    seen_keys: set[tuple[str, str, str]] = set()
    meetings: list[dict] = []
    for row_index, row in enumerate(tbody.find_all("tr", recursive=False), start=1):
        rows_seen += 1
        cells = row.find_all("td", recursive=False)
        if len(cells) != len(headers):
            raise RuntimeError(
                f"Sedona calendar row/header drift: row={row_index} cells={len(cells)} headers={len(headers)}"
            )
        by_header = {headers[index]: cells[index] for index in range(len(headers))}
        title_cell = by_header["event"]
        title = _clean(title_cell.get_text(" ", strip=True))
        if not _is_city_council_title(title):
            drops["not_city_council_governing_body"] += 1
            continue

        meeting_date, meeting_time = _parse_datetime(
            _clean(by_header["date/time"].get_text(" ", strip=True)), row_index
        )
        if date.fromisoformat(meeting_date) < cutoff:
            drops["before_current_calendar_month"] += 1
            logger.info(
                "Sedona row dropped: reason=before_current_calendar_month row=%d date=%s cutoff=%s",
                row_index,
                meeting_date,
                cutoff.isoformat(),
            )
            continue

        title_anchor = title_cell.find("a", href=True)
        meeting_id = _meeting_id(title_anchor.get("href", "") if title_anchor else "", row_index)
        urls = _row_urls(by_header, final_url, row_index)
        duplicate_key = (meeting_id or title.casefold(), meeting_date, meeting_time)
        if duplicate_key in seen_keys:
            drops["duplicate_meeting"] += 1
            logger.warning(
                "Sedona row dropped: reason=duplicate_meeting row=%d key=%r",
                row_index,
                duplicate_key,
            )
            continue
        seen_keys.add(duplicate_key)

        for field in ("meeting_location", "ecomment_url"):
            field_absences[f"{field}:not_exposed_by_calendar_list"] += 1
        for field in ("agenda_url", "minutes_url", "video_url", "agenda_packet_url"):
            if not urls[field]:
                field_absences[f"{field}:no_same_row_link"] += 1
        if not meeting_id:
            field_absences["meeting_id:no_vendor_id_in_event_link"] += 1

        meeting = {
            "meeting_title": title,
            "meeting_date": meeting_date,
            "meeting_time": meeting_time,
            "meeting_location": "",
            "meeting_status": _status(title, urls),
            "agenda_url": urls["agenda_url"],
            "minutes_url": urls["minutes_url"],
            "video_url": urls["video_url"],
            "agenda_packet_url": urls["agenda_packet_url"],
            "ecomment_url": "",
            "meeting_id": meeting_id,
        }
        _validate_meeting(meeting)
        meetings.append(meeting)
        accepted += 1
        logger.info("Sedona meeting emitted: row=%d fields=%s", row_index, meeting)

    logger.warning(
        "Sedona scrape summary: rows_seen=%d rows_accepted=%d rows_dropped=%d "
        "drop_reasons=%s field_absences=%s",
        rows_seen,
        accepted,
        rows_seen - accepted,
        dict(drops),
        dict(field_absences),
    )
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Sedona witnessed zero current-month-forward City Council rows on the official calendar"
        )
    return meetings


def _fetch_bounded(session, url: str) -> tuple[int, str, str]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        raise ValueError(f"Sedona parser called with disallowed source URL: {url!r}")
    with session.get(url, timeout=30, stream=True, allow_redirects=True) as response:
        final_host = (urlparse(response.url).hostname or "").lower()
        if final_host not in ALLOWED_HOSTS:
            raise ValueError(f"Sedona redirect reached disallowed host: {final_host}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Sedona response exceeded {MAX_RESPONSE_BYTES} bytes")
        return response.status_code, response.url, bytes(body).decode(response.encoding or "utf-8", "replace")


def _calendar_table(soup: BeautifulSoup) -> tuple[Tag | None, list[str]]:
    allowed = {"event", "date/time", "agenda", "minutes", "other"}
    for table in soup.find_all("table"):
        headers = [_header(th.get_text(" ", strip=True)) for th in table.select("thead th")]
        if len(headers) >= 2 and headers[:2] == ["event", "date/time"]:
            unknown = [header for header in headers if header not in allowed]
            if unknown:
                raise RuntimeError(f"Sedona calendar header vocabulary drifted: {headers}")
            if len(headers) != len(set(headers)):
                raise RuntimeError(f"Sedona calendar contains duplicate headers: {headers}")
            return table, headers
    return None, []


def _header(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value.strip()).casefold()
    return "date/time" if normalized in {"date / time", "date/time", "date & time"} else normalized


def _is_city_council_title(title: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()
    excluded = ("committee", "work group", "recess no meeting", "selected members")
    return (
        "city council" in normalized
        and ("meeting" in normalized or "session" in normalized)
        and not any(phrase in normalized for phrase in excluded)
    )


def _parse_datetime(value: str, row_index: int) -> tuple[str, str]:
    date_match = DATE_RE.search(value)
    if date_match is None:
        raise RuntimeError(f"Sedona council row {row_index} lacks a parseable date: {value!r}")
    try:
        parsed_date = datetime.strptime(date_match.group(1), "%m/%d/%Y").date()
    except ValueError as exc:
        raise RuntimeError(f"Sedona council row {row_index} has an invalid date: {value!r}") from exc
    time_match = TIME_RE.search(value)
    if time_match is None:
        logger.warning(
            "Sedona meeting_time empty: row=%d reason=no_parseable_time input=%r",
            row_index,
            value,
        )
        return parsed_date.isoformat(), ""
    hour = int(time_match.group(1))
    minute = int(time_match.group(2))
    if not 1 <= hour <= 12 or not 0 <= minute <= 59:
        raise RuntimeError(f"Sedona council row {row_index} has an invalid time: {value!r}")
    suffix = "AM" if time_match.group(3).casefold() == "a" else "PM"
    return parsed_date.isoformat(), f"{hour}:{minute:02d} {suffix}"


def _meeting_id(href: str, row_index: int) -> str:
    if not href:
        logger.warning("Sedona meeting_id empty: row=%d reason=event_title_has_no_link", row_index)
        return ""
    for pattern in EVENT_ID_PATTERNS:
        match = pattern.search(href)
        if match:
            return match.group(1)
    logger.warning(
        "Sedona meeting_id empty: row=%d reason=unrecognized_event_link rejected=%r",
        row_index,
        href,
    )
    return ""


def _row_urls(by_header: dict[str, Tag], base_url: str, row_index: int) -> dict[str, str]:
    result = {field: "" for field in ("agenda_url", "minutes_url", "video_url", "agenda_packet_url")}
    for header, field in (("agenda", "agenda_url"), ("minutes", "minutes_url")):
        cell = by_header.get(header)
        if cell is None:
            continue
        anchors = cell.find_all("a", href=True)
        for anchor in anchors:
            emitted = _safe_url(anchor.get("href", ""), base_url, row_index, field)
            if result[field]:
                logger.warning(
                    "Sedona duplicate link dropped: row=%d field=%s kept=%s dropped=%s",
                    row_index,
                    field,
                    result[field],
                    emitted,
                )
                continue
            result[field] = emitted

    other = by_header.get("other")
    if other is not None:
        for anchor in other.find_all("a", href=True):
            label = _clean(
                " ".join(
                    str(part)
                    for part in (
                        anchor.get_text(" ", strip=True),
                        anchor.get("title", ""),
                        anchor.get("aria-label", ""),
                    )
                    if part
                )
            ).casefold()
            if "video" in label or "webcast" in label:
                field = "video_url"
            elif "packet" in label:
                field = "agenda_packet_url"
            else:
                logger.warning(
                    "Sedona Other link dropped: row=%d reason=unmapped_label label=%r href=%r",
                    row_index,
                    label,
                    anchor.get("href"),
                )
                continue
            emitted = _safe_url(anchor.get("href", ""), base_url, row_index, field)
            if result[field]:
                logger.warning(
                    "Sedona duplicate Other link dropped: row=%d field=%s kept=%s dropped=%s",
                    row_index,
                    field,
                    result[field],
                    emitted,
                )
                continue
            result[field] = emitted
    return result


def _safe_url(raw: str, base_url: str, row_index: int, field: str) -> str:
    value = raw.strip()
    lowered = value.casefold()
    if not value or lowered.startswith(
        ("//", "javascript:", "data:", "vbscript:", "file:", "mailto:", "ftp:")
    ):
        logger.warning(
            "Sedona URL dropped: row=%d field=%s reason=empty_or_disallowed_scheme rejected=%r",
            row_index,
            field,
            raw,
        )
        return ""
    absolute = urljoin(base_url, value)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        logger.warning(
            "Sedona URL dropped: row=%d field=%s reason=scheme_or_host_not_allowed rejected=%r",
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


def _validate_meeting(meeting: dict[str, str]) -> None:
    if tuple(meeting) != FIELDS:
        raise RuntimeError(f"Sedona canonical schema drifted: {tuple(meeting)}")
    if any(not isinstance(value, str) for value in meeting.values()):
        raise RuntimeError(f"Sedona canonical values must be strings: {meeting}")


def _clean(value: object) -> str:
    if value in (None, ""):
        return ""
    return " ".join(BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True).split())
