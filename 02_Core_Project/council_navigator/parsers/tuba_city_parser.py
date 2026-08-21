"""Tuba City chapter calendar parser.

This parser resolves the public Google Calendar iCal feed from the chapter
website when production passes the HTML page URL, while still accepting a
direct iCal URL for local validation.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from html import unescape
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bs4 import BeautifulSoup
from icalendar import Calendar
from requests.exceptions import RequestException

from polite_http import make_session


logger = logging.getLogger(__name__)

GOOGLE_ICAL_BASE = "https://calendar.google.com/calendar/ical"
SOURCE_HOST = "tonaneesdizi.navajochapters.org"
MAX_HTML_BYTES = 2_000_000
MAX_ICS_BYTES = 1_000_000

CALENDAR_ID_RE = re.compile(r"^[A-Za-z0-9@._-]+$")
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
CHAPTER_MEETING_RE = re.compile(
    r"^(?:TNDLG\s+)?(?:Regular|Special) Chapter Meeting$",
    re.IGNORECASE,
)
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


class SourceBlocked(RuntimeError):
    """The official source could not be read through the polite transport."""


def scrape_calendar(url: str) -> list[dict]:
    """Scrape Tuba City's Google Calendar feed into canonical meeting rows."""
    logger.info(
        "Tuba City iCal surface exposes no agenda/minutes/video/packet/ecomment URLs; "
        "document URL fields will be empty for every emitted row."
    )

    try:
        with make_session() as session:
            ics_url = _resolve_ics_url(session, url)
            ics_text = _fetch_text(
                session,
                ics_url,
                MAX_ICS_BYTES,
                allowed_hosts={"calendar.google.com"},
            )
    except SourceBlocked as exc:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Tuba City official chapter calendar was blocked: failure_shape=honest-empty "
            "missing_scope=current_month_forward_regular_or_special_chapter_meetings "
            "error=%s",
            exc,
        )
        return []

    try:
        calendar = Calendar.from_ical(ics_text)
    except ValueError as exc:
        logger.warning(
            "Failed to parse Tuba City iCal feed url=%r content_type_shape=%r error=%s; "
            "returning [].",
            ics_url,
            _content_shape(ics_text),
            exc,
        )
        raise ValueError(f"Tuba City iCal structure could not be parsed: {exc}") from exc

    _validate_calendar_fingerprint(calendar)

    calendar_timezone = _calendar_timezone(calendar)
    month_floor = date.today().replace(day=1)
    meetings: list[dict] = []
    total_events = 0
    all_day_events = 0
    timed_events = 0
    missing_uid = 0
    cancelled_events = 0
    historical_events = 0
    non_chapter_events = 0
    ambiguous_chapter_titles: list[str] = []

    for component in calendar.walk():
        if component.name != "VEVENT":
            continue

        total_events += 1
        title = _clean_text(str(component.get("summary") or ""))
        location = _clean_text(str(component.get("location") or ""))
        meeting_id = _clean_text(str(component.get("uid") or ""))
        if not meeting_id:
            missing_uid += 1

        dtstart_value = component.get("dtstart")
        if not dtstart_value:
            logger.warning("Dropping VEVENT with title=%r because DTSTART is absent.", title)
            continue

        dtstart = dtstart_value.dt
        if isinstance(dtstart, datetime):
            local_dtstart = _localize_datetime(dtstart, calendar_timezone)
            meeting_date = local_dtstart.date().isoformat()
            meeting_time = _format_time(local_dtstart)
            timed_events += 1
        elif isinstance(dtstart, date):
            meeting_date = dtstart.isoformat()
            meeting_time = ""
            all_day_events += 1
        else:
            logger.warning(
                "Dropping VEVENT with title=%r because DTSTART has unsupported type=%s value=%r.",
                title,
                type(dtstart).__name__,
                dtstart,
            )
            continue

        if date.fromisoformat(meeting_date) < month_floor:
            historical_events += 1
            logger.warning(
                "Tuba City VEVENT dropped before current-calendar-month floor: "
                "meeting_date=%s floor=%s title=%r uid=%r",
                meeting_date,
                month_floor.isoformat(),
                title,
                meeting_id,
            )
            continue

        if not CHAPTER_MEETING_RE.fullmatch(title):
            non_chapter_events += 1
            if "chapter meeting" in title.casefold():
                ambiguous_chapter_titles.append(title)
                logger.warning(
                    "Tuba City VEVENT dropped for ambiguous flagship-body title: "
                    "title=%r uid=%r",
                    title,
                    meeting_id,
                )
            else:
                logger.warning(
                    "Tuba City VEVENT dropped as non-flagship event: title=%r uid=%r",
                    title,
                    meeting_id,
                )
            continue

        meeting_status = "Cancelled" if CANCELLED_RE.search(title) else "Scheduled"
        if meeting_status == "Cancelled":
            cancelled_events += 1

        meetings.append(
            {
                "meeting_title": title,
                "meeting_date": meeting_date,
                "meeting_time": meeting_time,
                "meeting_location": location,
                "meeting_status": meeting_status,
                "agenda_url": "",
                "minutes_url": "",
                "video_url": "",
                "agenda_packet_url": "",
                "ecomment_url": "",
                "meeting_id": meeting_id,
            }
        )

    meetings.sort(key=_meeting_sort_key)
    if ambiguous_chapter_titles:
        raise ValueError(
            "Tuba City calendar exposed unrecognized Chapter Meeting title vocabulary: "
            f"{sorted(set(ambiguous_chapter_titles))!r}"
        )
    dropped_events = total_events - len(meetings)
    logger.info(
        "Tuba City iCal parse summary: total_vevents=%d emitted=%d dropped=%d "
        "all_day_without_time=%d timed=%d cancelled_by_title=%d missing_uid=%d "
        "historical=%d non_flagship=%d drop_policy=current_month_forward_exact_chapter_meetings",
        total_events,
        len(meetings),
        dropped_events,
        all_day_events,
        timed_events,
        cancelled_events,
        missing_uid,
        historical_events,
        non_chapter_events,
    )
    _validate_schema(meetings)
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Tuba City official Google calendar was accessible but exposed no exact "
            "Regular or Special Chapter Meeting from %s forward",
            month_floor.isoformat(),
        )
    return meetings


