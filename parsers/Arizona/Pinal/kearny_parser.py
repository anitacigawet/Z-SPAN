"""Kearny — Municode meeting parser."""

from __future__ import annotations

import json
import logging
import re
import sys
from collections import Counter
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)
__all__ = ["scrape_calendar"]

CALENDAR_URL = "https://kearny-az.municodemeetings.com/"
MAX_BYTES = 10_000_000
MAX_PAGES = 100
ALLOWED_HOSTS = {
    "kearny-az.municodemeetings.com",
    "municodemeetings.com",
    "meetings.municode.com",
    "municode.com",
}
BLOCKED_HOSTS = {"townofkearny.com", "ww1.kearnyaz.gov"}
BAD_SCHEMES = ("javascript:", "data:", "vbscript:", "file:", "mailto:", "ftp:", "gopher:")
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
CANCELLED_RE = re.compile(r"\bcancell?ed\b", re.IGNORECASE)
# Time cases covered: "7:00 PM", "7:00 pm", "7:00 p.m.", "7:00pm", "19:00".
# Avoid a trailing \b after a literal dot so dotted a.m./p.m. variants still match.
TIME_RE = re.compile(
    r"(?P<hour>\b(?:[01]?\d|2[0-3])):(?P<minute>[0-5]\d)\s*(?P<ampm>[APap])?\.?\s*(?:[Mm]\.?)?(?=\s|$|[^\w.])"
)
DATE_RE = re.compile(r"\b(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{4})\b")


def scrape_calendar(calendar_url: str) -> list[dict]:
    """Scrape Kearny's Municode Meetings calendar into canonical meeting dictionaries."""
    base_url = _normalize_base_url(calendar_url)
    host = _host(base_url)
    if not _is_allowed_host(host):
        logger.warning(
            "input URL rejected: host=%s url=%s allowed_hosts=%s",
            host,
            calendar_url,
            sorted(ALLOWED_HOSTS),
        )
        return []

    if host == "kearny-az.municodemeetings.com":
        logger.info(
            "vendor inferred from URL subdomain pattern (%s); markup confirmation pending fetch",
            host,
        )

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }
    )

    meetings: list[dict] = []
    rows_seen = 0
    rows_accepted = 0
    rows_dropped = 0
    drop_reasons: Counter[str] = Counter()
    vendor_confirmed = False

    for page_num in range(MAX_PAGES):
        page_url = urljoin(base_url, f"meetings3?page={page_num}")
        logger.info("page fetch starting: page=%s url=%s", page_num, page_url)
        try:
            html = _fetch_text_bounded(session, page_url, allowed_hosts=ALLOWED_HOSTS)
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else "unknown"
            if status_code in (401, 403, 429):
                logger.warning(
                    "architectural blocker: Municode portal fetch blocked; "
                    "vendor inferred from URL subdomain pattern (kearny-az.municodemeetings.com), "
                    "NOT confirmed by markup — blocked by %s; missing-data scope=all meeting rows/documents; url=%s",
                    status_code,
                    page_url,
                )
                return []
            logger.warning(
                "page fetch failed and page skipped: page=%s url=%s status=%s error=%s",
                page_num,
                page_url,
                status_code,
                exc,
            )
            drop_reasons[f"http_error_{status_code}"] += 1
            if page_num == 0:
                break
            continue
        except (requests.RequestException, ValueError) as exc:
            logger.warning(
                "page fetch failed and page skipped: page=%s url=%s error=%s",
                page_num,
                page_url,
                exc,
            )
            drop_reasons[type(exc).__name__] += 1
            if page_num == 0:
                break
            continue

        soup = BeautifulSoup(html, "html.parser")
        fingerprint = _vendor_fingerprint(soup)
        if fingerprint:
            if not vendor_confirmed:
                logger.info("vendor fingerprint confirmed by markup token: %s", fingerprint)
            vendor_confirmed = True
        elif page_num == 0:
            logger.warning(
                "vendor fingerprint not confirmed by markup on first page; "
                "vendor inferred from URL subdomain pattern (%s), NOT confirmed by markup",
                host,
            )

        table = _find_meetings_table(soup)
        if table is None:
            empty_shape = _empty_state_shape(soup)
            logger.warning(
                "page produced 0 rows: page=%s url=%s reason=no meeting table empty_state=%s",
                page_num,
                page_url,
                empty_shape,
            )
            drop_reasons["no_table"] += 1
            if not _has_next_page(soup):
                logger.info("pagination stop: page=%s reason=no next pager after no-table page", page_num)
                break
            continue

        header_map = _header_map(table, page_url)
        if not header_map:
            logger.warning(
                "page dropped: page=%s url=%s reason=header row missing or unparseable",
                page_num,
                page_url,
            )
            drop_reasons["bad_header"] += 1
            if not _has_next_page(soup):
                logger.info("pagination stop: page=%s reason=no next pager after bad-header page", page_num)
                break
            continue

        rows = _data_rows(table)
        if not rows:
            logger.warning(
                "page produced 0 rows: page=%s url=%s reason=data rows missing empty_state=%s",
                page_num,
                page_url,
                _empty_state_shape(soup),
            )
            drop_reasons["zero_rows"] += 1
        for row_index, row in enumerate(rows, start=1):
            rows_seen += 1
            row_ref = f"page={page_num} row={row_index}"
            try:
                meeting = _parse_row(row, header_map, page_url, row_ref)
            except Exception as exc:
                rows_dropped += 1
                drop_reasons[type(exc).__name__] += 1
                logger.warning(
                    "row dropped: %s reason=parse_exception error=%s",
                    row_ref,
                    exc,
                    exc_info=True,
                )
                continue

            if meeting is None:
                rows_dropped += 1
                drop_reasons["missing_required_row_signal"] += 1
                logger.info("row skipped: %s reason=missing required date/title evidence", row_ref)
                continue

            meetings.append(meeting)
            rows_accepted += 1
            logger.info(
                "row accepted: %s meeting_id=%s title=%r date=%s status=%s",
                row_ref,
                meeting["meeting_id"],
                meeting["meeting_title"],
                meeting["meeting_date"],
                meeting["meeting_status"],
            )

        if not _has_next_page(soup):
            logger.info("pagination stop: page=%s reason=no pager-next token", page_num)
            break
    else:
        logger.warning("pagination cap reached: max_pages=%s base_url=%s", MAX_PAGES, base_url)

    logger.warning(
        "Kearny scrape audit summary: rows_seen=%s rows_accepted=%s rows_dropped=%s drop_reasons=%s",
        rows_seen,
        rows_accepted,
        rows_dropped,
        dict(drop_reasons),
    )
    if not vendor_confirmed:
        logger.warning(
            "vendor fingerprint remained unconfirmed by markup; vendor inferred from URL subdomain pattern (%s)",
            host,
        )
    return meetings


