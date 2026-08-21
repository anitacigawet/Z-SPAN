"""Current-month-forward Town Council meetings from Jerome's Municode calendar."""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from polite_http import make_session


logger = logging.getLogger(__name__)

DEFAULT_URL = "https://jerome-az.municodemeetings.com/calendar"
CALENDAR_HOST = "jerome-az.municodemeetings.com"
ALLOWED_FETCH_HOSTS = {CALENDAR_HOST}
ALLOWED_EMIT_HOSTS = {
    CALENDAR_HOST,
    "mccmeetings.blob.core.usgovcloudapi.net",
    "meetings.municode.com",
    "soundcloud.com",
    "www.soundcloud.com",
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
CALENDAR_DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")
MONTH_PATH_RE = re.compile(r"^/calendar/month/(\d{4})-(\d{2})/?$")
TIME_RE = re.compile(
    r"(?<!\d)([0-9]{1,2})(?::([0-9]{2}))?\s*([AP])\.?M\.?(?=\s|$|[^\w.])",
    re.IGNORECASE,
)
MAX_RESPONSE_BYTES = 2_000_000
MAX_MONTH_PAGES = 12
MAX_EVENT_DETAILS = 50
REQUEST_TIMEOUT = 45


class SourceBlockedError(RuntimeError):
    """Raised only when a recognizable upstream block page was witnessed."""


@dataclass(frozen=True)
class _CalendarRow:
    title: str
    meeting_date: str
    meeting_time: str
    detail_url: str


def scrape_calendar(url: str) -> list[dict[str, str]]:
    """Return Jerome Town Council rows from the current calendar month onward."""
    start_url = _validated_calendar_url(url or DEFAULT_URL)
    current_floor = date.today().replace(day=1)
    session = make_session()
    counters: Counter[str] = Counter()

    logger.warning(
        "field_absence field=meeting_location reason=municode_calendar_and_detail_pages_expose_no_per_row_location_signal"
    )
    logger.warning(
        "field_absence field=ecomment_url reason=municode_meeting_detail_exposes_no_ecomment_signal"
    )

    try:
        root_html = _fetch_html_bounded(session, start_url)
        page_html = _collect_current_forward_month_pages(
            session,
            start_url,
            root_html,
            current_floor=current_floor,
            counters=counters,
        )
        rows: list[_CalendarRow] = []
        for page_url, html in page_html:
            rows.extend(
                _parse_calendar_page(
                    html,
                    page_url,
                    current_floor=current_floor,
                    counters=counters,
                )
            )

        rows = _dedupe_rows(rows, counters)
        if not rows:
            logger.warning("health_empty_kind=confirmed_empty")
            logger.info("scrape_summary counters=%s", dict(sorted(counters.items())))
            return []
        if len(rows) > MAX_EVENT_DETAILS:
            raise ValueError(
                "Jerome current-month-forward calendar exceeded the bounded "
                f"detail cap ({len(rows)} > {MAX_EVENT_DETAILS})"
            )

        meetings: list[dict[str, str]] = []
        for row in rows:
            detail_html = _fetch_html_bounded(session, row.detail_url)
            meeting = _parse_detail_page(detail_html, row, counters=counters)
            meetings.append(meeting)
            counters["rows_accepted"] += 1
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


def _collect_current_forward_month_pages(
    session: requests.Session,
    start_url: str,
    root_html: str,
    *,
    current_floor: date,
    counters: Counter[str],
) -> list[tuple[str, str]]:
    soup = BeautifulSoup(root_html, "html.parser")
    root_month = _validate_calendar_surface(soup, start_url)
    current_key = current_floor.strftime("%Y-%m")
    discovered: dict[str, str] = {}
    for anchor in soup.find_all("a", href=True):
        absolute = urljoin(start_url, str(anchor.get("href", "")))
        parsed = urlparse(absolute)
        match = MONTH_PATH_RE.fullmatch(parsed.path)
        if not match:
            continue
        month_key = f"{match.group(1)}-{match.group(2)}"
        if month_key >= current_key:
            discovered[month_key] = _validated_calendar_url(absolute)

    if root_month >= current_key:
        discovered[root_month] = start_url
    if current_key not in discovered:
        raise ValueError(
            "Jerome calendar did not expose the current month in its witnessed month navigation "
            f"(current={current_key}, root={root_month}, discovered={sorted(discovered)})"
        )
    if len(discovered) > MAX_MONTH_PAGES:
        raise ValueError(
            "Jerome calendar exposed more current-forward month pages than the bounded contract "
            f"({len(discovered)} > {MAX_MONTH_PAGES})"
        )

    pages: list[tuple[str, str]] = []
    for month_key, page_url in sorted(discovered.items()):
        html = root_html if page_url == start_url else _fetch_html_bounded(session, page_url)
        page_soup = BeautifulSoup(html, "html.parser")
        witnessed_month = _validate_calendar_surface(page_soup, page_url)
        if witnessed_month != month_key:
            raise ValueError(
                f"Jerome month navigation drift: requested {month_key}, witnessed {witnessed_month} at {page_url}"
            )
        counters["month_pages_fetched"] += 1
        pages.append((page_url, html))
    return pages


def _parse_calendar_page(
    html: str,
    page_url: str,
    *,
    current_floor: date,
    counters: Counter[str],
) -> list[_CalendarRow]:
    soup = BeautifulSoup(html, "html.parser")
    _validate_calendar_surface(soup, page_url)
    rows: list[_CalendarRow] = []
    for container in soup.select("div.contents"):
        anchor = container.find("a", href=True)
        if anchor is None:
            continue
        title = _clean_text(anchor.get_text(" ", strip=True))
        href = str(anchor.get("href", "") or "")
        if not _is_governing_body_row(title, href):
            continue

        counters["governing_body_rows_seen"] += 1
        row_text = _clean_text(container.get_text(" ", strip=True))
        date_match = CALENDAR_DATE_RE.search(row_text)
        if not date_match:
            raise ValueError(
                f"Jerome governing-body row has no parseable MM/DD/YYYY date: title={title!r} text={row_text!r}"
            )
        meeting_day = datetime.strptime(date_match.group(1), "%m/%d/%Y").date()
        if meeting_day < current_floor:
            counters["rows_dropped_before_current_floor"] += 1
            logger.info(
                "drop_row_before_current_floor title=%r meeting_date=%s current_floor=%s",
                title,
                meeting_day.isoformat(),
                current_floor.isoformat(),
            )
            continue
        if not title:
            raise ValueError(f"Jerome governing-body row has an empty title: {row_text!r}")

        detail_url = _emit_url(
            href,
            page_url,
            field="detail_url",
            row_label=title,
            allowed_hosts=ALLOWED_FETCH_HOSTS,
        )
        if not detail_url:
            raise ValueError(f"Jerome governing-body row has no safe detail URL: title={title!r} href={href!r}")
        meeting_time = _extract_time(row_text, row_label=title)
        rows.append(
            _CalendarRow(
                title=title,
                meeting_date=meeting_day.isoformat(),
                meeting_time=meeting_time,
                detail_url=detail_url,
            )
        )
        counters["rows_in_current_window"] += 1
    return rows


def _parse_detail_page(
    html: str,
    row: _CalendarRow,
    *,
    counters: Counter[str],
) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1")
    detail_title = _clean_text(heading.get_text(" ", strip=True)) if heading else ""
    detail_date = soup.select_one(".date-display-single")
    if not detail_title or detail_date is None:
        raise ValueError(f"Jerome detail surface drift at {row.detail_url}")
    if detail_title.casefold() != row.title.casefold():
        raise ValueError(
            f"Jerome detail title mismatch: calendar={row.title!r}, detail={detail_title!r}, url={row.detail_url}"
        )
    detail_date_text = _clean_text(detail_date.get_text(" ", strip=True))
    try:
        witnessed_day = datetime.strptime(
            re.sub(r"\s+-\s+.*$", "", detail_date_text),
            "%A, %B %d, %Y",
        ).date()
    except ValueError:
        date_match = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})", detail_date_text)
        if not date_match:
            raise ValueError(f"Jerome detail has an unparseable date: {detail_date_text!r}")
        witnessed_day = datetime.strptime(date_match.group(1), "%B %d, %Y").date()
    if witnessed_day.isoformat() != row.meeting_date:
        raise ValueError(
            f"Jerome detail date mismatch: calendar={row.meeting_date}, detail={witnessed_day.isoformat()}, url={row.detail_url}"
        )

    agenda_url = _field_url(soup, ".field-name-field-agenda-link a[href]", row, "agenda_url", counters)
    minutes_url = _field_url(soup, ".field-name-field-minutes-link a[href]", row, "minutes_url", counters)
    agenda_packet_url = _field_url(
        soup,
        ".field-name-field-packets-link a[href]",
        row,
        "agenda_packet_url",
        counters,
    )
    video_url = _field_url(soup, ".field-name-field-video-link a[href]", row, "video_url", counters)
    meeting_id = urlparse(row.detail_url).path.rstrip("/").split("/")[-1]
    if not meeting_id:
        logger.warning("meeting_id_absent title=%r detail_url=%s", row.title, row.detail_url)

    meeting = {
        "meeting_title": row.title,
        "meeting_date": row.meeting_date,
        "meeting_time": row.meeting_time,
        "meeting_location": "",
        "meeting_status": _derive_status(row.title, agenda_url, minutes_url, agenda_packet_url),
        "agenda_url": agenda_url,
        "minutes_url": minutes_url,
        "video_url": video_url,
        "agenda_packet_url": agenda_packet_url,
        "ecomment_url": "",
        "meeting_id": meeting_id,
    }
    return {field: meeting[field] for field in CANONICAL_FIELDS}


