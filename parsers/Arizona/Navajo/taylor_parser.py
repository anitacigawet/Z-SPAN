"""Taylor — TablePress meeting parser."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
import json
import logging
import re
import sys
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

__all__ = ["scrape_calendar"]

logger = logging.getLogger(__name__)

_DEFAULT_URL = "https://www.tayloraz.gov/town-hall/town-council-meetings/agenda-minutes/"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_MAX_RESPONSE_BYTES = 8_000_000
_CHUNK_SIZE = 65_536
_ALLOWED_HOSTS = {"tayloraz.gov", "www.tayloraz.gov"}
_BAD_SCHEMES = ("javascript:", "data:", "vbscript:", "file:", "mailto:", "ftp:", "gopher:")
_FIELD_NAMES = (
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
_CANCELLED_RE = re.compile(r"\bcancell?ed\b", re.IGNORECASE)
_YEAR_HEADING_RE = re.compile(r"\b(20\d{2})\b")
_DATE_RE = re.compile(
    r"^\s*([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\s*$",
    re.IGNORECASE,
)
_TIME_RE = re.compile(
    # Accepted formats: 5:30 a.m. / 5:30 p.m. / 5:30am / 5:30 AM.
    r"(?<!\d)(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([ap])\.?\s*m\.?(?=\s|$|[^\w.])",
    re.IGNORECASE,
)
_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


@dataclass(frozen=True)
class _FetchResult:
    text: str
    status_code: int
    final_url: str
    byte_count: int


@dataclass
class _Stats:
    rows_seen: int = 0
    rows_accepted: int = 0
    rows_dropped: int = 0
    drop_reasons: Counter[str] = field(default_factory=Counter)
    tables_seen: list[str] = field(default_factory=list)
    table_rows: Counter[str] = field(default_factory=Counter)
    table_drops: Counter[str] = field(default_factory=Counter)
    field_absences: Counter[str] = field(default_factory=Counter)
    url_rejections: Counter[str] = field(default_factory=Counter)
    unsupported_links: list[str] = field(default_factory=list)
    document_fields_seen: Counter[str] = field(default_factory=Counter)

    def drop(self, table_id: str, reason: str) -> None:
        self.rows_dropped += 1
        self.drop_reasons[reason] += 1
        self.table_drops[table_id] += 1


def scrape_calendar(url: str) -> list[dict]:
    """Scrape Taylor's TablePress agenda/minutes tables into canonical rows."""
    stats = _Stats()
    session = requests.Session()
    session.headers.update({"User-Agent": _USER_AGENT})

    logger.info(
        "Taylor scrape started: url=%s allowed_hosts=%s tls_verify=True max_bytes=%d",
        url,
        sorted(_ALLOWED_HOSTS),
        _MAX_RESPONSE_BYTES,
    )
    logger.info(
        "Taylor source field policy: meeting_time, meeting_location, video_url, "
        "ecomment_url, and meeting_id are not exposed per row by this TablePress archive; "
        "first-Thursday-7PM and Council Chambers metadata are not hardcoded"
    )

    try:
        fetch = _fetch_text_bounded(session, url)
    except requests.RequestException as exc:
        logger.warning(
            "Taylor fetch failed; architectural blocker for all meetings; returning []: %s",
            exc,
        )
        _log_summary(stats)
        return []

    logger.info(
        "Taylor fetch observed: status=%d final_url=%s bytes=%d",
        fetch.status_code,
        fetch.final_url,
        fetch.byte_count,
    )
    if fetch.status_code != 200:
        logger.warning(
            "Taylor non-200 HTTP status=%d; missing-data scope=all meetings; returning []",
            fetch.status_code,
        )
        _log_summary(stats)
        return []

    soup = BeautifulSoup(fetch.text, "html.parser")
    if not _validate_vendor_fingerprint(soup, fetch.text):
        _log_summary(stats)
        return []

    meetings: list[dict] = []
    tables = _find_tablepress_tables(soup)
    if not tables:
        logger.warning("Taylor structural mismatch: TablePress fingerprint present but no tablepress tables found")
        _log_summary(stats)
        return []

    for table in tables:
        table_id = table.get("id", "") or "tablepress-without-id"
        stats.tables_seen.append(table_id)
        year = _extract_table_year(table, table_id)
        if not year:
            row_count = len(table.find_all("tr"))
            stats.rows_seen += row_count
            stats.rows_dropped += row_count
            stats.table_drops[table_id] += row_count
            stats.drop_reasons["missing_table_year"] += row_count
            logger.warning(
                "Taylor table %s dropped %d rows because no year heading was witnessed",
                table_id,
                row_count,
            )
            continue

        for row_index, row in enumerate(table.find_all("tr"), start=1):
            stats.rows_seen += 1
            row_id = f"{table_id}:row-{row_index}"
            try:
                meeting = _parse_row(row, table_id, row_id, year, fetch.final_url, stats)
            except Exception:
                stats.drop(table_id, "row_exception")
                logger.warning("Taylor row parse failed; row_id=%s dropped", row_id, exc_info=True)
                continue
            if meeting is None:
                continue
            meetings.append(meeting)
            stats.rows_accepted += 1
            stats.table_rows[table_id] += 1

    if not meetings:
        logger.warning(
            "Taylor structural mismatch: 0 accepted rows across TablePress tables=%s; returning []",
            stats.tables_seen,
        )

    _log_summary(stats)
    return meetings


