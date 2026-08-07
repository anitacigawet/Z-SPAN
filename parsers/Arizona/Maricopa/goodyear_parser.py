"""Goodyear — custom website meeting parser."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
import json
import logging
import re
import sys
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

__all__ = ["scrape_calendar"]

logger = logging.getLogger(__name__)

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

# The city's Legistar instance is a stale archive; current meetings are exposed
# on the city website.
_DEFAULT_URL = "https://www.goodyearaz.gov/government/council-meetings/calendar-council-meetings"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_MAX_RESPONSE_BYTES = 10_000_000
_CHUNK_SIZE = 65_536
_SOURCE_HOSTS = {"goodyearaz.gov", "www.goodyearaz.gov"}
ALLOWED_HOSTS = {
    "goodyearaz.gov",
    "www.goodyearaz.gov",
    # Current Goodyear recordings are hosted on OpenMedia.
    "goodyearaz.open.media",
}
FLAGGED_HOSTS: set[str] = set()
_BAD_SCHEMES = ("javascript:", "data:", "vbscript:", "file:", "mailto:", "ftp:", "gopher:")
_CANCELLED_RE = re.compile(r"\bcancell?ed\b", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-_/](0?[1-9]|1[0-2])[-_/](0?[1-9]|[12]\d|3[01])(?!\d)")
_US_DATE_RE = re.compile(r"(?<!\d)(0?[1-9]|1[0-2])[-_/](0?[1-9]|[12]\d|3[01])[-_/](20\d{2})(?!\d)")
_MONTH_DATE_RE = re.compile(
    r"\b("
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?"
    r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,)?\s+(20\d{2})\b",
    re.IGNORECASE,
)
# Must match "5:30 p.m.", "5:30 P.M.", "5:30 PM", and "5:30pm"; do not use \b after a dot.
_TIME_RE = re.compile(r"(?<!\d)(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([AP])\.?\s*M\.?(?=\s|$|[^\w.])", re.IGNORECASE)
_MONTHS = {
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


@dataclass(frozen=True)
class _FetchResult:
    text: str
    status_code: int
    final_url: str
    headers: dict[str, str]


@dataclass
class _Stats:
    rows_seen: int = 0
    rows_accepted: int = 0
    rows_dropped: int = 0
    anchors_seen: int = 0
    url_rejections: Counter[str] = field(default_factory=Counter)
    drop_reasons: Counter[str] = field(default_factory=Counter)

    def drop(self, reason: str) -> None:
        self.rows_dropped += 1
        self.drop_reasons[reason] += 1


def scrape_calendar(url: str) -> list[dict]:
    """Scrape Goodyear's current .gov council calendar into canonical meeting rows."""
    stats = _Stats()
    session = requests.Session()
    session.headers.update({"User-Agent": _USER_AGENT})

    logger.info(
        "Goodyear scrape started: url=%s source_hosts=%s allowed_emit_hosts=%s flagged_hosts=%s "
        "calendar_format=unknown stale_legistar_fallback=disabled tls_verify=true max_bytes=%d",
        url,
        sorted(_SOURCE_HOSTS),
        sorted(ALLOWED_HOSTS),
        sorted(FLAGGED_HOSTS),
        _MAX_RESPONSE_BYTES,
    )

    fetch = _fetch_text_bounded(session, url)
    blocker, blocker_evidence = _detect_blocker(fetch)
    logger.info(
        "Goodyear fetch observed: status=%d final_url=%s bytes=%d headers=%s",
        fetch.status_code,
        fetch.final_url,
        len(fetch.text.encode("utf-8", errors="replace")),
        {key: fetch.headers.get(key, "") for key in ("server", "x-reference-error", "cf-mitigated", "sg-captcha")},
    )

    if blocker:
        logger.warning(
            "Goodyear architectural blocker detected: blocker_class=%s failure_shape=honest-empty "
            "missing_data_scope=all meetings evidence=%s; returning []",
            blocker,
            blocker_evidence,
        )
        logger.warning(
            "Goodyear fingerprint witness blocked: observed blocker=%s, not city calendar markup; "
            "stale Legistar archive intentionally not fetched",
            blocker,
        )
        _log_summary(stats)
        return []

    if fetch.status_code != 200:
        _log_summary(stats)
        raise ValueError(
            f"Goodyear non-blocker HTTP status {fetch.status_code} from {fetch.final_url}; "
            "not returning a silent empty result"
        )

    soup = BeautifulSoup(fetch.text, "html.parser")
    _log_fingerprint_witness(soup, fetch.text)
    meetings = _parse_meetings(soup, fetch.final_url, stats)
    _log_summary(stats)
    return meetings


