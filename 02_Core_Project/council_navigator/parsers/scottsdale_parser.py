"""Scottsdale City Council agendas from the city's official document page."""

from __future__ import annotations

from collections import Counter
from datetime import date
from html import unescape
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from polite_http import make_session

__all__ = ["scrape_calendar"]

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://www.scottsdaleaz.gov/council/meeting-information/agendas-minutes"
ALLOWED_HOSTS = {"scottsdaleaz.gov", "www.scottsdaleaz.gov", "ww2.scottsdaleaz.gov"}
MAX_RESPONSE_BYTES = 10_000_000
CHUNK_SIZE = 65_536
FIELDS = (
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
AGENDA_RE = re.compile(
    r"(?P<month>\d{2})-(?P<day>\d{2})-(?P<year>\d{2})-(?P<label>[^?#]*agenda)(?:\.pdf)?$",
    re.IGNORECASE,
)


def scrape_calendar(url: str = DEFAULT_URL) -> list[dict[str, str]]:
    """Return official Scottsdale City Council agendas from this month forward."""
    _validate_input_url(url)
    status, final_url, body = _fetch_bounded(make_session(), url)
    if status in {401, 403}:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Scottsdale official council page blocked the neutral paced request: "
            "status=%d final_url=%s missing_data_scope=all_current_agendas",
            status,
            final_url,
        )
        return []
    if status != 200:
        raise RuntimeError(f"Scottsdale official council page returned HTTP {status}: {final_url}")

    soup = BeautifulSoup(body, "html.parser")
    heading = soup.find(
        lambda tag: tag.name in {"h1", "h2"}
        and "city council agendas and minutes" in _clean(tag.get_text(" ", strip=True)).casefold()
    )
    year_folder = soup.find(
        lambda tag: tag.name in {"a", "button"}
        and _clean(tag.get_text(" ", strip=True)).casefold() == f"{date.today().year}-agendas"
    )
    if heading is None or year_folder is None:
        raise RuntimeError("Scottsdale official document-page fingerprint drifted")
    logger.info("Scottsdale official council-document fingerprint witnessed")

    cutoff = date.today().replace(day=1)
    stats: Counter[str] = Counter()
    meetings: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    year_path = f"/{date.today().year}-agendas/"
    for position, anchor in enumerate(soup.find_all("a", href=True), start=1):
        raw_href = str(anchor.get("href") or "")
        href_path = urlparse(raw_href).path
        if year_path.casefold() not in href_path.casefold():
            continue
        stats["agenda_candidates_seen"] += 1
        match = AGENDA_RE.search(href_path.rsplit("/", 1)[-1])
        if not match:
            stats["unrecognized_agenda_filename"] += 1
            logger.warning(
                "Scottsdale agenda link dropped: reason=unrecognized_filename position=%d label=%r href=%r",
                position,
                _clean(anchor.get_text(" ", strip=True)),
                raw_href,
            )
            continue
        meeting_date = _date_from_match(match, raw_href)
        if date.fromisoformat(meeting_date) < cutoff:
            stats["before_current_month"] += 1
            continue
        agenda_url = _safe_url(raw_href, final_url, position)
        if not agenda_url:
            stats["unsafe_agenda_url"] += 1
            continue
        if agenda_url in seen_urls:
            stats["duplicate_agenda_url"] += 1
            logger.warning("Scottsdale agenda link dropped: reason=duplicate url=%s", agenda_url)
            continue
        seen_urls.add(agenda_url)
        title = _title_from_slug(match.group("label"), position)
        meeting = {
            "meeting_title": title,
            "meeting_date": meeting_date,
            "meeting_time": "",
            "meeting_location": "",
            "meeting_status": "Agenda Available",
            "agenda_url": agenda_url,
            "minutes_url": "",
            "video_url": "",
            "agenda_packet_url": "",
            "ecomment_url": "",
            "meeting_id": "",
        }
        _validate_meeting(meeting)
        meetings.append(meeting)
        stats["rows_accepted"] += 1
        logger.info("Scottsdale meeting emitted: date=%s title=%r agenda=%s", meeting_date, title, agenda_url)

    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Scottsdale official current-year agenda folder has no council agendas from cutoff=%s: stats=%s",
            cutoff.isoformat(),
            dict(stats),
        )
    logger.warning(
        "Scottsdale scrape summary: candidates=%d accepted=%d drop_reasons=%s "
        "fields_absent_by_construction=%s",
        stats["agenda_candidates_seen"],
        stats["rows_accepted"],
        {key: value for key, value in stats.items() if key not in {"agenda_candidates_seen", "rows_accepted"}},
        {
            "meeting_time": stats["rows_accepted"],
            "meeting_location": stats["rows_accepted"],
            "minutes_url": stats["rows_accepted"],
            "video_url": stats["rows_accepted"],
            "agenda_packet_url": stats["rows_accepted"],
            "ecomment_url": stats["rows_accepted"],
            "meeting_id": stats["rows_accepted"],
        },
    )
    return meetings


