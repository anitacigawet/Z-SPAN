import json
import logging
import re
from collections import OrderedDict
from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from requests.exceptions import RequestException

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

DATE_RE = re.compile(r"\b(?P<month>\d{1,2})[-/](?P<day>\d{1,2})[-/](?P<year>\d{2,4})\b")
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
YEAR_ARCHIVE_RE = re.compile(r"/(?P<year>20\d{2})-agendas-minutes/?$")
BAD_SCHEMES = ("javascript:", "data:", "vbscript:", "file:", "mailto:", "ftp:", "gopher:")
PAGE_HOST = "holbrookaz.gov"
ALLOWED_EMIT_HOSTS = {PAGE_HOST, "drive.google.com"}
MAX_RESPONSE_BYTES = 5_000_000
SOFT_BLOCK_TITLE = "one moment, please..."
SOFT_BLOCK_BODY_MARKER = "please wait while your request is being verified"
FLAGSHIP_TITLE_RE = re.compile(
    r"^(?:Regular Meeting|Special Meeting|Work Session|Public Hearing)$",
    re.IGNORECASE,
)
RECOGNIZED_NON_MEETING_RE = re.compile(
    r"^Statement of Legal Action$",
    re.IGNORECASE,
)


class SourceBlocked(RuntimeError):
    """The official Holbrook surface could not be read politely."""


def scrape_calendar(url: str) -> list[dict[str, str]]:
    source_url = (url or "").strip()
    parsed_source = urlparse(source_url)
    if (
        parsed_source.scheme != "https"
        or _host(source_url) != PAGE_HOST
        or not parsed_source.path.startswith("/government/council-agenda")
    ):
        raise ValueError(f"Holbrook source URL is not the approved council page: {source_url!r}")
    month_floor = date.today().replace(day=1)

    try:
        with make_session() as session:
            main_html = _fetch_text_bounded(session, source_url, allowed_hosts={PAGE_HOST})
            main_soup = BeautifulSoup(main_html, "html.parser")
            year_urls = _discover_year_pages(main_soup, source_url)
            current_year_urls = [
                item
                for item in year_urls
                if urlparse(item).path.rstrip("/").endswith(
                    f"/{month_floor.year}-agendas-minutes"
                )
            ]
            _validate_main_fingerprint(main_soup, source_url, current_year_urls)
            year_url = current_year_urls[0]
            year_html = _fetch_text_bounded(session, year_url, allowed_hosts={PAGE_HOST})
    except SourceBlocked as exc:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning(
            "Holbrook official council archive was blocked: failure_shape=honest-empty "
            "missing_scope=current_month_forward_city_council error=%s",
            exc,
        )
        return []

    records: "OrderedDict[tuple[str, str], dict[str, str]]" = OrderedDict()
    unknown_sections: list[str] = []
    ambiguous_current_titles: list[str] = []
    year_soup = BeautifulSoup(year_html, "html.parser")
    _validate_year_fingerprint(year_soup, year_url, month_floor.year)
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
        date_text, meeting_date = parsed_date
        if date.fromisoformat(meeting_date) < month_floor:
            logger.warning(
                "drop_historical_document text=%r meeting_date=%s floor=%s href=%r",
                text,
                meeting_date,
                month_floor.isoformat(),
                element.get("href", ""),
            )
            continue

        meeting_title = _extract_title(text, date_text)
        if RECOGNIZED_NON_MEETING_RE.fullmatch(meeting_title):
            logger.warning(
                "drop_recognized_non_meeting title=%r date=%s href=%r",
                meeting_title,
                meeting_date,
                element.get("href", ""),
            )
            continue
        if not FLAGSHIP_TITLE_RE.fullmatch(meeting_title):
            ambiguous_current_titles.append(meeting_title)
            logger.warning(
                "drop_ambiguous_current_council_document title=%r date=%s href=%r",
                meeting_title,
                meeting_date,
                element.get("href", ""),
            )
            continue

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

        document_url = _emit_url(
            element.get("href", ""),
            year_url,
            field=f"{current_section}_url",
            row_label=text,
        )
        if not document_url:
            continue

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

    if ambiguous_current_titles:
        raise ValueError(
            "Holbrook current archive exposed unrecognized council document titles: "
            f"{sorted(set(ambiguous_current_titles))!r}"
        )

    meetings = [_build_meeting(record) for record in records.values()]
    meetings.sort(key=lambda meeting: (meeting["meeting_date"], meeting["meeting_title"]))

    logger.warning(
        "field_absences count=%d fields=meeting_time,meeting_location,video_url,agenda_packet_url,ecomment_url,meeting_id reason=document_archive_page_exposes_only_titles_dates_agenda_minutes_links",
        len(meetings),
    )
    if unknown_sections:
        logger.warning("unclassified_document_links count=%d first_10=%r", len(unknown_sections), unknown_sections[:10])

    logger.info("scrape_complete meeting_count=%d source_year_pages=1", len(meetings))
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Holbrook official current-year council archive was accessible but had no "
            "qualifying meetings from %s forward",
            month_floor.isoformat(),
        )
    return meetings


