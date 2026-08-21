"""Bounded current/future adapter for Destiny AgendaQuick calendars.

Official tenant URLs come from the pinned Civic Source Catalog. This module is
the public vendor grammar: current month plus six future months, politely paced
requests, one narrowly bounded same-month fingerprint retry, canonical fields,
and fail-closed URL handling.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import logging
import re
from typing import Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, Tag

from polite_http import make_session


MAX_MONTHS = 7
MAX_RESPONSE_BYTES = 2_000_000
DESTINY_HOSTS = {"destinyhosted.com", "public.destinyhosted.com"}
PRODUCT_FINGERPRINT_ERROR = "AgendaQuick product fingerprint disappeared"
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
MONTH_DATE_RE = re.compile(r"\b([A-Z][a-z]+ \d{1,2}, \d{4})\b")
BOARD_ID_RE = re.compile(r"(?:[?&])id=(\d+)(?:&|$)", re.IGNORECASE)
COUNCIL_BODY_RE = re.compile(r"\bcouncil\b", re.IGNORECASE)
NON_MEETING_COUNCIL_RE = re.compile(
    r"\b(?:notice of quorum|public notices?|upcoming agenda items?)\b",
    re.IGNORECASE,
)

TitleAllowPredicate = Callable[[str], bool]

logger = logging.getLogger(__name__)


def scrape_destiny_current(
    calendar_url: str,
    *,
    media_hosts: set[str] | None = None,
    today: date | None = None,
    title_allow_predicate: TitleAllowPredicate | None = None,
) -> list[dict]:
    """Scrape council meetings from this month plus six future months.

    AgendaQuick's ``mt=ALL`` response can mix a city's council with boards,
    commissions, notices, and other bodies.  The default predicate accepts
    only titles with an explicit council token and rejects the observed
    notice-only vocabulary.  A city wrapper may pass a stricter predicate
    when its official title vocabulary differs.
    """
    board_id = _board_id(calendar_url)
    source_host = (urlparse(calendar_url).hostname or "").lower()
    if source_host not in DESTINY_HOSTS:
        raise ValueError(f"AgendaQuick source host is not allowed: {source_host!r}")
    approved_media = {host.lower() for host in (media_hosts or set())}
    approved_hosts = DESTINY_HOSTS | approved_media
    current_day = today or date.today()
    month_floor = current_day.replace(day=1)
    month_pairs = _month_window(current_day)
    stats: Counter[str] = Counter()
    meetings_by_key: dict[tuple[str, str, str], dict] = {}

    logger.warning(
        "AgendaQuick calendar rows do not expose meeting_time or "
        "meeting_location; both fields will remain honestly empty"
    )

    with make_session() as session:
        completed_months = 0
        for year, month in month_pairs:
            month_url = _month_url(calendar_url, board_id, year, month)
            try:
                page_meetings = _fetch_and_parse_month(
                    session,
                    month_url,
                    year,
                    month,
                    approved_hosts,
                    stats,
                    month_floor=month_floor,
                    title_allow_predicate=title_allow_predicate,
                )
            except Exception as exc:
                logger.error(
                    "AgendaQuick bounded scrape aborted without returning partial "
                    "output: requested=%s-%02d completed_months=%d "
                    "buffered_meetings=%d error_type=%s error=%s",
                    year,
                    month,
                    completed_months,
                    len(meetings_by_key),
                    type(exc).__name__,
                    exc,
                )
                raise
            for meeting in page_meetings:
                key = (
                    meeting["meeting_date"],
                    meeting["meeting_title"],
                    meeting["meeting_id"],
                )
                if key in meetings_by_key:
                    stats["duplicates_dropped"] += 1
                    logger.warning("AgendaQuick duplicate row dropped key=%r", key)
                    continue
                meetings_by_key[key] = meeting
            completed_months += 1

    if stats["rows_seen"] and not stats["council_rows_allowed"]:
        raise ValueError(
            "AgendaQuick mt=ALL exposed rows but none carried a trustworthy "
            "council/governing-body title signal"
        )

    meetings = sorted(
        meetings_by_key.values(),
        key=lambda row: (row["meeting_date"], row["meeting_title"], row["meeting_id"]),
    )
    logger.info(
        "AgendaQuick bounded scrape board_id=%s months=%d emitted=%d stats=%s",
        board_id,
        len(month_pairs),
        len(meetings),
        dict(stats),
    )
    return meetings


def _board_id(calendar_url: str) -> str:
    match = BOARD_ID_RE.search(calendar_url)
    if not match:
        raise ValueError(f"AgendaQuick URL is missing numeric tenant id: {calendar_url!r}")
    return match.group(1)


def _month_window(today: date) -> list[tuple[int, int]]:
    start = today.year * 12 + today.month - 1
    result: list[tuple[int, int]] = []
    for index in range(start, start + MAX_MONTHS):
        year, zero_based_month = divmod(index, 12)
        result.append((year, zero_based_month + 1))
    return result


def _month_url(calendar_url: str, board_id: str, year: int, month: int) -> str:
    parsed = urlparse(calendar_url)
    return urlunparse(
        parsed._replace(
            query=urlencode(
                {
                    "id": board_id,
                    "mt": "ALL",
                    "get_month": str(month),
                    "get_year": str(year),
                }
            ),
            fragment="",
        )
    )


def _fetch_html(session, url: str) -> tuple[str, str]:
    with session.get(url, timeout=(10, 30), stream=True, allow_redirects=True) as response:
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError(
                "AgendaQuick returned unexpected successful HTTP status: "
                f"status={response.status_code} url={response.url}"
            )
        final_host = (urlparse(response.url).hostname or "").lower()
        if final_host not in DESTINY_HOSTS:
            raise ValueError(f"AgendaQuick redirected to disallowed host: {final_host!r}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"AgendaQuick response exceeded {MAX_RESPONSE_BYTES} bytes: {url}")
        return bytes(body).decode(response.encoding or "utf-8", errors="replace"), response.url


def _fetch_and_parse_month(
    session,
    month_url: str,
    requested_year: int,
    requested_month: int,
    approved_hosts: set[str],
    stats: Counter[str],
    *,
    month_floor: date,
    title_allow_predicate: TitleAllowPredicate | None,
) -> list[dict]:
    """Fetch one month, retrying only a one-off missing product fingerprint."""
    html, final_url = _fetch_html(session, month_url)
    try:
        return _parse_page(
            html,
            final_url,
            requested_year,
            requested_month,
            approved_hosts,
            stats,
            month_floor=month_floor,
            title_allow_predicate=title_allow_predicate,
        )
    except ValueError as exc:
        if str(exc) != PRODUCT_FINGERPRINT_ERROR:
            raise

    logger.warning(
        "AgendaQuick HTTP-200 response rejected; retrying exact month once: "
        "requested=%s-%02d response_url=%s reason=%s",
        requested_year,
        requested_month,
        final_url,
        PRODUCT_FINGERPRINT_ERROR,
    )
    retry_html, retry_final_url = _fetch_html(session, month_url)
    try:
        records = _parse_page(
            retry_html,
            retry_final_url,
            requested_year,
            requested_month,
            approved_hosts,
            stats,
            month_floor=month_floor,
            title_allow_predicate=title_allow_predicate,
        )
    except ValueError as exc:
        if str(exc) == PRODUCT_FINGERPRINT_ERROR:
            logger.error(
                "AgendaQuick product fingerprint remained absent after bounded "
                "retry: requested=%s-%02d first_response_url=%s "
                "retry_response_url=%s",
                requested_year,
                requested_month,
                final_url,
                retry_final_url,
            )
        raise

    logger.warning(
        "AgendaQuick product fingerprint recovered on bounded retry: "
        "requested=%s-%02d first_response_url=%s retry_response_url=%s",
        requested_year,
        requested_month,
        final_url,
        retry_final_url,
    )
    return records


def _parse_page(
    html: str,
    base_url: str,
    requested_year: int,
    requested_month: int,
    approved_hosts: set[str],
    stats: Counter[str],
    *,
    month_floor: date | None = None,
    title_allow_predicate: TitleAllowPredicate | None = None,
) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    if "AgendaQuick" not in html:
        raise ValueError(PRODUCT_FINGERPRINT_ERROR)
    table = soup.select_one("table#meeting-table.listclass, table.listclass")
    if table is None:
        raise ValueError("AgendaQuick meeting table disappeared")
    selected_year = table.find_previous("select", attrs={"name": "get_year"})
    selected_month = table.find_previous("select", attrs={"name": "get_month"})
    if selected_year is None or selected_month is None:
        raise ValueError("AgendaQuick selected month/year controls disappeared")
    observed_year = _selected_value(selected_year)
    observed_month = _selected_value(selected_month)
    if observed_year != str(requested_year) or observed_month != str(requested_month):
        raise ValueError(
            "AgendaQuick ignored requested month: "
            f"requested={requested_year}-{requested_month:02d} "
            f"observed={observed_year}-{observed_month}"
        )

    headers = [_clean_text(cell).lower() for cell in table.select("thead th, thead td")]
    empty_notice = " ".join(headers)
    if "no agendas or minutes were found" in empty_notice:
        stats["empty_months"] += 1
        logger.warning(
            "AgendaQuick month is explicitly empty requested=%s-%02d notice=%r url=%s",
            requested_year,
            requested_month,
            empty_notice,
            base_url,
        )
        return []
    required = {"agendas", "meeting"}
    if not required.issubset(headers):
        raise ValueError(f"AgendaQuick headers changed: observed={headers}")
    rows = table.select("tbody > tr")
    records: list[dict] = []
    effective_floor = month_floor or date(requested_year, requested_month, 1)
    allow_title = title_allow_predicate or _default_council_title_allowed
    for row_number, row in enumerate(rows, start=1):
        stats["rows_seen"] += 1
        cells = row.find_all("td", recursive=False)
        if len(cells) < 2:
            stats["rows_dropped_short"] += 1
            logger.warning(
                "AgendaQuick row dropped: requested=%s-%02d row=%d cells=%d",
                requested_year,
                requested_month,
                row_number,
                len(cells),
            )
            continue

        title, raw_date, meeting_date = _row_identity(cells)
        if not title or not meeting_date:
            raise ValueError(
                "AgendaQuick data row lacks title/date evidence: "
                f"title={title!r} date={raw_date!r}"
            )
        meeting_day = date.fromisoformat(meeting_date)
        if meeting_day < effective_floor:
            stats["rows_dropped_before_month"] += 1
            logger.warning(
                "AgendaQuick row dropped before month floor: row=%d date=%s "
                "floor=%s title=%r",
                row_number,
                meeting_date,
                effective_floor.isoformat(),
                title,
            )
            continue
        if (meeting_day.year, meeting_day.month) != (requested_year, requested_month):
            stats["rows_dropped_wrong_month"] += 1
            logger.warning(
                "AgendaQuick row dropped outside requested month: row=%d "
                "requested=%s-%02d observed=%s title=%r",
                row_number,
                requested_year,
                requested_month,
                meeting_date,
                title,
            )
            continue
        try:
            allowed = allow_title(title)
        except Exception as exc:
            raise ValueError(
                f"AgendaQuick title allow predicate failed for {title!r}: {exc}"
            ) from exc
        if not isinstance(allowed, bool):
            raise TypeError("AgendaQuick title allow predicate must return bool")
        if not allowed:
            stats["rows_dropped_non_council"] += 1
            logger.warning(
                "AgendaQuick mt=ALL row dropped without council meeting evidence: "
                "row=%d date=%s title=%r",
                row_number,
                meeting_date,
                title,
            )
            continue
        stats["council_rows_allowed"] += 1
        record = _record_from_row(cells, headers, base_url, approved_hosts, stats)
        _validate_record(record)
        records.append(record)
        stats["rows_emitted"] += 1
    if not rows:
        stats["empty_months"] += 1
        logger.warning(
            "AgendaQuick month is explicitly empty requested=%s-%02d url=%s",
            requested_year,
            requested_month,
            base_url,
        )
    return records


def _default_council_title_allowed(title: str) -> bool:
    """Accept only explicit council meetings, not council-related notices."""
    return bool(COUNCIL_BODY_RE.search(title)) and not bool(
        NON_MEETING_COUNCIL_RE.search(title)
    )


def _row_identity(cells: list[Tag]) -> tuple[str, str, str]:
    date_text = _clean_text(cells[0])
    title = _clean_text(cells[1])
    return title, date_text, _parse_date(date_text)


def _selected_value(select: Tag) -> str:
    selected = select.select_one("option[selected]") or select.select_one("option")
    return str(selected.get("value") or "") if selected else ""


def _record_from_row(
    cells: list[Tag],
    headers: list[str],
    base_url: str,
    approved_hosts: set[str],
    stats: Counter[str],
) -> dict:
    title, date_text, meeting_date = _row_identity(cells)
    if not title or not meeting_date:
        raise ValueError(
            f"AgendaQuick data row lacks title/date evidence: title={title!r} date={date_text!r}"
        )
    links = {
        "agenda_url": "",
        "minutes_url": "",
        "video_url": "",
        "agenda_packet_url": "",
    }
    detail_href = ""
    for index, cell in enumerate(cells):
        header = headers[index] if index < len(headers) else ""
        for anchor in cell.select("a[href]"):
            href = str(anchor.get("href") or "")
            label = _clean_text(anchor)
            field = _classify_link(header, label, str(anchor.get("title") or ""), href)
            if not field:
                stats["unclassified_links"] += 1
                continue
            emitted = _emit_url(href, base_url, approved_hosts, field)
            if not emitted:
                stats[f"{field}_rejected"] += 1
                continue
            if links[field]:
                stats[f"{field}_duplicates"] += 1
                continue
            links[field] = emitted
            stats[f"{field}_emitted"] += 1
            if field == "agenda_url":
                detail_href = href

    status = _status(title, links)
    stats["meeting_time_absent_by_construction"] += 1
    stats["meeting_location_absent_by_construction"] += 1
    return {
        "meeting_title": title,
        "meeting_date": meeting_date,
        "meeting_time": "",
        "meeting_location": "",
        "meeting_status": status,
        "agenda_url": links["agenda_url"],
        "minutes_url": links["minutes_url"],
        "video_url": links["video_url"],
        "agenda_packet_url": links["agenda_packet_url"],
        "ecomment_url": "",
        "meeting_id": _meeting_id(detail_href, meeting_date, title),
    }


def _parse_date(value: str) -> str:
    match = MONTH_DATE_RE.search(value)
    if not match:
        return ""
    try:
        return datetime.strptime(match.group(1), "%B %d, %Y").date().isoformat()
    except ValueError:
        return ""


def _classify_link(header: str, label: str, title: str, href: str) -> str:
    combined = f"{header} {label} {title} {href}".lower()
    query = {key.lower(): [value.lower() for value in values] for key, values in parse_qs(urlparse(href).query).items()}
    dsp = set(query.get("dsp", []))
    if "min" in dsp or "minute" in combined:
        return "minutes_url"
    if "packet" in combined:
        return "agenda_packet_url"
    if "video" in combined or "swagit" in combined or "youtube" in combined or "youtu.be" in combined:
        return "video_url"
    if "ag" in dsp or "agenda" in combined or header == "agendas":
        return "agenda_url"
    logger.warning(
        "AgendaQuick link dropped as unclassified: header=%r label=%r title=%r href=%r",
        header,
        label,
        title,
        href,
    )
    return ""


def _emit_url(href: str, base_url: str, approved_hosts: set[str], field: str) -> str:
    raw = href.strip()
    if not raw or raw.lower().startswith(
        ("javascript:", "data:", "vbscript:", "file:", "mailto:", "ftp:", "gopher:")
    ):
        logger.warning("AgendaQuick %s URL rejected: href=%r", field, href)
        return ""
    absolute = urljoin(base_url, raw)
    parsed = urlparse(absolute)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in approved_hosts:
        logger.warning("AgendaQuick %s URL rejected: url=%r", field, absolute)
        return ""
    return absolute


def _meeting_id(detail_href: str, meeting_date: str, title: str) -> str:
    query = parse_qs(urlparse(detail_href).query)
    for key in ("seq", "id"):
        values = query.get(key)
        if values and values[0]:
            return values[0]
    normalized_title = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    logger.warning(
        "AgendaQuick row lacks vendor-native sequence; using deterministic date/title ID: date=%s title=%r",
        meeting_date,
        title,
    )
    return f"{meeting_date}-{normalized_title}"


def _status(title: str, links: dict[str, str]) -> str:
    if CANCELLED_RE.search(title):
        return "Cancelled"
    if links["minutes_url"]:
        return "Minutes Available"
    if links["agenda_url"] or links["agenda_packet_url"]:
        return "Agenda Available"
    return "Scheduled"


def _clean_text(node: Tag) -> str:
    return BeautifulSoup(node.decode_contents(), "html.parser").get_text(" ", strip=True)


def _validate_record(record: dict) -> None:
    expected = {
        "meeting_title", "meeting_date", "meeting_time", "meeting_location",
        "meeting_status", "agenda_url", "minutes_url", "video_url",
        "agenda_packet_url", "ecomment_url", "meeting_id",
    }
    if set(record) != expected:
        raise ValueError(f"AgendaQuick adapter emitted wrong fields: {sorted(record)}")
    if any(not isinstance(value, str) for value in record.values()):
        raise TypeError("AgendaQuick adapter emitted a non-string field")
