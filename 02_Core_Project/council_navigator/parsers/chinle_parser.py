from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from pathlib import PurePosixPath
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from polite_http import make_session


DEFAULT_URL = "https://chinle.navajochapters.org/records/"
FETCH_HOSTS = {"chinle.navajochapters.org"}
MAX_RESPONSE_BYTES = 2_000_000
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
CATEGORY_RE = re.compile(r"^(\d{4})\s+(planning|regular|special)\s+meetings?$", re.IGNORECASE)
MONTH_DATE_RE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"[-_ .]+([0-9]{1,2})[-_, .]+([0-9]{4})\b",
    re.IGNORECASE,
)
NUMERIC_DATE_RE = re.compile(r"\b(0?[1-9]|1[0-2])[-_.](0?[1-9]|[12]\d|3[01])[-_.](\d{4})\b")
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)

logger = logging.getLogger(__name__)

def parse_date_from_url(url: str) -> str:
    """Return only a complete day-level date witnessed in the document URL."""
    value = unquote(url)[:1200]
    match = MONTH_DATE_RE.search(value)
    if match:
        try:
            return datetime.strptime(" ".join(match.groups()), "%b %d %Y").date().isoformat()
        except ValueError:
            try:
                return datetime.strptime(" ".join(match.groups()), "%B %d %Y").date().isoformat()
            except ValueError:
                logger.warning("Chinle URL date is invalid: url=%r", url[:300])
                return ""
    match = NUMERIC_DATE_RE.search(value)
    if match:
        try:
            return date(int(match.group(3)), int(match.group(1)), int(match.group(2))).isoformat()
        except ValueError:
            logger.warning("Chinle numeric URL date is invalid: url=%r", url[:300])
            return ""
    logger.warning("Chinle URL date extraction returned empty: url=%r", url[:300])
    return ""

def parse_date_from_text(text: str) -> str:
    """Return an exact date from visible text; never invent a month-first date."""
    match = MONTH_DATE_RE.search(text[:1000])
    if not match:
        logger.warning("Chinle visible date extraction returned empty: text=%r", text[:240])
        return ""
    for fmt in ("%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(" ".join(match.groups()), fmt).date().isoformat()
        except ValueError:
            continue
    logger.warning("Chinle visible date is invalid: text=%r", text[:240])
    return ""

def scrape_calendar(url: str = DEFAULT_URL) -> list[dict[str, str]]:
    """Read current Chinle Chapter planning, regular, and special meetings."""
    _validate_source_url(url)
    with make_session() as session:
        html = _fetch_text_bounded(session, url)
    soup = BeautifulSoup(html, "html.parser")
    recognized = _recognized_toggles(soup)
    _validate_fingerprint(soup, recognized)

    current_floor = date.today().replace(day=1).isoformat()
    current_year = int(current_floor[:4])
    meetings: list[dict[str, str]] = []
    latest_archive_year = 0
    rows_seen = rows_dropped = 0
    for category_year, category_kind, content in recognized:
        latest_archive_year = max(latest_archive_year, category_year)
        if category_year < current_year:
            continue
        links = content.find_all("a", href=True)
        for anchor in links:
            rows_seen += 1
            label = _clean_text(anchor)
            document_url = _emit_url(anchor.get("href", ""), url, label)
            if not document_url:
                rows_dropped += 1
                continue
            meeting_date = parse_date_from_url(document_url) or parse_date_from_text(label)
            if not meeting_date:
                raise RuntimeError(
                    "Chinle current/future meeting record lacks an exact day-level date: "
                    f"category={category_year} {category_kind} label={label!r}"
                )
            if meeting_date < current_floor:
                continue
            meeting = _empty_row()
            meeting.update(
                {
                    "meeting_title": label or f"{category_kind.title()} Meeting",
                    "meeting_date": meeting_date,
                    "meeting_status": (
                        "Cancelled" if CANCELLED_RE.search(label) else "Agenda Available"
                    ),
                    "agenda_url": document_url,
                    "meeting_id": PurePosixPath(urlparse(document_url).path).stem,
                }
            )
            meetings.append(meeting)

    meetings.sort(key=lambda item: (item["meeting_date"], item["meeting_title"]))
    _assert_schema(meetings)
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Chinle official Meeting Records surface has no current-month-forward chapter rows; "
            "latest_archive_year=%d current_floor=%s",
            latest_archive_year,
            current_floor,
        )
    logger.info(
        "Chinle scrape summary: recognized_categories=%d rows_seen=%d rows_accepted=%d "
        "rows_dropped=%d",
        len(recognized),
        rows_seen,
        len(meetings),
        rows_dropped,
    )
    return meetings


