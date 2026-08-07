import json
import logging
import re
from collections import OrderedDict
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag


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

DATE_RE = re.compile(r"\b(?P<month>\d{1,2})[-/](?P<day>\d{1,2})[-/](?P<year>\d{2,4})\b")
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
YEAR_ARCHIVE_RE = re.compile(r"/(?P<year>20\d{2})-agendas-minutes/?$")
BAD_SCHEMES = ("javascript:", "data:", "vbscript:", "file:", "mailto:", "ftp:", "gopher:")
PAGE_HOST = "holbrookaz.gov"
ALLOWED_EMIT_HOSTS = {PAGE_HOST, "drive.google.com"}
MAX_RESPONSE_BYTES = 5_000_000


def scrape_calendar(url: str) -> list[dict[str, str]]:
    session = requests.Session()
    main_html = _fetch_text_bounded(session, url, allowed_hosts={PAGE_HOST})
    main_soup = BeautifulSoup(main_html, "html.parser")
    _log_vendor_fingerprint(main_soup, url)

    year_urls = _discover_year_pages(main_soup, url)
    if not year_urls:
        logger.warning("no_year_archives_discovered base_url=%s", url)
        return []

    records: "OrderedDict[tuple[str, str], dict[str, str]]" = OrderedDict()
    unknown_sections: list[str] = []

    for year_url in year_urls:
        year_html = _fetch_text_bounded(session, year_url, allowed_hosts={PAGE_HOST})
        year_soup = BeautifulSoup(year_html, "html.parser")
        page_title = _clean_text(_first_heading_text(year_soup))
        content_root = _content_root(year_soup)
        current_section = ""
        seen_links = 0
        emitted_links = 0

        logger.info("fetch_year_archive url=%s title=%r", year_url, page_title)

        for element in content_root.find_all(["h1", "h2", "h3", "a"]):
            if not isinstance(element, Tag):
                continue

            text = _clean_text(element.get_text(" ", strip=True))
            if not text:
                continue

            if element.name in {"h1", "h2", "h3"}:
                section = _classify_heading(text)
                if section:
                    current_section = section
                continue

            if element.name != "a" or not element.has_attr("href"):
                continue

            parsed_date = _parse_date_from_text(text)
            if not parsed_date:
                continue

            seen_links += 1
            if current_section not in {"agenda", "minutes"}:
                unknown_sections.append(f"{year_url} :: {text}")
                logger.warning(
                    "drop_unclassified_document section=%r text=%r href=%r page=%s",
                    current_section,
                    text,
                    element.get("href", ""),
                    year_url,
                )
                continue

            document_url = _emit_url(element.get("href", ""), year_url, field=f"{current_section}_url", row_label=text)
            if not document_url:
                continue

            date_text, meeting_date = parsed_date
            meeting_title = _extract_title(text, date_text)
            record_key = (_key_title(meeting_title), meeting_date)
            record = records.setdefault(
                record_key,
                {
                    "meeting_title": meeting_title,
                    "meeting_date": meeting_date,
                    "agenda_url": "",
                    "minutes_url": "",
                },
            )

            field_name = "agenda_url" if current_section == "agenda" else "minutes_url"
            if record[field_name] and record[field_name] != document_url:
                logger.warning(
                    "duplicate_document_field field=%s title=%r date=%s kept=%s dropped=%s",
                    field_name,
                    meeting_title,
                    meeting_date,
                    record[field_name],
                    document_url,
                )
                continue

            record[field_name] = document_url
            emitted_links += 1

        logger.info(
            "year_archive_summary url=%s dated_links_seen=%d dated_links_emitted=%d",
            year_url,
            seen_links,
            emitted_links,
        )

    meetings = [_build_meeting(record) for record in records.values()]
    meetings.sort(key=lambda meeting: (meeting["meeting_date"], meeting["meeting_title"]))

    logger.warning(
        "field_absences count=%d fields=meeting_time,meeting_location,video_url,agenda_packet_url,ecomment_url,meeting_id reason=document_archive_page_exposes_only_titles_dates_agenda_minutes_links",
        len(meetings),
    )
    if unknown_sections:
        logger.warning("unclassified_document_links count=%d first_10=%r", len(unknown_sections), unknown_sections[:10])

    logger.info("scrape_complete meeting_count=%d source_year_pages=%d", len(meetings), len(year_urls))
    return meetings


def _fetch_text_bounded(session: requests.Session, url: str, allowed_hosts: set[str]) -> str:
    with session.get(url, timeout=30, stream=True, allow_redirects=True) as response:
        response.raise_for_status()
        final_host = _host(response.url)
        if not _host_allowed(final_host, allowed_hosts):
            raise ValueError(f"Redirect to disallowed host: {final_host} (started from {url})")

        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Response from {url} exceeded {MAX_RESPONSE_BYTES} bytes")

        return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _log_vendor_fingerprint(soup: BeautifulSoup, url: str) -> None:
    h1_text = _first_heading_text(soup)
    elementor_page = soup.select_one("div.elementor[data-elementor-type='wp-page']")
    year_links = soup.find_all("a", href=YEAR_ARCHIVE_RE)
    logger.info(
        "vendor_fingerprint url=%s h1=%r elementor_page=%s year_archive_link_count=%d pattern=%s",
        url,
        _clean_text(h1_text),
        bool(elementor_page),
        len(year_links),
        YEAR_ARCHIVE_RE.pattern,
    )


