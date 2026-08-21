"""Camp Verde Town Council meetings from the official Agendas & Minutes page."""

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

DEFAULT_URL = "https://www.campverde.az.gov/government/town_council/agendas_minutes.php"
ALLOWED_HOST = "www.campverde.az.gov"
VIDEO_HOST = "townofcampverde.sharepoint.com"
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
MAX_RESPONSE_BYTES = 5_000_000
MAX_ROWS = 500
REQUEST_TIMEOUT = 45
BLOCKING_HTTP_STATUSES = {401, 403, 407, 423, 429, 451}
ROW_RE = re.compile(r"^(\d{2}/\d{2}/\d{2})\s+(.+)$")
TIME_RE = re.compile(
    r"(?<!\d)(1[0-2]|0?[1-9]):([0-5]\d)\s*([AP])\.?M\.?(?=\s|$|[^\w.])",
    re.IGNORECASE,
)
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
COUNCIL_TITLE_RE = re.compile(
    r"^(?:Regular|Special|Work|Study|Joint|Executive|Emergency)\s+Session\s+Meeting\b",
    re.IGNORECASE,
)
ARCHIVE_NAV_RE = re.compile(r"^Town Council Meeting Archives\s+\d{4}-\d{4}$", re.IGNORECASE)
BAD_SCHEMES = ("javascript:", "data:", "vbscript:", "file:", "mailto:", "ftp:", "gopher:")


