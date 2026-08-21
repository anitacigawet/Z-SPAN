from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import date, datetime
from typing import Any, NamedTuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from requests.exceptions import RequestException

from polite_http import make_session


logger = logging.getLogger(__name__)

CALENDAR_URL = "https://www.tocc.us/meetings"
MAX_CALENDAR_BYTES = 1_000_000
MAX_ROWS = 200

ALLOWED_HOSTS = {"www.tocc.us", "tocc.us"}

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

EMPTY_BY_CONSTRUCTION_FIELDS = (
    "meeting_location",
    "agenda_packet_url",
    "ecomment_url",
)

BAD_SCHEMES = (
    "javascript:",
    "data:",
    "vbscript:",
    "file:",
    "mailto:",
    "ftp:",
    "gopher:",
)

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

CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
POSTED_DATE_RE = re.compile(r"^Posted On:\s*([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})$")
# Tested against: "5:30 a.m.", "5:30 p.m.", "5:30am", "5:30 AM".
TIME_RE = re.compile(
    r"(?<!\d)(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?(?=\s|$|[^\w.])",
    re.IGNORECASE,
)
AGENDA_PACKET_RE = re.compile(r"\bagenda\s+packet\b", re.IGNORECASE)
AGENDA_RE = re.compile(r"\bagenda\b", re.IGNORECASE)
MINUTES_RE = re.compile(r"\bminutes?\b", re.IGNORECASE)
VIDEO_RE = re.compile(r"\b(?:video|recording|youtube|vimeo)\b", re.IGNORECASE)
ECOMMENT_RE = re.compile(r"\b(?:ecomment|comment)\b", re.IGNORECASE)
ONCLICK_URL_RE = re.compile(r"['\"]((?:https?:)?//[^'\"]+|/[^'\"]+)['\"]")


class LinkEvidence(NamedTuple):
    label: str
    raw_href: str
    url: str


def scrape_calendar(calendar_url: str) -> list[dict]:
    """Scrape Colorado City's custom recent-meetings list."""
    html = _load_calendar_html(calendar_url)
    if not html:
        logger.warning(
            "scrape_summary url=%r counters=%s reason=empty_html_after_fetch",
            calendar_url,
            {"rows_seen": 0, "rows_accepted": 0},
        )
        return []

    soup = BeautifulSoup(html, "html.parser")
    meeting_list = _validate_custom_meetings_surface(soup, calendar_url)
    _log_global_links_ignored(soup, meeting_list)
    _declare_empty_by_construction(EMPTY_BY_CONSTRUCTION_FIELDS)

    counters: Counter[str] = Counter()
    meetings: list[dict[str, str]] = []
    seen_keys: set[tuple[str, str, str]] = set()
    cutoff = date.today().replace(day=1)
    exposed_rows = meeting_list.select(":scope > li")
    if len(exposed_rows) > MAX_ROWS:
        raise RuntimeError(
            f"Colorado City official meeting list exceeded the {MAX_ROWS}-row safety cap: "
            f"{len(exposed_rows)}"
        )

    for index, row in enumerate(exposed_rows, start=1):
        row_id = f"recent-meetings-li-{index}"
        counters["rows_seen"] += 1

        title = _extract_title(row, row_id, counters)
        if not title:
            counters["rows_dropped"] += 1
            counters["rows_dropped_missing_title"] += 1
            logger.warning("row_dropped row=%s reason=missing_title", row_id)
            continue

        posted_date = _extract_posted_date(row, row_id, title, counters)
        if posted_date is None:
            counters["rows_dropped"] += 1
            counters["rows_dropped_missing_posted_date"] += 1
            logger.warning("row_dropped row=%s title=%r reason=missing_year_source", row_id, title)
            continue

        meeting_date = _extract_meeting_date(row, posted_date, row_id, title, counters)
        if not meeting_date:
            counters["rows_dropped"] += 1
            counters["rows_dropped_missing_meeting_date"] += 1
            logger.warning("row_dropped row=%s title=%r reason=missing_visible_event_date", row_id, title)
            continue

        meeting_day = date.fromisoformat(meeting_date)
        if meeting_day < cutoff:
            counters["rows_dropped"] += 1
            counters["rows_dropped_before_current_month"] += 1
            logger.info(
                "row_dropped row=%s title=%r date=%s reason=before_current_month cutoff=%s",
                row_id,
                title,
                meeting_date,
                cutoff.isoformat(),
            )
            continue

        body_decision = _town_council_title_decision(title)
        if body_decision == "subordinate":
            counters["rows_dropped"] += 1
            counters["rows_dropped_known_subordinate_body"] += 1
            logger.info(
                "row_dropped row=%s title=%r reason=known_subordinate_body",
                row_id,
                title,
            )
            continue
        if body_decision != "council":
            raise RuntimeError(
                f"Colorado City current governing-body vocabulary drift at {row_id}: {title!r}"
            )

        meeting_time = _extract_meeting_time(row, row_id, title, counters)
        if not meeting_time:
            counters["meeting_time_absent_or_unparsed"] += 1

        row_links = _extract_row_links(row, calendar_url, row_id, title, counters)
        urls = _classify_links(row_links, row_id, title, counters)

        meeting = {
            "meeting_title": title,
            "meeting_date": meeting_date,
            "meeting_time": meeting_time,
            "meeting_location": "",
            "meeting_status": _meeting_status(title, urls),
            "agenda_url": urls["agenda_url"],
            "minutes_url": urls["minutes_url"],
            "video_url": urls["video_url"],
            "agenda_packet_url": urls["agenda_packet_url"],
            "ecomment_url": urls["ecomment_url"],
            "meeting_id": _extract_meeting_id(row_links, row_id, title, counters),
        }
        _validate_schema(meeting, row_id)

        meeting_key = (meeting["meeting_date"], meeting["meeting_title"], meeting["meeting_time"])
        if meeting_key in seen_keys:
            counters["rows_dropped"] += 1
            counters["rows_dropped_duplicate"] += 1
            logger.warning("row_dropped row=%s title=%r reason=duplicate key=%r", row_id, title, meeting_key)
            continue

        seen_keys.add(meeting_key)
        meetings.append(meeting)
        counters["rows_accepted"] += 1
        counters["meeting_location_absent_by_construction"] += 1
        counters["agenda_packet_url_absent_by_construction"] += 1
        counters["ecomment_url_absent_by_construction"] += 1
        logger.info(
            "row_accepted row=%s title=%r date=%s time=%r status=%s meeting_id=%r urls=%s",
            row_id,
            title,
            meeting_date,
            meeting_time,
            meeting["meeting_status"],
            meeting["meeting_id"],
            {field: bool(meeting[field]) for field in URL_FIELDS},
        )

    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Colorado City official meeting list contained no Town Council rows from %s forward",
            cutoff.isoformat(),
        )
    logger.info("scrape_summary url=%r counters=%s", calendar_url, dict(sorted(counters.items())))
    return meetings


