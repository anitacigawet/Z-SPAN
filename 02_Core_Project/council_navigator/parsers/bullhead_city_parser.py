"""Bullhead City Granicus RSS parser."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from email.utils import parsedate_to_datetime
import html as html_lib
import json
import logging
import re
from typing import Any, Iterable
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
from defusedxml import ElementTree as ET
from requests.exceptions import RequestException

from polite_http import make_session


logger = logging.getLogger(__name__)

DEFAULT_CALENDAR_URL = "https://bullheadcity.granicus.com/ViewPublisherRSS.php?view_id=2"
MAX_RESPONSE_BYTES = 5_000_000
MAX_RSS_ITEMS = 200
ALLOWED_HOSTS = {
    "bullheadcity.granicus.com",
    "archive-video.granicus.com",
    "archive-media.granicus.com",
    "granicus.com",
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

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
MONTH_DATE_RE = re.compile(
    r"\b("
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
    r")\.?\s+([0-3]?\d)(?:,|\s)\s*(\d{4})\b",
    re.IGNORECASE,
)
NUMERIC_DATE_RE = re.compile(r"(?<!\d)([01]?\d)[/-]([0-3]?\d)[/-](\d{2}|\d{4})(?!\d)")
# Time regex test cases: "5:30 a.m.", "5:30 p.m.", "5:30am", "5:30 AM".
TIME_RE = re.compile(
    r"(?<!\d)(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([AaPp])\.?\s*[Mm]\.?(?=\s|$|[^\w.])"
)
CLIP_ID_RE = re.compile(r"(?:[?&]|^)clip_id=(\d+)(?:&|$)", re.IGNORECASE)
ONCLICK_URL_RE = re.compile(r"""['"]((?:https?:)?//[^'"]+|https?://[^'"]+|/[^'"]+)['"]""")

FIELD_TAG_ALIASES = {
    "agenda_url": {"agenda", "agendaurl", "agendalink", "agendahref"},
    "minutes_url": {"minutes", "minutesurl", "minuteslink", "minuteshref"},
    "video_url": {"video", "videourl", "videolink", "mediaurl", "media", "mp4", "asx"},
    "agenda_packet_url": {"agendapacket", "agendapacketurl", "packet", "packeturl", "packetlink"},
    "ecomment_url": {"ecomment", "ecommenturl", "publiccomment", "publiccommenturl", "commenturl"},
}


