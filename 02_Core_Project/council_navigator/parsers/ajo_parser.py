from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from polite_http import make_session


DEFAULT_URL = "https://www.ajowpccc.org/"
NOTES_URL = "https://www.ajowpccc.org/wpccc-meeting-notes"
FETCH_HOSTS = {"ajowpccc.org", "www.ajowpccc.org"}
MAX_RESPONSE_BYTES = 1_500_000
FIELD_NAMES = (
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
SLASH_DATE_RE = re.compile(r"\b(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])/(\d{2}|\d{4})\b")
MONTH_DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+([0-9]{1,2}),?\s+([0-9]{4})\b",
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r"(?<!\d)(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([AP])\.?M\.?(?=\s|$|[^\w.])",
    re.IGNORECASE,
)
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)

logger = logging.getLogger(__name__)


def scrape_calendar(calendar_url: str | None = None) -> list[dict[str, str]]:
    """Read the WPCCC announcement and official notes archive."""
    target = calendar_url or DEFAULT_URL
    _validate_source_url(target)
    with make_session() as session:
        home_html = _fetch_text_bounded(session, target)
        notes_html = _fetch_text_bounded(session, NOTES_URL)

    home = BeautifulSoup(home_html, "html.parser")
    notes = BeautifulSoup(notes_html, "html.parser")
    _validate_home_fingerprint(home)
    _validate_notes_fingerprint(notes)

    current_floor = date.today().replace(day=1).isoformat()
    meetings: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    next_heading = home.find(
        lambda tag: tag.name in {"h1", "h2", "h3"}
        and "next wpccc meeting" in _clean_text(tag).casefold()
    )
    if next_heading is not None:
        context_nodes = [next_heading]
        context_nodes.extend(next_heading.find_all_next(["h1", "h2", "h3", "p"], limit=4))
        context = " ".join(_clean_text(node) for node in context_nodes)
        meeting_date = _extract_date(context)
        if not meeting_date:
            raise RuntimeError(
                "Ajo WPCCC has a next-meeting announcement without an exact parseable date"
            )
        if meeting_date >= current_floor:
            meetings.append(_announcement_row(meeting_date, context))
            seen.add((meeting_date, ""))

    latest_note = ""
    for anchor in notes.find_all("a", href=True):
        label = _clean_text(anchor)
        href = anchor.get("href", "")
        if "meeting notes" not in label.casefold() or ("/s/" not in href and "|" not in label):
            continue
        meeting_date = _extract_date(f"{label} {href}")
        if not meeting_date:
            logger.warning(
                "Ajo meeting-notes row dropped: reason=exact_date_unparseable label=%r href=%r",
                label,
                href,
            )
            continue
        latest_note = max(latest_note, meeting_date)
        if meeting_date < current_floor:
            continue
        minutes_url = _emit_url(href, NOTES_URL, label)
        if not minutes_url:
            continue
        key = (meeting_date, minutes_url)
        if key in seen:
            continue
        seen.add(key)
        meeting = _empty_row()
        meeting.update(
            {
                "meeting_title": "Western Pima County Community Council Meeting",
                "meeting_date": meeting_date,
                "meeting_status": "Minutes Available",
                "minutes_url": minutes_url,
                "meeting_id": urlparse(minutes_url).path.rsplit("/", 1)[-1].removesuffix(".pdf"),
            }
        )
        meetings.append(meeting)

    meetings.sort(key=lambda item: (item["meeting_date"], item["meeting_time"]))
    _assert_schema(meetings)
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Ajo official WPCCC surfaces are accessible with no current-month-forward rows; "
            "latest_official_note=%s current_floor=%s",
            latest_note,
            current_floor,
        )
    return meetings


