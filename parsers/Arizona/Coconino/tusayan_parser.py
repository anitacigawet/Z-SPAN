"""Tusayan — Destiny AgendaQuick meeting parser."""

import datetime
import logging
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup


logger = logging.getLogger(__name__)

FIELDS = (
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
    "public.destinyhosted.com",
    "destinyhosted.com",
    "tusayan-az.gov",
    "www.tusayan-az.gov",
}

MAX_RESPONSE_BYTES = 5_000_000
MAX_MONTHS = 30
CANONICAL_STATUSES = {
    "Scheduled",
    "Agenda Available",
    "Minutes Available",
    "Cancelled",
}

TIME_RE = re.compile(
    r"\b(\d{1,2}):(\d{2})\s*([AaPp]\.?[Mm]\.?)(?=\s|$|[^\w.])",
    re.IGNORECASE,
)
CANCELLATION_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
BOARD_ID_RE = re.compile(r"(?:[?&])id=(\d+)(?:&|$)", re.IGNORECASE)
MONTH_DATE_RE = re.compile(
    r"\b([A-Z][a-z]+ \d{1,2}, \d{4})\b"
)
SLASH_DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")
ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
BAD_SCHEMES = (
    "javascript:",
    "data:",
    "vbscript:",
    "file:",
    "mailto:",
    "ftp:",
    "gopher:",
)


def scrape_calendar(calendar_url: str) -> list[dict]:
    """Scrape Tusayan AgendaQuick meeting rows from a rolling month window."""
    board_id = _extract_board_id(calendar_url)
    session = requests.Session()
    today = datetime.date.today()
    months = _month_window(today)
    results: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    totals = {
        "rows_seen": 0,
        "rows_accepted": 0,
        "rows_dropped": 0,
    }
    drop_reasons: dict[str, int] = {}

    logger.info(
        "Tusayan AgendaQuick parser startup board_id=%s window_start=%s-%02d "
        "window_end=%s-%02d months=%s",
        board_id,
        months[0][0],
        months[0][1],
        months[-1][0],
        months[-1][1],
        len(months),
    )
    logger.warning(
        "meeting_time and meeting_location are honest-empty unless visible in "
        "the same table row; Tusayan list rows observed without default time or "
        "location columns"
    )

    for year, month in months:
        month_url = _build_month_url(calendar_url, board_id, year, month)
        current_month = year == today.year and month == today.month
        try:
            html, response_url = _fetch_text_bounded(session, month_url)
        except requests.exceptions.SSLError:
            raise
        except requests.RequestException as exc:
            logger.warning(
                "architectural blocker fetching Tusayan month year=%s month=%s "
                "url=%s blocker=%s",
                year,
                month,
                month_url,
                exc,
            )
            continue
        except ValueError as exc:
            logger.warning(
                "architectural blocker fetching Tusayan month year=%s month=%s "
                "url=%s blocker=%s",
                year,
                month,
                month_url,
                exc,
            )
            continue

        if not _witness_destiny_surface(html, response_url):
            logger.warning(
                "vendor fingerprint failed for year=%s month=%s url=%s; "
                "returning no rows for this page",
                year,
                month,
                response_url,
            )
            _record_drop(drop_reasons, "fingerprint_failed")
            continue

        month_records, counters, month_drops = _parse_month_rows(
            html,
            response_url,
            year,
            month,
        )
        _merge_counters(totals, counters, drop_reasons, month_drops)
        logger.info(
            "page-fetch iteration complete year=%s month=%s url=%s row_count=%s",
            year,
            month,
            response_url,
            counters["rows_seen"],
        )

        if counters["rows_seen"] == 0:
            empty_shape = _empty_shape(html)
            if current_month:
                logger.warning(
                    "current month returned 0 rows url=%s empty_shape=%s",
                    response_url,
                    empty_shape,
                )
            else:
                logger.warning(
                    "non-current month returned 0 rows url=%s empty_shape=%s",
                    response_url,
                    empty_shape,
                )

        _append_deduped_records(month_records, seen, results, totals, drop_reasons)

        companion_records, companion_counters, companion_drops = _try_minutes_companion(
            session,
            calendar_url,
            board_id,
            year,
            month,
        )
        _merge_counters(totals, companion_counters, drop_reasons, companion_drops)
        _append_deduped_records(
            companion_records,
            seen,
            results,
            totals,
            drop_reasons,
        )

    logger.info(
        "Tusayan aggregate rows_seen=%s rows_accepted=%s rows_dropped=%s "
        "drop_reasons=%s emitted=%s",
        totals["rows_seen"],
        totals["rows_accepted"],
        totals["rows_dropped"],
        drop_reasons,
        len(results),
    )
    return results