def _town_council_title_decision(title: str) -> str:
    folded = " ".join(title.casefold().split())
    if re.search(r"(?<!\w)town council(?: meeting)?(?!\w)", folded):
        return "council"
    subordinate_terms = ("board", "commission", "committee", "authority", "advisory")
    if any(term in folded for term in subordinate_terms):
        return "subordinate"
    return "unknown"


def _load_calendar_html(calendar_url: str) -> str:
    session = make_session()
    try:
        return _fetch_text_bounded(session, calendar_url, MAX_CALENDAR_BYTES)
    except RequestException as exc:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "calendar_fetch_blocked honest_empty url=%r error=%s scope=all_meetings",
            calendar_url,
            exc,
        )
        return ""


def _fetch_text_bounded(session: Any, url: str, max_bytes: int) -> str:
    input_host = _host(url)
    if input_host not in ALLOWED_HOSTS:
        raise ValueError(f"Disallowed calendar host: {input_host}")

    with session.get(url, timeout=30, stream=True, allow_redirects=True) as response:
        response.raise_for_status()
        final_host = _host(response.url)
        if final_host not in ALLOWED_HOSTS:
            raise ValueError(f"Redirect to disallowed host: {final_host} (started from {url})")

        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"Response from {url} exceeded {max_bytes} bytes")

        return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")

def _validate_custom_meetings_surface(soup: BeautifulSoup, calendar_url: str) -> Tag:
    title = _clean_text(soup.title.get_text(" ", strip=True) if soup.title else "")
    meeting_blocks = [
        block for block in soup.select("div.blog.block") if "Recent meetings" in block.get_text(" ", strip=True)
    ]
    if not meeting_blocks:
        raise ValueError(
            "Colorado City custom surface mismatch: missing div.blog.block containing 'Recent meetings'"
        )

    meeting_list = meeting_blocks[0].find("ul")
    if not isinstance(meeting_list, Tag):
        raise ValueError("Colorado City custom surface mismatch: Recent meetings block has no ul")

    rows = meeting_list.select(":scope > li")
    rows_with_datetime = [row for row in rows if row.select_one("div.datetime")]
    if rows and not rows_with_datetime:
        raise ValueError("Colorado City custom surface mismatch: missing li rows with div.datetime")

    logger.warning(
        "vendor_fingerprint_witness url=%r title=%r tokens=%s rows=%d",
        calendar_url,
        title,
        ["div.blog.block", "Recent meetings", "ul > li", "div.datetime", "Posted On:"],
        len(rows_with_datetime),
    )
    return meeting_list


