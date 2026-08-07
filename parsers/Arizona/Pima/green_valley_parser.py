"""Green Valley — The Events Calendar meeting parser."""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests


logger = logging.getLogger(__name__)

DEFAULT_API_URL = "https://gvcouncil.org/wp-json/tribe/events/v1/events"
ALLOWED_HOSTS = {"gvcouncil.org", "www.gvcouncil.org"}
BAD_SCHEMES = ("javascript:", "data:", "vbscript:", "file:", "mailto:", "ftp:", "gopher:")
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
MAX_PAGES = 10
PER_PAGE = 50
MAX_RESPONSE_BYTES = 5_000_000
REQUEST_TIMEOUT = 20
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
URL_LIKE_RE = re.compile(r"https?://[^\s\"'<>]+")

# The feed is a mixed public community calendar with no categories/tags.
# These are Green Valley Council governance bodies observed in the live feed.
KNOWN_GOVERNANCE_TITLES = {
    "board of representatives",
    "citizen corp / emergency planning",
    "clr / parks advisory",
    "environmental",
    "executive committee",
    "health & human services",
    "traffic & arroyos",
}
GOVERNANCE_KEYWORD_RE = re.compile(
    r"\b("
    r"board of directors|board of representatives|"
    r"executive committee|finance committee|committee|"
    r"council meeting|annual meeting|membership meeting|advisory"
    r")\b",
    re.IGNORECASE,
)


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._current_href = ""
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attr_map = {name.lower(): value or "" for name, value in attrs}
        self._current_href = attr_map.get("href", "")
        self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current_href:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._current_href:
            self.links.append((self._current_href, " ".join(self._current_text).strip()))
            self._current_href = ""
            self._current_text = []


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = TAG_RE.sub(" ", str(value))
    return " ".join(unescape(text).split())


def _normalize_title_key(title: str) -> str:
    return title.casefold().replace("&amp;", "&").strip()


def _api_url(url: str | None) -> str:
    if not url:
        return DEFAULT_API_URL
    parsed = urlparse(url)
    if parsed.path.rstrip("/") == "/calendar":
        return DEFAULT_API_URL
    return url


def _page_url(url: str, page: int) -> str:
    parsed = urlparse(url)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key not in {"page", "per_page"}]
    query.extend([("per_page", str(PER_PAGE)), ("page", str(page))])
    return urlunparse(parsed._replace(query=urlencode(query)))


def _host_allowed(url: str) -> bool:
    host = urlparse(url).netloc.split(":")[0].lower()
    return host in ALLOWED_HOSTS


def _validate_fetch_host(url: str) -> None:
    if not _host_allowed(url):
        raise ValueError(f"Disallowed Green Valley API host: {urlparse(url).netloc}")


def _read_response_text_bounded(response: requests.Response, max_bytes: int = MAX_RESPONSE_BYTES) -> str:
    body = bytearray()
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        body.extend(chunk)
        if len(body) > max_bytes:
            raise ValueError(f"Response from {response.url} exceeded {max_bytes} bytes")
    return body.decode(response.encoding or "utf-8")


def _fetch_json(session: requests.Session, url: str) -> dict[str, Any]:
    _validate_fetch_host(url)
    with session.get(url, timeout=REQUEST_TIMEOUT, stream=True) as response:
        response.raise_for_status()
        _validate_fetch_host(response.url)
        text = _read_response_text_bounded(response)
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise TypeError(f"Expected JSON object from {url}, got {type(payload).__name__}")
    return payload


def _event_rows(payload: dict[str, Any], page: int) -> list[dict[str, Any]]:
    raw_events = payload.get("events") or []
    if not isinstance(raw_events, list):
        logger.warning("green_valley page=%s dropped events payload because events was %s", page, type(raw_events).__name__)
        return []

    events: list[dict[str, Any]] = []
    dropped = Counter(type(event).__name__ for event in raw_events if not isinstance(event, dict))
    for event in raw_events:
        if isinstance(event, dict):
            events.append(event)
    if dropped:
        logger.warning("green_valley page=%s dropped non-object event rows: %s", page, dict(dropped))
    return events


def _fetch_events(session: requests.Session, url: str) -> list[dict[str, Any]]:
    api_url = _api_url(url)
    first_page = _fetch_json(session, _page_url(api_url, 1))
    total = int(first_page.get("total") or 0)
    total_pages = int(first_page.get("total_pages") or 1)
    page_limit = min(total_pages, MAX_PAGES)
    events = _event_rows(first_page, 1)
    logger.info(
        "green_valley pagination page=1 events=%s total=%s total_pages=%s per_page=%s",
        len(events),
        total,
        total_pages,
        PER_PAGE,
    )

    for page in range(2, page_limit + 1):
        payload = _fetch_json(session, _page_url(api_url, page))
        page_events = _event_rows(payload, page)
        logger.info("green_valley pagination page=%s events=%s", page, len(page_events))
        events.extend(page_events)

    if total_pages > MAX_PAGES:
        logger.warning("green_valley pagination capped at %s of %s pages", MAX_PAGES, total_pages)
    if total and len(events) != min(total, page_limit * PER_PAGE):
        logger.warning("green_valley fetched %s events but API reported total=%s", len(events), total)
    logger.info("green_valley pagination complete pages_fetched=%s events_fetched=%s", page_limit, len(events))
    return events


