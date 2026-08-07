"""Kingman — Granicus meeting parser."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
import json
import logging
import re
from typing import Callable, Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, Tag
from defusedxml import ElementTree as ET

__all__ = ["scrape_calendar"]

logger = logging.getLogger(__name__)

DEFAULT_RSS_URL = "https://cityofkingman.granicus.com/ViewPublisherRSS.php?view_id=1"
GRANICUS_NS = "https://www.granicus.com/schema/rss-supplements"
MAX_RSS_BYTES = 2_000_000
CHUNK_SIZE = 64 * 1024
ALLOWED_HOSTS = {
    "cityofkingman.granicus.com",
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

MONTHS_SHORT = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
MONTHS_LONG = {
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
}

TITLE_DATE_RE = re.compile(
    r"\s+-\s+([A-Z][a-z]{2,8})\.?\s+(\d{1,2}),?\s+(\d{4})\s*$",
    re.IGNORECASE,
)
DESCRIPTION_DATE_RE = re.compile(
    r"\b(?:dated|date:?)\s+(?:[A-Za-z]+,\s+)?([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b"
    r"|\b(?:dated|date:?)\s+(?:[A-Za-z]+,\s+)?(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b",
    re.IGNORECASE,
)
# Accepted formats: "5:30 a.m.", "5:30 p.m.", "5:30am",
# "5:30 AM", "5 PM", and "10:00 p.m.". Do not end this with "\b" after
# a dotted suffix; that misses the "a.m." / "p.m." variants.
TIME_RE = re.compile(
    r"(?<!\d)(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([AaPp])\.?\s*[Mm]\.?(?=\s|$|[^\w.])"
)
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
ONCLICK_URL_RE = re.compile(r"""(?P<url>(?:https?:)?//[^'"\s)]+|/[A-Za-z0-9_./?&=%+-]+)""")
CLIP_ID_RE = re.compile(r"(?:[?&]clip_id=|/clip/)(\d+)", re.IGNORECASE)


@dataclass
class _Stats:
    rows_seen: int = 0
    rows_accepted: int = 0
    rows_dropped: int = 0
    drop_reasons: Counter[str] = field(default_factory=Counter)
    field_absences: Counter[str] = field(default_factory=Counter)
    url_rejections: Counter[str] = field(default_factory=Counter)
    field_sources: Counter[str] = field(default_factory=Counter)

    def drop(self, reason: str) -> None:
        self.rows_dropped += 1
        self.drop_reasons[reason] += 1

    def absence(self, field_name: str, reason: str) -> None:
        self.field_absences[f"{field_name}:{reason}"] += 1

    def source(self, field_name: str, source_name: str) -> None:
        self.field_sources[f"{field_name}:{source_name}"] += 1


def scrape_calendar(calendar_url: str) -> list[dict]:
    """Scrape Kingman's Granicus RSS feed into canonical meeting rows."""
    stats = _Stats()
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"})

    logger.warning(
        "kingman_scrape_started url=%s allowed_hosts=%s tls_verify=True max_bytes=%d",
        calendar_url,
        sorted(ALLOWED_HOSTS),
        MAX_RSS_BYTES,
    )
    logger.warning(
        "absent_by_construction fields=meeting_location,ecomment_url "
        "reason=granicus_rss_calendar_surface_has_no_per_row_location_or_ecomment_signal"
    )
    logger.warning(
        "time_policy field=meeting_time source=visible_title_or_description_only "
        "pubDate_and_gran_pubDateParts_not_used_as_meeting_time"
    )

    try:
        raw_xml = _fetch_text_bounded(session, calendar_url)
    except (requests.RequestException, ValueError) as exc:
        logger.warning(
            "architectural_blocker url=%s scope=all_meetings action=return_honest_empty "
            "reason=fetch_or_redirect_validation_failed exc=%r",
            calendar_url,
            exc,
        )
        _log_summary(stats)
        return []

    root = _parse_xml(raw_xml, calendar_url)
    _validate_granicus_fingerprint(root, raw_xml, calendar_url)
    view_id = _extract_view_id(calendar_url)
    minutes_by_clip_id = _parse_companion_minutes_feed(session, calendar_url, stats)
    items = root.findall("./channel/item")

    logger.info("rss_items_observed url=%s item_count=%d view_id=%s", calendar_url, len(items), view_id)

    meetings: list[dict] = []
    for index, item in enumerate(items, start=1):
        stats.rows_seen += 1
        row_label = f"item:{index}"
        try:
            meeting = _build_meeting(item, calendar_url, view_id, minutes_by_clip_id, row_label, stats)
        except Exception as exc:
            stats.drop("row_exception")
            logger.warning("row_dropped row=%s reason=row_exception exc=%r", row_label, exc, exc_info=True)
            continue

        if not meeting:
            stats.drop("missing_required_title_or_date")
            continue

        _validate_schema(meeting, row_label)
        stats.rows_accepted += 1
        logger.warning(
            "row_accepted row=%s meeting_id=%r title=%r date=%s time=%r status=%s "
            "agenda_url=%r minutes_url=%r video_url=%r agenda_packet_url=%r",
            row_label,
            meeting["meeting_id"],
            meeting["meeting_title"],
            meeting["meeting_date"],
            meeting["meeting_time"],
            meeting["meeting_status"],
            meeting["agenda_url"],
            meeting["minutes_url"],
            meeting["video_url"],
            meeting["agenda_packet_url"],
        )
        meetings.append(meeting)

    _log_summary(stats)
    return meetings


def _build_meeting(
    item: ET.Element,
    calendar_url: str,
    view_id: str,
    minutes_by_clip_id: dict[str, str],
    row_label: str,
    stats: _Stats,
) -> dict[str, str] | None:
    raw_title = _item_text(item, "title")
    description_html = _item_text(item, "description")
    description_soup = BeautifulSoup(description_html, "html.parser")
    description_text = _clean_text(description_html)

    meeting_title = _extract_meeting_title(raw_title, row_label, stats)
    meeting_date = _extract_meeting_date(raw_title, description_text, item, row_label, stats)
    if not meeting_title or not meeting_date:
        logger.warning(
            "row_dropped row=%s reason=missing_required_title_or_date raw_title=%r "
            "meeting_title=%r meeting_date=%r",
            row_label,
            raw_title,
            meeting_title,
            meeting_date,
        )
        return None

    candidates = _collect_link_candidates(item, description_soup, calendar_url, row_label)
    agenda_url = _extract_url_field("agenda_url", candidates, calendar_url, row_label, stats)
    minutes_url = _extract_url_field("minutes_url", candidates, calendar_url, row_label, stats)
    video_url = _extract_url_field("video_url", candidates, calendar_url, row_label, stats)
    agenda_packet_url = _extract_url_field("agenda_packet_url", candidates, calendar_url, row_label, stats)
    meeting_id = _extract_meeting_id((video_url, agenda_url, minutes_url, agenda_packet_url), row_label, stats)

    if not minutes_url and meeting_id:
        minutes_url = minutes_by_clip_id.get(meeting_id, "")
        if minutes_url:
            stats.source("minutes_url", "companion_minutes_feed")
            logger.warning(
                "field_recovered row=%s field=minutes_url source=companion_minutes_feed meeting_id=%s url=%s",
                row_label,
                meeting_id,
                minutes_url,
            )
    if not minutes_url:
        stats.absence("minutes_url", "no_minutes_url_signal")

    for field_name, emitted_url in (
        ("agenda_url", agenda_url),
        ("minutes_url", minutes_url),
        ("video_url", video_url),
        ("agenda_packet_url", agenda_packet_url),
    ):
        _warn_view_id_mismatch(emitted_url, view_id, row_label, field_name)

    meeting_time = _extract_meeting_time(raw_title, description_text, item, meeting_date, row_label, stats)
    meeting_location = ""
    ecomment_url = ""
    stats.absence("meeting_location", "absent_by_construction")
    stats.absence("ecomment_url", "absent_by_construction")

    meeting_status = _classify_meeting_status(
        meeting_title,
        description_text,
        agenda_url,
        agenda_packet_url,
        minutes_url,
        row_label,
        stats,
    )

    return {
        "meeting_title": meeting_title,
        "meeting_date": meeting_date,
        "meeting_time": meeting_time,
        "meeting_location": meeting_location,
        "meeting_status": meeting_status,
        "agenda_url": agenda_url,
        "minutes_url": minutes_url,
        "video_url": video_url,
        "agenda_packet_url": agenda_packet_url,
        "ecomment_url": ecomment_url,
        "meeting_id": meeting_id,
    }


def _fetch_text_bounded(session: requests.Session, url: str, max_bytes: int = MAX_RSS_BYTES) -> str:
    with session.get(url, timeout=30, stream=True) as response:
        response.raise_for_status()
        final_host = urlparse(response.url).netloc.split(":")[0].lower()
        if not _is_allowed_host(final_host):
            raise ValueError(f"Redirect to disallowed host: {final_host} (started from {url})")

        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"Response from {url} exceeded {max_bytes} bytes")
            chunks.append(chunk)

        return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")


