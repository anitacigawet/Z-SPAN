"""Current-month-forward parser for Thatcher's official Granicus calendar list."""

from __future__ import annotations

from datetime import date
import logging
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from polite_http import make_session

logger = logging.getLogger(__name__)

DEFAULT_URL = (
    "https://www.thatcher.az.gov/government/advanced-components/"
    "list-detail-pages/calendar-meeting-list"
)
OFFICIAL_HOSTS = {"thatcher.az.gov", "www.thatcher.az.gov"}
MAX_RESPONSE_BYTES = 2_000_000
_DATE_RE = re.compile(r"(?<!\d)(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])/(20\d{2})(?!\d)")
_TIME_RE = re.compile(r"(?<!\d)(1[0-2]|0?[1-9]):([0-5]\d)\s*([AP])\.?M\.?(?=\s|$|-)", re.I)
_CANCEL_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.I)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _validate_input(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or _host(url) not in OFFICIAL_HOSTS:
        raise ValueError("Thatcher calendar URL must use HTTPS on the official town host")


def _fetch_bounded(session) -> tuple[int, str, str, dict[str, str]]:
    with session.get(DEFAULT_URL, timeout=30, stream=True, allow_redirects=True) as response:
        if urlparse(response.url).scheme != "https" or _host(response.url) not in OFFICIAL_HOSTS:
            raise RuntimeError(f"Thatcher source redirected to a disallowed host: {response.url}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise RuntimeError(
                    f"Thatcher response exceeded the {MAX_RESPONSE_BYTES}-byte safety cap"
                )
        text = bytes(body).decode(response.encoding or "utf-8", errors="replace")
        logger.info(
            "Thatcher fetched status=%s bytes=%s url=%s",
            response.status_code,
            len(body),
            response.url,
        )
        return (
            response.status_code,
            response.url,
            text,
            {key.lower(): value for key, value in response.headers.items()},
        )


def _blocker(status: int, html: str, headers: dict[str, str]) -> str:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
    lowered = html[:50_000].lower()
    if (
        headers.get("cf-mitigated", "").lower() == "challenge"
        or "challenges.cloudflare.com" in lowered
        or "__cf_chl" in lowered
        or title.startswith("just a moment")
    ):
        return "Cloudflare challenge"
    if title == "access denied" or "access denied" in lowered[:5_000]:
        return "Access Denied"
    if status in {401, 403, 429}:
        return f"HTTP {status}"
    return ""


def _emit_url(raw: str, base_url: str, field_name: str) -> str:
    absolute = urljoin(base_url, raw.strip())
    if urlparse(absolute).scheme != "https" or _host(absolute) not in OFFICIAL_HOSTS:
        logger.warning(
            "Thatcher dropped %s URL %r: HTTPS host is not allowlisted",
            field_name,
            raw,
        )
        return ""
    return absolute


def _meeting_id(url: str) -> str:
    match = re.search(r"/Calendar/Event/(\d+)(?:/|$)", urlparse(url).path, re.I)
    return match.group(1) if match else ""


def _parse_date_time(text: str, title: str) -> tuple[date, str]:
    dates = {
        date(int(year), int(month), int(day))
        for month, day, year in _DATE_RE.findall(text[:500])
    }
    if len(dates) != 1:
        raise RuntimeError(
            f"Thatcher row {title!r} exposed {len(dates)} distinct dates; expected one"
        )
    time_match = _TIME_RE.search(text[:500])
    meeting_time = ""
    if time_match:
        hour, minute, meridiem = time_match.groups()
        meeting_time = f"{int(hour)}:{minute} {meridiem.upper()}M"
    return next(iter(dates)), meeting_time


def _classify_row_links(row, base_url: str, title: str) -> dict[str, str]:
    documents = {
        "agenda_url": "",
        "minutes_url": "",
        "agenda_packet_url": "",
        "video_url": "",
    }
    for anchor in row.find_all("a", href=True):
        raw = anchor["href"]
        label = " ".join(
            [
                anchor.get_text(" ", strip=True),
                anchor.get("title", ""),
                " ".join(anchor.get("class", [])),
                " ".join(anchor.parent.get("class", [])) if anchor.parent else "",
            ]
        ).lower()
        if "/calendar/event/" in raw.lower():
            continue
        if "minute" in label:
            field = "minutes_url"
        elif "packet" in label:
            field = "agenda_packet_url"
        elif "agenda" in label:
            field = "agenda_url"
        elif "video" in label or "recording" in label:
            field = "video_url"
        else:
            logger.warning(
                "Thatcher council row %r exposed an unclassified link: label=%r href=%r",
                title,
                label,
                raw,
            )
            continue
        emitted = _emit_url(raw, base_url, field)
        if emitted and not documents[field]:
            documents[field] = emitted
        elif emitted and documents[field] != emitted:
            logger.warning(
                "Thatcher council row %r exposed multiple %s values; retained the first",
                title,
                field,
            )
    return documents


def _parse_page(html: str, base_url: str, cutoff: date) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(" ", strip=True)
    if "calendar meeting list" not in page_text.lower():
        raise RuntimeError("Thatcher source lost its Calendar Meeting List fingerprint")

    rows = soup.select(".cat_list_row")
    if not rows:
        if "no results" in page_text.lower() or "no events" in page_text.lower():
            logger.warning("health_empty_kind=confirmed_empty")
            logger.warning("Thatcher calendar proved an explicit vendor event-empty state")
            return []
        raise RuntimeError("Thatcher calendar fingerprint was present but event rows were absent")

    meetings: list[dict[str, str]] = []
    current_non_council = 0
    historical = 0
    malformed_rows = 0
    seen_ids: set[str] = set()
    for row_index, row in enumerate(rows, start=1):
        title_link = row.select_one('a[href*="/Calendar/Event/"], a[href*="/calendar/event/"]')
        if title_link is None:
            malformed_rows += 1
            logger.warning("Thatcher event row %s had no event-detail title link", row_index)
            continue
        title = BeautifulSoup(title_link.decode_contents(), "html.parser").get_text(" ", strip=True)
        if not title:
            raise RuntimeError(f"Thatcher event row {row_index} has an empty title")
        meeting_date, meeting_time = _parse_date_time(row.get_text(" ", strip=True), title)
        if meeting_date < cutoff:
            historical += 1
            continue
        if "town council" not in title.lower():
            current_non_council += 1
            continue

        detail_url = _emit_url(title_link["href"], base_url, "detail_url")
        if not detail_url:
            raise RuntimeError(f"Thatcher council row {title!r} had an unsafe detail URL")
        meeting_id = _meeting_id(detail_url)
        if meeting_id and meeting_id in seen_ids:
            raise RuntimeError(f"Thatcher source emitted duplicate meeting ID {meeting_id}")
        if meeting_id:
            seen_ids.add(meeting_id)
        documents = _classify_row_links(row, base_url, title)
        status = (
            "Cancelled"
            if _CANCEL_RE.search(title)
            else "Minutes Available"
            if documents["minutes_url"]
            else "Agenda Available"
            if documents["agenda_url"] or documents["agenda_packet_url"]
            else "Scheduled"
        )
        meetings.append(
            {
                "meeting_title": title,
                "meeting_date": meeting_date.isoformat(),
                "meeting_time": meeting_time,
                "meeting_location": "",
                "meeting_status": status,
                "agenda_url": documents["agenda_url"],
                "minutes_url": documents["minutes_url"],
                "video_url": documents["video_url"],
                "agenda_packet_url": documents["agenda_packet_url"],
                "ecomment_url": "",
                "meeting_id": meeting_id,
            }
        )

    logger.warning(
        "Thatcher list surface does not expose per-row meeting location; emitting it empty"
    )
    logger.info(
        "Thatcher audit: rows_seen=%s historical=%s current_non_council=%s malformed=%s emitted=%s",
        len(rows),
        historical,
        current_non_council,
        malformed_rows,
        len(meetings),
    )
    if not meetings:
        if malformed_rows:
            raise RuntimeError(
                "Thatcher calendar contained malformed event rows, so an official zero cannot be witnessed"
            )
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Thatcher proved an honest current council empty from %s accessible event rows",
            len(rows),
        )
    return meetings


def scrape_calendar(url: str = DEFAULT_URL) -> list[dict[str, str]]:
    """Return official Thatcher council events from this calendar month forward."""
    _validate_input(url)
    cutoff = date.today().replace(day=1)
    try:
        with make_session() as session:
            status, final_url, html, headers = _fetch_bounded(session)
    except requests.exceptions.SSLError as exc:
        logger.warning("health_empty_kind=source_blocked")
        raise RuntimeError("Thatcher official calendar failed verified TLS") from exc
    blocker = _blocker(status, html, headers)
    if blocker:
        logger.warning("health_empty_kind=source_blocked")
        raise RuntimeError(
            f"Thatcher official calendar is blocked by {blocker}; "
            "this is not a successful empty source"
        )
    if status != 200:
        raise RuntimeError(
            f"Thatcher official calendar returned HTTP {status}; "
            "this is not a successful empty source"
        )
    return _parse_page(html, final_url, cutoff)
