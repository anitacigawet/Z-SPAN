from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from requests.exceptions import RequestException

from polite_http import make_session


logger = logging.getLogger(__name__)

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

ABSENT_BY_CONSTRUCTION_FIELDS = (
    "agenda_url",
    "minutes_url",
    "video_url",
    "agenda_packet_url",
    "ecomment_url",
)

BAD_URL_SCHEMES = ("javascript:", "data:", "vbscript:", "file:", "mailto:", "ftp:", "gopher:")
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
EVENT_ID_RE = re.compile(r"eventTitle_(\d+)$")
TIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})")
MAX_CALENDAR_BYTES = 2_000_000
MAX_ROWS = 300


def _fetch_text_bounded(
    session: Any,
    url: str,
    allowed_hosts: set[str],
    max_bytes: int = MAX_CALENDAR_BYTES,
) -> str:
    with session.get(url, timeout=30, stream=True, allow_redirects=True) as response:
        response.raise_for_status()
        final_host = _host(response.url)
        if final_host not in allowed_hosts:
            raise ValueError(f"Redirect to disallowed host: {final_host} (started from {url})")

        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ValueError(f"Response from {url} exceeded {max_bytes} bytes")
        return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _host(url: str) -> str:
    return (urlparse(url).netloc.split(":")[0] or "").lower()


def _allowed_hosts_for(url: str) -> set[str]:
    host = _host(url)
    if not host:
        raise ValueError(f"Input URL has no host: {url}")
    root = host.removeprefix("www.")
    return {root, f"www.{root}"}


def _calendar_id_from_url(url: str) -> str:
    values = parse_qs(urlparse(url).query).get("CID", [])
    if not values or not values[0].strip():
        raise ValueError(f"Input URL is missing required CID query parameter: {url}")
    calendar_id = values[0].strip().rstrip(",")
    if not calendar_id.isdigit():
        raise ValueError(f"Input URL has non-numeric CID query parameter: {calendar_id}")
    return calendar_id