def scrape_calendar(calendar_url: str) -> list[dict]:
    """Scrape Bullhead City meetings from the Granicus RSS calendar."""
    _declare_structural_absences(calendar_url)
    session = make_session()
    cutoff = date.today().replace(day=1)

    try:
        raw_xml = _fetch_text_bounded(session, calendar_url)
    except RequestException as exc:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "architectural_blocker_network_fetch_failed returning honest-empty [] "
            "url=%r error=%s missing_scope=all_rss_items",
            calendar_url,
            exc,
        )
        return []

    root = ET.fromstring(raw_xml.encode("utf-8"))
    _validate_granicus_rss_surface(root, raw_xml, calendar_url)

    items = list(_iter_items(root))
    if len(items) > MAX_RSS_ITEMS:
        raise RuntimeError(
            f"Bullhead City RSS exceeded the {MAX_RSS_ITEMS}-item safety cap: {len(items)}"
        )

    counters: Counter[str] = Counter()
    meetings: list[dict] = []

    for index, item in enumerate(items, start=1):
        counters["rows_seen"] += 1
        row_key = _row_key(index, item)
        meeting = _build_meeting(item, calendar_url, row_key, counters)

        if not _is_iso_date(meeting["meeting_date"]):
            counters["rows_dropped"] += 1
            counters["drop_missing_iso_date"] += 1
            logger.warning(
                "row_dropped reason=missing_iso_meeting_date row=%s title=%r extracted_date=%r",
                row_key,
                meeting["meeting_title"],
                meeting["meeting_date"],
            )
            continue

        meeting_day = date.fromisoformat(meeting["meeting_date"])
        if meeting_day < cutoff:
            counters["rows_dropped"] += 1
            counters["drop_before_current_month"] += 1
            logger.info(
                "row_dropped row=%s title=%r date=%s reason=before_current_month cutoff=%s",
                row_key,
                meeting["meeting_title"],
                meeting["meeting_date"],
                cutoff.isoformat(),
            )
            continue

        body_decision = _city_council_title_decision(meeting["meeting_title"])
        if body_decision == "subordinate":
            counters["rows_dropped"] += 1
            counters["drop_known_subordinate_body"] += 1
            logger.info(
                "row_dropped row=%s title=%r reason=known_subordinate_body",
                row_key,
                meeting["meeting_title"],
            )
            continue
        if body_decision != "council":
            raise RuntimeError(
                f"Bullhead City current governing-body vocabulary drift at {row_key}: "
                f"{meeting['meeting_title']!r}"
            )

        _validate_schema(meeting, row_key)
        meetings.append(meeting)
        counters["rows_accepted"] += 1
        logger.info(
            "row_accepted row=%s title=%r date=%s status=%s urls=%s",
            row_key,
            meeting["meeting_title"],
            meeting["meeting_date"],
            meeting["meeting_status"],
            {field: meeting[field] for field in URL_FIELDS if meeting[field]},
        )

    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Bullhead City official RSS contained no City Council rows from %s forward",
            cutoff.isoformat(),
        )

    logger.warning(
        "scrape_summary rows_seen=%d rows_accepted=%d rows_dropped=%d "
        "time_absent=%d location_absent=%d ecomment_absent=%d meeting_id_absent=%d",
        counters["rows_seen"],
        counters["rows_accepted"],
        counters["rows_dropped"],
        counters["meeting_time_absent"],
        counters["meeting_location_absent"],
        counters["ecomment_url_absent"],
        counters["meeting_id_absent"],
    )
    return meetings


def _city_council_title_decision(title: str) -> str:
    folded = " ".join(title.casefold().split())
    if re.search(r"(?<!\w)city council(?!\w)", folded):
        return "council"
    subordinate_terms = (
        "board",
        "commission",
        "committee",
        "authority",
        "advisory",
        "planning",
    )
    if any(term in folded for term in subordinate_terms):
        return "subordinate"
    return "unknown"


def _declare_structural_absences(url: str) -> None:
    logger.warning(
        "startup_absence_declaration vendor=granicus_rss url=%r "
        "meeting_time_source=visible_title_or_description_only "
        "pubDate_and_gran_pubDateParts_not_trusted_for_meeting_time "
        "meeting_location_absent_by_construction "
        "ecomment_absent_unless_explicit_url_present",
        url,
    )


def _fetch_text_bounded(session: Any, url: str) -> str:
    start_host = _host(url)
    if not _host_allowed(start_host):
        raise ValueError(f"Input URL host is not allowed: {start_host!r}")

    with session.get(url, timeout=30, stream=True, allow_redirects=True) as response:
        response.raise_for_status()
        final_host = _host(response.url)
        if not _host_allowed(final_host):
            raise ValueError(f"Redirect to disallowed host: {final_host!r} started_from={url!r}")

        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Response from {url!r} exceeded {MAX_RESPONSE_BYTES} bytes")

        return body.decode(response.encoding or "utf-8", errors="replace")


def _validate_granicus_rss_surface(root: ET.Element, raw_xml: str, source_url: str) -> None:
    witnessed = {
        "path_ViewPublisherRSS": "ViewPublisherRSS.php" in urlparse(source_url).path,
        "rss_root": _local_name(root.tag).lower() == "rss",
        "channel": root.find("./channel") is not None,
        "channel_item": root.find("./channel/item") is not None,
        "granicus_namespace": "granicus.com/schema/rss-supplements" in raw_xml,
        "pubDateParts": "pubDateParts" in raw_xml,
    }
    if not (witnessed["path_ViewPublisherRSS"] and witnessed["rss_root"] and witnessed["channel"]):
        raise ValueError(f"Unexpected Bullhead City Granicus RSS surface: {witnessed}")
    logger.info("vendor_fingerprint_witness vendor=granicus_rss witnesses=%s", witnessed)


