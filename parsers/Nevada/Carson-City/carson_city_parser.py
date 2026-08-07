"""Carson City — Granicus ViewPublisher meeting parser."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from email.utils import parsedate_to_datetime
import html as html_lib
import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse


logger = logging.getLogger(__name__)

DEFAULT_CALENDAR_URL = "https://carsoncity.granicus.com/ViewPublisher.php?view_id=2"
MAX_RSS_BYTES = 2_000_000
MAX_HTML_BYTES = 25_000_000
FETCH_ALLOWED_HOSTS = {"carsoncity.granicus.com"}
EMIT_ALLOWED_HOSTS = {
    "carsoncity.granicus.com",
    "d3n9y02raazwpg.cloudfront.net",
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
MONTH_DAY_YEAR_RE = re.compile(
    r"\b("
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
    r")\.?\s+([0-3]?\d),?\s+(\d{4})\b",
    re.IGNORECASE,
)
DAY_MONTH_YEAR_RE = re.compile(
    r"\b([0-3]?\d)\s+("
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
    r")\.?\s+(\d{4})\b",
    re.IGNORECASE,
)
NUMERIC_DATE_RE = re.compile(r"(?<!\d)([01]?\d)[/-]([0-3]?\d)[/-](\d{2}|\d{4})(?!\d)")
# Regex test cases: "5:30 a.m.", "5:30 p.m.", "5:30am", "5:30 AM".
TIME_RE = re.compile(
    r"(?<!\d)(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([AaPp])\.?\s*[Mm]\.?(?=\s|$|[^\w.])"
)
ONCLICK_URL_RE = re.compile(r"""['"]((?:https?:)?//[^'"]+|https?://[^'"]+|/[^'"]+)['"]""")


def scrape_calendar(url: str = DEFAULT_CALENDAR_URL) -> list[dict[str, str]]:
    """Scrape Carson City meetings from Granicus agenda RSS, enriched by ViewPublisher HTML."""
    deps = _load_runtime_dependencies()
    if deps is None:
        return []
    requests, soup_cls, element_tree = deps

    agenda_rss_url = _rss_url(url, mode="agendas")
    minutes_rss_url = _rss_url(url, mode="minutes")
    html_url = _html_url(url)
    logger.warning(
        "startup_absence_declaration vendor=granicus_rss_html "
        "meeting_location_absent_by_construction "
        "ecomment_absent_unless_explicit_url_present "
        "pubDate_and_gran_pubDateParts_not_trusted_for_meeting_time "
        "html_companion_used_for_same_row_time_packet_minutes_video"
    )

    with requests.Session() as session:
        session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"})
        try:
            raw_agenda_xml = _fetch_text_bounded(session, agenda_rss_url, MAX_RSS_BYTES)
        except requests.RequestException as exc:
            logger.warning(
                "architectural_blocker_network_fetch_failed returning honest-empty [] "
                "url=%r error=%s missing_scope=all_agenda_rss_items",
                agenda_rss_url,
                exc,
            )
            return []

        root = element_tree.fromstring(raw_agenda_xml.encode("utf-8"))
        items = list(_iter_items(root))
        _validate_rss_surface(root, raw_agenda_xml, agenda_rss_url, items, feed_label="agendas")

        html_companion = _fetch_html_companion(session, html_url, soup_cls)
        minutes_companion = _fetch_minutes_companion(
            session,
            minutes_rss_url,
            element_tree,
            feed_label="minutes",
        )

    counters: Counter[str] = Counter()
    meetings: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    for index, item in enumerate(items, start=1):
        counters["rows_seen"] += 1
        row_key = _row_key(index, item)
        meeting = _build_meeting(
            item,
            agenda_rss_url,
            row_key,
            html_companion,
            minutes_companion,
            soup_cls,
            counters,
        )

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

        meeting_id = meeting["meeting_id"]
        if meeting_id and meeting_id in seen_ids:
            counters["rows_dropped"] += 1
            counters["drop_duplicate_meeting_id"] += 1
            logger.warning(
                "row_dropped reason=duplicate_meeting_id row=%s meeting_id=%r title=%r",
                row_key,
                meeting_id,
                meeting["meeting_title"],
            )
            continue
        if meeting_id:
            seen_ids.add(meeting_id)

        _validate_schema(meeting, row_key)
        meetings.append(meeting)
        counters["rows_accepted"] += 1
        logger.info(
            "row_accepted row=%s title=%r date=%s time=%r status=%s urls=%s",
            row_key,
            meeting["meeting_title"],
            meeting["meeting_date"],
            meeting["meeting_time"],
            meeting["meeting_status"],
            {field: meeting[field] for field in URL_FIELDS if meeting[field]},
        )

    logger.warning(
        "scrape_summary rows_seen=%d rows_accepted=%d rows_dropped=%d "
        "html_companion_rows=%d minutes_companion_rows=%d "
        "time_absent=%d location_absent=%d ecomment_absent=%d "
        "agenda_packet_absent=%d minutes_absent=%d video_absent=%d",
        counters["rows_seen"],
        counters["rows_accepted"],
        counters["rows_dropped"],
        len(html_companion),
        len(minutes_companion),
        counters["meeting_time_absent"],
        counters["meeting_location_absent"],
        counters["ecomment_url_absent"],
        counters["agenda_packet_url_absent"],
        counters["minutes_url_absent"],
        counters["video_url_absent"],
    )
    return meetings