def _fetch_text_bounded(session: requests.Session, url: str) -> _FetchResult:
    start_host = (urlparse(url).hostname or "").lower()
    if not _source_host_allowed(start_host):
        raise ValueError(f"Goodyear parser called with disallowed source host: {start_host}")

    with session.get(url, timeout=30, stream=True, allow_redirects=True, verify=True) as response:
        final_host = (urlparse(response.url).hostname or "").lower()
        if not _source_host_allowed(final_host):
            raise ValueError(f"Redirect to disallowed host: {final_host} (started from {url})")

        body = bytearray()
        for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
            body.extend(chunk)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise ValueError(f"Response from {url} exceeded {_MAX_RESPONSE_BYTES} bytes")

        encoding = response.encoding or "utf-8"
        return _FetchResult(
            text=bytes(body).decode(encoding, errors="replace"),
            status_code=response.status_code,
            final_url=response.url,
            headers={key.lower(): value for key, value in response.headers.items()},
        )


def _detect_blocker(fetch: _FetchResult) -> tuple[str, dict[str, str | int | bool]]:
    sample = fetch.text[:20_000]
    lowered = sample.lower()
    decoded = _clean_text(sample).lower()
    title = _extract_html_title(sample)
    server = fetch.headers.get("server", "")
    evidence: dict[str, str | int | bool] = {
        "status": fetch.status_code,
        "title": title,
        "server": server,
        "x_reference_error": fetch.headers.get("x-reference-error", ""),
        "has_errors_edgesuite": "errors.edgesuite.net" in lowered or "errors.edgesuite.net" in decoded,
        "has_reference_number": "reference #" in decoded or "reference&#32;&#35;" in lowered,
    }

    if fetch.status_code == 403 and evidence["has_errors_edgesuite"]:
        return "akamai_edgesuite_403", evidence
    if title.lower() == "access denied" and ("akamaighost" in server.lower() or "ghost" in server.lower()):
        return "akamai_access_denied", evidence
    if "akamaighost" in server.lower() and evidence["has_reference_number"]:
        return "akamai_reference_block", evidence

    if (
        fetch.headers.get("cf-mitigated", "").lower() == "challenge"
        or "challenges.cloudflare.com" in lowered
        or "__cf_chl" in lowered
        or title.lower().startswith("just a moment")
    ):
        return "cloudflare_challenge", evidence

    if (
        fetch.headers.get("sg-captcha", "").lower() == "challenge"
        or "/.well-known/sgcaptcha/" in lowered
        or "sgcaptcha" in lowered
    ):
        return "siteground_sg_captcha", evidence

    return "", evidence


def _log_fingerprint_witness(soup: BeautifulSoup, html: str) -> None:
    title = _clean_text(soup.title.get_text(" ", strip=True) if soup.title else "")
    lowered = html[:50_000].lower()
    indicators = {
        "title": title,
        "title_contains_goodyear": "goodyear" in title.lower(),
        "body_contains_city_of_goodyear": "city of goodyear" in lowered,
        "body_contains_goodyearaz_gov": "goodyearaz.gov" in lowered,
        "body_contains_council_meetings": "council meeting" in lowered or "council-meetings" in lowered,
    }
    if indicators["title_contains_goodyear"] and (
        indicators["body_contains_city_of_goodyear"] or indicators["body_contains_council_meetings"]
    ):
        logger.info("Goodyear .gov city-site fingerprint witnessed: %s", indicators)
    else:
        logger.warning("Goodyear fingerprint warning: expected city .gov calendar chrome not witnessed: %s", indicators)