def _resolve_ics_url(session: object, url: str) -> str:
    candidate = (url or "").strip()
    if _looks_like_ics_url(candidate):
        if _host(candidate) != "calendar.google.com" or urlparse(candidate).scheme != "https":
            raise ValueError(f"Tuba City direct iCal URL is not approved: {candidate!r}")
        logger.info("Using direct iCal URL path for Tuba City: url=%r.", candidate)
        return candidate

    if urlparse(candidate).scheme != "https" or _host(candidate) != SOURCE_HOST:
        raise ValueError(f"Tuba City source page is not the approved HTTPS host: {candidate!r}")
    logger.info("Using HTML discovery path for Tuba City: page_url=%r.", candidate)
    html = _fetch_text(session, candidate, MAX_HTML_BYTES, allowed_hosts={SOURCE_HOST})
    calendar_id = _discover_calendar_id(html) if html else ""
    if calendar_id:
        logger.info("Discovered Google Calendar ID from Tuba City page markup.")
        return _build_ics_url(calendar_id)

    raise ValueError(
        f"Tuba City source page no longer exposes a valid public Google Calendar ID: {candidate!r}"
    )


def _validate_calendar_fingerprint(calendar: Calendar) -> None:
    product_id = str(calendar.get("PRODID") or "")
    calendar_name = str(calendar.get("X-WR-CALNAME") or "")
    if "Google Calendar" not in product_id or "Tuba City Chapter" not in calendar_name:
        raise ValueError(
            "Tuba City iCal fingerprint changed: "
            f"PRODID={product_id!r} X-WR-CALNAME={calendar_name!r}"
        )
    logger.info(
        "Tuba City iCal fingerprint matched product_id=%r calendar_name=%r",
        product_id,
        calendar_name,
    )


def _looks_like_ics_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    return "/ical/" in path or path.endswith(".ics") or path.endswith("/basic.ics")