def _extract_board_id(calendar_url: str) -> str:
    match = BOARD_ID_RE.search(calendar_url)
    if not match:
        logger.warning(
            "could not parse Destiny board id from calendar_url=%r",
            calendar_url,
        )
        raise ValueError(f"Could not parse Destiny board id from {calendar_url!r}")
    logger.info("parsed Destiny board_id=%s from calendar_url=%s", match.group(1), calendar_url)
    return match.group(1)


def _month_window(today: datetime.date) -> list[tuple[int, int]]:
    start_month_index = today.year * 12 + today.month - 1 - 18
    end_month_index = today.year * 12 + today.month - 1 + 6
    months: list[tuple[int, int]] = []
    for index in range(start_month_index, end_month_index + 1):
        if len(months) >= MAX_MONTHS:
            logger.warning(
                "month window hit cap max_months=%s last_index=%s",
                MAX_MONTHS,
                index,
            )
            break
        year, zero_based_month = divmod(index, 12)
        months.append((year, zero_based_month + 1))
    return months


def _build_month_url(
    calendar_url: str,
    board_id: str,
    year: int,
    month: int,
    dsp: str = "",
) -> str:
    parsed = urlparse(calendar_url)
    query = {
        "id": board_id,
        "mt": "ALL",
        "get_month": str(month),
        "get_year": str(year),
    }
    if dsp:
        query["dsp"] = dsp
    built = urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            "",
            urlencode(query),
            "",
        )
    )
    logger.info(
        "built Destiny month url year=%s month=%s dsp=%r url=%s",
        year,
        month,
        dsp,
        built,
    )
    return built