def _load_runtime_dependencies() -> tuple[Any, Any, Any] | None:
    missing: list[str] = []
    try:
        import requests  # type: ignore
    except ModuleNotFoundError:
        requests = None
        missing.append("requests")
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except ModuleNotFoundError:
        BeautifulSoup = None
        missing.append("bs4")
    try:
        from defusedxml import ElementTree as ET  # type: ignore
    except ModuleNotFoundError:
        ET = None
        missing.append("defusedxml")

    if missing:
        logger.warning(
            "architectural_blocker_dependency_missing returning honest-empty [] "
            "missing=%s missing_scope=remote_fetch_and_granicus_rss_parse",
            ",".join(missing),
        )
        return None
    return requests, BeautifulSoup, ET


def _fetch_text_bounded(session: Any, url: str, max_bytes: int) -> str:
    start_host = _host(url)
    if start_host not in FETCH_ALLOWED_HOSTS:
        raise ValueError(f"Input URL host is not allowed: {start_host!r}")

    with session.get(url, timeout=30, stream=True, allow_redirects=True, verify=True) as response:
        response.raise_for_status()
        final_host = _host(response.url)
        if final_host not in FETCH_ALLOWED_HOSTS:
            raise ValueError(f"Redirect to disallowed host: {final_host!r} started_from={url!r}")

        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ValueError(f"Response from {url!r} exceeded {max_bytes} bytes")

        return body.decode(response.encoding or "utf-8", errors="replace")


def _fetch_html_companion(session: Any, html_url: str, soup_cls: Any) -> dict[str, dict[str, str]]:
    try:
        html = _fetch_text_bounded(session, html_url, MAX_HTML_BYTES)
    except Exception as exc:
        logger.warning(
            "companion_fetch_failed source=ViewPublisher_html url=%r error=%s "
            "action=continue_with_rss_only affected_fields=meeting_time,minutes_url,video_url,agenda_packet_url",
            html_url,
            exc,
        )
        return {}

    _validate_html_surface(html, html_url)
    return _build_html_index(html, html_url, soup_cls)


def _fetch_minutes_companion(
    session: Any,
    minutes_rss_url: str,
    element_tree: Any,
    *,
    feed_label: str,
) -> dict[str, str]:
    try:
        raw_xml = _fetch_text_bounded(session, minutes_rss_url, MAX_RSS_BYTES)
    except Exception as exc:
        logger.warning(
            "companion_fetch_failed source=minutes_rss url=%r error=%s "
            "action=continue_without_minutes_rss_join",
            minutes_rss_url,
            exc,
        )
        return {}

    root = element_tree.fromstring(raw_xml.encode("utf-8"))
    items = list(_iter_items(root))
    _validate_rss_surface(root, raw_xml, minutes_rss_url, items, feed_label=feed_label)
    index: dict[str, str] = {}
    counters: Counter[str] = Counter()
    for item_index, item in enumerate(items, start=1):
        row_key = _row_key(item_index, item)
        link = _child_text(item, "link")
        meeting_id = _id_from_url(link)
        if not meeting_id:
            counters["missing_id"] += 1
            logger.warning("minutes_rss_row_dropped row=%s reason=missing_clip_id link=%r", row_key, link)
            continue
        emitted = _emit_url(link, minutes_rss_url, "minutes_url", row_key, "minutes_rss:link", counters)
        if not emitted:
            continue
        if meeting_id in index and index[meeting_id] != emitted:
            logger.warning(
                "minutes_rss_duplicate_id meeting_id=%r kept=%r dropped=%r",
                meeting_id,
                index[meeting_id],
                emitted,
            )
            continue
        index[meeting_id] = emitted
    logger.info(
        "minutes_rss_companion_indexed rows_seen=%d rows_indexed=%d missing_id=%d",
        len(items),
        len(index),
        counters["missing_id"],
    )
    return index