def _parse_xml(text: str, source_url: str) -> ET.Element:
    try:
        return ET.fromstring(text.encode("utf-8"))
    except ET.ParseError:
        logger.warning("rss_parse_failed url=%s bytes=%d", source_url, len(text.encode("utf-8")))
        raise


def _validate_granicus_fingerprint(root: ET.Element, raw_xml: str, source_url: str) -> None:
    items = root.findall("./channel/item")
    witnessed = {
        "path_ViewPublisherRSS": "ViewPublisherRSS.php" in urlparse(source_url).path,
        "rss_channel_items": bool(items),
        "xmlns_gran": "xmlns:gran" in raw_xml,
        "gran_pubDateParts": any(item.find(f"{{{GRANICUS_NS}}}pubDateParts") is not None for item in items),
        "item_title": bool(items) and items[0].find("title") is not None,
        "item_description": bool(items) and items[0].find("description") is not None,
    }
    missing = [name for name, present in witnessed.items() if not present]
    if missing:
        raise ValueError(f"Kingman Granicus RSS fingerprint missing tokens: {missing}")
    logger.info("vendor_fingerprint_confirmed url=%s witnessed=%s", source_url, witnessed)


def _extract_view_id(source_url: str) -> str:
    view_id = dict(parse_qsl(urlparse(source_url).query, keep_blank_values=True)).get("view_id", "").strip()
    if not view_id:
        raise ValueError(f"Missing view_id in Kingman RSS URL: {source_url}")
    logger.info("runtime_view_id_observed view_id=%s url=%s", view_id, source_url)
    return view_id


