"""Bounded current-window parser for Peoria's official NovusAGENDA portal."""

from __future__ import annotations

import html as html_lib
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from polite_http import make_session


logger = logging.getLogger(__name__)

DEFAULT_URL = "https://peoriaaz.novusagenda.com/agendapublic/meetingsgeneral.aspx"
FETCH_HOSTS = {"peoriaaz.novusagenda.com"}
EMIT_HOSTS = FETCH_HOSTS
MAX_RESPONSE_BYTES = 2_000_000
MAX_PAGE_RESPONSES = 8
MAX_MEETINGS = 80
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
    "Meeting Date",
    "Meeting Type",
    "Meeting Location",
    "Online Agenda",
    "Download Agenda",
    "Minutes Recap",
    "Legal Minutes",
)
COUNCIL_ROW_RE = re.compile(
    r"\b(?:city\s+council(?:\s+meeting)?|regular\s+meeting|special\s+meeting|"
    r"study\s+session|work\s+session|executive\s+session|joint\s+meeting)\b",
    re.IGNORECASE,
)
NON_COUNCIL_RE = re.compile(
    r"\b(?:planning\s+(?:and\s+zoning\s+)?commission|board\s+of\s+adjustment|"
    r"parks?\s+(?:and\s+recreation\s+)?board|library\s+board)\b",
    re.IGNORECASE,
)
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)


def scrape_calendar(url: str | None = None) -> list[dict[str, str]]:
    """Return Peoria City Council meetings in a rolling one-year window."""
    source_url = _validate_source_url(url or DEFAULT_URL)
    floor = date.today().replace(day=1)
    upper = floor.replace(year=floor.year + 1) - timedelta(days=1)
    target = _build_window_url(source_url, floor, upper)

    all_meetings: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    with make_session() as session:
        page = _request_html_bounded(session, "GET", target)
        if page is None:
            return []

        for page_index in range(1, MAX_PAGE_RESPONSES + 1):
            page_html, page_url = page
            soup = BeautifulSoup(page_html, "html.parser")
            rows, explicit_empty = _validate_surface(soup, floor, upper)
            if explicit_empty and page_index != 1:
                raise ValueError("Peoria pagination reached an unexpected empty page")

            page_meetings = _parse_rows(rows, page_url, floor, upper)
            new_count = 0
            for meeting in page_meetings:
                key = (
                    meeting["meeting_id"],
                    meeting["meeting_date"],
                    meeting["meeting_title"].casefold(),
                    meeting["meeting_location"].casefold(),
                )
                if key in seen:
                    logger.warning(
                        "Peoria duplicate row dropped: page=%d date=%s title=%r location=%r",
                        page_index,
                        meeting["meeting_date"],
                        meeting["meeting_title"],
                        meeting["meeting_location"],
                    )
                    continue
                seen.add(key)
                all_meetings.append(meeting)
                new_count += 1
                if len(all_meetings) > MAX_MEETINGS:
                    raise ValueError(
                        f"Peoria bounded current window exceeded {MAX_MEETINGS} meetings"
                    )

            next_postback = _next_page_postback(soup)
            if not next_postback:
                break
            if explicit_empty:
                raise ValueError("Peoria empty grid unexpectedly exposes a next-page control")
            if new_count == 0:
                raise ValueError("Peoria NovusAGENDA pager cycled without new rows")
            if page_index == MAX_PAGE_RESPONSES:
                raise ValueError(
                    f"Peoria pagination exceeded the hard cap of {MAX_PAGE_RESPONSES} responses"
                )

            event_target, event_argument = next_postback
            payload, post_url = _postback_payload(
                soup,
                page_url,
                event_target,
                event_argument,
            )
            page = _request_html_bounded(
                session,
                "POST",
                post_url,
                data=payload,
            )
            if page is None:
                return []

    _assert_schema(all_meetings)
    if not all_meetings:
        logger.warning("health_empty_kind=confirmed_empty")
    logger.warning(
        "Peoria field absence: meeting_time,video_url,agenda_packet_url,ecomment_url "
        "lack per-row signals on the NovusAGENDA meeting grid"
    )
    logger.info(
        "Peoria scrape complete: current_window=%d floor=%s upper=%s page_cap=%d",
        len(all_meetings),
        floor.isoformat(),
        upper.isoformat(),
        MAX_PAGE_RESPONSES,
    )
    return all_meetings


