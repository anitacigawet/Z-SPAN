"""Quartzsite, Arizona Revize calendar parser candidate."""

from __future__ import annotations

import calendar as calendar_lib
import html
import json
import logging
import re
from collections import Counter
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote, urlencode, urljoin, urlparse

from polite_http import make_session


logger = logging.getLogger(__name__)

DEFAULT_CALENDAR_URL = "https://www.ci.quartzsite.az.us/calendar.php"
TARGET_CALENDAR_NAME = "Town Council"
REVIZE_ENDPOINT_PATH = "/_assets_/plugins/revizeCalendar/calendar_data_handler.php"
MAX_PAGE_BYTES = 2_000_000
MAX_ASSET_BYTES = 500_000
MAX_JSON_BYTES = 5_000_000
EXPANSION_DAYS_AHEAD = 365
BLOCKED_STATUSES = {401, 403, 429}

# The Revize event source legitimately emits document/page links from cms8.revize.com.
ALLOWED_EMIT_HOSTS = {"ci.quartzsite.az.us", "www.ci.quartzsite.az.us", "cms8.revize.com"}
# Revize serves runtime plugin assets from its CDN; this is fetch-only, not an output URL host.
ALLOWED_REQUEST_HOSTS = ALLOWED_EMIT_HOSTS | {"cdn1-global.revize.com"}
BAD_URL_SCHEMES = ("javascript:", "data:", "vbscript:", "file:", "mailto:", "ftp:", "gopher:")
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
COMPACT_DATETIME_RE = re.compile(r"^\d{8}(?:T\d{6})?$")
ISO_START_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
WEEKDAY_CODES = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
SCHEMA_FIELDS = (
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


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return " ".join(" ".join(self.parts).split())


class SourceBlockedError(RuntimeError):
    """The official source explicitly blocked the neutral paced request."""


def scrape_calendar(
    url: str = DEFAULT_CALENDAR_URL,
    *,
    today: date | None = None,
) -> list[dict[str, str]]:
    try:
        return _scrape_calendar(url, today=today)
    except SourceBlockedError as exc:
        logger.warning("%s", exc)
        logger.warning("health_empty_kind=source_blocked")
        return []


def _scrape_calendar(url: str, *, today: date | None) -> list[dict[str, str]]:
    """Scrape upcoming Town Council meetings from Quartzsite's Revize calendar."""
    with make_session() as session:
        page_html = _fetch_text_bounded(session, url, MAX_PAGE_BYTES)
        _validate_revize_fingerprint(page_html, url)
        _validate_revize_index_js(url, session)

        target_calendar_id = _extract_calendar_id(page_html, TARGET_CALENDAR_NAME)
        webspace = _extract_required_js_value(page_html, r"RZ\.webspace\s*=\s*'([^']+)'", "RZ.webspace")
        revize_base = _extract_required_js_value(
            page_html,
            r"RZ\.protocolRelativeRevizeBaseUrl\s*=\s*'([^']+)'",
            "RZ.protocolRelativeRevizeBaseUrl",
        )
        endpoint_url = _build_endpoint_url(url, webspace, revize_base)
        logger.info(
            "revize calendar fingerprint confirmed: target_calendar_name=%r target_calendar_id=%s "
            "webspace=%s endpoint=%s",
            TARGET_CALENDAR_NAME,
            target_calendar_id,
            webspace,
            endpoint_url,
        )

        data = json.loads(_fetch_text_bounded(session, endpoint_url, MAX_JSON_BYTES))
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise ValueError("Revize calendar handler did not return a JSON array of objects")
    observed_keys = sorted({key for item in data for key in item.keys()})
    logger.info(
        "revize calendar data shape: json_array_count=%d observed_keys=%s",
        len(data),
        observed_keys,
    )

    month_floor = (today or date.today()).replace(day=1)
    window_start = datetime.combine(month_floor, time.min)
    window_end = window_start + timedelta(days=EXPANSION_DAYS_AHEAD)
    logger.info(
        "date window selected: start=%s end=%s reason=upcoming occurrences over %d days",
        window_start.date().isoformat(),
        window_end.date().isoformat(),
        EXPANSION_DAYS_AHEAD,
    )
    logger.warning(
        "source exposes no dedicated minutes_url/video_url/ecomment_url fields; those fields "
        "are emitted as empty strings unless a future source shape adds row-level evidence"
    )

    counters: Counter[str] = Counter()
    rejected_url_samples: list[str] = []
    unclassified_url_samples: list[str] = []
    meetings: list[dict[str, str]] = []

    for event in data:
        displays = {str(value) for value in event.get("calendar_displays", [])}
        if target_calendar_id not in displays:
            counters["non_target_events"] += 1
            continue

        counters["target_event_definitions"] += 1
        occurrences = _expand_occurrences(event, window_start, window_end, counters)
        if not occurrences:
            counters["target_definitions_with_no_occurrences_in_window"] += 1
            continue

        for occurrence_start in occurrences:
            meeting = _build_meeting(
                event,
                occurrence_start,
                target_calendar_id,
                url,
                counters,
                rejected_url_samples,
                unclassified_url_samples,
            )
            meetings.append(meeting)

    meetings.sort(key=lambda row: (row["meeting_date"], row["meeting_time"], row["meeting_id"]))
    if unclassified_url_samples:
        logger.warning(
            "dropped %d Revize event URLs because they did not classify as agenda/packet evidence; "
            "samples=%s",
            counters["unclassified_event_urls"],
            unclassified_url_samples[:10],
        )
    if rejected_url_samples:
        logger.warning(
            "rejected %d Revize event URLs during URL hygiene; samples=%s",
            counters["rejected_urls"],
            rejected_url_samples[:10],
        )
    if counters["target_event_definitions"] and not meetings:
        logger.warning(
            "Town Council calendar had %d source definitions but 0 occurrences in %s..%s",
            counters["target_event_definitions"],
            window_start.date().isoformat(),
            window_end.date().isoformat(),
        )
    logger.info("quartzsite scrape summary: %s emitted_meetings=%d", dict(counters), len(meetings))
    if not counters["target_event_definitions"]:
        raise ValueError("Quartzsite Town Council calendar id was witnessed but its JSON feed exposed no target definitions")
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
    return meetings


def _fetch_text_bounded(session, url: str, max_bytes: int) -> str:
    _validate_allowed_host(url, "request")
    with session.get(
        url,
        headers={"Accept": "application/json,text/javascript,text/html,*/*"},
        timeout=(10, 30),
        stream=True,
        allow_redirects=True,
    ) as response:
        _validate_allowed_host(response.url, "redirect")
        if response.status_code in BLOCKED_STATUSES:
            raise SourceBlockedError(
                f"Quartzsite official source blocked the neutral paced request: status={response.status_code} url={response.url}"
            )
        if response.status_code != 200:
            raise RuntimeError(f"Quartzsite source returned HTTP {response.status_code}: {response.url}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ValueError(f"Response from {url} exceeded {max_bytes} bytes")
        return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _validate_allowed_host(url: str, context: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in ALLOWED_REQUEST_HOSTS:
        raise ValueError(f"Disallowed {context} URL host: {url}")


def _validate_revize_fingerprint(page_html: str, url: str) -> None:
    required = [
        "/revize/plugins/revize_calendar/index.js",
        "/revize/plugins/revize_calendar/core/main.min.js",
        'id="calendar"',
        "calendarProps",
    ]
    missing = [needle for needle in required if needle not in page_html]
    if missing:
        raise ValueError(f"Page at {url} does not match Revize calendar fingerprint; missing={missing}")


def _validate_revize_index_js(page_url: str, session) -> None:
    index_url = urljoin(page_url, "/revize/plugins/revize_calendar/index.js")
    index_js = _fetch_text_bounded(session, index_url, MAX_ASSET_BYTES)
    required = ["calendar_data_handler.php", "jsonEvents = data", "eventSources"]
    missing = [needle for needle in required if needle not in index_js]
    if missing:
        raise ValueError(f"Revize index.js did not expose expected data handler shape; missing={missing}")
    logger.info("revize index.js fingerprint confirmed: index_url=%s handler=calendar_data_handler.php", index_url)


def _extract_required_js_value(page_html: str, pattern: str, label: str) -> str:
    match = re.search(pattern, page_html)
    if not match:
        raise ValueError(f"Could not derive {label} from live page")
    return match.group(1)


def _extract_calendar_id(page_html: str, calendar_name: str) -> str:
    for match in re.finditer(r"<a\b[^>]*\bdata-calendar-id=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>", page_html, re.I | re.S):
        candidate_id = match.group(1).strip()
        candidate_name = _clean_text(match.group(2))
        if candidate_name.casefold() == calendar_name.casefold():
            return candidate_id
    props_match = re.search(
        r"'([^']+)'\s*:\s*\{\s*'color'\s*:\s*'[^']*'\s*,\s*'name'\s*:\s*'"
        + re.escape(calendar_name)
        + r"'",
        page_html,
    )
    if props_match:
        return props_match.group(1)
    raise ValueError(f"Could not derive calendar id for {calendar_name!r} from live page")


def _build_endpoint_url(page_url: str, webspace: str, revize_base: str) -> str:
    base = urljoin(page_url, REVIZE_ENDPOINT_PATH)
    query = urlencode(
        {
            "webspace": webspace,
            "relative_revize_url": revize_base,
            "protocol": f"{urlparse(page_url).scheme}:",
        }
    )
    return f"{base}?{query}"


def _expand_occurrences(
    event: dict[str, Any],
    window_start: datetime,
    window_end: datetime,
    counters: Counter[str],
) -> list[datetime]:
    start_raw = str(event.get("start", ""))
    start = _parse_datetime(start_raw)
    rrule_raw = str(event.get("rrule", ""))
    if not rrule_raw:
        counters["one_time_definitions"] += 1
        if window_start <= start <= window_end:
            return [start]
        counters["one_time_definitions_outside_window"] += 1
        return []

    counters["recurring_definitions"] += 1
    lines = _parse_rrule_block(rrule_raw)
    dtstart = _parse_datetime(lines.get("DTSTART", [start_raw])[0])
    rdates = {_parse_datetime(value) for value in lines.get("RDATE", [])}
    exdates = {_parse_datetime(value) for value in lines.get("EXDATE", [])}
    rule_values = lines.get("RRULE", [])
    if len(rule_values) != 1:
        logger.warning(
            "unsupported recurrence rule count for event id=%s title=%r rrule=%r",
            event.get("id", ""),
            event.get("title", ""),
            rrule_raw,
        )
        counters["unsupported_rrule_definitions"] += 1
        return [start] if window_start <= start <= window_end else []

    rule = _parse_rule_parts(rule_values[0])
    if rule.get("FREQ") != "MONTHLY" or "BYDAY" not in rule or "BYSETPOS" not in rule:
        logger.warning(
            "unsupported recurrence shape for event id=%s title=%r rule=%s",
            event.get("id", ""),
            event.get("title", ""),
            rule,
        )
        counters["unsupported_rrule_definitions"] += 1
        return [start] if window_start <= start <= window_end else []

    until = _parse_datetime(rule["UNTIL"]) if "UNTIL" in rule else window_end
    expansion_end = min(window_end, until)
    if "UNTIL" not in rule and "COUNT" not in rule:
        counters["unbounded_recurring_definitions_expanded_with_window_cap"] += 1
        logger.info(
            "unbounded Revize recurrence expanded with parser window cap: event_id=%s title=%r "
            "through=%s",
            event.get("id", ""),
            event.get("title", ""),
            expansion_end.date().isoformat(),
        )

    occurrences: set[datetime] = set(rdates)
    interval = int(rule.get("INTERVAL", "1"))
    bysetpos = int(rule["BYSETPOS"])
    byday = rule["BYDAY"]
    if "," in byday:
        logger.warning(
            "unsupported multi-BYDAY recurrence for event id=%s title=%r byday=%s",
            event.get("id", ""),
            event.get("title", ""),
            byday,
        )
        counters["unsupported_rrule_definitions"] += 1
        return []
    weekday = WEEKDAY_CODES.get(byday)
    if weekday is None:
        logger.warning(
            "unknown BYDAY recurrence token for event id=%s title=%r byday=%s",
            event.get("id", ""),
            event.get("title", ""),
            byday,
        )
        counters["unsupported_rrule_definitions"] += 1
        return []

    year = dtstart.year
    month = dtstart.month
    month_index = 0
    while datetime(year, month, 1) <= expansion_end:
        if month_index % interval == 0:
            day = _nth_weekday_of_month(year, month, weekday, bysetpos)
            if day is not None:
                candidate = datetime(year, month, day, dtstart.hour, dtstart.minute, dtstart.second)
                if candidate >= dtstart:
                    occurrences.add(candidate)
        month += 1
        if month == 13:
            month = 1
            year += 1
        month_index += 1

    filtered = sorted(
        occurrence
        for occurrence in occurrences
        if window_start <= occurrence <= expansion_end and occurrence not in exdates
    )
    counters["recurring_occurrences_expanded"] += len(filtered)
    counters["recurrence_exdates_observed"] += len(exdates)
    return filtered


def _parse_rrule_block(rrule_raw: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for line in rrule_raw.splitlines():
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        key = key.upper()
        for value in raw_value.split(","):
            value = value.strip()
            if value:
                values.setdefault(key, []).append(value)
    return values


def _parse_rule_parts(rule_raw: str) -> dict[str, str]:
    parts: dict[str, str] = {}
    for chunk in rule_raw.split(";"):
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        parts[key.upper()] = value
    return parts


def _nth_weekday_of_month(year: int, month: int, weekday: int, position: int) -> int | None:
    days = [
        day
        for day in range(1, calendar_lib.monthrange(year, month)[1] + 1)
        if date(year, month, day).weekday() == weekday
    ]
    if position > 0 and len(days) >= position:
        return days[position - 1]
    if position < 0 and len(days) >= abs(position):
        return days[position]
    return None


def _parse_datetime(value: str) -> datetime:
    cleaned = value.strip().rstrip("Z")
    if ISO_START_RE.match(cleaned):
        return datetime.fromisoformat(cleaned[:19])
    if COMPACT_DATETIME_RE.match(cleaned):
        if "T" in cleaned:
            return datetime.strptime(cleaned, "%Y%m%dT%H%M%S")
        return datetime.strptime(cleaned, "%Y%m%d")
    raise ValueError(f"Unsupported Revize datetime value: {value!r}")


def _build_meeting(
    event: dict[str, Any],
    occurrence_start: datetime,
    calendar_id: str,
    page_url: str,
    counters: Counter[str],
    rejected_url_samples: list[str],
    unclassified_url_samples: list[str],
) -> dict[str, str]:
    title = _clean_text(str(event.get("title", "")))
    description = _clean_text(str(event.get("desc", "")))
    location = _clean_text(str(event.get("location", "")))
    agenda_url, agenda_packet_url = _classify_event_url(
        event,
        title,
        description,
        page_url,
        counters,
        rejected_url_samples,
        unclassified_url_samples,
    )
    status = _derive_status(title, agenda_url, agenda_packet_url)
    meeting = {
        "meeting_title": title,
        "meeting_date": occurrence_start.date().isoformat(),
        "meeting_time": "" if event.get("allDay") is True else _format_time(occurrence_start),
        "meeting_location": location,
        "meeting_status": status,
        "agenda_url": agenda_url,
        "minutes_url": "",
        "video_url": "",
        "agenda_packet_url": agenda_packet_url,
        "ecomment_url": "",
        "meeting_id": f"{calendar_id}:{event.get('rid') or event.get('id') or ''}:{occurrence_start:%Y%m%dT%H%M%S}",
    }
    if set(meeting) != set(SCHEMA_FIELDS):
        raise ValueError(f"Internal schema error: keys={sorted(meeting)}")
    return meeting


def _classify_event_url(
    event: dict[str, Any],
    title: str,
    description: str,
    page_url: str,
    counters: Counter[str],
    rejected_url_samples: list[str],
    unclassified_url_samples: list[str],
) -> tuple[str, str]:
    raw_url = str(event.get("url", "")).strip()
    if not raw_url:
        counters["events_without_url"] += 1
        return "", ""
    cleaned_url = _emit_url(raw_url, page_url, event, counters, rejected_url_samples)
    if not cleaned_url:
        return "", ""

    combined = " ".join([title, description, raw_url]).casefold()
    if "packet" in combined:
        counters["agenda_packet_urls_emitted"] += 1
        return "", cleaned_url
    if "agenda" in combined:
        counters["agenda_urls_emitted"] += 1
        return cleaned_url, ""

    counters["unclassified_event_urls"] += 1
    if len(unclassified_url_samples) < 10:
        unclassified_url_samples.append(
            f"id={event.get('id', '')} title={title!r} raw_url={raw_url!r}"
        )
    return "", ""


def _emit_url(
    raw_url: str,
    page_url: str,
    event: dict[str, Any],
    counters: Counter[str],
    rejected_url_samples: list[str],
) -> str:
    lowered = raw_url.lower().lstrip()
    if lowered == "#" or lowered.startswith(BAD_URL_SCHEMES):
        counters["rejected_urls"] += 1
        if len(rejected_url_samples) < 10:
            rejected_url_samples.append(
                f"id={event.get('id', '')} field=url reason=bad_scheme_or_placeholder raw={raw_url!r}"
            )
        return ""

    absolute = urljoin(page_url, raw_url)
    absolute = _normalize_revize_public_url(absolute, page_url, counters)
    parsed = urlparse(absolute)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in ALLOWED_EMIT_HOSTS:
        counters["rejected_urls"] += 1
        if len(rejected_url_samples) < 10:
            rejected_url_samples.append(
                f"id={event.get('id', '')} field=url reason=disallowed_host raw={raw_url!r} absolute={absolute!r}"
            )
        return ""
    return absolute


def _normalize_revize_public_url(absolute: str, page_url: str, counters: Counter[str]) -> str:
    parsed = urlparse(absolute)
    if parsed.hostname == "cms8.revize.com" and parsed.path.startswith("/revize/quartzsite/"):
        counters["cms_authoring_urls_normalized_to_public_host"] += 1
        public_path = parsed.path.removeprefix("/revize/quartzsite/")
        public_url = urljoin(page_url, public_path)
        if parsed.query:
            public_url = f"{public_url}?{parsed.query}"
        return public_url
    return absolute


def _derive_status(title: str, agenda_url: str, agenda_packet_url: str) -> str:
    if CANCELLED_RE.search(title[:300]):
        return "Cancelled"
    if agenda_url or agenda_packet_url:
        return "Agenda Available"
    return "Scheduled"


def _format_time(value: datetime) -> str:
    hour = value.hour
    minute = value.minute
    suffix = "AM" if hour < 12 else "PM"
    display_hour = hour % 12 or 12
    return f"{display_hour}:{minute:02d} {suffix}"


def _clean_text(value: str) -> str:
    decoded = html.unescape(unquote(value))
    parser = _TextExtractor()
    parser.feed(decoded)
    parser.close()
    stripped = parser.text() or decoded
    return " ".join(html.unescape(stripped).split())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    rows = scrape_calendar(DEFAULT_CALENDAR_URL)
    print(f"Found {len(rows)} meetings.")
    for row in rows[:10]:
        print(row)