def _validate_rss_surface(
    root: Any,
    raw_xml: str,
    source_url: str,
    items: list[Any],
    *,
    feed_label: str,
) -> None:
    witnessed = {
        "path_ViewPublisherRSS": "ViewPublisherRSS.php" in urlparse(source_url).path,
        "mode": parse_qs(urlparse(source_url).query).get("mode", [""])[0],
        "rss_root": _local_name(root.tag).lower() == "rss",
        "channel": root.find("./channel") is not None,
        "item_count": len(items),
        "granicus_namespace": "granicus.com/schema/rss-supplements" in raw_xml,
        "pubDateParts": "pubDateParts" in raw_xml,
    }
    if not (
        witnessed["path_ViewPublisherRSS"]
        and witnessed["rss_root"]
        and witnessed["channel"]
        and witnessed["item_count"] > 0
    ):
        raise ValueError(f"Carson City Granicus RSS fingerprint missing for {feed_label}: {witnessed}")
    logger.info("vendor_fingerprint_witness vendor=granicus_rss feed=%s witnesses=%s", feed_label, witnessed)


def _validate_html_surface(html: str, source_url: str) -> None:
    witnessed = {
        "path_ViewPublisher": "ViewPublisher.php" in urlparse(source_url).path,
        "listingTable": "listingTable" in html,
        "agenda_rss_link": "ViewPublisherRSS.php?view_id=2&mode=agendas" in html,
        "minutes_rss_link": "ViewPublisherRSS.php?view_id=2&mode=minutes" in html,
        "x_granicus_markup": "Granicus Content" in html or "granicus" in html.lower(),
    }
    if not (witnessed["path_ViewPublisher"] and witnessed["listingTable"] and witnessed["x_granicus_markup"]):
        raise ValueError(f"Carson City Granicus HTML fingerprint missing: {witnessed}")
    logger.info("vendor_fingerprint_witness vendor=granicus_html witnesses=%s", witnessed)


def _build_html_index(html: str, base_url: str, soup_cls: Any) -> dict[str, dict[str, str]]:
    soup = soup_cls(html, "html.parser")
    index: dict[str, dict[str, str]] = {}
    counters: Counter[str] = Counter()

    for table_number, table in enumerate(soup.select("table.listingTable"), start=1):
        headers = [_clean_text(th.get_text(" ", strip=True)) for th in table.select("th")]
        if not headers:
            logger.warning("html_table_skipped table=%d reason=no_headers", table_number)
            continue

        for row_number, row in enumerate(table.select("tr.listingRow"), start=1):
            counters["html_rows_seen"] += 1
            row_key = f"html_table={table_number} row={row_number}"
            companion = _parse_html_row(row, base_url, row_key, counters)
            if not companion:
                counters["html_rows_dropped"] += 1
                continue
            meeting_id = companion["meeting_id"]
            existing = index.get(meeting_id)
            if existing:
                _merge_html_companion(existing, companion, row_key)
            else:
                index[meeting_id] = companion
                counters["html_rows_indexed"] += 1

    logger.info(
        "html_companion_indexed rows_seen=%d rows_indexed=%d rows_dropped=%d url_rejected=%d",
        counters["html_rows_seen"],
        counters["html_rows_indexed"],
        counters["html_rows_dropped"],
        counters["url_rejected_bad_scheme"] + counters["url_rejected_disallowed_host"],
    )
    return index