def _fetch_text_bounded(
    session: requests.Session,
    url: str,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> tuple[str, str]:
    with session.get(
        url,
        timeout=30,
        stream=True,
        allow_redirects=True,
        verify=True,
    ) as response:
        final_host = _host(response.url)
        if not _is_allowed_host(final_host):
            raise ValueError(
                f"Redirect to disallowed host: {final_host} (started from {url})"
            )
        if response.status_code != 200:
            raise ValueError(
                f"Non-200 response from {url}: status={response.status_code}"
            )
        body = b""
        for chunk in response.iter_content(chunk_size=64 * 1024):
            body += chunk
            if len(body) > max_bytes:
                raise ValueError(f"Response from {url} exceeded {max_bytes} bytes")
        encoding = response.encoding or "utf-8"
        logger.info(
            "fetched url=%s final_url=%s bytes=%s encoding=%s",
            url,
            response.url,
            len(body),
            encoding,
        )
        return body.decode(encoding, errors="replace"), response.url


def _witness_destiny_surface(html: str, response_url: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    host = _host(response_url)
    tokens: list[str] = []
    if host == "public.destinyhosted.com":
        tokens.append("host:public.destinyhosted.com")
    if "AgendaQuick" in html:
        tokens.append("text:AgendaQuick")
    if soup.find("table", class_=lambda value: value and "listclass" in value.split()):
        tokens.append("table.listclass")
    if soup.find("form", attrs={"name": "form1"}):
        tokens.append("form[name=form1]")

    logger.info("Destiny fingerprint tokens url=%s tokens=%s", response_url, tokens)
    has_product = "text:AgendaQuick" in tokens
    has_structure = "table.listclass" in tokens or "form[name=form1]" in tokens
    has_expected_host = "host:public.destinyhosted.com" in tokens
    if has_expected_host and has_product and has_structure:
        return True
    logger.warning(
        "Destiny fingerprint incomplete url=%s host_ok=%s product_ok=%s "
        "structure_ok=%s tokens=%s",
        response_url,
        has_expected_host,
        has_product,
        has_structure,
        tokens,
    )
    return False


def _parse_month_rows(
    html: str,
    base_url: str,
    year: int,
    month: int,
) -> tuple[list[dict], dict[str, int], dict[str, int]]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="meeting-table")
    if table is None:
        table = soup.find(
            "table",
            class_=lambda value: value and "listclass" in value.split(),
        )
    counters = {
        "rows_seen": 0,
        "rows_accepted": 0,
        "rows_dropped": 0,
    }
    drop_reasons: dict[str, int] = {}
    records: list[dict] = []
    if table is None:
        logger.warning(
            "month dropped no meeting table found year=%s month=%s url=%s",
            year,
            month,
            base_url,
        )
        counters["rows_dropped"] += 1
        _record_drop(drop_reasons, "missing_table")
        return records, counters, drop_reasons

    headers = _extract_headers(table)
    tbody = table.find("tbody")
    row_elements = tbody.find_all("tr", recursive=False) if tbody else table.find_all("tr")
    for row_index, row in enumerate(row_elements, start=1):
        cells = row.find_all("td", recursive=False)
        if not cells:
            logger.info(
                "row skipped non-data row year=%s month=%s row_index=%s",
                year,
                month,
                row_index,
            )
            continue

        counters["rows_seen"] += 1
        row_text = _clean_text(row)
        logger.info(
            "row seen year=%s month=%s row_index=%s cell_count=%s row_text=%r",
            year,
            month,
            row_index,
            len(cells),
            row_text,
        )

        if len(cells) < 2:
            logger.warning(
                "row dropped insufficient cells year=%s month=%s row_index=%s "
                "cell_count=%s row_text=%r",
                year,
                month,
                row_index,
                len(cells),
                row_text,
            )
            counters["rows_dropped"] += 1
            _record_drop(drop_reasons, "insufficient_cells")
            continue

        record = _record_from_row(cells, headers, base_url, year, month, row_index)
        if not record["meeting_title"] or not record["meeting_date"]:
            logger.warning(
                "row dropped missing required evidence year=%s month=%s "
                "row_index=%s title=%r date=%r row_text=%r",
                year,
                month,
                row_index,
                record["meeting_title"],
                record["meeting_date"],
                row_text,
            )
            counters["rows_dropped"] += 1
            _record_drop(drop_reasons, "missing_title_or_date")
            continue

        records.append(record)
        counters["rows_accepted"] += 1
        logger.info(
            "row accepted year=%s month=%s row_index=%s emitted=%s",
            year,
            month,
            row_index,
            record,
        )

    return records, counters, drop_reasons


def _extract_headers(table) -> list[str]:
    thead = table.find("thead")
    header_cells = thead.find_all(["td", "th"]) if thead else []
    headers = [_clean_text(cell).lower() for cell in header_cells]
    logger.info("table headers witnessed headers=%s", headers)
    return headers


def _record_from_row(
    cells,
    headers: list[str],
    base_url: str,
    year: int,
    month: int,
    row_index: int,
) -> dict:
    date_cell = cells[0]
    title_cell = cells[1]
    date_text = _clean_text(date_cell)
    title = _clean_text(title_cell)
    meeting_date = _parse_date(date_text, year, month, row_index)
    meeting_time = _extract_time(_clean_text(date_cell.parent), year, month, row_index)
    meeting_location = _extract_location(cells, headers, year, month, row_index)

    agenda_url = ""
    minutes_url = ""
    video_url = ""
    agenda_packet_url = ""
    ecomment_url = ""
    detail_href = ""

    for cell_index, cell in enumerate(cells):
        header = headers[cell_index] if cell_index < len(headers) else ""
        links = cell.find_all("a", href=True)
        logger.info(
            "cell evidence year=%s month=%s row_index=%s cell_index=%s "
            "header=%r link_count=%s text=%r",
            year,
            month,
            row_index,
            cell_index,
            header,
            len(links),
            _clean_text(cell),
        )
        if not links:
            _log_honest_empty_links(header, year, month, row_index, cell_index)
            continue
        for link in links:
            href = link.get("href", "")
            label = _clean_text(link)
            title_attr = _clean_text(link.get("title", ""))
            target_field = _classify_link(header, label, title_attr, href)
            if target_field == "":
                logger.warning(
                    "link dropped unclassified year=%s month=%s row_index=%s "
                    "cell_index=%s header=%r label=%r title=%r href=%r",
                    year,
                    month,
                    row_index,
                    cell_index,
                    header,
                    label,
                    title_attr,
                    href,
                )
                continue
            emitted = _emit_url(href, base_url, target_field, year, month, row_index)
            if emitted == "":
                continue
            if target_field == "agenda_url" and agenda_url == "":
                agenda_url = emitted
                detail_href = href
            elif target_field == "minutes_url" and minutes_url == "":
                minutes_url = emitted
            elif target_field == "video_url" and video_url == "":
                video_url = emitted
            elif target_field == "agenda_packet_url" and agenda_packet_url == "":
                agenda_packet_url = emitted
            elif target_field == "ecomment_url" and ecomment_url == "":
                ecomment_url = emitted
            else:
                logger.warning(
                    "link dropped duplicate field year=%s month=%s row_index=%s "
                    "field=%s emitted_url=%s",
                    year,
                    month,
                    row_index,
                    target_field,
                    emitted,
                )

    meeting_id = _extract_meeting_id(detail_href, meeting_date, title, year, month, row_index)
    vendor_status = _extract_vendor_status(cells, headers)
    status = _classify_status(
        title,
        agenda_url,
        minutes_url,
        agenda_packet_url,
        vendor_status,
        year,
        month,
        row_index,
    )
    record = {
        "meeting_title": title,
        "meeting_date": meeting_date,
        "meeting_time": meeting_time,
        "meeting_location": meeting_location,
        "meeting_status": status,
        "agenda_url": agenda_url,
        "minutes_url": minutes_url,
        "video_url": video_url,
        "agenda_packet_url": agenda_packet_url,
        "ecomment_url": ecomment_url,
        "meeting_id": meeting_id,
    }
    return {field: str(record.get(field, "")) for field in FIELDS}


def _classify_link(header: str, label: str, title_attr: str, href: str) -> str:
    text = f"{header} {label} {title_attr} {href}".lower()
    parsed_query = parse_qs(urlparse(href).query)
    dsp_values = {value.lower() for value in parsed_query.get("dsp", [])}
    if "min" in dsp_values:
        return "minutes_url"
    if "ag" in dsp_values:
        return "agenda_url"
    if "minute" in text:
        return "minutes_url"
    if "packet" in text:
        return "agenda_packet_url"
    if "ecomment" in text or "comment" in text:
        return "ecomment_url"
    if (
        "video" in text
        or "youtube" in text
        or "youtu.be" in text
        or "swagit" in text
        or "recording" in text
    ):
        return "video_url"
    if "agenda" in text or header == "agendas":
        return "agenda_url"
    return ""


def _parse_date(date_text: str, year: int, month: int, row_index: int) -> str:
    for regex, date_format in (
        (MONTH_DATE_RE, "%B %d, %Y"),
        (SLASH_DATE_RE, "%m/%d/%Y"),
        (ISO_DATE_RE, "%Y-%m-%d"),
    ):
        match = regex.search(date_text)
        if not match:
            continue
        value = match.group(1)
        try:
            parsed = datetime.datetime.strptime(value, date_format).date()
        except ValueError as exc:
            logger.warning(
                "date candidate rejected year=%s month=%s row_index=%s "
                "value=%r reason=%s",
                year,
                month,
                row_index,
                value,
                exc,
            )
            continue
        iso = parsed.isoformat()
        logger.info(
            "date emitted year=%s month=%s row_index=%s raw=%r iso=%s",
            year,
            month,
            row_index,
            value,
            iso,
        )
        return iso
    logger.warning(
        "date extraction returned empty year=%s month=%s row_index=%s "
        "raw_text=%r reason=no supported date pattern",
        year,
        month,
        row_index,
        date_text,
    )
    return ""


def _extract_time(row_text: str, year: int, month: int, row_index: int) -> str:
    match = TIME_RE.search(row_text[:500])
    if not match:
        logger.info(
            "meeting_time honest-empty year=%s month=%s row_index=%s "
            "reason=no same-row time evidence",
            year,
            month,
            row_index,
        )
        return ""
    hour = int(match.group(1))
    minute = match.group(2)
    suffix = match.group(3).replace(".", "").upper()
    if hour == 0 or hour > 12:
        logger.warning(
            "meeting_time rejected year=%s month=%s row_index=%s raw=%r "
            "reason=hour outside 1-12",
            year,
            month,
            row_index,
            match.group(0),
        )
        return ""
    emitted = f"{hour}:{minute} {suffix}"
    logger.info(
        "meeting_time emitted year=%s month=%s row_index=%s raw=%r value=%s",
        year,
        month,
        row_index,
        match.group(0),
        emitted,
    )
    return emitted


def _extract_location(cells, headers: list[str], year: int, month: int, row_index: int) -> str:
    for index, header in enumerate(headers):
        if "location" not in header:
            continue
        if index >= len(cells):
            logger.warning(
                "meeting_location empty year=%s month=%s row_index=%s "
                "reason=location header without matching cell",
                year,
                month,
                row_index,
            )
            return ""
        value = _clean_text(cells[index])
        if value:
            logger.info(
                "meeting_location emitted year=%s month=%s row_index=%s value=%r",
                year,
                month,
                row_index,
                value,
            )
            return value
        logger.warning(
            "meeting_location empty year=%s month=%s row_index=%s "
            "reason=location cell present but blank",
            year,
            month,
            row_index,
        )
        return ""
    logger.info(
        "meeting_location honest-empty year=%s month=%s row_index=%s "
        "reason=no same-row location column",
        year,
        month,
        row_index,
    )
    return ""


def _classify_status(
    title: str,
    agenda_url: str,
    minutes_url: str,
    agenda_packet_url: str,
    vendor_status: str,
    year: int,
    month: int,
    row_index: int,
) -> str:
    if vendor_status and vendor_status not in CANONICAL_STATUSES:
        logger.warning(
            "unknown vendor status vocabulary year=%s month=%s row_index=%s "
            "vendor_status=%r; canonical status will use row evidence",
            year,
            month,
            row_index,
            vendor_status,
        )
    if CANCELLATION_RE.search(title[:300]):
        logger.info(
            "meeting_status emitted Cancelled year=%s month=%s row_index=%s "
            "evidence=title cancellation regex title=%r",
            year,
            month,
            row_index,
            title,
        )
        return "Cancelled"
    if minutes_url:
        logger.info(
            "meeting_status emitted Minutes Available year=%s month=%s "
            "row_index=%s evidence=minutes_url",
            year,
            month,
            row_index,
        )
        return "Minutes Available"
    if agenda_url or agenda_packet_url:
        logger.info(
            "meeting_status emitted Agenda Available year=%s month=%s "
            "row_index=%s evidence=agenda_or_packet",
            year,
            month,
            row_index,
        )
        return "Agenda Available"
    logger.info(
        "meeting_status default Scheduled year=%s month=%s row_index=%s "
        "reason=honest-empty no cancellation/minutes/agenda evidence",
        year,
        month,
        row_index,
    )
    return "Scheduled"


def _extract_vendor_status(cells, headers: list[str]) -> str:
    for index, header in enumerate(headers):
        if "status" in header and index < len(cells):
            return _clean_text(cells[index])
    return ""


def _extract_meeting_id(
    detail_href: str,
    meeting_date: str,
    title: str,
    year: int,
    month: int,
    row_index: int,
) -> str:
    seq_values = parse_qs(urlparse(detail_href).query).get("seq", [])
    if seq_values and seq_values[0]:
        logger.info(
            "meeting_id emitted year=%s month=%s row_index=%s seq=%s",
            year,
            month,
            row_index,
            seq_values[0],
        )
        return seq_values[0]
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:80]
    fallback = f"{meeting_date}-{slug}" if meeting_date and slug else ""
    if fallback:
        logger.warning(
            "meeting_id fallback emitted year=%s month=%s row_index=%s "
            "fallback=%s reason=no seq parameter",
            year,
            month,
            row_index,
            fallback,
        )
    else:
        logger.warning(
            "meeting_id empty year=%s month=%s row_index=%s "
            "reason=no seq parameter and insufficient fallback evidence",
            year,
            month,
            row_index,
        )
    return fallback