def _parse_meetings(soup: BeautifulSoup, base_url: str, stats: _Stats) -> list[dict]:
    rows = _candidate_rows(soup)
    logger.info("Goodyear candidate row discovery complete: candidate_rows=%d", len(rows))
    if not rows:
        logger.warning("Goodyear accessible page had zero candidate meeting rows; returning [] as witnessed empty page")

    meetings: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()
    for row_index, row in enumerate(rows, start=1):
        stats.rows_seen += 1
        row_text = _clean_text(row.get_text(" ", strip=True))
        evidence = f"row_index={row_index} text={row_text[:500]!r}"

        meeting_date = _extract_meeting_date(row_text, evidence)
        if not meeting_date:
            stats.drop("missing_or_ambiguous_date")
            logger.warning("Goodyear row dropped: reason=missing_or_ambiguous_date evidence=%s", evidence)
            continue

        meeting_title = _extract_title(row, row_text, evidence)
        if not meeting_title:
            stats.drop("missing_title")
            logger.warning("Goodyear row dropped: reason=missing_title date=%s evidence=%s", meeting_date, evidence)
            continue

        key = (meeting_date, meeting_title)
        if key in seen_keys:
            stats.drop("duplicate_date_title")
            logger.warning("Goodyear row dropped: reason=duplicate_date_title key=%s evidence=%s", key, evidence)
            continue
        seen_keys.add(key)

        meeting_time = _extract_meeting_time(row_text, evidence)
        meeting_location = _extract_meeting_location(row_text, evidence)
        urls = _extract_row_urls(row, base_url, evidence, stats)
        meeting_id = _extract_meeting_id(row, evidence)
        status = _derive_status(meeting_title, urls["agenda_url"], urls["minutes_url"], urls["agenda_packet_url"])
        meeting = _build_meeting(
            meeting_title=meeting_title,
            meeting_date=meeting_date,
            meeting_time=meeting_time,
            meeting_location=meeting_location,
            meeting_status=status,
            agenda_url=urls["agenda_url"],
            minutes_url=urls["minutes_url"],
            video_url=urls["video_url"],
            agenda_packet_url=urls["agenda_packet_url"],
            ecomment_url=urls["ecomment_url"],
            meeting_id=meeting_id,
        )
        stats.rows_accepted += 1
        logger.info("Goodyear meeting emitted: fields=%s evidence=%s", meeting, evidence)
        meetings.append(meeting)

    return meetings


def _candidate_rows(soup: BeautifulSoup) -> list[Tag]:
    candidates: list[Tag] = []
    seen_text: set[str] = set()
    for selector in ("tr", "li", "article", ".event", ".calendar-event", ".list-item", ".item"):
        for node in soup.select(selector):
            if not isinstance(node, Tag):
                continue
            text = _clean_text(node.get_text(" ", strip=True))
            if len(text) < 10 or not _extract_dates(text):
                continue
            lowered = text.lower()
            if "meeting" not in lowered and "council" not in lowered and "agenda" not in lowered:
                logger.info("Goodyear dated node skipped as non-meeting candidate: selector=%s text=%r", selector, text[:250])
                continue
            key = re.sub(r"\s+", " ", text.lower())[:500]
            if key in seen_text:
                continue
            seen_text.add(key)
            candidates.append(node)
    return candidates


def _extract_meeting_date(row_text: str, evidence: str) -> str:
    dates = _extract_dates(row_text)
    if len(dates) == 1:
        logger.info("Goodyear meeting_date emitted from row text: date=%s evidence=%s", dates[0], evidence)
        return dates[0]
    if not dates:
        logger.warning("Goodyear meeting_date empty: no parseable row-level date evidence=%s", evidence)
    else:
        logger.warning("Goodyear meeting_date ambiguous: dates=%s evidence=%s", dates, evidence)
    return ""


def _extract_dates(value: str) -> list[str]:
    cleaned = _clean_text(unquote(value))[:1_000]
    dates: set[str] = set()
    for match in _ISO_DATE_RE.finditer(cleaned):
        parsed = _date_from_parts(match.group(1), match.group(2), match.group(3), match.group(0))
        if parsed:
            dates.add(parsed)
    for match in _US_DATE_RE.finditer(cleaned):
        parsed = _date_from_parts(match.group(3), match.group(1), match.group(2), match.group(0))
        if parsed:
            dates.add(parsed)
    for match in _MONTH_DATE_RE.finditer(cleaned):
        month_name, day_token, year_token = match.groups()
        parsed = _date_from_parts(year_token, str(_MONTHS[month_name[:3].lower()]), day_token, match.group(0))
        if parsed:
            dates.add(parsed)
    return sorted(dates)


