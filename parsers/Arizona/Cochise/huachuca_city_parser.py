"""Huachuca City — WordPress m1.DownloadList meeting parser."""

from __future__ import annotations

import base64
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag


DEFAULT_CALENDAR_URL = (
    "https://huachucacityaz.gov/town-government/council-agendas-minutes/"
    "?d=LzIwMjY%3D&m1dll_index_get=6"
)
ALLOWED_HOSTS = {
    "huachucacityaz.gov",
    "www.huachucacityaz.gov",
}
BAD_SCHEMES = (
    "javascript:",
    "data:",
    "vbscript:",
    "file:",
    "mailto:",
    "ftp:",
    "gopher:",
)
M1DLL_INDEX_GET = "6"
MAX_CALENDAR_BYTES = 5_000_000
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
DATE_PATTERNS = (
    re.compile(r"(?P<month>\d{1,2})[-_](?P<day>\d{1,2})[-_](?P<year>\d{4})"),
    re.compile(r"(?P<year>\d{4})[-_](?P<month>\d{1,2})[-_](?P<day>\d{1,2})"),
)
CANCELLED_RE = re.compile(r"\bcancell?ed\b", re.IGNORECASE)
DOCUMENT_KEYWORDS = {
    "agenda",
    "minutes",
    "packet",
    "special",
    "regular",
    "study",
    "session",
    "public",
    "hearing",
    "workshop",
}
GENERIC_FILENAME_WORDS = {
    "amended",
    "city",
    "council",
    "draft",
    "final",
    "huachuca",
    "meeting",
    "revised",
    "town",
}
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

logger = logging.getLogger(__name__)


@dataclass
class MeetingGroup:
    meeting_date: str
    meeting_title: str
    meeting_id: str
    agenda_url: str = ""
    minutes_url: str = ""
    agenda_packet_url: str = ""
    labels: list[str] = field(default_factory=list)


def scrape_calendar(calendar_url: str) -> list[dict]:
    """Scrape Huachuca City council agenda/minutes PDF links into canonical rows."""
    logger.info("meeting_time and meeting_location are honest-empty for this vendor; the file list has no same-row time or location evidence")
    logger.info("m1dll_index_get=%s is a hardcoded council directory selector and may need updating if the directory moves", M1DLL_INDEX_GET)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    current_year = datetime.now().year
    for attempt, year in enumerate((current_year, current_year - 1), start=1):
        year_url = _build_year_url(calendar_url, year)
        logger.info("computed_year=%s built_url=%s", year, year_url)

        html, final_url = _fetch_text_bounded(session, year_url)
        if html == "":
            logger.warning("architectural blocker: fetch failed for url=%s; returning []", year_url)
            return []

        soup = BeautifulSoup(html, "html.parser")
        fingerprint_tokens = _vendor_fingerprint_tokens(soup, final_url)
        if not fingerprint_tokens:
            logger.warning("vendor NOT confirmed by markup for url=%s; returning []", final_url)
            return []
        logger.info("vendor fingerprint confirmed tokens=%s", ", ".join(fingerprint_tokens))

        rows, rows_dropped = _extract_rows_from_soup(soup, final_url)
        if rows:
            logger.info("scrape complete rows_emitted=%s rows_dropped=%s", len(rows), rows_dropped)
            return rows

        if attempt == 1:
            logger.warning(
                "current year url returned 0 rows; retrying prior year year=%s url=%s",
                current_year - 1,
                _build_year_url(calendar_url, current_year - 1),
            )
        else:
            logger.warning("both current and prior year attempts returned 0 rows; returning []")

    logger.info("scrape complete rows_emitted=%s rows_dropped=%s", 0, 0)
    return []


def _build_year_url(calendar_url: str, year: int) -> str:
    parsed = urlparse(calendar_url or DEFAULT_CALENDAR_URL)
    host = parsed.netloc.lower()
    if host not in ALLOWED_HOSTS:
        logger.warning("calendar_url host is not allowed host=%s url=%s", host, calendar_url)
        return ""
    d_param = base64.b64encode(f"/{year}".encode()).decode().rstrip("=")
    base_path = parsed.path or "/town-government/council-agendas-minutes/"
    return f"{parsed.scheme}://{parsed.netloc}{base_path}?d={d_param}%3D&m1dll_index_get={M1DLL_INDEX_GET}"