def _minutes_feed_url(source_url: str) -> str:
    parsed = urlparse(source_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["mode"] = "minutes"
    return urlunparse(parsed._replace(query=urlencode(query)))


def _parse_companion_minutes_feed(session: requests.Session, source_url: str, stats: _Stats) -> dict[str, str]:
    companion_url = _minutes_feed_url(source_url)
    try:
        raw_xml = _fetch_text_bounded(session, companion_url)
        root = _parse_xml(raw_xml, companion_url)
    except (requests.RequestException, ValueError) as exc:
        logger.warning(
            "companion_minutes_fetch_blocked url=%s scope=minutes_url_enrichment "
            "action=continue_without_minutes exc=%r",
            companion_url,
            exc,
        )
        return {}

    mapped: dict[str, str] = {}
    dropped = Counter()
    for index, item in enumerate(root.findall("./channel/item"), start=1):
        row_label = f"minutes_companion:{index}"
        soup = BeautifulSoup(_item_text(item, "description"), "html.parser")
        candidates = _collect_link_candidates(item, soup, companion_url, row_label)
        minutes_url = _extract_url_field("minutes_url", candidates, companion_url, row_label, stats)
        meeting_id = _extract_meeting_id((minutes_url,), row_label, stats)
        if not minutes_url:
            dropped["no_minutes_url"] += 1
            continue
        if not meeting_id:
            dropped["no_meeting_id"] += 1
            continue
        mapped[meeting_id] = minutes_url

    logger.warning(
        "companion_minutes_summary url=%s rows_seen=%d rows_mapped=%d rows_dropped=%d drop_reasons=%s",
        companion_url,
        len(root.findall("./channel/item")),
        len(mapped),
        sum(dropped.values()),
        dict(dropped),
    )
    return mapped


def _item_text(item: ET.Element, tag: str) -> str:
    return (item.findtext(tag) or "").strip()


def _clean_text(value: str) -> str:
    return BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)