def _request_html_bounded(
    session: Any,
    method: str,
    url: str,
    *,
    data: dict[str, str] | None = None,
) -> tuple[str, str] | None:
    with session.request(
        method,
        url,
        data=data,
        timeout=35,
        stream=True,
        allow_redirects=True,
    ) as response:
        final_host = _host(response.url)
        if final_host not in FETCH_HOSTS:
            raise ValueError(f"Peoria redirect reached disallowed host: {final_host}")

        body = bytearray()
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Peoria response exceeded {MAX_RESPONSE_BYTES} bytes")
        text = bytes(body).decode(response.encoding or "utf-8", errors="replace")

        if response.status_code in {401, 403, 429} or _is_managed_challenge(
            response.status_code, text
        ):
            logger.warning("health_empty_kind=source_blocked")
            logger.warning(
                "Peoria official NovusAGENDA source blocked: status=%s final_url=%s "
                "failure_shape=honest-empty missing_scope=current_city_council_meetings",
                response.status_code,
                response.url,
            )
            return None

        response.raise_for_status()
        return text, response.url


def _build_window_url(source_url: str, floor: date, upper: date) -> str:
    origin = f"https://{_host(source_url)}"
    params = {
        "meetingtype": "1",
        "Date": "cus",
        "From": floor.strftime("%m/%d/%Y"),
        "To": upper.strftime("%m/%d/%Y"),
    }
    return f"{origin}/agendapublic/meetingsgeneral.aspx?{urlencode(params)}"


def _validate_surface(
    soup: BeautifulSoup,
    floor: date,
    upper: date,
) -> tuple[list[Tag], bool]:
    page_title = _clean_text(soup.title)
    form = soup.select_one("form#form1")
    grid = soup.select_one("#SearchAgendasMeetings_radGridMeetings_ctl00")
    if (
        page_title != "NovusAGENDA"
        or not isinstance(form, Tag)
        or not isinstance(grid, Tag)
    ):
        raise ValueError(
            "Peoria NovusAGENDA fingerprint drifted: "
            f"title={page_title!r} form_present={isinstance(form, Tag)} "
            f"grid_present={isinstance(grid, Tag)}"
        )

    date_range = _selected_option(form, "SearchAgendasMeetings$ddlDateRange")
    meeting_type = _selected_option(form, "SearchAgendasMeetings$ctl00")
    observed_from = _input_value(form, "SearchAgendasMeetings$radCalendarFrom")
    observed_to = _input_value(form, "SearchAgendasMeetings$radCalendarTo")
    expected_from = floor.isoformat()
    expected_to = upper.isoformat()
    if (
        date_range != ("cus", "Custom Date Range")
        or meeting_type != ("1", "City Council Meetings")
        or observed_from != expected_from
        or observed_to != expected_to
    ):
        raise ValueError(
            "Peoria current City Council filter was not applied: "
            f"date_range={date_range!r} meeting_type={meeting_type!r} "
            f"from={observed_from!r} to={observed_to!r} "
            f"expected_from={expected_from!r} expected_to={expected_to!r}"
        )

    headers = tuple(_clean_text(cell) for cell in grid.select("thead th"))
    if headers != EXPECTED_HEADERS:
        raise ValueError(f"Peoria meeting-grid headers drifted: {headers!r}")

    rows = [row for row in grid.select("tbody > tr") if isinstance(row, Tag)]
    explicit_empty = (
        len(rows) == 1
        and _clean_text(rows[0]).casefold() == "no records to display."
    )
    if explicit_empty:
        rows = []
    elif any(len(row.find_all("td", recursive=False)) == 1 for row in rows):
        raise ValueError("Peoria meeting grid contains an unknown single-cell state row")

    logger.info(
        "Peoria NovusAGENDA fingerprint witnessed: city_council_filter=1 "
        "from=%s to=%s rows=%d explicit_empty=%s",
        observed_from,
        observed_to,
        len(rows),
        explicit_empty,
    )
    return rows, explicit_empty