def _fetch_text_bounded(session: object, url: str) -> str:
    with session.get(url, timeout=35, stream=True, allow_redirects=True) as response:
        final_host = (urlparse(response.url).hostname or "").casefold()
        if final_host not in FETCH_HOSTS:
            raise ValueError(f"Chinle redirect reached disallowed host: {final_host}")
        body = bytearray()
        for chunk in response.iter_content(64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Chinle response exceeded {MAX_RESPONSE_BYTES} bytes")
        if response.status_code in {401, 403, 429}:
            logger.warning("health_empty_kind=source_blocked")
        response.raise_for_status()
        return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _validate_source_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() not in FETCH_HOSTS:
        raise ValueError("Chinle source must use HTTPS on the official chapter host")


def _recognized_toggles(soup: BeautifulSoup) -> list[tuple[int, str, object]]:
    recognized: list[tuple[int, str, object]] = []
    for toggle in soup.select("div.et_pb_toggle"):
        title_tag = toggle.select_one("h5.et_pb_toggle_title")
        content = toggle.select_one("div.et_pb_toggle_content")
        if title_tag is None or content is None:
            logger.warning("Chinle toggle dropped: reason=missing_title_or_content")
            continue
        title = _clean_text(title_tag)
        match = CATEGORY_RE.fullmatch(title)
        if not match:
            logger.warning("Chinle toggle dropped: reason=unrecognized_category title=%r", title)
            continue
        recognized.append((int(match.group(1)), match.group(2).casefold(), content))
    return recognized


def _validate_fingerprint(
    soup: BeautifulSoup,
    recognized: list[tuple[int, str, object]],
) -> None:
    title = _clean_text(soup.title)
    visible = _clean_text(soup)
    if "Meeting Records" not in title or "Chinle Chapter" not in visible or not recognized:
        raise ValueError("Chinle Divi Meeting Records fingerprint drifted")
    logger.info(
        "vendor fingerprint witness=Chinle_Chapter_plus_Meeting_Records_plus_Divi_toggles"
    )


def _emit_url(href: str, base_url: str, row_label: str) -> str:
    absolute = urljoin(base_url, str(href or "").strip())
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").casefold() not in FETCH_HOSTS:
        logger.warning(
            "Chinle URL dropped: row=%r href=%r reason=scheme_or_host_not_allowlisted",
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
            raise ValueError(f"Chinle row {index} schema mismatch: {tuple(meeting)}")
        if any(not isinstance(value, str) for value in meeting.values()):
            raise ValueError(f"Chinle row {index} contains a non-string value")

if __name__ == '__main__':
    # URL for the Chinle Chapter Government Meeting Records
    CALENDAR_URL = "https://chinle.navajochapters.org/records/"

    print(f"Scraping meetings from: {CALENDAR_URL}")
    all_meetings = scrape_calendar(CALENDAR_URL)

    # Print a summary of the results
    print(f"\nFound {len(all_meetings)} meetings.")
    if all_meetings:
        print("\nSample Meetings (first 5):")
        for i, m in enumerate(all_meetings[:5]):
            print(f"--- Meeting {i+1} ---")
            for key, value in m.items():
                if value:
                    print(f"{key}: {value}")

        # Print a sample with a known date issue from the previous run to verify fix
        print("\nSample Meeting (Jan 2, 2024):")
        for m in all_meetings:
            if m.get('meeting_date') == '2024-01-02':
                print(f"--- Verified Meeting ---")
                for key, value in m.items():
                    if value:
                        print(f"{key}: {value}")
                break