def _log_global_links_ignored(soup: BeautifulSoup, meeting_list: Tag) -> None:
    for anchor in soup.find_all("a", href=True):
        if meeting_list in anchor.parents:
            continue
        label = _clean_text(anchor.get_text(" ", strip=True))
        href = str(anchor.get("href", "")).strip()
        evidence = f"{label} {href}"
        if MINUTES_RE.search(evidence) or AGENDA_RE.search(evidence) or VIDEO_RE.search(evidence):
            logger.warning(
                "global_link_ignored field=document_urls href=%r label=%r reason=not_same_row_evidence",
                href,
                label,
            )


def _declare_empty_by_construction(fields: tuple[str, ...]) -> None:
    logger.warning(
        "field_absence_declared fields=%s reason=custom_recent_meetings_list_has_no_per_row_location_packet_or_ecomment_columns",
        fields,
    )


def _extract_title(row: Tag, row_id: str, counters: Counter[str]) -> str:
    title_tag = row.find("h2")
    if not isinstance(title_tag, Tag):
        logger.warning("field_empty row=%s field=meeting_title reason=missing_h2", row_id)
        return ""
    title = _clean_text(title_tag.get_text(" ", strip=True))
    if not title:
        logger.warning("field_empty row=%s field=meeting_title reason=empty_h2_text", row_id)
        return ""
    counters["meeting_title_emitted"] += 1
    return title


def _extract_posted_date(row: Tag, row_id: str, title: str, counters: Counter[str]) -> date | None:
    posted_tag = row.find("i", string=lambda value: bool(value and "Posted On:" in value))
    if not isinstance(posted_tag, Tag):
        logger.warning(
            "field_empty row=%s title=%r field=posted_date reason=missing_posted_on_year_source",
            row_id,
            title,
        )
        return None

    posted_text = _clean_text(posted_tag.get_text(" ", strip=True))
    match = POSTED_DATE_RE.match(posted_text[:80])
    if not match:
        logger.warning(
            "field_empty row=%s title=%r field=posted_date reason=unparsed_posted_on value=%r",
            row_id,
            title,
            posted_text,
        )
        return None

    month_name, day_text, year_text = match.groups()
    month = _month_number(month_name)
    if month == 0:
        logger.warning(
            "field_empty row=%s title=%r field=posted_date reason=unknown_month value=%r",
            row_id,
            title,
            month_name,
        )
        return None

    counters["posted_date_year_source_seen"] += 1
    return date(int(year_text), month, int(day_text))


def _extract_meeting_date(row: Tag, posted_date: date, row_id: str, title: str, counters: Counter[str]) -> str:
    datetime_block = row.select_one("div.datetime")
    if not isinstance(datetime_block, Tag):
        logger.warning("field_empty row=%s title=%r field=meeting_date reason=missing_datetime_block", row_id, title)
        return ""

    month_tag = datetime_block.find("strong")
    day_tag = datetime_block.find("p")
    if not isinstance(month_tag, Tag) or not isinstance(day_tag, Tag):
        logger.warning(
            "field_empty row=%s title=%r field=meeting_date reason=missing_datetime_month_or_day",
            row_id,
            title,
        )
        return ""

    month_text = _clean_text(month_tag.get_text(" ", strip=True))
    day_text = _clean_text(day_tag.get_text(" ", strip=True))
    month = _month_number(month_text)
    if month == 0 or not day_text.isdigit():
        logger.warning(
            "field_empty row=%s title=%r field=meeting_date reason=unparsed_datetime month=%r day=%r",
            row_id,
            title,
            month_text,
            day_text,
        )
        return ""

    event_date = date(posted_date.year, month, int(day_text))
    if event_date < posted_date and (posted_date - event_date).days > 180:
        event_date = date(posted_date.year + 1, month, int(day_text))
        counters["meeting_date_year_rollover_from_posted_date"] += 1
        logger.warning(
            "field_year_inferred row=%s title=%r field=meeting_date visible_month_day=%s-%s posted_date=%s inferred_date=%s reason=calendar_year_rollover",
            row_id,
            title,
            month_text,
            day_text,
            posted_date.isoformat(),
            event_date.isoformat(),
        )
    elif event_date < posted_date:
        counters["meeting_date_before_posted_date"] += 1
        logger.warning(
            "field_year_inferred row=%s title=%r field=meeting_date visible_month_day=%s-%s posted_date=%s inferred_date=%s reason=event_date_before_posted_date_same_year",
            row_id,
            title,
            month_text,
            day_text,
            posted_date.isoformat(),
            event_date.isoformat(),
        )

    counters["meeting_date_emitted"] += 1
    logger.info(
        "field_emitted row=%s title=%r field=meeting_date value=%s evidence=div.datetime_month_day_plus_posted_year posted_date=%s",
        row_id,
        title,
        event_date.isoformat(),
        posted_date.isoformat(),
    )
    return event_date.isoformat()


