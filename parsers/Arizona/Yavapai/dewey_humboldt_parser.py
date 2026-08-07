"""Dewey-Humboldt — Granicus ViewPublisher meeting parser."""

import logging
import re
from collections import Counter
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

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

DEFAULT_CALENDAR_URL = "https://dhaz.granicus.com/ViewPublisher.php?view_id=2"
# This stable view ID is not year-keyed. ViewPublisher exposes title, date/time,
# and document cells but no per-row location column.
DEFAULT_VIEW_ID = "2"
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
MAX_BODY_BYTES = 10_000_000
FETCH_TIMEOUT = 30

ALLOWED_HOSTS = frozenset(
    {
        "dhaz.granicus.com",
        "archive-video.granicus.com",
        "archive-media.granicus.com",
    }
)

BAD_SCHEMES = (
    "javascript:",
    "data:",
    "vbscript:",
    "file:",
    "mailto:",
    "ftp:",
    "gopher:",
)

# Tested against: "5:30 a.m.", "5:30 p.m.", "5:30am", "5:30 AM".
TIME_RE = re.compile(
    r"(\d{1,2}:\d{2})\s*([AaPp])\.?\s*[Mm]\.?(?=\s|$|[^\w.])",
)
DATE_RE = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b")
CANCELLED_RE = re.compile(r"\bcancell?ed\b", re.IGNORECASE)
ONCLICK_URL_RE = re.compile(r"window\.open\(['\"]([^'\"]+)['\"]")
CLIP_ID_RE = re.compile(r"[?&]clip_id=(\d+)")
EVENT_ID_RE = re.compile(r"[?&]event_id=(\d+)")

MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _host_allowed(host: str, allowed_hosts: frozenset[str]) -> bool:
    return host.lower() in allowed_hosts


def emit_url(
    href: Optional[str],
    base_url: str,
    allowed_hosts: frozenset[str],
    field_name: str = "url",
    row_label: str = "",
) -> str:
    """Validate and absolutize an extracted URL, returning empty on rejected input."""
    if not href:
        return ""

    raw = href.strip()
    low = raw.lower()
    log_value = row_label if row_label else ""

    if low == "#" or low.startswith("#"):
        logger.warning(
            "URL rejected:%s field=%s href=%r reason=fragment_placeholder",
            log_value if row_label else "",
            field_name,
            href,
        )
        return ""
    for scheme in BAD_SCHEMES:
        if low.startswith(scheme):
            logger.warning(
                "URL rejected:%s field=%s href=%r reason=bad_scheme",
                log_value if row_label else "",
                field_name,
                href,
            )
            return ""

    absolute = urljoin(base_url, raw)
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https"):
        logger.warning(
            "URL rejected:%s field=%s href=%r absolute=%r reason=non_http_scheme",
            log_value if row_label else "",
            field_name,
            href,
            absolute,
        )
        return ""

    host = (parsed.netloc.split(":")[0] or "").lower()
    if not _host_allowed(host, allowed_hosts):
        logger.warning(
            "URL rejected:%s field=%s href=%r absolute=%r host=%s reason=disallowed_host",
            log_value if row_label else "",
            field_name,
            href,
            absolute,
            host,
        )
        return ""

    return absolute


def _fetch_text_bounded(
    session: requests.Session,
    url: str,
    max_bytes: int = MAX_BODY_BYTES,
) -> str:
    with session.get(
        url,
        timeout=FETCH_TIMEOUT,
        stream=True,
        allow_redirects=True,
        verify=True,
    ) as response:
        response.raise_for_status()
        final_host = (urlparse(response.url).netloc.split(":")[0] or "").lower()
        if not _host_allowed(final_host, ALLOWED_HOSTS):
            raise ValueError(f"Redirect to disallowed host: {final_host} (started from {url})")

        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ValueError(f"Response from {url} exceeded {max_bytes} bytes")

        logger.info(
            "Fetched Dewey-Humboldt Granicus archive url=%s final_url=%s bytes=%s",
            url,
            response.url,
            len(body),
        )
        return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _extract_view_id(url: str) -> str:
    query = parse_qs(urlparse(url).query)
    view_ids = query.get("view_id") or query.get("View_ID")
    if view_ids and view_ids[0].strip():
        return view_ids[0].strip()
    logger.warning("view_id missing from input URL; falling back to DEFAULT_VIEW_ID=%s url=%s", DEFAULT_VIEW_ID, url)
    return DEFAULT_VIEW_ID