def _extract_meeting_title(raw_title: str, row_label: str, stats: _Stats) -> str:
    clean_title = _clean_text(raw_title)
    if not clean_title:
        stats.absence("meeting_title", "missing_title_tag")
        logger.warning("field_empty row=%s field=meeting_title reason=missing_title_tag", row_label)
        return ""
    title = TITLE_DATE_RE.sub("", clean_title).strip()
    if not title:
        stats.absence("meeting_title", "date_suffix_removed_entire_title")
        logger.warning(
            "field_empty row=%s field=meeting_title raw_title=%r reason=date_suffix_removed_entire_title",
            row_label,
            clean_title,
        )
        return ""
    stats.source("meeting_title", "title_tag")
    return title


def _extract_meeting_date(raw_title: str, description_text: str, item: ET.Element, row_label: str, stats: _Stats) -> str:
    title_date = _date_from_title(raw_title, row_label)
    description_date = _date_from_description(description_text, row_label)
    pub_date = _pub_date_iso(item, row_label)

    if title_date and description_date and title_date != description_date:
        stats.absence("meeting_date", "title_description_mismatch")
        logger.warning(
            "field_empty row=%s field=meeting_date title_date=%r description_date=%r pubDate_date=%r "
            "reason=title_description_mismatch",
            row_label,
            title_date,
            description_date,
            pub_date,
        )
        return ""

    if title_date:
        if not description_date:
            logger.warning(
                "meeting_date_single_visible_signal row=%s emitted=%s source=title "
                "description_date_absent=True pubDate_date=%r",
                row_label,
                title_date,
                pub_date,
            )
        if pub_date and pub_date != title_date:
            logger.warning(
                "pubDate_not_used_for_meeting_date row=%s emitted=%s pubDate_date=%s "
                "reason=archive_publication_date_mismatch",
                row_label,
                title_date,
                pub_date,
            )
        stats.source("meeting_date", "title_suffix")
        return title_date

    stats.absence("meeting_date", "no_title_date")
    logger.warning(
        "field_empty row=%s field=meeting_date title_date=%r description_date=%r pubDate_date=%r "
        "reason=no_title_date",
        row_label,
        title_date,
        description_date,
        pub_date,
    )
    return ""


def _date_from_title(raw_title: str, row_label: str) -> str:
    clean_title = _clean_text(raw_title)
    match = TITLE_DATE_RE.search(clean_title)
    if not match:
        logger.warning("date_signal_absent row=%s source=title raw_title=%r", row_label, clean_title)
        return ""
    month_name, day, year = match.groups()
    month = MONTHS_SHORT.get(month_name[:3].lower())
    if not month:
        logger.warning("date_signal_rejected row=%s source=title month=%r raw_title=%r", row_label, month_name, clean_title)
        return ""
    return _format_iso(year, month, day)


def _date_from_description(description_text: str, row_label: str) -> str:
    match = DESCRIPTION_DATE_RE.search(description_text[:1000])
    if not match:
        logger.warning(
            "date_signal_absent row=%s source=description description_sample=%r",
            row_label,
            description_text[:220],
        )
        return ""
    if match.group(1):
        month_name, day, year = match.group(1), match.group(2), match.group(3)
    else:
        day, month_name, year = match.group(4), match.group(5), match.group(6)
    month = MONTHS_LONG.get(month_name.lower()) or MONTHS_SHORT.get(month_name[:3].lower())
    if not month:
        logger.warning(
            "date_signal_rejected row=%s source=description month=%r description_sample=%r",
            row_label,
            month_name,
            description_text[:220],
        )
        return ""
    return _format_iso(year, month, day)