def _discover_calendar_id(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["iframe", "a"]):
        raw_src = tag.get("src") or tag.get("href") or ""
        src = unescape(raw_src)
        parsed = urlparse(src)
        if parsed.netloc.lower() != "calendar.google.com":
            continue
        if parsed.path.rstrip("/") != "/calendar/embed":
            continue
        calendar_id = parse_qs(parsed.query).get("src", [""])[0]
        calendar_id = unquote(calendar_id).strip()
        if _valid_calendar_id(calendar_id):
            return calendar_id
        logger.warning("Rejected discovered Google Calendar ID candidate=%r as invalid.", calendar_id)
    return ""


def _valid_calendar_id(calendar_id: str) -> bool:
    return bool(
        calendar_id
        and CALENDAR_ID_RE.fullmatch(calendar_id)
        and calendar_id.endswith("@group.calendar.google.com")
    )


def _build_ics_url(calendar_id: str) -> str:
    if not _valid_calendar_id(calendar_id):
        raise ValueError(f"Refusing to build iCal URL from invalid calendar_id={calendar_id!r}")
    return f"{GOOGLE_ICAL_BASE}/{calendar_id}/public/basic.ics"


def _calendar_timezone(calendar: Calendar) -> ZoneInfo | None:
    timezone_name = str(calendar.get("X-WR-TIMEZONE") or "").strip()
    if not timezone_name:
        logger.info("Tuba City iCal feed did not declare X-WR-TIMEZONE; timed DTSTART values used as-is.")
        return None
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        logger.warning(
            "Tuba City iCal feed declared unsupported X-WR-TIMEZONE=%r; timed DTSTART values used as-is.",
            timezone_name,
        )
        return None
    logger.info("Using Tuba City iCal timezone %s for aware DTSTART date/time emission.", timezone_name)
    return timezone


def _localize_datetime(value: datetime, timezone: ZoneInfo | None) -> datetime:
    if timezone is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone)


def _fetch_text(
    session: object,
    url: str,
    max_bytes: int,
    allowed_hosts: set[str],
) -> str:
    try:
        with session.get(url, timeout=30, stream=True, allow_redirects=True) as response:
            response.raise_for_status()
            final_host = _host(response.url)
            if final_host not in allowed_hosts:
                raise ValueError(
                    f"Tuba City fetch redirected to disallowed host: {final_host!r}; "
                    f"allowed={sorted(allowed_hosts)!r}"
                )

            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise SourceBlocked(f"Response from {url} exceeded {max_bytes} bytes")
                chunks.append(chunk)

            encoding = response.encoding or "utf-8"
            return b"".join(chunks).decode(encoding, errors="replace")
    except RequestException as exc:
        raise SourceBlocked(f"Network failure fetching {url!r}: {exc}") from exc


def _host(url: str) -> str:
    return (urlparse(url).netloc.split(":")[0] or "").lower()


def _format_time(value: datetime) -> str:
    return value.strftime("%I:%M %p").lstrip("0")


def _meeting_sort_key(meeting: dict) -> tuple[str, int, str, str]:
    return (
        meeting["meeting_date"],
        _time_sort_value(meeting["meeting_time"]),
        meeting["meeting_title"].casefold(),
        meeting["meeting_id"],
    )


def _time_sort_value(value: str) -> int:
    if not value:
        return -1
    return int(datetime.strptime(value, "%I:%M %p").strftime("%H%M"))


def _clean_text(value: str) -> str:
    return BeautifulSoup(unescape(value), "html.parser").get_text(" ", strip=True)


def _content_shape(text: str) -> str:
    prefix = text[:80].replace("\n", "\\n").replace("\r", "\\r")
    return prefix


def _validate_schema(meetings: list[dict]) -> None:
    expected = set(CANONICAL_FIELDS)
    for index, meeting in enumerate(meetings, start=1):
        keys = set(meeting)
        if keys != expected:
            raise ValueError(f"Meeting {index} has invalid schema keys={sorted(keys)}")
        for field, value in meeting.items():
            if not isinstance(value, str):
                raise TypeError(f"Meeting {index} field {field} is {type(value).__name__}, not str")
