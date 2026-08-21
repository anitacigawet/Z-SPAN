"""Bounded current-window parser for Kearny's official Municode portal."""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from polite_http import make_session


logger = logging.getLogger(__name__)

DEFAULT_URL = "https://kearny-az.municodemeetings.com/"
FETCH_HOSTS = {"kearny-az.municodemeetings.com"}
EMIT_HOSTS = FETCH_HOSTS | {
    "meetings.municode.com",
    "mccmeetings.blob.core.usgovcloudapi.net",
}
MAX_RESPONSE_BYTES = 2_000_000
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
EXPECTED_HEADERS = (
    "Date",
    "Meeting",
    "Agenda",
    "Agenda Packet",
    "Minutes",
    "Video",
    "View",
)
COUNCIL_RE = re.compile(
    r"\b(?:regular\s+meeting\s+of\s+the\s+council\s+of\s+the\s+town\s+of\s+kearny|"
    r"town\s+of\s+kearny\s+council|town\s+council|city\s+council)\b",
    re.IGNORECASE,
)
NON_COUNCIL_RE = re.compile(
    r"\b(?:planning\s+(?:and\s+zoning\s+)?commission|board\s+of\s+adjustment|"
    r"library\s+board|industrial\s+development\s+authority)\b",
    re.IGNORECASE,
)
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
DATE_RE = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b")
TIME_RE = re.compile(
    r"(?<!\d)(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([AP])\.?M\.?(?=\s|$|[^\w.])",
    re.IGNORECASE,
)


def scrape_calendar(url: str | None = None) -> list[dict[str, str]]:
    """Return Kearny council meetings in a rolling, one-year current window."""
    source_url = _validate_source_url(url or DEFAULT_URL)
    floor = date.today().replace(day=1)
    upper = floor.replace(year=floor.year + 1) - timedelta(days=1)
    target = _build_window_url(source_url, floor, upper)

    with make_session() as session:
        html = _fetch_html_bounded(session, target)
    if html is None:
        return []

    meetings = _parse_html(html, target, floor, upper)
    _assert_schema(meetings)
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
    logger.info(
        "Kearny scrape complete: current_window=%d floor=%s upper=%s",
        len(meetings),
        floor.isoformat(),
        upper.isoformat(),
    )
    return meetings


def _fetch_html_bounded(session: Any, url: str) -> str | None:
    with session.get(url, timeout=35, stream=True, allow_redirects=True) as response:
        final_host = _host(response.url)
        if final_host not in FETCH_HOSTS:
            raise ValueError(f"Kearny redirect reached disallowed host: {final_host}")

        body = bytearray()
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Kearny response exceeded {MAX_RESPONSE_BYTES} bytes")
        text = bytes(body).decode(response.encoding or "utf-8", errors="replace")

        if response.status_code in {401, 403, 429} or _is_managed_challenge(
            response.status_code, text
        ):
            logger.warning("health_empty_kind=source_blocked")
            logger.warning(
                "Kearny official Municode source blocked: status=%s final_url=%s "
                "failure_shape=honest-empty missing_scope=current_council_meetings",
                response.status_code,
                response.url,
            )
            return None

        response.raise_for_status()
        return text


def _build_window_url(source_url: str, floor: date, upper: date) -> str:
    origin = f"https://{_host(source_url)}"
    params = {
        "date_filter[value][month]": str(floor.month),
        "date_filter[value][day]": str(floor.day),
        "date_filter[value][year]": str(floor.year),
        "date_filter_1[value][month]": str(upper.month),
        "date_filter_1[value][day]": str(upper.day),
        "date_filter_1[value][year]": str(upper.year),
        "field_microsite_tid_selective": "All",
    }
    return f"{origin}/meetings3?{urlencode(params)}"


