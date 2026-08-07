"""Mesquite — Granicus ViewPublisher meeting parser."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
import logging
import re
import ssl
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse
from urllib.request import HTTPSHandler, Request, build_opener


__all__ = ["scrape_calendar"]

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://mesquitenv.granicus.com/ViewPublisher.php?view_id=1"
ALLOWED_HOSTS = {
    "mesquitenv.granicus.com",
    # Witnessed live in same-row Agenda Packet links on Mesquite ViewPublisher.
    "d3n9y02raazwpg.cloudfront.net",
}
BAD_SCHEMES = ("javascript:", "data:", "vbscript:", "file:", "mailto:", "ftp:", "gopher:")
MAX_RESPONSE_BYTES = 10_000_000
CHUNK_SIZE = 64 * 1024
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

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
URL_FIELDS = (
    "agenda_url",
    "minutes_url",
    "video_url",
    "agenda_packet_url",
    "ecomment_url",
)

CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
DATE_RE = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2}),\s+(\d{4})$")
ONCLICK_URL_RE = re.compile(r"""(?P<url>(?:https?:)?//[^'"\s)]+|/[A-Za-z0-9_./?&=%+-]+)""")

MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


@dataclass
class _Anchor:
    href: str
    onclick: str
    attrs: dict[str, str]
    text_parts: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return _clean_text("".join(self.text_parts))


@dataclass
class _Cell:
    classes: set[str]
    data_label: str
    text_parts: list[str] = field(default_factory=list)
    anchors: list[_Anchor] = field(default_factory=list)

    @property
    def text(self) -> str:
        return _clean_text("".join(self.text_parts))


@dataclass
class _ArchiveRow:
    cells: list[_Cell] = field(default_factory=list)


@dataclass
class _Stats:
    rows_seen: int = 0
    rows_accepted: int = 0
    rows_dropped: int = 0
    drop_reasons: Counter[str] = field(default_factory=Counter)
    field_absences: Counter[str] = field(default_factory=Counter)
    url_rejections: Counter[str] = field(default_factory=Counter)
    placeholder_recoveries: Counter[str] = field(default_factory=Counter)

    def drop(self, reason: str) -> None:
        self.rows_dropped += 1
        self.drop_reasons[reason] += 1

    def absence(self, field: str, reason: str) -> None:
        self.field_absences[f"{field}:{reason}"] += 1


class _ArchiveHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[_ArchiveRow] = []
        self._row: _ArchiveRow | None = None
        self._cell: _Cell | None = None
        self._anchor: _Anchor | None = None
        self._stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_name = tag.lower()
        attr_map = {name.lower(): value or "" for name, value in attrs}
        classes = set(attr_map.get("class", "").split())

        if tag_name in {"li", "div"} and "table-row" in classes:
            self._row = _ArchiveRow()
            self._stack.append("row")
            return

        if self._row is not None and tag_name == "div" and "table-cell" in classes:
            self._cell = _Cell(classes=classes, data_label=attr_map.get("data-label", ""))
            self._stack.append("cell")
            return

        if self._row is not None and tag_name in {"li", "div"}:
            self._stack.append("other")

        if tag_name == "a" and self._cell is not None:
            self._anchor = _Anchor(
                href=attr_map.get("href", ""),
                onclick=attr_map.get("onclick", ""),
                attrs=attr_map,
            )
            self._cell.anchors.append(self._anchor)

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.text_parts.append(data)
        if self._anchor is not None:
            self._anchor.text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag_name = tag.lower()
        if tag_name == "a":
            self._anchor = None
            return

        if tag_name not in {"li", "div"} or not self._stack:
            return

        kind = self._stack.pop()
        if kind == "cell":
            if self._row is not None and self._cell is not None:
                self._row.cells.append(self._cell)
            self._cell = None
        elif kind == "row":
            if self._row is not None:
                self.rows.append(self._row)
            self._row = None
            self._cell = None
            self._anchor = None


def scrape_calendar(url: str) -> list[dict[str, str]]:
    """Scrape Mesquite meetings from the live Granicus ViewPublisher archive."""
    stats = _Stats()
    publisher_url = _publisher_url_from_input(url)
    view_id = _view_id(publisher_url)

    logger.warning(
        "mesquite_scrape_started url=%s publisher_url=%s view_id=%s allowed_hosts=%s "
        "tls_verify=True max_bytes=%d",
        url,
        publisher_url,
        view_id,
        sorted(ALLOWED_HOSTS),
        MAX_RESPONSE_BYTES,
    )
    logger.warning(
        "startup_absence_declaration fields=meeting_time,meeting_location,ecomment_url "
        "reason=mesquite_granicus_viewpublisher_archive_rows_expose_date_duration_and_document_links_but_no_per_row_time_location_or_ecomment"
    )

    try:
        html, headers, final_url = _fetch_text_bounded(publisher_url)
    except (HTTPError, URLError, OSError, ValueError) as exc:
        logger.warning(
            "architectural_blocker url=%s scope=all_meetings action=return_honest_empty "
            "reason=fetch_or_redirect_validation_failed exc=%r",
            publisher_url,
            exc,
        )
        _log_summary(stats)
        return []

    _validate_granicus_viewpublisher_surface(html, headers, publisher_url, view_id)
    raw_rows = _parse_archive_rows(html)
    logger.info("archive_rows_observed url=%s rows=%d", final_url, len(raw_rows))

    meetings: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for index, raw_row in enumerate(raw_rows, start=1):
        stats.rows_seen += 1
        row_label = f"archive_row:{index}"
        meeting = _build_meeting(raw_row, final_url, view_id, row_label, stats)
        if meeting is None:
            continue

        meeting_id = meeting["meeting_id"]
        if meeting_id and meeting_id in seen_ids:
            stats.drop("duplicate_meeting_id")
            logger.warning(
                "row_dropped row=%s reason=duplicate_meeting_id meeting_id=%s title=%r",
                row_label,
                meeting_id,
                meeting["meeting_title"],
            )
            continue
        if meeting_id:
            seen_ids.add(meeting_id)

        _validate_schema(meeting, row_label)
        meetings.append(meeting)
        stats.rows_accepted += 1
        logger.info(
            "row_accepted row=%s meeting_id=%s date=%s title=%r status=%s agenda=%s packet=%s minutes=%s video=%s",
            row_label,
            meeting_id,
            meeting["meeting_date"],
            meeting["meeting_title"],
            meeting["meeting_status"],
            bool(meeting["agenda_url"]),
            bool(meeting["agenda_packet_url"]),
            bool(meeting["minutes_url"]),
            bool(meeting["video_url"]),
        )

    if stats.rows_seen and not meetings:
        raise ValueError("Mesquite Granicus archive rows were witnessed but no meetings were emitted")

    _log_summary(stats)
    return meetings


def _build_meeting(
    raw_row: _ArchiveRow,
    base_url: str,
    view_id: str,
    row_label: str,
    stats: _Stats,
) -> dict[str, str] | None:
    cells = _cells_by_field(raw_row)
    title = cells.get("meeting_title", _Cell(set(), "")).text
    date_text = cells.get("meeting_date", _Cell(set(), "")).text

    if not title:
        stats.drop("missing_title")
        logger.warning("row_dropped row=%s reason=missing_title row_text=%r", row_label, _row_text(raw_row))
        return None

    meeting_date = _parse_date(date_text, row_label, stats)
    if not meeting_date:
        stats.drop("missing_iso_date")
        logger.warning(
            "row_dropped row=%s reason=missing_iso_date title=%r date_text=%r",
            row_label,
            title,
            date_text,
        )
        return None

    agenda_url = _extract_url_from_cell(cells.get("agenda_url"), "agenda_url", base_url, row_label, stats)
    agenda_packet_url = _extract_url_from_cell(
        cells.get("agenda_packet_url"),
        "agenda_packet_url",
        base_url,
        row_label,
        stats,
    )
    minutes_url = _extract_url_from_cell(cells.get("minutes_url"), "minutes_url", base_url, row_label, stats)
    video_url = _extract_url_from_cell(cells.get("video_url"), "video_url", base_url, row_label, stats)
    meeting_id = _meeting_id_from_urls((agenda_url, minutes_url, video_url, agenda_packet_url), row_label, stats)

    stats.absence("meeting_time", "absent_by_construction")
    stats.absence("meeting_location", "absent_by_construction")
    stats.absence("ecomment_url", "absent_by_construction")

    for field, emitted_url in (
        ("agenda_url", agenda_url),
        ("minutes_url", minutes_url),
        ("video_url", video_url),
        ("agenda_packet_url", agenda_packet_url),
    ):
        _warn_view_id_mismatch(emitted_url, view_id, row_label, field)

    return {
        "meeting_title": title,
        "meeting_date": meeting_date,
        "meeting_time": "",
        "meeting_location": "",
        "meeting_status": _status_from_evidence(title, agenda_url, agenda_packet_url, minutes_url),
        "agenda_url": agenda_url,
        "minutes_url": minutes_url,
        "video_url": video_url,
        "agenda_packet_url": agenda_packet_url,
        "ecomment_url": "",
        "meeting_id": meeting_id,
    }


def _fetch_text_bounded(url: str) -> tuple[str, dict[str, str], str]:
    start_host = _host(url)
    if start_host not in ALLOWED_HOSTS:
        raise ValueError(f"Input URL host is not allowlisted: {start_host!r}")

    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        method="GET",
    )
    with _http_opener().open(request, timeout=30) as response:
        final_url = response.geturl()
        final_host = _host(final_url)
        if final_host not in ALLOWED_HOSTS:
            raise ValueError(f"Redirect to disallowed host: {final_host!r} started_from={url!r}")

        body = bytearray()
        while True:
            chunk = response.read(CHUNK_SIZE)
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Response from {url!r} exceeded {MAX_RESPONSE_BYTES} bytes")

        encoding = response.headers.get_content_charset() or "utf-8"
        headers = {key.lower(): value for key, value in response.headers.items()}

    return bytes(body).decode(encoding, errors="replace"), headers, final_url