def scrape_calendar(url: str = DEFAULT_URL) -> list[dict[str, str]]:
    """Return current-month-forward Camp Verde Town Council rows."""
    _validate_input_url(url)
    status, final_url, body = _fetch_bounded(make_session(), url)
    if status in BLOCKING_HTTP_STATUSES:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Camp Verde official council archive blocked the neutral paced request: "
            "status=%d final_url=%s failure_shape=honest-empty "
            "missing_data_scope=all_current_month_forward_meetings",
            status,
            final_url,
        )
        return []
    if status != 200:
        raise RuntimeError(f"Camp Verde council archive returned HTTP {status}: {final_url}")

    soup = BeautifulSoup(body, "html.parser")
    heading = soup.find("h1")
    breadcrumb_text = " ".join(
        node.get_text(" ", strip=True)
        for node in soup.select(".breadcrumb, .breadcrumbs, [aria-label*='breadcrumb' i]")
    )
    tables = soup.select("table.table")
    if not isinstance(heading, Tag) or _clean(heading.get_text(" ", strip=True)) != "Agendas & Minutes":
        raise RuntimeError("Camp Verde fingerprint drift: h1 Agendas & Minutes missing")
    if "town council" not in _fold(breadcrumb_text) or not tables:
        raise RuntimeError(
            "Camp Verde fingerprint drift: Town Council breadcrumb or table.table missing"
        )
    logger.info(
        "Camp Verde vendor_fingerprint witness=h1_Agendas_Minutes+Town_Council_breadcrumb+table.table"
    )
    logger.warning(
        "Camp Verde field_absence field=meeting_location "
        "reason=archive_has_no_per_row_location_signal"
    )
    logger.warning(
        "Camp Verde field_absence fields=meeting_id,ecomment_url "
        "reason=archive_exposes_no_vendor_id_or_comment_signal"
    )

    rows = [row for table in tables for row in table.find_all("tr") if row.find_all("td", recursive=False)]
    if len(rows) > MAX_ROWS:
        raise RuntimeError(f"Camp Verde archive exceeded the {MAX_ROWS}-row cap: rows={len(rows)}")
    if not rows:
        page_text = _fold(soup.get_text(" ", strip=True))
        if "no meetings" not in page_text and "no agendas" not in page_text:
            raise RuntimeError("Camp Verde archive had no meeting rows and no official empty marker")
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning("Camp Verde official council archive explicitly reports no meetings")
        return []

    cutoff = date.today().replace(day=1)
    counters: Counter[str] = Counter()
    meetings: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for position, row in enumerate(rows, start=1):
        counters["rows_seen"] += 1
        cells = row.find_all("td", recursive=False)
        if len(cells) < 2:
            raise RuntimeError(
                f"Camp Verde row width drift: position={position} cells={len(cells)}"
            )
        date_title = _clean(cells[0].get_text(" ", strip=True))
        match = ROW_RE.fullmatch(date_title)
        if not match:
            if ARCHIVE_NAV_RE.fullmatch(date_title):
                counters["known_archive_navigation"] += 1
                logger.info(
                    "Camp Verde row dropped: reason=known_archive_navigation "
                    "position=%d text=%r",
                    position,
                    date_title,
                )
                continue
            raise RuntimeError(
                f"Camp Verde row has unrecognized date/title shape: "
                f"position={position} value={date_title!r}"
            )
        try:
            meeting_day = datetime.strptime(match.group(1), "%m/%d/%y").date()
        except ValueError as exc:
            raise RuntimeError(
                f"Camp Verde row has invalid date: position={position} value={match.group(1)!r}"
            ) from exc
        if meeting_day < cutoff:
            counters["before_current_month"] += 1
            continue
        title = match.group(2)
        if not COUNCIL_TITLE_RE.search(title):
            raise RuntimeError(
                f"Camp Verde current council-title vocabulary drift: "
                f"position={position} title={title!r}"
            )

        urls = _row_urls(cells[1], final_url, title=title, position=position)
        key = (meeting_day.isoformat(), _fold(title))
        if key in seen:
            counters["duplicate"] += 1
            logger.warning(
                "Camp Verde row dropped: reason=duplicate position=%d date=%s title=%r",
                position,
                meeting_day.isoformat(),
                title,
            )
            continue
        seen.add(key)
        meeting_time = _time_from_title(title, position=position)
        meeting = {
            "meeting_title": title,
            "meeting_date": meeting_day.isoformat(),
            "meeting_time": meeting_time,
            "meeting_location": "",
            "meeting_status": _status(
                title,
                urls["agenda_url"],
                urls["minutes_url"],
                urls["agenda_packet_url"],
            ),
            "agenda_url": urls["agenda_url"],
            "minutes_url": urls["minutes_url"],
            "video_url": urls["video_url"],
            "agenda_packet_url": urls["agenda_packet_url"],
            "ecomment_url": "",
            "meeting_id": "",
        }
        _validate_meeting(meeting, position=position)
        meetings.append(meeting)
        counters["rows_accepted"] += 1
        counters["meeting_location_absent_by_construction"] += 1
        counters["meeting_id_absent_by_construction"] += 1
        counters["ecomment_absent_by_construction"] += 1
        logger.info(
            "Camp Verde meeting emitted: date=%s title=%r links=%s",
            meeting_day.isoformat(),
            title,
            {field: bool(value) for field, value in urls.items()},
        )

    meetings.sort(key=lambda item: (item["meeting_date"], item["meeting_time"], item["meeting_title"]))
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Camp Verde official council archive contained no current-month rows: stats=%s",
            dict(counters),
        )
    logger.warning("Camp Verde scrape summary: counters=%s", dict(counters))
    return meetings


def _row_urls(cell: Tag, base_url: str, *, title: str, position: int) -> dict[str, str]:
    label_to_field = {
        "agenda": "agenda_url",
        "minutes": "minutes_url",
        "video": "video_url",
        "packet": "agenda_packet_url",
    }
    candidates: dict[str, set[str]] = {field: set() for field in label_to_field.values()}
    for anchor in cell.find_all("a"):
        label = _fold(anchor.get_text(" ", strip=True))
        href = str(anchor.get("href", "") or "").strip()
        if label in {"", "more", "more..."}:
            if href:
                logger.warning(
                    "Camp Verde link dropped: position=%d title=%r "
                    "reason=unmapped_more_link href=%r",
                    position,
                    title,
                    href,
                )
            continue
        field = label_to_field.get(label)
        if field is None:
            raise RuntimeError(
                f"Camp Verde current document vocabulary drift: "
                f"position={position} title={title!r} label={label!r}"
            )
        if not href:
            logger.warning(
                "Camp Verde URL dropped: position=%d title=%r field=%s reason=href_empty",
                position,
                title,
                field,
            )
            continue
        emitted = _safe_url(href, base_url, field=field, title=title, position=position)
        if emitted:
            candidates[field].add(emitted)

    output: dict[str, str] = {}
    for field, values in candidates.items():
        if len(values) > 1:
            raise RuntimeError(
                f"Camp Verde row exposed conflicting {field} links: "
                f"position={position} title={title!r} links={sorted(values)!r}"
            )
        output[field] = next(iter(values), "")
        if not output[field]:
            logger.info(
                "Camp Verde %s honest-empty: position=%d title=%r reason=no_same_row_link",
                field,
                position,
                title,
            )
    return output