def _validate_input_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        raise ValueError(f"Scottsdale parser called with disallowed URL: {url!r}")
    if not parsed.path.casefold().rstrip("/").endswith("/council/meeting-information/agendas-minutes"):
        raise ValueError(f"Scottsdale parser called with unexpected path: {url!r}")


def _fetch_bounded(session: Any, url: str) -> tuple[int, str, str]:
    with session.get(url, timeout=30, stream=True, allow_redirects=True) as response:
        final_host = (urlparse(response.url).hostname or "").lower()
        if final_host not in ALLOWED_HOSTS:
            raise ValueError(f"Scottsdale redirect reached disallowed host: {final_host}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Scottsdale response exceeded {MAX_RESPONSE_BYTES} bytes")
        return response.status_code, response.url, bytes(body).decode(response.encoding or "utf-8", "replace")


def _date_from_match(match: re.Match[str], href: str) -> str:
    try:
        return date(
            2000 + int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        ).isoformat()
    except ValueError as exc:
        raise RuntimeError(f"Scottsdale agenda filename contains invalid date: {href!r}") from exc


def _title_from_slug(slug: str, position: int) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", slug.casefold()).strip()
    normalized = re.sub(r"\b(?:marked|amended|agenda|revised|final|\d+)\b", " ", normalized)
    normalized = " ".join(normalized.split())
    if "special" in normalized and "regular" in normalized:
        kind = "Regular and Special"
    elif "regular" in normalized and "work study" in normalized:
        kind = "Regular and Work Study"
    elif "special" in normalized:
        kind = "Special"
    elif "regular" in normalized:
        kind = "Regular"
    elif "work study" in normalized:
        kind = "Work Study"
    else:
        raise RuntimeError(
            f"Scottsdale council agenda type drifted: position={position} filename_label={slug!r}"
        )
    return f"Scottsdale City Council {kind} Meeting"


def _safe_url(raw: str, base_url: str, position: int) -> str:
    lowered = raw.strip().casefold()
    if not raw or lowered.startswith(("//", "javascript:", "data:", "file:", "mailto:", "ftp:")):
        logger.warning(
            "Scottsdale agenda URL dropped: position=%d reason=empty_or_disallowed_scheme value=%r",
            position,
            raw,
        )
        return ""
    absolute = urljoin(base_url, raw)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        logger.warning(
            "Scottsdale agenda URL dropped: position=%d reason=disallowed_host value=%r",
            position,
            raw,
        )
        return ""
    return absolute


def _validate_meeting(meeting: dict[str, str]) -> None:
    if tuple(meeting) != FIELDS:
        raise RuntimeError(f"Scottsdale canonical schema drifted: {tuple(meeting)}")
    if any(not isinstance(value, str) for value in meeting.values()):
        raise RuntimeError(f"Scottsdale canonical values must be strings: {meeting!r}")


def _clean(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = unescape(str(value))
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return " ".join(text.split())