def _format_iso(year: str, month: int, day: str) -> str:
    return f"{int(year):04d}-{month:02d}-{int(day):02d}"


def _pub_date_iso(item: ET.Element, row_label: str) -> str:
    raw_pub_date = _item_text(item, "pubDate")
    if not raw_pub_date:
        logger.warning("date_signal_absent row=%s source=pubDate reason=missing_pubDate_tag", row_label)
        return ""
    try:
        return parsedate_to_datetime(raw_pub_date).date().isoformat()
    except (TypeError, ValueError):
        logger.warning("date_signal_rejected row=%s source=pubDate raw_pubDate=%r", row_label, raw_pub_date)
        return ""


def _extract_meeting_time(
    raw_title: str,
    description_text: str,
    item: ET.Element,
    meeting_date: str,
    row_label: str,
    stats: _Stats,
) -> str:
    searchable = f"{_clean_text(raw_title)} {description_text}"[:3000]
    match = TIME_RE.search(searchable)
    if match:
        hour = int(match.group(1))
        minute = match.group(2) or "00"
        suffix = "AM" if match.group(3).lower() == "a" else "PM"
        stats.source("meeting_time", "visible_title_or_description")
        return f"{hour}:{minute} {suffix}"

    stats.absence("meeting_time", "no_visible_time_signal")
    logger.warning(
        "field_empty row=%s field=meeting_time reason=no_visible_title_or_description_time "
        "meeting_date=%r pubDate_date=%r gran_pubDateParts=%s note=publication_time_not_used",
        row_label,
        meeting_date,
        _pub_date_iso(item, row_label),
        _pub_date_parts(item),
    )
    return ""


def _pub_date_parts(item: ET.Element) -> dict[str, str]:
    node = item.find(f"{{{GRANICUS_NS}}}pubDateParts")
    return dict(node.attrib) if node is not None else {}


@dataclass(frozen=True)
class _LinkCandidate:
    raw_url: str
    label: str
    source: str


def _collect_link_candidates(
    item: ET.Element,
    description_soup: BeautifulSoup,
    base_url: str,
    row_label: str,
) -> list[_LinkCandidate]:
    candidates: list[_LinkCandidate] = []

    xml_link = _item_text(item, "link")
    if xml_link:
        candidates.append(_LinkCandidate(xml_link, "item_link", "item.link"))

    enclosure = item.find("enclosure")
    if enclosure is not None:
        enclosure_url = (enclosure.attrib.get("url") or "").strip()
        enclosure_type = (enclosure.attrib.get("type") or "").strip()
        if enclosure_url:
            candidates.append(_LinkCandidate(enclosure_url, enclosure_type or "enclosure", "item.enclosure"))

    for position, anchor in enumerate(description_soup.find_all("a"), start=1):
        if not isinstance(anchor, Tag):
            continue
        label = _clean_text(anchor.get_text(" ", strip=True))
        href = (anchor.get("href") or "").strip()
        if href:
            candidates.append(_LinkCandidate(href, label, f"description.a[{position}].href"))
        else:
            logger.warning("url_candidate_absent row=%s source=description.a[%d].href reason=empty_href", row_label, position)

        fallback_urls = _fallback_urls_from_anchor(anchor)
        if fallback_urls and (href.lower().startswith(BAD_SCHEMES) or href in {"", "#"}):
            logger.warning(
                "url_placeholder_recovered row=%s source=description.a[%d] placeholder_href=%r fallback_count=%d",
                row_label,
                position,
                href,
                len(fallback_urls),
            )
        if not fallback_urls and (href.lower().startswith(BAD_SCHEMES) or href == "#"):
            logger.warning(
                "url_placeholder_unrecovered row=%s source=description.a[%d] placeholder_href=%r reason=no_onclick_or_data_url",
                row_label,
                position,
                href,
            )
        for fallback_url in fallback_urls:
            candidates.append(_LinkCandidate(fallback_url, label, f"description.a[{position}].fallback"))

    logger.info("link_candidates_observed row=%s count=%d", row_label, len(candidates))
    return candidates