def _fetch_text_bounded(session: requests.Session, url: str) -> tuple[str, str]:
    if not url:
        logger.warning("fetch skipped because url is empty")
        return "", ""
    try:
        with session.get(url, timeout=30, stream=True, allow_redirects=True, verify=True) as response:
            final_host = urlparse(response.url).netloc.split(":")[0].lower()
            if final_host not in ALLOWED_HOSTS:
                logger.warning("redirect to disallowed host final_host=%s started_url=%s final_url=%s", final_host, url, response.url)
                return "", response.url
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > MAX_CALENDAR_BYTES:
                    logger.warning("response from %s exceeded %s bytes", response.url, MAX_CALENDAR_BYTES)
                    return "", response.url
            return bytes(body).decode(response.encoding or "utf-8", errors="replace"), response.url
    except requests.RequestException as exc:
        logger.warning("fetch failed url=%s blocker=%s", url, exc, exc_info=True)
        return "", url


def _vendor_fingerprint_tokens(soup: BeautifulSoup, page_url: str) -> list[str]:
    tokens: list[str] = []
    if soup.find(class_=_class_contains_m1dll):
        tokens.append("class:m1downloadlist-or-m1dll")
    if "m1dll_index_get" in {key for key in _query_keys(page_url)}:
        tokens.append("url-param:m1dll_index_get")
    if soup.find("a", href=lambda href: bool(href and ".pdf" in href.lower() and "_wpnonce=" in href)):
        tokens.append("pdf-href:_wpnonce")
    if soup.find(src=lambda src: bool(src and "/wp-content/plugins/m1downloadlist/" in src)):
        tokens.append("asset:/wp-content/plugins/m1downloadlist/")
    if soup.find(href=lambda href: bool(href and "/wp-content/plugins/m1downloadlist/" in href)):
        tokens.append("asset:/wp-content/plugins/m1downloadlist/")
    return sorted(set(tokens))


def _query_keys(url: str) -> list[str]:
    return [part.split("=", 1)[0] for part in urlparse(url).query.split("&") if part]


def _class_contains_m1dll(class_value: object) -> bool:
    if not class_value:
        return False
    if isinstance(class_value, str):
        class_names = [class_value]
    else:
        class_names = [str(value) for value in class_value]
    return any("m1downloadlist" in name.lower() or "m1dll" in name.lower() for name in class_names)


