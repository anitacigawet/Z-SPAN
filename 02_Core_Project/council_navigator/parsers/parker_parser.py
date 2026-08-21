"""Parker, AZ — Thrillshare CMS events REST parser.

Parker's town site (https://www.townofparkeraz.com/) migrated to the Thrillshare
CMS by Apptegy (org_id=18804). The public site sits behind an Apptegy JS client
challenge (`_fs-ch-` fingerprint) that blocks non-browser clients, but the
Thrillshare CMS JSON events endpoint at
`https://thrillshare-cmsv2.services.thrillshare.com/api/v4/o/18804/cms/events`
is directly accessible without the challenge.

The events feed is chronological forward from *now* (no historical archive — the
feed's page 602 is dated 2085; the CMS front-loads a small window of upcoming
+ far-future recurring events). Historical council-agenda PDFs live at
`/documents/departments/town-clerk/council-agenda/<id>` behind the same Nuxt SPA
and are not reachable via this parser without headless rendering. That's a
V2 nice-to-have; this V1 parser catches upcoming governance meetings and
emits the canonical schema for the pipeline.

The current parser emits only Town Council meeting/session titles; advisory
boards, commissions, and community events are outside the flagship scope.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from html import unescape
from urllib.parse import urlencode, urlparse

from polite_http import make_session


logger = logging.getLogger(__name__)

DEFAULT_URL = "https://www.townofparkeraz.com/events"
API_URL = "https://thrillshare-cmsv2.services.thrillshare.com/api/v4/o/18804/cms/events"
ALLOWED_HOSTS = {
    "thrillshare-cmsv2.services.thrillshare.com",
    "parkeraz.thrillshare.com",
    "www.townofparkeraz.com",
    "townofparkeraz.com",
}
MAX_RESPONSE_BYTES = 5_000_000
PER_PAGE = 100
MAX_PAGES = 4
FUTURE_MONTHS = 13
BLOCKED_STATUSES = {401, 403, 429}
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

COUNCIL_RE = re.compile(
    r"\b(?:(?:regular|special|town)\s+)?council\s+(?:meeting|session|work\s+session|study\s+session)\b",
    re.IGNORECASE,
)
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)


def scrape_calendar(
    url: str = DEFAULT_URL,
    *,
    today: date | None = None,
) -> list[dict[str, str]]:
    """Return Parker Town Council meetings from the current month forward."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {
        "www.townofparkeraz.com",
        "townofparkeraz.com",
    }:
        raise ValueError(f"Parker parser requires the official town events URL: {url!r}")
    month_floor = (today or date.today()).replace(day=1)
    events, source_blocked = _fetch_events(month_floor)
    if source_blocked:
        logger.warning("health_empty_kind=source_blocked")
        return []
    logger.warning(
        "Thrillshare Council search rows expose no agenda_url, minutes_url, video_url, "
        "agenda_packet_url, or ecomment_url fields; all remain empty without row evidence"
    )
    meetings: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    dropped_non_council = 0

    for event in events:
        if not isinstance(event, dict):
            continue

        title = _clean_text(str(event.get("title") or ""))
        if not title:
            continue

        if not COUNCIL_RE.search(title):
            dropped_non_council += 1
            continue

        meeting = _build_meeting(event)
        if not meeting["meeting_date"]:
            raise ValueError(f"Parker council event lacks a valid date: id={meeting['meeting_id']!r}")
        if date.fromisoformat(meeting["meeting_date"]) < month_floor:
            logger.warning(
                "dropped council event before current-month floor id=%s date=%s floor=%s title=%r",
                meeting["meeting_id"],
                meeting["meeting_date"],
                month_floor.isoformat(),
                title,
            )
            continue
        meeting_id = meeting["meeting_id"]
        if meeting_id and meeting_id in seen_ids:
            logger.warning(
                "dropped duplicate event id=%s title=%r",
                meeting_id,
                meeting["meeting_title"],
            )
            continue
        if meeting_id:
            seen_ids.add(meeting_id)

        _validate_schema(meeting)
        meetings.append(meeting)
        logger.info(
            "emitted event id=%s date=%s title=%r status=%s",
            meeting["meeting_id"],
            meeting["meeting_date"],
            meeting["meeting_title"],
            meeting["meeting_status"],
        )

    meetings.sort(key=lambda row: (row["meeting_date"], row["meeting_time"], row["meeting_id"]))
    logger.info(
        "parker_parser emitted %d meetings from %d bounded Thrillshare search results (dropped %d non-council)",
        len(meetings),
        len(events),
        dropped_non_council,
    )
    if events and not meetings:
        raise ValueError("Parker Thrillshare Council search returned rows but none matched Town Council scope")
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
    return meetings