def _venue_shape(venue: Any, event: dict[str, Any]) -> str:
    if "venue" not in event:
        return "absent"
    if venue is None:
        return "none"
    if isinstance(venue, dict):
        return "dict"
    if isinstance(venue, list):
        return "list"
    return type(venue).__name__


def _venue_name(venue: Any) -> str:
    if isinstance(venue, dict):
        return _clean_text(venue.get("venue", ""))
    return ""


def _extract_location(venue: Any, event_id: str, title: str, absence_counter: Counter[str]) -> str:
    if not isinstance(venue, dict):
        absence_counter["location_no_dict_venue"] += 1
        return ""
    parts = [
        _clean_text(venue.get("venue", "")),
        _clean_text(venue.get("address", "")),
        _clean_text(venue.get("city", "")),
        _clean_text(venue.get("state") or venue.get("stateprovince", "")),
        _clean_text(venue.get("zip", "")),
    ]
    location = ", ".join(part for part in parts if part)
    if not location:
        logger.info("event_id=%s title=%r emitted empty meeting_location because venue dict had no address fields", event_id, title)
        absence_counter["location_empty_dict"] += 1
    return location


def _classify_is_governance_meeting(event: dict[str, Any]) -> tuple[bool, str]:
    title = _clean_text(event.get("title", ""))
    title_key = _normalize_title_key(title)
    venue = event.get("venue")
    venue = _venue_name(venue)
    if title_key in KNOWN_GOVERNANCE_TITLES:
        return True, f"title matches known Green Valley Council governance body; venue={venue or 'not listed'}"
    match = GOVERNANCE_KEYWORD_RE.search(title)
    if match:
        return True, f"title contains governance keyword {match.group(0)!r}; venue={venue or 'not listed'}"
    return False, f"title lacks governance keyword/known-body signal; venue={venue or 'not listed'}"


def _emit_url(href: str, base_url: str, field: str, event_id: str, title: str) -> str:
    if not href:
        return ""
    stripped = href.strip()
    lowered = stripped.lower()
    if lowered.startswith(BAD_SCHEMES):
        logger.warning("event_id=%s title=%r rejected %s URL %r: bad scheme", event_id, title, field, href)
        return ""
    absolute = urljoin(base_url, stripped)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        logger.warning("event_id=%s title=%r rejected %s URL %r: non-http scheme", event_id, title, field, href)
        return ""
    if not _host_allowed(absolute):
        logger.warning("event_id=%s title=%r rejected %s URL %r: disallowed host", event_id, title, field, href)
        return ""
    return absolute


def _description_links(description: str) -> list[tuple[str, str]]:
    parser = _AnchorParser()
    parser.feed(description or "")
    return parser.links