def _parse_html(
    html: str,
    source_url: str,
    floor: date,
    upper: date,
) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    table = _validate_surface(soup, floor, upper)
    if table is None:
        logger.info(
            "Kearny explicit official empty state witnessed for %s through %s",
            floor,
            upper,
        )
        return []

    if soup.select_one(".pager a"):
        raise ValueError(
            "Kearny bounded one-year result unexpectedly paginated; "
            "refusing an incomplete or unbounded crawl"
        )

    headers = tuple(_clean_text(cell) for cell in table.select("thead th"))
    if headers != EXPECTED_HEADERS:
        raise ValueError(f"Kearny meeting-table headers drifted: {headers!r}")
    header_map = {heading: index for index, heading in enumerate(headers)}

    meetings: list[dict[str, str]] = []
    rows_seen = dropped = 0
    for index, row in enumerate(table.select("tbody tr"), start=1):
        rows_seen += 1
        cells = [cell for cell in row.find_all("td", recursive=False) if isinstance(cell, Tag)]
        if len(cells) != len(EXPECTED_HEADERS):
            raise ValueError(
                f"Kearny row {index} column count drifted: {len(cells)} "
                f"expected={len(EXPECTED_HEADERS)}"
            )

        date_text = _clean_text(cells[header_map["Date"]])
        meeting_date = _meeting_date(date_text, index)
        parsed_date = date.fromisoformat(meeting_date)
        if not floor <= parsed_date <= upper:
            raise ValueError(
                "Kearny echoed date filter returned an out-of-window row: "
                f"row={index} date={meeting_date} floor={floor} upper={upper}"
            )

        title = _clean_text(cells[header_map["Meeting"]])
        if NON_COUNCIL_RE.search(title):
            dropped += 1
            logger.warning(
                "Kearny row dropped: row=%d date=%s title=%r reason=explicit_non_council_body",
                index,
                meeting_date,
                title,
            )
            continue
        if not COUNCIL_RE.search(title):
            raise ValueError(
                f"Kearny current meeting row is governing-body ambiguous: row={index} title={title!r}"
            )

        agenda_url = _cell_url(cells[header_map["Agenda"]], source_url, "agenda_url", index)
        agenda_packet_url = _cell_url(
            cells[header_map["Agenda Packet"]],
            source_url,
            "agenda_packet_url",
            index,
        )
        minutes_url = _cell_url(cells[header_map["Minutes"]], source_url, "minutes_url", index)
        video_url = _cell_url(cells[header_map["Video"]], source_url, "video_url", index)
        detail_url = _cell_url(cells[header_map["View"]], source_url, "detail_url", index)
        meeting_id = _meeting_id(detail_url, index)
        meeting = {
            "meeting_title": title,
            "meeting_date": meeting_date,
            "meeting_time": _meeting_time(date_text, index),
            "meeting_location": "",
            "meeting_status": _status(title, agenda_url, agenda_packet_url, minutes_url),
            "agenda_url": agenda_url,
            "minutes_url": minutes_url,
            "video_url": video_url,
            "agenda_packet_url": agenda_packet_url,
            "ecomment_url": "",
            "meeting_id": meeting_id,
        }
        meetings.append({field: meeting[field] for field in CANONICAL_FIELDS})

    logger.warning(
        "Kearny field absence: meeting_location,ecomment_url "
        "lack per-row columns on the official Municode list"
    )
    logger.info(
        "Kearny source summary: rows_seen=%d rows_dropped=%d accepted=%d",
        rows_seen,
        dropped,
        len(meetings),
    )
    return meetings


def _validate_surface(
    soup: BeautifulSoup,
    floor: date,
    upper: date,
) -> Tag | None:
    page_title = _clean_text(soup.title)
    form = soup.select_one("form#views-exposed-form-calendar-page-6")
    if "City of Kearny Arizona Meetings" not in page_title or not isinstance(form, Tag):
        raise ValueError(
            "Kearny Municode fingerprint drifted: "
            f"title={page_title!r} form_present={isinstance(form, Tag)}"
        )

    expected = {
        "date_filter[value][month]": str(floor.month),
        "date_filter[value][day]": str(floor.day),
        "date_filter[value][year]": str(floor.year),
        "date_filter_1[value][month]": str(upper.month),
        "date_filter_1[value][day]": str(upper.day),
        "date_filter_1[value][year]": str(upper.year),
        "field_microsite_tid_selective": "All",
    }
    observed = {name: _selected_value(form, name) for name in expected}
    if observed != expected:
        raise ValueError(
            "Kearny Municode date filter was not applied: "
            f"expected={expected!r} observed={observed!r}"
        )

    table = soup.select_one("table.views-table")
    empty_witness = any(
        _clean_text(paragraph).casefold()
        == "there are no meetings that match this criteria."
        for paragraph in soup.find_all("p")
    )
    if not isinstance(table, Tag) and not empty_witness:
        raise ValueError("Kearny meeting table is absent without the official empty-state witness")
    if isinstance(table, Tag) and empty_witness:
        raise ValueError("Kearny page exposes both meeting rows and an empty-state witness")

    logger.info(
        "Kearny Municode fingerprint witnessed: title=%r filters=%r table_present=%s empty=%s",
        page_title,
        observed,
        isinstance(table, Tag),
        empty_witness,
    )
    return table if isinstance(table, Tag) else None


