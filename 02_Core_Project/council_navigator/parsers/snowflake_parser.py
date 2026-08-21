from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from polite_http import make_session


logger = logging.getLogger(__name__)

CALENDAR_URL = "https://www.snowflakeaz.gov/events/category/council-meetings/"
API_URL = "https://www.snowflakeaz.gov/wp-json/tribe/events/v1/events"
ALLOWED_HOSTS = {"www.snowflakeaz.gov", "snowflakeaz.gov"}
BLOCKED_STATUSES = {401, 403, 407, 423, 429, 451}
MAX_RESPONSE_BYTES = 5_000_000
PER_PAGE = 50
MAX_PAGES = 4
MAX_EVENTS = PER_PAGE * MAX_PAGES
FUTURE_MONTHS = 13
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
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)


class LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []
        self._current_link: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attr_map = {name.lower(): value or "" for name, value in attrs}
        self._current_link = {"href": attr_map.get("href", ""), "text": ""}
        self.links.append(self._current_link)

    def handle_data(self, data: str) -> None:
        if self._current_link is not None:
            self._current_link["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a":
            self._current_link = None


def scrape_calendar(
    url: str = CALENDAR_URL,
    *,
    today: date | None = None,
) -> list[dict[str, str]]:
    """Scrape Snowflake council meetings from The Events Calendar REST API."""
    category_slug = _category_slug_from_url(url) or "council-meetings"
    if category_slug != "council-meetings":
        raise ValueError(f"Snowflake parser requires the official council category: {url!r}")
    month_floor = (today or date.today()).replace(day=1)
    events, source_blocked = _fetch_rest_events(category_slug, month_floor)
    if source_blocked:
        logger.warning("health_empty_kind=source_blocked")
        return []
    meetings: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    council_signal_count = 0

    for event in events:
        if not _event_has_category(event, category_slug):
            logger.warning(
                "dropped event id=%r title=%r: missing expected category %r",
                event.get("id"),
                event.get("title"),
                category_slug,
            )
            continue

        title = _clean_text(str(event.get("title") or ""))
        if not re.search(r"\btown council\b", title, re.IGNORECASE):
            logger.warning(
                "dropped current category event id=%r title=%r: missing explicit Town Council body signal",
                event.get("id"),
                title,
            )
            continue
        council_signal_count += 1

        meeting = _build_meeting(event)
        if not meeting["meeting_date"]:
            raise ValueError(f"Snowflake Town Council event lacks a valid meeting date: id={meeting['meeting_id']!r}")
        if date.fromisoformat(meeting["meeting_date"]) < month_floor:
            logger.warning(
                "dropped event before current-month floor id=%s date=%s floor=%s title=%r",
                meeting["meeting_id"],
                meeting["meeting_date"],
                month_floor.isoformat(),
                meeting["meeting_title"],
            )
            continue
        meeting_id = meeting["meeting_id"]
        if meeting_id and meeting_id in seen_ids:
            logger.warning("dropped duplicate event id=%s title=%r", meeting_id, meeting["meeting_title"])
            continue
        if meeting_id:
            seen_ids.add(meeting_id)

        _validate_schema(meeting)
        meetings.append(meeting)
        logger.info(
            "emitted event id=%s date=%s title=%r status=%s agenda=%r packet=%r minutes=%r video=%r",
            meeting["meeting_id"],
            meeting["meeting_date"],
            meeting["meeting_title"],
            meeting["meeting_status"],
            meeting["agenda_url"],
            meeting["agenda_packet_url"],
            meeting["minutes_url"],
            meeting["video_url"],
        )

    meetings.sort(key=lambda row: (row["meeting_date"], row["meeting_time"], row["meeting_id"]))
    logger.info(
        "snowflake_parser emitted %d meetings from %d bounded current REST events for category=%s floor=%s",
        len(meetings),
        len(events),
        category_slug,
        month_floor.isoformat(),
    )
    if events and not council_signal_count:
        raise ValueError(
            "Snowflake official council category exposed current events but none carried the witnessed Town Council title signal"
        )
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
    return meetings


def _fetch_rest_events(category_slug: str, month_floor: date) -> tuple[list[dict], bool]:
    events: list[dict] = []
    end_date = _month_start_offset(month_floor, FUTURE_MONTHS) - timedelta(days=1)
    with make_session() as session:
        for page in range(1, MAX_PAGES + 1):
            params = {
                "categories": category_slug,
                "start_date": month_floor.isoformat(),
                "end_date": end_date.isoformat(),
                "per_page": str(PER_PAGE),
                "page": str(page),
            }
            api_page_url = f"{API_URL}?{urlencode(params)}"
            status, text, final_url = _fetch_text(
                session,
                api_page_url,
                accept="application/json",
            )
            if status in BLOCKED_STATUSES:
                logger.warning(
                    "Snowflake official calendar blocked the neutral paced request: status=%d url=%s page=%d",
                    status,
                    final_url,
                    page,
                )
                return [], True
            if status != 200:
                raise RuntimeError(f"Snowflake calendar returned HTTP {status}: {final_url}")
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Unexpected Tribe Events API JSON on page {page}") from exc
            if not isinstance(data, dict):
                raise ValueError(f"Unexpected Tribe Events API payload on page {page}: {type(data).__name__}")

            page_events = data.get("events", [])
            if not isinstance(page_events, list):
                raise ValueError(f"Unexpected Tribe Events API events payload on page {page}")

            logger.info(
                "fetched bounded Tribe REST page=%d count=%d total=%r total_pages=%r window=%s..%s",
                page,
                len(page_events),
                data.get("total"),
                data.get("total_pages"),
                month_floor.isoformat(),
                end_date.isoformat(),
            )
            if any(not isinstance(event, dict) for event in page_events):
                raise ValueError(f"Snowflake Tribe Events API page {page} contains a non-object event")
            events.extend(page_events)
            if len(events) > MAX_EVENTS:
                raise ValueError(f"Snowflake REST event cap exceeded: {len(events)} > {MAX_EVENTS}")

            total_pages = _to_int(data.get("total_pages"))
            more_pages = bool(page_events) and (
                (total_pages is not None and page < total_pages)
                or (total_pages is None and bool(data.get("next_rest_url")))
            )
            if not more_pages:
                break
            if page == MAX_PAGES:
                raise ValueError(
                    f"Snowflake REST pagination exceeded the {MAX_PAGES}-request hard cap"
                )

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

    urls = {
        "agenda_url": "",
        "minutes_url": "",
        "video_url": "",
        "agenda_packet_url": "",
        "ecomment_url": "",
    }
    _assign_event_links(event, urls, event_id)
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


def _assign_event_links(event: dict, urls: dict[str, str], event_id: str) -> None:
    description = event.get("description") or ""
    if description:
        extractor = LinkExtractor()
        extractor.feed(str(description))
        for link in extractor.links:
            _assign_link(
                urls,
                href=link.get("href", ""),
                label=_clean_text(link.get("text", "")),
                base_url=str(event.get("url") or CALENDAR_URL),
                row_id=event_id,
            )

    website = str(event.get("website") or "").strip()
    if website:
        _assign_link(
            urls,
            href=website,
            label="event website",
            base_url=str(event.get("url") or CALENDAR_URL),
            row_id=event_id,
        )


def _assign_link(
    urls: dict[str, str],
    *,
    href: str,
    label: str,
    base_url: str,
    row_id: str,
) -> bool:
    field = _classify_link(href, label)
    if not field:
        logger.warning(
            "dropped unclassified link for row %s: label=%r href=%r",
            row_id,
            label,
            href,
        )
        return False

    emitted = _emit_url(href, base_url, field, row_id)
    if not emitted:
        return False
    if urls[field]:
        logger.warning(
            "dropped duplicate %s for row %s: kept=%r dropped=%r",
            field,
            row_id,
            urls[field],
            emitted,
        )
        return False

    urls[field] = emitted
    return True


def _classify_link(href: str, label: str) -> str:
    evidence = f"{label} {href}".lower()
    if "minutes" in evidence:
        return "minutes_url"
    if re.search(r"(^|[^a-z0-9])packet([^a-z0-9]|$)", evidence):
        return "agenda_packet_url"
    if "agenda" in evidence:
        return "agenda_url"
    if "video" in evidence or "media" in evidence:
        return "video_url"
    logger.warning("unclassified Snowflake event link: label=%r href=%r", label, href)
    return ""


def _status_from_evidence(title: str, urls: dict[str, str]) -> str:
    if CANCELLED_RE.search(title[:300]):
        return "Cancelled"
    if urls["minutes_url"]:
        return "Minutes Available"
    if urls["agenda_url"] or urls["agenda_packet_url"]:
        return "Agenda Available"
    return "Scheduled"


def _extract_date(event: dict, event_id: str) -> str:
    details = event.get("start_date_details")
    if isinstance(details, dict):
        year = str(details.get("year") or "")
        month = str(details.get("month") or "").zfill(2)
        day = str(details.get("day") or "").zfill(2)
        if re.fullmatch(r"\d{4}", year) and re.fullmatch(r"\d{2}", month) and re.fullmatch(r"\d{2}", day):
            return f"{year}-{month}-{day}"

    raw_start = str(event.get("start_date") or "")
    match = re.match(r"^(\d{4}-\d{2}-\d{2})\b", raw_start)
    if match:
        logger.warning(
            "row %s used fallback start_date for meeting_date because start_date_details was incomplete: %r",
            event_id,
            raw_start,
        )
        return match.group(1)

    logger.warning("row %s has no parseable meeting_date: start_date=%r", event_id, raw_start)
    return ""


def _extract_time(event: dict) -> str:
    if event.get("all_day") is True:
        logger.warning("Snowflake all-day event has no meeting_time: id=%r", event.get("id"))
        return ""

    details = event.get("start_date_details")
    if isinstance(details, dict):
        hour = _to_int(details.get("hour"))
        minute = _to_int(details.get("minutes"))
        if hour is not None and minute is not None:
            return _format_time(hour, minute)

    raw_start = str(event.get("start_date") or "")
    match = re.match(r"^\d{4}-\d{2}-\d{2}\s+(\d{1,2}):(\d{2})", raw_start)
    if match:
        return _format_time(int(match.group(1)), int(match.group(2)))
    logger.warning(
        "Snowflake meeting_time left empty after all extraction signals: id=%r start_date=%r",
        event.get("id"),
        raw_start,
    )
    return ""


def _extract_location(event: dict) -> str:
    venue = event.get("venue")
    if not isinstance(venue, dict):
        logger.warning("Snowflake meeting_location absent by source row: id=%r", event.get("id"))
        return ""
    location = _clean_text(str(venue.get("venue") or ""))
    if not location:
        logger.warning("Snowflake meeting_location empty in venue object: id=%r venue=%r", event.get("id"), venue)
    return location


def _format_time(hour_24: int, minute: int) -> str:
    suffix = "AM" if hour_24 < 12 else "PM"
    hour_12 = hour_24 % 12 or 12
    return f"{hour_12}:{minute:02d} {suffix}"


def _event_has_category(event: dict, category_slug: str) -> bool:
    categories = event.get("categories")
    if not isinstance(categories, list):
        logger.warning("event id=%r has no categories list", event.get("id"))
        return False
    return any(isinstance(category, dict) and category.get("slug") == category_slug for category in categories)


def _category_slug_from_url(url: str) -> str:
    parsed = urlparse(url or CALENDAR_URL)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in ALLOWED_HOSTS:
        raise ValueError(f"Snowflake source URL is not allowlisted: {url!r}")
    if "/wp-json/tribe/events/v1/events" in parsed.path:
        values = parse_qs(parsed.query).get("categories", [])
        return values[0] if values else ""

    match = re.search(r"/events/category/([^/?#]+)/?", parsed.path)
    return match.group(1) if match else ""


def _fetch_text(session, url: str, *, accept: str) -> tuple[int, str, str]:
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
                raise ValueError(f"Response from {url} exceeded {MAX_RESPONSE_BYTES} bytes")
        return (
            response.status_code,
            bytes(body).decode(response.encoding or "utf-8", errors="replace"),
            response.url,
        )


def _emit_url(href: str, base_url: str, field: str, row_id: str) -> str:
    stripped = href.strip()
    if not stripped:
        return ""

    lowered = stripped.lower()
    bad_prefixes = (
        "javascript:",
        "data:",
        "vbscript:",
        "file:",
        "mailto:",
        "ftp:",
        "gopher:",
    )
    if lowered.startswith(bad_prefixes) or lowered in {"#", ""}:
        logger.warning(
            "dropped %s for row %s: rejected non-http href %r",
            field,
            row_id,
            href,
        )
        return ""

    absolute = urljoin(base_url, stripped)
    parsed = urlparse(absolute)
    host = _hostname(absolute)
    if parsed.scheme not in {"http", "https"}:
        logger.warning("dropped %s for row %s: disallowed scheme in %r", field, row_id, absolute)
        return ""
    if host not in ALLOWED_HOSTS:
        logger.warning("dropped %s for row %s: disallowed host %r in %r", field, row_id, host, absolute)
        return ""
    return absolute


def _hostname(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", unescape(value)).strip()


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _validate_schema(row: dict[str, str]) -> None:
    keys = tuple(row.keys())
    if keys != CANONICAL_FIELDS:
        raise ValueError(f"Unexpected Snowflake parser schema: {keys}")
    bad_fields = [key for key, value in row.items() if not isinstance(value, str)]
    if bad_fields:
        raise TypeError(f"Snowflake parser emitted non-string fields: {bad_fields}")


if __name__ == "__main__":
    parsed_meetings = scrape_calendar(CALENDAR_URL)
    print(f"Found {len(parsed_meetings)} meetings.")
    for parsed_meeting in parsed_meetings:
        print(parsed_meeting)