def _extract_rows_from_soup(soup: BeautifulSoup, page_url: str) -> tuple[list[dict], int]:
    container = _find_filelist_container(soup)
    anchors = [anchor for anchor in container.find_all("a", href=True) if ".pdf" in str(anchor.get("href", "")).lower()]
    if container is not soup and not anchors:
        logger.warning("m1dll_filelist container found but yielded 0 PDF links; container structure may have changed")
    logger.info("pdf link scan rows_seen=%s", len(anchors))

    groups: dict[str, MeetingGroup] = {}
    rows_seen = 0
    rows_accepted = 0
    rows_dropped = 0

    for anchor in anchors:
        rows_seen += 1
        raw_href = str(anchor.get("href", ""))
        link_text = _clean_text(anchor)
        filename = _filename_from_href(raw_href)
        row_id = filename or link_text or f"pdf-link-{rows_seen}"
        logger.info("row iteration rows_seen=%s row_id=%s raw_href=%s", rows_seen, row_id, raw_href)

        pdf_url = _emit_url(raw_href, page_url, "document_url", row_id)
        if pdf_url == "":
            rows_dropped += 1
            logger.warning("row dropped row_id=%s reason=document_url rejected rows_accepted=%s rows_dropped=%s", row_id, rows_accepted, rows_dropped)
            continue

        date_source = filename or link_text
        meeting_date = _parse_date_from_filename(date_source, row_id)
        if meeting_date == "":
            rows_dropped += 1
            logger.warning("row dropped row_id=%s reason=date parse failed filename=%s rows_accepted=%s rows_dropped=%s", row_id, date_source, rows_accepted, rows_dropped)
            continue
        logger.info("parsed filename=%s meeting_date=%s", date_source, meeting_date)

        label = link_text or filename
        _warn_unknown_keywords(label, row_id)
        field_name = _classify_pdf_field(label, row_id)
        if field_name == "":
            rows_dropped += 1
            logger.warning("row dropped row_id=%s reason=pdf classification ambiguous label=%s rows_accepted=%s rows_dropped=%s", row_id, label, rows_accepted, rows_dropped)
            continue

        group = groups.get(meeting_date)
        if group is None:
            group = MeetingGroup(
                meeting_date=meeting_date,
                meeting_title=_derive_meeting_title(label),
                meeting_id=meeting_date,
            )
            groups[meeting_date] = group
        group.labels.append(label)

        existing_value = getattr(group, field_name)
        if existing_value:
            rows_dropped += 1
            logger.warning(
                "duplicate document dropped row_id=%s field=%s kept=%s dropped=%s rows_accepted=%s rows_dropped=%s",
                row_id,
                field_name,
                existing_value,
                pdf_url,
                rows_accepted,
                rows_dropped,
            )
            continue

        setattr(group, field_name, pdf_url)
        rows_accepted += 1
        logger.info(
            "row accepted row_id=%s meeting_date=%s field=%s rows_accepted=%s rows_dropped=%s",
            row_id,
            meeting_date,
            field_name,
            rows_accepted,
            rows_dropped,
        )

    emitted = [_group_to_row(group) for group in sorted(groups.values(), key=lambda item: item.meeting_date, reverse=True)]
    logger.info(
        "decision loop complete rows_seen=%s rows_accepted=%s rows_dropped=%s meeting_rows=%s",
        rows_seen,
        rows_accepted,
        rows_dropped,
        len(emitted),
    )
    return emitted, rows_dropped


def _find_filelist_container(soup: BeautifulSoup) -> Tag | BeautifulSoup:
    candidates = soup.find_all(class_=_class_contains_m1dll)
    for candidate in candidates:
        if not isinstance(candidate, Tag):
            continue
        pdf_count = len(candidate.find_all("a", href=lambda href: bool(href and ".pdf" in href.lower())))
        if pdf_count:
            logger.info("m1downloadlist container found class=%s pdf_links=%s", candidate.get("class", ""), pdf_count)
            return candidate
    if candidates:
        first_candidate = candidates[0]
        if isinstance(first_candidate, Tag):
            logger.info("m1downloadlist container found class=%s pdf_links=0", first_candidate.get("class", ""))
            return first_candidate
    logger.warning("m1downloadlist filelist container not found; falling back to full-page PDF scan after vendor fingerprint confirmation")
    return soup


def _clean_text(tag: Tag) -> str:
    return tag.get_text(" ", strip=True)


def _filename_from_href(raw_href: str) -> str:
    parsed = urlparse(raw_href)
    filename = unquote(PurePosixPath(parsed.path).name)
    if filename == "":
        logger.warning("filename extraction returned empty for href=%s reason=path has no basename", raw_href)
    return filename


def _emit_url(raw_href: str, base_url: str, field: str, row_id: str) -> str:
    if not raw_href:
        logger.warning("url rejected field=%s row_id=%s raw_href=%s reason=empty href", field, row_id, raw_href)
        return ""

    stripped = raw_href.strip()
    low = stripped.lower()
    for bad_scheme in BAD_SCHEMES:
        if low.startswith(bad_scheme):
            logger.warning("url rejected field=%s row_id=%s raw_href=%s reason=bad scheme %s", field, row_id, raw_href, bad_scheme)
            return ""

    absolute = urljoin(base_url, stripped)
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https"):
        logger.warning("url rejected field=%s row_id=%s raw_href=%s reason=non-http scheme %s", field, row_id, raw_href, parsed.scheme)
        return ""

    emit_host = parsed.netloc.split(":")[0].lower()
    if emit_host not in ALLOWED_HOSTS:
        logger.warning("url rejected field=%s row_id=%s raw_href=%s reason=host not allowed host=%s", field, row_id, raw_href, emit_host)
        return ""

    return absolute