def _normalize_base_url(calendar_url: str) -> str:
    """Return a slash-terminated base URL for Municode pagination."""
    parsed = urlparse(calendar_url.strip())
    if not parsed.scheme:
        calendar_url = "https://" + calendar_url.strip()
    normalized = calendar_url.strip()
    if not normalized.endswith("/"):
        normalized += "/"
    return normalized


def _host(url: str) -> str:
    """Return a lower-cased hostname without a port."""
    return (urlparse(url).netloc.split(":")[0] or "").lower()


def _is_allowed_host(host: str) -> bool:
    """Return whether a host is in Kearny's explicit Municode allowlist."""
    if host in BLOCKED_HOSTS or any(host.endswith("." + blocked) for blocked in BLOCKED_HOSTS):
        return False
    return any(host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_HOSTS)


def _fetch_text_bounded(
    session: requests.Session,
    url: str,
    allowed_hosts: set[str],
    max_bytes: int = MAX_BYTES,
) -> str:
    """Fetch a URL with TLS verification, redirect host validation, and a response size cap."""
    with session.get(url, timeout=30, stream=True, allow_redirects=True, verify=True) as response:
        final_host = _host(response.url)
        if not _is_allowed_host(final_host) or not any(
            final_host == allowed or final_host.endswith("." + allowed) for allowed in allowed_hosts
        ):
            raise ValueError(f"Redirect to disallowed host: {final_host} (started from {url})")
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ValueError(f"Response from {url} exceeded {max_bytes} bytes")
        return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def emit_url(href: str, base_url: str, allowed_hosts: set[str], field: str = "url", row_ref: str = "") -> str:
    """Validate and absolutize an extracted URL, returning an empty string when rejected."""
    raw = (href or "").strip()
    if not raw:
        return ""
    lowered = raw.lower().lstrip()
    for bad_scheme in BAD_SCHEMES:
        if lowered.startswith(bad_scheme):
            logger.warning(
                "URL dropped: row=%s field=%s rejected=%r reason=bad_scheme:%s",
                row_ref,
                field,
                raw,
                bad_scheme,
            )
            return ""

    absolute = urljoin(base_url, raw)
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https"):
        logger.warning(
            "URL dropped: row=%s field=%s rejected=%r reason=bad_scheme_after_join:%s",
            row_ref,
            field,
            raw,
            parsed.scheme,
        )
        return ""
    emit_host = (parsed.netloc.split(":")[0] or "").lower()
    if not _is_allowed_host(emit_host) or not any(
        emit_host == allowed or emit_host.endswith("." + allowed) for allowed in allowed_hosts
    ):
        logger.warning(
            "URL dropped: row=%s field=%s rejected=%r absolute=%r reason=host_not_allowed:%s",
            row_ref,
            field,
            raw,
            absolute,
            emit_host,
        )
        return ""
    return absolute