def _parse_rows(
    rows: list[Tag],
    base_url: str,
    floor: date,
    upper: date,
) -> list[dict[str, str]]:
    meetings: list[dict[str, str]] = []
    for index, row in enumerate(rows, start=1):
        cells = [cell for cell in row.find_all("td", recursive=False) if isinstance(cell, Tag)]
        if len(cells) != len(EXPECTED_HEADERS):
            raise ValueError(
                f"Peoria row {index} column count drifted: {len(cells)} "
                f"expected={len(EXPECTED_HEADERS)}"
            )

        meeting_date = _meeting_date(_clean_text(cells[0]), index)
        parsed_date = date.fromisoformat(meeting_date)
        if not floor <= parsed_date <= upper:
            raise ValueError(
                "Peoria echoed date filter returned an out-of-window row: "
                f"row={index} date={meeting_date} floor={floor} upper={upper}"
            )

        title = _clean_text(cells[1])
        if NON_COUNCIL_RE.search(title):
            logger.warning(
                "Peoria row dropped: row=%d date=%s title=%r reason=explicit_non_council_body",
                index,
                meeting_date,
                title,
            )
            continue
        if not COUNCIL_ROW_RE.search(title):
            raise ValueError(
                f"Peoria City Council row title vocabulary drifted: row={index} title={title!r}"
            )

        online_agendas = _cell_urls(cells[3], base_url, "online_agenda", index)
        download_agendas = _cell_urls(cells[4], base_url, "download_agenda", index)
        agenda_url = _preferred_url(
            online_agendas,
            download_agendas,
            "agenda_url",
            index,
        )
        recap_urls = _cell_urls(cells[5], base_url, "minutes_recap", index)
        legal_minutes_urls = _cell_urls(cells[6], base_url, "legal_minutes", index)
        minutes_url = _preferred_url(
            legal_minutes_urls,
            recap_urls,
            "minutes_url",
            index,
        )
        meeting_id = _meeting_id(
            online_agendas + download_agendas + legal_minutes_urls + recap_urls,
            index,
        )
        location = _clean_text(cells[2])
        meeting = {
            "meeting_title": title,
            "meeting_date": meeting_date,
            "meeting_time": "",
            "meeting_location": location,
            "meeting_status": _status(title, agenda_url, minutes_url),
            "agenda_url": agenda_url,
            "minutes_url": minutes_url,
            "video_url": "",
            "agenda_packet_url": "",
            "ecomment_url": "",
            "meeting_id": meeting_id,
        }
        meetings.append({field: meeting[field] for field in CANONICAL_FIELDS})
    return meetings


def _selected_option(form: Tag, name: str) -> tuple[str, str]:
    select = form.find("select", attrs={"name": name})
    if not isinstance(select, Tag):
        return "", ""
    option = select.find("option", selected=True)
    if not isinstance(option, Tag):
        return "", ""
    return str(option.get("value", "")), _clean_text(option)


def _input_value(form: Tag, name: str) -> str:
    field = form.find("input", attrs={"name": name})
    return str(field.get("value", "")) if isinstance(field, Tag) else ""


def _meeting_date(text: str, row_index: int) -> str:
    for pattern in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Peoria row {row_index} date signal drifted: {text!r}")