def _date_from_parts(year_token: str, month_token: str, day_token: str, original: str) -> str:
    try:
        return date(int(year_token), int(month_token), int(day_token)).isoformat()
    except ValueError:
        logger.warning("Goodyear date token rejected as invalid: token=%r", original)
        return ""


def _extract_title(row: Tag, row_text: str, evidence: str) -> str:
    for selector in ("h1", "h2", "h3", "h4", "strong", "a"):
        node = row.select_one(selector)
        if node is None:
            continue
        text = _clean_text(node.get_text(" ", strip=True))
        if text and _looks_like_title(text):
            logger.info("Goodyear meeting_title emitted from selector=%s title=%r evidence=%s", selector, text, evidence)
            return text

    for part in re.split(r"\s{2,}|\s+-\s+|\s+\|\s+", row_text):
        text = _clean_text(part)
        if text and _looks_like_title(text):
            logger.info("Goodyear meeting_title emitted from row segment title=%r evidence=%s", text, evidence)
            return text

    logger.warning("Goodyear meeting_title empty: no row-level title evidence=%s", evidence)
    return ""


def _looks_like_title(text: str) -> bool:
    lowered = text.lower()
    return "meeting" in lowered or "council" in lowered or "agenda" in lowered


def _extract_meeting_time(row_text: str, evidence: str) -> str:
    match = _TIME_RE.search(row_text[:1_000])
    if not match:
        logger.warning("Goodyear meeting_time empty: no row-level time evidence=%s", evidence)
        return ""
    hour = str(int(match.group(1)))
    minute = match.group(2) or "00"
    suffix = match.group(3).upper() + "M"
    value = f"{hour}:{minute} {suffix}"
    logger.info("Goodyear meeting_time emitted: time=%s evidence=%s", value, evidence)
    return value


def _extract_meeting_location(row_text: str, evidence: str) -> str:
    cleaned = _clean_text(row_text)[:1_000]
    for pattern in (
        r"(?:location|where|venue)\s*:\s*([^|;\n]+)",
        r"(?:at)\s+([^|;\n]+(?:chambers|room|hall|center|centre|building))",
    ):
        match = re.search(pattern, cleaned, re.IGNORECASE)
        if not match:
            continue
        location = _clean_text(match.group(1)).strip(" .,-")
        if location:
            logger.info("Goodyear meeting_location emitted: location=%r evidence=%s", location, evidence)
            return location
    logger.warning("Goodyear meeting_location empty: no row-level location evidence=%s", evidence)
    return ""


def _extract_row_urls(row: Tag, base_url: str, row_evidence: str, stats: _Stats) -> dict[str, str]:
    urls = {
        "agenda_url": "",
        "minutes_url": "",
        "video_url": "",
        "agenda_packet_url": "",
        "ecomment_url": "",
    }
    priorities = {field: -1 for field in urls}
    anchors = row.find_all("a")
    stats.anchors_seen += len(anchors)
    if not anchors:
        logger.info("Goodyear row has no anchor URL evidence: %s", row_evidence)

    for anchor_index, anchor in enumerate(anchors, start=1):
        anchor_text = _clean_text(anchor.get_text(" ", strip=True))
        raw_href = anchor.get("href", "")
        anchor_evidence = (
            f"{row_evidence} anchor_index={anchor_index} text={anchor_text!r} href={raw_href!r} "
            f"onclick={anchor.get('onclick', '')!r}"
        )
        field, priority = _classify_anchor(anchor_text, raw_href, anchor_evidence)
        if not field:
            logger.warning("Goodyear anchor dropped: reason=unclassified_url_field evidence=%s", anchor_evidence)
            continue

        emitted = ""
        candidates = _url_candidates(anchor)
        if not candidates:
            emit_url("", base_url, field, anchor_evidence, stats)
        for candidate in candidates:
            emitted = emit_url(candidate, base_url, field, anchor_evidence, stats)
            if emitted:
                break
        if not emitted:
            logger.warning("Goodyear anchor dropped after URL hygiene rejection: field=%s evidence=%s", field, anchor_evidence)
            continue

        if urls[field] and priorities[field] > priority:
            logger.warning(
                "Goodyear duplicate URL skipped by lower priority: field=%s kept=%s dropped=%s evidence=%s",
                field,
                urls[field],
                emitted,
                anchor_evidence,
            )
            continue
        if urls[field]:
            logger.warning(
                "Goodyear duplicate URL replaced by equal/higher priority: field=%s old=%s new=%s evidence=%s",
                field,
                urls[field],
                emitted,
                anchor_evidence,
            )
        urls[field] = emitted
        priorities[field] = priority
        logger.info("Goodyear URL field emitted: field=%s url=%s evidence=%s", field, emitted, anchor_evidence)

    return urls