def _http_opener():
    verify_paths = ssl.get_default_verify_paths()
    if verify_paths.cafile or not Path("/etc/ssl/cert.pem").exists():
        return build_opener()
    context = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    return build_opener(HTTPSHandler(context=context))


def _publisher_url_from_input(url: str) -> str:
    source = url or DEFAULT_URL
    parsed = urlparse(source)
    if "ViewPublisherRSS.php" not in parsed.path:
        return source

    query = parse_qs(parsed.query)
    view_ids = query.get("view_id") or []
    if not view_ids or not view_ids[0]:
        raise ValueError(f"Mesquite RSS URL lacks view_id: {url!r}")
    return urlunparse(parsed._replace(path="/ViewPublisher.php", query=f"view_id={view_ids[0]}"))


def _view_id(url: str) -> str:
    values = parse_qs(urlparse(url).query).get("view_id") or []
    view_id = values[0].strip() if values else ""
    if not view_id:
        raise ValueError(f"Mesquite Granicus URL lacks view_id: {url!r}")
    return view_id


def _validate_granicus_viewpublisher_surface(
    html: str,
    headers: dict[str, str],
    source_url: str,
    view_id: str,
) -> None:
    witnessed = {
        "path_ViewPublisher": "ViewPublisher.php" in urlparse(source_url).path,
        "x_granicus_server": "x-granicus-server" in headers,
        "rss_agendas_link": f"ViewPublisherRSS.php?view_id={view_id}&mode=agendas" in html,
        "archive_list": 'class="archive-list"' in html,
        "archive_rows": "table-row--7column" in html,
        "agenda_viewer_links": "AgendaViewer.php" in html,
    }
    if not (
        witnessed["path_ViewPublisher"]
        and (witnessed["x_granicus_server"] or witnessed["archive_list"])
        and witnessed["archive_rows"]
        and witnessed["agenda_viewer_links"]
    ):
        raise ValueError(f"Mesquite Granicus ViewPublisher fingerprint missing tokens: {witnessed}")
    logger.info("vendor_fingerprint_witness vendor=granicus_viewpublisher witnesses=%s", witnessed)


