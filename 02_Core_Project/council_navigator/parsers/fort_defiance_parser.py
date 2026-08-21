from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from polite_http import make_session

BASE_URL = "https://ftdefiance.navajochapters.org/blog/category/chapter-meeting-agenda-and-minutes/"
FETCH_HOSTS = {"ftdefiance.navajochapters.org"}
MAX_RESPONSE_BYTES = 2_000_000
FIELD_NAMES = (
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
MONTH_DATE_RE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"[-_ .]+([0-9]{1,2})[-_, .]+([0-9]{4})\b",
    re.IGNORECASE,
)
NUMERIC_DATE_RE = re.compile(r"\b(0?[1-9]|1[0-2])[-_.](0?[1-9]|[12]\d|3[01])[-_.](\d{4})\b")
CHAPTER_MEETING_RE = re.compile(r"\bchapter\s+meeting\b", re.IGNORECASE)
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)

logger = logging.getLogger(__name__)

def standardize_date(date_str: str) -> str:
    """Parse the WordPress publication date for archive-horizon evidence only."""
    try:
        return datetime.strptime(date_str, "%b %d, %Y").date().isoformat()
    except ValueError:
        logger.warning("Fort Defiance publication date is unparseable: value=%r", date_str)
        return ""

def scrape_post_details(post: object, base_url: str, row_label: str) -> dict[str, str]:
    """Classify same-article documents without any secondary requests."""
    details = {"agenda_url": "", "minutes_url": ""}
    content = post.select_one("div.entry-content, div.entry-content-area, div.post-content")
    if content is None:
        logger.warning("Fort Defiance article has no content container: row=%r", row_label)
        return details
    for anchor in content.find_all("a", href=True):
        label = _clean_text(anchor)
        probe = f"{label} {anchor.get('href', '')}".casefold()
        field = "agenda_url" if "agenda" in probe else "minutes_url" if "minute" in probe else ""
        if not field:
            logger.warning(
                "Fort Defiance article link dropped: row=%r label=%r reason=unclassified_document",
                row_label,
                label,
            )
            continue
        emitted = _emit_url(anchor.get("href", ""), base_url, row_label, field)
        if emitted and not details[field]:
            details[field] = emitted
    return details

def scrape_calendar(url: str | None = None) -> list[dict[str, str]]:
    """Read current Fort Defiance Chapter Meeting rows from the newest category page."""
    target = url or BASE_URL
    _validate_source_url(target)
    with make_session() as session:
        html = _fetch_text_bounded(session, target)
    soup = BeautifulSoup(html, "html.parser")
    articles = soup.find_all("article")
    _validate_fingerprint(soup, articles)

    current_floor = date.today().replace(day=1).isoformat()
    meetings: list[dict[str, str]] = []
    latest_publication = ""
    rows_dropped = 0
    for article in articles:
        title_anchor = article.select_one("h2.entry-title a[href], h1.entry-title a[href]")
        if title_anchor is None:
            logger.warning("Fort Defiance article dropped: reason=missing_entry_title_link")
            rows_dropped += 1
            continue
        title = _clean_text(title_anchor)
        post_url = _emit_url(title_anchor.get("href", ""), target, title, "post_url")
        if not post_url:
            rows_dropped += 1
            continue
        published_node = article.select_one("span.published")
        published = standardize_date(_clean_text(published_node)) if published_node else ""
        latest_publication = max(latest_publication, published)

        content = article.select_one("div.entry-content, div.entry-content-area, div.post-content")
        content_text = _clean_text(content)
        scope_evidence = f"{title} {content_text}"
        if not CHAPTER_MEETING_RE.search(scope_evidence):
            logger.warning(
                "Fort Defiance article dropped: title=%r reason=no_chapter_meeting_evidence",
                title,
            )
            rows_dropped += 1
            continue

        evidence_parts = [title, content_text]
        if content is not None:
            evidence_parts.extend(anchor.get("href", "") for anchor in content.find_all("a", href=True))
        meeting_date = _extract_exact_date(" ".join(evidence_parts))
        if not meeting_date:
            if published and published >= current_floor:
                raise RuntimeError(
                    "Fort Defiance current Chapter Meeting article lacks an exact meeting date: "
                    f"title={title!r} publication_date={published}"
                )
            rows_dropped += 1
            continue
        if meeting_date < current_floor:
            continue

        details = scrape_post_details(article, target, title)
        status = (
            "Cancelled"
            if CANCELLED_RE.search(scope_evidence)
            else "Minutes Available"
            if details["minutes_url"]
            else "Agenda Available"
            if details["agenda_url"]
            else "Scheduled"
        )
        meeting = _empty_row()
        meeting.update(
            {
                "meeting_title": title,
                "meeting_date": meeting_date,
                "meeting_status": status,
                "agenda_url": details["agenda_url"],
                "minutes_url": details["minutes_url"],
                "meeting_id": urlparse(post_url).path.rstrip("/").rsplit("/", 1)[-1],
            }
        )
        meetings.append(meeting)

    meetings.sort(key=lambda item: (item["meeting_date"], item["meeting_title"]))
    _assert_schema(meetings)
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Fort Defiance official Chapter Meeting category is accessible with no "
            "current-month-forward rows; latest_publication=%s current_floor=%s",
            latest_publication,
            current_floor,
        )
    logger.info(
        "Fort Defiance scrape summary: articles_seen=%d rows_accepted=%d rows_dropped=%d",
        len(articles),
        len(meetings),
        rows_dropped,
    )
    return meetings


