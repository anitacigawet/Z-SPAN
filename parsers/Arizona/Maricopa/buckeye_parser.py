"""Buckeye — Granicus ViewPublisher meeting parser."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag

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

ALLOWED_HOSTS = {
    "buckeyeaz.granicus.com",
    "granicus.com",
    "www.buckeyeaz.gov",
    "buckeyeaz.gov",
    # Witnessed live on Buckeye ViewPublisher packet PDF links.
    "d3n9y02raazwpg.cloudfront.net",
}

BAD_SCHEMES = ("javascript:", "data:", "vbscript:", "mailto:", "file:", "ftp:")
MAX_RESPONSE_BYTES = 10_000_000

# Test cases: "5:30 PM", "5:30 P.M.", "5:30 p.m.", "5:30PM", "9:00 AM".
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*([AaPp])\.?[Mm]\.?(?=\s|$|[^\w.])")
DATE_RE = re.compile(r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})")
CANCELLED_RE = re.compile(r"\bcancell?ed\b", re.IGNORECASE)


class VendorFingerprintError(ValueError):
    """Raised when the fetched page is not a witnessed Granicus surface."""


def scrape_calendar(url: str) -> list[dict]:
    """Scrape Buckeye meetings from Granicus ViewPublisher."""
    session = requests.Session()
    try:
        html, headers, final_url, status_code = _fetch_text_bounded(
            session,
            url,
            ALLOWED_HOSTS,
            max_bytes=MAX_RESPONSE_BYTES,
        )
    except (requests.RequestException, ValueError) as exc:
        logger.warning(
            "buckeye: fetch failed; returning honest-empty [] for %s; failure=%s",
            url,
            exc,
        )
        return []

    try:
        _validate_vendor_fingerprint(html, dict(headers))
    except VendorFingerprintError:
        return []

    soup = BeautifulSoup(html, "html.parser")
    tables = soup.select("table.listingTable")
    if not tables:
        logger.warning(
            "buckeye: no table.listingTable rows witnessed after Granicus fingerprint; returning []"
        )
        return []

    logger.info(
        "buckeye: structural absence: meeting_location and ecomment_url are not exposed "
        "on Granicus ViewPublisher listing rows; emitting '' for those fields"
    )

    meetings: list[dict] = []
    candidate_rows = 0
    dropped_rows = 0
    blank_time_rows: list[str] = []
    blank_video_rows = 0
    no_video_header_tables = 0

    for table_index, table in enumerate(tables, start=1):
        headers_text = _table_headers(table)
        header_map = _header_map(headers_text)
        if not headers_text:
            logger.warning(
                "buckeye: table %s dropped because header row was absent; headers=%r",
                table_index,
                headers_text,
            )
            dropped_rows += len(table.find_all("tr"))
            continue
        if "meeting name" not in header_map or "date" not in header_map:
            logger.warning(
                "buckeye: table %s dropped because required headers were missing; headers=%r",
                table_index,
                headers_text,
            )
            dropped_rows += len(table.find_all("tr"))
            continue

        video_index = _find_header_index(header_map, ("video",))
        if video_index is None:
            no_video_header_tables += 1

        for row_index, tr in enumerate(table.find_all("tr"), start=1):
            cells = tr.find_all("td")
            if not cells:
                continue
            candidate_rows += 1
            row_label = f"table={table_index} row={row_index}"

            title_cell = _cell_at(cells, header_map["meeting name"])
            date_cell = _cell_at(cells, header_map["date"])
            title = _clean_cell_text(title_cell)
            date_text = _clean_cell_text(date_cell)
            if not title:
                logger.warning(
                    "buckeye: dropping %s because meeting_title was empty; row_text=%r",
                    row_label,
                    _clean_cell_text(tr),
                )
                dropped_rows += 1
                continue
            meeting_date, meeting_time = _extract_date_time(date_text, row_label)
            if not meeting_date:
                logger.warning(
                    "buckeye: dropping %s title=%r because meeting_date could not be parsed; "
                    "date_cell=%r",
                    row_label,
                    title,
                    date_text,
                )
                dropped_rows += 1
                continue
            if not meeting_time:
                blank_time_rows.append(f"{title} {meeting_date}")

            agenda_url = _extract_link_from_column(
                cells,
                _find_header_index(header_map, ("agenda",)),
                "agenda_url",
                row_label,
                final_url,
                ALLOWED_HOSTS,
            )
            minutes_url = _extract_link_from_column(
                cells,
                _find_header_index(header_map, ("minutes", "legal action")),
                "minutes_url",
                row_label,
                final_url,
                ALLOWED_HOSTS,
            )
            agenda_packet_url = _extract_link_from_column(
                cells,
                _find_header_index(header_map, ("packet",)),
                "agenda_packet_url",
                row_label,
                final_url,
                ALLOWED_HOSTS,
            )
            video_url = ""
            if video_index is not None:
                video_url = _extract_link_from_column(
                    cells,
                    video_index,
                    "video_url",
                    row_label,
                    final_url,
                    ALLOWED_HOSTS,
                )
            if not video_url:
                blank_video_rows += 1

            meeting_id = _extract_meeting_id(agenda_url, row_label)
            status = _meeting_status(title, agenda_url, minutes_url, agenda_packet_url)

            meeting = {
                "meeting_title": title,
                "meeting_date": meeting_date,
                "meeting_time": meeting_time,
                "meeting_location": "",
                "meeting_status": status,
                "agenda_url": agenda_url,
                "minutes_url": minutes_url,
                "video_url": video_url,
                "agenda_packet_url": agenda_packet_url,
                "ecomment_url": "",
                "meeting_id": meeting_id,
            }
            _assert_schema(meeting, row_label)
            meetings.append(meeting)

    if blank_time_rows:
        logger.warning(
            "buckeye: emitted '' for meeting_time on %s rows because the ViewPublisher Date "
            "cells contained no time token; first_10=%r",
            len(blank_time_rows),
            blank_time_rows[:10],
        )
    if no_video_header_tables:
        logger.warning(
            "buckeye: emitted '' for video_url on %s rows because %s listing tables had no "
            "Video header; RSS video feed was not joined because its dates are not same-row "
            "ViewPublisher evidence",
            blank_video_rows,
            no_video_header_tables,
        )
    logger.info(
        "buckeye: fetched %s status=%s final_url=%s, parsed %s candidate rows, "
        "dropped %s rows, emitted %s meetings",
        url,
        status_code,
        final_url,
        candidate_rows,
        dropped_rows,
        len(meetings),
    )
    return meetings


def _fetch_text_bounded(
    session: requests.Session,
    url: str,
    allowed_hosts: set[str],
    *,
    max_bytes: int,
) -> tuple[str, requests.structures.CaseInsensitiveDict[str], str, int]:
    with session.get(
        url,
        timeout=30,
        stream=True,
        allow_redirects=True,
        verify=True,
    ) as response:
        final_host = _host(response.url)
        if final_host not in allowed_hosts:
            raise ValueError(
                f"Redirect to disallowed host: {final_host} (started from {url})"
            )
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ValueError(f"Response from {url} exceeded {max_bytes} bytes")
        encoding = response.encoding or "utf-8"
        return body.decode(encoding, errors="replace"), response.headers, response.url, response.status_code


def _validate_vendor_fingerprint(html: str, headers: dict) -> None:
    header_names = {str(key).lower() for key in headers}
    has_granicus_header = "x-granicus-server" in header_names
    has_viewpublisher = "ViewPublisher" in html
    has_listing_marker = (
        'class="listingTable"' in html
        or "archiveMeetingTable" in html
        or ("<table" in html and 'cellpadding="1"' in html)
    )
    if has_granicus_header:
        logger.info("buckeye: Granicus fingerprint witnessed via X-Granicus-Server header")
        return
    if has_viewpublisher and has_listing_marker:
        logger.info(
            "buckeye: Granicus fingerprint witnessed via ViewPublisher + listing table markup"
        )
        return
    logger.warning(
        "buckeye: vendor fingerprint mismatch; looked_for=X-Granicus-Server header OR "
        "ViewPublisher plus listingTable/archiveMeetingTable/table marker; witnessed "
        "x_granicus=%s viewpublisher=%s listing_marker=%s; returning honest-empty []",
        has_granicus_header,
        has_viewpublisher,
        has_listing_marker,
    )
    raise VendorFingerprintError("Buckeye Granicus fingerprint not witnessed")


def emit_url(
    href: str,
    base_url: str,
    allowed_hosts: set[str],
    *,
    field_name: str = "url",
    row_label: str = "",
) -> str:
    label = f"{row_label} {field_name}".strip()
    if not href:
        logger.warning("buckeye: rejected %s URL; rejected_value=%r reason=empty_href", label, href)
        return ""
    raw = href.strip()
    low = raw.lower()
    for bad_scheme in BAD_SCHEMES:
        if low.startswith(bad_scheme):
            logger.warning(
                "buckeye: rejected %s URL; rejected_value=%r reason=bad_scheme_%s",
                label,
                href,
                bad_scheme.rstrip(":"),
            )
            return ""
    absolute = urljoin(base_url, raw)
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https"):
        logger.warning(
            "buckeye: rejected %s URL; rejected_value=%r absolute=%r reason=bad_scheme_%s",
            label,
            href,
            absolute,
            parsed.scheme,
        )
        return ""
    emit_host = _host(absolute)
    if emit_host not in allowed_hosts:
        logger.warning(
            "buckeye: rejected %s URL; rejected_value=%r absolute=%r host=%s "
            "reason=host_not_allowlisted",
            label,
            href,
            absolute,
            emit_host,
        )
        return ""
    return absolute


def _table_headers(table: Tag) -> list[str]:
    return [_clean_cell_text(th).lower() for th in table.find_all("th")]


def _header_map(headers: list[str]) -> dict[str, int]:
    return {_normalize_header(header): index for index, header in enumerate(headers)}


def _normalize_header(header: str) -> str:
    return " ".join(header.lower().split())


def _find_header_index(header_map: dict[str, int], needles: tuple[str, ...]) -> int | None:
    for header, index in header_map.items():
        if any(needle in header for needle in needles):
            return index
    return None


def _cell_at(cells: list[Tag], index: int | None) -> Tag | None:
    if index is None or index >= len(cells):
        return None
    return cells[index]


def _clean_cell_text(cell: Tag | None) -> str:
    if cell is None:
        return ""
    return " ".join(cell.get_text(" ", strip=True).split())


def _extract_date_time(date_text: str, row_label: str) -> tuple[str, str]:
    if not date_text:
        logger.warning(
            "buckeye: %s meeting_date extraction returned empty; rejected_input=%r",
            row_label,
            date_text,
        )
        return "", ""
    date_match = DATE_RE.search(date_text)
    if not date_match:
        logger.warning(
            "buckeye: %s meeting_date extraction failed; rejected_input=%r",
            row_label,
            date_text,
        )
        return "", ""
    try:
        meeting_date = datetime.strptime(
            " ".join(date_match.groups()),
            "%B %d %Y",
        ).date().isoformat()
    except ValueError:
        try:
            meeting_date = datetime.strptime(
                " ".join(date_match.groups()),
                "%b %d %Y",
            ).date().isoformat()
        except ValueError:
            logger.warning(
                "buckeye: %s meeting_date parse failed; rejected_input=%r",
                row_label,
                date_text,
            )
            return "", ""

    time_match = TIME_RE.search(date_text)
    if not time_match:
        return meeting_date, ""
    hour = int(time_match.group(1))
    minute = time_match.group(2)
    ampm = time_match.group(3).upper() + "M"
    if hour < 1 or hour > 12:
        logger.warning(
            "buckeye: %s meeting_time rejected; rejected_input=%r reason=hour_out_of_range",
            row_label,
            date_text,
        )
        return meeting_date, ""
    return meeting_date, f"{hour}:{minute} {ampm}"


def _extract_link_from_column(
    cells: list[Tag],
    index: int | None,
    field_name: str,
    row_label: str,
    base_url: str,
    allowed_hosts: set[str],
) -> str:
    cell = _cell_at(cells, index)
    if cell is None:
        return ""
    anchor = cell.find("a", href=True)
    if anchor is None:
        cell_text = _clean_cell_text(cell)
        if cell_text:
            logger.warning(
                "buckeye: %s %s had non-empty cell with no href; rejected_input=%r",
                row_label,
                field_name,
                cell_text,
            )
        return ""
    href = anchor.get("href", "")
    emitted = emit_url(
        href,
        base_url,
        allowed_hosts,
        field_name=field_name,
        row_label=row_label,
    )
    if not emitted:
        for attr_name, attr_value in anchor.attrs.items():
            if attr_name.startswith("data-") or attr_name == "onclick":
                logger.warning(
                    "buckeye: %s %s fallback attribute inspected after href rejection; "
                    "attr=%s value=%r",
                    row_label,
                    field_name,
                    attr_name,
                    attr_value,
                )
    return emitted


def _extract_meeting_id(agenda_url: str, row_label: str) -> str:
    if not agenda_url:
        logger.warning(
            "buckeye: %s meeting_id emitted '' because agenda_url carried no event_id evidence",
            row_label,
        )
        return ""
    query = parse_qs(urlparse(agenda_url).query)
    event_ids = query.get("event_id", [])
    if not event_ids or not event_ids[0]:
        logger.warning(
            "buckeye: %s meeting_id extraction failed; agenda_url=%r reason=no_event_id",
            row_label,
            agenda_url,
        )
        return ""
    return event_ids[0]


def _meeting_status(
    title: str,
    agenda_url: str,
    minutes_url: str,
    agenda_packet_url: str,
) -> str:
    if CANCELLED_RE.search(title[:300]):
        return "Cancelled"
    if minutes_url:
        return "Minutes Available"
    if agenda_url or agenda_packet_url:
        return "Agenda Available"
    return "Scheduled"


def _assert_schema(meeting: dict, row_label: str) -> None:
    keys = tuple(meeting.keys())
    if keys != FIELD_NAMES:
        raise ValueError(f"{row_label} schema mismatch: {keys!r}")
    non_strings = [key for key, value in meeting.items() if not isinstance(value, str)]
    if non_strings:
        raise TypeError(f"{row_label} non-string fields: {non_strings!r}")


def _host(url: str) -> str:
    return (urlparse(url).netloc.split(":")[0] or "").lower()


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    meetings = scrape_calendar("https://buckeyeaz.granicus.com/ViewPublisher.php?view_id=1")
    print(f"row_count: {len(meetings)}")
    print(json.dumps(meetings[:2], indent=2))
