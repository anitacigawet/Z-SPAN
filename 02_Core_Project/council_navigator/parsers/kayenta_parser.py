"""Current-window Kayenta Township Commission events parser."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import html
import json
import logging
import re
from urllib.parse import urlencode, urljoin, urlparse, urlunparse

from requests import RequestException

from polite_http import make_session


DEFAULT_API_URL = (
    "https://www.kayentatownship-nsn.gov/"
    "wp-json/tribe/events/v1/events?per_page=50"
)
EXPECTED_HOST = "www.kayentatownship-nsn.gov"
MAX_RESPONSE_BYTES = 3_000_000
MAX_PAGES = 4
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
COMMISSION_RE = re.compile(
    r"^(?:Kayenta Township )?(?:Commission .*Meeting|Monthly Meeting|Public Meeting Notice)$",
    re.IGNORECASE,
)
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")

logger = logging.getLogger(__name__)


class SourceBlocked(RuntimeError):
    """The official event source could not be safely witnessed."""


def scrape_calendar(calendar_url: str | None = None) -> list[dict]:
    api_url = _api_url(calendar_url)
    floor = date.today().replace(day=1)
    upper = date(floor.year + 1, floor.month, 1)
    stats: Counter[str] = Counter()
    logger.warning(
        "Kayenta Tribe Events API does not expose agenda, minutes, video, "
        "agenda-packet, or ecomment URLs as separate event fields"
    )

    try:
        with make_session() as session:
            events = _fetch_events(session, api_url, floor, upper)
    except SourceBlocked as exc:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Kayenta official Township events source blocked: "
            "failure_shape=honest-empty "
            "missing_scope=current_month_forward_commission_meetings error=%s",
            exc,
        )
        return []

    meetings: list[dict] = []
    for index, event in enumerate(events, start=1):
        stats["rows_seen"] += 1
        event_id = str(event.get("id") or "")
        title = _clean_text(event.get("title"))
        if not COMMISSION_RE.search(title):
            stats["rows_dropped_non_commission"] += 1
            logger.warning(
                "Kayenta event dropped without Township Commission evidence: "
                "event_id=%s title=%r",
                event_id,
                title,
            )
            continue
        start = _parse_start(event.get("start_date"), event_id)
        if start is None:
            stats["rows_dropped_bad_date"] += 1
            continue
        if not (floor <= start.date() < upper):
            stats["rows_dropped_outside_window"] += 1
            logger.warning(
                "Kayenta Commission event outside bounded window: "
                "event_id=%s date=%s floor=%s upper=%s",
                event_id,
                start.date().isoformat(),
                floor.isoformat(),
                upper.isoformat(),
            )
            continue
        event_url = _emit_url(event.get("url"), api_url, event_id)
        location = _location(event.get("venue"), event_id)
        record = {
            "meeting_title": title,
            "meeting_date": start.date().isoformat(),
            "meeting_time": start.strftime("%I:%M %p").lstrip("0"),
            "meeting_location": location,
            "meeting_status": (
                "Cancelled" if CANCELLED_RE.search(title) else "Scheduled"
            ),
            "agenda_url": "",
            "minutes_url": "",
            "video_url": "",
            "agenda_packet_url": "",
            "ecomment_url": "",
            "meeting_id": event_id,
        }
        _validate_record(record)
        logger.info(
            "Kayenta Commission meeting emitted: event_id=%s date=%s "
            "time=%s location=%r source_event_url=%s",
            event_id,
            record["meeting_date"],
            record["meeting_time"],
            location,
            event_url,
        )
        meetings.append(record)
        stats["rows_emitted"] += 1

    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Kayenta official Tribe Events API is accessible with no "
            "current-month-forward Township Commission rows; "
            "floor=%s upper=%s stats=%s",
            floor.isoformat(),
            upper.isoformat(),
            dict(stats),
        )
    logger.info("Kayenta current-window scrape summary: %s", dict(stats))
    return sorted(meetings, key=lambda row: (row["meeting_date"], row["meeting_title"]))


def _api_url(calendar_url: str | None) -> str:
    source = calendar_url or DEFAULT_API_URL
    parsed = urlparse(source)
    host = (parsed.hostname or "").lower()
    if host not in {EXPECTED_HOST, "kayentatownship-nsn.gov"}:
        raise ValueError(f"Kayenta source host is not allowlisted: {host!r}")
    return urlunparse(
        parsed._replace(
            scheme="https",
            netloc=EXPECTED_HOST,
            path="/wp-json/tribe/events/v1/events",
            query="per_page=50",
            fragment="",
        )
    )


def _page_url(api_url: str, page: int, floor: date, upper: date) -> str:
    parsed = urlparse(api_url)
    query = urlencode(
        {
            "per_page": "50",
            "page": str(page),
            "start_date": f"{floor.isoformat()} 00:00:00",
            "end_date": f"{upper.isoformat()} 00:00:00",
        }
    )
    return urlunparse(parsed._replace(query=query))


def _fetch_events(session, api_url: str, floor: date, upper: date) -> list[dict]:
    events: list[dict] = []
    expected_total_pages: int | None = None
    for page in range(1, MAX_PAGES + 1):
        url = _page_url(api_url, page, floor, upper)
        data = _fetch_json(session, url)
        _validate_fingerprint(data, url)
        total_pages = data["total_pages"]
        if expected_total_pages is None:
            expected_total_pages = total_pages
            if total_pages > MAX_PAGES:
                raise SourceBlocked(
                    f"page_cap_exceeded total_pages={total_pages} max_pages={MAX_PAGES}"
                )
        elif total_pages != expected_total_pages:
            raise SourceBlocked(
                "pagination_shape_changed "
                f"first_total_pages={expected_total_pages} observed={total_pages}"
            )
        events.extend(data["events"])
        if page >= total_pages:
            break
    return events


def _fetch_json(session, url: str) -> dict:
    try:
        response_context = session.get(
            url,
            timeout=(10, 30),
            stream=True,
            allow_redirects=True,
            headers={"Accept": "application/json"},
        )
    except RequestException as exc:
        raise SourceBlocked(f"request_failed url={url} error={exc}") from exc
    with response_context as response:
        final_host = (urlparse(response.url).hostname or "").lower()
        if final_host != EXPECTED_HOST:
            raise SourceBlocked(
                f"redirect_disallowed url={url} final_host={final_host!r}"
            )
        try:
            response.raise_for_status()
        except RequestException as exc:
            raise SourceBlocked(
                f"http_status_failed url={url} status={response.status_code}"
            ) from exc
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise SourceBlocked(
                    f"response_too_large url={url} max_bytes={MAX_RESPONSE_BYTES}"
                )
    try:
        data = json.loads(bytes(body).decode(response.encoding or "utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise SourceBlocked(f"json_decode_failed url={url}") from exc
    if not isinstance(data, dict):
        raise SourceBlocked(
            f"fingerprint_mismatch url={url} expected=object observed={type(data).__name__}"
        )
    return data


def _validate_fingerprint(data: dict, url: str) -> None:
    expected = {"events", "rest_url", "total", "total_pages"}
    missing = sorted(expected - set(data))
    if missing or not isinstance(data.get("events"), list):
        raise SourceBlocked(
            f"fingerprint_mismatch url={url} missing={missing} "
            f"events_type={type(data.get('events')).__name__}"
        )
    if not isinstance(data.get("total"), int) or not isinstance(data.get("total_pages"), int):
        raise SourceBlocked(f"fingerprint_mismatch url={url} invalid_count_types")
    for index, event in enumerate(data["events"], start=1):
        if not isinstance(event, dict):
            raise SourceBlocked(
                f"fingerprint_mismatch url={url} event={index} not_object"
            )
        required = {"id", "title", "url", "start_date", "venue", "status"}
        if required - set(event):
            raise SourceBlocked(
                f"fingerprint_mismatch url={url} event={index} "
                f"missing={sorted(required - set(event))}"
            )
    logger.info(
        "vendor fingerprint witness=Tribe_Events_REST "
        "url=%s total=%s total_pages=%s",
        url,
        data["total"],
        data["total_pages"],
    )


def _parse_start(value: object, event_id: str) -> datetime | None:
    if not isinstance(value, str):
        logger.warning(
            "Kayenta event dropped: event_id=%s start_date=%r reason=not_string",
            event_id,
            value,
        )
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        logger.warning(
            "Kayenta event dropped: event_id=%s start_date=%r reason=bad_format",
            event_id,
            value,
        )
        return None


def _location(value: object, event_id: str) -> str:
    if value in (None, [], {}):
        logger.info(
            "Kayenta meeting_location honest-empty: event_id=%s reason=no_venue",
            event_id,
        )
        return ""
    if not isinstance(value, dict):
        raise SourceBlocked(
            f"fingerprint_mismatch event_id={event_id} venue_type={type(value).__name__}"
        )
    location = _clean_text(value.get("venue"))
    if not location:
        logger.info(
            "Kayenta meeting_location honest-empty: event_id=%s "
            "reason=venue_object_without_name",
            event_id,
        )
    return location


def _emit_url(value: object, base_url: str, event_id: str) -> str:
    href = str(value or "").strip()
    if not href:
        logger.warning(
            "Kayenta source event URL absent: event_id=%s value=%r",
            event_id,
            value,
        )
        return ""
    absolute = urljoin(base_url, href)
    parsed = urlparse(absolute)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {
        EXPECTED_HOST,
        "kayentatownship-nsn.gov",
    }:
        logger.warning(
            "Kayenta source event URL rejected: event_id=%s value=%r",
            event_id,
            absolute,
        )
        return ""
    return absolute


def _clean_text(value: object) -> str:
    text = html.unescape(str(value or ""))
    return " ".join(TAG_RE.sub(" ", text).split())


def _validate_record(record: dict[str, str]) -> None:
    if tuple(record) != CANONICAL_FIELDS:
        raise ValueError(f"Kayenta parser emitted noncanonical fields: {tuple(record)}")
    if any(not isinstance(value, str) for value in record.values()):
        raise TypeError("Kayenta parser emitted a non-string field")
