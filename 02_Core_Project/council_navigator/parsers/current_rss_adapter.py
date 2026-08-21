"""Bounded current-window adapters for official RSS civic calendars."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import html
import logging
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from defusedxml import ElementTree as ET
from requests import RequestException

from polite_http import make_session


logger = logging.getLogger(__name__)

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
MAX_RESPONSE_BYTES = 2_000_000
CHUNK_SIZE = 64 * 1024
GRANICUS_NS = "https://www.granicus.com/schema/rss-supplements"
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
GRANICUS_TITLE_RE = re.compile(
    r"\s+-\s+([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{4})\s*$"
)
GRANICUS_DESCRIPTION_DATE_RE = re.compile(
    r"\bdated\s+[A-Za-z]+,\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\b",
    re.IGNORECASE,
)
CIVICPLUS_TITLE_DATE_RE = re.compile(
    r"^([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})\s+(.+)$"
)
TIME_RE = re.compile(
    r"(?<!\d)(1[0-2]|0?[1-9]):([0-5]\d)\s*([AaPp])\.?\s*[Mm]\.?(?=\s|$|[^\w.])"
)
CLIP_ID_RE = re.compile(r"(?:[?&])clip_id=(\d+)(?:&|$)", re.IGNORECASE)
PREVIOUS_VERSION_ID_RE = re.compile(r"/PreviousVersions/(\d+)(?:/|$)", re.IGNORECASE)
MONTHS = {
    datetime(2000, month, 1).strftime("%b").lower(): month
    for month in range(1, 13)
}
MONTHS.update(
    {
        datetime(2000, month, 1).strftime("%B").lower(): month
        for month in range(1, 13)
    }
)


class SourceBlocked(RuntimeError):
    """Official source could not be safely fetched or witnessed."""


def scrape_granicus_rss(
    calendar_url: str,
    *,
    expected_host: str,
    title_allowed,
) -> list[dict]:
    """Return current-month-forward governing-body rows from one Granicus feed."""
    rss_url = _granicus_rss_url(calendar_url, expected_host)
    floor = date.today().replace(day=1)
    stats: Counter[str] = Counter()
    logger.warning(
        "Granicus RSS does not expose meeting_location or ecomment_url; "
        "meeting_time remains empty unless visible title/description text supplies it"
    )

    with make_session() as session:
        primary_text = _fetch_text_bounded(session, rss_url, {expected_host})
        primary_root = _parse_xml(primary_text, rss_url)
        _validate_granicus(primary_root, primary_text, rss_url)
        minutes = _fetch_granicus_minutes(session, rss_url, expected_host, stats)

    meetings: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for index, item in enumerate(primary_root.findall("./channel/item"), start=1):
        stats["rows_seen"] += 1
        row = f"item:{index}"
        raw_title = _item_text(item, "title")
        title_date = _parse_granicus_title(raw_title, row)
        if title_date is None:
            stats["rows_dropped_bad_title_date"] += 1
            continue
        meeting_title, meeting_date = title_date
        description_html = _item_text(item, "description")
        description_text = _clean_text(description_html)
        description_date = _granicus_description_date(description_text)
        if description_date != meeting_date:
            raise ValueError(
                "Granicus title/description date disagreement: "
                f"row={row} title_date={meeting_date!r} "
                f"description_date={description_date!r} title={meeting_title!r}"
            )
        _warn_pubdate_difference(item, meeting_date, row)
        meeting_day = date.fromisoformat(meeting_date)
        if meeting_day < floor:
            stats["rows_dropped_before_floor"] += 1
            continue
        if not title_allowed(meeting_title):
            stats["rows_dropped_non_governing_body"] += 1
            logger.warning(
                "Granicus row dropped without flagship governing-body evidence: "
                "row=%s date=%s title=%r",
                row,
                meeting_date,
                meeting_title,
            )
            continue

        video_url = _granicus_video_url(item, rss_url, expected_host, row)
        meeting_id = _clip_id(video_url) or _clean_text(_item_text(item, "guid"))
        minutes_url = minutes.get(meeting_id, "")
        agenda_url = video_url if "the agenda for" in description_text.lower() else ""
        meeting_time = _visible_time(f"{meeting_title} {description_text}", row)
        record = {
            "meeting_title": meeting_title,
            "meeting_date": meeting_date,
            "meeting_time": meeting_time,
            "meeting_location": "",
            "meeting_status": _status(meeting_title, agenda_url, "", minutes_url),
            "agenda_url": agenda_url,
            "minutes_url": minutes_url,
            "video_url": video_url,
            "agenda_packet_url": "",
            "ecomment_url": "",
            "meeting_id": meeting_id,
        }
        _validate_record(record)
        key = (meeting_date, meeting_title, meeting_id)
        if key in seen:
            stats["duplicates_dropped"] += 1
            logger.warning("Granicus duplicate row dropped key=%r", key)
            continue
        seen.add(key)
        meetings.append(record)
        stats["rows_emitted"] += 1

    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Official Granicus feed is accessible with no current-month-forward "
            "flagship governing-body rows; floor=%s stats=%s",
            floor.isoformat(),
            dict(stats),
        )
    logger.info("Granicus current-window scrape summary: %s", dict(stats))
    return sorted(meetings, key=lambda row: (row["meeting_date"], row["meeting_title"]))


def scrape_civicplus_rss(
    rss_url: str,
    *,
    expected_host: str,
    title_allowed,
) -> list[dict]:
    """Return current-month-forward governing-body documents from CivicPlus RSS."""
    floor = date.today().replace(day=1)
    stats: Counter[str] = Counter()
    logger.warning(
        "CivicPlus AgendaCenter RSS does not expose meeting_time, "
        "meeting_location, video_url, or ecomment_url"
    )
    with make_session() as session:
        raw_xml = _fetch_text_bounded(session, rss_url, {expected_host})
    root = _parse_xml(raw_xml, rss_url)
    items = root.findall("./channel/item")
    channel = root.find("./channel")
    if root.tag != "rss" or channel is None:
        raise ValueError("CivicPlus RSS fingerprint disappeared")
    channel_link = _clean_text(channel.findtext("link") or "")
    if (urlparse(channel_link).hostname or "").lower() != expected_host:
        raise ValueError(f"CivicPlus RSS channel host changed: {channel_link!r}")
    logger.info(
        "vendor fingerprint witness=CivicPlus_RSSFeed_plus_AgendaCenter "
        "items=%d channel_link=%s",
        len(items),
        channel_link,
    )

    meetings: list[dict] = []
    for index, item in enumerate(items, start=1):
        stats["rows_seen"] += 1
        row = f"item:{index}"
        raw_title = _clean_text(_item_text(item, "title"))
        match = CIVICPLUS_TITLE_DATE_RE.match(raw_title)
        if match is None:
            stats["rows_dropped_no_exact_date"] += 1
            logger.warning(
                "CivicPlus row dropped: row=%s title=%r reason=no_exact_day_level_date",
                row,
                raw_title,
            )
            continue
        month_name, day_text, year_text, document_title = match.groups()
        meeting_date = _iso_date(month_name, day_text, year_text)
        if date.fromisoformat(meeting_date) < floor:
            stats["rows_dropped_before_floor"] += 1
            continue
        meeting_title = _civicplus_meeting_title(document_title)
        if not title_allowed(meeting_title):
            stats["rows_dropped_non_governing_body"] += 1
            logger.warning(
                "CivicPlus row dropped without flagship governing-body evidence: "
                "row=%s date=%s title=%r",
                row,
                meeting_date,
                meeting_title,
            )
            continue
        link = _emit_url(_item_text(item, "link"), rss_url, {expected_host}, row, "document_url")
        if not link:
            raise ValueError(f"CivicPlus accepted row lacks document URL: {row}")
        agenda_url = link if "agenda" in document_title.lower() else ""
        minutes_url = link if "minute" in document_title.lower() else ""
        packet_url = link if "packet" in document_title.lower() else ""
        guid = _item_text(item, "guid")
        id_match = PREVIOUS_VERSION_ID_RE.search(link) or PREVIOUS_VERSION_ID_RE.search(guid)
        meeting_id = id_match.group(1) if id_match else _clean_text(guid)
        record = {
            "meeting_title": meeting_title,
            "meeting_date": meeting_date,
            "meeting_time": "",
            "meeting_location": "",
            "meeting_status": _status(meeting_title, agenda_url, packet_url, minutes_url),
            "agenda_url": agenda_url,
            "minutes_url": minutes_url,
            "video_url": "",
            "agenda_packet_url": packet_url,
            "ecomment_url": "",
            "meeting_id": meeting_id,
        }
        _validate_record(record)
        meetings.append(record)
        stats["rows_emitted"] += 1

    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Official CivicPlus Town Council RSS is accessible with no "
            "current-month-forward meeting rows; floor=%s stats=%s",
            floor.isoformat(),
            dict(stats),
        )
    logger.info("CivicPlus current-window scrape summary: %s", dict(stats))
    return sorted(meetings, key=lambda row: (row["meeting_date"], row["meeting_title"]))


def _fetch_text_bounded(session, url: str, allowed_hosts: set[str]) -> str:
    input_host = (urlparse(url).hostname or "").lower()
    if input_host not in allowed_hosts:
        raise ValueError(f"Input host is not allowlisted: {input_host!r}")
    try:
        response_context = session.get(
            url, timeout=(10, 30), stream=True, allow_redirects=True
        )
    except RequestException as exc:
        raise SourceBlocked(f"request_failed url={url} error={exc}") from exc
    with response_context as response:
        final_host = (urlparse(response.url).hostname or "").lower()
        if final_host not in allowed_hosts:
            raise ValueError(
                f"Redirect to disallowed host: {final_host!r} (started from {url})"
            )
        try:
            response.raise_for_status()
        except RequestException as exc:
            raise SourceBlocked(
                f"http_status_failed url={url} status={response.status_code} error={exc}"
            ) from exc
        body = bytearray()
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise SourceBlocked(
                    f"response_too_large url={url} max_bytes={MAX_RESPONSE_BYTES}"
                )
        return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _parse_xml(raw_xml: str, source_url: str) -> ET.Element:
    try:
        return ET.fromstring(raw_xml.encode("utf-8"))
    except ET.ParseError as exc:
        raise SourceBlocked(
            f"xml_parse_failed url={source_url} bytes={len(raw_xml.encode('utf-8'))}"
        ) from exc


def _granicus_rss_url(calendar_url: str, expected_host: str) -> str:
    parsed = urlparse(calendar_url)
    if (parsed.hostname or "").lower() != expected_host:
        raise ValueError(f"Unexpected Granicus tenant host: {parsed.hostname!r}")
    query = parse_qs(parsed.query)
    view_id = (query.get("view_id") or [""])[0]
    if not view_id.isdigit():
        raise ValueError(f"Granicus URL is missing numeric view_id: {calendar_url!r}")
    return urlunparse(
        parsed._replace(
            path="/ViewPublisherRSS.php",
            query=urlencode({"view_id": view_id}),
            fragment="",
        )
    )


def _validate_granicus(root: ET.Element, raw_xml: str, source_url: str) -> None:
    items = root.findall("./channel/item")
    witnessed = {
        "rss_root": root.tag == "rss",
        "channel": root.find("./channel") is not None,
        "xmlns_gran": "xmlns:gran" in raw_xml,
        "pubDateParts": not items or any(
            item.find(f"{{{GRANICUS_NS}}}pubDateParts") is not None
            for item in items
        ),
        "viewpublisher_path": "ViewPublisherRSS.php" in urlparse(source_url).path,
    }
    missing = [key for key, value in witnessed.items() if not value]
    if missing:
        raise ValueError(f"Granicus RSS fingerprint missing tokens: {missing}")
    logger.info(
        "vendor fingerprint witness=Granicus_RSS_plus_pubDateParts "
        "url=%s items=%d",
        source_url,
        len(items),
    )


def _fetch_granicus_minutes(
    session, rss_url: str, expected_host: str, stats: Counter[str]
) -> dict[str, str]:
    parsed = urlparse(rss_url)
    query = parse_qs(parsed.query)
    query["mode"] = ["minutes"]
    companion_url = urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
    try:
        raw_xml = _fetch_text_bounded(session, companion_url, {expected_host})
        root = _parse_xml(raw_xml, companion_url)
        _validate_granicus(root, raw_xml, companion_url)
    except SourceBlocked as exc:
        logger.warning(
            "Granicus companion minutes source blocked; continuing without "
            "minutes_url enrichment: %s",
            exc,
        )
        return {}
    result: dict[str, str] = {}
    for index, item in enumerate(root.findall("./channel/item"), start=1):
        row = f"minutes:{index}"
        link = _emit_url(
            _item_text(item, "link"), companion_url, {expected_host}, row, "minutes_url"
        )
        clip_id = _clip_id(link)
        if not link or not clip_id or "minutesviewer.php" not in link.lower():
            stats["minutes_rows_dropped"] += 1
            logger.warning(
                "Granicus minutes row dropped: row=%s link=%r clip_id=%r",
                row,
                link,
                clip_id,
            )
            continue
        result[clip_id] = link
    stats["minutes_rows_mapped"] = len(result)
    return result


def _parse_granicus_title(raw_title: str, row: str) -> tuple[str, str] | None:
    clean_title = _clean_text(raw_title)
    match = GRANICUS_TITLE_RE.search(clean_title)
    if match is None:
        logger.warning(
            "Granicus row dropped: row=%s title=%r reason=no_exact_trailing_date",
            row,
            clean_title,
        )
        return None
    month_name, day_text, year_text = match.groups()
    title = GRANICUS_TITLE_RE.sub("", clean_title).strip()
    return title, _iso_date(month_name, day_text, year_text)


def _granicus_description_date(description: str) -> str:
    match = GRANICUS_DESCRIPTION_DATE_RE.search(description[:1200])
    if match is None:
        return ""
    day_text, month_name, year_text = match.groups()
    return _iso_date(month_name, day_text, year_text)


def _iso_date(month_name: str, day_text: str, year_text: str) -> str:
    month = MONTHS.get(month_name.lower())
    if month is None:
        raise ValueError(f"Unknown month name: {month_name!r}")
    return date(int(year_text), month, int(day_text)).isoformat()


def _warn_pubdate_difference(item: ET.Element, meeting_date: str, row: str) -> None:
    raw_pubdate = _item_text(item, "pubDate")
    if not raw_pubdate:
        logger.warning("Granicus pubDate absent: row=%s", row)
        return
    try:
        from email.utils import parsedate_to_datetime

        pubdate = parsedate_to_datetime(raw_pubdate).date().isoformat()
    except (TypeError, ValueError):
        logger.warning("Granicus pubDate rejected: row=%s value=%r", row, raw_pubdate)
        return
    if pubdate != meeting_date:
        logger.warning(
            "Granicus pubDate not used as meeting date: row=%s "
            "meeting_date=%s publication_date=%s",
            row,
            meeting_date,
            pubdate,
        )


def _granicus_video_url(
    item: ET.Element, base_url: str, expected_host: str, row: str
) -> str:
    candidates = [_item_text(item, "link")]
    description = BeautifulSoup(_item_text(item, "description"), "html.parser")
    candidates.extend(str(anchor.get("href") or "") for anchor in description.select("a"))
    for raw_url in candidates:
        if "mediaplayer.php" not in raw_url.lower():
            continue
        emitted = _emit_url(raw_url, base_url, {expected_host}, row, "video_url")
        if emitted:
            return emitted
    logger.warning("Granicus video_url absent: row=%s reason=no_MediaPlayer_link", row)
    return ""


def _emit_url(
    raw_url: str,
    base_url: str,
    allowed_hosts: set[str],
    row: str,
    field: str,
) -> str:
    href = (raw_url or "").strip()
    if not href or href.lower().startswith(
        ("javascript:", "data:", "vbscript:", "file:", "mailto:", "ftp:", "gopher:")
    ):
        logger.warning("URL rejected: row=%s field=%s value=%r", row, field, raw_url)
        return ""
    absolute = urljoin(base_url, href)
    parsed = urlparse(absolute)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in allowed_hosts:
        logger.warning("URL rejected: row=%s field=%s value=%r", row, field, absolute)
        return ""
    return absolute


def _clip_id(url: str) -> str:
    match = CLIP_ID_RE.search(url or "")
    return match.group(1) if match else ""


def _visible_time(value: str, row: str) -> str:
    match = TIME_RE.search(value[:3000])
    if match is None:
        logger.info(
            "meeting_time honest-empty: row=%s reason=no_visible_title_or_description_time",
            row,
        )
        return ""
    hour = int(match.group(1))
    return f"{hour}:{match.group(2)} {match.group(3).upper()}M"


def _civicplus_meeting_title(document_title: str) -> str:
    clean = re.sub(r"\s*\(PDF\)\s*$", "", document_title, flags=re.IGNORECASE)
    clean = re.sub(r"\s+Update:.*$", "", clean, flags=re.IGNORECASE)
    for suffix in (
        "Agenda and Packet Material",
        "Packet Material",
        "Agenda",
        "Minutes",
    ):
        clean = re.sub(
            rf"\s+{re.escape(suffix)}\s*$",
            "",
            clean,
            flags=re.IGNORECASE,
        )
    return _clean_text(clean)


def _status(title: str, agenda_url: str, packet_url: str, minutes_url: str) -> str:
    if CANCELLED_RE.search(title):
        return "Cancelled"
    if minutes_url:
        return "Minutes Available"
    if agenda_url or packet_url:
        return "Agenda Available"
    return "Scheduled"


def _item_text(item: ET.Element, name: str) -> str:
    return (item.findtext(name) or "").strip()


def _clean_text(value: str) -> str:
    unescaped = html.unescape(value or "")
    if "<" not in unescaped:
        return " ".join(unescaped.split())
    return " ".join(
        BeautifulSoup(unescaped, "html.parser").get_text(" ", strip=True).split()
    )


def _validate_record(record: dict[str, str]) -> None:
    if tuple(record) != CANONICAL_FIELDS:
        raise ValueError(f"RSS adapter emitted noncanonical fields: {tuple(record)}")
    if any(not isinstance(value, str) for value in record.values()):
        raise TypeError("RSS adapter emitted a non-string field")
