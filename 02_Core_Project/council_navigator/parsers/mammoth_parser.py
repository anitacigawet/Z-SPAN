"""Town of Mammoth council agendas from the official site's bounded Vite bundle."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from html import unescape
import json
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from polite_http import make_session

__all__ = ["scrape_calendar"]

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://mammothaz.gov/council-agendas"
ALLOWED_HOSTS = {"mammothaz.gov", "www.mammothaz.gov"}
HTML_MAX_BYTES = 1_000_000
BUNDLE_MAX_BYTES = 3_000_000
CHUNK_SIZE = 65_536
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
BUNDLE_PATH_RE = re.compile(r"^/assets/[A-Za-z0-9_-]+\.js$")
YEAR_GROUP_RE = re.compile(r'\{year:"(?P<year>20\d{2})",items:\[(?P<body>.*?)\]\}', re.DOTALL)
ITEM_RE = re.compile(r'\{label:"(?P<label>(?:\\.|[^"\\])*)",href:"(?P<href>(?:\\.|[^"\\])*)"\}')
LABEL_DATE_RE = re.compile(
    r"^(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?[,]?\s+(20\d{2})\b",
    re.IGNORECASE,
)
CANCELLED_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)


def scrape_calendar(url: str = DEFAULT_URL) -> list[dict[str, str]]:
    """Return official Mammoth council agendas from this calendar month forward."""
    _validate_input_url(url)
    session = make_session()
    status, final_url, html = _fetch_bounded(session, url, HTML_MAX_BYTES)
    if status in {401, 403}:
        return _source_blocked("HTML shell", status, final_url)
    if status != 200:
        raise RuntimeError(f"Mammoth official council page returned HTTP {status}: {final_url}")

    bundle_url = _bundle_url(html, final_url)
    bundle_status, bundle_final_url, bundle = _fetch_bounded(session, bundle_url, BUNDLE_MAX_BYTES)
    if bundle_status in {401, 403}:
        return _source_blocked("Vite bundle", bundle_status, bundle_final_url)
    if bundle_status != 200:
        raise RuntimeError(
            f"Mammoth official council bundle returned HTTP {bundle_status}: {bundle_final_url}"
        )

    groups = _year_groups(bundle)
    current_year = str(date.today().year)
    if current_year not in groups or not groups[current_year]:
        raise RuntimeError(
            f"Mammoth current-year council bundle witness missing or empty: "
            f"current_year={current_year} years={sorted(groups)}"
        )
    logger.info(
        "Mammoth official Vite council-agenda fingerprint witnessed: bundle=%s years=%s current_year_items=%d",
        bundle_final_url,
        sorted(groups),
        len(groups[current_year]),
    )

    cutoff = date.today().replace(day=1)
    stats: Counter[str] = Counter()
    meetings: list[dict[str, str]] = []
    seen: set[str] = set()
    for position, (label, raw_href) in enumerate(groups[current_year], start=1):
        stats["current_year_items_seen"] += 1
        meeting_date = _parse_date(label, position)
        if date.fromisoformat(meeting_date) < cutoff:
            stats["before_current_month"] += 1
            continue
        agenda_url = _safe_url(raw_href, bundle_final_url, position)
        if not agenda_url:
            stats["unsafe_agenda_url"] += 1
            continue
        if agenda_url in seen:
            stats["duplicate"] += 1
            logger.warning("Mammoth row dropped: reason=duplicate position=%d url=%s", position, agenda_url)
            continue
        seen.add(agenda_url)
        title = _title(label, position)
        meeting = {
            "meeting_title": title,
            "meeting_date": meeting_date,
            "meeting_time": "",
            "meeting_location": "",
            "meeting_status": "Cancelled" if CANCELLED_RE.search(title) else "Agenda Available",
            "agenda_url": agenda_url,
            "minutes_url": "",
            "video_url": "",
            "agenda_packet_url": "",
            "ecomment_url": "",
            "meeting_id": "",
        }
        _validate_meeting(meeting)
        meetings.append(meeting)
        stats["rows_accepted"] += 1
        logger.info("Mammoth meeting emitted: date=%s title=%r agenda=%s", meeting_date, title, agenda_url)

    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "Mammoth witnessed official current-year agenda group has no rows from cutoff=%s: stats=%s",
            cutoff.isoformat(),
            dict(stats),
        )
    logger.warning(
        "Mammoth scrape summary: current_year_items=%d accepted=%d drop_reasons=%s "
        "fields_absent_by_construction=%s",
        stats["current_year_items_seen"],
        stats["rows_accepted"],
        {key: value for key, value in stats.items() if key not in {"current_year_items_seen", "rows_accepted"}},
        {
            "meeting_time": stats["rows_accepted"],
            "meeting_location": stats["rows_accepted"],
            "minutes_url": stats["rows_accepted"],
            "video_url": stats["rows_accepted"],
            "agenda_packet_url": stats["rows_accepted"],
            "ecomment_url": stats["rows_accepted"],
            "meeting_id": stats["rows_accepted"],
        },
    )
    return meetings


def _source_blocked(surface: str, status: int, final_url: str) -> list[dict[str, str]]:
    logger.warning("health_empty_kind=source_blocked")
    logger.warning(
        "Mammoth official %s blocked the neutral paced request: "
        "status=%d final_url=%s missing_data_scope=all_current_council_agendas",
        surface,
        status,
        final_url,
    )
    return []


def _validate_input_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        raise ValueError(f"Mammoth parser called with disallowed URL: {url!r}")
    if not parsed.path.casefold().rstrip("/").endswith("/council-agendas"):
        raise ValueError(f"Mammoth parser called with unexpected path: {url!r}")


def _fetch_bounded(session: Any, url: str, max_bytes: int) -> tuple[int, str, str]:
    with session.get(url, timeout=30, stream=True, allow_redirects=True) as response:
        final_host = (urlparse(response.url).hostname or "").lower()
        if final_host not in ALLOWED_HOSTS:
            raise ValueError(f"Mammoth redirect reached disallowed host: {final_host}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ValueError(f"Mammoth response exceeded {max_bytes} bytes: {response.url}")
        return response.status_code, response.url, bytes(body).decode(response.encoding or "utf-8", "replace")


def _bundle_url(html: str, base_url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    title = _clean(soup.title.get_text(" ", strip=True) if soup.title else "")
    candidates = []
    for script in soup.find_all("script", src=True):
        raw = _clean(script.get("src"))
        if _clean(script.get("type")).casefold() == "module" and BUNDLE_PATH_RE.fullmatch(urlparse(raw).path):
            candidates.append(raw)
    if "town of mammoth" not in title.casefold() or len(candidates) != 1:
        raise RuntimeError(
            f"Mammoth official Vite shell fingerprint drifted: title={title!r} bundles={candidates!r}"
        )
    emitted = _safe_url(candidates[0], base_url, 0)
    if not emitted:
        raise RuntimeError(f"Mammoth Vite bundle URL failed validation: {candidates[0]!r}")
    return emitted


def _year_groups(bundle: str) -> dict[str, list[tuple[str, str]]]:
    groups: dict[str, list[tuple[str, str]]] = {}
    for match in YEAR_GROUP_RE.finditer(bundle):
        year = match.group("year")
        items: list[tuple[str, str]] = []
        for item in ITEM_RE.finditer(match.group("body")):
            label = _decode_js_string(item.group("label"))
            href = _decode_js_string(item.group("href"))
            items.append((label, href))
        agenda_items = [item for item in items if "/documents/council-agendas/" in item[1]]
        other_items = [item for item in items if "/documents/council-agendas/" not in item[1]]
        if not agenda_items:
            logger.info(
                "Mammoth compiled non-agenda year group ignored: year=%s items=%d paths=%s",
                year,
                len(other_items),
                sorted({urlparse(href).path.rsplit("/", 2)[-2] for _, href in other_items}),
            )
            continue
        if other_items:
            raise RuntimeError(
                f"Mammoth compiled year group mixes council agendas with other documents: "
                f"year={year} other_paths={[href for _, href in other_items[:3]]!r}"
            )
        items = agenda_items
        if year in groups:
            if groups[year] != items:
                raise RuntimeError(f"Mammoth bundle contains conflicting council year groups: {year}")
            logger.info("Mammoth bundle repeated an identical compiled year group: year=%s", year)
            continue
        groups[year] = items
    if not groups:
        raise RuntimeError("Mammoth official bundle no longer contains council year groups")
    return groups


def _decode_js_string(value: str) -> str:
    try:
        decoded = json.loads(f'"{value}"')
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Mammoth bundle contains invalid encoded string: {value[:120]!r}") from exc
    if not isinstance(decoded, str):
        raise RuntimeError("Mammoth bundle string decoded to a non-string value")
    return _clean(decoded)


def _parse_date(label: str, position: int) -> str:
    match = LABEL_DATE_RE.search(label)
    if match is None:
        raise RuntimeError(f"Mammoth current-year agenda has unparseable date: position={position} label={label!r}")
    raw = f"{match.group(1)} {match.group(2)} {match.group(3)}"
    try:
        return datetime.strptime(raw, "%B %d %Y").date().isoformat()
    except ValueError as exc:
        raise RuntimeError(f"Mammoth agenda has invalid date: position={position} label={label!r}") from exc


def _title(label: str, position: int) -> str:
    match = LABEL_DATE_RE.search(label)
    if match is None:
        raise RuntimeError(f"Mammoth agenda title lacks date witness: position={position} label={label!r}")
    remainder = label[match.end() :].strip(" -–—")
    remainder = re.sub(r"\bagenda\b", "", remainder, flags=re.IGNORECASE).strip(" -–—")
    remainder = " ".join(remainder.split())
    if not remainder:
        return "Mammoth Town Council Meeting"
    if "council" not in remainder.casefold():
        remainder = f"Town Council {remainder}"
    title = f"Mammoth {remainder}"
    if CANCELLED_RE.search(label) and not CANCELLED_RE.search(title):
        title = f"{title} - Cancelled"
    return title


def _safe_url(raw: str, base_url: str, position: int) -> str:
    if not raw or raw.casefold().startswith(("//", "javascript:", "data:", "file:", "mailto:", "ftp:")):
        logger.warning(
            "Mammoth URL dropped: position=%d reason=empty_or_disallowed_scheme value=%r",
            position,
            raw,
        )
        return ""
    absolute = urljoin(base_url, raw)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        logger.warning(
            "Mammoth URL dropped: position=%d reason=disallowed_host value=%r",
            position,
            raw,
        )
        return ""
    return absolute


def _validate_meeting(meeting: dict[str, str]) -> None:
    if tuple(meeting) != FIELDS:
        raise RuntimeError(f"Mammoth canonical schema drifted: {tuple(meeting)}")
    if any(not isinstance(value, str) for value in meeting.values()):
        raise RuntimeError(f"Mammoth canonical values must be strings: {meeting!r}")


def _clean(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = unescape(str(value))
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return " ".join(text.split())