def _list_url(url: str, calendar_id: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query["view"] = ["list"]
    query["CID"] = [calendar_id]
    query["showPastEvents"] = ["true"]
    flattened = [(key, value) for key, values in query.items() for value in values]
    return parsed._replace(query=urlencode(flattened)).geturl()


def _clean_text(node: Tag | None) -> str:
    if node is None:
        return ""
    text = BeautifulSoup(str(node), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _parse_start(start_text: str, row_label: str) -> tuple[str, str]:
    match = TIME_RE.match(start_text.strip())
    if not match:
        logger.warning("dropping row=%s reason=unparseable_startDate value=%r", row_label, start_text)
        return "", ""

    date_text, hour_text, minute_text = match.groups()
    hour = int(hour_text)
    minute = int(minute_text)
    marker = "AM" if hour < 12 else "PM"
    display_hour = hour % 12 or 12
    return date_text, f"{display_hour}:{minute:02d} {marker}"


def _emit_url(href: str, base_url: str, allowed_hosts: set[str], row_label: str, field: str) -> str:
    raw = (href or "").strip()
    if not raw:
        return ""
    lowered = raw.lower().lstrip()
    if lowered.startswith(BAD_URL_SCHEMES):
        logger.warning("rejected url row=%s field=%s reason=bad_scheme value=%r", row_label, field, raw)
        return ""

    absolute = urljoin(base_url, raw)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        logger.warning("rejected url row=%s field=%s reason=bad_absolute_scheme value=%r", row_label, field, raw)
        return ""

    emit_host = _host(absolute)
    if emit_host not in allowed_hosts:
        logger.warning(
            "rejected url row=%s field=%s reason=disallowed_host host=%s value=%r",
            row_label,
            field,
            emit_host,
            raw,
        )
        return ""
    return absolute


def _extract_location(event_data: Tag | None, row_label: str) -> str:
    location_node = event_data.select_one('[itemprop="location"] [itemprop="name"]') if event_data else None
    location = _clean_text(location_node)
    if location.lower() == "event location":
        logger.warning("dropped placeholder location row=%s value=%r", row_label, location)
        return ""
    return location


def _status_for(title: str, agenda_url: str, minutes_url: str, agenda_packet_url: str) -> str:
    if CANCELLED_RE.search(title[:500]):
        return "Cancelled"
    if minutes_url:
        return "Minutes Available"
    if agenda_url or agenda_packet_url:
        return "Agenda Available"
    return "Scheduled"


def _empty_meeting() -> dict[str, str]:
    return {field: "" for field in CANONICAL_FIELDS}


def _extract_meeting(
    row: Tag,
    title_link: Tag,
    base_url: str,
    allowed_hosts: set[str],
    counters: Counter[str],
) -> dict[str, str] | None:
    row_label = title_link.get("id", "")
    title = _clean_text(title_link)
    event_id_match = EVENT_ID_RE.match(row_label)
    meeting_id = event_id_match.group(1) if event_id_match else ""
    if not meeting_id:
        logger.warning("dropping row=%s reason=missing_event_id title=%r", row_label or "unknown", title)
        counters["dropped_missing_event_id"] += 1
        return None
    if not title:
        logger.warning("dropping row=%s reason=missing_title meeting_id=%s", row_label, meeting_id)
        counters["dropped_missing_title"] += 1
        return None

    event_data = row.select_one('[itemtype="http://schema.org/Event"]')
    if event_data is None:
        logger.warning("dropping row=%s reason=missing_schema_event title=%r", row_label, title)
        counters["dropped_missing_schema_event"] += 1
        return None

    start_node = event_data.select_one('[itemprop="startDate"]')
    start_text = _clean_text(start_node)
    meeting_date, meeting_time = _parse_start(start_text, row_label)
    if not meeting_date:
        counters["dropped_unparseable_start"] += 1
        return None

    location = _extract_location(event_data, row_label)
    if not location:
        counters["location_absent_or_placeholder"] += 1

    detail_href = title_link.get("href", "")
    detail_url = _emit_url(detail_href, base_url, allowed_hosts, row_label, "detail_url")
    if not detail_url:
        counters["detail_url_rejected"] += 1

    meeting = _empty_meeting()
    meeting.update(
        {
            "meeting_title": title,
            "meeting_date": meeting_date,
            "meeting_time": meeting_time,
            "meeting_location": location,
            "meeting_status": _status_for(title, "", "", ""),
            "meeting_id": meeting_id,
        }
    )
    return meeting


def scrape_calendar(url: str) -> list[dict]:
    logger.warning(
        "absent_by_construction fields=agenda_url,minutes_url,video_url,agenda_packet_url,ecomment_url "
        "reason=civicplus_calendar_list_has_no_same_row_document_links"
    )
    calendar_id = _calendar_id_from_url(url)
    allowed_hosts = _allowed_hosts_for(url)
    source_url = _list_url(url, calendar_id)

    session = make_session()
    try:
        html = _fetch_text_bounded(session, source_url, allowed_hosts)
    except RequestException as exc:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Sahuarita official calendar blocked the neutral paced request: "
            "url=%s failure_shape=honest-empty missing_data_scope=all_current_month_forward_meetings error=%r",
            source_url,
            exc,
        )
        return []
    soup = BeautifulSoup(html, "html.parser")

    container_selector = f"div#CID{calendar_id}.calendar"
    container = soup.select_one(container_selector)
    if container is None:
        raise RuntimeError(
            f"Sahuarita CivicPlus fingerprint drift: missing {container_selector!r} at {source_url}"
        )

    title_selector = 'a[id^="eventTitle_"]'
    title_links = container.select(title_selector)
    if not title_links:
        empty_text = _clean_text(container).casefold()
        if "no events" not in empty_text and "no results" not in empty_text:
            raise RuntimeError(
                "Sahuarita CivicPlus row drift: calendar container had no eventTitle anchors "
                "and no explicit empty-state marker"
            )
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning("Sahuarita official CivicPlus calendar explicitly reports no events")
        return []
    if len(title_links) > MAX_ROWS:
        raise RuntimeError(
            f"Sahuarita calendar exceeded the {MAX_ROWS}-row safety cap: {len(title_links)}"
        )

    counters: Counter[str] = Counter(exposed_rows=len(title_links))
    meetings: list[dict] = []
    seen_ids: set[str] = set()
    cutoff = date.today().replace(day=1)
    for title_link in title_links:
        row = title_link.find_parent("li")
        row_label = title_link.get("id", "unknown")
        if row is None:
            logger.warning("dropping row=%s reason=missing_parent_li", row_label)
            counters["dropped_missing_parent_li"] += 1
            continue

        try:
            meeting = _extract_meeting(row, title_link, source_url, allowed_hosts, counters)
        except Exception:
            logger.exception("dropping row=%s reason=row_parse_exception", row_label)
            counters["dropped_row_exception"] += 1
            continue

        if meeting is None:
            continue
        meeting_day = date.fromisoformat(meeting["meeting_date"])
        if meeting_day < cutoff:
            counters["dropped_before_current_month"] += 1
            logger.info(
                "dropping row=%s title=%r date=%s reason=before_current_month cutoff=%s",
                row_label,
                meeting["meeting_title"],
                meeting["meeting_date"],
                cutoff.isoformat(),
            )
            continue
        body_decision = _town_council_title_decision(meeting["meeting_title"])
        if body_decision == "subordinate":
            counters["dropped_known_subordinate_body"] += 1
            logger.info(
                "dropping row=%s title=%r reason=known_subordinate_body",
                row_label,
                meeting["meeting_title"],
            )
            continue
        if body_decision != "council":
            raise RuntimeError(
                f"Sahuarita current governing-body vocabulary drift at {row_label}: "
                f"{meeting['meeting_title']!r}"
            )
        if meeting["meeting_id"] in seen_ids:
            logger.warning("dropping row=%s reason=duplicate_meeting_id", row_label)
            counters["dropped_duplicate_id"] += 1
            continue
        seen_ids.add(meeting["meeting_id"])
        meetings.append(meeting)

    counters["accepted_rows"] = len(meetings)
    counters["dropped_rows"] = counters["exposed_rows"] - counters["accepted_rows"]
    status_counts = Counter(meeting["meeting_status"] for meeting in meetings)
    logger.info(
        "sahuarita scrape summary source_url=%s counters=%s status_counts=%s",
        source_url,
        dict(counters),
        dict(status_counts),
    )
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Sahuarita official calendar contained no Town Council rows from %s forward",
            cutoff.isoformat(),
        )
    return meetings


def _town_council_title_decision(title: str) -> str:
    folded = " ".join(title.casefold().split())
    if re.search(r"(?<!\w)town of sahuarita council meeting(?!\w)", folded):
        return "council"
    subordinate_terms = ("board", "commission", "committee", "authority", "advisory")
    if any(term in folded for term in subordinate_terms):
        return "subordinate"
    return "unknown"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    results = scrape_calendar("https://sahuaritaaz.gov/calendar.aspx?CID=26")
    print(f"Found {len(results)} meetings")
    for result in results[:5]:
        print(f"{result['meeting_date']} {result['meeting_time']} - {result['meeting_title']}")