def _fallback_urls_from_anchor(anchor: Tag) -> list[str]:
    values: list[str] = []
    onclick = (anchor.get("onclick") or "").strip()
    if onclick:
        values.extend(match.group("url") for match in ONCLICK_URL_RE.finditer(onclick))
    for name, value in anchor.attrs.items():
        if name.startswith("data-") and isinstance(value, str):
            values.extend(match.group("url") for match in ONCLICK_URL_RE.finditer(value))
            if "://" in value or value.startswith("/"):
                values.append(value)
    return values


def _extract_url_field(
    field_name: str,
    candidates: Iterable[_LinkCandidate],
    base_url: str,
    row_label: str,
    stats: _Stats,
) -> str:
    predicate = _url_predicate(field_name)
    rejected_matching = 0
    for candidate in candidates:
        if not predicate(candidate):
            continue
        emitted = _emit_url(candidate.raw_url, base_url, row_label, field_name, candidate.source, stats)
        if emitted:
            stats.source(field_name, candidate.source)
            logger.warning(
                "field_emitted row=%s field=%s source=%s label=%r url=%s",
                row_label,
                field_name,
                candidate.source,
                candidate.label,
                emitted,
            )
            return emitted
        rejected_matching += 1

    reason = "matching_candidates_rejected" if rejected_matching else "no_matching_url_signal"
    stats.absence(field_name, reason)
    logger.warning("field_empty row=%s field=%s reason=%s", row_label, field_name, reason)
    return ""


def _url_predicate(field_name: str) -> Callable[[_LinkCandidate], bool]:
    def has_word(candidate: _LinkCandidate, word: str) -> bool:
        text = f"{candidate.label} {candidate.raw_url}".lower()
        return re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", text) is not None

    def url_contains(candidate: _LinkCandidate, needle: str) -> bool:
        return needle in candidate.raw_url.lower()

    if field_name == "minutes_url":
        return lambda c: url_contains(c, "minutesviewer") or has_word(c, "minutes")
    if field_name == "agenda_packet_url":
        return lambda c: has_word(c, "packet") or url_contains(c, "packet")
    if field_name == "agenda_url":
        return lambda c: (
            not _url_predicate("agenda_packet_url")(c)
            and (
                url_contains(c, "agendaviewer")
                or url_contains(c, "generatedagendaviewer")
                or has_word(c, "agenda")
            )
        )
    if field_name == "video_url":
        return lambda c: url_contains(c, "mediaplayer.php") or has_word(c, "video")
    raise ValueError(f"Unsupported URL field: {field_name}")


def _emit_url(raw_href: str, base_url: str, row_label: str, field_name: str, source: str, stats: _Stats) -> str:
    href = (raw_href or "").strip()
    if not href:
        _record_url_rejection(row_label, field_name, source, raw_href, "", "empty_href", stats)
        return ""

    lower = href.lower().lstrip()
    for bad_scheme in BAD_SCHEMES:
        if lower.startswith(bad_scheme):
            _record_url_rejection(row_label, field_name, source, href, "", f"bad_scheme:{bad_scheme}", stats)
            return ""

    absolute = urljoin(base_url, href)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        _record_url_rejection(row_label, field_name, source, href, absolute, "bad_scheme_after_join", stats)
        return ""

    host = parsed.netloc.split(":")[0].lower()
    if not _is_allowed_host(host):
        _record_url_rejection(row_label, field_name, source, href, absolute, f"disallowed_host:{host}", stats)
        return ""

    return absolute


def _record_url_rejection(
    row_label: str,
    field_name: str,
    source: str,
    rejected: str,
    absolute: str,
    reason: str,
    stats: _Stats,
) -> None:
    stats.url_rejections[f"{field_name}:{reason}"] += 1
    logger.warning(
        "url_rejected row=%s field=%s source=%s rejected=%r absolute=%r reason=%s",
        row_label,
        field_name,
        source,
        rejected,
        absolute,
        reason,
    )