def _extract_meeting_time(row: Tag, row_id: str, title: str, counters: Counter[str]) -> str:
    datetime_block = row.select_one("div.datetime")
    if not isinstance(datetime_block, Tag):
        logger.warning("field_empty row=%s title=%r field=meeting_time reason=missing_datetime_block", row_id, title)
        return ""

    time_container = datetime_block.find("span")
    if not isinstance(time_container, Tag):
        logger.warning("field_empty row=%s title=%r field=meeting_time reason=missing_datetime_span", row_id, title)
        return ""

    time_text = _clean_text(time_container.get_text(" ", strip=True))
    match = TIME_RE.search(time_text[:80])
    if not match:
        logger.warning(
            "field_empty row=%s title=%r field=meeting_time reason=unparsed_time value=%r",
            row_id,
            title,
            time_text,
        )
        return ""

    hour_text, minute_text, suffix = match.groups()
    hour = int(hour_text)
    minute = int(minute_text or "00")
    if not 1 <= hour <= 12 or not 0 <= minute <= 59:
        logger.warning(
            "field_empty row=%s title=%r field=meeting_time reason=invalid_time_components value=%r",
            row_id,
            title,
            time_text,
        )
        return ""

    counters["meeting_time_emitted"] += 1
    return f"{hour}:{minute:02d} {suffix.upper()}M"


def _extract_row_links(
    row: Tag,
    calendar_url: str,
    row_id: str,
    title: str,
    counters: Counter[str],
) -> list[LinkEvidence]:
    links: list[LinkEvidence] = []
    for anchor in row.find_all("a", href=True):
        raw_href = str(anchor.get("href", "")).strip()
        label = _clean_text(anchor.get_text(" ", strip=True)) or title
        url = _emit_url(raw_href, calendar_url, row_id, "row_link")
        if not url:
            fallback = _fallback_url_from_attributes(anchor, calendar_url, row_id)
            if fallback:
                url = fallback
                counters["row_link_fallback_url_used"] += 1
            else:
                counters["row_links_dropped"] += 1
                logger.warning(
                    "link_dropped row=%s title=%r href=%r label=%r reason=url_validation_failed_no_fallback",
                    row_id,
                    title,
                    raw_href,
                    label,
                )
                continue

        counters["row_links_seen"] += 1
        links.append(LinkEvidence(label=label, raw_href=raw_href, url=url))

    if not links:
        logger.warning("field_empty row=%s title=%r field=row_links reason=no_valid_anchors", row_id, title)
    return links


def _classify_links(
    links: list[LinkEvidence],
    row_id: str,
    title: str,
    counters: Counter[str],
) -> dict[str, str]:
    urls = {field: "" for field in URL_FIELDS}
    for link in links:
        field = _classify_link(link)
        if not field:
            counters["row_links_unclassified"] += 1
            logger.warning(
                "link_unclassified row=%s title=%r href=%r label=%r reason=not_agenda_minutes_video_packet_or_ecomment",
                row_id,
                title,
                link.url,
                link.label,
            )
            continue

        if urls[field]:
            counters["row_links_dropped_duplicate_field"] += 1
            logger.warning(
                "link_dropped row=%s title=%r field=%s href=%r reason=field_already_populated existing=%r",
                row_id,
                title,
                field,
                link.url,
                urls[field],
            )
            continue

        urls[field] = link.url
        counters[f"{field}_emitted"] += 1
        logger.info("link_classified row=%s title=%r field=%s href=%r", row_id, title, field, link.url)

    return urls


def _classify_link(link: LinkEvidence) -> str:
    evidence = f"{link.label} {urlparse(link.url).path}".replace("-", " ")
    if ECOMMENT_RE.search(evidence):
        return "ecomment_url"
    if AGENDA_PACKET_RE.search(evidence):
        return "agenda_packet_url"
    if MINUTES_RE.search(evidence):
        return "minutes_url"
    if VIDEO_RE.search(evidence):
        return "video_url"
    if AGENDA_RE.search(evidence):
        return "agenda_url"
    logger.warning(
        "link_unclassified_in_helper label=%r url=%r reason=no_document_vocabulary_match",
        link.label,
        link.url,
    )
    return ""


