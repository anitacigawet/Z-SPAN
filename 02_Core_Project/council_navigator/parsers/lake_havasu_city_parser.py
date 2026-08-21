"""Lake Havasu City Legistar calendar parser."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import html
import ipaddress
import json
import logging
import re
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag
from requests.exceptions import RequestException

from legistar_current_adapter import scrape_legistar_current


logger = logging.getLogger(__name__)

DEFAULT_CALENDAR_URL = "https://lakehavasucity.legistar.com/Calendar.aspx"
MAX_RESPONSE_BYTES = 8_000_000
ALLOWED_HOSTS = {
    "lakehavasucity.legistar.com",
    "legistar.com",
    "lakehavasucity.granicus.com",
    "archive-video.granicus.com",
    "archive-media.granicus.com",
}
BAD_SCHEMES = ("javascript:", "data:", "vbscript:", "file:", "mailto:", "ftp:", "gopher:")

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
URL_FIELDS = {
    "agenda_url",
    "minutes_url",
    "video_url",
    "agenda_packet_url",
    "ecomment_url",
}

HEADER_ALIASES = {
    "title": {"name", "meetingname", "meetingbody", "body"},
    "date": {"date", "meetingdate"},
    "time": {"time", "meetingtime"},
    "details": {"details", "meetingdetails"},
    "agenda_url": {"agenda"},
    "agenda_packet_url": {"agendapacket", "packet", "agendaandpacket"},
    "minutes_url": {"minutes", "minute", "results", "meetingresults", "action", "actions"},
    "video_url": {"video", "media", "audio", "videoaudio", "webcast"},
    "ecomment_url": {"ecomment", "ecomments", "publiccomment", "comments"},
}

CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
DATE_NUMERIC_RE = re.compile(r"(?<!\d)([01]?\d)[/-]([0-3]?\d)[/-](\d{2}|\d{4})(?!\d)")
MONTH_DATE_RE = re.compile(
    r"\b("
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
    r")\.?\s+([0-3]?\d)(?:,|\s)\s*(\d{4})\b",
    re.IGNORECASE,
)
# Time regex test cases: "5:30 a.m.", "5:30 p.m.", "5:30am", "5:30 AM".
TIME_RE = re.compile(
    r"(?<!\d)(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([AaPp])\.?\s*[Mm]\.?(?=\s|$|[^\w.])"
)
MEETING_ID_RE = re.compile(r"^[0-9]+$")
ONCLICK_URL_RE = re.compile(r"""['"]((?:https?:)?//[^'"]+|https?://[^'"]+|/[^'"]+)['"]""")


def scrape_calendar(calendar_url: str) -> list[dict]:
    """Return only Lake Havasu City's flagship City Council meetings."""
    try:
        meetings = scrape_legistar_current(
            calendar_url,
            city_label="Lake Havasu City",
            allowed_titles=frozenset({"City Council", "City Council Special Meeting"}),
            allowed_media_hosts=frozenset({"lakehavasucity.granicus.com"}),
        )
        return _disambiguate_same_day_city_council_rows(meetings)
    except RequestException as exc:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Lake Havasu City official Legistar calendar blocked the neutral paced request: "
            "failure_shape=honest-empty missing_data_scope=all_current_month_forward_meetings error=%r",
            exc,
        )
        return []