def _is_allowed_host(host: str) -> bool:
    normalized = host.split(":")[0].lower()
    return normalized in ALLOWED_HOSTS or normalized.endswith(".granicus.com")


def _extract_meeting_id(urls: Iterable[str], row_label: str, stats: _Stats) -> str:
    for url in urls:
        match = CLIP_ID_RE.search(url or "")
        if match:
            stats.source("meeting_id", "clip_id")
            return match.group(1)
    stats.absence("meeting_id", "no_clip_id_in_emitted_urls")
    logger.warning("field_empty row=%s field=meeting_id reason=no_clip_id_in_emitted_urls", row_label)
    return ""


def _warn_view_id_mismatch(emitted_url: str, expected_view_id: str, row_label: str, field_name: str) -> None:
    if not emitted_url:
        return
    actual_view_id = dict(parse_qsl(urlparse(emitted_url).query, keep_blank_values=True)).get("view_id", "")
    if actual_view_id and actual_view_id != expected_view_id:
        logger.warning(
            "view_id_mismatch row=%s field=%s expected_view_id=%s emitted_view_id=%s url=%s",
            row_label,
            field_name,
            expected_view_id,
            actual_view_id,
            emitted_url,
        )


def _classify_meeting_status(
    meeting_title: str,
    description_text: str,
    agenda_url: str,
    agenda_packet_url: str,
    minutes_url: str,
    row_label: str,
    stats: _Stats,
) -> str:
    lower_description = description_text.lower()
    if "archived" in lower_description or "closed" in lower_description:
        logger.warning(
            "vendor_vocab_observed row=%s vocab_sample=%r action=status_uses_same_row_canonical_url_evidence",
            row_label,
            description_text[:220],
        )

    if CANCELLED_RE.search(meeting_title[:500]):
        stats.source("meeting_status", "title_cancelled_regex")
        return "Cancelled"
    if minutes_url:
        stats.source("meeting_status", "same_row_minutes_url")
        return "Minutes Available"
    if agenda_url or agenda_packet_url:
        stats.source("meeting_status", "same_row_agenda_or_packet_url")
        return "Agenda Available"

    logger.warning(
        "status_defaulted row=%s emitted=Scheduled reason=no_cancelled_minutes_or_agenda_evidence",
        row_label,
    )
    stats.source("meeting_status", "default_no_document_evidence")
    return "Scheduled"


def _validate_schema(meeting: dict[str, str], row_label: str) -> None:
    if tuple(meeting.keys()) != CANONICAL_FIELDS:
        raise ValueError(f"Schema key order violation at {row_label}: {tuple(meeting.keys())!r}")
    non_strings = {key: type(value).__name__ for key, value in meeting.items() if not isinstance(value, str)}
    if non_strings:
        raise ValueError(f"Schema type violation at {row_label}: {non_strings!r}")
    for field_name in ("agenda_url", "minutes_url", "video_url", "agenda_packet_url", "ecomment_url"):
        value = meeting[field_name]
        if value and not value.startswith(("http://", "https://")):
            raise ValueError(f"Schema URL violation at {row_label}: {field_name}={value!r}")


def _log_summary(stats: _Stats) -> None:
    logger.warning(
        "run_summary rows_seen=%d rows_accepted=%d rows_dropped=%d drop_reasons=%s",
        stats.rows_seen,
        stats.rows_accepted,
        stats.rows_dropped,
        dict(stats.drop_reasons),
    )
    logger.warning("field_absence_summary counts=%s", dict(stats.field_absences))
    logger.warning("field_source_summary counts=%s", dict(stats.field_sources))
    logger.warning("url_rejection_summary counts=%s", dict(stats.url_rejections))


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s:%(name)s:%(message)s")
    print(json.dumps(scrape_calendar(DEFAULT_RSS_URL), indent=2, sort_keys=True))
