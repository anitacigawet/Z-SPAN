
"""Chino Valley Town Council parser for the official CivicClerk API."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import json
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from polite_http import make_session

__all__ = ["scrape_calendar"]

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://chinovalleyaz.portal.civicclerk.com/"
API_URL = "https://chinovalleyaz.api.civicclerk.com/v1/Events"
API_ROOT = "https://chinovalleyaz.api.civicclerk.com/"
PORTAL_HOST = "chinovalleyaz.portal.civicclerk.com"
API_HOST = "chinovalleyaz.api.civicclerk.com"
ALLOWED_OUTPUT_HOSTS = {
    API_HOST,
    "cpmedia.azureedge.net",
    "youtu.be",
    "youtube.com",
    "www.youtube.com",
}
MAX_RESPONSE_BYTES = 12_000_000
CHUNK_SIZE = 65_536
EVENT_LIMIT = 100
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
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


def scrape_calendar(url: str = DEFAULT_URL) -> list[dict]:
    """Return official Chino Valley Town Council events from this month forward."""
    _validate_input_url(url)
    cutoff = date.today().replace(day=1)
    params = {
        "$filter": f"startDateTime ge {cutoff.isoformat()}T00:00:00Z",
        "$orderby": "startDateTime asc",
        "$top": str(EVENT_LIMIT),
    }
    session = make_session()
    try:
        status, final_url, body = _fetch_bounded(session, API_URL, params)
    except requests.exceptions.SSLError:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning("Chino Valley official CivicClerk API failed verified TLS")
        return []
    if status in {401, 403, 429}:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Chino Valley official CivicClerk API blocked the neutral paced request: "
            "failure_shape=honest-empty missing_data_scope=all_current_and_future_meetings "
            "status=%d final_url=%s",
            status,
            final_url,
        )
        return []
    if status != 200:
        raise RuntimeError(f"Chino Valley CivicClerk API returned HTTP {status}: {final_url}")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Chino Valley CivicClerk API returned invalid JSON") from exc
    events = _validate_payload(payload)

    rows_seen = 0
    accepted = 0
    drops: Counter[str] = Counter()
    field_absences: Counter[str] = Counter()
    seen_ids: set[str] = set()
    meetings: list[dict] = []
    for position, value in enumerate(events, start=1):
        rows_seen += 1
        if not isinstance(value, dict):
            drops["non_object_event"] += 1
            logger.warning(
                "Chino Valley event dropped: reason=non_object_event position=%d value_type=%s",
                position,
                type(value).__name__,
            )
            continue

        meeting_id = _clean(value.get("id"))
        title = _clean(value.get("eventName"))
        row_id = meeting_id or f"position-{position}"
        if not _is_town_council_title(title):
            drops["not_town_council"] += 1
            continue
        if not meeting_id:
            raise RuntimeError(f"Chino Valley Town Council event lacks vendor id: position={position}")
        if meeting_id in seen_ids:
            drops["duplicate_event_id"] += 1
            logger.warning("Chino Valley event dropped: reason=duplicate_event_id id=%s", meeting_id)
            continue

        meeting_date, meeting_time = _parse_datetime(value.get("startDateTime"), row_id)
        if date.fromisoformat(meeting_date) < cutoff:
            drops["before_current_calendar_month"] += 1
            logger.warning(
                "Chino Valley event dropped despite API filter: reason=before_current_calendar_month "
                "id=%s date=%s cutoff=%s",
                row_id,
                meeting_date,
                cutoff.isoformat(),
            )
            continue
        seen_ids.add(meeting_id)

        documents = _documents(value.get("publishedFiles"), row_id, field_absences)
        location = _location(value.get("eventLocation"), row_id, field_absences)
        video_url = _video_url(value, row_id, field_absences)
        ecomment_url = _ecomment_url(value, row_id, field_absences)
        status_value = _status(value, title, documents, row_id)
        meeting = {
            "meeting_title": title,
            "meeting_date": meeting_date,
            "meeting_time": meeting_time,
            "meeting_location": location,
            "meeting_status": status_value,
            "agenda_url": documents["agenda_url"],
            "minutes_url": documents["minutes_url"],
            "video_url": video_url,
            "agenda_packet_url": documents["agenda_packet_url"],
            "ecomment_url": ecomment_url,
            "meeting_id": meeting_id,
        }
        _validate_meeting(meeting)
        meetings.append(meeting)
        accepted += 1
        logger.info("Chino Valley meeting emitted: id=%s fields=%s", meeting_id, meeting)

    logger.warning(
        "Chino Valley scrape summary: rows_seen=%d rows_accepted=%d rows_dropped=%d "
        "drop_reasons=%s field_absences=%s",
        rows_seen,
        accepted,
        rows_seen - accepted,
        dict(drops),
        dict(field_absences),
    )
    if not meetings:
        if drops["non_object_event"]:
            raise RuntimeError(
                "Chino Valley CivicClerk payload contained malformed event entries, so an official zero cannot be witnessed"
            )
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Chino Valley witnessed zero current-month-forward Town Council rows in the official API"
        )
    return meetings


def _validate_input_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in {PORTAL_HOST, API_HOST}:
        raise ValueError(f"Chino Valley parser called with disallowed source URL: {url!r}")
    if host == API_HOST and parsed.path.rstrip("/") != "/v1/Events":
        raise ValueError(f"Chino Valley parser called with unexpected CivicClerk API path: {url!r}")


def _fetch_bounded(session, url: str, params: dict[str, str]) -> tuple[int, str, str]:
    with session.get(
        url,
        params=params,
        timeout=30,
        stream=True,
        allow_redirects=True,
    ) as response:
        final_host = (urlparse(response.url).hostname or "").lower()
        if final_host != API_HOST:
            raise ValueError(f"Chino Valley redirect reached disallowed host: {final_host}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Chino Valley response exceeded {MAX_RESPONSE_BYTES} bytes")
        return response.status_code, response.url, bytes(body).decode(response.encoding or "utf-8", "replace")


def _validate_payload(payload: Any) -> list[Any]:
    if not isinstance(payload, dict):
        raise RuntimeError(f"Chino Valley CivicClerk fingerprint drifted: payload={type(payload).__name__}")
    context = str(payload.get("@odata.context") or "")
    if API_HOST not in context.casefold() or "/v1/$metadata#events" not in context.casefold():
        raise RuntimeError(f"Chino Valley CivicClerk fingerprint drifted: context={context!r}")
    events = payload.get("value")
    if not isinstance(events, list):
        raise RuntimeError("Chino Valley CivicClerk fingerprint drifted: value is not a list")
    if payload.get("@odata.nextLink"):
        raise RuntimeError(
            "Chino Valley CivicClerk current-forward result exceeded the one-request cap; "
            f"top={EVENT_LIMIT} nextLink={payload['@odata.nextLink']!r}"
        )
    logger.info("Chino Valley CivicClerk fingerprint witnessed: context=%s events=%d", context, len(events))
    return events


def _is_town_council_title(title: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()
    return "town council" in normalized and ("meeting" in normalized or "session" in normalized)


def _parse_datetime(value: Any, row_id: str) -> tuple[str, str]:
    raw = _clean(value)
    if not raw:
        raise RuntimeError(f"Chino Valley Town Council event lacks startDateTime: id={row_id}")
    try:
        # CivicClerk represents local wall-clock values with a trailing Z on this tenant.
        parsed = datetime.fromisoformat(raw[:-1] if raw.endswith("Z") else raw)
    except ValueError as exc:
        raise RuntimeError(f"Chino Valley event has unparsable startDateTime: id={row_id} value={raw!r}") from exc
    return parsed.date().isoformat(), parsed.strftime("%I:%M %p").lstrip("0")


def _documents(value: Any, row_id: str, field_absences: Counter[str]) -> dict[str, str]:
    result = {field: "" for field in ("agenda_url", "minutes_url", "agenda_packet_url")}
    if not isinstance(value, list):
        if value not in (None, ""):
            logger.warning(
                "Chino Valley documents dropped: id=%s reason=publishedFiles_not_list value_type=%s",
                row_id,
                type(value).__name__,
            )
        for field in result:
            field_absences[f"{field}:no_published_file"] += 1
        return result
    for position, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            logger.warning(
                "Chino Valley document dropped: id=%s position=%d reason=not_object value_type=%s",
                row_id,
                position,
                type(item).__name__,
            )
            continue
        document_type = _clean(item.get("type")).casefold()
        field = DOCUMENT_FIELDS.get(document_type)
        if field is None:
            logger.warning(
                "Chino Valley document dropped: id=%s position=%d reason=unmapped_type "
                "type=%r name=%r url=%r",
                row_id,
                position,
                item.get("type"),
                item.get("name"),
                item.get("url"),
            )
            continue
        candidate = item.get("url") or item.get("streamUrl")
        emitted = _safe_url(candidate, API_ROOT, row_id, field)
        if not emitted:
            continue
        if result[field]:
            logger.warning(
                "Chino Valley duplicate document dropped: id=%s field=%s kept=%s dropped=%s",
                row_id,
                field,
                result[field],
                emitted,
            )
            continue
        result[field] = emitted
    for field, emitted in result.items():
        if not emitted:
            field_absences[f"{field}:no_matching_published_file"] += 1
    return result


def _location(value: Any, row_id: str, field_absences: Counter[str]) -> str:
    if not isinstance(value, dict):
        field_absences["meeting_location:no_eventLocation"] += 1
        return ""
    parts: list[str] = []
    for key in ("name", "address1", "address2", "city", "state", "zipCode"):
        part = _clean(value.get(key))
        if part and part.casefold() not in {seen.casefold() for seen in parts}:
            parts.append(part)
    if not parts:
        field_absences["meeting_location:eventLocation_without_text"] += 1
        return ""
    return ", ".join(parts)


def _video_url(event: dict[str, Any], row_id: str, field_absences: Counter[str]) -> str:
    seen: set[str] = set()
    for key in ("externalMediaUrl", "mediaSourcePath", "mediaStreamPath", "mediaSourcePathMp4"):
        raw_candidate = event.get(key)
        candidate = "" if raw_candidate in (None, "") else str(raw_candidate).strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        emitted = _safe_url(candidate, API_ROOT, row_id, "video_url")
        if emitted:
            return emitted
    field_absences["video_url:no_valid_media_url"] += 1
    return ""


def _ecomment_url(event: dict[str, Any], row_id: str, field_absences: Counter[str]) -> str:
    for key in ("eCommentUrl", "ecommentUrl", "publicCommentUrl", "publicCommentsUrl"):
        candidate = event.get(key)
        if candidate:
            emitted = _safe_url(candidate, API_ROOT, row_id, "ecomment_url")
            if emitted:
                return emitted
    if event.get("publicCommentsEnabled") is True:
        logger.warning(
            "Chino Valley ecomment URL absent: id=%s publicCommentsEnabled=true but API exposed no stable URL",
            row_id,
        )
        field_absences["ecomment_url:enabled_without_url"] += 1
    else:
        field_absences["ecomment_url:not_exposed"] += 1
    return ""


def _safe_url(value: Any, base_url: str, row_id: str, field: str) -> str:
    raw = "" if value in (None, "") else str(value).strip()
    lowered = raw.casefold()
    if not raw:
        logger.warning("Chino Valley URL dropped: id=%s field=%s reason=empty", row_id, field)
        return ""
    if lowered.startswith(("//", "javascript:", "data:", "vbscript:", "file:", "mailto:", "ftp:")):
        logger.warning(
            "Chino Valley URL dropped: id=%s field=%s reason=disallowed_scheme rejected=%r",
            row_id,
            field,
            raw,
        )
        return ""
    absolute = urljoin(base_url, raw)
    parsed = urlparse(absolute)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in ALLOWED_OUTPUT_HOSTS:
        logger.warning(
            "Chino Valley URL dropped: id=%s field=%s reason=scheme_or_host_not_allowed rejected=%r",
            row_id,
            field,
            raw,
        )
        return ""
    return absolute


def _status(event: dict[str, Any], title: str, documents: dict[str, str], row_id: str) -> str:
    title_cancelled = bool(CANCELLED_RE.search(title))
    vendor_cancelled = event.get("isCancelled") is True or event.get("isCanceled") is True
    if vendor_cancelled and not title_cancelled:
        logger.warning(
            "Chino Valley non-canonical cancellation signal ignored: id=%s vendor flag true but title lacks cancellation wording",
            row_id,
        )
    if title_cancelled:
        return "Cancelled"
    if documents["minutes_url"]:
        return "Minutes Available"
    if documents["agenda_url"] or documents["agenda_packet_url"]:
        return "Agenda Available"
    return "Scheduled"


def _validate_meeting(meeting: dict[str, str]) -> None:
    if tuple(meeting) != FIELDS:
        raise RuntimeError(f"Chino Valley canonical schema drifted: {tuple(meeting)}")
    if any(not isinstance(value, str) for value in meeting.values()):
        raise RuntimeError(f"Chino Valley canonical values must be strings: {meeting}")


def _clean(value: Any) -> str:
    if value in (None, ""):
        return ""
    return " ".join(BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True).split())