def _selected_value(form: Tag, name: str) -> str:
    select = form.find("select", attrs={"name": name})
    if not isinstance(select, Tag):
        return ""
    selected = select.find("option", selected=True)
    return str(selected.get("value", "")) if isinstance(selected, Tag) else ""


def _meeting_date(text: str, row_index: int) -> str:
    match = DATE_RE.search(text[:300])
    if not match:
        raise ValueError(f"Kearny row {row_index} date signal drifted: {text!r}")
    try:
        return date(int(match.group(3)), int(match.group(1)), int(match.group(2))).isoformat()
    except ValueError as exc:
        raise ValueError(f"Kearny row {row_index} date is invalid: {match.group(0)!r}") from exc


def _meeting_time(text: str, row_index: int) -> str:
    match = TIME_RE.search(text[:300])
    if not match:
        logger.warning(
            "Kearny meeting_time absent: row=%d reason=no_visible_time text=%r",
            row_index,
            text[:200],
        )
        return ""
    return f"{int(match.group(1))}:{match.group(2) or '00'} {match.group(3).upper()}M"


def _cell_url(cell: Tag, base_url: str, field: str, row_index: int) -> str:
    emitted: list[str] = []
    for anchor in cell.find_all("a", href=True):
        candidate = _emit_url(str(anchor.get("href", "")), base_url, field, row_index)
        if candidate and candidate not in emitted:
            emitted.append(candidate)
    if not emitted:
        text = _clean_text(cell)
        if text:
            logger.warning(
                "Kearny URL field dropped: row=%d field=%s text=%r reason=text_without_allowed_link",
                row_index,
                field,
                text[:200],
            )
        return ""
    if len(emitted) > 1:
        logger.warning(
            "Kearny alternate URLs not represented: row=%d field=%s kept=%s dropped=%s",
            row_index,
            field,
            emitted[0],
            emitted[1:],
        )
    return emitted[0]


def _emit_url(href: str, base_url: str, field: str, row_index: int) -> str:
    candidate = urljoin(base_url, href.strip())
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or _host(candidate) not in EMIT_HOSTS:
        logger.warning(
            "Kearny URL dropped: row=%d field=%s href=%r reason=scheme_or_host_not_allowlisted",
            row_index,
            field,
            href,
        )
        return ""
    return candidate


def _meeting_id(detail_url: str, row_index: int) -> str:
    if not detail_url:
        logger.info("Kearny meeting_id absent: row=%d reason=no_detail_url", row_index)
        return ""
    slug = urlparse(detail_url).path.rstrip("/").split("/")[-1]
    match = re.search(r"-(\d+)$", slug)
    if match:
        return match.group(1)
    logger.warning(
        "Kearny meeting_id absent: row=%d detail_url=%s reason=numeric_slug_suffix_missing",
        row_index,
        detail_url,
    )
    return ""


def _status(
    title: str,
    agenda_url: str,
    agenda_packet_url: str,
    minutes_url: str,
) -> str:
    if CANCELLED_RE.search(title):
        return "Cancelled"
    if minutes_url:
        return "Minutes Available"
    if agenda_url or agenda_packet_url:
        return "Agenda Available"
    return "Scheduled"


def _validate_source_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or _host(url) not in FETCH_HOSTS:
        raise ValueError("Kearny source URL must use HTTPS on the official Municode host")
    return url


def _is_managed_challenge(status_code: int, text: str) -> bool:
    lowered = text.casefold()
    return status_code in {403, 503} and (
        "just a moment" in lowered
        or "challenges.cloudflare.com" in lowered
        or "access denied" in lowered
    )


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _clean_text(value: object) -> str:
    return " ".join(BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True).split())


def _assert_schema(meetings: list[dict[str, str]]) -> None:
    for index, meeting in enumerate(meetings):
        if tuple(meeting) != CANONICAL_FIELDS:
            raise ValueError(f"Kearny row {index} schema mismatch: {tuple(meeting)}")
        if any(not isinstance(value, str) for value in meeting.values()):
            raise ValueError(f"Kearny row {index} contains a non-string value")