def _classify_anchor(text: str, href: str, evidence: str) -> tuple[str, int]:
    combined = _word_text(f"{text} {href}")
    parsed = urlparse(href if re.match(r"^https?://", href, re.IGNORECASE) else urljoin(_DEFAULT_URL, href or ""))
    query = {key.lower(): values for key, values in parse_qs(parsed.query).items()}

    if "minutes" in query or _word_present(combined, "minutes"):
        logger.info("Goodyear anchor classified as minutes_url: evidence=%s", evidence)
        return "minutes_url", 50
    if _word_present(combined, "packet") or _word_present(combined, "agenda packet"):
        logger.info("Goodyear anchor classified as agenda_packet_url: evidence=%s", evidence)
        return "agenda_packet_url", 45
    if _word_present(combined, "agenda") or query.get("doctype") == ["agenda"]:
        logger.info("Goodyear anchor classified as agenda_url: evidence=%s", evidence)
        return "agenda_url", 40
    if _word_present(combined, "video") or _word_present(combined, "media") or "open media" in combined:
        logger.info("Goodyear anchor classified as video_url: evidence=%s", evidence)
        return "video_url", 35
    if _word_present(combined, "comment") or _word_present(combined, "ecomment"):
        logger.info("Goodyear anchor classified as ecomment_url: evidence=%s", evidence)
        return "ecomment_url", 30
    return "", 0


def _url_candidates(anchor: Tag) -> list[str]:
    candidates: list[str] = []
    for attr in ("href", "data-url", "data-href", "data-link"):
        value = anchor.get(attr, "")
        if isinstance(value, str) and value and value not in candidates:
            candidates.append(value)

    onclick = anchor.get("onclick", "")
    if isinstance(onclick, str) and onclick:
        for match in re.finditer(r"""['"]((?:https?:)?//[^'"]+|https?://[^'"]+|/[^'"]+)['"]""", onclick):
            value = match.group(1)
            if value not in candidates:
                candidates.append(value)
    return candidates


def emit_url(href: str, base_url: str, field: str, evidence: str, stats: _Stats) -> str:
    if not href:
        _warn_url_rejection(field, href, "empty_href", evidence, stats)
        return ""

    stripped = href.strip()
    lowered = stripped.lower()
    if lowered.startswith("//"):
        _warn_url_rejection(field, href, "scheme_relative_not_allowed", evidence, stats)
        return ""
    if lowered.startswith(_BAD_SCHEMES):
        _warn_url_rejection(field, href, "bad_scheme", evidence, stats)
        return ""

    absolute = urljoin(base_url, stripped)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        _warn_url_rejection(field, href, f"scheme_not_http_or_https:{parsed.scheme}", evidence, stats)
        return ""

    host = (parsed.hostname or "").lower()
    if _host_flagged(host):
        _warn_url_rejection(field, href, f"flagged_host:{host}", evidence, stats)
        return ""
    if not _emit_host_allowed(host):
        _warn_url_rejection(field, href, f"host_not_allowed:{host}", evidence, stats)
        return ""

    logger.info("Goodyear URL accepted: field=%s url=%s evidence=%s", field, absolute, evidence)
    return absolute


def _warn_url_rejection(field: str, href: str, reason: str, evidence: str, stats: _Stats) -> None:
    stats.url_rejections[reason] += 1
    logger.warning(
        "Goodyear URL rejected: field=%s reason=%s rejected_url=%r context=%s",
        field,
        reason,
        href,
        evidence,
    )