def _fetch_text_bounded(session: requests.Session, url: str) -> _FetchResult:
    start_host = (urlparse(url).hostname or "").lower()
    if start_host not in _ALLOWED_HOSTS:
        raise ValueError(f"Taylor parser called with disallowed host: {start_host}")

    with session.get(url, timeout=30, stream=True, allow_redirects=True, verify=True) as response:
        final_host = (urlparse(response.url).hostname or "").lower()
        if final_host not in _ALLOWED_HOSTS:
            raise ValueError(f"Redirect to disallowed host: {final_host} (started from {url})")

        body = bytearray()
        for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
            body.extend(chunk)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise ValueError(f"Response from {url} exceeded {_MAX_RESPONSE_BYTES} bytes")

        encoding = response.encoding or "utf-8"
        return _FetchResult(
            text=bytes(body).decode(encoding, errors="replace"),
            status_code=response.status_code,
            final_url=response.url,
            byte_count=len(body),
        )


def _validate_vendor_fingerprint(soup: BeautifulSoup, html: str) -> bool:
    table = soup.find("table", id=re.compile(r"^tablepress-\d+$"))
    table_id = table.get("id", "") if isinstance(table, Tag) else ""
    class_string = " ".join(table.get("class", [])) if isinstance(table, Tag) else ""
    asset_present = "/wp-content/plugins/tablepress/" in html

    body_classes = soup.body.get("class", []) if soup.body else []
    main = soup.find("main") or soup.find(id="main") or soup.find("article") or soup
    main_view_classes = sorted(
        {
            class_name
            for tag in main.find_all(True)
            for class_name in tag.get("class", [])
            if class_name.startswith("tribe-events")
        }
    )
    body_view_classes = sorted(
        class_name for class_name in body_classes if class_name.startswith("tribe-events")
    )
    logger.info(
        "Taylor surface disambiguation: body_tribe_no_js=%s body_tribe_events_view_classes=%s "
        "main_tribe_events_view_classes=%s",
        "tribe-no-js" in body_classes,
        body_view_classes,
        main_view_classes,
    )

    if table_id or asset_present:
        logger.info(
            "Taylor TablePress fingerprint witnessed: table_id=%r class=%r asset_path_present=%s",
            table_id,
            class_string,
            asset_present,
        )
        return True

    logger.warning(
        "Taylor vendor fingerprint missing: no table id^=tablepress- and no "
        "/wp-content/plugins/tablepress/ asset; architectural blocker; returning []"
    )
    return False


def _find_tablepress_tables(soup: BeautifulSoup) -> list[Tag]:
    tables: list[Tag] = []
    for table in soup.find_all("table"):
        table_id = table.get("id", "")
        classes = table.get("class", [])
        if table_id.startswith("tablepress-") or "tablepress" in classes:
            tables.append(table)
            logger.info(
                "Taylor TablePress table queued: id=%r class=%r",
                table_id,
                " ".join(classes),
            )
    return tables