def _parse_html_row(row: Any, base_url: str, row_key: str, counters: Counter[str]) -> dict[str, str]:
    urls = {field: "" for field in URL_FIELDS}
    text = _clean_text(row.get_text(" ", strip=True))
    if not text or "Currently there are no archived videos" in text:
        logger.info("html_row_dropped row=%s reason=empty_or_vendor_empty_state text=%r", row_key, text)
        return {}

    for anchor in row.find_all("a"):
        label = _clean_text(anchor.get_text(" ", strip=True))
        href = (anchor.get("href") or "").strip()
        onclick = (anchor.get("onclick") or anchor.get("onClick") or "").strip()
        candidates = [href] if href and not _is_placeholder_href(href) else []
        if href and _is_placeholder_href(href):
            fallback_urls = ONCLICK_URL_RE.findall(onclick[:2_000])
            if fallback_urls:
                candidates.extend(fallback_urls)
            else:
                logger.warning(
                    "url_placeholder_without_fallback row=%s label=%r rejected_href=%r checked=onclick",
                    row_key,
                    label,
                    href,
                )

        for candidate in candidates:
            field = _classify_url(candidate, label, anchor)
            if not field:
                counters["url_unclassified"] += 1
                logger.warning(
                    "url_unclassified row=%s source=html_anchor label=%r rejected=%r",
                    row_key,
                    label,
                    candidate,
                )
                continue
            _assign_url(urls, field, candidate, base_url, row_key, f"html_anchor:{label}", counters)

    meeting_id = _first_meeting_id(urls)
    if not meeting_id:
        logger.warning("html_row_dropped row=%s reason=no_agenda_event_or_clip_id text=%r", row_key, text[:300])
        return {}

    date_text = _date_cell_text(row)
    html_date, html_time = _date_time_from_html_text(date_text, row_key)
    return {
        "meeting_id": meeting_id,
        "meeting_date": html_date,
        "meeting_time": html_time,
        "agenda_url": urls["agenda_url"],
        "minutes_url": urls["minutes_url"],
        "video_url": urls["video_url"],
        "agenda_packet_url": urls["agenda_packet_url"],
        "ecomment_url": urls["ecomment_url"],
    }


def _merge_html_companion(existing: dict[str, str], incoming: dict[str, str], row_key: str) -> None:
    for field, value in incoming.items():
        if field == "meeting_id" or not value:
            continue
        if existing.get(field) and existing[field] != value:
            logger.warning(
                "html_companion_conflict row=%s meeting_id=%r field=%s kept=%r dropped=%r",
                row_key,
                incoming["meeting_id"],
                field,
                existing[field],
                value,
            )
            continue
        existing[field] = value


def _build_meeting(
    item: Any,
    base_url: str,
    row_key: str,
    html_companion: dict[str, dict[str, str]],
    minutes_companion: dict[str, str],
    soup_cls: Any,
    counters: Counter[str],
) -> dict[str, str]:
    title = _clean_text(_child_text(item, "title"))
    description_html = _child_text(item, "description")
    description_text = _html_to_text(description_html, soup_cls)
    meeting_date = _extract_meeting_date(item, title, description_text, row_key, counters)

    urls = {field: "" for field in URL_FIELDS}
    _extract_rss_urls(item, description_html, base_url, row_key, soup_cls, urls, counters)
    meeting_id = _first_meeting_id(urls) or _id_from_url(_child_text(item, "link")) or _clean_guid(item, row_key)

    companion = html_companion.get(meeting_id, {}) if meeting_id else {}
    _merge_companion_urls(urls, companion, row_key)
    if meeting_id and not urls["minutes_url"] and meeting_id in minutes_companion:
        urls["minutes_url"] = minutes_companion[meeting_id]
        logger.info("field_emitted row=%s field=minutes_url source=minutes_rss_companion", row_key)

    meeting_time = _extract_meeting_time(item, meeting_date, companion, row_key, counters)
    status = _derive_status(title, urls, row_key)

    counters["meeting_location_absent"] += 1
    counters["ecomment_url_absent"] += 1 if not urls["ecomment_url"] else 0
    for field in ("minutes_url", "video_url", "agenda_packet_url"):
        if not urls[field]:
            counters[f"{field}_absent"] += 1

    return {
        "meeting_title": title,
        "meeting_date": meeting_date,
        "meeting_time": meeting_time,
        "meeting_location": "",
        "meeting_status": status,
        "agenda_url": urls["agenda_url"],
        "minutes_url": urls["minutes_url"],
        "video_url": urls["video_url"],
        "agenda_packet_url": urls["agenda_packet_url"],
        "ecomment_url": urls["ecomment_url"],
        "meeting_id": meeting_id,
    }