def _custom_field_texts(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        texts: list[str] = []
        for key, nested_value in value.items():
            texts.extend(_custom_field_texts(key))
            texts.extend(_custom_field_texts(nested_value))
        return texts
    if isinstance(value, list):
        texts = []
        for item in value:
            texts.extend(_custom_field_texts(item))
        return texts
    return []


def _classify_document_links(event: dict[str, Any], base_url: str, event_id: str, title: str) -> dict[str, str]:
    links: list[tuple[str, str, str]] = []
    website = event.get("website")
    if website:
        links.append((str(website), "website", "website"))
    virtual_url = event.get("virtual_url")
    if virtual_url:
        links.append((str(virtual_url), "virtual_url", "virtual_url"))
    for href, label in _description_links(str(event.get("description") or "")):
        links.append((href, label, "description"))
    for text in _custom_field_texts(event.get("custom_fields")):
        for href, label in _description_links(text):
            links.append((href, label, "custom_fields"))
        for href in URL_LIKE_RE.findall(text):
            links.append((href, "custom_fields", "custom_fields"))

    docs = {"agenda_url": "", "minutes_url": "", "agenda_packet_url": "", "video_url": "", "ecomment_url": ""}
    for href, label, source in links:
        emitted = _emit_url(href, base_url, "document_candidate", event_id, title)
        if not emitted:
            continue
        combined = f"{label} {emitted}".casefold()
        if "minutes" in combined:
            docs["minutes_url"] = docs["minutes_url"] or emitted
        elif "packet" in combined:
            docs["agenda_packet_url"] = docs["agenda_packet_url"] or emitted
        elif "agenda" in combined or emitted.casefold().endswith(".pdf"):
            docs["agenda_url"] = docs["agenda_url"] or emitted
        elif "video" in combined or "recording" in combined:
            docs["video_url"] = docs["video_url"] or emitted
        else:
            logger.warning(
                "event_id=%s title=%r ignored unclassified same-row URL from %s: label=%r href=%r",
                event_id,
                title,
                source,
                label,
                href,
            )
    return docs


def _parse_start(start_date: Any, event_id: str, title: str) -> tuple[str, str]:
    if not start_date:
        logger.warning("event_id=%s title=%r emitted empty date/time: missing start_date", event_id, title)
        return "", ""
    try:
        parsed = datetime.strptime(str(start_date), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        logger.warning("event_id=%s title=%r emitted empty date/time: unparseable start_date=%r", event_id, title, start_date)
        return "", ""
    return parsed.strftime("%Y-%m-%d"), parsed.strftime("%I:%M %p").lstrip("0")


def _classify_status(title: str, docs: dict[str, str]) -> str:
    if CANCELLED_RE.search(title):
        return "Cancelled"
    if docs["minutes_url"]:
        return "Minutes Available"
    if docs["agenda_url"] or docs["agenda_packet_url"]:
        return "Agenda Available"
    return "Scheduled"


def _build_meeting(event: dict[str, Any], base_url: str, absence_counter: Counter[str]) -> dict[str, str]:
    event_id = str(event.get("id") or "")
    title = _clean_text(event.get("title", ""))
    venue = event.get("venue")
    meeting_date, meeting_time = _parse_start(event.get("start_date"), event_id, title)
    docs = _classify_document_links(event, base_url, event_id, title)
    meeting = {
        "meeting_title": title,
        "meeting_date": meeting_date,
        "meeting_time": meeting_time,
        "meeting_location": _extract_location(venue, event_id, title, absence_counter),
        "meeting_status": _classify_status(title, docs),
        "agenda_url": docs["agenda_url"],
        "minutes_url": docs["minutes_url"],
        "video_url": docs["video_url"],
        "agenda_packet_url": docs["agenda_packet_url"],
        "ecomment_url": docs["ecomment_url"],
        "meeting_id": event_id,
    }
    return {field: str(meeting[field] or "") for field in CANONICAL_FIELDS}


def _log_absence_summary(events: list[dict[str, Any]], meetings: list[dict[str, str]], absence_counter: Counter[str]) -> None:
    if not any(event.get("categories") for event in events) and not any(event.get("tags") for event in events):
        logger.warning("green_valley vendor exposes empty categories/tags on all fetched rows; governance classification used title signals")
    has_document_signal = any(
        event.get("website")
        or event.get("virtual_url")
        or event.get("custom_fields")
        or _description_links(str(event.get("description") or ""))
        for event in events
    )
    if not has_document_signal:
        logger.warning(
            "green_valley vendor exposes no agenda/minutes/packet/video/ecomment document links in website, virtual_url, custom_fields, or description fields; document URL fields emitted empty"
        )
    if absence_counter:
        logger.info("green_valley field absence summary: %s", dict(absence_counter))
    if not any(meeting["agenda_url"] or meeting["agenda_packet_url"] for meeting in meetings):
        logger.warning("green_valley emitted Scheduled status for accepted meetings because no same-row agenda evidence was present")


def scrape_calendar(url: str = DEFAULT_API_URL) -> list[dict]:
    """Scrape Green Valley Council governance meetings from the mixed events feed."""
    session = requests.Session()
    try:
        events = _fetch_events(session, url)
    except requests.exceptions.RequestException as exc:
        logger.exception("green_valley request failure while fetching Tribe Events API: %s", exc)
        return []
    except json.JSONDecodeError as exc:
        logger.exception("green_valley JSON decode failure while parsing Tribe Events API: %s", exc)
        return []

    base_url = _api_url(url)
    venue_shapes: Counter[str] = Counter()
    exclusion_reasons: Counter[str] = Counter()
    absence_counter: Counter[str] = Counter()
    meetings: list[dict[str, str]] = []

    for event in events:
        event_id = str(event.get("id") or "")
        title = _clean_text(event.get("title", ""))
        venue_shapes[_venue_shape(event.get("venue"), event)] += 1
        is_meeting, reason = _classify_is_governance_meeting(event)
        if not is_meeting:
            exclusion_reasons[reason] += 1
            logger.info("event_id=%s title=%r excluded: %s", event_id, title, reason)
            continue
        try:
            meeting = _build_meeting(event, base_url, absence_counter)
        except (AttributeError, TypeError) as exc:
            logger.exception("event_id=%s title=%r dropped after defensive type failure: %s", event_id, title, exc)
            continue
        meetings.append(meeting)
        logger.info(
            "event_id=%s title=%r included: %s; status=%s date=%s",
            event_id,
            title,
            reason,
            meeting["meeting_status"],
            meeting["meeting_date"],
        )

    logger.info("green_valley venue shape summary: %s", dict(venue_shapes))
    logger.info("green_valley excluded %s of %s rows: %s", sum(exclusion_reasons.values()), len(events), dict(exclusion_reasons))
    logger.info("green_valley accepted %s governance meetings from %s fetched events", len(meetings), len(events))
    _log_absence_summary(events, meetings, absence_counter)
    return meetings


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    scraped = scrape_calendar(DEFAULT_API_URL)
    sample_titles = [meeting["meeting_title"] for meeting in scraped[:5]]
    print(f"Found {len(scraped)} Green Valley governance meetings. Sample titles: {sample_titles}")
