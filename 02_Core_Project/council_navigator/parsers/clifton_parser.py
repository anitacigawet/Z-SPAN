from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from polite_http import make_session


logger = logging.getLogger(__name__)

BASE_URL = "https://cliftonaz.com"
DEFAULT_URL = f"{BASE_URL}/2026-meetings"
FETCH_HOSTS = {"cliftonaz.com", "www.cliftonaz.com"}
EMIT_HOSTS = FETCH_HOSTS | {"img1.wsimg.com"}
MAX_RESPONSE_BYTES = 2_000_000
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
DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"([0-9]{1,2}),?\s+([0-9]{4})",
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r"(?<!\d)([0-9]{1,2})(?::([0-9]{2}))?\s*([AP])\.?M\.?(?=\s|$|[^\w.])",
    re.IGNORECASE,
)
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
COUNCIL_RE = re.compile(r"\b(?:town|city)\s+council\b", re.IGNORECASE)
GENERIC_MEETING_RE = re.compile(
    r"^(?:(?:regular|special)\s+(?:meeting|session|work\s+session|study\s+session)|"
    r"(?:work|study)\s+session)$",
    re.IGNORECASE,
)
NON_COUNCIL_BODY_RE = re.compile(
    r"\b(?:committee|commission|board|authority|public\s+hearing)\b",
    re.IGNORECASE,
)


def scrape_calendar(url: str | None = None) -> list[dict[str, str]]:
    """Read current council agenda rows from Clifton's official annual page."""
    current_floor_date = date.today().replace(day=1)
    target = _current_year_url(url or DEFAULT_URL, current_floor_date.year)
    with make_session() as session:
        html = _fetch_text_bounded(session, target)
    soup = BeautifulSoup(html, "html.parser")
    page_title = _clean_text(soup.title)
    agenda_links = [
        anchor
        for anchor in soup.find_all("a", href=True)
        if "agenda" in _clean_text(anchor).lower()
    ]
    expected_title = f"{current_floor_date.year} Meetings"
    if expected_title not in page_title or not agenda_links:
        logger.warning(
            "vendor_fingerprint_failed expected=%r_plus_agenda_links title=%r agenda_links=%d",
            expected_title,
            page_title,
            len(agenda_links),
        )
        raise ValueError("Clifton annual meetings surface drifted")
    logger.info(
        "vendor_fingerprint witness=%r_plus_agenda_links agenda_links=%d",
        expected_title,
        len(agenda_links),
    )
    logger.warning(
        "field_absence fields=meeting_location,minutes_url,video_url,agenda_packet_url,ecomment_url "
        "reason=annual_download_list_exposes_agenda_documents_only"
    )

    current_floor = current_floor_date.isoformat()
    meetings: list[dict[str, str]] = []
    seen_keys: set[tuple[str, str]] = set()
    rows_seen = rows_dropped = historical = 0
    for anchor in soup.find_all("a", href=True):
        label = _clean_text(anchor)
        date_value = _extract_date(label)
        if not date_value:
            continue
        rows_seen += 1
        if date_value < current_floor:
            historical += 1
            continue
        lowered = label.lower()
        if "public notice" in lowered:
            rows_dropped += 1
            logger.warning(
                "drop_row reason=public_notice_companion_not_meeting date=%s label=%r",
                date_value,
                label,
            )
            continue
        if "agenda" not in lowered:
            rows_dropped += 1
            logger.warning(
                "drop_row reason=not_agenda_document date=%s label=%r",
                date_value,
                label,
            )
            continue
        title = _title_from_label(label)
        if NON_COUNCIL_BODY_RE.search(title):
            rows_dropped += 1
            logger.warning(
                "drop_row reason=explicit_non_council_body date=%s label=%r",
                date_value,
                label,
            )
            continue
        if COUNCIL_RE.search(title):
            logger.info(
                "governing_body_witness=explicit_town_or_city_council date=%s label=%r",
                date_value,
                label,
            )
        elif GENERIC_MEETING_RE.fullmatch(title):
            # The annual official page uses bare Regular/Special Meeting,
            # Regular/Special Session, and Work/Study Session for Town Council
            # rows while naming every subordinate body in its own link label.
            logger.info(
                "governing_body_witness=official_default_session_vocabulary "
                "date=%s title=%r subordinate_body_signal=absent",
                date_value,
                title,
            )
        else:
            raise RuntimeError(
                "Clifton current agenda label is governing-body ambiguous; "
                f"unrecognized current descriptor: {label!r}"
            )
        agenda_url = _emit_url(anchor.get("href", ""), target, label)
        if not agenda_url:
            rows_dropped += 1
            continue
        key = (date_value, title.casefold())
        if key in seen_keys:
            rows_dropped += 1
            logger.warning(
                "drop_row reason=duplicate_council_agenda date=%s title=%r url=%s",
                date_value,
                title,
                agenda_url,
            )
            continue
        seen_keys.add(key)
        id_match = re.search(r"/downloads/([0-9a-f-]{20,})/", urlparse(agenda_url).path, re.IGNORECASE)
        meeting_id = id_match.group(1) if id_match else ""
        if not meeting_id:
            logger.warning("meeting_id_absent date=%s title=%r url=%s", date_value, title, agenda_url)
        status = "Cancelled" if CANCELLED_RE.search(title) else "Agenda Available"
        meeting = {
            "meeting_title": title,
            "meeting_date": date_value,
            "meeting_time": _extract_time(label),
            "meeting_location": "",
            "meeting_status": status,
            "agenda_url": agenda_url,
            "minutes_url": "",
            "video_url": "",
            "agenda_packet_url": "",
            "ecomment_url": "",
            "meeting_id": meeting_id,
        }
        meetings.append({field: meeting[field] for field in CANONICAL_FIELDS})

    _assert_schema(meetings)
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
    logger.info(
        "scrape_summary rows_seen=%d rows_accepted=%d rows_dropped=%d historical_ignored=%d current_floor=%s",
        rows_seen,
        len(meetings),
        rows_dropped,
        historical,
        current_floor,
    )
    return meetings