def _parse_archive_rows(html: str) -> list[_ArchiveRow]:
    parser = _ArchiveHTMLParser()
    parser.feed(html)
    data_rows = []
    for raw_row in parser.rows:
        cells = _cells_by_field(raw_row)
        if "meeting_title" in cells and "meeting_date" in cells:
            data_rows.append(raw_row)
    if not data_rows:
        raise ValueError("Mesquite Granicus archive-list was present but no data rows were parsed")
    return data_rows


def _cells_by_field(raw_row: _ArchiveRow) -> dict[str, _Cell]:
    cells: dict[str, _Cell] = {}
    for cell in raw_row.cells:
        if "archive-name" in cell.classes and cell.data_label == "Name:":
            cells["meeting_title"] = cell
        elif "archive-date" in cell.classes:
            cells["meeting_date"] = cell
        elif "archive-agenda" in cell.classes:
            cells["agenda_url"] = cell
        elif "archive-packet" in cell.classes:
            cells["agenda_packet_url"] = cell
        elif "archive-minutes" in cell.classes:
            cells["minutes_url"] = cell
        elif "archive-video" in cell.classes:
            cells["video_url"] = cell
    return cells


def _extract_url_from_cell(
    cell: _Cell | None,
    field: str,
    base_url: str,
    row_label: str,
    stats: _Stats,
) -> str:
    if cell is None:
        stats.absence(field, "cell_absent")
        return ""
    if not cell.anchors:
        if cell.text:
            logger.warning(
                "field_empty row=%s field=%s reason=non_empty_cell_without_anchor rejected_text=%r",
                row_label,
                field,
                cell.text,
            )
            stats.absence(field, "non_empty_cell_without_anchor")
        else:
            stats.absence(field, "empty_cell")
        return ""

    emitted = ""
    for position, anchor in enumerate(cell.anchors, start=1):
        raw_href = anchor.href.strip()
        if raw_href and not _is_placeholder_href(raw_href):
            candidate = raw_href
            source = f"anchor[{position}].href"
        else:
            fallback_urls = _fallback_urls_from_anchor(anchor)
            if fallback_urls:
                stats.placeholder_recoveries[f"{field}:anchor_placeholder"] += 1
                logger.info(
                    "url_placeholder_recovered row=%s field=%s href=%r fallback_count=%d",
                    row_label,
                    field,
                    raw_href,
                    len(fallback_urls),
                )
                candidate = fallback_urls[0]
                source = f"anchor[{position}].fallback"
            else:
                stats.absence(field, "placeholder_without_fallback")
                logger.warning(
                    "field_empty row=%s field=%s reason=placeholder_href_without_fallback rejected_href=%r checked=onclick_data_attrs",
                    row_label,
                    field,
                    raw_href,
                )
                continue

        url_value = _emit_url(candidate, base_url, field, row_label, source, stats)
        if not url_value:
            continue
        if emitted and emitted != url_value:
            logger.warning(
                "url_conflict row=%s field=%s kept=%r rejected_additional=%r source=%s",
                row_label,
                field,
                emitted,
                url_value,
                source,
            )
            continue
        emitted = url_value

    if not emitted:
        stats.absence(field, "no_emitted_url")
    return emitted