def _validate_vendor_fingerprint(html: str, url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.netloc.split(":")[0] or "").lower()
    if not host.endswith("granicus.com"):
        raise ValueError(f"Expected Granicus host ending in granicus.com; saw host={host!r} url={url!r}")

    soup = BeautifulSoup(html, "html.parser")
    has_listing_table = soup.select_one("table.listingTable") is not None
    has_archive_table = soup.select_one("table#archive.sortable") is not None
    has_footer_literal = "Granicus" in html
    has_archive_video = "archive-video.granicus.com" in html
    if not (has_listing_table or has_archive_table or has_footer_literal or has_archive_video):
        seen = {
            "table.listingTable": has_listing_table,
            "table#archive.sortable": has_archive_table,
            "literal_Granicus": has_footer_literal,
            "archive-video.granicus.com": has_archive_video,
        }
        raise ValueError(f"Granicus markup witness failed for {url}; looked for {seen}")

    witnesses = []
    if has_listing_table:
        witnesses.append("table.listingTable")
    if has_archive_table:
        witnesses.append("table#archive.sortable")
    if has_footer_literal:
        witnesses.append("literal_Granicus")
    if has_archive_video:
        witnesses.append("archive-video.granicus.com")
    logger.info("Vendor fingerprint confirmed for %s with markup witnesses=%s", url, witnesses)


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    if hasattr(value, "get_text"):
        text = value.get_text(" ", strip=True)
    else:
        text = BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)
    return " ".join(text.replace("\xa0", " ").split())


def _extract_meeting_date(cell_text: str, row_label: str) -> str:
    match = DATE_RE.search(cell_text)
    if not match:
        logger.warning(
            "meeting_date extraction returned empty: row=%s reason=no_month_day_year text=%r",
            row_label,
            cell_text[:220],
        )
        return ""

    month_text, day_text, year_text = match.groups()
    month = MONTHS.get(month_text.lower().rstrip(".")[:4]) or MONTHS.get(month_text.lower().rstrip(".")[:3])
    if month is None:
        logger.warning(
            "meeting_date extraction returned empty: row=%s reason=unknown_month month=%r text=%r",
            row_label,
            month_text,
            cell_text[:220],
        )
        return ""

    try:
        parsed = datetime(int(year_text), month, int(day_text))
    except ValueError as exc:
        logger.warning(
            "meeting_date extraction returned empty: row=%s reason=invalid_date date_parts=%r error=%s",
            row_label,
            match.group(0),
            exc,
        )
        return ""
    return parsed.strftime("%Y-%m-%d")


def _extract_meeting_time(cell_text: str, row_label: str, absent_by_construction: bool = False) -> str:
    match = TIME_RE.search(cell_text)
    if not match:
        if not absent_by_construction:
            reason = "unparsed_time_signal" if re.search(r"[AaPp]\.?\s*[Mm]\.?", cell_text) else "no_visible_time_signal"
            logger.warning(
                "meeting_time extraction returned empty: row=%s reason=%s text=%r",
                row_label,
                reason,
                cell_text[:220],
            )
        return ""

    time_part, meridiem = match.groups()
    hour_text, minute_text = time_part.split(":", 1)
    try:
        hour = int(hour_text)
    except ValueError:
        logger.warning(
            "meeting_time extraction returned empty: row=%s reason=invalid_hour text=%r",
            row_label,
            match.group(0),
        )
        return ""
    if hour < 1 or hour > 12:
        logger.warning(
            "meeting_time extraction returned empty: row=%s reason=hour_out_of_range text=%r",
            row_label,
            match.group(0),
        )
        return ""
    return f"{hour}:{minute_text} {meridiem.upper()}M"


_LOCATION_ABSENCE_LOGGED = False