def _extract_table_year(table: Tag, table_id: str) -> int:
    for heading in table.find_all_previous(["h1", "h2", "h3", "h4"], limit=6):
        text = _clean_text(heading.get_text(" ", strip=True))
        match = _YEAR_HEADING_RE.search(text)
        if match:
            year = int(match.group(1))
            logger.info("Taylor table year witnessed: table=%s heading=%r year=%d", table_id, text, year)
            return year
    logger.warning("Taylor table %s has no parseable year heading before the table", table_id)
    return 0


def _parse_row(
    row: Tag,
    table_id: str,
    row_id: str,
    year: int,
    base_url: str,
    stats: _Stats,
) -> dict | None:
    cells = row.find_all(["td", "th"], recursive=False)
    cell_texts = [_clean_text(cell.get_text(" ", strip=True)) for cell in cells]
    logger.debug("Taylor row seen: row_id=%s cells=%r", row_id, cell_texts)

    if not any(cell_texts):
        stats.drop(table_id, "blank_row")
        logger.warning("Taylor row dropped: row_id=%s reason=blank_row cells=%r", row_id, cell_texts)
        return None

    if len(cells) < 2:
        stats.drop(table_id, "too_few_cells")
        logger.warning("Taylor row dropped: row_id=%s reason=too_few_cells cells=%r", row_id, cell_texts)
        return None

    meeting_date = _extract_date(cell_texts[0], year, row_id, stats)
    if not meeting_date:
        stats.drop(table_id, "unparseable_date")
        logger.warning(
            "Taylor row dropped: row_id=%s reason=unparseable_date date_cell=%r cells=%r",
            row_id,
            cell_texts[0],
            cell_texts,
        )
        return None

    title = _extract_title(cell_texts, row_id, stats)
    if not title:
        stats.drop(table_id, "missing_title")
        logger.warning("Taylor row dropped: row_id=%s reason=missing_title cells=%r", row_id, cell_texts)
        return None

    urls = {
        "agenda_url": "",
        "minutes_url": "",
        "agenda_packet_url": "",
    }
    _extract_document_links(cells, row_id, base_url, urls, stats)

    if _row_has_cancelled_signal(cell_texts):
        if not _CANCELLED_RE.search(title):
            title = f"{title} - Cancelled"
        logger.info("Taylor cancellation evidence witnessed: row_id=%s cells=%r", row_id, cell_texts)

    meeting_time = _extract_time(" ".join(cell_texts), row_id, stats)
    meeting_location = _extract_location(None, row_id, stats)

    if not urls["agenda_packet_url"]:
        stats.field_absences["agenda_packet_url"] += 1
    video_url = _absent_by_construction("video_url", row_id, stats)
    ecomment_url = _absent_by_construction("ecomment_url", row_id, stats)
    meeting_id = _absent_by_construction("meeting_id", row_id, stats)

    status = _classify_status(title, urls["agenda_url"], urls["minutes_url"], urls["agenda_packet_url"], row_id)
    meeting = {
        "meeting_title": title,
        "meeting_date": meeting_date,
        "meeting_time": meeting_time,
        "meeting_location": meeting_location,
        "meeting_status": status,
        "agenda_url": urls["agenda_url"],
        "minutes_url": urls["minutes_url"],
        "video_url": video_url,
        "agenda_packet_url": urls["agenda_packet_url"],
        "ecomment_url": ecomment_url,
        "meeting_id": meeting_id,
    }
    logger.info(
        "Taylor row accepted: row_id=%s date=%s title=%r status=%s url_fields=%s",
        row_id,
        meeting_date,
        title,
        status,
        {key: bool(meeting[key]) for key in ("agenda_url", "minutes_url", "agenda_packet_url")},
    )
    return {field: str(meeting[field]) for field in _FIELD_NAMES}


def _extract_date(text: str, year: int, row_id: str, stats: _Stats) -> str:
    if not text:
        logger.warning("Taylor meeting_date abandoned: row_id=%s reason=empty_date_cell", row_id)
        return ""
    match = _DATE_RE.match(text[:80])
    if not match:
        logger.warning(
            "Taylor meeting_date abandoned: row_id=%s rejected=%r reason=unrecognized_date_format",
            row_id,
            text,
        )
        return ""

    month_token = match.group(1).lower().rstrip(".")
    month = _MONTHS.get(month_token)
    if not month:
        logger.warning(
            "Taylor meeting_date abandoned: row_id=%s rejected=%r reason=unknown_month",
            row_id,
            text,
        )
        return ""

    try:
        parsed = date(year, month, int(match.group(2)))
    except ValueError as exc:
        logger.warning(
            "Taylor meeting_date abandoned: row_id=%s rejected=%r reason=invalid_calendar_date error=%s",
            row_id,
            text,
            exc,
        )
        return ""
    stats.document_fields_seen["meeting_date"] += 1
    return parsed.isoformat()