def _extract_rss_urls(
    item: Any,
    description_html: str,
    base_url: str,
    row_key: str,
    soup_cls: Any,
    urls: dict[str, str],
    counters: Counter[str],
) -> None:
    link = _child_text(item, "link")
    if link:
        _assign_url(urls, "agenda_url", link, base_url, row_key, "rss:link", counters)

    if not description_html:
        return
    soup = soup_cls(description_html, "html.parser")
    for anchor in soup.find_all("a"):
        label = _clean_text(anchor.get_text(" ", strip=True))
        href = (anchor.get("href") or "").strip()
        if not href:
            continue
        field = _classify_url(href, label, anchor)
        if not field:
            logger.warning("url_unclassified row=%s source=rss_description label=%r rejected=%r", row_key, label, href)
            continue
        _assign_url(urls, field, href, base_url, row_key, f"rss_description:{label}", counters)


def _merge_companion_urls(urls: dict[str, str], companion: dict[str, str], row_key: str) -> None:
    for field in URL_FIELDS:
        value = companion.get(field, "")
        if not value:
            continue
        if urls[field] and urls[field] != value:
            logger.warning(
                "companion_url_conflict row=%s field=%s rss_value=%r companion_value=%r action=kept_rss",
                row_key,
                field,
                urls[field],
                value,
            )
            continue
        urls[field] = value


def _extract_meeting_date(
    item: Any,
    title: str,
    description: str,
    row_key: str,
    counters: Counter[str],
) -> str:
    visible_dates = _dates_from_text(f"{title} {description}")
    pub_date = _date_from_pubdate(_child_text(item, "pubDate"), row_key)
    parts_date, _parts_time = _date_time_from_pubdate_parts(item, row_key)

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
        logger.info("field_emitted row=%s field=meeting_date value=%s source=visible_title_description", row_key, chosen)
        return chosen

    if pub_date and parts_date and pub_date == parts_date:
        logger.warning(
            "date_signal_no_visible_date row=%s source=pubDate_and_pubDateParts_agree action=using_date_only",
            row_key,
        )
        return pub_date
    if pub_date or parts_date:
        counters["meeting_date_ambiguous"] += 1
        logger.warning(
            "date_signal_ambiguous row=%s pubDate_date=%r gran_pubDateParts_date=%r action=emit_empty",
            row_key,
            pub_date,
            parts_date,
        )
        return ""

    counters["meeting_date_absent"] += 1
    logger.warning("field_absent row=%s field=meeting_date reason=no_visible_or_rss_date_signal", row_key)
    return ""


def _extract_meeting_time(
    item: Any,
    meeting_date: str,
    companion: dict[str, str],
    row_key: str,
    counters: Counter[str],
) -> str:
    companion_time = companion.get("meeting_time", "")
    companion_date = companion.get("meeting_date", "")
    if companion_time and (not companion_date or companion_date == meeting_date):
        logger.info("field_emitted row=%s field=meeting_time value=%r source=html_companion_date_cell", row_key, companion_time)
        return companion_time
    if companion_time and companion_date != meeting_date:
        logger.warning(
            "time_signal_conflict row=%s meeting_date=%r html_date=%r html_time=%r action=emit_empty",
            row_key,
            meeting_date,
            companion_date,
            companion_time,
        )

    parts_date, parts_time = _date_time_from_pubdate_parts(item, row_key)
    if parts_time:
        counters["meeting_time_absent"] += 1
        logger.warning(
            "meeting_time_untrusted row=%s rejected_pubDateParts_time=%r pubDateParts_date=%r "
            "meeting_date=%r reason=publication_time_not_used_without_html_same_row_time",
            row_key,
            parts_time,
            parts_date,
            meeting_date,
        )
        return ""

    counters["meeting_time_absent"] += 1
    logger.warning("field_absent row=%s field=meeting_time reason=no_html_or_visible_time_signal", row_key)
    return ""


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

    absolute = urljoin(base_url, stripped)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        counters["url_rejected_bad_scheme"] += 1
        logger.warning(
            "url_rejected row=%s field=%s source=%s reason=non_http_scheme rejected=%r absolute=%r",
            row_key,
            field,
            source,
            href,
            absolute,
        )
        return ""

    host = _host(absolute)
    if host not in EMIT_ALLOWED_HOSTS:
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