def _vendor_fingerprint(soup: BeautifulSoup) -> str:
    """Return the runtime markup token that witnesses Municode Meetings, if present."""
    view_content = soup.select_one("div.view-content")
    meetings_link = soup.find("a", href=re.compile(r"/bc-[^/]+/page/[^/]+-\d+$"))
    ada_link = soup.find("a", href=lambda href: bool(href and "meetings.municode.com/adaHtmlDocument" in href))
    if view_content and view_content.find("table"):
        return "div.view-content table"
    if meetings_link:
        return "Municode meeting detail href pattern /bc-*/page/*-N"
    if ada_link:
        return "meetings.municode.com/adaHtmlDocument link"
    return ""


def _find_meetings_table(soup: BeautifulSoup) -> Tag | None:
    """Find the Municode meeting-list table using the expected view-content container first."""
    view_content = soup.select_one("div.view-content")
    if view_content:
        table = view_content.find("table")
        if isinstance(table, Tag):
            return table
        logger.warning("runtime sanity check failed: div.view-content present but no table found")
    table = soup.find("table")
    if isinstance(table, Tag):
        logger.warning(
            "runtime sanity check warning: using fallback first table because div.view-content table was absent"
        )
        return table
    return None


def _header_map(table: Tag, page_url: str) -> dict[str, int]:
    """Build a semantic header map from the table header row."""
    header_row = table.find("tr")
    if not isinstance(header_row, Tag):
        return {}
    cells = header_row.find_all(["th", "td"])
    headers = [_clean_text(cell.get_text(" ", strip=True)) for cell in cells]
    if not headers:
        return {}

    mapping: dict[str, int] = {}
    for index, header in enumerate(headers):
        key = _canonical_header(header)
        if key:
            if key in mapping:
                logger.warning(
                    "duplicate semantic header encountered: page=%s header=%r key=%s first_index=%s duplicate_index=%s",
                    page_url,
                    header,
                    key,
                    mapping[key],
                    index,
                )
                continue
            mapping[key] = index
        else:
            logger.warning(
                "unknown header vocabulary encountered: page=%s index=%s header=%r",
                page_url,
                index,
                header,
            )

    required_any = {"date_time", "date"}
    if not (required_any & mapping.keys()) or "title" not in mapping:
        logger.warning(
            "runtime sanity check failed: expected date/time and title headers; page=%s headers=%s mapping=%s",
            page_url,
            headers,
            mapping,
        )
    else:
        logger.info("header mapping accepted: page=%s headers=%s mapping=%s", page_url, headers, mapping)
    return mapping


def _canonical_header(header: str) -> str:
    """Map a vendor header label to a parser field key."""
    normalized = re.sub(r"[^a-z0-9]+", " ", header.lower()).strip()
    if not normalized:
        return ""
    if normalized in {"date time", "meeting date time", "date"} or (
        "date" in normalized and "time" in normalized
    ):
        return "date_time"
    if normalized == "time" or normalized.endswith(" time"):
        return "time"
    if normalized in {"meeting", "meeting name", "name", "title", "meeting title"}:
        return "title"
    if normalized == "agenda" or normalized.endswith(" agenda"):
        return "agenda_url"
    if "agenda packet" in normalized or normalized in {"packet", "agenda package"}:
        return "agenda_packet_url"
    if normalized == "minutes" or normalized.endswith(" minutes"):
        return "minutes_url"
    if normalized == "video" or "video" in normalized:
        return "video_url"
    if "comment" in normalized:
        return "ecomment_url"
    if normalized in {"location", "meeting location", "place"}:
        return "meeting_location"
    if "status" in normalized:
        return "vendor_status"
    return ""