def _meeting_location(row_label: str) -> str:
    # Granicus ViewPublisher exposes no meeting location in either listing
    # table. Log the structural absence once and aggregate affected rows to
    # avoid flooding the log with identical warnings.
    global _LOCATION_ABSENCE_LOGGED
    if not _LOCATION_ABSENCE_LOGGED:
        logger.warning(
            "Dewey-Humboldt meeting_location absent_by_construction: "
            "granicus_viewpublisher_archive_no_visible_location_column "
            "row_label=%r",
            row_label[:80] if row_label else "",
        )
        _LOCATION_ABSENCE_LOGGED = True
    return ""


def _classify_status(
    meeting_title: str,
    agenda_url: str,
    minutes_url: str,
    agenda_packet_url: str,
    row_label: str,
) -> str:
    if CANCELLED_RE.search(meeting_title):
        return "Cancelled"
    if minutes_url:
        return "Minutes Available"
    if agenda_url or agenda_packet_url:
        return "Agenda Available"
    return "Scheduled"


def _empty_record() -> dict[str, str]:
    return {field: "" for field in FIELD_NAMES}


def _record_sample(samples: dict[str, list[str]], key: str, value: str, limit: int = 10) -> None:
    if not value:
        return
    bucket = samples.setdefault(key, [])
    if len(bucket) < limit:
        bucket.append(value)


def _validate_table_headers(table: object, source: str) -> None:
    header_row = table.find("tr") if hasattr(table, "find") else None
    if header_row is None:
        raise ValueError(f"{source} table has no header row")
    headers = [_clean_text(cell).lower() for cell in header_row.find_all(["th", "td"])]
    if len(headers) < 2 or headers[0] != "name" or headers[1] != "date":
        raise ValueError(f"{source} table header shape changed; expected first columns Name/Date, saw {headers}")
    logger.info("%s table headers witnessed headers=%s", source, headers)


def _extract_onclick_url(anchor: object) -> str:
    onclick = anchor.get("onclick") if hasattr(anchor, "get") else ""
    if not onclick:
        logger.warning(
            "Dewey-Humboldt onclick_url abandoned: reason=anchor_has_no_onclick_attr "
            "anchor_text=%r",
            (anchor.get_text(strip=True)[:80] if hasattr(anchor, "get_text") else ""),
        )
        return ""
    match = ONCLICK_URL_RE.search(onclick)
    if not match:
        logger.warning(
            "Dewey-Humboldt onclick_url abandoned: reason=onclick_url_re_no_match "
            "onclick=%r",
            onclick[:120],
        )
        return ""
    return match.group(1)


def _id_from_text(text: str, pattern: re.Pattern[str]) -> str:
    match = pattern.search(text)
    return match.group(1) if match else ""


def _extract_meeting_id(row: object, row_label: str) -> str:
    for anchor in row.find_all("a") if hasattr(row, "find_all") else []:
        href = anchor.get("href") or ""
        onclick_url = _extract_onclick_url(anchor)
        meeting_id = _id_from_text(href, CLIP_ID_RE) or _id_from_text(onclick_url, CLIP_ID_RE)
        if meeting_id:
            return meeting_id
        meeting_id = _id_from_text(href, EVENT_ID_RE) or _id_from_text(onclick_url, EVENT_ID_RE)
        if meeting_id:
            return meeting_id
        data_event_id = (anchor.get("data-event-id") or "").strip()
        if data_event_id:
            return data_event_id

    logger.warning("meeting_id extraction returned empty: row=%s reason=no_clip_or_event_id", row_label)
    return ""