def _classify_url(href: str, label: str, anchor: Any | None = None) -> str:
    absolute_hint = urljoin(DEFAULT_CALENDAR_URL, href)
    parsed = urlparse(absolute_hint)
    path = parsed.path.lower()
    query = {key.lower(): values for key, values in parse_qs(parsed.query).items()}
    host = _host(absolute_hint)
    evidence = f"{label} {host} {path} {parsed.query}".lower()

    if "ecomment" in evidence or "publiccomment" in evidence:
        return "ecomment_url"
    if "minutesviewer.php" in path or re.search(r"\bminutes\b", evidence):
        return "minutes_url"
    if "mediaplayer.php" in path or path.endswith((".mp4", ".asx", ".m3u8")):
        return "video_url"
    if "agendaviewer.php" in path:
        return "agenda_url"
    if re.search(r"\bpacket\b", evidence):
        return "agenda_packet_url"
    if path.endswith(".pdf") and anchor is not None:
        parent_text = _clean_text(anchor.parent.get_text(" ", strip=True) if anchor.parent else "")
        parent_headers = " ".join(anchor.parent.get("headers", []) if anchor.parent else [])
        if "packet" in f"{parent_text} {parent_headers}".lower():
            return "agenda_packet_url"
    if "clip_id" in query:
        logger.warning(
            "url_classification_ambiguous href=%r label=%r reason=clip_id_without_known_viewer_path",
            href,
            label,
        )
    return ""


def _derive_status(title: str, urls: dict[str, str], row_key: str) -> str:
    if CANCELLED_RE.search(title[:500]):
        logger.info("field_emitted row=%s field=meeting_status value=Cancelled source=title_regex", row_key)
        return "Cancelled"
    if urls["minutes_url"]:
        logger.info("field_emitted row=%s field=meeting_status value='Minutes Available' source=minutes_url", row_key)
        return "Minutes Available"
    if urls["agenda_url"] or urls["agenda_packet_url"]:
        logger.info("field_emitted row=%s field=meeting_status value='Agenda Available' source=agenda_or_packet_url", row_key)
        return "Agenda Available"
    logger.info("field_emitted row=%s field=meeting_status value=Scheduled source=no_doc_or_cancel_evidence", row_key)
    return "Scheduled"


def _dates_from_text(text: str) -> list[str]:
    capped = text[:5_000]
    dates: list[str] = []

    for match in MONTH_DAY_YEAR_RE.finditer(capped):
        month = MONTHS[match.group(1).rstrip(".").lower()]
        day = int(match.group(2))
        year = int(match.group(3))
        _append_date(dates, year, month, day)

    for match in DAY_MONTH_YEAR_RE.finditer(capped):
        day = int(match.group(1))
        month = MONTHS[match.group(2).rstrip(".").lower()]
        year = int(match.group(3))
        _append_date(dates, year, month, day)

    for match in NUMERIC_DATE_RE.finditer(capped):
        month = int(match.group(1))
        day = int(match.group(2))
        year = int(match.group(3))
        if year < 100:
            year += 2000
        _append_date(dates, year, month, day)

    return dates


def _append_date(dates: list[str], year: int, month: int, day: int) -> None:
    try:
        value = datetime(year, month, day).date().isoformat()
    except ValueError:
        return
    if value not in dates:
        dates.append(value)


def _date_from_pubdate(value: str, row_key: str) -> str:
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError, IndexError, OverflowError) as exc:
        logger.warning("date_parse_failed row=%s source=pubDate rejected=%r error=%s", row_key, value, exc)
        return ""


def _date_time_from_pubdate_parts(item: Any, row_key: str) -> tuple[str, str]:
    for element in item.iter():
        if _local_name(element.tag) != "pubDateParts":
            continue
        attrs = {_normalize_key(key): value for key, value in element.attrib.items()}
        try:
            year = int(attrs.get("yr", ""))
            month = int(attrs.get("mo", ""))
            day = int(attrs.get("day", ""))
            hour = int(attrs.get("hr", ""))
            minute = int(attrs.get("min", ""))
        except ValueError:
            logger.warning("date_parse_failed row=%s source=gran_pubDateParts rejected_attrs=%s", row_key, element.attrib)
            return "", ""
        try:
            parsed = datetime(year, month, day, hour, minute)
        except ValueError:
            logger.warning("date_parse_failed row=%s source=gran_pubDateParts rejected_attrs=%s", row_key, element.attrib)
            return "", ""
        return parsed.date().isoformat(), parsed.strftime("%I:%M %p").lstrip("0")
    return "", ""