def _iter_items(root: ET.Element) -> Iterable[ET.Element]:
    yield from root.findall("./channel/item")


def _build_meeting(
    item: ET.Element,
    base_url: str,
    row_key: str,
    counters: Counter[str],
) -> dict[str, str]:
    title = _clean_text(_child_text(item, "title"))
    if not title:
        counters["meeting_title_absent"] += 1
        logger.warning("field_absent row=%s field=meeting_title reason=missing_title_element", row_key)

    description_html = _child_text(item, "description")
    description_text = _html_to_text(description_html)
    date_value = _extract_meeting_date(item, title, description_text, row_key, counters)
    time_value = _extract_meeting_time(title, description_text, row_key, counters)

    urls = _extract_urls(item, description_html, base_url, row_key, counters)
    meeting_id = _extract_meeting_id(item, urls, row_key, counters)
    status = _derive_status(title, urls, row_key)

    counters["meeting_location_absent"] += 1

    return {
        "meeting_title": title,
        "meeting_date": date_value,
        "meeting_time": time_value,
        "meeting_location": "",
        "meeting_status": status,
        "agenda_url": urls["agenda_url"],
        "minutes_url": urls["minutes_url"],
        "video_url": urls["video_url"],
        "agenda_packet_url": urls["agenda_packet_url"],
        "ecomment_url": urls["ecomment_url"],
        "meeting_id": meeting_id,
    }


def _extract_meeting_date(
    item: ET.Element,
    title: str,
    description: str,
    row_key: str,
    counters: Counter[str],
) -> str:
    visible_dates = _dates_from_text(f"{title} {description}")
    pub_date = _date_from_pubdate(_child_text(item, "pubDate"), row_key)
    parts_date = _date_from_pubdate_parts(item, row_key)

    if visible_dates:
        chosen = visible_dates[0]
        for other in visible_dates[1:]:
            if other != chosen:
                logger.warning(
                    "date_signal_conflict row=%s source=visible_text first=%s other=%s action=using_first_visible",
                    row_key,
                    chosen,
                    other,
                )
        if pub_date and pub_date != chosen:
            logger.warning(
                "date_signal_conflict row=%s visible_date=%s pubDate_date=%s action=using_visible_date",
                row_key,
                chosen,
                pub_date,
            )
        if parts_date and parts_date != chosen:
            logger.warning(
                "date_signal_conflict row=%s visible_date=%s gran_pubDateParts_date=%s action=using_visible_date",
                row_key,
                chosen,
                parts_date,
            )
        return chosen

    if pub_date and parts_date and pub_date != parts_date:
        counters["meeting_date_ambiguous"] += 1
        logger.warning(
            "date_signal_conflict row=%s pubDate_date=%s gran_pubDateParts_date=%s action=emit_empty",
            row_key,
            pub_date,
            parts_date,
        )
        return ""

    if pub_date:
        return pub_date
    if parts_date:
        logger.warning(
            "date_signal_single_source row=%s source=gran_pubDateParts action=using_date_only_not_time",
            row_key,
        )
        return parts_date

    counters["meeting_date_absent"] += 1
    logger.warning("field_absent row=%s field=meeting_date reason=no_visible_or_rss_date_signal", row_key)
    return ""


def _extract_meeting_time(title: str, description: str, row_key: str, counters: Counter[str]) -> str:
    text = _cap_text(f"{title} {description}", 4_000)
    match = TIME_RE.search(text)
    if not match:
        counters["meeting_time_absent"] += 1
        logger.warning(
            "field_absent row=%s field=meeting_time reason=no_visible_title_or_description_time",
            row_key,
        )
        return ""

    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    suffix = match.group(3).upper()
    if suffix == "P" and hour != 12:
        hour += 12
    if suffix == "A" and hour == 12:
        hour = 0
    normalized = datetime(2000, 1, 1, hour, minute).strftime("%I:%M %p").lstrip("0")
    logger.info("field_emitted row=%s field=meeting_time value=%r source=visible_text", row_key, normalized)
    return normalized