def _extract_meeting_id(
    links: list[LinkEvidence],
    row_id: str,
    title: str,
    counters: Counter[str],
) -> str:
    if not links:
        logger.warning("field_empty row=%s title=%r field=meeting_id reason=no_row_link_slug", row_id, title)
        return ""

    path = urlparse(links[0].url).path.strip("/")
    if not path:
        logger.warning("field_empty row=%s title=%r field=meeting_id reason=empty_row_link_path", row_id, title)
        return ""

    counters["meeting_id_emitted_from_slug"] += 1
    return path


def _meeting_status(title: str, urls: dict[str, str]) -> str:
    if CANCELLED_RE.search(title[:240]):
        return "Cancelled"
    if urls["minutes_url"]:
        return "Minutes Available"
    if urls["agenda_url"] or urls["agenda_packet_url"]:
        return "Agenda Available"
    return "Scheduled"


def _emit_url(raw_href: str, base_url: str, row_id: str, field: str) -> str:
    href = raw_href.strip()
    if not href:
        logger.warning("url_dropped row=%s field=%s href=%r reason=empty_href", row_id, field, raw_href)
        return ""

    lowered = href.lower().lstrip()
    for bad_scheme in BAD_SCHEMES:
        if lowered.startswith(bad_scheme):
            logger.warning(
                "url_dropped row=%s field=%s href=%r reason=bad_scheme scheme=%s",
                row_id,
                field,
                raw_href,
                bad_scheme,
            )
            return ""

    absolute = urljoin(base_url, href)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        logger.warning(
            "url_dropped row=%s field=%s href=%r absolute=%r reason=bad_absolute_scheme",
            row_id,
            field,
            raw_href,
            absolute,
        )
        return ""

    emit_host = _host(absolute)
    if emit_host not in ALLOWED_HOSTS:
        logger.warning(
            "url_dropped row=%s field=%s href=%r absolute=%r reason=host_not_allowlisted host=%s",
            row_id,
            field,
            raw_href,
            absolute,
            emit_host,
        )
        return ""

    return absolute


def _fallback_url_from_attributes(anchor: Tag, calendar_url: str, row_id: str) -> str:
    for attr_name in ("data-href", "data-url", "data-link"):
        attr_value = str(anchor.get(attr_name, "")).strip()
        if not attr_value:
            continue
        url = _emit_url(attr_value, calendar_url, row_id, f"fallback_{attr_name}")
        if url:
            logger.warning(
                "url_fallback_used row=%s attr=%s href=%r reason=primary_href_unusable",
                row_id,
                attr_name,
                attr_value,
            )
            return url

    onclick = str(anchor.get("onclick", "")).strip()
    if onclick:
        match = ONCLICK_URL_RE.search(onclick[:500])
        if match:
            url = _emit_url(match.group(1), calendar_url, row_id, "fallback_onclick")
            if url:
                logger.warning("url_fallback_used row=%s attr=onclick reason=primary_href_unusable", row_id)
                return url
        logger.warning("url_fallback_failed row=%s attr=onclick value=%r reason=no_allowed_url", row_id, onclick[:160])

    return ""


def _validate_schema(meeting: dict[str, str], row_id: str) -> None:
    keys = tuple(meeting.keys())
    if keys != CANONICAL_FIELDS:
        raise ValueError(f"{row_id}: schema keys mismatch: {keys!r}")
    for field, value in meeting.items():
        if not isinstance(value, str):
            raise TypeError(f"{row_id}: field {field} must be str, got {type(value).__name__}")
    for field in URL_FIELDS:
        value = meeting[field]
        if value and not value.startswith(("http://", "https://")):
            raise ValueError(f"{row_id}: field {field} has non-http URL {value!r}")
        if value and _host(value) not in ALLOWED_HOSTS:
            raise ValueError(f"{row_id}: field {field} has disallowed host {value!r}")
    if meeting["meeting_status"] not in {"Scheduled", "Agenda Available", "Minutes Available", "Cancelled"}:
        raise ValueError(f"{row_id}: invalid meeting_status {meeting['meeting_status']!r}")
    datetime.strptime(meeting["meeting_date"], "%Y-%m-%d")


def _host(url: str) -> str:
    return urlparse(url).netloc.split(":")[0].lower()


def _month_number(month_name: str) -> int:
    return MONTHS.get(month_name.strip().lower().rstrip("."), 0)


def _clean_text(value: str) -> str:
    return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s:%(name)s:%(message)s")
    print(json.dumps(scrape_calendar(CALENDAR_URL), indent=2))
