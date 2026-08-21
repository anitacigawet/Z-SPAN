"""Somerton City Council meetings from the official CivicWeb portal."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import logging
import re
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from polite_http import make_session


logger = logging.getLogger(__name__)

DEFAULT_URL = "https://cityofsomerton.civicweb.net/Portal/MeetingTypeList.aspx"
ALLOWED_HOST = "cityofsomerton.civicweb.net"
ALLOWED_CATEGORIES = frozenset({"City Council", "Work Session", "Special Council Meeting"})
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
MAX_RESPONSE_BYTES = 4_000_000
MAX_DETAIL_REQUESTS = 12
REQUEST_TIMEOUT = 45
BLOCKING_HTTP_STATUSES = {401, 403, 407, 423, 429, 451}
DATE_RE = re.compile(r"^(?P<title>.+?)\s+-\s+(?P<day>\d{1,2}\s+[A-Za-z]{3}\s+\d{4})$")
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)


def scrape_calendar(url: str = DEFAULT_URL) -> list[dict[str, str]]:
    """Return Somerton council meetings from this calendar month forward."""
    _validate_url(url)
    list_url = DEFAULT_URL
    session = make_session()
    status, final_url, html = _fetch_bounded(session, list_url)
    if status in BLOCKING_HTTP_STATUSES:
        _source_blocked("meeting_type_list", status, final_url)
        return []
    if status != 200:
        raise RuntimeError(f"Somerton meeting list returned HTTP {status}: {final_url}")

    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1")
    if not isinstance(heading, Tag) or _clean(heading.get_text(" ", strip=True)) != "Meetings":
        raise RuntimeError("Somerton CivicWeb fingerprint drift: h1 Meetings missing")
    if "iCompass - A Diligent Brand" not in _clean(soup.get_text(" ", strip=True)):
        raise RuntimeError("Somerton CivicWeb fingerprint drift: iCompass marker missing")
    logger.info("Somerton vendor_fingerprint witness=h1_Meetings+iCompass_Diligent")
    logger.warning(
        "Somerton field_absence field=meeting_time reason=portal_list_has_no_per_row_time_signal"
    )
    logger.warning(
        "Somerton field_absence field=meeting_location reason=portal_list_has_no_per_row_location_signal"
    )
    logger.warning(
        "Somerton field_absence field=ecomment_url reason=portal_exposes_no_ecomment_signal"
    )

    cutoff = date.today().replace(day=1)
    counters: Counter[str] = Counter()
    candidates: list[tuple[str, str, date, str]] = []
    for category_heading in soup.find_all("h2"):
        category = _clean(category_heading.get_text(" ", strip=True))
        if not category:
            continue
        item = category_heading.find_parent("div", class_="item-template")
        listing = item.find("ol") if isinstance(item, Tag) else None
        if not isinstance(listing, Tag):
            continue
        category_links = [
            anchor
            for anchor in listing.find_all("a", href=True)
            if "MeetingInformation.aspx?Id=" in str(anchor.get("href", ""))
        ]
        if not category_links:
            continue
        if category not in ALLOWED_CATEGORIES:
            counters["subordinate_category_rows"] += len(category_links)
            logger.info(
                "Somerton category dropped: reason=not_council category=%r rows=%d",
                category,
                len(category_links),
            )
            continue
        for anchor in category_links:
            counters["council_rows_seen"] += 1
            label = _clean(anchor.get_text(" ", strip=True))
            match = DATE_RE.fullmatch(label)
            if not match:
                raise RuntimeError(
                    f"Somerton council label vocabulary drift: category={category!r} label={label!r}"
                )
            try:
                meeting_day = datetime.strptime(match.group("day"), "%d %b %Y").date()
            except ValueError as exc:
                raise RuntimeError(f"Somerton council label has invalid date: {label!r}") from exc
            if meeting_day < cutoff:
                counters["before_current_month"] += 1
                continue
            detail_url = _safe_url(str(anchor.get("href", "")), final_url, field="detail_url", label=label)
            if not detail_url:
                raise RuntimeError(f"Somerton current council row has no safe detail URL: {label!r}")
            meeting_id = _meeting_id(detail_url, label=label)
            candidates.append((category, match.group("title"), meeting_day, detail_url))
            logger.info(
                "Somerton candidate accepted: category=%r date=%s id=%s label=%r",
                category,
                meeting_day.isoformat(),
                meeting_id,
                label,
            )

    if len(candidates) > MAX_DETAIL_REQUESTS:
        raise RuntimeError(
            f"Somerton current council rows exceeded detail-request cap: "
            f"rows={len(candidates)} cap={MAX_DETAIL_REQUESTS}"
        )

    meetings: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for category, title, meeting_day, detail_url in candidates:
        meeting_id = _meeting_id(detail_url, label=title)
        if meeting_id in seen_ids:
            counters["duplicate_id"] += 1
            logger.warning("Somerton row dropped: reason=duplicate_id id=%s title=%r", meeting_id, title)
            continue
        seen_ids.add(meeting_id)
        detail_status, detail_final_url, detail_html = _fetch_bounded(session, detail_url)
        if detail_status in BLOCKING_HTTP_STATUSES:
            raise RuntimeError(
                f"Somerton detail page was blocked after list succeeded: "
                f"id={meeting_id} status={detail_status} url={detail_final_url}"
            )
        if detail_status != 200:
            raise RuntimeError(
                f"Somerton detail page returned HTTP {detail_status}: id={meeting_id} url={detail_final_url}"
            )
        detail = BeautifulSoup(detail_html, "html.parser")
        detail_heading = detail.find("h1")
        detail_category = _clean(detail_heading.get_text(" ", strip=True)) if isinstance(detail_heading, Tag) else ""
        if detail_category != category:
            raise RuntimeError(
                f"Somerton detail identity drift: id={meeting_id} "
                f"expected_category={category!r} actual={detail_category!r}"
            )
        agenda_url = _detail_agenda_url(detail, detail_final_url, title=title)
        video_url = _detail_optional_link(
            detail.find(id="ctl00_MainContent_VideoButton"),
            detail_final_url,
            title=title,
            field="video_url",
        )
        minutes_url = _detail_optional_link(
            detail.find(id="ExternalMinutesLink"),
            detail_final_url,
            title=title,
            field="minutes_url",
        )
        meeting = {
            "meeting_title": title,
            "meeting_date": meeting_day.isoformat(),
            "meeting_time": "",
            "meeting_location": "",
            "meeting_status": _status(title, agenda_url, minutes_url),
            "agenda_url": agenda_url,
            "minutes_url": minutes_url,
            "video_url": video_url,
            "agenda_packet_url": "",
            "ecomment_url": "",
            "meeting_id": meeting_id,
        }
        _validate_meeting(meeting, title=title)
        meetings.append(meeting)
        counters["rows_accepted"] += 1
        counters["meeting_time_absent_by_construction"] += 1
        counters["meeting_location_absent_by_construction"] += 1
        counters["ecomment_absent_by_construction"] += 1
        logger.info("Somerton meeting emitted: id=%s date=%s title=%r", meeting_id, meeting_day, title)

    meetings.sort(key=lambda item: (item["meeting_date"], item["meeting_title"], item["meeting_id"]))
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Somerton official CivicWeb list contained no current-month-forward council rows: stats=%s",
            dict(counters),
        )
    logger.warning("Somerton scrape summary: counters=%s", dict(counters))
    return meetings


def _fetch_bounded(session: Any, url: str) -> tuple[int, str, str]:
    with session.get(url, timeout=REQUEST_TIMEOUT, stream=True, allow_redirects=True) as response:
        final_host = (urlparse(response.url).hostname or "").lower()
        if final_host != ALLOWED_HOST:
            raise ValueError(f"Somerton redirect reached disallowed host: {final_host}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Somerton response exceeded {MAX_RESPONSE_BYTES} bytes: {url}")
        return (
            response.status_code,
            response.url,
            bytes(body).decode(response.encoding or "utf-8", errors="replace"),
        )


def _detail_agenda_url(detail: BeautifulSoup, base_url: str, *, title: str) -> str:
    agenda_button = detail.find(id="ctl00_MainContent_AgendaDocument")
    document = detail.find(id="ctl00_MainContent_DocumentPrintVersion")
    if not isinstance(document, Tag) or not document.get("href"):
        logger.info("Somerton agenda_url honest-empty: title=%r reason=no_document_print_link", title)
        return ""
    if not isinstance(agenda_button, Tag) or _fold(agenda_button.get_text(" ", strip=True)) != "agenda":
        raise RuntimeError(
            f"Somerton document print link lacked the Agenda type witness: title={title!r}"
        )
    return _safe_url(str(document.get("href", "")), base_url, field="agenda_url", label=title)


def _detail_optional_link(element: Tag | None, base_url: str, *, title: str, field: str) -> str:
    if not isinstance(element, Tag):
        logger.info("Somerton %s honest-empty: title=%r reason=element_missing", field, title)
        return ""
    href = str(element.get("href", "") or "")
    if not href:
        logger.info("Somerton %s honest-empty: title=%r reason=href_empty", field, title)
        return ""
    return _safe_url(href, base_url, field=field, label=title)


def _safe_url(raw: str, base_url: str, *, field: str, label: str) -> str:
    absolute = urljoin(base_url, raw.strip())
    parsed = urlparse(absolute)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != ALLOWED_HOST:
        logger.warning(
            "Somerton URL dropped: field=%s label=%r reason=scheme_or_host raw=%r absolute=%r",
            field,
            label,
            raw,
            absolute,
        )
        return ""
    return absolute


def _meeting_id(detail_url: str, *, label: str) -> str:
    values = parse_qs(urlparse(detail_url).query).get("Id", [])
    if len(values) != 1 or not values[0].isdigit():
        raise RuntimeError(f"Somerton detail URL has no stable numeric ID: label={label!r} url={detail_url!r}")
    return values[0]


def _status(title: str, agenda_url: str, minutes_url: str) -> str:
    if CANCELLED_RE.search(title):
        return "Cancelled"
    if minutes_url:
        return "Minutes Available"
    if agenda_url:
        return "Agenda Available"
    return "Scheduled"


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    allowed_paths = {"/portal/meetingtypelist.aspx", "/portal/meetingschedule.aspx"}
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != ALLOWED_HOST
        or parsed.path.casefold() not in allowed_paths
    ):
        raise ValueError(f"Somerton parser called with unexpected URL: {url!r}")


def _source_blocked(phase: str, status: int, final_url: str) -> None:
    logger.warning("health_empty_kind=source_blocked")
    logger.warning(
        "Somerton source_blocked phase=%s status=%d final_url=%s "
        "failure_shape=honest-empty missing_data_scope=all_current_month_forward_meetings",
        phase,
        status,
        final_url,
    )


def _validate_meeting(meeting: dict[str, str], *, title: str) -> None:
    if tuple(meeting) != CANONICAL_FIELDS:
        raise RuntimeError(f"Somerton schema mismatch: title={title!r} keys={tuple(meeting)!r}")
    if any(not isinstance(value, str) for value in meeting.values()):
        raise TypeError(f"Somerton row contains non-string values: title={title!r}")


def _fold(value: str) -> str:
    return _clean(value).casefold()


def _clean(value: str) -> str:
    text = BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


__all__ = ["scrape_calendar"]