def _assign_document_link(
    record: dict[str, str],
    anchor: object,
    base_url: str,
    row_label: str,
    counters: Counter[str],
    samples: dict[str, list[str]],
) -> None:
    text = _clean_text(anchor).lower()
    href = anchor.get("href") or ""
    onclick_url = _extract_onclick_url(anchor)
    href_low = href.lower().strip()
    onclick_low = onclick_url.lower()

    if "agendaviewer.php" in href_low or text == "agenda":
        url = emit_url(href, base_url, ALLOWED_HOSTS, "agenda_url", row_label)
        if url:
            record["agenda_url"] = url
            counters["agenda_url_emitted"] += 1
        return

    if "minutesviewer.php" in href_low or text == "minutes":
        url = emit_url(href, base_url, ALLOWED_HOSTS, "minutes_url", row_label)
        if url:
            record["minutes_url"] = url
            counters["minutes_url_emitted"] += 1
        return

    if "packet" in text:
        url = emit_url(href, base_url, ALLOWED_HOSTS, "agenda_packet_url", row_label)
        if url:
            record["agenda_packet_url"] = url
            counters["agenda_packet_url_emitted"] += 1
        return

    if text == "video" or "mediaplayer.php" in onclick_low:
        if onclick_url:
            url = emit_url(onclick_url, base_url, ALLOWED_HOSTS, "video_url", row_label)
            if url:
                record["video_url"] = url
                counters["video_onclick_fallback_used"] += 1
                return
        url = emit_url(href, base_url, ALLOWED_HOSTS, "video_url", row_label)
        if url:
            record["video_url"] = url
            counters["video_url_emitted"] += 1
        return

    if text.startswith("open video only") or "asx.php" in href_low:
        if not record["video_url"]:
            url = emit_url(href, base_url, ALLOWED_HOSTS, "video_url", row_label)
            if url:
                record["video_url"] = url
                counters["video_asx_fallback_used"] += 1
        return

    if text == "ecomment" or "ecomment" in text:
        url = _extract_ecomment_url(anchor, base_url, row_label)
        if url:
            record["ecomment_url"] = url
            counters["ecomment_url_emitted"] += 1
        else:
            counters["ecomment_placeholder_dropped"] += 1
        return

    if text == "mp3 audio":
        counters["mp3_links_ignored_no_canonical_field"] += 1
        _record_sample(samples, "mp3_links_ignored_no_canonical_field", href)
        return

    logger.warning(
        "Unclassified row anchor dropped: row=%s text=%r href=%r onclick=%r",
        row_label,
        _clean_text(anchor),
        href,
        anchor.get("onclick") or "",
    )
    counters["unclassified_anchor_dropped"] += 1


def _extract_ecomment_url(anchor: object, base_url: str, row_label: str) -> str:
    candidates = []
    seen_candidates = set()
    for candidate in (
        anchor.get("href") or "",
        anchor.get("link") or "",
        anchor.get("data-url") or "",
        anchor.get("data-href") or "",
    ):
        if candidate and candidate not in seen_candidates:
            candidates.append(candidate)
            seen_candidates.add(candidate)
    for candidate in candidates:
        url = emit_url(candidate, base_url, ALLOWED_HOSTS, "ecomment_url", row_label)
        if url:
            return url

    logger.warning(
        "ecomment_url extraction returned empty: row=%s reason=placeholder_without_allowed_url href=%r link=%r data_event_id=%r",
        row_label,
        anchor.get("href") or "",
        anchor.get("link") or "",
        anchor.get("data-event-id") or "",
    )
    return ""


def _parse_row(
    row: object,
    base_url: str,
    source: str,
    row_index: int,
    counters: Counter[str],
    samples: dict[str, list[str]],
) -> Optional[dict[str, str]]:
    cells = row.find_all("td") if hasattr(row, "find_all") else []
    row_label = f"{source}:{row_index}"
    if len(cells) < 2:
        logger.warning("Dropped row: row=%s reason=too_few_cells cell_count=%s", row_label, len(cells))
        counters["drop_too_few_cells"] += 1
        return None

    record = _empty_record()
    title = _clean_text(cells[0])
    date_text = _clean_text(cells[1])
    if not title:
        logger.warning("meeting_title extraction returned empty: row=%s reason=empty_title_cell", row_label)
        counters["drop_missing_title"] += 1
        return None

    meeting_date = _extract_meeting_date(date_text, row_label)
    if not meeting_date:
        logger.warning("Dropped row: row=%s reason=missing_required_date title=%r date_text=%r", row_label, title, date_text)
        counters["drop_missing_date"] += 1
        return None

    archive_time_absent = source == "archive"
    meeting_time = _extract_meeting_time(date_text, row_label, absent_by_construction=archive_time_absent)
    if archive_time_absent and not meeting_time:
        counters["archive_rows_without_visible_time"] += 1

    record["meeting_title"] = title
    record["meeting_date"] = meeting_date
    record["meeting_time"] = meeting_time
    record["meeting_location"] = _meeting_location(row_label)
    record["meeting_id"] = _extract_meeting_id(row, row_label)

    for anchor in row.find_all("a"):
        _assign_document_link(record, anchor, base_url, row_label, counters, samples)

    record["meeting_status"] = _classify_status(
        record["meeting_title"],
        record["agenda_url"],
        record["minutes_url"],
        record["agenda_packet_url"],
        row_label,
    )

    logger.debug(
        "Meeting emitted: row=%s title=%r date=%s time=%r status=%s id=%r agenda=%s minutes=%s video=%s ecomment=%s",
        row_label,
        record["meeting_title"],
        record["meeting_date"],
        record["meeting_time"],
        record["meeting_status"],
        record["meeting_id"],
        bool(record["agenda_url"]),
        bool(record["minutes_url"]),
        bool(record["video_url"]),
        bool(record["ecomment_url"]),
    )
    return {field: str(record.get(field, "") or "") for field in FIELD_NAMES}