def _fetch_text_bounded(session: object, url: str, allowed_hosts: set[str]) -> str:
    try:
        response_context = session.get(url, timeout=30, stream=True, allow_redirects=True)
    except RequestException as exc:
        raise SourceBlocked(f"request failed url={url}: {exc}") from exc

    with response_context as response:
        try:
            response.raise_for_status()
        except RequestException as exc:
            raise SourceBlocked(f"HTTP failure url={url}: {exc}") from exc
        final_host = _host(response.url)
        if not _host_allowed(final_host, allowed_hosts):
            raise ValueError(f"Redirect to disallowed host: {final_host} (started from {url})")

        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise SourceBlocked(f"Response from {url} exceeded {MAX_RESPONSE_BYTES} bytes")

        text = bytes(body).decode(response.encoding or "utf-8", errors="replace")
        _raise_if_soft_blocked(text, url)
        return text


def _raise_if_soft_blocked(html: str, url: str) -> None:
    soup = BeautifulSoup(html, "html.parser")
    title = _clean_text(soup.title.get_text(" ", strip=True) if soup.title else "")
    body_text = _clean_text(soup.get_text(" ", strip=True)[:2_000])
    if (
        title.casefold() == SOFT_BLOCK_TITLE
        and SOFT_BLOCK_BODY_MARKER in body_text.casefold()
    ):
        raise SourceBlocked(
            "HTTP 200 verification interstitial "
            f"url={url} title={title!r} marker={SOFT_BLOCK_BODY_MARKER!r}"
        )


def _validate_main_fingerprint(
    soup: BeautifulSoup,
    url: str,
    current_year_urls: list[str],
) -> None:
    h1_text = _first_heading_text(soup)
    elementor_page = soup.select_one("div.elementor[data-elementor-type='wp-page']")
    year_links = soup.find_all("a", href=YEAR_ARCHIVE_RE)
    if _clean_text(h1_text) != "Agendas & Minutes" or elementor_page is None:
        raise ValueError(
            f"Holbrook main council archive fingerprint changed: h1={_clean_text(h1_text)!r} "
            f"elementor={bool(elementor_page)} url={url}"
        )
    if len(current_year_urls) != 1:
        raise ValueError(
            f"Holbrook main archive exposed {len(current_year_urls)} current-year links: "
            f"{current_year_urls!r}"
        )
    logger.info(
        "vendor_fingerprint url=%s h1=%r elementor_page=%s year_archive_link_count=%d pattern=%s",
        url,
        _clean_text(h1_text),
        bool(elementor_page),
        len(year_links),
        YEAR_ARCHIVE_RE.pattern,
    )


def _validate_year_fingerprint(soup: BeautifulSoup, url: str, year: int) -> None:
    headings = [_clean_text(item.get_text(" ", strip=True)) for item in soup.find_all(["h1", "h2", "h3"])]
    if f"{year} Agendas & Minutes" not in headings:
        raise ValueError(f"Holbrook year archive heading changed: year={year} headings={headings!r}")
    if "Agendas" not in headings or "Minutes" not in headings:
        raise ValueError(f"Holbrook year archive document headings changed: headings={headings!r}")
    logger.info("Holbrook year archive fingerprint matched year=%s url=%s", year, url)


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
    logger.warning(
        "heading_not_document_section heading=%r reason=not_exact_agendas_or_minutes",
        text,
    )
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
    if parsed.scheme != "https":
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
