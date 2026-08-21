"""Wellton Town Council meetings from the official CivicEngage Agenda Center."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import html
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from polite_http import make_session


logger = logging.getLogger(__name__)

DEFAULT_URL = "https://www.welltonaz.gov/AgendaCenter"
ALLOWED_HOST = "www.welltonaz.gov"
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
MAX_RESPONSE_BYTES = 3_000_000
MAX_ROWS = 500
REQUEST_TIMEOUT = 45
BLOCKING_HTTP_STATUSES = {401, 403, 407, 423, 429, 451}
DATE_RE = re.compile(r"^([A-Za-z]{3}\s+\d{1,2},\s+\d{4})(?:\s|$)")
TIME_RE = re.compile(
    r"(?<!\d)(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([AP])\.?M\.?(?=\s|$|[^\w.])",
    re.IGNORECASE,
)
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
COUNCIL_RE = re.compile(r"\b(?:wellton\s+)?town\s+council\b", re.IGNORECASE)
EXCLUDED_RE = re.compile(r"\b(?:community\s+facilities\s+district|board|commission|committee)\b", re.IGNORECASE)
AGENDA_PATH = "/agendacenter/viewfile/agenda/"
MINUTES_PATH = "/agendacenter/viewfile/minutes/"
ID_RE = re.compile(r"/_?(\d{8}-\d+)(?:\?.*)?$")
BAD_SCHEMES = ("javascript:", "data:", "vbscript:", "file:", "mailto:", "ftp:", "gopher:")


def scrape_calendar(url: str = DEFAULT_URL) -> list[dict[str, str]]:
    """Return current-month-forward Wellton Town Council rows."""
    _validate_input_url(url)
    status, final_url, body = _fetch_bounded(make_session(), url)
    if status in BLOCKING_HTTP_STATUSES:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Wellton official Agenda Center blocked the neutral paced request: "
            "status=%d final_url=%s failure_shape=honest-empty "
            "missing_data_scope=all_current_month_forward_meetings",
            status,
            final_url,
        )
        return []
    if status != 200:
        raise RuntimeError(f"Wellton Agenda Center returned HTTP {status}: {final_url}")

    soup = BeautifulSoup(body, "html.parser")
    heading = soup.find("h1")
    table = soup.find("table", id="table2")
    if not isinstance(heading, Tag) or _clean(heading.get_text(" ", strip=True)) != "Agenda Center":
        raise RuntimeError("Wellton CivicEngage fingerprint drift: h1 Agenda Center missing")
    if not isinstance(table, Tag):
        raise RuntimeError("Wellton CivicEngage fingerprint drift: table#table2 missing")
    logger.info("Wellton vendor_fingerprint witness=h1_Agenda_Center+table2")
    logger.warning(
        "Wellton field_absence field=meeting_location "
        "reason=Agenda_Center_has_no_per_row_location_signal"
    )
    logger.warning(
        "Wellton field_absence fields=video_url,agenda_packet_url,ecomment_url "
        "reason=Agenda_Center_table_exposes_no_corresponding_columns"
    )

    rows = [row for row in table.find_all("tr") if row.find_all("td", recursive=False)]
    if len(rows) > MAX_ROWS:
        raise RuntimeError(f"Wellton Agenda Center exceeded the {MAX_ROWS}-row cap: rows={len(rows)}")
    if not rows:
        table_text = _fold(table.get_text(" ", strip=True))
        if "no records" not in table_text and "no agendas" not in table_text:
            raise RuntimeError("Wellton Agenda Center had no data rows and no official empty marker")
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning("Wellton official Agenda Center explicitly reports no records")
        return []

    cutoff = date.today().replace(day=1)
    counters: Counter[str] = Counter()
    meetings: list[dict[str, str]] = []
    seen: set[str] = set()
    for position, row in enumerate(rows, start=1):
        counters["rows_seen"] += 1
        cells = row.find_all("td", recursive=False)
        row_text = _clean(cells[0].get_text(" ", strip=True))
        match = DATE_RE.match(row_text)
        if not match:
            raise RuntimeError(
                f"Wellton Agenda Center row has no leading meeting date: "
                f"position={position} text={row_text!r}"
            )
        try:
            meeting_day = datetime.strptime(match.group(1), "%b %d, %Y").date()
        except ValueError as exc:
            raise RuntimeError(
                f"Wellton row has an invalid date: position={position} value={match.group(1)!r}"
            ) from exc
        if meeting_day < cutoff:
            counters["before_current_month"] += 1
            continue

        title, agenda_url, minutes_url, meeting_id = _row_documents(
            row,
            final_url,
            position=position,
        )
        if not COUNCIL_RE.search(title):
            if EXCLUDED_RE.search(title):
                counters["known_subordinate_body"] += 1
                logger.info(
                    "Wellton row dropped: reason=known_subordinate_body position=%d title=%r",
                    position,
                    title,
                )
                continue
            raise RuntimeError(
                f"Wellton current governing-body vocabulary drift: position={position} title={title!r}"
            )
        if meeting_id in seen:
            counters["duplicate_id"] += 1
            logger.warning(
                "Wellton row dropped: reason=duplicate_id position=%d id=%r title=%r",
                position,
                meeting_id,
                title,
            )
            continue
        seen.add(meeting_id)

        meeting_time = _time_from_title(title, position=position)
        meeting = {
            "meeting_title": title,
            "meeting_date": meeting_day.isoformat(),
            "meeting_time": meeting_time,
            "meeting_location": "",
            "meeting_status": _status(title, agenda_url, minutes_url),
            "agenda_url": agenda_url,
            "minutes_url": minutes_url,
            "video_url": "",
            "agenda_packet_url": "",
            "ecomment_url": "",
            "meeting_id": meeting_id,
        }
        _validate_meeting(meeting, position=position)
        meetings.append(meeting)
        counters["rows_accepted"] += 1
        counters["meeting_location_absent_by_construction"] += 1
        logger.info(
            "Wellton meeting emitted: id=%s date=%s title=%r",
            meeting_id,
            meeting_day.isoformat(),
            title,
        )

    meetings.sort(key=lambda item: (item["meeting_date"], item["meeting_time"], item["meeting_id"]))
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Wellton official Agenda Center contained no current council rows: stats=%s",
            dict(counters),
        )
    logger.warning("Wellton scrape summary: counters=%s", dict(counters))
    return meetings


def _row_documents(row: Tag, base_url: str, *, position: int) -> tuple[str, str, str, str]:
    title_candidates: set[str] = set()
    agenda_urls: set[str] = set()
    minutes_urls: set[str] = set()
    meeting_ids: set[str] = set()
    for anchor in row.find_all("a"):
        raw = str(anchor.get("href", "") or "").strip()
        if not raw:
            continue
        path = urlparse(urljoin(base_url, raw)).path.casefold()
        field = "agenda_url" if AGENDA_PATH in path else "minutes_url" if MINUTES_PATH in path else ""
        if not field:
            continue
        emitted = _safe_url(raw, base_url, field=field, position=position)
        if not emitted:
            continue
        if field == "agenda_url":
            agenda_urls.add(emitted)
        else:
            minutes_urls.add(emitted)
        label = _clean(anchor.get_text(" ", strip=True))
        if label and _fold(label) not in {"agenda", "minutes", "download"}:
            title_candidates.add(label)
        id_match = ID_RE.search(urlparse(emitted).path)
        if id_match:
            meeting_ids.add(id_match.group(1))

    if len(title_candidates) != 1:
        raise RuntimeError(
            f"Wellton row lacks one unambiguous named council document: "
            f"position={position} titles={sorted(title_candidates)!r}"
        )
    if len(agenda_urls) > 1 or len(minutes_urls) > 1 or len(meeting_ids) != 1:
        raise RuntimeError(
            f"Wellton row document identity drift: position={position} "
            f"agenda={sorted(agenda_urls)!r} minutes={sorted(minutes_urls)!r} "
            f"ids={sorted(meeting_ids)!r}"
        )
    return (
        next(iter(title_candidates)),
        next(iter(agenda_urls), ""),
        next(iter(minutes_urls), ""),
        next(iter(meeting_ids)),
    )


def _time_from_title(title: str, *, position: int) -> str:
    matches = list(TIME_RE.finditer(title[:1000]))
    if not matches:
        logger.info(
            "Wellton meeting_time honest-empty: position=%d reason=no_time_in_named_document_title",
            position,
        )
        return ""
    if len(matches) > 1:
        raise RuntimeError(f"Wellton title exposed multiple meeting times: position={position} title={title!r}")
    match = matches[0]
    return f"{int(match.group(1))}:{match.group(2) or '00'} {match.group(3).upper()}M"


def _fetch_bounded(session: Any, url: str) -> tuple[int, str, str]:
    with session.get(url, timeout=REQUEST_TIMEOUT, stream=True, allow_redirects=True) as response:
        final_host = (urlparse(response.url).hostname or "").lower()
        if final_host != ALLOWED_HOST:
            raise ValueError(f"Wellton redirect reached disallowed host: {final_host}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Wellton response exceeded {MAX_RESPONSE_BYTES} bytes: {url}")
        text = bytes(body).decode(response.encoding or "utf-8", errors="replace")
        if "\ufffd" in text:
            raise ValueError("Wellton response contained undecodable text replacement characters")
        return response.status_code, response.url, text


def _safe_url(raw: str, base_url: str, *, field: str, position: int) -> str:
    value = raw.strip()
    if value.casefold().startswith(BAD_SCHEMES) or value.startswith("//"):
        logger.warning(
            "Wellton URL dropped: position=%d field=%s reason=disallowed_input value=%r",
            position,
            field,
            raw,
        )
        return ""
    absolute = urljoin(base_url, value)
    parsed = urlparse(absolute)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != ALLOWED_HOST:
        logger.warning(
            "Wellton URL dropped: position=%d field=%s reason=scheme_or_host raw=%r absolute=%r",
            position,
            field,
            raw,
            absolute,
        )
        return ""
    return absolute


def _status(title: str, agenda_url: str, minutes_url: str) -> str:
    if CANCELLED_RE.search(title):
        return "Cancelled"
    if minutes_url:
        return "Minutes Available"
    if agenda_url:
        return "Agenda Available"
    return "Scheduled"


def _validate_input_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != ALLOWED_HOST
        or parsed.path.casefold().rstrip("/") != "/agendacenter"
    ):
        raise ValueError(f"Wellton parser called with unexpected URL: {url!r}")


def _validate_meeting(meeting: dict[str, str], *, position: int) -> None:
    if tuple(meeting) != CANONICAL_FIELDS:
        raise RuntimeError(f"Wellton schema mismatch: position={position} keys={tuple(meeting)!r}")
    if any(not isinstance(value, str) for value in meeting.values()):
        raise TypeError(f"Wellton row contains non-string values: position={position}")
    for field in ("agenda_url", "minutes_url", "video_url", "agenda_packet_url", "ecomment_url"):
        if meeting[field] and not meeting[field].startswith("https://"):
            raise RuntimeError(
                f"Wellton row contains invalid URL: position={position} "
                f"field={field} value={meeting[field]!r}"
            )


def _fold(value: str) -> str:
    return _clean(value).casefold()


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or "")).replace("\xa0", " ")).strip()


__all__ = ["scrape_calendar"]
