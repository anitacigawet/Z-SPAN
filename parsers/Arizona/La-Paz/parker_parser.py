"""Parker — Thrillshare CMS meeting parser."""

from __future__ import annotations

import json
import logging
import re
import ssl
from html import unescape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPSHandler, Request, build_opener


logger = logging.getLogger(__name__)

DEFAULT_URL = "https://www.townofparkeraz.com/events"
# The public Apptegy site is protected by a JavaScript client challenge, while
# the underlying Thrillshare CMS events endpoint is directly accessible.
API_URL = "https://thrillshare-cmsv2.services.thrillshare.com/api/v4/o/18804/cms/events"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)
ALLOWED_HOSTS = {
    "thrillshare-cmsv2.services.thrillshare.com",
    "parkeraz.thrillshare.com",
    "www.townofparkeraz.com",
    "townofparkeraz.com",
}
MAX_RESPONSE_BYTES = 5_000_000
PER_PAGE = 100
MAX_PAGES = 40  # ~4,000 events forward — generous coverage without draining the 12k feed
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

# Governance-title whitelist — town council + advisory boards + planning commission.
GOVERNANCE_INCLUDE_RE = re.compile(
    r"""(?ix)\b(
    town\s+council
    | council\s+meeting
    | regular\s+council
    | special\s+council
    | council\s+session
    | work\s+session
    | study\s+session
    | planning\s+commission
    | planning\s+(?:and|&)\s+zoning
    | board\s+of\s+adjustment
    | board\s+of\s+directors
    | streets?\s+and\s+traffic\s+advisory
    | library\s+advisory\s+board
    | budget\s+hearing
    | public\s+hearing
    )\b"""
)
# Community-event exclusion — filters obvious non-governance titles even when
# they contain governance-adjacent substrings.
GOVERNANCE_EXCLUDE_RE = re.compile(
    r"""(?ix)\b(
    teen\s+advisory
    | book\s+club
    | AA\s+meeting
    | zumba
    | line\s+dancing
    | art\s+guild
    | project\s+linus
    | veterans\s+counseling
    | senior\s+committee\s+meeting
    | arts\s+&\s+crafts
    )\b"""
)
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)


def scrape_calendar(url: str = DEFAULT_URL) -> list[dict[str, str]]:
    """Parker Town Council + advisory-board meetings from Thrillshare CMS events REST."""
    events = _fetch_events()
    meetings: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    dropped_non_governance = 0
    dropped_excluded = 0

    for event in events:
        if not isinstance(event, dict):
            continue

        title = _clean_text(str(event.get("title") or ""))
        if not title:
            continue

        if not GOVERNANCE_INCLUDE_RE.search(title):
            dropped_non_governance += 1
            continue
        if GOVERNANCE_EXCLUDE_RE.search(title):
            dropped_excluded += 1
            logger.info("dropped excluded row: title=%r", title)
            continue

        meeting = _build_meeting(event)
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
        "parker_parser emitted %d meetings from %d Thrillshare events (dropped %d non-governance + %d excluded)",
        len(meetings),
        len(events),
        dropped_non_governance,
        dropped_excluded,
    )
    return meetings


def _fetch_events() -> list[dict]:
    """Paginate through Thrillshare events REST, bounded by MAX_PAGES."""
    events: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        params = {"locale": "en", "per_page": str(PER_PAGE), "page_no": str(page)}
        api_page_url = f"{API_URL}?{urlencode(params)}"
        text = _fetch_text(api_page_url, accept="application/json")
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
        if not page_events:
            logger.info("stopping pagination: empty page at page_no=%d", page)
            break
        events.extend(e for e in page_events if isinstance(e, dict))

    return events


def _build_meeting(event: dict) -> dict[str, str]:
    event_id = _clean_text(str(event.get("id") or ""))
    title = _clean_text(str(event.get("title") or ""))
    meeting_date = _extract_date(event, event_id)
    meeting_time = _extract_time(event)
    meeting_location = _extract_location(event)

    # Thrillshare events do not expose per-event agenda, minutes, or video URLs.
    # Historical PDFs sit behind the site's Nuxt interface and require rendered-
    # page extraction, so these fields remain empty instead of being fabricated.
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

    return ""


def _extract_location(event: dict) -> str:
    venue = _clean_text(str(event.get("venue") or ""))
    if venue:
        return venue
    address = _clean_text(str(event.get("address") or ""))
    if address:
        return address
    return ""


def _format_time(hour_24: int, minute: int) -> str:
    suffix = "AM" if hour_24 < 12 else "PM"
    hour_12 = hour_24 % 12 or 12
    return f"{hour_12}:{minute:02d} {suffix}"


def _fetch_text(url: str, *, accept: str) -> str:
    request = Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": accept},
        method="GET",
    )
    try:
        return _read_response(_open_request(request), url)
    except URLError as exc:
        if not _is_certificate_error(exc):
            raise RuntimeError(f"Failed to fetch {url}: {exc}") from exc
        logger.warning(
            "default TLS verification failed for %s; retrying with /etc/ssl/cert.pem",
            url,
        )
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
                raise ValueError(
                    f"Response from {started_url} exceeded {MAX_RESPONSE_BYTES} bytes"
                )
        encoding = response.headers.get_content_charset() or "utf-8"
    return bytes(body).decode(encoding, errors="replace")


def _is_certificate_error(exc: URLError) -> bool:
    reason = getattr(exc, "reason", None)
    return isinstance(reason, ssl.SSLCertVerificationError)


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