def _date_cell_text(row: Any) -> str:
    for cell in row.find_all("td"):
        attrs = " ".join(str(value) for value in cell.get("headers", []))
        attrs += " " + " ".join(str(value) for value in cell.get("class", []))
        attrs += " " + str(cell.get("id", ""))
        if "date" in attrs.lower():
            return _clean_text(cell.get_text(" ", strip=True))
    return ""


def _date_time_from_html_text(value: str, row_key: str) -> tuple[str, str]:
    if not value:
        logger.warning("html_time_absent row=%s reason=no_date_cell_text", row_key)
        return "", ""
    dates = _dates_from_text(value)
    if not dates:
        logger.warning("html_date_parse_failed row=%s rejected_input=%r", row_key, value)
        return "", ""

    match = TIME_RE.search(value[:500])
    if not match:
        logger.warning("html_time_absent row=%s rejected_input=%r reason=no_time_token", row_key, value)
        return dates[0], ""
    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    suffix = match.group(3).upper()
    if suffix == "P" and hour != 12:
        hour += 12
    if suffix == "A" and hour == 12:
        hour = 0
    return dates[0], datetime(2000, 1, 1, hour, minute).strftime("%I:%M %p").lstrip("0")


def _first_meeting_id(urls: dict[str, str]) -> str:
    for field in ("agenda_url", "minutes_url", "video_url"):
        meeting_id = _id_from_url(urls.get(field, ""))
        if meeting_id:
            return meeting_id
    return ""


def _id_from_url(value: str) -> str:
    query = parse_qs(urlparse(value or "").query)
    for key in ("clip_id", "event_id"):
        ids = query.get(key, [])
        if ids and ids[0]:
            return ids[0]
    return ""


def _clean_guid(item: Any, row_key: str) -> str:
    guid = _clean_text(_child_text(item, "guid"))
    if guid and len(guid) <= 120:
        logger.warning("field_emitted row=%s field=meeting_id value=%r source=guid_fallback", row_key, guid)
        return guid
    logger.warning("field_absent row=%s field=meeting_id reason=no_clip_event_or_guid", row_key)
    return ""


def _child_text(item: Any, local_name: str) -> str:
    for child in list(item):
        if _local_name(child.tag) == local_name:
            return child.text or ""
    return ""


def _iter_items(root: Any) -> list[Any]:
    channel = root.find("./channel")
    if channel is None:
        return []
    return list(channel.findall("item"))


def _row_key(index: int, item: Any) -> str:
    guid = _clean_text(_child_text(item, "guid"))
    title = _clean_text(_child_text(item, "title"))
    if guid:
        return f"item#{index}:guid={guid[:120]}"
    if title:
        return f"item#{index}:title={title[:120]}"
    return f"item#{index}"


def _html_to_text(value: str, soup_cls: Any) -> str:
    if not value:
        return ""
    return _clean_text(soup_cls(value, "html.parser").get_text(" ", strip=True))


def _clean_text(value: str) -> str:
    return " ".join(html_lib.unescape(value or "").split())


def _is_placeholder_href(href: str) -> bool:
    low = href.strip().lower()
    return low in {"", "#"} or low.startswith("javascript:")


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


def _rss_url(url: str, *, mode: str) -> str:
    parsed = urlparse(url)
    view_id = parse_qs(parsed.query).get("view_id", [""])[0]
    if not view_id:
        raise ValueError(f"Carson City Granicus URL lacks view_id: {url!r}")
    query = urlencode({"view_id": view_id, "mode": mode})
    return urlunparse((parsed.scheme or "https", parsed.netloc, "/ViewPublisherRSS.php", "", query, ""))


def _html_url(url: str) -> str:
    parsed = urlparse(url)
    view_id = parse_qs(parsed.query).get("view_id", [""])[0]
    if not view_id:
        raise ValueError(f"Carson City Granicus URL lacks view_id: {url!r}")
    query = urlencode({"view_id": view_id})
    return urlunparse((parsed.scheme or "https", parsed.netloc, "/ViewPublisher.php", "", query, ""))


def _host(url: str) -> str:
    return (urlparse(url).netloc.split("@")[-1].split(":")[0] or "").lower()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag.split(":", 1)[-1]


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parsed = scrape_calendar(DEFAULT_CALENDAR_URL)
    print(f"row_count: {len(parsed)}")
    for sample in parsed[:3]:
        print(f"{sample['meeting_date']} {sample['meeting_time']} {sample['meeting_title']}")
