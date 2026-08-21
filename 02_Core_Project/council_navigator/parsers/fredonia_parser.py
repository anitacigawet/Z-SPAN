"""Current-window parser for Fredonia's official Streamline council feed."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from polite_http import make_session


logger = logging.getLogger(__name__)

DEFAULT_URL = "https://www.fredoniaaz.gov/council-meeting"
FETCH_HOSTS = {"fredoniaaz.gov", "www.fredoniaaz.gov"}
EMIT_HOSTS = FETCH_HOSTS | {"d2blwilx4xw5sk.cloudfront.net", "streamline.imgix.net"}
MAX_RESPONSE_BYTES = 2_000_000
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
COUNCIL_RE = re.compile(r"\b(?:town|city)?\s*council\b", re.IGNORECASE)
NON_COUNCIL_RE = re.compile(
    r"\b(?:planning\s+(?:and\s+zoning\s+)?commission|board\s+of\s+adjustment|"
    r"library\s+board|fire\s+district)\b",
    re.IGNORECASE,
)
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
TIME_RE = re.compile(
    r"(?<!\d)(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([AP])\.?M\.?(?=\s|$|[^\w.])",
    re.IGNORECASE,
)


def scrape_calendar(url: str | None = None) -> list[dict[str, str]]:
    """Return Fredonia council meetings from the current calendar month forward."""
    target = _validate_source_url(url or DEFAULT_URL)
    floor = date.today().replace(day=1)

    with make_session() as session:
        html = _fetch_html_bounded(session, target)
    if html is None:
        return []

    meetings = _parse_html(html, target, floor)
    _assert_schema(meetings)
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
    logger.info(
        "Fredonia scrape complete: current_month_forward=%d floor=%s",
        len(meetings),
        floor.isoformat(),
    )
    return meetings


def _fetch_html_bounded(session: Any, url: str) -> str | None:
    with session.get(url, timeout=35, stream=True, allow_redirects=True) as response:
        final_host = _host(response.url)
        if final_host not in FETCH_HOSTS:
            raise ValueError(f"Fredonia redirect reached disallowed host: {final_host}")

        body = bytearray()
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Fredonia response exceeded {MAX_RESPONSE_BYTES} bytes")
        text = bytes(body).decode(response.encoding or "utf-8", errors="replace")

        if response.status_code in {401, 403, 429} or _is_managed_challenge(
            response.status_code, text
        ):
            logger.warning("health_empty_kind=source_blocked")
            logger.warning(
                "Fredonia official source blocked: status=%s final_url=%s "
                "failure_shape=honest-empty missing_scope=current_council_meetings",
                response.status_code,
                response.url,
            )
            return None

        response.raise_for_status()
        return text


def _parse_html(
    html: str,
    source_url: str,
    floor: date,
) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    articles = _validate_surface(soup, html)
    meetings: dict[tuple[str, str], dict[str, str]] = {}
    rows_seen = historical = dropped = 0

    for index, article in enumerate(articles, start=1):
        rows_seen += 1
        row_label = f"row={index}"
        meeting_date = _meeting_date(article, row_label)
        if date.fromisoformat(meeting_date) < floor:
            historical += 1
            continue

        row_text = _clean_text(article)
        if NON_COUNCIL_RE.search(row_text):
            dropped += 1
            logger.warning(
                "Fredonia row dropped: row=%s date=%s reason=explicit_non_council_body text=%r",
                index,
                meeting_date,
                row_text[:240],
            )
            continue
        if not COUNCIL_RE.search(row_text):
            raise ValueError(
                "Fredonia current Streamline row is governing-body ambiguous: "
                f"row={index} text={row_text[:300]!r}"
            )

        title = _meeting_title(article, row_label)
        meeting_id = _meeting_id(article, source_url, row_label)
        key = (meeting_date, meeting_id or title.casefold())
        record = meetings.setdefault(
            key,
            _new_record(
                title=title,
                meeting_date=meeting_date,
                meeting_time=_meeting_time(article, row_label),
                meeting_id=meeting_id,
            ),
        )
        for field, emitted_url in _document_urls(article, source_url, row_label).items():
            if not emitted_url:
                continue
            if record[field] and record[field] != emitted_url:
                logger.warning(
                    "Fredonia duplicate document dropped: row=%s field=%s kept=%s dropped=%s",
                    index,
                    field,
                    record[field],
                    emitted_url,
                )
            else:
                record[field] = emitted_url

    result = sorted(meetings.values(), key=lambda row: (row["meeting_date"], row["meeting_title"]))
    for record in result:
        record["meeting_status"] = _status(record)

    logger.warning(
        "Fredonia field absence: meeting_location,video_url,ecomment_url "
        "lack per-row signals on the Streamline council cards"
    )
    logger.info(
        "Fredonia source summary: rows_seen=%d historical_ignored=%d rows_dropped=%d accepted=%d",
        rows_seen,
        historical,
        dropped,
        len(result),
    )
    return result


def _validate_surface(soup: BeautifulSoup, html: str) -> list[Tag]:
    page_title = _clean_text(soup.title)
    streamline_witness = (
        "Powered by Streamline" in html
        or "poc-type-meeting" in html
        or bool(re.search(r"planBrand\s*=\s*['\"]streamline['\"]", html, re.IGNORECASE))
    )
    if "Council Meeting - Town of Fredonia" not in page_title or not streamline_witness:
        raise ValueError(
            "Fredonia Streamline council fingerprint drifted: "
            f"title={page_title!r} streamline_witness={streamline_witness}"
        )

    articles = [article for article in soup.select("article.poc-type-meeting") if isinstance(article, Tag)]
    if not articles:
        text = _clean_text(soup).casefold()
        if "no meetings" in text or "no results" in text:
            logger.info("Fredonia Streamline council page exposes an explicit empty state")
            return []
        raise ValueError("Fredonia council page has no meeting cards and no explicit empty state")

    logger.info(
        "Fredonia fingerprint witnessed: title=%r article_count=%d",
        page_title,
        len(articles),
    )
    return articles


def _meeting_date(article: Tag, row_label: str) -> str:
    time_tag = article.find("time")
    raw = str(time_tag.get("datetime", "")).strip() if isinstance(time_tag, Tag) else ""
    match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:T|$)", raw)
    if not match:
        raise ValueError(f"Fredonia {row_label} date signal drifted: {raw!r}")
    try:
        return date.fromisoformat(match.group(1)).isoformat()
    except ValueError as exc:
        raise ValueError(f"Fredonia {row_label} date is invalid: {raw!r}") from exc


def _meeting_title(article: Tag, row_label: str) -> str:
    heading = article.find(["h2", "h3"])
    raw = _clean_text(heading)
    if not raw:
        raise ValueError(f"Fredonia {row_label} has no meeting heading")

    remainder = re.sub(r"^Council\s+Meeting\b", "", raw, count=1, flags=re.IGNORECASE).strip(" -")
    if not remainder or remainder.casefold() == "meeting":
        return "Town Council Meeting"
    if COUNCIL_RE.search(remainder):
        return remainder
    if re.search(r"\b(?:regular|special|emergency|work)\s+(?:meeting|session)\b", remainder, re.IGNORECASE):
        return f"Town Council {remainder}"
    raise ValueError(f"Fredonia {row_label} title vocabulary drifted: {raw!r}")


def _meeting_time(article: Tag, row_label: str) -> str:
    time_tag = article.find("time")
    raw_datetime = str(time_tag.get("datetime", "")).strip() if isinstance(time_tag, Tag) else ""
    if "T" in raw_datetime:
        time_part = raw_datetime.split("T", 1)[1]
        match_24 = re.match(r"^([01]\d|2[0-3]):([0-5]\d)", time_part)
        if match_24 and match_24.group(0) != "00:00":
            hour = int(match_24.group(1))
            minute = int(match_24.group(2))
            return f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"

    match = TIME_RE.search(_clean_text(article)[:2_000])
    if match:
        return f"{int(match.group(1))}:{match.group(2) or '00'} {match.group(3).upper()}M"

    logger.warning(
        "Fredonia meeting_time absent: %s reason=no_per_row_time_signal",
        row_label,
    )
    return ""


def _document_urls(
    article: Tag,
    source_url: str,
    row_label: str,
) -> dict[str, str]:
    urls = {"agenda_url": "", "minutes_url": "", "agenda_packet_url": ""}
    for anchor in article.find_all("a", href=True):
        label = _clean_text(anchor)
        lowered = label.casefold()
        if "minutes" in lowered:
            field = "minutes_url"
        elif "agenda packet" in lowered or lowered == "packet":
            field = "agenda_packet_url"
        elif "agenda" in lowered:
            field = "agenda_url"
        else:
            href_path = urlparse(str(anchor.get("href", ""))).path.casefold()
            if href_path.endswith((".pdf", ".doc", ".docx")):
                logger.warning(
                    "Fredonia document dropped: %s href=%r label=%r reason=unrecognized_document_label",
                    row_label,
                    anchor.get("href", ""),
                    label,
                )
            continue

        emitted = _emit_url(str(anchor.get("href", "")), source_url, field, row_label)
        if not emitted:
            continue
        if urls[field] and urls[field] != emitted:
            logger.warning(
                "Fredonia duplicate URL dropped: %s field=%s kept=%s dropped=%s",
                row_label,
                field,
                urls[field],
                emitted,
            )
            continue
        urls[field] = emitted
    return urls


def _meeting_id(article: Tag, source_url: str, row_label: str) -> str:
    source_path = urlparse(source_url).path.rstrip("/")
    for anchor in article.find_all("a", href=True):
        absolute = urljoin(source_url, str(anchor.get("href", "")))
        parsed = urlparse(absolute)
        if _host(absolute) not in FETCH_HOSTS:
            continue
        path = parsed.path.rstrip("/")
        if not path or path == source_path or "/files/" in path:
            continue
        slug = path.split("/")[-1]
        if slug and slug not in {"council-meeting", "engage"}:
            logger.info("Fredonia meeting_id witnessed: %s slug=%r", row_label, slug)
            return slug
    logger.warning("Fredonia meeting_id absent: %s reason=no_detail_slug", row_label)
    return ""


def _new_record(
    *,
    title: str,
    meeting_date: str,
    meeting_time: str,
    meeting_id: str,
) -> dict[str, str]:
    return {
        "meeting_title": title,
        "meeting_date": meeting_date,
        "meeting_time": meeting_time,
        "meeting_location": "",
        "meeting_status": "Scheduled",
        "agenda_url": "",
        "minutes_url": "",
        "video_url": "",
        "agenda_packet_url": "",
        "ecomment_url": "",
        "meeting_id": meeting_id,
    }


def _status(record: dict[str, str]) -> str:
    if CANCELLED_RE.search(record["meeting_title"]):
        return "Cancelled"
    if record["minutes_url"]:
        return "Minutes Available"
    if record["agenda_url"] or record["agenda_packet_url"]:
        return "Agenda Available"
    return "Scheduled"


def _emit_url(href: str, base_url: str, field: str, row_label: str) -> str:
    candidate = urljoin(base_url, href.strip())
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or _host(candidate) not in EMIT_HOSTS:
        logger.warning(
            "Fredonia URL dropped: %s field=%s href=%r reason=scheme_or_host_not_allowlisted",
            row_label,
            field,
            href,
        )
        return ""
    return candidate


def _validate_source_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or _host(url) not in FETCH_HOSTS:
        raise ValueError("Fredonia source URL must use HTTPS on the official town host")
    return url


def _is_managed_challenge(status_code: int, text: str) -> bool:
    lowered = text.casefold()
    return status_code in {403, 503} and (
        "just a moment" in lowered
        or "challenges.cloudflare.com" in lowered
        or "access denied" in lowered
    )


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _clean_text(value: object) -> str:
    return " ".join(BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True).split())


def _assert_schema(meetings: list[dict[str, str]]) -> None:
    for index, meeting in enumerate(meetings):
        if tuple(meeting) != CANONICAL_FIELDS:
            raise ValueError(f"Fredonia row {index} schema mismatch: {tuple(meeting)}")
        if any(not isinstance(value, str) for value in meeting.values()):
            raise ValueError(f"Fredonia row {index} contains a non-string value")