def _emit_url(
    href: str,
    base_url: str,
    field: str,
    year: int,
    month: int,
    row_index: int,
) -> str:
    if not href:
        logger.warning(
            "URL dropped field=%s year=%s month=%s row_index=%s "
            "rejected_input=%r reason=empty href",
            field,
            year,
            month,
            row_index,
            href,
        )
        return ""
    lowered = href.lower().lstrip()
    for bad_scheme in BAD_SCHEMES:
        if lowered.startswith(bad_scheme):
            logger.warning(
                "URL dropped field=%s year=%s month=%s row_index=%s "
                "rejected_input=%r reason=bad scheme %s",
                field,
                year,
                month,
                row_index,
                href,
                bad_scheme,
            )
            return ""
    absolute = urljoin(base_url, href)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        logger.warning(
            "URL dropped field=%s year=%s month=%s row_index=%s "
            "rejected_input=%r absolute=%r reason=scheme not http/https",
            field,
            year,
            month,
            row_index,
            href,
            absolute,
        )
        return ""
    emit_host = _host(absolute)
    if not _is_allowed_host(emit_host):
        logger.warning(
            "URL dropped field=%s year=%s month=%s row_index=%s "
            "rejected_input=%r absolute=%r reason=host %s not allowlisted",
            field,
            year,
            month,
            row_index,
            href,
            absolute,
            emit_host,
        )
        return ""
    logger.info(
        "URL emitted field=%s year=%s month=%s row_index=%s href=%r absolute=%s",
        field,
        year,
        month,
        row_index,
        href,
        absolute,
    )
    return absolute