def _fetch_text_bounded(session: requests.Session, url: str) -> str:
    with session.get(url, timeout=30, stream=True, allow_redirects=True) as response:
        if _host(response.url) not in FETCH_HOSTS:
            raise ValueError(f"Clifton redirect reached disallowed host: {_host(response.url)}")
        body = bytearray()
        for chunk in response.iter_content(64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Clifton response exceeded {MAX_RESPONSE_BYTES} bytes")
        if response.status_code in {401, 403, 429}:
            logger.warning("health_empty_kind=source_blocked")
        response.raise_for_status()
        return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _current_year_url(url: str, year: int) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or _host(url) not in FETCH_HOSTS:
        raise ValueError("Clifton calendar URL must use HTTPS on the official town host")
    expected_path = f"/{year}-meetings"
    if parsed.path.rstrip("/").casefold() == expected_path.casefold():
        return url
    replacement = f"{BASE_URL}{expected_path}"
    logger.warning(
        "registry_url_stale supplied_url=%s replacement_url=%s reason=annual_page_rollover",
        url,
        replacement,
    )
    return replacement


def _emit_url(href: str, base_url: str, row_label: str) -> str:
    absolute = urljoin(base_url, str(href or "").strip())
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or _host(absolute) not in EMIT_HOSTS:
        logger.warning(
            "drop_url field=agenda_url row=%r href=%r reason=scheme_or_host_not_allowlisted",
            row_label,
            href,
        )
        return ""
    return absolute


def _extract_date(text: str) -> str:
    match = DATE_RE.search(text[:500])
    if not match:
        return ""
    try:
        return datetime.strptime(" ".join(match.groups()), "%B %d %Y").date().isoformat()
    except ValueError:
        logger.warning("meeting_date_unparseable text=%r", text[:240])
        return ""


def _extract_time(text: str) -> str:
    match = TIME_RE.search(text[:500])
    if not match:
        if re.search(r"\b(?:a\.?m\.?|p\.?m\.?)\b", text[:500], re.IGNORECASE):
            logger.warning("meeting_time_unparseable text=%r", text[:240])
        else:
            logger.info("meeting_time_absent reason=no_visible_time text=%r", text[:240])
        return ""
    hour = int(match.group(1))
    if not 1 <= hour <= 12:
        logger.warning("meeting_time_invalid raw=%r text=%r", match.group(0), text[:240])
        return ""
    return f"{hour}:{match.group(2) or '00'} {match.group(3).upper()}M"


def _title_from_label(label: str) -> str:
    descriptor = DATE_RE.sub("", label, count=1)
    descriptor = re.sub(r"\(\s*(?:pdf|docx?|download)\s*\)", "", descriptor, flags=re.IGNORECASE)
    descriptor = re.sub(r"\bDownload\b", "", descriptor, flags=re.IGNORECASE)
    descriptor = re.sub(r"\bAgenda\b", "", descriptor, flags=re.IGNORECASE)
    descriptor = " ".join(descriptor.strip(" -\u2013\u2014").split())
    if not descriptor:
        raise RuntimeError(f"Clifton title extraction produced an empty descriptor: {label!r}")
    return descriptor


def _clean_text(value: object) -> str:
    return " ".join(BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True).split())


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _assert_schema(meetings: list[dict[str, str]]) -> None:
    for index, meeting in enumerate(meetings):
        if tuple(meeting) != CANONICAL_FIELDS:
            raise ValueError(f"Clifton row {index} schema mismatch: {tuple(meeting)}")
        if any(not isinstance(value, str) for value in meeting.values()):
            raise ValueError(f"Clifton row {index} contains a non-string value")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    print(json.dumps(scrape_calendar(DEFAULT_URL), indent=2))