def _extract_title(cell_texts: list[str], row_id: str, stats: _Stats) -> str:
    raw_title = cell_texts[1] if len(cell_texts) > 1 else ""
    title = _clean_text(raw_title)
    if not title:
        logger.warning(
            "Taylor meeting_title abandoned: row_id=%s rejected=%r reason=empty_title_cell",
            row_id,
            raw_title,
        )
        return ""
    stats.document_fields_seen["meeting_title"] += 1
    if title.lower().startswith("town council"):
        return title
    return f"Town Council {title}"


def _extract_time(row_text: str, row_id: str, stats: _Stats) -> str:
    match = _TIME_RE.search(row_text[:500])
    if not match:
        stats.field_absences["meeting_time"] += 1
        logger.warning(
            "Taylor meeting_time abandoned: row_id=%s reason=no_time_re_match "
            "row_text_prefix=%r",
            row_id,
            row_text[:120],
        )
        return ""
    hour = int(match.group(1))
    minute = match.group(2) or "00"
    suffix = f"{match.group(3).upper()}M"
    parsed = f"{hour}:{minute} {suffix}"
    logger.info("Taylor meeting_time witnessed: row_id=%s value=%s source=%r", row_id, parsed, match.group(0))
    return parsed


def _extract_location(raw_location: str | None, row_id: str, stats: _Stats) -> str:
    if raw_location is None:
        stats.field_absences["meeting_location"] += 1
        logger.warning(
            "Taylor meeting_location abandoned: row_id=%s reason=raw_location_is_none "
            "explanation=tablepress_archive_does_not_expose_per_row_location",
            row_id,
        )
        return ""
    location = _clean_text(raw_location)
    if not location:
        stats.field_absences["meeting_location"] += 1
        logger.warning("Taylor meeting_location abandoned: row_id=%s reason=empty_location_signal", row_id)
        return ""
    return location


def _extract_document_links(
    cells: list[Tag],
    row_id: str,
    base_url: str,
    urls: dict[str, str],
    stats: _Stats,
) -> None:
    for index, cell in enumerate(cells):
        for anchor in cell.find_all("a"):
            label = _clean_text(anchor.get_text(" ", strip=True))
            href = anchor.get("href", "")
            field_name = _classify_document_link(index, label, href, row_id)
            if not field_name:
                rejected = href or label
                stats.unsupported_links.append(f"{row_id} col={index + 1} label={label!r} href={href!r}")
                logger.warning(
                    "Taylor document link dropped: row_id=%s field=unsupported_document "
                    "label=%r href=%r reason=no_canonical_actions_field_or_unclassified_label",
                    row_id,
                    label,
                    rejected,
                )
                continue

            emitted_url = emit_url(href, base_url, field_name, row_id, stats)
            if not emitted_url:
                continue
            if urls[field_name]:
                logger.warning(
                    "Taylor duplicate document link dropped: row_id=%s field=%s kept=%r dropped=%r",
                    row_id,
                    field_name,
                    urls[field_name],
                    emitted_url,
                )
                continue
            urls[field_name] = emitted_url
            stats.document_fields_seen[field_name] += 1
            logger.info(
                "Taylor document link emitted: row_id=%s field=%s label=%r url=%s",
                row_id,
                field_name,
                label,
                emitted_url,
            )


def _classify_document_link(index: int, label: str, href: str, row_id: str) -> str:
    signal = f"{label} {urlparse(href).path}".lower()
    if re.search(r"\bminutes?\b", signal):
        return "minutes_url"
    if re.search(r"\bpackets?\b", signal):
        return "agenda_packet_url"
    if re.search(r"\bagendas?\b", signal):
        return "agenda_url"
    if index == 2:
        logger.warning(
            "Taylor agenda-column link not classified as agenda: row_id=%s label=%r href=%r",
            row_id,
            label,
            href,
        )
    return ""