def _field_url(
    soup: BeautifulSoup,
    selector: str,
    row: _CalendarRow,
    field: str,
    counters: Counter[str],
) -> str:
    anchors = soup.select(selector)
    if not anchors:
        counters[f"{field}_absent"] += 1
        return ""
    if len(anchors) > 1:
        logger.warning(
            "multiple_field_links field=%s title=%r count=%d using_first=true",
            field,
            row.title,
            len(anchors),
        )
    href = str(anchors[0].get("href", "") or "")
    return _emit_url(
        href,
        row.detail_url,
        field=field,
        row_label=row.title,
        allowed_hosts=ALLOWED_EMIT_HOSTS,
    )


def _dedupe_rows(rows: list[_CalendarRow], counters: Counter[str]) -> list[_CalendarRow]:
    unique: dict[str, _CalendarRow] = {}
    for row in rows:
        if row.detail_url in unique:
            counters["duplicate_calendar_rows_dropped"] += 1
            logger.warning(
                "drop_duplicate_calendar_row detail_url=%s title=%r meeting_date=%s",
                row.detail_url,
                row.title,
                row.meeting_date,
            )
            continue
        unique[row.detail_url] = row
    return list(unique.values())


def _validate_calendar_surface(soup: BeautifulSoup, url: str) -> str:
    title = _clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    h1 = soup.find("h1")
    h1_text = _clean_text(h1.get_text(" ", strip=True)) if h1 else ""
    month_heading = None
    for heading in soup.find_all(["h2", "h3"]):
        candidate = _clean_text(heading.get_text(" ", strip=True))
        if re.fullmatch(r"[A-Z][a-z]+ \d{4}", candidate):
            month_heading = candidate
            break
    if (
        "Town of Jerome Arizona Meetings" not in title
        or h1_text != "Calendar"
        or month_heading is None
        or soup.select_one("div.calendar-calendar div.month-view") is None
    ):
        page_text = _clean_text(soup.get_text(" ", strip=True))[:1000]
        if BLOCK_PAGE_RE.search(f"{title} {page_text}"):
            raise SourceBlockedError(f"recognized block page at {url}: title={title!r}")
        raise ValueError(
            f"Jerome calendar fingerprint drift at {url}: title={title!r}, h1={h1_text!r}, month={month_heading!r}"
        )
    month_day = datetime.strptime(month_heading, "%B %Y").date()
    logger.info(
        "vendor_fingerprint witness=municode_calendar_monthview url=%s month=%s",
        url,
        month_day.strftime("%Y-%m"),
    )
    return month_day.strftime("%Y-%m")