def _fetch_events(month_floor: date) -> tuple[list[dict], bool]:
    """Fetch the finite Council search window through the paced session."""
    events: list[dict] = []
    window_end = _month_start_offset(month_floor, FUTURE_MONTHS) - timedelta(days=1)
    with make_session() as session:
        for page in range(1, MAX_PAGES + 1):
            params = {
                "locale": "en",
                "per_page": str(PER_PAGE),
                "page_no": str(page),
                "start_date": month_floor.isoformat(),
                "end_date": window_end.isoformat(),
                "search": "Council",
            }
            api_page_url = f"{API_URL}?{urlencode(params)}"
            status, text, final_url = _fetch_text(session, api_page_url, accept="application/json")
            if status in BLOCKED_STATUSES:
                logger.warning(
                    "Parker official Thrillshare feed blocked the neutral paced request: status=%d url=%s page=%d",
                    status,
                    final_url,
                    page,
                )
                return [], True
            if status != 200:
                raise RuntimeError(f"Parker Thrillshare feed returned HTTP {status}: {final_url}")
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError(
                    f"Unexpected Thrillshare payload on page {page}: {type(data).__name__}"
                )

            page_events = data.get("events", [])
            if not isinstance(page_events, list):
                raise ValueError(
                    f"Unexpected Thrillshare events payload on page {page}: {type(page_events).__name__}"
                )

            total_entries = (
                data.get("meta", {}).get("links", {}).get("total_entries")
                if isinstance(data.get("meta"), dict)
                else None
            )
            logger.info(
                "fetched Thrillshare page=%d count=%d total_entries=%r",
                page,
                len(page_events),
                total_entries,
            )
            if any(not isinstance(event, dict) for event in page_events):
                raise ValueError(f"Thrillshare page {page} contains a non-object event")
            events.extend(page_events)
            links = data.get("meta", {}).get("links", {}) if isinstance(data.get("meta"), dict) else {}
            next_url = links.get("next") if isinstance(links, dict) else None
            if not next_url:
                break
            if page == MAX_PAGES:
                raise ValueError(f"Parker Thrillshare search exceeded the {MAX_PAGES}-request hard cap")

    return events, False


def _month_start_offset(month_floor: date, months: int) -> date:
    month_index = month_floor.year * 12 + month_floor.month - 1 + months
    year, zero_based_month = divmod(month_index, 12)
    return date(year, zero_based_month + 1, 1)


def _build_meeting(event: dict) -> dict[str, str]:
    event_id = _clean_text(str(event.get("id") or ""))
    title = _clean_text(str(event.get("title") or ""))
    meeting_date = _extract_date(event, event_id)
    meeting_time = _extract_time(event)
    meeting_location = _extract_location(event)

    # Thrillshare events feed does not expose per-event agenda/minutes/video URLs
    # in this schema. Per AGENTS.md Case A (guard 0): emit "" rather than
    # fabricate values. Historical PDFs live behind a Nuxt SPA and want a
    # separate crawler (V2 track).
    urls = {
        "agenda_url": "",
        "minutes_url": "",
        "video_url": "",
        "agenda_packet_url": "",
        "ecomment_url": "",
    }
    status = _status_from_evidence(title, urls)

    return {
        "meeting_title": title,
        "meeting_date": meeting_date,
        "meeting_time": meeting_time,
        "meeting_location": meeting_location,
        "meeting_status": status,
        "agenda_url": urls["agenda_url"],
        "minutes_url": urls["minutes_url"],
        "video_url": urls["video_url"],
        "agenda_packet_url": urls["agenda_packet_url"],
        "ecomment_url": urls["ecomment_url"],
        "meeting_id": event_id,
    }