def emit_url(href: str, base_url: str, field_name: str, row_id: str, stats: _Stats) -> str:
    if not href:
        stats.url_rejections["empty_href"] += 1
        logger.warning(
            "Taylor URL rejected: row_id=%s field=%s rejected=%r reason=empty_href",
            row_id,
            field_name,
            href,
        )
        return ""

    stripped = href.strip()
    lowered = stripped.lower()
    if lowered.startswith("//"):
        stats.url_rejections["scheme_relative"] += 1
        logger.warning(
            "Taylor URL rejected: row_id=%s field=%s rejected=%r reason=scheme_relative_url",
            row_id,
            field_name,
            href,
        )
        return ""
    for bad_scheme in _BAD_SCHEMES:
        if lowered.startswith(bad_scheme):
            stats.url_rejections[f"bad_scheme:{bad_scheme.rstrip(':')}"] += 1
            logger.warning(
                "Taylor URL rejected: row_id=%s field=%s rejected=%r reason=bad_scheme_%s",
                row_id,
                field_name,
                href,
                bad_scheme.rstrip(":"),
            )
            return ""

    absolute = urljoin(base_url, stripped)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        stats.url_rejections[f"scheme:{parsed.scheme or 'missing'}"] += 1
        logger.warning(
            "Taylor URL rejected: row_id=%s field=%s rejected=%r absolute=%r reason=unsupported_scheme",
            row_id,
            field_name,
            href,
            absolute,
        )
        return ""
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        stats.url_rejections[f"host:{host or 'missing'}"] += 1
        logger.warning(
            "Taylor URL rejected: row_id=%s field=%s rejected=%r absolute=%r reason=disallowed_host",
            row_id,
            field_name,
            href,
            absolute,
        )
        return ""
    return absolute


def _row_has_cancelled_signal(cell_texts: list[str]) -> bool:
    return any(_CANCELLED_RE.search(text[:200]) for text in cell_texts)


def _classify_status(
    title: str,
    agenda_url: str,
    minutes_url: str,
    agenda_packet_url: str,
    row_id: str,
) -> str:
    if _CANCELLED_RE.search(title):
        logger.info("Taylor status emitted: row_id=%s status=Cancelled evidence=title_cancelled_regex", row_id)
        return "Cancelled"
    if minutes_url:
        logger.info("Taylor status emitted: row_id=%s status=Minutes Available evidence=minutes_url", row_id)
        return "Minutes Available"
    if agenda_url or agenda_packet_url:
        logger.info(
            "Taylor status emitted: row_id=%s status=Agenda Available evidence=agenda_or_packet_url",
            row_id,
        )
        return "Agenda Available"
    logger.info("Taylor status emitted: row_id=%s status=Scheduled evidence=no_doc_or_cancel_signal", row_id)
    return "Scheduled"


def _absent_by_construction(field_name: str, row_id: str, stats: _Stats) -> str:
    stats.field_absences[field_name] += 1
    logger.info("Taylor %s honest-empty: row_id=%s reason=not_exposed_by_tablepress_archive", field_name, row_id)
    return ""


def _clean_text(value: str) -> str:
    if not value:
        return ""
    return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)


def _log_summary(stats: _Stats) -> None:
    logger.info(
        "Taylor scrape summary: rows_seen=%d rows_accepted=%d rows_dropped=%d drop_reasons=%s "
        "tables_seen=%s rows_by_table=%s drops_by_table=%s document_fields_seen=%s "
        "field_absences=%s url_rejections=%s unsupported_links_count=%d unsupported_links_sample=%s",
        stats.rows_seen,
        stats.rows_accepted,
        stats.rows_dropped,
        dict(stats.drop_reasons),
        stats.tables_seen,
        dict(stats.table_rows),
        dict(stats.table_drops),
        dict(stats.document_fields_seen),
        dict(stats.field_absences),
        dict(stats.url_rejections),
        len(stats.unsupported_links),
        stats.unsupported_links[:10],
    )
    if stats.rows_accepted and stats.document_fields_seen["agenda_packet_url"] == 0:
        logger.info(
            "Taylor source field policy: no agenda_packet_url links witnessed in accepted TablePress rows"
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s:%(name)s:%(message)s")
    results = scrape_calendar(_DEFAULT_URL)
    print(f"row_count={len(results)}", file=sys.stderr)
    print(json.dumps(results[:2], indent=2))