def _fetch_text_bounded(session: object, url: str) -> str:
    with session.get(url, timeout=35, stream=True, allow_redirects=True) as response:
        final_host = (urlparse(response.url).hostname or "").casefold()
        if final_host not in FETCH_HOSTS:
            raise ValueError(f"Ajo redirect reached disallowed host: {final_host}")
        body = bytearray()
        for chunk in response.iter_content(64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Ajo response exceeded {MAX_RESPONSE_BYTES} bytes")
        if response.status_code in {401, 403, 429}:
            logger.warning("health_empty_kind=source_blocked")
        response.raise_for_status()
        return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _validate_source_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() not in FETCH_HOSTS:
        raise ValueError("Ajo source must use HTTPS on the official WPCCC host")


def _validate_home_fingerprint(soup: BeautifulSoup) -> None:
    title = _clean_text(soup.title)
    visible = _clean_text(soup)
    if "Western Pima County Community Council" not in title or "monthly public meeting" not in visible:
        raise ValueError("Ajo WPCCC homepage fingerprint drifted")
    logger.info(
        "vendor fingerprint witness=Western_Pima_County_Community_Council_plus_monthly_public_meeting"
    )


def _validate_notes_fingerprint(soup: BeautifulSoup) -> None:
    title = _clean_text(soup.title)
    visible = _clean_text(soup)
    if "Meeting Notes" not in title or "Past Meeting Notes" not in visible:
        raise ValueError("Ajo WPCCC meeting-notes fingerprint drifted")


def _extract_date(value: str) -> str:
    match = SLASH_DATE_RE.search(value[:1000])
    if match:
        year = int(match.group(3))
        year = 2000 + year if year < 100 else year
        try:
            return date(year, int(match.group(1)), int(match.group(2))).isoformat()
        except ValueError:
            logger.warning("Ajo date extraction failed: reason=invalid_slash_date value=%r", value[:240])
            return ""
    match = MONTH_DATE_RE.search(value[:1000])
    if match:
        try:
            return datetime.strptime(" ".join(match.groups()), "%B %d %Y").date().isoformat()
        except ValueError:
            logger.warning("Ajo date extraction failed: reason=invalid_month_date value=%r", value[:240])
            return ""
    logger.warning("Ajo date extraction returned empty: reason=no_exact_date value=%r", value[:240])
    return ""


def _extract_time(value: str) -> str:
    match = TIME_RE.search(value[:1000])
    if not match:
        logger.warning("Ajo meeting_time absent: reason=no_visible_time")
        return ""
    return f"{int(match.group(1))}:{match.group(2) or '00'} {match.group(3).upper()}M"


def _extract_location(value: str) -> str:
    match = re.search(
        r"@\s*(?:1[0-2]|0?[1-9])(?::[0-5]\d)?\s*[AP]\.?M\.?\s*@\s*([^|;]+)",
        value[:1000],
        re.IGNORECASE,
    )
    if not match:
        logger.warning("Ajo meeting_location absent: reason=no_same-announcement_location")
        return ""
    return _clean_text(match.group(1)).strip(" .")


def _announcement_row(meeting_date: str, context: str) -> dict[str, str]:
    meeting = _empty_row()
    meeting.update(
        {
            "meeting_title": "Western Pima County Community Council Meeting",
            "meeting_date": meeting_date,
            "meeting_time": _extract_time(context),
            "meeting_location": _extract_location(context),
            "meeting_status": "Cancelled" if CANCELLED_RE.search(context) else "Scheduled",
            "meeting_id": f"wpccc-{meeting_date}",
        }
    )
    return meeting


def _emit_url(href: str, base_url: str, row_label: str) -> str:
    absolute = urljoin(base_url, str(href or "").strip())
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").casefold() not in FETCH_HOSTS:
        logger.warning(
            "Ajo URL dropped: row=%r href=%r reason=scheme_or_host_not_allowlisted",
            row_label,
            href,
        )
        return ""
    return absolute


def _empty_row() -> dict[str, str]:
    return {field: "" for field in FIELD_NAMES}


def _clean_text(value: object) -> str:
    return " ".join(BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True).split())


def _assert_schema(meetings: list[dict[str, str]]) -> None:
    for index, meeting in enumerate(meetings):
        if tuple(meeting) != FIELD_NAMES:
            raise ValueError(f"Ajo row {index} schema mismatch: {tuple(meeting)}")
        if any(not isinstance(value, str) for value in meeting.values()):
            raise ValueError(f"Ajo row {index} contains a non-string value")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    print(json.dumps(scrape_calendar(DEFAULT_URL), indent=2))