def _try_minutes_companion(
    session: requests.Session,
    calendar_url: str,
    board_id: str,
    year: int,
    month: int,
) -> tuple[list[dict], dict[str, int], dict[str, int]]:
    empty_counters = {
        "rows_seen": 0,
        "rows_accepted": 0,
        "rows_dropped": 0,
    }
    empty_drops: dict[str, int] = {}
    minutes_url = _build_month_url(calendar_url, board_id, year, month, dsp="min")
    try:
        html, response_url = _fetch_text_bounded(session, minutes_url)
    except requests.exceptions.SSLError:
        raise
    except (requests.RequestException, ValueError) as exc:
        logger.warning(
            "minutes companion fetch failed year=%s month=%s url=%s blocker=%s",
            year,
            month,
            minutes_url,
            exc,
        )
        _record_drop(empty_drops, "minutes_companion_fetch_failed")
        return [], empty_counters, empty_drops
    if not _witness_destiny_surface(html, response_url):
        logger.warning(
            "minutes companion rejected year=%s month=%s url=%s "
            "reason=fingerprint failed",
            year,
            month,
            response_url,
        )
        _record_drop(empty_drops, "minutes_companion_fingerprint_failed")
        return [], empty_counters, empty_drops
    records, counters, drops = _parse_month_rows(html, response_url, year, month)
    logger.info(
        "minutes companion witnessed year=%s month=%s url=%s row_count=%s",
        year,
        month,
        response_url,
        counters["rows_seen"],
    )
    return records, counters, drops


