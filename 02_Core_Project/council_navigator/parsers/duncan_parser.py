"""Current-window parser for the Town of Duncan council source."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from polite_http import make_session


logger = logging.getLogger(__name__)

DEFAULT_URL = "https://duncanaz.us/home/government/town-meeting-agendas-minutes/"
FETCH_HOSTS = {"duncanaz.us", "www.duncanaz.us"}
EMIT_HOSTS = FETCH_HOSTS
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
ARCHIVE_DATE_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
NON_COUNCIL_RE = re.compile(
    r"\b(?:planning\s+(?:and\s+zoning\s+)?commission|board\s+of\s+adjustment|"
    r"fire\s+district|school\s+board)\b",
    re.IGNORECASE,
)
ANNOUNCEMENT_RE = re.compile(
    r"(?P<title>(?:combined\s+public\s+hearing\s*(?:&|and)\s*)?"
    r"(?:town|city)\s+council\s+meeting)"
    r"\s*-\s*(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:,\s*(?P<year>\d{4}))?\s*@\s*(?P<hour>\d{1,2})"
    r"(?::(?P<minute>\d{2}))?\s*(?P<ampm>[AP])\.?\s*M\.?",
    re.IGNORECASE,
)


def scrape_calendar(url: str | None = None) -> list[dict[str, str]]:
    """Return Duncan governing-body meetings from this calendar month forward."""
    target = _validate_source_url(url or DEFAULT_URL)
    floor = date.today().replace(day=1)

    with make_session() as session:
        html = _fetch_html_bounded(session, target)
    if html is None:
        return []

    meetings = _parse_html(html, target, floor)
    _assert_schema(meetings)
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
    logger.info(
        "Duncan scrape complete: current_month_forward=%d floor=%s",
        len(meetings),
        floor.isoformat(),
    )
    return meetings


def _fetch_html_bounded(session: Any, url: str) -> str | None:
    with session.get(url, timeout=35, stream=True, allow_redirects=True) as response:
        final_host = _host(response.url)
        if final_host not in FETCH_HOSTS:
            raise ValueError(f"Duncan redirect reached disallowed host: {final_host}")

        body = bytearray()
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Duncan response exceeded {MAX_RESPONSE_BYTES} bytes")
        text = bytes(body).decode(response.encoding or "utf-8", errors="replace")

        if response.status_code in {401, 403, 429} or _is_managed_challenge(
            response.status_code, text
        ):
            logger.warning("health_empty_kind=source_blocked")
            logger.warning(
                "Duncan official source blocked: status=%s final_url=%s "
                "failure_shape=honest-empty missing_scope=current_council_meetings",
                response.status_code,
                response.url,
            )
            return None

        response.raise_for_status()
        return text


def _parse_html(
    html: str,
    source_url: str,
    floor: date,
) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    _validate_surface(soup)

    records: dict[tuple[str, str], dict[str, str]] = {}
    rows_seen = historical = dropped = 0
    upper = floor.replace(year=floor.year + 1) - timedelta(days=1)

    for anchor in soup.find_all("a", href=True):
        label = _clean_text(anchor)
        date_match = ARCHIVE_DATE_RE.search(label)
        if not date_match:
            continue
        if not re.search(r"\b(?:agenda|packet|minutes?|notice)\b", label, re.IGNORECASE):
            continue
        rows_seen += 1
        meeting_date = _archive_date(date_match, label)
        parsed_date = date.fromisoformat(meeting_date)
        if parsed_date < floor:
            historical += 1
            continue
        if parsed_date > upper:
            dropped += 1
            logger.warning(
                "Duncan row dropped: date=%s label=%r reason=implausible_beyond_rolling_window "
                "upper=%s",
                meeting_date,
                label,
                upper,
            )
            continue

        title = _governing_title(label)
        if not title:
            dropped += 1
            logger.warning(
                "Duncan row dropped: date=%s label=%r reason=not_governing_body",
                meeting_date,
                label,
            )
            continue

        field = _document_field(label)
        if not field:
            raise ValueError(f"Duncan current document vocabulary drifted: {label!r}")
        emitted_url = _emit_url(str(anchor.get("href", "")), source_url, field, label)
        if not emitted_url:
            dropped += 1
            continue

        key = (meeting_date, title.casefold())
        record = records.setdefault(key, _new_record(title, meeting_date))
        if record[field] and record[field] != emitted_url:
            logger.warning(
                "Duncan duplicate document dropped: date=%s field=%s kept=%s dropped=%s",
                meeting_date,
                field,
                record[field],
                emitted_url,
            )
        else:
            record[field] = emitted_url

    announcement_count = _add_current_announcements(soup, floor, upper, records)
    meetings = sorted(records.values(), key=lambda row: (row["meeting_date"], row["meeting_title"]))
    for record in meetings:
        record["meeting_status"] = _status(record)

    logger.warning(
        "Duncan field absence: meeting_location,video_url,ecomment_url,meeting_id "
        "lack per-row signals on the official source"
    )
    logger.info(
        "Duncan source summary: archive_rows_seen=%d historical_ignored=%d "
        "rows_dropped=%d current_announcements=%d accepted=%d",
        rows_seen,
        historical,
        dropped,
        announcement_count,
        len(meetings),
    )
    return meetings


def _validate_surface(soup: BeautifulSoup) -> None:
    page_title = _clean_text(soup.title)
    body = _clean_text(soup)
    dated_documents = [
        anchor
        for anchor in soup.find_all("a", href=True)
        if ARCHIVE_DATE_RE.search(_clean_text(anchor))
        and re.search(r"\b(?:agenda|packet|minutes?)\b", _clean_text(anchor), re.IGNORECASE)
    ]
    if (
        "Town Meeting Agendas and Minutes" not in page_title
        or "MEETING AGENDAS AND MINUTES" not in body
        or not dated_documents
    ):
        raise ValueError(
            "Duncan official archive fingerprint drifted: "
            f"title={page_title!r} dated_document_count={len(dated_documents)}"
        )
    logger.info(
        "Duncan fingerprint witnessed: page_title=%r dated_document_count=%d",
        page_title,
        len(dated_documents),
    )


def _add_current_announcements(
    soup: BeautifulSoup,
    floor: date,
    upper: date,
    records: dict[tuple[str, str], dict[str, str]],
) -> int:
    accepted = 0
    for span in soup.select(".hsas-widget span"):
        text = _clean_text(span).strip("| ")
        if not re.search(r"\b(?:town|city)\s+council\b", text, re.IGNORECASE):
            continue
        match = ANNOUNCEMENT_RE.search(text)
        if not match:
            raise ValueError(f"Duncan current council announcement drifted: {text!r}")

        month = datetime.strptime(match.group("month"), "%B").month
        explicit_year = match.group("year")
        if explicit_year:
            year = int(explicit_year)
        elif month == floor.month:
            year = floor.year
            logger.info(
                "Duncan announcement year resolved from active current-month context: text=%r year=%d",
                text,
                year,
            )
        else:
            raise ValueError(
                "Duncan council announcement omits its year outside the current month: "
                f"{text!r}"
            )
        try:
            event_date = date(year, month, int(match.group("day")))
        except ValueError as exc:
            raise ValueError(f"Duncan announcement date is invalid: {text!r}") from exc
        if event_date < floor:
            logger.warning(
                "Duncan announcement dropped: text=%r reason=historical_before_floor floor=%s",
                text,
                floor,
            )
            continue
        if event_date > upper:
            raise ValueError(
                "Duncan current council announcement falls beyond the rolling source window: "
                f"text={text!r} upper={upper}"
            )

        hour = int(match.group("hour"))
        minute = int(match.group("minute") or "0")
        if not 1 <= hour <= 12 or not 0 <= minute <= 59:
            raise ValueError(f"Duncan announcement time is invalid: {text!r}")
        meeting_time = f"{hour}:{minute:02d} {match.group('ampm').upper()}M"
        title = " ".join(match.group("title").replace("&", "and").split())
        key = (event_date.isoformat(), title.casefold())
        record = records.setdefault(key, _new_record(title, event_date.isoformat()))
        record["meeting_time"] = meeting_time
        accepted += 1
        logger.info(
            "Duncan current announcement accepted: date=%s title=%r time=%s",
            event_date.isoformat(),
            title,
            meeting_time,
        )
    return accepted


def _archive_date(match: re.Match[str], label: str) -> str:
    month, day, year = map(int, match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError as exc:
        raise ValueError(f"Duncan archive date is invalid: {label!r}") from exc


def _governing_title(label: str) -> str:
    lowered = label.casefold()
    if NON_COUNCIL_RE.search(label):
        return ""
    if "combined public hearing" in lowered:
        if "special meeting" in lowered:
            return "Combined Public Hearing and Town Council Special Meeting"
        if "regular meeting" in lowered:
            return "Combined Public Hearing and Town Council Regular Meeting"
        if re.search(r"\b(?:town|city)\s+council\s+meeting\b", label, re.IGNORECASE):
            return "Combined Public Hearing and Town Council Meeting"
        raise ValueError(f"Duncan combined-hearing row lacks governing meeting type: {label!r}")
    if "work session" in lowered or "worksession" in lowered or "workshop" in lowered:
        return "Town Council Work Session"
    if "emergency meeting" in lowered:
        return "Town Council Emergency Meeting"
    if "special meeting" in lowered:
        return "Town Council Special Meeting"
    if "regular meeting" in lowered:
        return "Town Council Regular Meeting"
    if re.search(r"\b(?:town|city)\s+council\s+meeting\b", label, re.IGNORECASE):
        return "Town Council Meeting"
    if "public hearing" in lowered:
        return ""
    if "meeting" in lowered:
        raise ValueError(f"Duncan current meeting row is governing-body ambiguous: {label!r}")
    return ""


def _document_field(label: str) -> str:
    lowered = label.casefold()
    if "minutes" in lowered:
        return "minutes_url"
    if "agenda packet" in lowered or "meeting packet" in lowered:
        return "agenda_packet_url"
    if "agenda" in lowered or "notice of special meeting" in lowered:
        return "agenda_url"
    return ""


def _new_record(title: str, meeting_date: str) -> dict[str, str]:
    return {
        "meeting_title": title,
        "meeting_date": meeting_date,
        "meeting_time": "",
        "meeting_location": "",
        "meeting_status": "Scheduled",
        "agenda_url": "",
        "minutes_url": "",
        "video_url": "",
        "agenda_packet_url": "",
        "ecomment_url": "",
        "meeting_id": "",
    }


def _status(record: dict[str, str]) -> str:
    if CANCELLED_RE.search(record["meeting_title"]):
        return "Cancelled"
    if record["minutes_url"]:
        return "Minutes Available"
    if record["agenda_url"] or record["agenda_packet_url"]:
        return "Agenda Available"
    return "Scheduled"


def _emit_url(href: str, base_url: str, field: str, label: str) -> str:
    candidate = urljoin(base_url, href.strip())
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or _host(candidate) not in EMIT_HOSTS:
        logger.warning(
            "Duncan URL dropped: field=%s label=%r href=%r reason=scheme_or_host_not_allowlisted",
            field,
            label,
            href,
        )
        return ""
    return candidate


def _validate_source_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or _host(url) not in FETCH_HOSTS:
        raise ValueError("Duncan source URL must use HTTPS on the official town host")
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
            raise ValueError(f"Duncan row {index} schema mismatch: {tuple(meeting)}")
        if any(not isinstance(value, str) for value in meeting.values()):
            raise ValueError(f"Duncan row {index} contains a non-string value")