def _is_governing_body_row(title: str, href: str) -> bool:
    path = urlparse(urljoin(DEFAULT_URL, href)).path.casefold()
    return path.startswith("/bc-towncouncil/") or bool(
        re.search(r"\b(?:town\s+)?council\b", title, re.IGNORECASE)
    )


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


def _emit_url(
    href: str,
    base_url: str,
    *,
    field: str,
    row_label: str,
    allowed_hosts: set[str],
) -> str:
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
    if not _host_allowed(host, allowed_hosts):
        logger.warning(
            "drop_url_disallowed_host field=%s row=%r href=%r host=%r allowed=%r",
            field,
            row_label,
            href,
            host,
            sorted(allowed_hosts),
        )
        return ""
    return absolute


def _fetch_html_bounded(session: requests.Session, url: str) -> str:
    with session.get(url, timeout=REQUEST_TIMEOUT, stream=True, allow_redirects=True) as response:
        response.raise_for_status()
        final_host = (urlparse(response.url).hostname or "").lower()
        if not _host_allowed(final_host, ALLOWED_FETCH_HOSTS):
            raise ValueError(f"Redirect to disallowed host: {final_host} (started from {url})")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Response from {url} exceeded {MAX_RESPONSE_BYTES} bytes")
        return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _validated_calendar_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not _host_allowed(host, ALLOWED_FETCH_HOSTS):
        raise ValueError(f"Jerome calendar URL must be HTTPS on {CALENDAR_HOST}: {url!r}")
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
