"""Rio Rico coverage via Santa Cruz County's official Board of Supervisors feed."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from html import unescape
import json
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from polite_http import make_session

__all__ = ["scrape_calendar"]

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://santacruzcoaz.portal.civicclerk.com/"
API_URL = "https://santacruzcoaz.api.civicclerk.com/v1/Events"
API_ROOT = "https://santacruzcoaz.api.civicclerk.com/"
PORTAL_HOST = "santacruzcoaz.portal.civicclerk.com"
API_HOST = "santacruzcoaz.api.civicclerk.com"
ALLOWED_OUTPUT_HOSTS = {
    API_HOST,
    "cpmedia.azureedge.net",
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
}
MAX_RESPONSE_BYTES = 12_000_000
CHUNK_SIZE = 65_536
MAX_PAGES = 3
EVENT_LIMIT = 100
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
DOCUMENT_FIELDS = {
    "agenda": "agenda_url",
    "agenda packet": "agenda_packet_url",
    "minutes": "minutes_url",
}
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
BOARD_TITLE_RE = re.compile(
    r"^(?:regular|special) meeting of the board of supervisors$",
    re.IGNORECASE,
)


def scrape_calendar(url: str = DEFAULT_URL) -> list[dict[str, str]]:
    """Return Santa Cruz County Board meetings from this month forward."""
    _validate_input_url(url)
    cutoff = date.today().replace(day=1)
    params = {
        "$filter": f"startDateTime ge {cutoff.isoformat()}T00:00:00Z",
        "$orderby": "startDateTime asc",
        "$top": str(EVENT_LIMIT),
    }
    events = _fetch_events(make_session(), params)
    stats: Counter[str] = Counter()
    meetings: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for position, value in enumerate(events, start=1):
        stats["rows_seen"] += 1
        if not isinstance(value, dict):
            raise RuntimeError(
                f"Santa Cruz County CivicClerk event is not an object: position={position} type={type(value).__name__}"
            )
        meeting_id = _clean(value.get("id"))
        title = _clean(value.get("eventName"))
        row_id = meeting_id or f"position-{position}"
        if not BOARD_TITLE_RE.fullmatch(title):
            stats["not_board_meeting"] += 1
            logger.info(
                "Rio Rico coverage row dropped: reason=not_board_meeting id=%s title=%r category=%r",
                row_id,
                title,
                value.get("eventCategoryName") or value.get("categoryName"),
            )
            continue
        if not meeting_id:
            raise RuntimeError(f"Santa Cruz County Board event lacks vendor ID: position={position}")
        if meeting_id in seen_ids:
            stats["duplicate_id"] += 1
            logger.warning("Rio Rico coverage row dropped: reason=duplicate_id id=%s", meeting_id)
            continue
        meeting_date, meeting_time = _parse_datetime(value.get("startDateTime"), row_id)
        if date.fromisoformat(meeting_date) < cutoff:
            stats["before_current_month"] += 1
            logger.warning(
                "Rio Rico coverage row dropped despite API filter: id=%s date=%s cutoff=%s",
                row_id,
                meeting_date,
                cutoff.isoformat(),
            )
            continue
        if value.get("isDeleted") is True:
            stats["deleted_event"] += 1
            logger.warning("Rio Rico coverage row dropped: reason=isDeleted_true id=%s", row_id)
            continue
        seen_ids.add(meeting_id)
        documents = _documents(value.get("publishedFiles"), row_id, stats)
        video_url = _video_url(value, row_id, stats)
        meeting = {
            "meeting_title": title,
            "meeting_date": meeting_date,
            "meeting_time": meeting_time,
            "meeting_location": _location(value.get("eventLocation"), row_id, stats),
            "meeting_status": _status(title, documents),
            "agenda_url": documents["agenda_url"],
            "minutes_url": documents["minutes_url"],
            "video_url": video_url,
            "agenda_packet_url": documents["agenda_packet_url"],
            "ecomment_url": "",
            "meeting_id": meeting_id,
        }
        _validate_meeting(meeting)
        meetings.append(meeting)
        stats["rows_accepted"] += 1
        stats["ecomment_url:not_exposed"] += 1
        logger.info("Rio Rico coverage meeting emitted: id=%s date=%s title=%r", meeting_id, meeting_date, title)

    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Santa Cruz County official current-forward feed has no qualifying Board meetings: stats=%s",
            dict(stats),
        )
    logger.warning(
        "Rio Rico coverage scrape summary: rows_seen=%d accepted=%d drop_reasons=%s field_absences=%s",
        stats["rows_seen"],
        stats["rows_accepted"],
        {
            key: value
            for key, value in stats.items()
            if ":" not in key and key not in {"rows_seen", "rows_accepted"}
        },
        {key: value for key, value in stats.items() if ":" in key},
    )
    return meetings


def _fetch_events(session: Any, params: dict[str, str]) -> list[Any]:
    url = API_URL
    page_params: dict[str, str] | None = params
    events: list[Any] = []
    visited: set[str] = set()
    for page_number in range(1, MAX_PAGES + 1):
        status, final_url, body = _fetch_bounded(session, url, page_params)
        if status in {401, 403}:
            logger.warning("health_empty_kind=source_blocked")
            logger.warning(
                "Santa Cruz County official CivicClerk API blocked the neutral paced request: "
                "status=%d page=%d final_url=%s missing_data_scope=all_current_and_future_board_meetings",
                status,
                page_number,
                final_url,
            )
            return []
        if status != 200:
            raise RuntimeError(
                f"Santa Cruz County CivicClerk API returned HTTP {status}: page={page_number} url={final_url}"
            )
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Santa Cruz County CivicClerk API returned invalid JSON") from exc
        page_events, next_link = _validate_payload(payload, page_number)
        events.extend(page_events)
        if not next_link:
            return events
        next_url = _validate_next_link(next_link)
        if next_url in visited:
            raise RuntimeError(f"Santa Cruz County CivicClerk pagination loop detected: {next_url}")
        visited.add(next_url)
        url = next_url
        page_params = None
    raise RuntimeError(
        f"Santa Cruz County current-forward CivicClerk results exceeded bounded pagination cap={MAX_PAGES}"
    )


def _fetch_bounded(
    session: Any,
    url: str,
    params: dict[str, str] | None,
) -> tuple[int, str, str]:
    with session.get(
        url,
        params=params,
        timeout=30,
        stream=True,
        allow_redirects=True,
    ) as response:
        final_host = (urlparse(response.url).hostname or "").lower()
        if final_host != API_HOST:
            raise ValueError(f"Santa Cruz County redirect reached disallowed host: {final_host}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Santa Cruz County response exceeded {MAX_RESPONSE_BYTES} bytes")
        return response.status_code, response.url, bytes(body).decode(response.encoding or "utf-8", "replace")


def _validate_payload(payload: Any, page_number: int) -> tuple[list[Any], str]:
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"Santa Cruz County CivicClerk fingerprint drifted: page={page_number} type={type(payload).__name__}"
        )
    context = _clean(payload.get("@odata.context"))
    if API_HOST not in context.casefold() or "/v1/$metadata#events" not in context.casefold():
        raise RuntimeError(
            f"Santa Cruz County CivicClerk context drifted: page={page_number} context={context!r}"
        )
    values = payload.get("value")
    if not isinstance(values, list):
        raise RuntimeError(f"Santa Cruz County CivicClerk value is not a list: page={page_number}")
    logger.info(
        "Santa Cruz County CivicClerk fingerprint witnessed: page=%d events=%d context=%s",
        page_number,
        len(values),
        context,
    )
    return values, _clean(payload.get("@odata.nextLink"))


def _validate_next_link(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != API_HOST:
        raise RuntimeError(f"Santa Cruz County CivicClerk nextLink host drifted: {value!r}")
    if parsed.path.rstrip("/").casefold() != "/v1/events":
        raise RuntimeError(f"Santa Cruz County CivicClerk nextLink path drifted: {value!r}")
    return value


def _validate_input_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in {PORTAL_HOST, API_HOST}:
        raise ValueError(f"Rio Rico parser called with disallowed source URL: {url!r}")
    if host == API_HOST and parsed.path.rstrip("/").casefold() != "/v1/events":
        raise ValueError(f"Rio Rico parser called with unexpected CivicClerk path: {url!r}")


def _parse_datetime(value: Any, row_id: str) -> tuple[str, str]:
    raw = _clean(value)
    if not raw:
        raise RuntimeError(f"Santa Cruz County Board event lacks startDateTime: id={row_id}")
    try:
        parsed = datetime.fromisoformat(raw[:-1] if raw.endswith("Z") else raw)
    except ValueError as exc:
        raise RuntimeError(
            f"Santa Cruz County Board event has unparseable datetime: id={row_id} value={raw!r}"
        ) from exc
    return parsed.date().isoformat(), parsed.strftime("%I:%M %p").lstrip("0")


def _documents(value: Any, row_id: str, stats: Counter[str]) -> dict[str, str]:
    result = {field: "" for field in ("agenda_url", "minutes_url", "agenda_packet_url")}
    if not isinstance(value, list):
        raise RuntimeError(
            f"Santa Cruz County Board event publishedFiles is not a list: id={row_id} type={type(value).__name__}"
        )
    for position, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise RuntimeError(
                f"Santa Cruz County published file is not an object: id={row_id} position={position}"
            )
        document_type = _clean(item.get("type")).casefold()
        field = DOCUMENT_FIELDS.get(document_type)
        if field is None:
            logger.warning(
                "Rio Rico coverage document dropped: id=%s position=%d reason=unmapped_type "
                "type=%r name=%r url=%r",
                row_id,
                position,
                item.get("type"),
                item.get("name"),
                item.get("url"),
            )
            stats[f"document_type:{document_type or 'empty'}"] += 1
            continue
        emitted = _safe_url(item.get("url") or item.get("streamUrl"), API_ROOT, row_id, field)
        if not emitted:
            continue
        if result[field]:
            logger.warning(
                "Rio Rico coverage duplicate document dropped: id=%s field=%s kept=%s dropped=%s",
                row_id,
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


def _location(value: Any, row_id: str, stats: Counter[str]) -> str:
    if not isinstance(value, dict):
        stats["meeting_location:not_exposed"] += 1
        return ""
    parts: list[str] = []
    for key in ("address1", "address2", "city", "state", "zipCode"):
        part = _clean(value.get(key))
        if part and part.casefold() not in {existing.casefold() for existing in parts}:
            parts.append(part)
    if not parts:
        stats["meeting_location:not_exposed"] += 1
        logger.warning("Rio Rico coverage location honest-empty: id=%s reason=no_text_in_eventLocation", row_id)
        return ""
    return ", ".join(parts)


def _video_url(event: dict[str, Any], row_id: str, stats: Counter[str]) -> str:
    candidates = (
        event.get("externalMediaUrl"),
        event.get("mediaSourcePath"),
        event.get("mediaStreamPath"),
        event.get("mediaSourcePathMp4"),
    )
    for candidate in candidates:
        if candidate:
            emitted = _safe_url(candidate, API_ROOT, row_id, "video_url")
            if emitted:
                return emitted
    stats["video_url:not_exposed"] += 1
    return ""


def _safe_url(value: Any, base_url: str, row_id: str, field: str) -> str:
    raw = _clean(value)
    if not raw:
        logger.warning("Rio Rico coverage URL dropped: id=%s field=%s reason=empty", row_id, field)
        return ""
    if raw.casefold().startswith(("//", "javascript:", "data:", "file:", "mailto:", "ftp:")):
        logger.warning(
            "Rio Rico coverage URL dropped: id=%s field=%s reason=disallowed_scheme value=%r",
            row_id,
            field,
            raw,
        )
        return ""
    absolute = urljoin(base_url, raw)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower() not in ALLOWED_OUTPUT_HOSTS:
        logger.warning(
            "Rio Rico coverage URL dropped: id=%s field=%s reason=disallowed_host value=%r",
            row_id,
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
        raise RuntimeError(f"Rio Rico canonical schema drifted: {tuple(meeting)}")
    if any(not isinstance(value, str) for value in meeting.values()):
        raise RuntimeError(f"Rio Rico canonical values must be strings: {meeting!r}")


def _clean(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = unescape(str(value))
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return " ".join(text.split())
