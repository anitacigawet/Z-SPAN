"""Strict current-month adapter for public Legistar calendar tables.

The public calendar is used instead of the OData API because some Legistar
tenants expose scheduled future rows in Calendar.aspx before those rows are
published through OData.  Callers supply the exact governing-body vocabulary
for their jurisdiction; unfamiliar current rows fail loudly instead of being
silently included or silently mistaken for an empty calendar.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
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
MAX_ROWS = 500
REQUEST_TIMEOUT = 45
BLOCKING_HTTP_STATUSES = {401, 403, 407, 423, 429, 451}
BAD_SCHEMES = ("javascript:", "data:", "vbscript:", "file:", "mailto:", "ftp:", "gopher:")
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
TIME_RE = re.compile(
    r"^(1[0-2]|0?[1-9]):([0-5]\d)\s*([AP])\.?M\.?(?=\s|$|[^\w.])",
    re.IGNORECASE,
)
DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
WINDOW_OPEN_RE = re.compile(r"window\.open\(\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
DEFAULT_EXCLUDED_TITLE_TERMS = (
    "board",
    "commission",
    "committee",
    "district",
    "corporation",
    "authority",
)


def scrape_legistar_current(
    url: str,
    *,
    city_label: str,
    allowed_titles: frozenset[str],
    allowed_title_prefixes: tuple[str, ...] = (),
    excluded_title_terms: tuple[str, ...] = DEFAULT_EXCLUDED_TITLE_TERMS,
    allowed_media_hosts: frozenset[str] = frozenset(),
) -> list[dict[str, str]]:
    """Return only witnessed governing-body rows from this month forward."""
    input_host = _validate_input_url(url)
    allowed_exact = frozenset(_fold(value) for value in allowed_titles)
    allowed_prefixes = tuple(_fold(value) for value in allowed_title_prefixes)
    excluded_terms = tuple(_fold(value) for value in excluded_title_terms)
    media_hosts = _validate_extra_hosts(allowed_media_hosts)
    if not allowed_exact and not allowed_prefixes:
        raise ValueError(f"{city_label} Legistar adapter requires governing-body vocabulary")

    status, final_url, html = _fetch_bounded(make_session(), url, input_host=input_host)
    if status in BLOCKING_HTTP_STATUSES:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "%s official Legistar calendar blocked the neutral paced request: "
            "status=%d final_url=%s failure_shape=honest-empty "
            "missing_data_scope=all_current_month_forward_meetings",
            city_label,
            status,
            final_url,
        )
        return []
    if status != 200:
        raise RuntimeError(f"{city_label} Legistar calendar returned HTTP {status}: {final_url}")

    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.rgMasterTable")
    if not isinstance(table, Tag):
        raise RuntimeError(f"{city_label} Legistar fingerprint drift: table.rgMasterTable missing")

    header_row = table.select_one("thead tr") or table.find("tr")
    headers = (
        [_clean(cell.get_text(" ", strip=True)) for cell in header_row.find_all(["th", "td"], recursive=False)]
        if isinstance(header_row, Tag)
        else []
    )
    indices = _header_indices(headers, city_label=city_label)
    logger.info(
        "%s vendor_fingerprint witness=legistar_rgMasterTable semantic_headers=%s",
        city_label,
        headers,
    )

    rows = table.select("tbody > tr")
    if not rows:
        rows = [row for row in table.find_all("tr") if row is not header_row]
    no_record_rows = [
        row
        for row in rows
        if "no records to display" in _fold(row.get_text(" ", strip=True))
        or "rgNoRecords" in (row.get("class") or [])
    ]
    data_rows = [
        row
        for row in rows
        if row not in no_record_rows and row.find_all("td", recursive=False)
    ]
    if len(data_rows) > MAX_ROWS:
        raise RuntimeError(
            f"{city_label} Legistar table exceeded the {MAX_ROWS}-row safety cap: "
            f"rows={len(data_rows)}"
        )
    if not data_rows:
        page_text = _clean(table.get_text(" ", strip=True)).casefold()
        if "no records to display" not in page_text and table.select_one(".rgNoRecords") is None:
            raise RuntimeError(
                f"{city_label} Legistar table had no rows and no witnessed empty-state marker"
            )
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning("%s official Legistar table explicitly reports no records", city_label)
        return []

    cutoff = date.today().replace(day=1)
    counters: Counter[str] = Counter()
    meetings: list[dict[str, str]] = []
    seen: set[str] = set()

    for position, row in enumerate(data_rows, start=1):
        counters["rows_seen"] += 1
        cells = row.find_all("td", recursive=False)
        if len(cells) != len(headers):
            raise RuntimeError(
                f"{city_label} Legistar row width drift: position={position} "
                f"cells={len(cells)} headers={len(headers)}"
            )

        title = _clean(cells[indices["title"]].get_text(" ", strip=True))
        meeting_day = _parse_date(
            _clean(cells[indices["date"]].get_text(" ", strip=True)),
            city_label=city_label,
            position=position,
        )
        if meeting_day < cutoff:
            counters["before_current_month"] += 1
            continue

        folded_title = _fold(title)
        accepted = folded_title in allowed_exact or any(
            folded_title.startswith(prefix) for prefix in allowed_prefixes
        )
        if not accepted:
            if any(_term_present(folded_title, term) for term in excluded_terms):
                counters["known_subordinate_body"] += 1
                logger.info(
                    "%s row dropped: reason=known_subordinate_body position=%d title=%r",
                    city_label,
                    position,
                    title,
                )
                continue
            raise RuntimeError(
                f"{city_label} current Legistar body vocabulary drift: "
                f"position={position} title={title!r}"
            )

        time_raw = _clean(cells[indices["time"]].get_text(" ", strip=True))
        location_raw = _clean(cells[indices["location"]].get_text(" ", strip=True))
        cancellation_raw = " ".join((title, time_raw, location_raw))
        cancelled = bool(CANCELLED_RE.search(cancellation_raw))
        if cancelled and not CANCELLED_RE.search(title):
            title = f"{title} — Cancelled"
            logger.warning(
                "%s cancellation surfaced outside title; normalized title for canonical evidence: "
                "position=%d raw_time=%r raw_location=%r",
                city_label,
                position,
                time_raw,
                location_raw,
            )

        meeting_time = _parse_time(
            time_raw,
            city_label=city_label,
            position=position,
            cancelled=cancelled,
        )
        meeting_location = _optional_text(
            location_raw,
            city_label=city_label,
            position=position,
            field="meeting_location",
        )
        agenda_url = _cell_url(
            _cell(cells, indices.get("agenda")),
            final_url,
            city_label=city_label,
            position=position,
            field="agenda_url",
            input_host=input_host,
        )
        minutes_url = _cell_url(
            _cell(cells, indices.get("minutes")),
            final_url,
            city_label=city_label,
            position=position,
            field="minutes_url",
            input_host=input_host,
        )
        video_url = _cell_url(
            _cell(cells, indices.get("video")),
            final_url,
            city_label=city_label,
            position=position,
            field="video_url",
            input_host=input_host,
            extra_allowed_hosts=media_hosts,
        )
        packet_url = _cell_url(
            _cell(cells, indices.get("packet")),
            final_url,
            city_label=city_label,
            position=position,
            field="agenda_packet_url",
            input_host=input_host,
        )
        ecomment_url = _cell_url(
            _cell(cells, indices.get("ecomment")),
            final_url,
            city_label=city_label,
            position=position,
            field="ecomment_url",
            input_host=input_host,
        )
        meeting_id = _meeting_id(row, city_label=city_label, position=position)
        dedupe_key = meeting_id or f"{meeting_day.isoformat()}|{meeting_time}|{_fold(title)}"
        if dedupe_key in seen:
            counters["duplicate"] += 1
            logger.warning(
                "%s row dropped: reason=duplicate key=%r position=%d title=%r",
                city_label,
                dedupe_key,
                position,
                title,
            )
            continue
        seen.add(dedupe_key)

        meeting = {
            "meeting_title": title,
            "meeting_date": meeting_day.isoformat(),
            "meeting_time": meeting_time,
            "meeting_location": meeting_location,
            "meeting_status": _status(title, agenda_url, minutes_url, packet_url),
            "agenda_url": agenda_url,
            "minutes_url": minutes_url,
            "video_url": video_url,
            "agenda_packet_url": packet_url,
            "ecomment_url": ecomment_url,
            "meeting_id": meeting_id,
        }
        _validate_meeting(meeting, city_label=city_label, position=position)
        meetings.append(meeting)
        counters["rows_accepted"] += 1
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
            "%s official current-month Legistar table contained no qualifying governing-body rows: stats=%s",
            city_label,
            dict(counters),
        )
    logger.warning(
        "%s scrape summary: rows_seen=%d rows_accepted=%d drop_reasons=%s",
        city_label,
        counters["rows_seen"],
        counters["rows_accepted"],
        {
            key: value
            for key, value in counters.items()
            if key not in {"rows_seen", "rows_accepted"}
        },
    )
    return meetings


def _validate_input_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host.endswith(".legistar.com"):
        raise ValueError(f"Legistar adapter requires an HTTPS *.legistar.com URL: {url!r}")
    if parsed.path.casefold().rstrip("/") != "/calendar.aspx":
        raise ValueError(f"Legistar adapter requires Calendar.aspx: {url!r}")
    return host


def _fetch_bounded(session: Any, url: str, *, input_host: str) -> tuple[int, str, str]:
    with session.get(url, timeout=REQUEST_TIMEOUT, stream=True, allow_redirects=True) as response:
        final_host = (urlparse(response.url).hostname or "").lower()
        if final_host != input_host:
            raise ValueError(f"Legistar redirect reached disallowed host: {final_host}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Legistar response exceeded {MAX_RESPONSE_BYTES} bytes: {url}")
        return (
            response.status_code,
            response.url,
            bytes(body).decode(response.encoding or "utf-8", errors="replace"),
        )


def _header_indices(headers: list[str], *, city_label: str) -> dict[str, int]:
    normalized = [_fold(header) for header in headers]

    def exact(*names: str, required: bool = False) -> int | None:
        candidates = [index for index, header in enumerate(normalized) if header in names]
        if len(candidates) > 1:
            raise RuntimeError(
                f"{city_label} Legistar header is ambiguous: names={names!r} headers={headers!r}"
            )
        if not candidates:
            if required:
                raise RuntimeError(
                    f"{city_label} Legistar required header missing: names={names!r} headers={headers!r}"
                )
            return None
        return candidates[0]

    return {
        "title": exact("name", "meeting name", required=True),
        "date": exact("date", "meeting date", required=True),
        "time": exact("time", "meeting time", required=True),
        "location": exact("location", "meeting location", required=True),
        "details": exact("details", "meeting details", required=True),
        "agenda": exact("agenda"),
        "minutes": exact("minutes"),
        "video": exact("video"),
        "packet": exact("agenda packet", "packet"),
        "ecomment": exact("ecomment", "public comment"),
    }


def _parse_date(raw: str, *, city_label: str, position: int) -> date:
    match = DATE_RE.fullmatch(raw)
    if not match:
        raise RuntimeError(
            f"{city_label} Legistar row has unparseable date: position={position} value={raw!r}"
        )
    try:
        return date(int(match.group(3)), int(match.group(1)), int(match.group(2)))
    except ValueError as exc:
        raise RuntimeError(
            f"{city_label} Legistar row has invalid date: position={position} value={raw!r}"
        ) from exc


def _parse_time(raw: str, *, city_label: str, position: int, cancelled: bool) -> str:
    folded = _fold(raw)
    if not raw or folded in {"not available", "n/a"} or CANCELLED_RE.fullmatch(raw):
        logger.warning(
            "%s meeting_time honest-empty: position=%d reason=%s raw=%r",
            city_label,
            position,
            "cancelled_meeting" if cancelled else "source_cell_empty",
            raw,
        )
        return ""
    match = TIME_RE.fullmatch(raw)
    if not match:
        raise RuntimeError(
            f"{city_label} Legistar row has unparseable time: position={position} value={raw!r}"
        )
    return f"{int(match.group(1))}:{match.group(2)} {match.group(3).upper()}M"


def _optional_text(raw: str, *, city_label: str, position: int, field: str) -> str:
    if not raw or _fold(raw) in {"not available", "n/a"} or CANCELLED_RE.fullmatch(raw):
        logger.warning(
            "%s %s honest-empty: position=%d reason=source_cell_empty raw=%r",
            city_label,
            field,
            position,
            raw,
        )
        return ""
    return raw


def _cell(cells: list[Tag], index: int | None) -> Tag | None:
    return cells[index] if index is not None and index < len(cells) else None


def _cell_url(
    cell: Tag | None,
    base_url: str,
    *,
    city_label: str,
    position: int,
    field: str,
    input_host: str,
    extra_allowed_hosts: frozenset[str] = frozenset(),
) -> str:
    if not isinstance(cell, Tag):
        logger.info(
            "%s %s honest-empty: position=%d reason=column_not_present",
            city_label,
            field,
            position,
        )
        return ""

    label = _clean(cell.get_text(" ", strip=True))
    candidates: list[str] = []
    rejected_placeholders: list[str] = []
    for anchor in cell.find_all("a"):
        href = _clean(str(anchor.get("href", "") or ""))
        if href and not href.casefold().startswith(BAD_SCHEMES) and href != "#":
            candidates.append(href)
            continue
        onclick = str(anchor.get("onclick", "") or "")
        match = WINDOW_OPEN_RE.search(onclick)
        if match:
            candidates.append(match.group(1))
        elif href:
            rejected_placeholders.append(href)

    safe = []
    for candidate in candidates:
        emitted = _safe_url(
            candidate,
            base_url,
            city_label=city_label,
            position=position,
            field=field,
            input_host=input_host,
            extra_allowed_hosts=extra_allowed_hosts,
        )
        if emitted and emitted not in safe:
            safe.append(emitted)
    if len(safe) > 1:
        raise RuntimeError(
            f"{city_label} Legistar cell exposed conflicting {field} links: "
            f"position={position} links={safe!r}"
        )
    if safe:
        return safe[0]
    if rejected_placeholders:
        logger.warning(
            "%s URL dropped: position=%d field=%s reason=placeholder_without_fallback values=%r",
            city_label,
            position,
            field,
            rejected_placeholders,
        )
    elif label and _fold(label) not in {"not available", "n/a"}:
        logger.warning(
            "%s URL dropped: position=%d field=%s reason=label_without_href label=%r",
            city_label,
            position,
            field,
            label,
        )
    return ""


def _safe_url(
    raw: str,
    base_url: str,
    *,
    city_label: str,
    position: int,
    field: str,
    input_host: str,
    extra_allowed_hosts: frozenset[str] = frozenset(),
) -> str:
    value = raw.strip()
    if value.casefold().startswith(BAD_SCHEMES) or value.startswith("//"):
        logger.warning(
            "%s URL dropped: position=%d field=%s reason=disallowed_input value=%r",
            city_label,
            position,
            field,
            raw,
        )
        return ""
    absolute = urljoin(base_url, value)
    parsed = urlparse(absolute)
    host = (parsed.hostname or "").lower()
    jurisdiction = input_host.removesuffix(".legistar.com")
    allowed_hosts = {
        input_host,
        "legistar1.granicus.com",
        f"{jurisdiction}.legistar1.com",
    }
    if field == "video_url":
        allowed_hosts.update(extra_allowed_hosts)
    allowed = host in allowed_hosts
    if parsed.scheme != "https" or not allowed:
        logger.warning(
            "%s URL dropped: position=%d field=%s reason=scheme_or_host "
            "value=%r absolute=%r host=%r",
            city_label,
            position,
            field,
            raw,
            absolute,
            host,
        )
        return ""
    return absolute


def _validate_extra_hosts(hosts: frozenset[str]) -> frozenset[str]:
    normalized: set[str] = set()
    for raw in hosts:
        host = str(raw or "").strip().lower().rstrip(".")
        if not host or "://" in host or "/" in host or ":" in host:
            raise ValueError(f"Legistar media allowlist contains an invalid host: {raw!r}")
        normalized.add(host)
    return frozenset(normalized)


def _meeting_id(row: Tag, *, city_label: str, position: int) -> str:
    ids: set[str] = set()
    for anchor in row.find_all("a"):
        anchor_id = str(anchor.get("id", "") or "").casefold()
        label = _fold(anchor.get_text(" ", strip=True))
        if "hypmeetingdetail" not in anchor_id and label != "meeting details":
            continue
        for raw in (str(anchor.get("href", "") or ""), str(anchor.get("onclick", "") or "")):
            parsed = urlparse(raw.replace("&amp;", "&"))
            query = parse_qs(parsed.query)
            for key in ("LEGID", "ID", "MeetingID", "legid", "id", "meetingid"):
                for value in query.get(key, []) + query.get(key.casefold(), []):
                    if str(value).isdigit():
                        ids.add(str(value))
    if not ids:
        # Some Legistar tenants render Meeting Details as an anchor without an
        # href while retaining the same event ID in the adjacent iCalendar link.
        for anchor in row.find_all("a"):
            anchor_id = str(anchor.get("id", "") or "").casefold()
            if "hypical" not in anchor_id:
                continue
            parsed = urlparse(str(anchor.get("href", "") or "").replace("&amp;", "&"))
            for value in parse_qs(parsed.query).get("ID", []):
                if str(value).isdigit():
                    ids.add(str(value))
    if len(ids) > 1:
        raise RuntimeError(
            f"{city_label} Legistar row exposed conflicting meeting IDs: "
            f"position={position} ids={sorted(ids)!r}"
        )
    if not ids:
        logger.warning("%s meeting_id honest-empty: position=%d reason=no_row_id_signal", city_label, position)
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


def _validate_meeting(meeting: dict[str, str], *, city_label: str, position: int) -> None:
    if tuple(meeting) != CANONICAL_FIELDS:
        raise RuntimeError(
            f"{city_label} row schema mismatch: position={position} keys={tuple(meeting)!r}"
        )
    for field, value in meeting.items():
        if not isinstance(value, str):
            raise TypeError(
                f"{city_label} row contains non-string field: position={position} "
                f"field={field} type={type(value).__name__}"
            )
    for field in ("agenda_url", "minutes_url", "video_url", "agenda_packet_url", "ecomment_url"):
        if meeting[field] and not meeting[field].startswith("https://"):
            raise RuntimeError(
                f"{city_label} row contains invalid URL: position={position} "
                f"field={field} value={meeting[field]!r}"
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


__all__ = ["CANONICAL_FIELDS", "scrape_legistar_current"]