def _extract_urls(
    item: ET.Element,
    description_html: str,
    base_url: str,
    row_key: str,
    counters: Counter[str],
) -> dict[str, str]:
    urls = {field: "" for field in URL_FIELDS}

    for element in list(item):
        local = _normalize_key(_local_name(element.tag))
        text = _clean_text(element.text or "")
        if not text:
            continue
        field = _field_from_tag(local)
        if field:
            _assign_url(urls, field, text, base_url, row_key, f"tag:{_local_name(element.tag)}", counters)
        elif local in {"link", "guid"}:
            _classify_and_assign_url(urls, text, base_url, row_key, f"tag:{local}", counters)

    for enclosure in item.findall("enclosure"):
        href = enclosure.attrib.get("url", "")
        media_type = enclosure.attrib.get("type", "")
        if href:
            field = "video_url" if media_type.startswith("video/") else _classify_url(href, "enclosure")
            if field:
                _assign_url(urls, field, href, base_url, row_key, "tag:enclosure", counters)
            else:
                _warn_unclassified_url(row_key, href, "tag:enclosure", counters)

    for label, href in _description_link_candidates(description_html, row_key, counters):
        _classify_and_assign_url(urls, href, base_url, row_key, f"description:{label}", counters)

    for field in URL_FIELDS:
        if not urls[field]:
            counters[f"{field}_absent"] += 1
    return urls


def _assign_url(
    urls: dict[str, str],
    field: str,
    href: str,
    base_url: str,
    row_key: str,
    source: str,
    counters: Counter[str],
) -> None:
    emitted = _emit_url(href, base_url, field, row_key, source, counters)
    if not emitted:
        return
    if urls[field] and urls[field] != emitted:
        logger.warning(
            "url_conflict row=%s field=%s existing=%r rejected_additional=%r source=%s",
            row_key,
            field,
            urls[field],
            emitted,
            source,
        )
        return
    urls[field] = emitted
    logger.info("field_emitted row=%s field=%s value=%r source=%s", row_key, field, emitted, source)


def _classify_and_assign_url(
    urls: dict[str, str],
    href: str,
    base_url: str,
    row_key: str,
    source: str,
    counters: Counter[str],
) -> None:
    field = _classify_url(href, source)
    if not field:
        _warn_unclassified_url(row_key, href, source, counters)
        return
    _assign_url(urls, field, href, base_url, row_key, source, counters)


def _classify_url(href: str, label: str) -> str:
    absolute_hint = urljoin(DEFAULT_CALENDAR_URL, href)
    parsed = urlparse(absolute_hint)
    path = parsed.path.lower()
    query = {key.lower(): values for key, values in parse_qs(parsed.query).items()}
    host = parsed.netloc.lower()
    label_low = label.lower()
    url_low = f"{host} {path} {parsed.query}".lower()

    if "ecomment" in url_low or "publiccomment" in url_low or "ecomment" in label_low:
        return "ecomment_url"
    if "minutes" in url_low or re.search(r"\bminutes\b", label_low):
        return "minutes_url"
    if re.search(r"\bpacket\b", url_low) or re.search(r"\bpacket\b", label_low):
        return "agenda_packet_url"
    if "agenda" in url_low or re.search(r"\bagenda\b", label_low):
        return "agenda_url"
    if "mediaplayer.php" in path or host == "archive-video.granicus.com":
        return "video_url"
    if path.endswith((".mp4", ".asx", ".m3u8")):
        return "video_url"
    if "clip_id" in query:
        logger.warning(
            "url_classification_ambiguous source=%s href=%r reason=clip_id_without_media_or_document_path",
            label,
            href,
        )
    return ""