def _log_honest_empty_links(
    header: str,
    year: int,
    month: int,
    row_index: int,
    cell_index: int,
) -> None:
    logger.info(
        "cell links honest-empty year=%s month=%s row_index=%s cell_index=%s "
        "header=%r reason=no same-row anchors in cell",
        year,
        month,
        row_index,
        cell_index,
        header,
    )


def _clean_text(value) -> str:
    if value is None:
        return ""
    text = BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _empty_shape(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="meeting-table")
    if table is None:
        return "no meeting-table"
    tbody = table.find("tbody")
    if tbody is None:
        return "meeting-table without tbody"
    return "meeting-table tbody with zero data rows"


def _is_allowed_host(host: str) -> bool:
    normalized = host.lower()
    return normalized in ALLOWED_HOSTS or normalized.endswith(".destinyhosted.com")


def _host(url: str) -> str:
    return (urlparse(url).netloc.split(":")[0] or "").lower()


def _record_drop(drop_reasons: dict[str, int], reason: str) -> None:
    drop_reasons[reason] = drop_reasons.get(reason, 0) + 1


def _merge_counters(
    totals: dict[str, int],
    counters: dict[str, int],
    drop_reasons: dict[str, int],
    month_drops: dict[str, int],
) -> None:
    for key in totals:
        totals[key] += counters.get(key, 0)
    for reason, count in month_drops.items():
        drop_reasons[reason] = drop_reasons.get(reason, 0) + count


def _append_deduped_records(
    records: list[dict],
    seen: set[tuple[str, str, str]],
    results: list[dict],
    totals: dict[str, int],
    drop_reasons: dict[str, int],
) -> None:
    for record in records:
        key = (
            record["meeting_title"],
            record["meeting_date"],
            record["agenda_url"],
        )
        if key in seen:
            logger.warning(
                "row dropped duplicate key title=%r date=%s agenda_url=%s",
                record["meeting_title"],
                record["meeting_date"],
                record["agenda_url"],
            )
            totals["rows_dropped"] += 1
            _record_drop(drop_reasons, "duplicate")
            continue
        seen.add(key)
        results.append(record)