def _data_rows(table: Tag) -> list[Tag]:
    """Return table rows after the header row."""
    rows = [row for row in table.find_all("tr") if isinstance(row, Tag)]
    return rows[1:]


def _parse_row(row: Tag, header_map: dict[str, int], base_url: str, row_ref: str) -> dict | None:
    """Parse one Municode table row into a canonical meeting dictionary or None when essential evidence is absent."""
    cells = row.find_all("td")
    if not cells:
        logger.info("row skipped: %s reason=no td cells", row_ref)
        return None

    title_cell = _cell_for(header_map, cells, "title")
    date_time_cell = _cell_for(header_map, cells, "date_time") or _cell_for(header_map, cells, "date")
    if title_cell is None or date_time_cell is None:
        logger.warning(
            "row skipped: %s reason=missing title/date cells header_map=%s cell_count=%s",
            row_ref,
            header_map,
            len(cells),
        )
        return None

    title = _clean_text(title_cell.get_text(" ", strip=True))
    if not title:
        logger.warning("row skipped: %s reason=title signal empty after sanitization", row_ref)
        return None

    date_time_text = _clean_text(date_time_cell.get_text(" ", strip=True))
    meeting_date = _parse_date(date_time_text, row_ref)
    if not meeting_date:
        logger.warning(
            "row skipped: %s reason=date parse failed rejected=%r",
            row_ref,
            date_time_text,
        )
        return None

    separate_time_text = _cell_text(header_map, cells, "time")
    meeting_time = _parse_time(separate_time_text or date_time_text, row_ref)
    location = _cell_text(header_map, cells, "meeting_location")
    vendor_status = _cell_text(header_map, cells, "vendor_status")

    detail_link = title_cell.find("a", href=True)
    detail_url = ""
    meeting_id = ""
    if isinstance(detail_link, Tag):
        detail_url = emit_url(str(detail_link.get("href", "")), base_url, ALLOWED_HOSTS, "meeting_detail_url", row_ref)
        meeting_id = _meeting_id_from_url(detail_url or str(detail_link.get("href", "")), row_ref)
    else:
        logger.info("field absent: row=%s field=meeting_detail_url reason=no title anchor", row_ref)

    agenda_url = _extract_url_field(row, header_map, cells, "agenda_url", base_url, row_ref)
    agenda_packet_url = _extract_url_field(row, header_map, cells, "agenda_packet_url", base_url, row_ref)
    minutes_url = _extract_url_field(row, header_map, cells, "minutes_url", base_url, row_ref)
    video_url = _extract_url_field(row, header_map, cells, "video_url", base_url, row_ref)
    ecomment_url = _extract_url_field(row, header_map, cells, "ecomment_url", base_url, row_ref)

    meeting_status = _derive_status(
        title=title,
        vendor_status=vendor_status,
        agenda_url=agenda_url,
        agenda_packet_url=agenda_packet_url,
        minutes_url=minutes_url,
        row_ref=row_ref,
    )

    meeting = {
        "meeting_title": title,
        "meeting_date": meeting_date,
        "meeting_time": meeting_time,
        "meeting_location": location,
        "meeting_status": meeting_status,
        "agenda_url": agenda_url,
        "minutes_url": minutes_url,
        "video_url": video_url,
        "agenda_packet_url": agenda_packet_url,
        "ecomment_url": ecomment_url,
        "meeting_id": meeting_id,
    }
    return _canonicalize(meeting, row_ref)


def _cell_for(header_map: dict[str, int], cells: list[Tag], key: str) -> Tag | None:
    """Return the cell for a semantic key, logging stale column assumptions when missing."""
    index = header_map.get(key)
    if index is None:
        return None
    if index >= len(cells):
        logger.warning(
            "column index out of range: key=%s index=%s cell_count=%s",
            key,
            index,
            len(cells),
        )
        return None
    return cells[index]


