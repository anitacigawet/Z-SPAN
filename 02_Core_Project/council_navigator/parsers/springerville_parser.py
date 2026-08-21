"""Bounded current-month-forward parser for Springerville's official agenda API."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import json
import logging
import re
from urllib.parse import quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from polite_http import make_session

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://www.springervilleaz.gov/agendas"
API_URL = "https://www.springervilleaz.gov/GetAgendaData"
OFFICIAL_HOSTS = {"springervilleaz.gov", "www.springervilleaz.gov"}
VIDEO_HOSTS = OFFICIAL_HOSTS | {
    "youtube.com", "www.youtube.com", "youtu.be", "www.youtu.be",
}
MAX_RESPONSE_BYTES = 2_000_000
PAGE_SIZE = 100
COUNCIL_TYPE = "Current Council"
_CANCEL_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)

_REQUEST_DATA = {
    "draw": "1",
    "start": "1",
    "length": str(PAGE_SIZE),
    "take": str(PAGE_SIZE),
    "SortBy": "AgendaDate",
    "SortType": "desc",
    "search": "",
    "Classifications": "",
    "type": "",
}


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _validate_calendar_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or _host(url) not in OFFICIAL_HOSTS:
        raise ValueError("Springerville calendar URL must use HTTPS on the official town host")


def _post_json_bounded(session) -> dict:
    try:
        response_context = session.post(
            API_URL,
            data=_REQUEST_DATA,
            timeout=30,
            stream=True,
            allow_redirects=True,
        )
    except requests.exceptions.SSLError:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning("Springerville official agenda API failed verified TLS")
        raise
    with response_context as response:
        if getattr(response, "status_code", None) in {401, 403, 429}:
            logger.warning("health_empty_kind=source_blocked")
        response.raise_for_status()
        if urlparse(response.url).scheme != "https" or _host(response.url) not in OFFICIAL_HOSTS:
            raise RuntimeError(f"Springerville API redirected to a disallowed host: {response.url}")
        if "json" not in response.headers.get("content-type", "").lower():
            raise RuntimeError("Springerville GetAgendaData response is no longer JSON")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise RuntimeError(
                    f"Springerville API response exceeded the {MAX_RESPONSE_BYTES}-byte safety cap"
                )
        try:
            payload = json.loads(bytes(body).decode(response.encoding or "utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Springerville GetAgendaData returned malformed JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise RuntimeError("Springerville GetAgendaData lost its DataTables data fingerprint")
        logger.info("Springerville fetched %s API bytes", len(body))
        return payload


def _parse_iso_date(raw: object, row_id: str) -> date:
    if not isinstance(raw, str):
        raise RuntimeError(f"Springerville row {row_id} has no string agendaDate")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"Springerville row {row_id} has an unparseable agendaDate: {raw!r}"
        ) from exc


def _clean_title(raw: object, row_id: str) -> str:
    if not isinstance(raw, str):
        raise RuntimeError(f"Springerville row {row_id} has no string title")
    title = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    if not title:
        raise RuntimeError(f"Springerville row {row_id} has an empty title")
    return title


def _format_time(raw: object, row_id: str) -> str:
    if raw in (None, ""):
        return ""
    if not isinstance(raw, str):
        raise RuntimeError(f"Springerville row {row_id} has a non-string meetingTime")
    try:
        parsed = datetime.strptime(raw.strip(), "%H:%M")
    except ValueError as exc:
        raise RuntimeError(
            f"Springerville row {row_id} has an unparseable meetingTime: {raw!r}"
        ) from exc
    return parsed.strftime("%I:%M %p").lstrip("0")


def _media_url(raw_path: object, row_id: str, field: str) -> str:
    if raw_path in (None, ""):
        return ""
    if not isinstance(raw_path, str):
        raise RuntimeError(f"Springerville row {row_id} has a non-string {field} path")
    path = raw_path.strip().replace("\\", "/")
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc or path.startswith("/") or ".." in path.split("/"):
        logger.warning(
            "Springerville row %s dropped %s path %r: expected a safe relative media path",
            row_id, field, raw_path,
        )
        return ""
    absolute = urljoin("https://www.springervilleaz.gov/media/", quote(path, safe="/"))
    if urlparse(absolute).scheme != "https" or _host(absolute) not in OFFICIAL_HOSTS:
        logger.warning(
            "Springerville row %s dropped %s path %r: resolved host is not allowlisted",
            row_id, field, raw_path,
        )
        return ""
    return absolute


def _video_url(raw_media: object, row_id: str) -> str:
    if raw_media in (None, "", "[]", []):
        return ""
    media = raw_media
    if isinstance(raw_media, str):
        try:
            media = json.loads(raw_media)
        except json.JSONDecodeError:
            logger.warning(
                "Springerville row %s exposed unparseable media metadata: %r", row_id, raw_media
            )
            return ""
    if not isinstance(media, list):
        logger.warning("Springerville row %s exposed non-list media metadata: %r", row_id, media)
        return ""
    for item in media:
        if not isinstance(item, dict):
            continue
        label = " ".join(str(item.get(key, "")) for key in ("title", "type", "name")).lower()
        candidate = next(
            (item.get(key) for key in ("url", "mediaUrl", "href") if item.get(key)), ""
        )
        if not isinstance(candidate, str) or not candidate:
            continue
        absolute = urljoin("https://www.springervilleaz.gov/", candidate)
        parsed = urlparse(absolute)
        youtube_hosts = {"youtube.com", "www.youtube.com", "youtu.be", "www.youtu.be"}
        if "video" not in label and _host(absolute) not in youtube_hosts:
            continue
        if parsed.scheme == "https" and _host(absolute) in VIDEO_HOSTS:
            return absolute
        logger.warning(
            "Springerville row %s dropped video URL %r: HTTPS host is not allowlisted",
            row_id, candidate,
        )
    logger.warning(
        "Springerville row %s exposed media metadata but no supported video URL: %r",
        row_id, media,
    )
    return ""


def _status(title: str, minutes_url: str, agenda_url: str, packet_url: str) -> str:
    if _CANCEL_RE.search(title):
        return "Cancelled"
    if minutes_url:
        return "Minutes Available"
    if agenda_url or packet_url:
        return "Agenda Available"
    return "Scheduled"


def _parse_payload(payload: dict, cutoff: date) -> list[dict[str, str]]:
    rows = payload["data"]
    try:
        records_total = int(payload["recordsTotal"])
        records_filtered = int(payload["recordsFiltered"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Springerville DataTables response lost its record counts") from exc
    if records_total < 0 or records_filtered < 0 or records_filtered > records_total:
        raise RuntimeError("Springerville DataTables response has impossible record counts")
    if len(rows) > PAGE_SIZE:
        raise RuntimeError("Springerville API exceeded the requested 100-row safety cap")

    dated_rows: list[tuple[date, dict, str]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise RuntimeError(f"Springerville API row {index} is not an object")
        row_id = str(row.get("contentItemId") or "")
        if not row_id:
            raise RuntimeError(f"Springerville API row {index} has no contentItemId")
        dated_rows.append((_parse_iso_date(row.get("agendaDate"), row_id), row, row_id))

    dates = [meeting_date for meeting_date, _row, _row_id in dated_rows]
    if dates != sorted(dates, reverse=True):
        raise RuntimeError("Springerville API did not honor the requested descending AgendaDate sort")
    if records_filtered > len(rows):
        if len(rows) != PAGE_SIZE:
            raise RuntimeError("Springerville API truncated the requested 100-row page unexpectedly")
        if not dates or dates[-1] >= cutoff:
            raise RuntimeError(
                "Springerville's 100-row cap could hide current-month records; refusing partial output"
            )

    logger.warning(
        "Springerville agenda surface does not expose a per-row meeting location; emitting empty locations"
    )
    meetings: list[dict[str, str]] = []
    skipped_types: Counter[str] = Counter()
    historical = 0
    field_absences: Counter[str] = Counter()
    seen_ids: set[str] = set()
    for meeting_date, row, row_id in dated_rows:
        if meeting_date < cutoff:
            historical += 1
            continue
        agenda_type = row.get("agendaType")
        if agenda_type != COUNCIL_TYPE:
            skipped_types[str(agenda_type)] += 1
            continue
        if row_id in seen_ids:
            raise RuntimeError(f"Springerville API emitted duplicate contentItemId {row_id}")
        seen_ids.add(row_id)

        title = _clean_title(row.get("title"), row_id)
        meeting_time = _format_time(row.get("meetingTime"), row_id)
        agenda_url = _media_url(row.get("agendaUpload"), row_id, "agenda_url")
        minutes_url = _media_url(row.get("minutesUpload"), row_id, "minutes_url")
        packet_url = _media_url(row.get("packetUpload"), row_id, "agenda_packet_url")
        video_url = _video_url(row.get("media"), row_id)
        for field, value in (
            ("meeting_time", meeting_time),
            ("agenda_url", agenda_url),
            ("minutes_url", minutes_url),
            ("agenda_packet_url", packet_url),
            ("video_url", video_url),
        ):
            if not value:
                field_absences[field] += 1
        meetings.append(
            {
                "meeting_title": title,
                "meeting_date": meeting_date.isoformat(),
                "meeting_time": meeting_time,
                "meeting_location": "",
                "meeting_status": _status(title, minutes_url, agenda_url, packet_url),
                "agenda_url": agenda_url,
                "minutes_url": minutes_url,
                "video_url": video_url,
                "agenda_packet_url": packet_url,
                "ecomment_url": "",
                "meeting_id": row_id,
            }
        )

    logger.info(
        "Springerville audit: rows_seen=%s total=%s historical_dropped=%s "
        "non_council_current=%s emitted=%s absent_fields=%s",
        len(rows), records_total, historical, dict(skipped_types), len(meetings), dict(field_absences),
    )
    return meetings


def scrape_calendar(calendar_url: str = DEFAULT_URL) -> list[dict[str, str]]:
    """Return official Current Council records from this calendar month forward."""
    _validate_calendar_url(calendar_url)
    cutoff = date.today().replace(day=1)
    with make_session() as session:
        payload = _post_json_bounded(session)
    meetings = _parse_payload(payload, cutoff)
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Springerville witnessed zero current-month-forward Current Council rows in the official API"
        )
    return meetings