def _extract_meeting_id(row: Tag, evidence: str) -> str:
    row_id = row.get("data-id") or row.get("id") or ""
    if isinstance(row_id, str) and row_id:
        cleaned = _clean_text(row_id)
        logger.info("Goodyear meeting_id emitted from row attribute: meeting_id=%s evidence=%s", cleaned, evidence)
        return cleaned

    for anchor in row.find_all("a"):
        href = anchor.get("href", "")
        if not isinstance(href, str) or not href:
            continue
        query = parse_qs(urlparse(href).query)
        for key in ("ID", "id", "meetingId", "meetingid", "MeetingID"):
            values = query.get(key)
            if values and values[0]:
                meeting_id = _clean_text(values[0])
                logger.info("Goodyear meeting_id emitted from href query: meeting_id=%s evidence=%s", meeting_id, evidence)
                return meeting_id
    logger.warning("Goodyear meeting_id empty: no row-level native ID evidence=%s", evidence)
    return ""


def _derive_status(title: str, agenda_url: str, minutes_url: str, agenda_packet_url: str) -> str:
    if _CANCELLED_RE.search(title):
        logger.info("Goodyear meeting_status emitted as Cancelled from title evidence=%r", title)
        return "Cancelled"
    if minutes_url:
        logger.info("Goodyear meeting_status emitted as Minutes Available from same-row minutes_url=%s", minutes_url)
        return "Minutes Available"
    if agenda_url or agenda_packet_url:
        logger.info(
            "Goodyear meeting_status emitted as Agenda Available from same-row agenda evidence agenda_url=%s agenda_packet_url=%s",
            agenda_url,
            agenda_packet_url,
        )
        return "Agenda Available"
    logger.info("Goodyear meeting_status emitted as Scheduled from no agenda/minutes/cancelled same-row evidence")
    return "Scheduled"


def _build_meeting(
    *,
    meeting_title: str,
    meeting_date: str,
    meeting_time: str,
    meeting_location: str,
    meeting_status: str,
    agenda_url: str,
    minutes_url: str,
    video_url: str,
    agenda_packet_url: str,
    ecomment_url: str,
    meeting_id: str,
) -> dict:
    meeting = {
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
    if tuple(meeting.keys()) != FIELD_NAMES:
        raise ValueError(f"Goodyear schema key order drifted: {tuple(meeting.keys())}")
    if any(value is None or not isinstance(value, str) for value in meeting.values()):
        raise ValueError(f"Goodyear schema value type drifted: {meeting}")
    return meeting


def _source_host_allowed(host: str) -> bool:
    normalized = host.lower().split(":")[0]
    return normalized in _SOURCE_HOSTS and not _host_flagged(normalized)


def _emit_host_allowed(host: str) -> bool:
    normalized = host.lower().split(":")[0]
    return normalized in ALLOWED_HOSTS and not _host_flagged(normalized)


def _host_flagged(host: str) -> bool:
    normalized = host.lower().split(":")[0]
    return normalized in FLAGGED_HOSTS


def _extract_html_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return _clean_text(soup.title.get_text(" ", strip=True) if soup.title else "")


def _clean_text(value: str) -> str:
    if not value:
        return ""
    return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)


def _word_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _clean_text(unquote(value)).lower()).strip()


def _word_present(value: str, word: str) -> bool:
    return f" {word} " in f" {value} "


def _log_summary(stats: _Stats) -> None:
    logger.info(
        "Goodyear scrape summary: rows_seen=%d rows_accepted=%d rows_dropped=%d anchors_seen=%d "
        "drop_reasons=%s url_rejections=%s",
        stats.rows_seen,
        stats.rows_accepted,
        stats.rows_dropped,
        stats.anchors_seen,
        dict(stats.drop_reasons),
        dict(stats.url_rejections),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)
    results = scrape_calendar(_DEFAULT_URL)
    print(json.dumps(results, indent=2))
    print(f"Found {len(results)} meetings", file=sys.stderr)
    if results:
        print(json.dumps(results[:2], indent=2), file=sys.stderr)