def _cell_urls(
    cell: Tag,
    base_url: str,
    field: str,
    row_index: int,
) -> list[str]:
    emitted: list[str] = []
    for anchor in cell.find_all("a", href=True):
        candidate = _emit_url(str(anchor.get("href", "")), base_url, field, row_index)
        if candidate and candidate not in emitted:
            emitted.append(candidate)
    if not emitted:
        text = _clean_text(cell)
        if text:
            logger.warning(
                "Peoria URL field dropped: row=%d field=%s text=%r reason=text_without_allowed_link",
                row_index,
                field,
                text[:200],
            )
    return emitted


def _preferred_url(
    preferred: list[str],
    fallback: list[str],
    field: str,
    row_index: int,
) -> str:
    candidates = preferred + [url for url in fallback if url not in preferred]
    if not candidates:
        return ""
    if len(candidates) > 1:
        logger.warning(
            "Peoria alternate URLs not represented: row=%d field=%s kept=%s dropped=%s",
            row_index,
            field,
            candidates[0],
            candidates[1:],
        )
    return candidates[0]


def _emit_url(href: str, base_url: str, field: str, row_index: int) -> str:
    candidate = urljoin(base_url, href.strip())
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or _host(candidate) not in EMIT_HOSTS:
        logger.warning(
            "Peoria URL dropped: row=%d field=%s href=%r reason=scheme_or_host_not_allowlisted",
            row_index,
            field,
            href,
        )
        return ""
    return candidate


def _meeting_id(urls: list[str], row_index: int) -> str:
    for url in urls:
        query = parse_qs(urlparse(url).query)
        for key in ("MeetingID", "meetingid", "ID", "id"):
            values = query.get(key)
            if values and values[0]:
                return values[0]
    logger.warning("Peoria meeting_id absent: row=%d reason=no_vendor_id_in_document_urls", row_index)
    return ""


def _status(title: str, agenda_url: str, minutes_url: str) -> str:
    if CANCELLED_RE.search(title):
        return "Cancelled"
    if minutes_url:
        return "Minutes Available"
    if agenda_url:
        return "Agenda Available"
    return "Scheduled"


def _next_page_postback(soup: BeautifulSoup) -> tuple[str, str] | None:
    anchor = soup.find("a", attrs={"title": "Next Page"})
    if not isinstance(anchor, Tag):
        return None
    href = html_lib.unescape(str(anchor.get("href", "")))
    match = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", href)
    if not match:
        raise ValueError(f"Peoria next-page postback shape drifted: {href!r}")
    return match.group(1), match.group(2)


def _postback_payload(
    soup: BeautifulSoup,
    page_url: str,
    event_target: str,
    event_argument: str,
) -> tuple[dict[str, str], str]:
    form = soup.select_one("form#form1")
    if not isinstance(form, Tag):
        raise ValueError("Peoria postback form disappeared")

    payload: dict[str, str] = {}
    for field in form.find_all(["input", "textarea", "select"]):
        name = str(field.get("name", ""))
        if not name:
            continue
        if field.name == "input":
            if str(field.get("type", "")).casefold() in {"button", "submit", "image"}:
                continue
            payload[name] = str(field.get("value", "") or "")
        elif field.name == "textarea":
            payload[name] = field.get_text()
        else:
            selected = field.find("option", selected=True)
            payload[name] = str(selected.get("value", "")) if isinstance(selected, Tag) else ""

    payload["__EVENTTARGET"] = event_target
    payload["__EVENTARGUMENT"] = event_argument
    action = str(form.get("action", "") or "")
    post_url = urljoin(page_url, action)
    if _host(post_url) not in FETCH_HOSTS:
        raise ValueError(f"Peoria postback action reached disallowed host: {_host(post_url)}")
    return payload, post_url


def _validate_source_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or _host(url) not in FETCH_HOSTS:
        raise ValueError("Peoria source URL must use HTTPS on the official NovusAGENDA host")
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
            raise ValueError(f"Peoria row {index} schema mismatch: {tuple(meeting)}")
        if any(not isinstance(value, str) for value in meeting.values()):
            raise ValueError(f"Peoria row {index} contains a non-string value")