def _cell_text(header_map: dict[str, int], cells: list[Tag], key: str) -> str:
    """Return sanitized text from a semantic cell key, or an empty string when the cell is absent."""
    cell = _cell_for(header_map, cells, key)
    if cell is None:
        return ""
    return _clean_text(cell.get_text(" ", strip=True))


def _extract_url_field(
    row: Tag,
    header_map: dict[str, int],
    cells: list[Tag],
    key: str,
    base_url: str,
    row_ref: str,
) -> str:
    """Extract and validate the first URL for a semantic document/video/comment field."""
    cell = _cell_for(header_map, cells, key)
    if cell is None:
        logger.info("field absent: row=%s field=%s reason=header not exposed", row_ref, key)
        return ""

    anchors = [anchor for anchor in cell.find_all("a", href=True) if isinstance(anchor, Tag)]
    if not anchors:
        text = _clean_text(cell.get_text(" ", strip=True))
        if text:
            logger.warning(
                "field dropped: row=%s field=%s rejected=%r reason=text signal without URL anchor",
                row_ref,
                key,
                text,
            )
        else:
            logger.info("field empty: row=%s field=%s reason=empty cell", row_ref, key)
        return ""

    for anchor in anchors:
        href = str(anchor.get("href", ""))
        emitted = emit_url(href, base_url, ALLOWED_HOSTS, key, row_ref)
        if emitted:
            logger.info(
                "URL emitted: row=%s field=%s href=%r absolute=%s evidence=cell_anchor",
                row_ref,
                key,
                href,
                emitted,
            )
            return emitted

        fallback = _onclick_url(anchor, row_ref, key)
        if fallback:
            emitted = emit_url(fallback, base_url, ALLOWED_HOSTS, key, row_ref)
            if emitted:
                logger.info(
                    "URL emitted: row=%s field=%s href=%r absolute=%s evidence=onclick_fallback",
                    row_ref,
                    key,
                    fallback,
                    emitted,
                )
                return emitted

    logger.warning(
        "field dropped: row=%s field=%s reason=all anchors rejected anchor_count=%s row_text=%r",
        row_ref,
        key,
        len(anchors),
        _clean_text(cell.get_text(" ", strip=True)),
    )
    return ""


def _onclick_url(anchor: Tag, row_ref: str, field: str) -> str:
    """Extract a URL-like value from onclick or data attributes when href is a placeholder."""
    for attr in ("onclick", "data-url", "data-href"):
        value = str(anchor.get(attr, "") or "")
        if not value:
            continue
        match = re.search(r"(?P<url>(?:https?:)?//[^'\"\s)]+|/[^'\"\s)]+)", value[:1000])
        if match:
            candidate = match.group("url")
            logger.info(
                "fallback URL signal found: row=%s field=%s attr=%s candidate=%r",
                row_ref,
                field,
                attr,
                candidate,
            )
            return candidate
        logger.warning(
            "fallback URL signal rejected: row=%s field=%s attr=%s rejected=%r reason=no URL-like token",
            row_ref,
            field,
            attr,
            value[:200],
        )
    return ""


def _parse_date(text: str, row_ref: str) -> str:
    """Parse an MM/DD/YYYY date signal to ISO YYYY-MM-DD."""
    signal = text[:200]
    match = DATE_RE.search(signal)
    if not match:
        if signal:
            logger.warning("date parse failed: row=%s rejected=%r reason=no MM/DD/YYYY token", row_ref, signal)
        return ""
    try:
        parsed = datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError as exc:
        logger.warning("date parse failed: row=%s rejected=%r reason=%s", row_ref, match.group(0), exc)
        return ""
    return parsed.strftime("%Y-%m-%d")


def _parse_time(text: str, row_ref: str) -> str:
    """Parse a time signal to H:MM AM/PM or return an empty string when no parseable signal exists."""
    signal = text[:200]
    match = TIME_RE.search(signal)
    if not match:
        if signal:
            logger.warning("time parse failed: row=%s rejected=%r reason=no supported time token", row_ref, signal)
        return ""

    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    ampm = match.group("ampm")
    if ampm:
        suffix = "AM" if ampm.upper() == "A" else "PM"
        if hour == 0 or hour > 12:
            logger.warning(
                "time parse failed: row=%s rejected=%r reason=12-hour clock with AM/PM had invalid hour",
                row_ref,
                match.group(0),
            )
            return ""
        return f"{hour}:{minute:02d} {suffix}"

    if hour > 23:
        logger.warning("time parse failed: row=%s rejected=%r reason=24-hour clock invalid hour", row_ref, match.group(0))
        return ""
    suffix = "AM" if hour < 12 else "PM"
    normalized_hour = hour % 12 or 12
    return f"{normalized_hour}:{minute:02d} {suffix}"


