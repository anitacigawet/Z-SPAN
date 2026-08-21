"""Strict current-month adapter for Granicus ViewPublisher listing tables."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import html as html_lib
import logging
import re
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from polite_http import make_session


logger = logging.getLogger(__name__)

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

MAX_RESPONSE_BYTES = 10_000_000
MAX_ROWS = 1_000
REQUEST_TIMEOUT = 45
BLOCKING_HTTP_STATUSES = {401, 403, 407, 423, 429, 451}
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
DATE_RE = re.compile(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})")
TIME_RE = re.compile(
    r"(?<!\d)(1[0-2]|0?[1-9]):([0-5]\d)\s*([AP])\.?M\.?(?=\s|$|[^\w.])",
    re.IGNORECASE,
)
WINDOW_OPEN_RE = re.compile(r"window\.open\(\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
BAD_SCHEMES = ("javascript:", "data:", "vbscript:", "file:", "mailto:", "ftp:", "gopher:")
DEFAULT_EXCLUDED_TITLE_TERMS = (
    "board",
    "commission",
    "committee",
    "district",
    "corporation",
    "authority",
)


def scrape_granicus_current(
    url: str,
    *,
    city_label: str,
    allowed_title_prefixes: tuple[str, ...],
    excluded_titles: frozenset[str] = frozenset(),
    excluded_title_terms: tuple[str, ...] = DEFAULT_EXCLUDED_TITLE_TERMS,
) -> list[dict[str, str]]:
    """Return only current-month-forward rows matching reviewed body vocabulary."""
    input_host = _validate_input_url(url)
    allowed_prefixes = tuple(_fold(value) for value in allowed_title_prefixes)
    excluded_exact = frozenset(_fold(value) for value in excluded_titles)
    excluded_terms = tuple(_fold(value) for value in excluded_title_terms)
    if not allowed_prefixes:
        raise ValueError(f"{city_label} Granicus adapter requires allowed title prefixes")

    status, final_url, headers, html = _fetch_bounded(make_session(), url, input_host=input_host)
    if status in BLOCKING_HTTP_STATUSES:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "%s official Granicus publisher blocked the neutral paced request: "
            "status=%d final_url=%s failure_shape=honest-empty "
            "missing_data_scope=all_current_month_forward_meetings",
            city_label,
            status,
            final_url,
        )
        return []
    if status != 200:
        raise RuntimeError(f"{city_label} Granicus publisher returned HTTP {status}: {final_url}")

    soup = BeautifulSoup(html, "html.parser")
    tables = soup.select("table.listingTable")
    has_server_header = any(key.casefold() == "x-granicus-server" for key in headers)
    if not tables or ("ViewPublisher" not in html and not has_server_header):
        raise RuntimeError(
            f"{city_label} Granicus fingerprint drift: "
            f"listing_tables={len(tables)} x_granicus={has_server_header}"
        )
    logger.info(
        "%s vendor_fingerprint witness=granicus_ViewPublisher_listingTable "
        "table_count=%d x_granicus=%s",
        city_label,
        len(tables),
        has_server_header,
    )
    logger.warning(
        "%s field_absence field=meeting_location "
        "reason=granicus_ViewPublisher_has_no_per_row_location_column",
        city_label,
    )
    logger.warning(
        "%s field_absence field=ecomment_url "
        "reason=granicus_ViewPublisher_has_no_ecomment_column",
        city_label,
    )

    cutoff = date.today().replace(day=1)
    counters: Counter[str] = Counter()
    meetings: list[dict[str, str]] = []
    seen: set[str] = set()
    total_rows = sum(
        len(table.select("tr.listingRow"))
        or len(
            [
                row
                for row in table.find_all("tr")
                if row is not table.find("tr") and row.find_all("td", recursive=False)
            ]
        )
        for table in tables
    )
    if total_rows > MAX_ROWS:
        raise RuntimeError(
            f"{city_label} Granicus listing exceeded the {MAX_ROWS}-row safety cap: "
            f"rows={total_rows}"
        )

    for table_number, table in enumerate(tables, start=1):
        header_row = table.find("tr")
        headers_text = (
            [_clean(cell.get_text(" ", strip=True)) for cell in header_row.find_all(["th", "td"], recursive=False)]
            if isinstance(header_row, Tag)
            else []
        )
        indices = _header_indices(headers_text, city_label=city_label, table_number=table_number)
        rows = table.select("tr.listingRow")
        if not rows:
            rows = [row for row in table.find_all("tr") if row is not header_row and row.find_all("td", recursive=False)]
        for row_number, row in enumerate(rows, start=1):
            counters["rows_seen"] += 1
            cells = row.find_all("td", recursive=False)
            row_text = _clean(row.get_text(" ", strip=True))
            if len(cells) == 1 and (
                "no archived videos" in _fold(row_text)
                or "no upcoming events" in _fold(row_text)
                or "no meetings" in _fold(row_text)
            ):
                counters["witnessed_empty_section"] += 1
                logger.info(
                    "%s row dropped: reason=witnessed_empty_section table=%d row=%d text=%r",
                    city_label,
                    table_number,
                    row_number,
                    row_text,
                )
                continue
            if len(cells) != len(headers_text):
                raise RuntimeError(
                    f"{city_label} Granicus row width drift: table={table_number} "
                    f"row={row_number} cells={len(cells)} headers={len(headers_text)}"
                )
            title = _clean(cells[indices["title"]].get_text(" ", strip=True))
            date_text = _clean(cells[indices["date"]].get_text(" ", strip=True))
            meeting_day = _parse_date(
                date_text,
                city_label=city_label,
                table_number=table_number,
                row_number=row_number,
            )
            if meeting_day < cutoff:
                counters["before_current_month"] += 1
                continue

            folded_title = _fold(title)
            accepted = any(folded_title.startswith(prefix) for prefix in allowed_prefixes)
            if not accepted:
                if folded_title in excluded_exact:
                    counters["reviewed_nonmeeting_title"] += 1
                    logger.info(
                        "%s row dropped: reason=reviewed_nonmeeting_title title=%r date=%s",
                        city_label,
                        title,
                        meeting_day.isoformat(),
                    )
                    continue
                if any(_term_present(folded_title, term) for term in excluded_terms):
                    counters["known_subordinate_body"] += 1
                    logger.info(
                        "%s row dropped: reason=known_subordinate_body title=%r date=%s",
                        city_label,
                        title,
                        meeting_day.isoformat(),
                    )
                    continue
                raise RuntimeError(
                    f"{city_label} current Granicus title vocabulary drift: "
                    f"table={table_number} row={row_number} title={title!r}"
                )

            meeting_time = _parse_time(
                date_text,
                city_label=city_label,
                title=title,
            )
            agenda_url = _field_url(
                _cell(cells, indices.get("agenda")),
                final_url,
                city_label=city_label,
                title=title,
                field="agenda_url",
                allowed_labels=("agenda",),
                input_host=input_host,
            )
            minutes_url = _field_url(
                _cell(cells, indices.get("minutes")),
                final_url,
                city_label=city_label,
                title=title,
                field="minutes_url",
                allowed_labels=("minute",),
                input_host=input_host,
            )
            video_url = _field_url(
                _cell(cells, indices.get("video")),
                final_url,
                city_label=city_label,
                title=title,
                field="video_url",
                allowed_labels=("video", "event"),
                input_host=input_host,
            )
            packet_url = _field_url(
                _cell(cells, indices.get("packet")),
                final_url,
                city_label=city_label,
                title=title,
                field="agenda_packet_url",
                allowed_labels=("packet",),
                input_host=input_host,
            )
            meeting_id = _extract_meeting_id(
                row,
                city_label=city_label,
                title=title,
            )
            dedupe_key = meeting_id or f"{meeting_day.isoformat()}|{meeting_time}|{folded_title}"
            if dedupe_key in seen:
                counters["duplicate"] += 1
                logger.warning(
                    "%s row dropped: reason=duplicate key=%r title=%r",
                    city_label,
                    dedupe_key,
                    title,
                )
                continue
            seen.add(dedupe_key)

            meeting = {
                "meeting_title": title,
                "meeting_date": meeting_day.isoformat(),
                "meeting_time": meeting_time,
                "meeting_location": "",
                "meeting_status": _status(title, agenda_url, minutes_url, packet_url),
                "agenda_url": agenda_url,
                "minutes_url": minutes_url,
                "video_url": video_url,
                "agenda_packet_url": packet_url,
                "ecomment_url": "",
                "meeting_id": meeting_id,
            }
            _validate_meeting(meeting, city_label=city_label, title=title)
            meetings.append(meeting)
            counters["rows_accepted"] += 1
            counters["meeting_location_absent_by_construction"] += 1
            counters["ecomment_absent_by_construction"] += 1
            logger.info(
                "%s meeting emitted: id=%s date=%s title=%r",
                city_label,
                meeting_id,
                meeting_day.isoformat(),
                title,
            )

    meetings.sort(key=lambda item: (item["meeting_date"], item["meeting_time"], item["meeting_id"]))
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "%s official Granicus publisher contained no qualifying current-month-forward rows: stats=%s",
            city_label,
            dict(counters),
        )
    logger.warning(
        "%s scrape summary: rows_seen=%d rows_accepted=%d counters=%s",
        city_label,
        counters["rows_seen"],
        counters["rows_accepted"],
        dict(counters),
    )
    return meetings


def _validate_input_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    query = parse_qs(parsed.query)
    if parsed.scheme != "https" or not host.endswith(".granicus.com"):
        raise ValueError(f"Granicus adapter requires HTTPS on *.granicus.com: {url!r}")
    if parsed.path != "/ViewPublisher.php" or not query.get("view_id"):
        raise ValueError(f"Granicus adapter requires a ViewPublisher view_id URL: {url!r}")
    return host


def _fetch_bounded(
    session: Any,
    url: str,
    *,
    input_host: str,
) -> tuple[int, str, dict[str, str], str]:
    with session.get(url, timeout=REQUEST_TIMEOUT, stream=True, allow_redirects=True) as response:
        final_host = (urlparse(response.url).hostname or "").lower()
        if final_host != input_host:
            raise ValueError(f"Granicus redirect reached disallowed host: {final_host}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Granicus response exceeded {MAX_RESPONSE_BYTES} bytes: {url}")
        return (
            response.status_code,
            response.url,
            dict(response.headers),
            bytes(body).decode(response.encoding or "utf-8", errors="replace"),
        )


def _header_indices(headers: list[str], *, city_label: str, table_number: int) -> dict[str, int | None]:
    normalized = [_fold(header) for header in headers]

    def exact(*names: str, required: bool = False) -> int | None:
        matches = [index for index, value in enumerate(normalized) if value in names]
        if len(matches) > 1:
            raise RuntimeError(
                f"{city_label} Granicus header ambiguity: table={table_number} "
                f"names={names!r} headers={headers!r}"
            )
        if not matches:
            if required:
                raise RuntimeError(
                    f"{city_label} Granicus required header missing: table={table_number} "
                    f"names={names!r} headers={headers!r}"
                )
            return None
        return matches[0]

    return {
        "title": exact("name", "meeting name", required=True),
        "date": exact("date", required=True),
        "agenda": exact("agenda"),
        "minutes": exact("minutes", "minutes/meeting notes"),
        "video": exact("video", "live video", "event", "download video file"),
        "packet": exact("agenda packet"),
    }


def _parse_date(raw: str, *, city_label: str, table_number: int, row_number: int) -> date:
    match = DATE_RE.search(raw)
    if not match:
        raise RuntimeError(
            f"{city_label} Granicus row has unparseable date: table={table_number} "
            f"row={row_number} value={raw!r}"
        )
    value = " ".join(match.groups())
    for pattern in ("%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    raise RuntimeError(
        f"{city_label} Granicus row has invalid date: table={table_number} "
        f"row={row_number} value={raw!r}"
    )


def _parse_time(raw: str, *, city_label: str, title: str) -> str:
    match = TIME_RE.search(raw[:1000])
    if not match:
        logger.warning(
            "%s meeting_time honest-empty: title=%r reason=no_per_row_time_signal raw=%r",
            city_label,
            title,
            raw[:500],
        )
        return ""
    return f"{int(match.group(1))}:{match.group(2)} {match.group(3).upper()}M"


def _cell(cells: list[Tag], index: int | None) -> Tag | None:
    return cells[index] if index is not None and index < len(cells) else None


def _field_url(
    cell: Tag | None,
    base_url: str,
    *,
    city_label: str,
    title: str,
    field: str,
    allowed_labels: tuple[str, ...],
    input_host: str,
) -> str:
    if not isinstance(cell, Tag):
        logger.info("%s %s honest-empty: title=%r reason=column_not_present", city_label, field, title)
        return ""
    candidates: list[str] = []
    rejected: list[str] = []
    for anchor in cell.find_all("a"):
        label = _fold(anchor.get_text(" ", strip=True))
        if label and not any(term in label for term in allowed_labels):
            continue
        href = str(anchor.get("href", "") or "").strip()
        if href and not href.casefold().startswith(BAD_SCHEMES) and href != "#":
            candidates.append(href)
            continue
        onclick = str(anchor.get("onclick", "") or "")
        match = WINDOW_OPEN_RE.search(onclick)
        if match:
            candidates.append(match.group(1))
        elif href:
            rejected.append(href)
    safe = []
    for candidate in candidates:
        value = _safe_url(
            candidate,
            base_url,
            city_label=city_label,
            title=title,
            field=field,
            input_host=input_host,
        )
        if value and value not in safe:
            safe.append(value)
    if len(safe) > 1:
        raise RuntimeError(
            f"{city_label} Granicus row exposed conflicting {field} links: "
            f"title={title!r} links={safe!r}"
        )
    if safe:
        return safe[0]
    if rejected:
        logger.warning(
            "%s URL dropped: title=%r field=%s reason=placeholder_without_fallback values=%r",
            city_label,
            title,
            field,
            rejected,
        )
    return ""


def _safe_url(
    raw: str,
    base_url: str,
    *,
    city_label: str,
    title: str,
    field: str,
    input_host: str,
) -> str:
    value = raw.strip()
    if value.casefold().startswith(BAD_SCHEMES):
        logger.warning(
            "%s URL dropped: title=%r field=%s reason=disallowed_input value=%r",
            city_label,
            title,
            field,
            raw,
        )
        return ""
    if value.startswith("//"):
        value = "https:" + value
    absolute = urljoin(base_url, value)
    parsed = urlparse(absolute)
    host = (parsed.hostname or "").lower()
    allowed_hosts = {
        input_host,
        "d3n9y02raazwpg.cloudfront.net",
        "archive-video.granicus.com",
        "archive-media.granicus.com",
    }
    if parsed.scheme != "https" or host not in allowed_hosts:
        logger.warning(
            "%s URL dropped: title=%r field=%s reason=scheme_or_host "
            "value=%r absolute=%r host=%r",
            city_label,
            title,
            field,
            raw,
            absolute,
            host,
        )
        return ""
    return absolute


def _extract_meeting_id(row: Tag, *, city_label: str, title: str) -> str:
    ids: set[str] = set()
    for anchor in row.find_all("a"):
        for raw in (str(anchor.get("href", "") or ""), str(anchor.get("onclick", "") or "")):
            normalized = raw.replace("&amp;", "&")
            for key in ("event_id", "clip_id"):
                for value in parse_qs(urlparse(normalized).query).get(key, []):
                    if str(value).isdigit():
                        ids.add(str(value))
            for match in re.finditer(r"(?:[?&]|&amp;)(?:event_id|clip_id)=(\d+)", normalized):
                ids.add(match.group(1))
    if len(ids) > 1:
        raise RuntimeError(
            f"{city_label} Granicus row exposed conflicting meeting IDs: title={title!r} ids={sorted(ids)!r}"
        )
    if not ids:
        logger.warning("%s meeting_id honest-empty: title=%r reason=no_same_row_id_signal", city_label, title)
        return ""
    return next(iter(ids))


def _status(title: str, agenda_url: str, minutes_url: str, packet_url: str) -> str:
    if CANCELLED_RE.search(title):
        return "Cancelled"
    if minutes_url:
        return "Minutes Available"
    if agenda_url or packet_url:
        return "Agenda Available"
    return "Scheduled"


def _validate_meeting(meeting: dict[str, str], *, city_label: str, title: str) -> None:
    if tuple(meeting) != CANONICAL_FIELDS:
        raise RuntimeError(f"{city_label} schema mismatch: title={title!r} keys={tuple(meeting)!r}")
    if any(not isinstance(value, str) for value in meeting.values()):
        raise TypeError(f"{city_label} row contains non-string values: title={title!r}")
    for field in ("agenda_url", "minutes_url", "video_url", "agenda_packet_url", "ecomment_url"):
        if meeting[field] and not meeting[field].startswith("https://"):
            raise RuntimeError(
                f"{city_label} row contains invalid URL: title={title!r} field={field} value={meeting[field]!r}"
            )


def _term_present(folded_title: str, folded_term: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(folded_term)}(?!\w)", folded_title))


def _fold(value: str) -> str:
    return _clean(value).casefold()


def _clean(value: str) -> str:
    raw = str(value or "")
    text = (
        BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
        if "<" in raw and ">" in raw
        else html_lib.unescape(raw)
    )
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


__all__ = ["CANONICAL_FIELDS", "scrape_granicus_current"]