def _fallback_urls_from_anchor(anchor: _Anchor) -> list[str]:
    values: list[str] = []
    if anchor.onclick:
        values.extend(match.group("url") for match in ONCLICK_URL_RE.finditer(anchor.onclick[:2_000]))
    for name, value in anchor.attrs.items():
        if name in {"href", "onclick"} or value is None:
            continue
        lowered = name.lower()
        if lowered.startswith("data-") or any(token in lowered for token in ("url", "href", "src")):
            values.extend(match.group("url") for match in ONCLICK_URL_RE.finditer(value[:2_000]))
            if value.startswith(("http://", "https://", "//", "/")):
                values.append(value)
    return values


def _emit_url(
    href: str,
    base_url: str,
    field: str,
    row_label: str,
    source: str,
    stats: _Stats,
) -> str:
    raw = href.strip()
    if not raw:
        stats.url_rejections[f"{field}:empty"] += 1
        logger.warning("url_rejected row=%s field=%s source=%s reason=empty_href", row_label, field, source)
        return ""

    lowered = raw.lower().lstrip()
    for bad_scheme in BAD_SCHEMES:
        if lowered.startswith(bad_scheme):
            stats.url_rejections[f"{field}:bad_scheme"] += 1
            logger.warning(
                "url_rejected row=%s field=%s source=%s reason=bad_scheme rejected=%r",
                row_label,
                field,
                source,
                href,
            )
            return ""

    absolute = urljoin(base_url, raw)
    parsed = urlparse(absolute)
    host = _host(absolute)
    if parsed.scheme not in {"http", "https"}:
        stats.url_rejections[f"{field}:non_http_scheme"] += 1
        logger.warning(
            "url_rejected row=%s field=%s source=%s reason=non_http_scheme rejected=%r absolute=%r",
            row_label,
            field,
            source,
            href,
            absolute,
        )
        return ""
    if host not in ALLOWED_HOSTS:
        stats.url_rejections[f"{field}:disallowed_host"] += 1
        logger.warning(
            "url_rejected row=%s field=%s source=%s reason=disallowed_host host=%r rejected=%r absolute=%r",
            row_label,
            field,
            source,
            host,
            href,
            absolute,
        )
        return ""
    return absolute