def _parse_table(
    soup: BeautifulSoup,
    selector: str,
    source: str,
    base_url: str,
    counters: Counter[str],
    samples: dict[str, list[str]],
) -> list[dict[str, str]]:
    table = soup.select_one(selector)
    if table is None:
        logger.warning("Meeting table absent: source=%s selector=%s", source, selector)
        counters[f"{source}_table_absent"] += 1
        return []

    _validate_table_headers(table, source)
    rows = table.find_all("tr")[1:]
    meetings: list[dict[str, str]] = []
    counters[f"{source}_rows_seen"] += len(rows)

    for index, row in enumerate(rows, start=1):
        parsed = _parse_row(row, base_url, source, index, counters, samples)
        if parsed is None:
            counters["rows_dropped"] += 1
            continue
        meetings.append(parsed)
        counters["rows_accepted"] += 1
    return meetings


def scrape_calendar(url: str) -> list[dict]:
    """Scrape Dewey-Humboldt's Granicus ViewPublisher archive."""
    if not url:
        url = DEFAULT_CALENDAR_URL
        logger.warning("Empty scrape URL supplied; falling back to DEFAULT_CALENDAR_URL=%s", DEFAULT_CALENDAR_URL)

    view_id = _extract_view_id(url)
    logger.info("Using Dewey-Humboldt Granicus view_id=%s parsed_from_url=%s", view_id, url)
    logger.warning(
        "meeting_location absent by construction: Dewey-Humboldt Granicus ViewPublisher rows expose title, date/time, and document cells, but no per-row location column; emitting empty meeting_location for all rows"
    )
    logger.warning(
        "archive meeting_time absent by construction: table#archive exposes date and duration but no visible start time; emitting empty meeting_time for archive rows"
    )

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        }
    )
    html = _fetch_text_bounded(session, url, MAX_BODY_BYTES)
    _validate_vendor_fingerprint(html, url)

    soup = BeautifulSoup(html, "html.parser")
    counters: Counter[str] = Counter()
    samples: dict[str, list[str]] = {}
    meetings: list[dict[str, str]] = []
    meetings.extend(_parse_table(soup, "table.listingTable", "upcoming", url, counters, samples))
    meetings.extend(_parse_table(soup, "table#archive", "archive", url, counters, samples))

    if not meetings:
        logger.warning("No Dewey-Humboldt meetings emitted after parsing witnessed Granicus tables url=%s", url)

    logger.info(
        "Dewey-Humboldt scrape complete rows_seen=%s rows_accepted=%s rows_dropped=%s counters=%s",
        counters["upcoming_rows_seen"] + counters["archive_rows_seen"],
        counters["rows_accepted"],
        counters["rows_dropped"],
        dict(sorted(counters.items())),
    )
    if counters["mp3_links_ignored_no_canonical_field"]:
        logger.warning(
            "Dropped MP3 Audio links because canonical schema has no audio_url field: count=%s first_samples=%s",
            counters["mp3_links_ignored_no_canonical_field"],
            samples.get("mp3_links_ignored_no_canonical_field", []),
        )
    return meetings


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    rows = scrape_calendar(DEFAULT_CALENDAR_URL)
    print(f"Found {len(rows)} meetings.")
    for r in rows[:2]:
        print(r)
