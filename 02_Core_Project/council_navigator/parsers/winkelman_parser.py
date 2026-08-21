"""Town of Winkelman council documents from the official meeting-minutes page."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from html import unescape
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from polite_http import make_session

__all__ = ["scrape_calendar"]

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://winkelmanaz.gov/meeting-minutes/"
ALLOWED_HOSTS = {"winkelmanaz.gov", "www.winkelmanaz.gov"}
MAX_RESPONSE_BYTES = 4_000_000
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
DATE_RE = re.compile(r"^\s*(\d{1,2}/\d{1,2}/\d{2,4})\s*$")


def scrape_calendar(url: str = DEFAULT_URL) -> list[dict[str, str]]:
    """Return official Winkelman council documents from this calendar month forward."""
    _validate_input_url(url)
    status, final_url, body = _fetch_bounded(make_session(), url)
    if status in {401, 403}:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Winkelman official meeting-minutes page blocked the neutral paced request: "
            "status=%d final_url=%s missing_data_scope=all_current_council_documents",
            status,
            final_url,
        )
        return []
    if status != 200:
        raise RuntimeError(f"Winkelman official meeting-minutes page returned HTTP {status}: {final_url}")

    soup = BeautifulSoup(body, "html.parser")
    title = _clean(soup.title.get_text(" ", strip=True) if soup.title else "")
    candidates = [anchor for anchor in soup.find_all("a", href=True) if DATE_RE.fullmatch(_clean(anchor.get_text(" ", strip=True)))]
    if "meeting minutes" not in title.casefold() or not candidates:
        raise RuntimeError(
            f"Winkelman official dated-document fingerprint drifted: "
            f"title={title!r} dated_links={len(candidates)}"
        )
    logger.info("Winkelman official dated-document fingerprint witnessed: links=%d", len(candidates))

    cutoff = date.today().replace(day=1)
    stats: Counter[str] = Counter()
    meetings: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for position, anchor in enumerate(candidates, start=1):
        stats["dated_links_seen"] += 1
        raw_date = _clean(anchor.get_text(" ", strip=True))
        meeting_date = _parse_date(raw_date, position)
        if date.fromisoformat(meeting_date) < cutoff:
            stats["before_current_month"] += 1
            continue
        raw_href = _clean(anchor.get("href"))
        document_url = _safe_url(raw_href, final_url, position)
        if not document_url:
            stats["unsafe_document_url"] += 1
            continue
        field = _document_field(document_url, position)
        key = (meeting_date, field)
        if key in seen:
            stats["duplicate"] += 1
            logger.warning("Winkelman row dropped: reason=duplicate position=%d key=%r", position, key)
            continue
        seen.add(key)
        agenda_url = document_url if field == "agenda_url" else ""
        minutes_url = document_url if field == "minutes_url" else ""
        meeting = {
            "meeting_title": "Winkelman Town Council Meeting",
            "meeting_date": meeting_date,
            "meeting_time": "",
            "meeting_location": "",
            "meeting_status": "Minutes Available" if minutes_url else "Agenda Available",
            "agenda_url": agenda_url,
            "minutes_url": minutes_url,
            "video_url": "",
            "agenda_packet_url": "",
            "ecomment_url": "",
            "meeting_id": "",
        }
        _validate_meeting(meeting)
        meetings.append(meeting)
        stats["rows_accepted"] += 1
        logger.info(
            "Winkelman meeting emitted: date=%s field=%s document=%s",
            meeting_date,
            field,
            document_url,
        )

    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Winkelman witnessed official dated-document archive has no rows from cutoff=%s: stats=%s",
            cutoff.isoformat(),
            dict(stats),
        )
    logger.warning(
        "Winkelman scrape summary: dated_links=%d accepted=%d drop_reasons=%s "
        "fields_absent_by_construction=%s",
        stats["dated_links_seen"],
        stats["rows_accepted"],
        {key: value for key, value in stats.items() if key not in {"dated_links_seen", "rows_accepted"}},
        {
            "meeting_time": stats["rows_accepted"],
            "meeting_location": stats["rows_accepted"],
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
        raise ValueError(f"Winkelman parser called with disallowed URL: {url!r}")
    if not parsed.path.casefold().rstrip("/").endswith("/meeting-minutes"):
        raise ValueError(f"Winkelman parser called with unexpected path: {url!r}")


def _fetch_bounded(session: Any, url: str) -> tuple[int, str, str]:
    with session.get(url, timeout=30, stream=True, allow_redirects=True) as response:
        final_host = (urlparse(response.url).hostname or "").lower()
        if final_host not in ALLOWED_HOSTS:
            raise ValueError(f"Winkelman redirect reached disallowed host: {final_host}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Winkelman response exceeded {MAX_RESPONSE_BYTES} bytes")
        return response.status_code, response.url, bytes(body).decode(response.encoding or "utf-8", "replace")


def _parse_date(raw: str, position: int) -> str:
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    raise RuntimeError(f"Winkelman dated link has invalid date: position={position} value={raw!r}")


def _document_field(document_url: str, position: int) -> str:
    path = urlparse(document_url).path.casefold()
    filename = path.rsplit("/", 1)[-1]
    if "agenda" in filename:
        return "agenda_url"
    if "minutes" in filename or re.search(r"(?:^|[_-])mm(?:[_.-]|$)", filename):
        return "minutes_url"
    raise RuntimeError(
        f"Winkelman current dated-document vocabulary drifted: position={position} url={document_url!r}"
    )


def _safe_url(raw: str, base_url: str, position: int) -> str:
    if not raw or raw.casefold().startswith(("//", "javascript:", "data:", "file:", "mailto:", "ftp:")):
        logger.warning(
            "Winkelman URL dropped: position=%d reason=empty_or_disallowed_scheme value=%r",
            position,
            raw,
        )
        return ""
    absolute = urljoin(base_url, raw)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        logger.warning(
            "Winkelman URL dropped: position=%d reason=disallowed_host value=%r",
            position,
            raw,
        )
        return ""
    return absolute


def _validate_meeting(meeting: dict[str, str]) -> None:
    if tuple(meeting) != FIELDS:
        raise RuntimeError(f"Winkelman canonical schema drifted: {tuple(meeting)}")
    if any(not isinstance(value, str) for value in meeting.values()):
        raise RuntimeError(f"Winkelman canonical values must be strings: {meeting!r}")


def _clean(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = unescape(str(value))
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return " ".join(text.split())