def _parse_date(value: str, row_label: str, stats: _Stats) -> str:
    cleaned = _clean_text(value)
    match = DATE_RE.fullmatch(cleaned)
    if not match:
        stats.absence("meeting_date", "date_regex_failed")
        logger.warning("date_parse_failed row=%s rejected=%r reason=unexpected_format", row_label, value)
        return ""

    month_name, day, year = match.groups()
    month = MONTHS.get(month_name.rstrip(".").lower())
    if not month:
        stats.absence("meeting_date", "unknown_month")
        logger.warning("date_parse_failed row=%s rejected=%r reason=unknown_month", row_label, value)
        return ""

    try:
        return datetime(int(year), month, int(day)).date().isoformat()
    except ValueError as exc:
        stats.absence("meeting_date", "invalid_date")
        logger.warning("date_parse_failed row=%s rejected=%r reason=invalid_date exc=%s", row_label, value, exc)
        return ""


def _meeting_id_from_urls(urls: tuple[str, ...], row_label: str, stats: _Stats) -> str:
    for value in urls:
        if not value:
            continue
        clip_ids = parse_qs(urlparse(value).query).get("clip_id") or []
        if clip_ids and clip_ids[0]:
            return clip_ids[0]
    stats.absence("meeting_id", "no_clip_id_in_same_row_urls")
    logger.warning("field_empty row=%s field=meeting_id reason=no_clip_id_in_same_row_urls", row_label)
    return ""


def _status_from_evidence(title: str, agenda_url: str, agenda_packet_url: str, minutes_url: str) -> str:
    if CANCELLED_RE.search(title[:300]):
        return "Cancelled"
    if minutes_url:
        return "Minutes Available"
    if agenda_url or agenda_packet_url:
        return "Agenda Available"
    return "Scheduled"


def _warn_view_id_mismatch(url_value: str, expected_view_id: str, row_label: str, field: str) -> None:
    if not url_value:
        return
    values = parse_qs(urlparse(url_value).query).get("view_id") or []
    actual = values[0].strip() if values else ""
    if actual and actual != expected_view_id:
        logger.warning(
            "url_view_id_mismatch row=%s field=%s expected_view_id=%s actual_view_id=%s url=%r",
            row_label,
            field,
            expected_view_id,
            actual,
            url_value,
        )


def _is_placeholder_href(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered in {"", "#"} or lowered.startswith(BAD_SCHEMES)


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value or "")).strip()


def _row_text(raw_row: _ArchiveRow) -> str:
    return _clean_text(" ".join(cell.text for cell in raw_row.cells))[:500]


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _validate_schema(meeting: dict[str, str], row_label: str) -> None:
    if tuple(meeting.keys()) != CANONICAL_FIELDS:
        raise ValueError(f"Schema mismatch row={row_label}: keys={tuple(meeting.keys())!r}")
    non_strings = {key: type(value).__name__ for key, value in meeting.items() if not isinstance(value, str)}
    if non_strings:
        raise TypeError(f"Non-string parser fields row={row_label}: {non_strings}")
    if meeting["meeting_date"] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", meeting["meeting_date"]):
        raise ValueError(f"Non-ISO meeting_date row={row_label}: {meeting['meeting_date']!r}")
    for field in URL_FIELDS:
        value = meeting[field]
        if value and not value.startswith(("http://", "https://")):
            raise ValueError(f"Non-absolute {field} row={row_label}: {value!r}")


def _log_summary(stats: _Stats) -> None:
    logger.warning(
        "scrape_summary rows_seen=%d rows_accepted=%d rows_dropped=%d drop_reasons=%s "
        "field_absences=%s url_rejections=%s placeholder_recoveries=%s",
        stats.rows_seen,
        stats.rows_accepted,
        stats.rows_dropped,
        dict(sorted(stats.drop_reasons.items())),
        dict(sorted(stats.field_absences.items())),
        dict(sorted(stats.url_rejections.items())),
        dict(sorted(stats.placeholder_recoveries.items())),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s:%(name)s:%(message)s")
    parsed_meetings = scrape_calendar(DEFAULT_URL)
    print(f"Found {len(parsed_meetings)} meetings.")