def _parse_date_from_filename(filename: str, row_id: str) -> str:
    for pattern in DATE_PATTERNS:
        match = pattern.search(filename)
        if not match:
            continue
        try:
            parsed = datetime(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            )
        except ValueError as exc:
            logger.warning("date parse failed row_id=%s filename=%s reason=%s", row_id, filename, exc)
            return ""
        return parsed.strftime("%Y-%m-%d")
    logger.warning("date parse failed row_id=%s filename=%s reason=no supported date pattern", row_id, filename)
    return ""


def _warn_unknown_keywords(label: str, row_id: str) -> None:
    label_without_ext = re.sub(r"\.pdf$", "", label, flags=re.IGNORECASE)
    label_without_dates = re.sub(r"\b\d{1,4}[-_]\d{1,2}[-_]\d{1,4}\b", " ", label_without_ext)
    words = re.findall(r"[A-Za-z]+", label_without_dates)
    unknown = sorted(
        {
            word.lower()
            for word in words
            if word.lower() not in DOCUMENT_KEYWORDS and word.lower() not in GENERIC_FILENAME_WORDS
        }
    )
    if unknown:
        logger.warning("unrecognized filename keyword row_id=%s label=%s unknown_keywords=%s", row_id, label, ", ".join(unknown))


def _classify_pdf_field(label: str, row_id: str) -> str:
    lowered = label.lower()
    if "minutes" in lowered:
        return "minutes_url"
    if "packet" in lowered:
        return "agenda_packet_url"
    if "agenda" in lowered:
        return "agenda_url"
    logger.warning("pdf classification ambiguous row_id=%s label=%s reason=no Agenda/Minutes/Packet keyword", row_id, label)
    return ""


def _derive_meeting_title(label: str) -> str:
    lowered = label.lower()
    if "study session" in lowered:
        title = "Town Council Study Session"
    elif "public hearing" in lowered:
        title = "Town Council Public Hearing"
    elif "workshop" in lowered:
        title = "Town Council Workshop"
    elif "work session" in lowered:
        title = "Town Council Work Session"
    elif "special" in lowered:
        title = "Special Town Council Meeting"
    elif "regular" in lowered:
        title = "Regular Town Council Meeting"
    else:
        title = "Town Council Meeting"
    if CANCELLED_RE.search(label) and not CANCELLED_RE.search(title):
        title = f"Cancelled {title}"
    return BeautifulSoup(title, "html.parser").get_text(" ", strip=True)


def _group_to_row(group: MeetingGroup) -> dict:
    status = _meeting_status(group)
    row = {
        "meeting_title": group.meeting_title,
        "meeting_date": group.meeting_date,
        "meeting_time": "",
        "meeting_location": "",
        "meeting_status": status,
        "agenda_url": group.agenda_url,
        "minutes_url": group.minutes_url,
        "video_url": "",
        "agenda_packet_url": group.agenda_packet_url,
        "ecomment_url": "",
        "meeting_id": group.meeting_id,
    }
    logger.info(
        "meeting row emitted meeting_id=%s meeting_date=%s status=%s evidence agenda_url=%s minutes_url=%s agenda_packet_url=%s labels=%s",
        group.meeting_id,
        group.meeting_date,
        status,
        bool(group.agenda_url),
        bool(group.minutes_url),
        bool(group.agenda_packet_url),
        " | ".join(group.labels),
    )
    return {field_name: str(row[field_name]) for field_name in CANONICAL_FIELDS}


def _meeting_status(group: MeetingGroup) -> str:
    if CANCELLED_RE.search(group.meeting_title):
        return "Cancelled"
    if group.minutes_url:
        return "Minutes Available"
    if group.agenda_url or group.agenda_packet_url:
        return "Agenda Available"
    return "Scheduled"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    input_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CALENDAR_URL
    result = scrape_calendar(input_url)
    print(json.dumps(result, indent=2))
    print(f"row count: {len(result)}", file=sys.stderr)
