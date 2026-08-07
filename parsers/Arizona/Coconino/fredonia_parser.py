"""Fredonia — Streamline meeting parser."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag


logger = logging.getLogger(__name__)

FIELDS = (
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

ALLOWED_HOSTS = {
    "www.fredoniaaz.gov",
    "fredoniaaz.gov",
    "d2blwilx4xw5sk.cloudfront.net",
    "streamline.imgix.net",
}
FLAGGED_HOSTS = {"fredoniaaz.net"}
HTML_MAX_BYTES = 5_000_000
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

TIME_RE = re.compile(
    r"\b(\d{1,2}:\d{2})\s*([AaPp]\.?[Mm]\.?)(?=\s|$|[^\w.])",
    re.IGNORECASE,
)
CANCELLED_RE = re.compile(r"\bcancell?ed\b", re.IGNORECASE)

_BAD_SCHEMES = (
    "javascript:",
    "data:",
    "vbscript:",
    "file:",
    "mailto:",
    "ftp:",
    "gopher:",
)


def _host_from_url(url: str) -> str:
    return (urlparse(url).netloc.split(":")[0] or "").lower()


def _validate_host(url: str, context: str) -> None:
    host = _host_from_url(url)
    if host in FLAGGED_HOSTS or host.endswith(".fredoniaaz.net"):
        raise ValueError(f"{context} uses flagged host: {host}")
    if host not in ALLOWED_HOSTS:
        raise ValueError(f"{context} uses disallowed host: {host}")


def _fetch_text_bounded(session: requests.Session, url: str) -> str:
    _validate_host(url, "Request URL")
    with session.get(url, timeout=30, stream=True, allow_redirects=True, verify=True) as response:
        response.raise_for_status()
        _validate_host(response.url, "Redirect target")

        body = bytearray()
        for chunk in response.iter_content(chunk_size=65_536):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > HTML_MAX_BYTES:
                raise ValueError(f"Response from {url} exceeded {HTML_MAX_BYTES} bytes")

        encoding = response.encoding or "utf-8"
        return bytes(body).decode(encoding, errors="replace")


def _witness_streamline(html: str) -> bool:
    signatures = {
        "poc-type-meeting": "poc-type-meeting" in html,
        "Powered by Streamline": "Powered by Streamline" in html,
        "planBrand='streamline'": bool(
            re.search(r"planBrand\s*=\s*['\"]streamline['\"]", html, re.IGNORECASE)
        ),
    }
    for token, witnessed in signatures.items():
        if witnessed:
            logger.info("Streamline vendor fingerprint witnessed: %s", token)
            return True

    logger.warning(
        "Streamline vendor fingerprint absent; checked signatures=%s",
        list(signatures),
    )
    return False


def _clean_text(value: str) -> str:
    return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)


def _article_text(article: Tag) -> str:
    return article.get_text(" ", strip=True)


def _extract_title(article: Tag, row_label: str) -> str:
    heading = article.find(["h2", "h3"])
    if heading is None:
        logger.warning("%s: no h2/h3 title found; emitting empty title", row_label)
        return ""
    title = _clean_text(str(heading))
    if not title:
        logger.warning("%s: h2/h3 title sanitized to empty string", row_label)
    return title


def _extract_date(article: Tag, row_label: str) -> str:
    time_tag = article.find("time")
    datetime_value = ""
    if isinstance(time_tag, Tag):
        datetime_value = str(time_tag.get("datetime", "")).strip()

    match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", datetime_value)
    if match:
        return match.group(1)

    logger.warning("%s: no parseable ISO date in time datetime=%r; dropping row", row_label, datetime_value)
    return ""


def _format_time(hour: int, minute: int) -> str:
    suffix = "AM" if hour < 12 else "PM"
    display_hour = hour % 12
    if display_hour == 0:
        display_hour = 12
    return f"{display_hour}:{minute:02d} {suffix}"


def _normalize_time_match(match: re.Match[str]) -> str:
    hour_text, minute_text = match.group(1).split(":", 1)
    suffix = match.group(2).replace(".", "").upper()
    hour = int(hour_text)
    minute = int(minute_text)
    if hour < 1 or hour > 12 or minute > 59:
        return ""
    return f"{hour}:{minute:02d} {suffix}"


def _extract_time(article: Tag, row_label: str) -> str:
    time_tag = article.find("time")
    datetime_value = ""
    if isinstance(time_tag, Tag):
        datetime_value = str(time_tag.get("datetime", "")).strip()

    if "T" in datetime_value:
        try:
            parsed = datetime.fromisoformat(datetime_value.replace("Z", "+00:00"))
        except ValueError:
            logger.warning("%s: datetime attribute has unparseable time component: %r", row_label, datetime_value)
        else:
            if parsed.hour or parsed.minute:
                meeting_time = _format_time(parsed.hour, parsed.minute)
                logger.info("%s: meeting_time extracted from time datetime=%r", row_label, datetime_value)
                return meeting_time

    text_match = TIME_RE.search(_article_text(article)[:2_000])
    if text_match:
        meeting_time = _normalize_time_match(text_match)
        if meeting_time:
            logger.info("%s: meeting_time extracted from article text", row_label)
            return meeting_time

    logger.warning("%s: no same-row time evidence found; emitting empty meeting_time", row_label)
    return ""


def _emit_url(href: str, base_url: str, field: str, row_label: str) -> str:
    raw_href = (href or "").strip()
    if not raw_href:
        return ""

    low = raw_href.lower().lstrip()
    for bad_scheme in _BAD_SCHEMES:
        if low.startswith(bad_scheme):
            logger.warning(
                "%s: dropped URL for %s; rejected_href=%r reason=bad_scheme:%s",
                row_label,
                field,
                raw_href,
                bad_scheme[:-1],
            )
            return ""

    absolute = urljoin(base_url, raw_href)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        logger.warning(
            "%s: dropped URL for %s; rejected_href=%r reason=bad_scheme_after_join:%s",
            row_label,
            field,
            raw_href,
            parsed.scheme,
        )
        return ""

    host = (parsed.netloc.split(":")[0] or "").lower()
    if host in FLAGGED_HOSTS or host.endswith(".fredoniaaz.net"):
        logger.warning(
            "%s: dropped URL for %s; rejected_href=%r reason=flagged_host:%s",
            row_label,
            field,
            raw_href,
            host,
        )
        return ""
    if host not in ALLOWED_HOSTS:
        logger.warning(
            "%s: dropped URL for %s; rejected_href=%r reason=disallowed_host:%s",
            row_label,
            field,
            raw_href,
            host,
        )
        return ""

    return absolute


def _is_document_href(href: str) -> bool:
    path = urlparse(href).path.lower()
    return path.endswith((".pdf", ".doc", ".docx"))


def _classify_document_link(anchor: Tag, row_label: str) -> str:
    label = anchor.get_text(" ", strip=True).lower()
    href = str(anchor.get("href", ""))
    if not urlparse(href).path.lower().endswith(".pdf"):
        logger.warning("%s: non-PDF document link found in Streamline card; href=%r", row_label, href)
    if "minute" in label:
        logger.info("%s: classified document link as minutes_url; label=%r href=%r", row_label, label, href)
        return "minutes_url"
    if "agenda packet" in label or "packet" in label:
        logger.info("%s: classified document link as agenda_packet_url; label=%r href=%r", row_label, label, href)
        return "agenda_packet_url"
    if "agenda" in label:
        logger.info("%s: classified document link as agenda_url; label=%r href=%r", row_label, label, href)
        return "agenda_url"

    logger.warning(
        "%s: PDF link did not match agenda/minutes/packet labels; falling back to agenda_url; label=%r href=%r",
        row_label,
        label,
        href,
    )
    return "agenda_url"


def _extract_document_urls(article: Tag, calendar_url: str, row_label: str) -> dict[str, str]:
    urls = {"agenda_url": "", "minutes_url": "", "agenda_packet_url": ""}
    for anchor in article.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        if not _is_document_href(href):
            continue

        field = _classify_document_link(anchor, row_label)
        emitted = _emit_url(href, calendar_url, field, row_label)
        if not emitted:
            continue
        if urls[field]:
            logger.warning(
                "%s: duplicate %s PDF ignored; kept=%r dropped=%r",
                row_label,
                field,
                urls[field],
                emitted,
            )
            continue
        urls[field] = emitted

    return urls


def _extract_meeting_id(article: Tag, calendar_url: str, meeting_date: str, row_label: str) -> str:
    root_slug_candidate = ""
    for anchor in article.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        absolute = urljoin(calendar_url, href)
        parsed = urlparse(absolute)
        if (parsed.netloc.split(":")[0] or "").lower() not in ALLOWED_HOSTS:
            continue
        match = re.search(r"/council-meeting/([^/?#]+)", parsed.path)
        if match:
            slug = match.group(1).strip()
            if slug:
                logger.info("%s: meeting_id extracted from detail slug=%r", row_label, slug)
                return slug
        if not root_slug_candidate and parsed.path.startswith("/") and "/files/" not in parsed.path:
            root_slug = parsed.path.strip("/")
            if root_slug and root_slug != "council-meeting" and "/" not in root_slug:
                root_slug_candidate = root_slug

    if root_slug_candidate:
        logger.warning(
            "%s: specified /council-meeting/<slug> detail link absent; meeting_id extracted from root slug=%r",
            row_label,
            root_slug_candidate,
        )
        return root_slug_candidate

    logger.warning("%s: no detail slug found; meeting_id falling back to meeting_date=%r", row_label, meeting_date)
    return meeting_date


def _determine_status(meeting_title: str, urls: dict[str, str], row_label: str) -> str:
    if CANCELLED_RE.search(meeting_title[:500]):
        return "Cancelled"
    if urls["minutes_url"]:
        return "Minutes Available"
    if urls["agenda_url"] or urls["agenda_packet_url"]:
        return "Agenda Available"
    logger.info("%s: meeting_status defaulted to Scheduled with no document/cancellation evidence", row_label)
    return "Scheduled"


def _empty_record() -> dict[str, str]:
    return {field: "" for field in FIELDS}


def scrape_calendar(calendar_url: str) -> list[dict]:
    """Scrape Fredonia Streamline meeting cards into canonical meeting rows."""
    logger.info("Streamline vendor: meeting_location not exposed in calendar cards; emitting '' for all rows")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    html = _fetch_text_bounded(session, calendar_url)

    if not _witness_streamline(html):
        return []

    soup = BeautifulSoup(html, "html.parser")
    articles = soup.select("article.poc-type-meeting")
    if not articles:
        logger.warning(
            "Streamline surface sanity check failed: article_count=%d html_length=%d",
            len(articles),
            len(html),
        )
        return []

    meetings: list[dict[str, str]] = []
    rows_seen = 0
    rows_dropped = 0
    drop_reasons: dict[str, int] = {}

    for index, article in enumerate(articles, start=1):
        rows_seen += 1
        preliminary_date = _extract_date(article, f"row={index}")
        preliminary_id = _extract_meeting_id(article, calendar_url, preliminary_date, f"row={index}")
        row_label = f"row={index} meeting_id={preliminary_id or ''}"
        logger.info("%s: processing Streamline article", row_label)

        if not preliminary_date:
            rows_dropped += 1
            drop_reasons["missing_date"] = drop_reasons.get("missing_date", 0) + 1
            logger.warning("%s: dropped row reason=missing_date", row_label)
            continue

        meeting_title = _extract_title(article, row_label)
        urls = _extract_document_urls(article, calendar_url, row_label)
        status = _determine_status(meeting_title, urls, row_label)

        record = _empty_record()
        record.update(
            {
                "meeting_title": meeting_title,
                "meeting_date": preliminary_date,
                "meeting_time": _extract_time(article, row_label),
                "meeting_location": "",
                "meeting_status": status,
                "agenda_url": urls["agenda_url"],
                "minutes_url": urls["minutes_url"],
                "video_url": "",
                "agenda_packet_url": urls["agenda_packet_url"],
                "ecomment_url": "",
                "meeting_id": preliminary_id or preliminary_date,
            }
        )
        meetings.append(record)

    logger.info(
        "Fredonia Streamline scrape complete: rows_seen=%d rows_accepted=%d rows_dropped=%d drop_reasons=%s",
        rows_seen,
        len(meetings),
        rows_dropped,
        drop_reasons,
    )
    return meetings


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = scrape_calendar("https://www.fredoniaaz.gov/council-meeting")
    print(json.dumps(result, indent=2))
    print(f"count={len(result)}", file=sys.stderr)