def _derive_status(
    title: str,
    vendor_status: str,
    agenda_url: str,
    agenda_packet_url: str,
    minutes_url: str,
    row_ref: str,
) -> str:
    """Derive the canonical status from same-row evidence."""
    title_for_regex = title[:500]
    if CANCELLED_RE.search(title_for_regex):
        return "Cancelled"
    if minutes_url:
        return "Minutes Available"
    if agenda_url or agenda_packet_url:
        return "Agenda Available"

    if vendor_status:
        normalized = vendor_status.strip().lower()
        known_neutral = {"scheduled", "upcoming", "regular", "special", "cancelled", "canceled"}
        if normalized not in known_neutral:
            logger.warning(
                "vendor status vocabulary not mapped to canonical enum by direct evidence: "
                "row=%s vendor_status=%r emitted=Scheduled reason=no same-row agenda/minutes/cancellation evidence",
                row_ref,
                vendor_status,
            )
        elif normalized in {"cancelled", "canceled"}:
            logger.warning(
                "vendor status claimed cancellation without title regex evidence: "
                "row=%s vendor_status=%r emitted=Scheduled required_regex=%s",
                row_ref,
                vendor_status,
                CANCELLED_RE.pattern,
            )
    return "Scheduled"


def _meeting_id_from_url(url: str, row_ref: str) -> str:
    """Extract the Municode meeting ID from a detail URL slug."""
    if not url:
        return ""
    path = urlparse(url).path
    match = re.search(r"-(?P<id>\d+)$", path[:500])
    if match:
        return match.group("id")
    slug = path.rstrip("/").split("/")[-1]
    if slug:
        logger.warning(
            "meeting_id fallback: row=%s url=%r reason=numeric suffix missing emitted_slug=%r",
            row_ref,
            url,
            slug,
        )
        return slug
    logger.warning("meeting_id empty: row=%s url=%r reason=no path slug", row_ref, url)
    return ""


def _canonicalize(meeting: dict, row_ref: str) -> dict:
    """Ensure the meeting has exactly the canonical fields and only string values."""
    canonical: dict[str, str] = {}
    for field in CANONICAL_FIELDS:
        value = meeting.get(field, "")
        if value is None:
            logger.warning("field coerced from None to empty string: row=%s field=%s", row_ref, field)
            value = ""
        canonical[field] = str(value)
    extras = set(meeting) - set(CANONICAL_FIELDS)
    if extras:
        logger.warning("extra fields dropped during canonicalization: row=%s extras=%s", row_ref, sorted(extras))
    return canonical


def _clean_text(value: str) -> str:
    """Strip HTML tags/entities and normalize whitespace for emitted free-text fields."""
    if not value:
        return ""
    text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _has_next_page(soup: BeautifulSoup) -> bool:
    """Return whether the Municode pager exposes a next-page control."""
    next_item = soup.select_one("li.pager-next")
    if next_item:
        disabled_class = next_item.get("class", [])
        if "disabled" in disabled_class:
            logger.info("pagination witness: pager-next present but disabled")
            return False
        logger.info("pagination witness: pager-next present and enabled")
        return True
    logger.info("pagination witness: pager-next absent")
    return False


def _empty_state_shape(soup: BeautifulSoup) -> str:
    """Summarize an empty or unexpected page shape for audit logs."""
    title = _clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    body_text = _clean_text(soup.get_text(" ", strip=True))[:200]
    has_view_content = bool(soup.select_one("div.view-content"))
    has_table = bool(soup.find("table"))
    return f"title={title!r} has_view_content={has_view_content} has_table={has_table} body_start={body_text!r}"


if __name__ == "__main__":
    scraped_meetings = scrape_calendar(CALENDAR_URL)
    print(json.dumps(scraped_meetings, indent=2))
    print(f"Kearny: {len(scraped_meetings)} meetings scraped", file=sys.stderr)