def _disambiguate_same_day_city_council_rows(
    meetings: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Use witnessed Lake Havasu meeting-type text for same-day title collisions.

    Lake Havasu's Legistar tenant exposes both the governing body (``City
    Council``) and the meeting type, but places the latter in the location
    column.  When two council rows share a date, retaining only the body name
    erases the official distinction between a work session and a regular
    meeting.  Preserve the ordinary body title when it is already unique; for
    collisions only, append the exact witnessed meeting type.  Unknown or
    still-ambiguous collision shapes fail loudly rather than inventing a
    distinction.
    """
    counts = Counter((row["meeting_date"], row["meeting_title"]) for row in meetings)
    rewritten: list[dict[str, str]] = []

    for row in meetings:
        key = (row["meeting_date"], row["meeting_title"])
        if counts[key] == 1:
            rewritten.append(row)
            continue

        location = row["meeting_location"]
        folded_location = location.casefold()
        if "work session" in folded_location:
            meeting_kind = "Work Session"
        elif "regular meeting" in folded_location:
            meeting_kind = "Regular Meeting"
        else:
            raise RuntimeError(
                "Lake Havasu same-day City Council rows cannot be truthfully "
                "disambiguated from witnessed meeting type: "
                f"date={row['meeting_date']!r} title={row['meeting_title']!r} "
                f"meeting_id={row['meeting_id']!r} location={location!r}"
            )

        normalized = dict(row)
        normalized["meeting_title"] = f"{row['meeting_title']} {meeting_kind}"
        rewritten.append(normalized)
        logger.warning(
            "Lake Havasu same-day title disambiguated from official location text: "
            "meeting_id=%s date=%s original_title=%r meeting_type=%r location=%r",
            row["meeting_id"],
            row["meeting_date"],
            row["meeting_title"],
            meeting_kind,
            location,
        )

    natural_keys = [(row["meeting_date"], row["meeting_title"]) for row in rewritten]
    if len(natural_keys) != len(set(natural_keys)):
        raise RuntimeError(
            "Lake Havasu same-day City Council title collision remained after "
            f"official meeting-type disambiguation: keys={natural_keys!r}"
        )
    return rewritten


def _fetch_text_bounded(session: Any, url: str) -> str:
    start_host = _host(url)
    if start_host not in ALLOWED_HOSTS:
        raise ValueError(f"Input URL host is not allowed: {start_host!r}")

    with session.get(url, timeout=30, stream=True, allow_redirects=True) as response:
        response.raise_for_status()
        final_host = _host(response.url)
        if final_host not in ALLOWED_HOSTS:
            raise ValueError(f"Redirect to disallowed host: {final_host!r} started_from={url!r}")

        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Response from {url!r} exceeded {MAX_RESPONSE_BYTES} bytes")

        return body.decode(response.encoding or "utf-8", errors="replace")


def _validate_legistar_surface(soup: BeautifulSoup, raw_html: str, source_url: str) -> None:
    witnessed = {
        "host": _host(source_url),
        "calendar_path": urlparse(source_url).path.lower().endswith("/calendar.aspx"),
        "rgMasterTable": soup.select_one("table.rgMasterTable") is not None,
        "gridCalendar": soup.find(id=re.compile(r"gridCalendar", re.IGNORECASE)) is not None,
        "meeting_detail_link": "MeetingDetail.aspx?ID=" in raw_html,
        "legistar_token": "legistar" in raw_html.lower(),
    }
    if not (witnessed["calendar_path"] and (witnessed["rgMasterTable"] or witnessed["gridCalendar"])):
        if "no records" not in _clean_text(soup).lower():
            raise ValueError(f"Unexpected Lake Havasu Legistar calendar surface: {witnessed}")
    logger.info("vendor_fingerprint_witness vendor=legistar witnesses=%s", witnessed)


def _find_calendar_table(soup: BeautifulSoup) -> Tag | None:
    for table in soup.select("table.rgMasterTable"):
        table_id = table.get("id", "")
        if "gridCalendar" in table_id or _headers_contain_calendar_fields(table):
            return table

    grid = soup.find(id=re.compile(r"gridCalendar", re.IGNORECASE))
    if isinstance(grid, Tag):
        nested = grid.find("table", class_=re.compile(r"rgMasterTable"))
        if isinstance(nested, Tag):
            return nested
        if grid.name == "table":
            return grid
    return None


def _headers_contain_calendar_fields(table: Tag) -> bool:
    normalized = {_normalize_header(_clean_text(cell)) for cell in table.find_all("th")}
    return bool({"name", "date", "time"} & normalized) and "date" in normalized


def _extract_headers(table: Tag) -> list[str]:
    header_row = table.select_one("thead tr")
    if header_row is None:
        header_row = table.find("tr")
    if header_row is None:
        raise ValueError("Lake Havasu Legistar table has no header row")
    headers = [_clean_text(cell) for cell in header_row.find_all("th", recursive=False)]
    if not headers:
        raise ValueError("Lake Havasu Legistar table header row has no th cells")
    logger.info("legistar_header_witness headers=%s", headers)
    return headers


def _map_headers(headers: list[str]) -> dict[str, int]:
    mapped: dict[str, int] = {}
    unknown: list[str] = []
    for index, header in enumerate(headers):
        normalized = _normalize_header(header)
        if not normalized:
            continue
        role = _header_role(normalized)
        if role:
            mapped.setdefault(role, index)
        else:
            unknown.append(header)

    for required in ("title", "date", "time"):
        if required not in mapped:
            raise ValueError(f"Lake Havasu Legistar required header {required!r} missing from {headers!r}")
    if unknown:
        logger.warning("legistar_unknown_headers headers=%s mapped=%s", unknown, mapped)
    return mapped


def _header_role(normalized: str) -> str:
    for role, aliases in HEADER_ALIASES.items():
        if normalized in aliases:
            return role
    return ""


def _data_rows(table: Tag) -> list[Tag]:
    rows: list[Tag] = []
    for row in table.select("tbody tr"):
        classes = set(row.get("class", []))
        if classes & {"rgRow", "rgAltRow", "rgSelectedRow"}:
            rows.append(row)
    if rows:
        return rows

    for row in table.find_all("tr"):
        if row.find("td"):
            rows.append(row)
    return rows


def _build_meeting(
    row: Tag,
    cells: list[Tag],
    header_map: dict[str, int],
    base_url: str,
    row_key: str,
    counters: Counter[str],
) -> dict[str, str]:
    title = _clean_text(cells[header_map["title"]])
    date_value = _extract_meeting_date(_clean_text(cells[header_map["date"]]), row_key, counters)
    time_value = _extract_meeting_time(_clean_text(cells[header_map["time"]]), row_key, counters)

    urls = {field: "" for field in URL_FIELDS}
    for role, cell_index in header_map.items():
        if role in URL_FIELDS:
            _extract_urls_from_cell(cells[cell_index], role, base_url, row_key, counters, urls)

    meeting_id = _extract_meeting_id(row, row_key, counters)
    if not title:
        counters["meeting_title_absent"] += 1
        logger.warning("field_absent row=%s field=meeting_title reason=empty_name_cell", row_key)

    return {
        "meeting_title": title,
        "meeting_date": date_value,
        "meeting_time": time_value,
        "meeting_location": "",
        "meeting_status": "",
        "agenda_url": urls["agenda_url"],
        "minutes_url": urls["minutes_url"],
        "video_url": urls["video_url"],
        "agenda_packet_url": urls["agenda_packet_url"],
        "ecomment_url": urls["ecomment_url"],
        "meeting_id": meeting_id,
        "_vendor_status": "",
    }


def _merge_detail_page(
    session: Any,
    detail_url: str,
    meeting: dict[str, str],
    row_key: str,
    counters: Counter[str],
) -> None:
    try:
        detail_html = _fetch_text_bounded(session, detail_url)
    except RequestException as exc:
        counters["detail_fetch_failed"] += 1
        logger.warning(
            "detail_page_fetch_failed row=%s url=%r error=%s continuing_with_calendar_row_evidence",
            row_key,
            detail_url,
            exc,
        )
        return

    soup = BeautifulSoup(detail_html, "html.parser")
    location = _extract_detail_location(soup, row_key, counters)
    if location and not meeting["meeting_location"]:
        meeting["meeting_location"] = location
    elif not meeting["meeting_location"]:
        counters["meeting_location_absent"] += 1

    vendor_status = _extract_detail_status(soup)
    if vendor_status:
        meeting["_vendor_status"] = vendor_status

    detail_urls = {field: "" for field in URL_FIELDS}
    for link in soup.find_all("a"):
        if isinstance(link, Tag):
            _classify_and_assign_link(link, "", detail_url, row_key, counters, detail_urls)
    for field, emitted in detail_urls.items():
        if emitted and not meeting[field]:
            meeting[field] = emitted


def _extract_detail_location(soup: BeautifulSoup, row_key: str, counters: Counter[str]) -> str:
    location_node = soup.find(id=re.compile(r"lblLocation", re.IGNORECASE))
    if location_node:
        location = _clean_text(location_node)
        if location:
            return location
        counters["meeting_location_absent"] += 1
        logger.warning("field_absent row=%s field=meeting_location reason=detail_location_label_empty", row_key)
        return ""

    logger.warning("field_absent row=%s field=meeting_location reason=detail_location_label_missing", row_key)
    return ""


def _extract_detail_status(soup: BeautifulSoup) -> str:
    status_node = soup.find(id=re.compile(r"lblMeetingStatus", re.IGNORECASE))
    if status_node:
        return _clean_text(status_node)
    logger.warning(
        "legacy_detail_status_absent reason=detail_page_has_no_lblMeetingStatus_signal"
    )
    return ""


def _extract_urls_from_cell(
    cell: Tag,
    expected_field: str,
    base_url: str,
    row_key: str,
    counters: Counter[str],
    urls: dict[str, str],
) -> None:
    links = [link for link in cell.find_all("a") if isinstance(link, Tag)]
    if not links:
        text = _clean_text(cell)
        if text and text.lower() not in {"not available", "n/a", "none", "-"}:
            logger.warning(
                "field_absent row=%s field=%s reason=cell_text_without_link text=%r",
                row_key,
                expected_field,
                text,
            )
        return

    for link in links:
        _classify_and_assign_link(link, expected_field, base_url, row_key, counters, urls)


def _classify_and_assign_link(
    link: Tag,
    expected_field: str,
    base_url: str,
    row_key: str,
    counters: Counter[str],
    urls: dict[str, str],
) -> None:
    label = _clean_text(link)
    candidates = _url_candidates(link)
    if not candidates:
        logger.warning(
            "url_drop row=%s field=%s reason=link_without_url_or_fallback label=%r",
            row_key,
            expected_field or "unknown",
            label,
        )
        counters["url_dropped"] += 1
        return

    for candidate in candidates:
        emitted = _emit_url(candidate, base_url, expected_field or "unknown", row_key, counters)
        if not emitted:
            continue
        classified_field = _classify_url(emitted, label, expected_field, row_key, counters)
        if classified_field in URL_FIELDS:
            if urls[classified_field] and urls[classified_field] != emitted:
                logger.warning(
                    "url_duplicate_ignored row=%s field=%s existing=%r ignored=%r",
                    row_key,
                    classified_field,
                    urls[classified_field],
                    emitted,
                )
                return
            urls[classified_field] = emitted
            return

        logger.warning(
            "url_drop row=%s field=%s reason=unclassified_url url=%r label=%r",
            row_key,
            expected_field or "unknown",
            emitted,
            label,
        )
        counters["url_dropped"] += 1


def _url_candidates(link: Tag) -> list[str]:
    candidates: list[str] = []
    href = html.unescape(str(link.get("href", ""))).strip()
    if href:
        candidates.append(href)

    onclick = html.unescape(str(link.get("onclick", ""))).strip()
    if onclick:
        candidates.extend(match.group(1) for match in ONCLICK_URL_RE.finditer(onclick[:2000]))

    for attr, value in link.attrs.items():
        if not attr.startswith("data-") or isinstance(value, list):
            continue
        data_value = html.unescape(str(value)).strip()
        if data_value.startswith(("http://", "https://", "//", "/")):
            candidates.append(data_value)

    deduped: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in deduped:
            deduped.append(candidate)
    return deduped


def _emit_url(href: str, base_url: str, field: str, row_key: str, counters: Counter[str]) -> str:
    cleaned = html.unescape(href).strip()
    if not cleaned:
        return ""

    lowered = cleaned.lower()
    for bad_scheme in BAD_SCHEMES:
        if lowered.startswith(bad_scheme):
            logger.warning(
                "url_drop row=%s field=%s reason=bad_scheme input=%r",
                row_key,
                field,
                href,
            )
            counters["url_dropped"] += 1
            return ""

    absolute = urljoin(base_url, cleaned)
    parsed = urlparse(absolute)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        logger.warning("url_drop row=%s field=%s reason=bad_scheme input=%r", row_key, field, href)
        counters["url_dropped"] += 1
        return ""
    if _is_unsafe_host(host):
        logger.warning("url_drop row=%s field=%s reason=unsafe_host host=%r input=%r", row_key, field, host, href)
        counters["url_dropped"] += 1
        return ""
    if host not in ALLOWED_HOSTS:
        logger.warning(
            "url_drop row=%s field=%s reason=host_not_allowlisted host=%r input=%r",
            row_key,
            field,
            host,
            href,
        )
        counters["url_dropped"] += 1
        return ""
    return absolute


def _classify_url(
    absolute_url: str,
    label: str,
    expected_field: str,
    row_key: str,
    counters: Counter[str],
) -> str:
    parsed = urlparse(absolute_url)
    query = parse_qs(parsed.query)
    media_code = (query.get("M") or [""])[0].upper()

    structured = {
        "A": "agenda_url",
        "AP": "agenda_packet_url",
        "E2": "minutes_url",
        "M": "minutes_url",
        "V": "video_url",
        "VID": "video_url",
        "C": "ecomment_url",
    }
    if media_code:
        if media_code in structured:
            field = structured[media_code]
            if expected_field and expected_field != field:
                logger.warning(
                    "url_field_reclassified_by_structured_code row=%s expected_field=%s "
                    "structured_field=%s media_code=%s url=%r",
                    row_key,
                    expected_field,
                    field,
                    media_code,
                    absolute_url,
                )
            return field
        logger.warning(
            "unknown_legistar_media_code row=%s media_code=%r url=%r label=%r",
            row_key,
            media_code,
            absolute_url,
            label,
        )
        counters["url_dropped"] += 1
        return ""

    path = parsed.path.lower()
    if "meetingdetail.aspx" in path:
        return "details"
    if "video" in path or "mediaplayer" in path or "asx.php" in path:
        return "video_url"

    combined = f"{label} {absolute_url}"
    if re.search(r"\bagenda\s+packet\b|\bpacket\b", combined, re.IGNORECASE):
        return "agenda_packet_url"
    if re.search(r"\bagenda\b", combined, re.IGNORECASE):
        return "agenda_url"
    if re.search(r"\bminutes?\b|\bresults?\b", combined, re.IGNORECASE):
        return "minutes_url"
    if re.search(r"\bvideo\b|\bmedia\b", combined, re.IGNORECASE):
        return "video_url"
    if re.search(r"\be-?comment\b|\bpublic\s+comment\b", combined, re.IGNORECASE):
        return "ecomment_url"

    if expected_field in URL_FIELDS:
        logger.warning(
            "url_classified_by_header_fallback row=%s field=%s url=%r label=%r",
            row_key,
            expected_field,
            absolute_url,
            label,
        )
        return expected_field
    return ""


def _extract_detail_url(row: Tag, base_url: str, row_key: str, counters: Counter[str]) -> str:
    for link in row.find_all("a"):
        if not isinstance(link, Tag):
            continue
        for candidate in _url_candidates(link):
            emitted = _emit_url(candidate, base_url, "details", row_key, counters)
            if not emitted:
                continue
            parsed = urlparse(emitted)
            if "meetingdetail.aspx" in parsed.path.lower():
                return emitted
    logger.warning("field_absent row=%s field=detail_url reason=no_meetingdetail_link", row_key)
    return ""


def _extract_meeting_id(row: Tag, row_key: str, counters: Counter[str]) -> str:
    for link in row.find_all("a"):
        if not isinstance(link, Tag):
            continue
        for candidate in _url_candidates(link):
            parsed = urlparse(html.unescape(candidate))
            meeting_id = (parse_qs(parsed.query).get("ID") or [""])[0]
            if MEETING_ID_RE.match(meeting_id):
                return meeting_id

    counters["meeting_id_absent"] += 1
    logger.warning("field_absent row=%s field=meeting_id reason=no_legistar_id_in_row_links", row_key)
    return ""


def _extract_meeting_date(raw_value: str, row_key: str, counters: Counter[str]) -> str:
    value = _clean_text(raw_value)
    if not value:
        counters["meeting_date_absent"] += 1
        logger.warning("field_absent row=%s field=meeting_date reason=empty_date_cell", row_key)
        return ""

    numeric = DATE_NUMERIC_RE.search(value)
    if numeric:
        month = int(numeric.group(1))
        day = int(numeric.group(2))
        year = int(numeric.group(3))
        if year < 100:
            year += 2000
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            pass

    month_date = MONTH_DATE_RE.search(value[:100])
    if month_date:
        for fmt in ("%b %d %Y", "%B %d %Y"):
            candidate = f"{month_date.group(1).rstrip('.')} {month_date.group(2)} {month_date.group(3)}"
            try:
                return datetime.strptime(candidate, fmt).date().isoformat()
            except ValueError:
                continue

    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        counters["meeting_date_parse_failed"] += 1
        logger.warning(
            "field_parse_failed row=%s field=meeting_date raw=%r reason=unrecognized_date_format",
            row_key,
            raw_value,
        )
        return ""


def _extract_meeting_time(raw_value: str, row_key: str, counters: Counter[str]) -> str:
    value = _clean_text(raw_value)
    if not value:
        counters["meeting_time_absent"] += 1
        return ""

    match = TIME_RE.search(value[:80])
    if not match:
        counters["meeting_time_parse_failed"] += 1
        logger.warning(
            "field_parse_failed row=%s field=meeting_time raw=%r reason=unrecognized_time_format",
            row_key,
            raw_value,
        )
        return ""

    hour = int(match.group(1))
    minute = match.group(2) or "00"
    suffix = f"{match.group(3).upper()}M"
    return f"{hour}:{minute} {suffix}"


def _derive_status(
    title: str,
    agenda_url: str,
    agenda_packet_url: str,
    minutes_url: str,
    row_key: str,
    counters: Counter[str],
    vendor_status: str = "",
) -> str:
    if CANCELLED_RE.search(title[:300]):
        status = "Cancelled"
    elif minutes_url:
        status = "Minutes Available"
    elif agenda_url or agenda_packet_url:
        status = "Agenda Available"
    else:
        status = "Scheduled"

    if vendor_status:
        counters["vendor_status_observed"] += 1
        logger.warning(
            "vendor_status_observed_not_authoritative row=%s vendor_status=%r "
            "canonical_status=%s evidence=title_cancelled:%s agenda:%s packet:%s minutes:%s",
            row_key,
            vendor_status,
            status,
            bool(CANCELLED_RE.search(title[:300])),
            bool(agenda_url),
            bool(agenda_packet_url),
            bool(minutes_url),
        )
    return status


def _clean_text(value: object) -> str:
    if isinstance(value, Tag):
        text = value.get_text(" ", strip=True)
    else:
        text = BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)
    return " ".join(html.unescape(text).split())


def _normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _is_unsafe_host(host: str) -> bool:
    if not host:
        return True
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _validate_schema(meeting: dict[str, str], row_key: str) -> None:
    if tuple(meeting.keys()) != CANONICAL_FIELDS:
        raise ValueError(f"{row_key} unexpected schema fields/order: {list(meeting.keys())!r}")
    for field, value in meeting.items():
        if not isinstance(value, str):
            raise TypeError(f"{row_key} {field} must be str, got {type(value).__name__}")
    for field in URL_FIELDS:
        value = meeting[field]
        if value and not value.startswith(("http://", "https://")):
            raise ValueError(f"{row_key} {field} is not absolute HTTP(S): {value!r}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s:%(name)s:%(message)s")
    print(json.dumps(scrape_calendar(DEFAULT_CALENDAR_URL), indent=2))