def _fetch_text_bounded(session: object, url: str) -> str:
    with session.get(url, timeout=35, stream=True, allow_redirects=True) as response:
        final_host = (urlparse(response.url).hostname or "").casefold()
        if final_host not in FETCH_HOSTS:
            raise ValueError(f"Fort Defiance redirect reached disallowed host: {final_host}")
        body = bytearray()
        for chunk in response.iter_content(64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Fort Defiance response exceeded {MAX_RESPONSE_BYTES} bytes")
        if response.status_code in {401, 403, 429}:
            logger.warning("health_empty_kind=source_blocked")
        response.raise_for_status()
        return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _validate_source_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() not in FETCH_HOSTS:
        raise ValueError("Fort Defiance source must use HTTPS on the official chapter host")


def _validate_fingerprint(soup: BeautifulSoup, articles: list[object]) -> None:
    title = _clean_text(soup.title)
    if "Chapter Meeting Agenda and Minutes" not in title or not articles:
        raise ValueError("Fort Defiance WordPress category fingerprint drifted")
    if not any(article.select_one("span.published") for article in articles):
        raise ValueError("Fort Defiance category lost its WordPress publication-date witness")
    logger.info(
        "vendor fingerprint witness=Chapter_Meeting_Agenda_and_Minutes_plus_articles_plus_published"
    )


def _extract_exact_date(value: str) -> str:
    match = MONTH_DATE_RE.search(value[:3000])
    if match:
        for fmt in ("%b %d %Y", "%B %d %Y"):
            try:
                return datetime.strptime(" ".join(match.groups()), fmt).date().isoformat()
            except ValueError:
                continue
        logger.warning("Fort Defiance meeting date is invalid: evidence=%r", value[:300])
        return ""
    match = NUMERIC_DATE_RE.search(value[:3000])
    if match:
        try:
            return date(int(match.group(3)), int(match.group(1)), int(match.group(2))).isoformat()
        except ValueError:
            logger.warning("Fort Defiance numeric meeting date is invalid: evidence=%r", value[:300])
            return ""
    logger.warning("Fort Defiance meeting date extraction returned empty: evidence=%r", value[:300])
    return ""


def _emit_url(href: str, base_url: str, row_label: str, field: str) -> str:
    absolute = urljoin(base_url, str(href or "").strip())
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").casefold() not in FETCH_HOSTS:
        logger.warning(
            "Fort Defiance URL dropped: field=%s row=%r href=%r reason=scheme_or_host_not_allowlisted",
            field,
            row_label,
            href,
        )
        return ""
    return absolute


def _empty_row() -> dict[str, str]:
    return {field: "" for field in FIELD_NAMES}


def _clean_text(value: object) -> str:
    return " ".join(BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True).split())


def _assert_schema(meetings: list[dict[str, str]]) -> None:
    for index, meeting in enumerate(meetings):
        if tuple(meeting) != FIELD_NAMES:
            raise ValueError(f"Fort Defiance row {index} schema mismatch: {tuple(meeting)}")
        if any(not isinstance(value, str) for value in meeting.values()):
            raise ValueError(f"Fort Defiance row {index} contains a non-string value")

if __name__ == '__main__':
    # Example execution for testing
    meetings = scrape_calendar()
    print(f"\n--- Scraper Test Results ---")
    print(f"Total meetings found: {len(meetings)}")
    if meetings:
        print("\nFirst 7 meetings:")
        for m in meetings[:7]:
            print(f"  Title: {m['meeting_title']}")
            print(f"  Date: {m['meeting_date']}")
            print(f"  Agenda: {m['agenda_url']}")
            print(f"  Minutes: {m['minutes_url']}")
            print("-" * 20)
