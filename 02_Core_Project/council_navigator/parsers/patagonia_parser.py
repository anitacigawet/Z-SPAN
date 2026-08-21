"""Current-month-forward parser for Patagonia's official council document page."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import logging
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from polite_http import make_session

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://patagonia-az.gov/mayor-council/meetings-agendas-minutes/"
OFFICIAL_HOSTS = {"patagonia-az.gov", "www.patagonia-az.gov"}
VIDEO_HOSTS = OFFICIAL_HOSTS | {"us02web.zoom.us"}
MAX_RESPONSE_BYTES = 2_000_000
_CANCEL_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
_DATE_RE = re.compile(
    r"(?<!\d)(0?[1-9]|1[0-2])[\s/_-]+(0?[1-9]|[12]\d|3[01])[\s/_-]+(20\d{2})(?!\d)"
)


@dataclass
class _Group:
    meeting_date: str
    meeting_title: str
    cancelled: bool = False
    agenda_url: str = ""
    minutes_url: str = ""
    video_url: str = ""
    priorities: dict[str, int] = field(default_factory=dict)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _validate_input(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or _host(url) not in OFFICIAL_HOSTS:
        raise ValueError("Patagonia calendar URL must use HTTPS on the official town host")


def _fetch_bounded(session, url: str) -> str:
    try:
        response_context = session.get(url, timeout=30, stream=True, allow_redirects=True)
    except requests.exceptions.SSLError:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning("Patagonia official council source failed verified TLS")
        raise
    with response_context as response:
        if getattr(response, "status_code", None) in {401, 403, 429}:
            logger.warning("health_empty_kind=source_blocked")
        response.raise_for_status()
        if urlparse(response.url).scheme != "https" or _host(response.url) not in OFFICIAL_HOSTS:
            raise RuntimeError(f"Patagonia source redirected to a disallowed host: {response.url}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise RuntimeError(
                    f"Patagonia response exceeded the {MAX_RESPONSE_BYTES}-byte safety cap"
                )
        logger.info("Patagonia fetched %s bytes from %s", len(body), response.url)
        return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _extract_date(text: str) -> date | None:
    matches = {
        date(int(year), int(month), int(day))
        for month, day, year in _DATE_RE.findall(text[:500])
    }
    if len(matches) == 1:
        return next(iter(matches))
    if len(matches) > 1:
        logger.warning("Patagonia link exposed ambiguous dates and was dropped: %r", text)
    return None


def _meeting_title(text: str) -> str:
    lowered = text.lower()
    if "work session" in lowered:
        title = "Town Council Work Session"
    elif "special" in lowered:
        title = "Town Council Special Meeting"
    else:
        title = "Town Council Regular Meeting"
    if "public hearing" in lowered:
        title += " & Public Hearing"
    if _CANCEL_RE.search(text):
        title += " (Cancelled)"
    return title


def _classify(text: str) -> tuple[str, int]:
    lowered = text.lower()
    amended = int("amended" in lowered or "revised" in lowered)
    if "minutes" in lowered:
        return "minutes_url", 30 + amended
    if "video" in lowered:
        return "video_url", 20 + amended
    if "agenda" in lowered:
        return "agenda_url", 10 + amended
    return "", 0


def _emit_url(raw: str, base_url: str, field_name: str) -> str:
    absolute = urljoin(base_url, raw.strip())
    allowed = VIDEO_HOSTS if field_name == "video_url" else OFFICIAL_HOSTS
    if urlparse(absolute).scheme != "https" or _host(absolute) not in allowed:
        logger.warning(
            "Patagonia dropped %s URL %r: HTTPS host is not allowlisted",
            field_name,
            raw,
        )
        return ""
    return absolute


def _parse_page(html: str, cutoff: date) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1")
    heading_text = heading.get_text(" ", strip=True).lower() if heading else ""
    if "meetings" not in heading_text or "agenda" not in heading_text:
        raise RuntimeError("Patagonia source lost its Meetings - Agendas & Minutes fingerprint")

    scope = soup.find("main") or soup.find("article") or soup
    groups: dict[tuple[str, str], _Group] = {}
    recognized = 0
    historical = 0
    unclassified_current = 0
    newest_date: date | None = None
    for anchor in scope.find_all("a", href=True):
        text = BeautifulSoup(anchor.decode_contents(), "html.parser").get_text(" ", strip=True)
        lowered = text.lower()
        if "council meeting" not in lowered:
            continue
        meeting_date = _extract_date(text)
        if meeting_date is None:
            logger.warning(
                "Patagonia council-meeting link had no unambiguous date: text=%r href=%r",
                text,
                anchor["href"],
            )
            continue
        recognized += 1
        newest_date = max(newest_date, meeting_date) if newest_date else meeting_date
        if meeting_date < cutoff:
            historical += 1
            continue

        field_name, priority = _classify(text)
        if not field_name:
            unclassified_current += 1
            logger.warning(
                "Patagonia current council link was not a supported agenda/minutes/video document: "
                "text=%r href=%r",
                text,
                anchor["href"],
            )
            continue
        emitted = _emit_url(anchor["href"], DEFAULT_URL, field_name)
        if not emitted:
            continue
        title = _meeting_title(text)
        key = (meeting_date.isoformat(), title)
        group = groups.setdefault(key, _Group(key[0], title, bool(_CANCEL_RE.search(text))))
        existing_priority = group.priorities.get(field_name, -1)
        if priority < existing_priority:
            logger.warning(
                "Patagonia retained the higher-priority %s for %s and dropped %s",
                field_name,
                key,
                emitted,
            )
            continue
        if getattr(group, field_name) and getattr(group, field_name) != emitted:
            logger.warning(
                "Patagonia replaced %s for %s with equal/higher-priority evidence",
                field_name,
                key,
            )
        setattr(group, field_name, emitted)
        group.priorities[field_name] = priority

    if recognized == 0:
        raise RuntimeError("Patagonia page had no date-bearing council-meeting document links")

    meetings: list[dict[str, str]] = []
    for key in sorted(groups):
        group = groups[key]
        status = (
            "Cancelled"
            if group.cancelled
            else "Minutes Available"
            if group.minutes_url
            else "Agenda Available"
            if group.agenda_url
            else "Scheduled"
        )
        meetings.append(
            {
                "meeting_title": group.meeting_title,
                "meeting_date": group.meeting_date,
                "meeting_time": "",
                "meeting_location": "",
                "meeting_status": status,
                "agenda_url": group.agenda_url,
                "minutes_url": group.minutes_url,
                "video_url": group.video_url,
                "agenda_packet_url": "",
                "ecomment_url": "",
                "meeting_id": "",
            }
        )

    logger.warning(
        "Patagonia source does not expose per-document meeting time or location; "
        "emitting those fields empty"
    )
    logger.info(
        "Patagonia audit: recognized_links=%s historical_dropped=%s current_unclassified=%s "
        "current_meetings=%s newest_source_date=%s",
        recognized,
        historical,
        unclassified_current,
        len(meetings),
        newest_date.isoformat() if newest_date else "",
    )
    if not meetings:
        if unclassified_current:
            raise RuntimeError(
                "Patagonia exposed current council links with unknown document vocabulary, so an official zero cannot be witnessed"
            )
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Patagonia proved an honest current-month empty from %s recognized historical links; "
            "newest source date=%s cutoff=%s",
            recognized,
            newest_date,
            cutoff,
        )
    return meetings


def scrape_calendar(url: str = DEFAULT_URL) -> list[dict[str, str]]:
    """Return official Patagonia council records from this calendar month forward."""
    _validate_input(url)
    cutoff = date.today().replace(day=1)
    with make_session() as session:
        html = _fetch_bounded(session, DEFAULT_URL)
    return _parse_page(html, cutoff)