def _emit_url(
    href: str,
    base_url: str,
    field: str,
    row_key: str,
    source: str,
    counters: Counter[str],
) -> str:
    if not href:
        return ""

    stripped = href.strip()
    low = stripped.lower()
    for bad_scheme in BAD_SCHEMES:
        if low.startswith(bad_scheme):
            counters["url_rejected_bad_scheme"] += 1
            logger.warning(
                "url_rejected row=%s field=%s source=%s reason=bad_scheme rejected=%r",
                row_key,
                field,
                source,
                href,
            )
            return ""

    if not _looks_like_href(stripped):
        counters["url_rejected_not_url_like"] += 1
        logger.warning(
            "url_rejected row=%s field=%s source=%s reason=not_url_like rejected=%r",
            row_key,
            field,
            source,
            href,
        )
        return ""

    absolute = urljoin(base_url, stripped)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        counters["url_rejected_bad_scheme"] += 1
        logger.warning(
            "url_rejected row=%s field=%s source=%s reason=non_http_scheme rejected=%r",
            row_key,
            field,
            source,
            href,
        )
        return ""

    host = _host(absolute)
    if not _host_allowed(host):
        counters["url_rejected_disallowed_host"] += 1
        logger.warning(
            "url_rejected row=%s field=%s source=%s reason=disallowed_host host=%r rejected=%r",
            row_key,
            field,
            source,
            host,
            href,
        )
        return ""

    return absolute


def _description_link_candidates(
    description_html: str,
    row_key: str,
    counters: Counter[str],
) -> Iterable[tuple[str, str]]:
    if not description_html:
        return

    soup = BeautifulSoup(description_html, "html.parser")
    for anchor in soup.find_all("a"):
        label = _clean_text(anchor.get_text(" ", strip=True)) or "unlabeled_anchor"
        href = (anchor.get("href") or "").strip()
        if href and not _is_placeholder_href(href):
            yield label, href
            continue

        fallback_urls: list[str] = []
        for attr_name, attr_value in anchor.attrs.items():
            if attr_name == "href" or attr_value is None:
                continue
            values = attr_value if isinstance(attr_value, list) else [str(attr_value)]
            for value in values:
                if attr_name == "onclick":
                    fallback_urls.extend(ONCLICK_URL_RE.findall(_cap_text(value, 2_000)))
                elif any(token in attr_name.lower() for token in ("url", "href", "src")):
                    fallback_urls.append(value)

        if fallback_urls:
            for fallback in fallback_urls:
                yield label, fallback
        elif href:
            counters["placeholder_href_without_fallback"] += 1
            logger.warning(
                "url_placeholder_without_fallback row=%s label=%r rejected_href=%r checked=onclick_data_attrs",
                row_key,
                label,
                href,
            )


def _warn_unclassified_url(row_key: str, href: str, source: str, counters: Counter[str]) -> None:
    counters["url_unclassified"] += 1
    logger.warning(
        "url_unclassified row=%s source=%s rejected=%r reason=no_known_granicus_url_fingerprint",
        row_key,
        source,
        href,
    )


def _extract_meeting_id(
    item: ET.Element,
    urls: dict[str, str],
    row_key: str,
    counters: Counter[str],
) -> str:
    candidates = [
        urls["video_url"],
        urls["agenda_url"],
        urls["minutes_url"],
        _child_text(item, "guid"),
        _child_text(item, "link"),
    ]
    for candidate in candidates:
        match = CLIP_ID_RE.search(candidate or "")
        if match:
            return match.group(1)

    guid = _clean_text(_child_text(item, "guid"))
    if guid and not urlparse(guid).scheme and len(guid) <= 100:
        logger.info("field_emitted row=%s field=meeting_id value=%r source=guid_text", row_key, guid)
        return guid

    counters["meeting_id_absent"] += 1
    logger.warning(
        "field_absent row=%s field=meeting_id reason=no_clip_id_or_safe_guid_signal",
        row_key,
    )
    return ""


def _derive_status(title: str, urls: dict[str, str], row_key: str) -> str:
    if CANCELLED_RE.search(_cap_text(title, 2_000)):
        logger.info("field_emitted row=%s field=meeting_status value=Cancelled source=title_regex", row_key)
        return "Cancelled"
    if urls["minutes_url"]:
        return "Minutes Available"
    if urls["agenda_url"] or urls["agenda_packet_url"]:
        return "Agenda Available"
    return "Scheduled"


