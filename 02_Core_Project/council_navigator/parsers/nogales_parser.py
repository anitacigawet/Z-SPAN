"""Current-month-forward Mayor and City Council meetings from Nogales Granicus."""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import date, datetime
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from polite_http import make_session


logger = logging.getLogger(__name__)

DEFAULT_URL = "https://www.nogalesaz.gov/165/Meetings-Agendas"
CITY_HOSTS = {"nogalesaz.gov", "www.nogalesaz.gov"}
GRANICUS_HOST = "nogalesaz.granicus.com"
ALLOWED_FETCH_HOSTS = CITY_HOSTS | {GRANICUS_HOST}
ALLOWED_EMIT_HOSTS = {
    GRANICUS_HOST,
    "d3n9y02raazwpg.cloudfront.net",
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
BAD_SCHEMES = ("javascript:", "data:", "vbscript:", "file:", "mailto:", "ftp:", "gopher:")
BLOCKING_HTTP_STATUSES = {401, 403, 407, 423, 429, 451}
BLOCK_PAGE_RE = re.compile(r"\b(?:access denied|attention required|just a moment|captcha)\b", re.IGNORECASE)
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
COUNCIL_TITLE_RE = re.compile(r"^(?:(?:mayor\s+(?:and|&)\s+)?city council)\b", re.IGNORECASE)
DATE_RE = re.compile(r"([A-Z][a-z]+\s+\d{1,2},\s*\d{4})")
TIME_RE = re.compile(
    r"(?<!\d)([0-9]{1,2})(?::([0-9]{2}))?\s*([AP])\.?M\.?(?=\s|$|[^\w.])",
    re.IGNORECASE,
)
WINDOW_OPEN_RE = re.compile(r"window\.open\(\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
MAX_RESPONSE_BYTES = 8_000_000
REQUEST_TIMEOUT = 45


class SourceBlockedError(RuntimeError):
    """Raised only when a recognizable upstream block page was witnessed."""


def scrape_calendar(url: str) -> list[dict[str, str]]:
    """Return Nogales Mayor and City Council rows from the current month onward."""
    wrapper_url = _validated_wrapper_url(url or DEFAULT_URL)
    current_floor = date.today().replace(day=1)
    session = make_session()
    counters: Counter[str] = Counter()

    logger.warning(
        "field_absence field=meeting_location reason=granicus_viewpublisher_exposes_no_per_row_location_signal"
    )
    logger.warning(
        "field_absence field=ecomment_url reason=granicus_viewpublisher_exposes_no_ecomment_signal"
    )

    try:
        wrapper_html = _fetch_html_bounded(session, wrapper_url, allowed_hosts=CITY_HOSTS)
        publisher_url = _discover_publisher_url(wrapper_html, wrapper_url)
        publisher_html = _fetch_html_bounded(
            session,
            publisher_url,
            allowed_hosts={GRANICUS_HOST},
        )
        meetings = _parse_publisher(
            publisher_html,
            publisher_url,
            current_floor=current_floor,
            counters=counters,
        )
    except requests.HTTPError as exc:
        if not _is_witnessed_http_blocker(exc):
            raise
        _log_source_blocked(exc=exc)
        return []
    except SourceBlockedError as exc:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "source_blocked phase=html_fingerprint failure_shape=honest-empty "
            "missing_data_scope=all_current_month_forward_meetings error=%r",
            exc,
        )
        return []

    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.info("scrape_summary counters=%s", dict(sorted(counters.items())))
        return []

    meetings.sort(
        key=lambda item: (
            item["meeting_date"],
            item["meeting_time"],
            item["meeting_title"].casefold(),
            item["meeting_id"],
        )
    )
    _assert_schema(meetings)
    logger.info("scrape_summary counters=%s", dict(sorted(counters.items())))
    return meetings


def _discover_publisher_url(html: str, wrapper_url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    title = _clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    heading = soup.find("h1")
    heading_text = _clean_text(heading.get_text(" ", strip=True)) if heading else ""
    iframe = soup.find("iframe", src=True)
    if "Meetings & Agendas" not in title or heading_text != "Meetings & Agendas" or iframe is None:
        page_text = _clean_text(soup.get_text(" ", strip=True))[:1000]
        if BLOCK_PAGE_RE.search(f"{title} {page_text}"):
            raise SourceBlockedError(f"recognized block page at {wrapper_url}: title={title!r}")
        raise ValueError(
            f"Nogales official wrapper fingerprint drift: title={title!r}, h1={heading_text!r}, iframe={iframe is not None}"
        )

    publisher_url = urljoin(wrapper_url, str(iframe.get("src", "") or ""))
    parsed = urlparse(publisher_url)
    query = parse_qs(parsed.query)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != GRANICUS_HOST
        or parsed.path != "/ViewPublisher.php"
        or query.get("view_id") != ["1"]
    ):
        raise ValueError(f"Nogales wrapper exposed an unexpected publisher iframe: {publisher_url!r}")
    logger.info(
        "vendor_fingerprint witness=official_civicengage_wrapper_granicus_iframe wrapper=%s publisher=%s",
        wrapper_url,
        publisher_url,
    )
    return publisher_url


def _parse_publisher(
    html: str,
    publisher_url: str,
    *,
    current_floor: date,
    counters: Counter[str],
) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    title = _clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if title != "Nogales AZ - Granicus Content":
        page_text = _clean_text(soup.get_text(" ", strip=True))[:1000]
        if BLOCK_PAGE_RE.search(f"{title} {page_text}"):
            raise SourceBlockedError(f"recognized block page at {publisher_url}: title={title!r}")
        raise ValueError(f"Nogales Granicus title fingerprint drift: {title!r}")

    archive_headers = (
        "Name",
        "Date",
        "Duration",
        "Agenda",
        "Minutes/Meeting Notes",
        "Video",
        "Agenda Packet",
    )
    upcoming_headers = (
        "Name",
        "Date",
        "Agenda",
        "Live Video",
        "Agenda Packet",
    )
    recognized_headers: set[tuple[str, ...]] = set()
    council_rows: list[tuple[list, tuple[str, ...]]] = []
    for table in soup.select("table.listingTable"):
        header = table.find("tr")
        header_cells = tuple(
            _clean_text(cell.get_text(" ", strip=True))
            for cell in header.find_all(["th", "td"], recursive=False)
        ) if header else ()
        if header_cells in {archive_headers, upcoming_headers}:
            recognized_headers.add(header_cells)
        table_council_rows: list[list] = []
        for row in table.select("tr.listingRow"):
            cells = row.find_all("td", recursive=False)
            if not cells:
                continue
            row_title = _clean_text(cells[0].get_text(" ", strip=True))
            if COUNCIL_TITLE_RE.search(row_title):
                table_council_rows.append(cells)
        if table_council_rows:
            if header_cells not in {archive_headers, upcoming_headers}:
                raise ValueError(
                    "Nogales council table exposed an unrecognized header layout: "
                    f"headers={header_cells!r}, rows={len(table_council_rows)}"
                )
            if any(len(cells) != len(header_cells) for cells in table_council_rows):
                raise ValueError(
                    "Nogales council row cell count does not match its witnessed header: "
                    f"headers={header_cells!r}, cell_counts={[len(cells) for cells in table_council_rows[:10]]!r}"
                )
            council_rows.extend((cells, header_cells) for cells in table_council_rows)

    if archive_headers not in recognized_headers or upcoming_headers not in recognized_headers or not council_rows:
        raise ValueError(
            "Nogales Granicus fingerprint drift: "
            f"recognized_headers={sorted(recognized_headers)!r}, council_rows={len(council_rows)}"
        )
    logger.info(
        "vendor_fingerprint witness=granicus_listing_table_and_mayor_city_council_rows "
        "publisher=%s council_rows=%d",
        publisher_url,
        len(council_rows),
    )

    meetings: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    for cells, header_cells in council_rows:
        counters["council_rows_seen"] += 1
        row_title = _clean_text(cells[0].get_text(" ", strip=True))
        date_text = _clean_text(cells[1].get_text(" ", strip=True))
        date_match = DATE_RE.search(date_text)
        if not date_match:
            raise ValueError(
                f"Nogales council row has no parseable date: title={row_title!r}, date_cell={date_text!r}"
            )
        meeting_day = datetime.strptime(
            re.sub(r"\s+", " ", date_match.group(1)),
            "%B %d, %Y",
        ).date()
        if meeting_day < current_floor:
            counters["rows_dropped_before_current_floor"] += 1
            continue

        meeting_time = _extract_time(date_text, row_label=row_title)
        is_archive = header_cells == archive_headers
        agenda_cell = cells[3] if is_archive else cells[2]
        minutes_cell = cells[4] if is_archive else None
        video_cell = cells[5] if is_archive else cells[3]
        packet_cell = cells[6] if is_archive else cells[4]

        agenda_url = _first_link_url(
            agenda_cell,
            publisher_url,
            field="agenda_url",
            row_label=row_title,
            required_label="agenda",
        )
        minutes_url = "" if minutes_cell is None else _first_link_url(
            minutes_cell,
            publisher_url,
            field="minutes_url",
            row_label=row_title,
            required_label="minute",
        )
        video_url = _video_url(video_cell, publisher_url, row_label=row_title)
        agenda_packet_url = _first_link_url(
            packet_cell,
            publisher_url,
            field="agenda_packet_url",
            row_label=row_title,
            required_label="packet",
        )
        meeting_id = _extract_clip_id(cells, row_label=row_title)
        dedupe_key = meeting_id or f"{meeting_day.isoformat()}|{meeting_time}|{row_title.casefold()}"
        if dedupe_key in seen_keys:
            counters["duplicate_rows_dropped"] += 1
            logger.warning(
                "drop_duplicate_council_row key=%r title=%r meeting_date=%s",
                dedupe_key,
                row_title,
                meeting_day.isoformat(),
            )
            continue
        seen_keys.add(dedupe_key)

        meeting = {
            "meeting_title": row_title,
            "meeting_date": meeting_day.isoformat(),
            "meeting_time": meeting_time,
            "meeting_location": "",
            "meeting_status": _derive_status(
                row_title,
                agenda_url,
                minutes_url,
                agenda_packet_url,
            ),
            "agenda_url": agenda_url,
            "minutes_url": minutes_url,
            "video_url": video_url,
            "agenda_packet_url": agenda_packet_url,
            "ecomment_url": "",
            "meeting_id": meeting_id,
        }
        meetings.append({field: meeting[field] for field in CANONICAL_FIELDS})
        counters["rows_accepted"] += 1
    return meetings


def _first_link_url(
    cell,
    base_url: str,
    *,
    field: str,
    row_label: str,
    required_label: str,
) -> str:
    matching = []
    for anchor in cell.find_all("a", href=True):
        label = _clean_text(anchor.get_text(" ", strip=True)).casefold()
        if required_label in label:
            matching.append(anchor)
    if not matching:
        return ""
    if len(matching) > 1:
        logger.warning(
            "multiple_field_links field=%s row=%r count=%d using_first=true",
            field,
            row_label,
            len(matching),
        )
    return _emit_url(
        str(matching[0].get("href", "") or ""),
        base_url,
        field=field,
        row_label=row_label,
    )


def _video_url(cell, base_url: str, *, row_label: str) -> str:
    anchor = cell.find("a")
    if anchor is None:
        return ""
    href = str(anchor.get("href", "") or "")
    if href and not href.lower().startswith(BAD_SCHEMES) and href != "#":
        return _emit_url(href, base_url, field="video_url", row_label=row_label)

    onclick = str(anchor.get("onclick", "") or "")
    match = WINDOW_OPEN_RE.search(onclick)
    if not match:
        logger.warning(
            "drop_video_placeholder_without_onclick_fallback row=%r href=%r onclick=%r",
            row_label,
            href,
            onclick,
        )
        return ""
    return _emit_url(match.group(1), base_url, field="video_url", row_label=row_label)


def _extract_clip_id(cells: list, *, row_label: str) -> str:
    clip_ids: set[str] = set()
    for cell in cells:
        for anchor in cell.find_all("a"):
            values = [str(anchor.get("href", "") or ""), str(anchor.get("onclick", "") or "")]
            for value in values:
                for match in re.finditer(r"(?:[?&]|&amp;)clip_id=(\d+)", value):
                    clip_ids.add(match.group(1))
    if len(clip_ids) > 1:
        raise ValueError(f"Nogales row exposes conflicting clip ids: row={row_label!r}, ids={sorted(clip_ids)}")
    if not clip_ids:
        logger.warning("meeting_id_absent row=%r", row_label)
        return ""
    return next(iter(clip_ids))


def _extract_time(value: str, *, row_label: str) -> str:
    match = TIME_RE.search(value[:1000])
    if not match:
        logger.warning("meeting_time_absent row=%r source_text=%r", row_label, value[:500])
        return ""
    hour = int(match.group(1))
    minute = match.group(2) or "00"
    if not 1 <= hour <= 12:
        logger.warning("meeting_time_dropped_invalid_hour row=%r raw=%r", row_label, match.group(0))
        return ""
    return f"{hour}:{minute} {match.group(3).upper()}M"


def _derive_status(title: str, agenda_url: str, minutes_url: str, agenda_packet_url: str) -> str:
    if CANCELLED_RE.search(title[:500]):
        return "Cancelled"
    if minutes_url:
        return "Minutes Available"
    if agenda_url or agenda_packet_url:
        return "Agenda Available"
    return "Scheduled"


def _emit_url(href: str, base_url: str, *, field: str, row_label: str) -> str:
    value = href.strip()
    if not value:
        return ""
    if value.lower().startswith(BAD_SCHEMES) or value == "#":
        logger.warning("drop_url_bad_scheme field=%s row=%r href=%r", field, row_label, href)
        return ""
    absolute = urljoin(base_url, value)
    parsed = urlparse(absolute)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        logger.warning("drop_url_non_https field=%s row=%r href=%r absolute=%r", field, row_label, href, absolute)
        return ""
    if not _host_allowed(host, ALLOWED_EMIT_HOSTS):
        logger.warning(
            "drop_url_disallowed_host field=%s row=%r href=%r host=%r allowed=%r",
            field,
            row_label,
            href,
            host,
            sorted(ALLOWED_EMIT_HOSTS),
        )
        return ""
    return absolute


def _fetch_html_bounded(
    session: requests.Session,
    url: str,
    *,
    allowed_hosts: set[str],
) -> str:
    with session.get(url, timeout=REQUEST_TIMEOUT, stream=True, allow_redirects=True) as response:
        response.raise_for_status()
        final_host = (urlparse(response.url).hostname or "").lower()
        if not _host_allowed(final_host, allowed_hosts):
            raise ValueError(f"Redirect to disallowed host: {final_host} (started from {url})")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Response from {url} exceeded {MAX_RESPONSE_BYTES} bytes")
        return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _validated_wrapper_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not _host_allowed(host, CITY_HOSTS):
        raise ValueError(f"Nogales wrapper URL must be HTTPS on nogalesaz.gov: {url!r}")
    return url


def _host_allowed(host: str, allowed_hosts: set[str]) -> bool:
    return any(host == allowed or host.endswith("." + allowed) for allowed in allowed_hosts)


def _is_witnessed_http_blocker(exc: requests.HTTPError) -> bool:
    return exc.response is not None and exc.response.status_code in BLOCKING_HTTP_STATUSES


def _log_source_blocked(*, exc: requests.HTTPError) -> None:
    response = exc.response
    logger.warning("health_empty_kind=source_blocked")
    logger.warning(
        "source_blocked phase=http_fetch status=%s final_url=%s failure_shape=honest-empty "
        "missing_data_scope=all_current_month_forward_meetings",
        response.status_code if response is not None else 0,
        response.url if response is not None else "",
    )


def _clean_text(value: str) -> str:
    text = BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _assert_schema(meetings: list[dict[str, str]]) -> None:
    for index, meeting in enumerate(meetings):
        if tuple(meeting) != CANONICAL_FIELDS:
            raise ValueError(f"Row {index} schema mismatch: {tuple(meeting)}")
        for field, value in meeting.items():
            if not isinstance(value, str):
                raise ValueError(f"Row {index} field {field} is not str: {type(value).__name__}")
        for field in ("agenda_url", "minutes_url", "video_url", "agenda_packet_url", "ecomment_url"):
            value = meeting[field]
            if value and not value.startswith("https://"):
                raise ValueError(f"Row {index} field {field} has invalid URL: {value}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    result = scrape_calendar(DEFAULT_URL)
    print(json.dumps({"count": len(result), "samples": result[:5]}, indent=2))