def _status_from_evidence(title: str, urls: dict[str, str]) -> str:
    if CANCELLED_RE.search(title[:300]):
        return "Cancelled"
    if urls["minutes_url"]:
        return "Minutes Available"
    if urls["agenda_url"] or urls["agenda_packet_url"]:
        return "Agenda Available"
    return "Scheduled"


def _extract_date(event: dict, event_id: str) -> str:
    for key in ("formatted_start", "start_at"):
        raw = str(event.get(key) or "")
        match = re.match(r"^(\d{4}-\d{2}-\d{2})\b", raw)
        if match:
            return match.group(1)

    year = str(event.get("year") or "").strip()
    month_name = str(event.get("month") or "").strip().upper()
    day = str(event.get("day") or "").strip().zfill(2)
    months = {
        "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04",
        "MAY": "05", "JUN": "06", "JUL": "07", "AUG": "08",
        "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
    }
    month_num = months.get(month_name, "")
    if re.fullmatch(r"\d{4}", year) and month_num and re.fullmatch(r"\d{2}", day):
        return f"{year}-{month_num}-{day}"

    logger.warning(
        "row %s has no parseable meeting_date: start_at=%r formatted_start=%r",
        event_id,
        event.get("start_at"),
        event.get("formatted_start"),
    )
    return ""


def _extract_time(event: dict) -> str:
    if event.get("all_day") is True:
        logger.warning("Parker all-day council event has no meeting_time: id=%r", event.get("id"))
        return ""

    raw = str(event.get("formatted_start") or event.get("start_at") or "")
    match = re.search(r"[T ](\d{1,2}):(\d{2})", raw)
    if match:
        return _format_time(int(match.group(1)), int(match.group(2)))

    raw_time = str(event.get("time") or "").strip()
    match = re.match(r"(\d{1,2}):(\d{2})\s*([AP]M)", raw_time, re.IGNORECASE)
    if match:
        hour = int(match.group(1)) % 12
        if match.group(3).upper() == "PM":
            hour += 12
        return _format_time(hour, int(match.group(2)))

    logger.warning(
        "Parker meeting_time left empty after all source-row signals: id=%r formatted_start=%r time=%r",
        event.get("id"),
        event.get("formatted_start"),
        event.get("time"),
    )
    return ""


def _extract_location(event: dict) -> str:
    venue = _clean_text(str(event.get("venue") or ""))
    if venue:
        return venue
    address = _clean_text(str(event.get("address") or ""))
    if address:
        return address
    logger.warning("Parker meeting_location absent in source row: id=%r", event.get("id"))
    return ""


def _format_time(hour_24: int, minute: int) -> str:
    suffix = "AM" if hour_24 < 12 else "PM"
    hour_12 = hour_24 % 12 or 12
    return f"{hour_12}:{minute:02d} {suffix}"


def _fetch_text(session, url: str, *, accept: str) -> tuple[int, str, str]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or _hostname(url) not in ALLOWED_HOSTS:
        raise ValueError(f"Parker request URL is not allowlisted: {url!r}")
    with session.get(
        url,
        headers={"Accept": accept},
        timeout=(10, 30),
        stream=True,
        allow_redirects=True,
    ) as response:
        final_host = _hostname(response.url)
        if final_host not in ALLOWED_HOSTS:
            raise ValueError(
                f"Redirect to disallowed host: {final_host} (started from {url})"
            )
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(
                    f"Response from {url} exceeded {MAX_RESPONSE_BYTES} bytes"
                )
        return (
            response.status_code,
            bytes(body).decode(response.encoding or "utf-8", errors="replace"),
            response.url,
        )


def _hostname(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", unescape(value)).strip()


def _validate_schema(row: dict[str, str]) -> None:
    keys = tuple(row.keys())
    if keys != CANONICAL_FIELDS:
        raise ValueError(f"Unexpected Parker parser schema: {keys}")
    bad_fields = [key for key, value in row.items() if not isinstance(value, str)]
    if bad_fields:
        raise TypeError(f"Parker parser emitted non-string fields: {bad_fields}")


if __name__ == "__main__":
    parsed_meetings = scrape_calendar()
    print(f"Found {len(parsed_meetings)} meetings.")
    for parsed_meeting in parsed_meetings[:20]:
        print(parsed_meeting)