def _dates_from_text(text: str) -> list[str]:
    capped = _cap_text(text, 4_000)
    dates: list[str] = []

    for match in MONTH_DATE_RE.finditer(capped):
        month = MONTHS[match.group(1).rstrip(".").lower()]
        day = int(match.group(2))
        year = int(match.group(3))
        normalized = _date_from_parts(year, month, day)
        if normalized:
            dates.append(normalized)

    for match in NUMERIC_DATE_RE.finditer(capped):
        month = int(match.group(1))
        day = int(match.group(2))
        year = int(match.group(3))
        if year < 100:
            year += 2000
        normalized = _date_from_parts(year, month, day)
        if normalized:
            dates.append(normalized)

    return dates


def _date_from_pubdate(value: str, row_key: str) -> str:
    if not value:
        return ""
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError) as exc:
        logger.warning("date_parse_failed row=%s source=pubDate rejected=%r error=%s", row_key, value, exc)
        return ""
    return parsed.date().isoformat()


def _date_from_pubdate_parts(item: ET.Element, row_key: str) -> str:
    for element in item.iter():
        if _local_name(element.tag) != "pubDateParts":
            continue
        attrs = {_normalize_key(key): value for key, value in element.attrib.items()}
        try:
            year = int(attrs.get("yr", ""))
            month = int(attrs.get("mo", ""))
            day = int(attrs.get("day", ""))
        except ValueError:
            logger.warning(
                "date_parse_failed row=%s source=gran_pubDateParts rejected_attrs=%s",
                row_key,
                element.attrib,
            )
            return ""
        return _date_from_parts(year, month, day)
    return ""


def _date_from_parts(year: int, month: int, day: int) -> str:
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return ""


def _child_text(item: ET.Element, local_name: str) -> str:
    for child in list(item):
        if _local_name(child.tag) == local_name:
            return child.text or ""
    return ""


def _field_from_tag(local_key: str) -> str:
    for field, aliases in FIELD_TAG_ALIASES.items():
        if local_key in aliases:
            return field
    logger.warning(
        "field_tag_unclassified tag=%r reason=not_a_known_document_field_alias",
        local_key,
    )
    return ""


def _row_key(index: int, item: ET.Element) -> str:
    guid = _clean_text(_child_text(item, "guid"))
    title = _clean_text(_child_text(item, "title"))
    if guid:
        return f"item#{index}:guid={_cap_text(guid, 120)}"
    if title:
        return f"item#{index}:title={_cap_text(title, 120)}"
    return f"item#{index}"


def _validate_schema(meeting: dict[str, str], row_key: str) -> None:
    keys = tuple(meeting.keys())
    if keys != CANONICAL_FIELDS:
        raise ValueError(f"Schema mismatch row={row_key}: keys={keys!r}")
    bad_types = {field: type(value).__name__ for field, value in meeting.items() if not isinstance(value, str)}
    if bad_types:
        raise TypeError(f"Non-string parser fields row={row_key}: {bad_types}")
    bad_urls = {
        field: meeting[field]
        for field in URL_FIELDS
        if meeting[field] and not meeting[field].startswith(("http://", "https://"))
    }
    if bad_urls:
        raise ValueError(f"Non-http URL emitted row={row_key}: {bad_urls}")


def _is_iso_date(value: str) -> bool:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _is_placeholder_href(href: str) -> bool:
    low = href.strip().lower()
    return low in {"", "#"} or low.startswith("javascript:")


def _html_to_text(value: str) -> str:
    if not value:
        return ""
    return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)


def _clean_text(value: str) -> str:
    decoded = html_lib.unescape(value or "")
    if "<" in decoded and ">" in decoded:
        decoded = BeautifulSoup(decoded, "html.parser").get_text(" ", strip=True)
    return " ".join(decoded.split())


def _cap_text(value: str, limit: int) -> str:
    return value[:limit]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _looks_like_href(value: str) -> bool:
    low = value.lower().strip()
    return (
        low.startswith(("http://", "https://", "//", "/"))
        or ".php" in low
        or ".aspx" in low
        or ".pdf" in low
        or ".mp4" in low
        or ".asx" in low
        or "?" in low
    )


def _host(url: str) -> str:
    return (urlparse(url).netloc.split("@")[-1].split(":")[0] or "").lower()


def _host_allowed(host: str) -> bool:
    return host in ALLOWED_HOSTS


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    print(json.dumps(scrape_calendar(DEFAULT_CALENDAR_URL), indent=2))