def _discover_year_pages(soup: BeautifulSoup, base_url: str) -> list[str]:
    year_urls: list[str] = []
    for anchor in soup.find_all("a", href=True):
        if not isinstance(anchor, Tag):
            continue

        href = str(anchor["href"])
        text = _clean_text(anchor.get_text(" ", strip=True))
        if not YEAR_ARCHIVE_RE.search(urlparse(href).path) and not YEAR_ARCHIVE_RE.search(href):
            continue
        if not re.search(r"\b20\d{2}\s+Documents\b", text):
            logger.warning("skip_year_archive_link_without_documents_label text=%r href=%r", text, href)
            continue

        archive_url = _emit_url(href, base_url, field="year_archive_url", row_label=text, allowed_hosts={PAGE_HOST})
        if archive_url and archive_url not in year_urls:
            year_urls.append(archive_url)

    logger.info("year_archives_discovered count=%d urls=%r", len(year_urls), year_urls)
    return year_urls


def _content_root(soup: BeautifulSoup) -> Tag:
    page_title = soup.find("h1", string=lambda value: bool(value and "Agendas" in value and "Minutes" in value))
    if isinstance(page_title, Tag):
        container = page_title.find_parent("div", class_=lambda value: bool(value and "e-con" in str(value).split()))
        if isinstance(container, Tag):
            return container
    body = soup.body
    if not isinstance(body, Tag):
        raise ValueError("No body element found in Holbrook archive page")
    logger.warning("content_root_fallback reason=missing_agendas_minutes_h1")
    return body


def _classify_heading(text: str) -> str:
    lowered = text.strip().lower()
    if lowered == "agendas":
        return "agenda"
    if lowered == "minutes":
        return "minutes"
    return ""


def _parse_date_from_text(text: str) -> tuple[str, str] | None:
    match = DATE_RE.search(text[:200])
    if not match:
        return None

    year = int(match.group("year"))
    if year < 100:
        year += 2000

    try:
        parsed = datetime(year, int(match.group("month")), int(match.group("day")))
    except ValueError:
        logger.warning("drop_unparseable_date text=%r date_text=%r", text, match.group(0))
        return None

    return match.group(0), parsed.strftime("%Y-%m-%d")


def _extract_title(text: str, date_text: str) -> str:
    title = _clean_text(text.replace(date_text, ""))
    title = re.sub(r"\bAgenda\b", "", title, flags=re.IGNORECASE)
    title = _clean_text(title)
    return title or "Council Meeting"


def _build_meeting(record: dict[str, str]) -> dict[str, str]:
    title = record["meeting_title"]
    agenda_url = record["agenda_url"]
    minutes_url = record["minutes_url"]

    if CANCELLED_RE.search(title[:200]):
        status = "Cancelled"
    elif minutes_url:
        status = "Minutes Available"
    elif agenda_url:
        status = "Agenda Available"
    else:
        status = "Scheduled"
        logger.warning("status_scheduled_without_documents title=%r date=%s", title, record["meeting_date"])

    meeting = {
        "meeting_title": title,
        "meeting_date": record["meeting_date"],
        "meeting_time": "",
        "meeting_location": "",
        "meeting_status": status,
        "agenda_url": agenda_url,
        "minutes_url": minutes_url,
        "video_url": "",
        "agenda_packet_url": "",
        "ecomment_url": "",
        "meeting_id": "",
    }
    return {field: meeting[field] for field in CANONICAL_FIELDS}


def _emit_url(
    href: str,
    base_url: str,
    *,
    field: str,
    row_label: str,
    allowed_hosts: set[str] | None = None,
) -> str:
    if not href:
        return ""

    stripped = href.strip()
    lowered = stripped.lower()
    if lowered.startswith(BAD_SCHEMES) or stripped == "#":
        logger.warning("drop_url_bad_scheme field=%s row=%r href=%r", field, row_label, href)
        return ""

    absolute = urljoin(base_url, stripped)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        logger.warning("drop_url_bad_scheme field=%s row=%r href=%r absolute=%r", field, row_label, href, absolute)
        return ""

    host = _host(absolute)
    hosts = allowed_hosts or ALLOWED_EMIT_HOSTS
    if not _host_allowed(host, hosts):
        logger.warning("drop_url_disallowed_host field=%s row=%r href=%r host=%r allowed=%r", field, row_label, href, host, sorted(hosts))
        return ""

    return absolute


def _host_allowed(host: str, allowed_hosts: set[str]) -> bool:
    for allowed in allowed_hosts:
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


def _host(url: str) -> str:
    return (urlparse(url).netloc.split(":")[0] or "").lower()


def _first_heading_text(soup: BeautifulSoup) -> str:
    heading = soup.find("h1")
    return heading.get_text(" ", strip=True) if isinstance(heading, Tag) else ""


def _clean_text(value: Any) -> str:
    text = BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)
    text = text.replace("\u2013", "-").replace("\u2014", "-").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip(" -")


def _key_title(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip().casefold()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    calendar_url = "https://holbrookaz.gov/government/council-agenda/"
    scraped = scrape_calendar(calendar_url)
    print(json.dumps({"count": len(scraped), "samples": scraped[:5]}, indent=2))