def _time_from_title(title: str, *, position: int) -> str:
    matches = list(TIME_RE.finditer(title[:1000]))
    if not matches:
        logger.info(
            "Camp Verde meeting_time honest-empty: position=%d reason=no_time_in_row_title",
            position,
        )
        return ""
    if len(matches) > 1:
        raise RuntimeError(
            f"Camp Verde title exposed multiple meeting times: position={position} title={title!r}"
        )
    match = matches[0]
    return f"{int(match.group(1))}:{match.group(2)} {match.group(3).upper()}M"


def _fetch_bounded(session: Any, url: str) -> tuple[int, str, str]:
    with session.get(url, timeout=REQUEST_TIMEOUT, stream=True, allow_redirects=True) as response:
        final_host = (urlparse(response.url).hostname or "").lower()
        if final_host != ALLOWED_HOST:
            raise ValueError(f"Camp Verde redirect reached disallowed host: {final_host}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Camp Verde response exceeded {MAX_RESPONSE_BYTES} bytes: {url}")
        text = bytes(body).decode(response.encoding or "utf-8", errors="replace")
        if "\ufffd" in text:
            raise ValueError("Camp Verde response contained undecodable text replacement characters")
        return response.status_code, response.url, text


def _safe_url(
    raw: str,
    base_url: str,
    *,
    field: str,
    title: str,
    position: int,
) -> str:
    value = raw.strip()
    if value.casefold().startswith(BAD_SCHEMES) or value.startswith("//"):
        logger.warning(
            "Camp Verde URL dropped: position=%d title=%r field=%s "
            "reason=disallowed_input raw=%r",
            position,
            title,
            field,
            raw,
        )
        return ""
    absolute = urljoin(base_url, value)
    parsed = urlparse(absolute)
    host = (parsed.hostname or "").lower()
    allowed_hosts = {ALLOWED_HOST, VIDEO_HOST} if field == "video_url" else {ALLOWED_HOST}
    if parsed.scheme != "https" or host not in allowed_hosts:
        logger.warning(
            "Camp Verde URL dropped: position=%d title=%r field=%s "
            "reason=scheme_or_host raw=%r absolute=%r host=%r",
            position,
            title,
            field,
            raw,
            absolute,
            host,
        )
        return ""
    return absolute


def _status(title: str, agenda_url: str, minutes_url: str, packet_url: str) -> str:
    if CANCELLED_RE.search(title):
        return "Cancelled"
    if minutes_url:
        return "Minutes Available"
    if agenda_url or packet_url:
        return "Agenda Available"
    return "Scheduled"


def _validate_input_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != ALLOWED_HOST
        or parsed.path != "/government/town_council/agendas_minutes.php"
    ):
        raise ValueError(f"Camp Verde parser called with unexpected URL: {url!r}")


def _validate_meeting(meeting: dict[str, str], *, position: int) -> None:
    if tuple(meeting) != CANONICAL_FIELDS:
        raise RuntimeError(f"Camp Verde schema mismatch: position={position} keys={tuple(meeting)!r}")
    if any(not isinstance(value, str) for value in meeting.values()):
        raise TypeError(f"Camp Verde row contains non-string values: position={position}")
    for field in ("agenda_url", "minutes_url", "video_url", "agenda_packet_url", "ecomment_url"):
        if meeting[field] and not meeting[field].startswith("https://"):
            raise RuntimeError(
                f"Camp Verde row contains invalid URL: position={position} "
                f"field={field} value={meeting[field]!r}"
            )


def _fold(value: str) -> str:
    return _clean(value).casefold()


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or "")).replace("\xa0", " ")).strip()


__all__ = ["scrape_calendar"]
