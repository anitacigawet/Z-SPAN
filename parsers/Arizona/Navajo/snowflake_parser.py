from __future__ import annotations

import json
import logging
import re
import ssl
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import HTTPSHandler, Request, build_opener


logger = logging.getLogger(__name__)

CALENDAR_URL = "https://www.snowflakeaz.gov/events/category/council-meetings/"
API_URL = "https://www.snowflakeaz.gov/wp-json/tribe/events/v1/events"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)
ALLOWED_HOSTS = {"www.snowflakeaz.gov", "snowflakeaz.gov"}
MAX_RESPONSE_BYTES = 5_000_000
PER_PAGE = 50
START_DATE = "2000-01-01"
END_DATE = "2100-12-31"
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


def scrape_calendar(url: str = CALENDAR_URL) -> list[dict[str, str]]:
    """Scrape Snowflake council meetings from The Events Calendar REST API."""
    category_slug = _category_slug_from_url(url) or "council-meetings"
    events = _fetch_rest_events(category_slug)
    meetings: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    for event in events:
        if not _event_has_category(event, category_slug):
            logger.warning(
                "dropped event id=%r title=%r: missing expected category %r",
                event.get("id"),
                event.get("title"),
                category_slug,
            )
            continue

        meeting = _build_meeting(event)
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
        "snowflake_parser emitted %d meetings from %d REST events for category=%s",
        len(meetings),
        len(events),
        category_slug,
    )
    return meetings


def _fetch_rest_events(category_slug: str) -> list[dict]:
    page = 1
    events: list[dict] = []

    while True:
        params = {
            "categories": category_slug,
            "start_date": START_DATE,
            "end_date": END_DATE,
            "per_page": str(PER_PAGE),
            "page": str(page),
        }
        api_page_url = f"{API_URL}?{urlencode(params)}"
        text = _fetch_text(api_page_url, accept="application/json")
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError(f"Unexpected Tribe Events API payload on page {page}: {type(data).__name__}")

        page_events = data.get("events", [])
        if not isinstance(page_events, list):
            raise ValueError(f"Unexpected Tribe Events API events payload on page {page}")

        logger.info(
            "fetched Tribe REST page=%d count=%d total=%r total_pages=%r",
            page,
            len(page_events),
            data.get("total"),
            data.get("total_pages"),
        )
        events.extend(event for event in page_events if isinstance(event, dict))

        total_pages = _to_int(data.get("total_pages"))
        if not page_events:
            break
        if total_pages and page >= total_pages:
            break
        if not total_pages and not data.get("next_rest_url"):
            break
        page += 1

    return events


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
    return ""


def _extract_location(event: dict) -> str:
    venue = event.get("venue")
    if not isinstance(venue, dict):
        return ""
    return _clean_text(str(venue.get("venue") or ""))


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
    if "/wp-json/tribe/events/v1/events" in parsed.path:
        values = parse_qs(parsed.query).get("categories", [])
        return values[0] if values else ""

    match = re.search(r"/events/category/([^/?#]+)/?", parsed.path)
    return match.group(1) if match else ""


def _fetch_text(url: str, *, accept: str) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": accept,
        },
        method="GET",
    )
    try:
        return _read_response(_open_request(request), url)
    except URLError as exc:
        if not _is_certificate_error(exc):
            raise RuntimeError(f"Failed to fetch {url}: {exc}") from exc
        logger.warning("default TLS verification failed for %s; retrying with /etc/ssl/cert.pem", url)
        return _read_response(_open_request(request, cafile="/etc/ssl/cert.pem"), url)
    except HTTPError as exc:
        raise RuntimeError(f"Failed to fetch {url}: {exc}") from exc


def _open_request(request: Request, cafile: str | None = None):
    opener = build_opener()
    if cafile:
        if not Path(cafile).exists():
            raise RuntimeError(f"TLS CA file not found: {cafile}")
        context = ssl.create_default_context(cafile=cafile)
        opener = build_opener(HTTPSHandler(context=context))
    return opener.open(request, timeout=30)


def _read_response(response, started_url: str) -> str:
    with response:
        final_host = _hostname(response.geturl())
        if final_host not in ALLOWED_HOSTS:
            raise ValueError(
                f"Redirect to disallowed host: {final_host} (started from {started_url})"
            )
        body = bytearray()
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Response from {started_url} exceeded {MAX_RESPONSE_BYTES} bytes")
        encoding = response.headers.get_content_charset() or "utf-8"
    return bytes(body).decode(encoding, errors="replace")


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


def _is_certificate_error(exc: URLError) -> bool:
    reason = getattr(exc, "reason", None)
    return isinstance(reason, ssl.SSLCertVerificationError)


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
