"""Superior — Joomla meeting parser."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
import logging
import re
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

__all__ = ["scrape_calendar"]

logger = logging.getLogger(__name__)

_DEFAULT_URL = "https://superioraz.gov/index.php/government/town-council/town-council-meeting-agendas2/"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_MAX_RESPONSE_BYTES = 5_000_000
_CHUNK_SIZE = 65_536
_SOURCE_HOSTS = {"superioraz.gov", "www.superioraz.gov"}
_EMIT_HOSTS = {
    "superioraz.gov",
    "www.superioraz.gov",
    "townofsuperioraz.com",
    "www.townofsuperioraz.com",
    "succeedinsuperioraz.com",
    "www.succeedinsuperioraz.com",
}
_FLAGGED_HOST_SUFFIXES = ("townofsuperior.com",)
_BAD_SCHEMES = ("javascript:", "data:", "vbscript:", "file:", "mailto:", "ftp:", "gopher:")
_DOCUMENT_EXTENSIONS = (".pdf", ".doc", ".docx")
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
_SCHEMA_KEYS = (
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


@dataclass
class _FetchResult:
    text: str
    status_code: int
    final_url: str
    headers: dict[str, str]


@dataclass
class _Stats:
    links_seen: int = 0
    document_candidates_seen: int = 0
    document_links_accepted: int = 0
    rows_accepted: int = 0
    rows_dropped: int = 0
    drop_reasons: Counter[str] = field(default_factory=Counter)
    url_rejections: Counter[str] = field(default_factory=Counter)
    groups_seen: int = 0

    def drop(self, reason: str) -> None:
        self.rows_dropped += 1
        self.drop_reasons[reason] += 1


@dataclass(frozen=True)
class _Document:
    meeting_date: str
    field_name: str
    url: str
    title: str
    cancelled: bool
    priority: int
    evidence: str


@dataclass
class _MeetingGroup:
    meeting_date: str
    title: str = "Town Council Meeting"
    cancelled: bool = False
    agenda_url: str = ""
    minutes_url: str = ""
    agenda_packet_url: str = ""
    field_priorities: dict[str, int] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)


def scrape_calendar(calendar_url: str) -> list[dict]:
    """Scrape Superior's Town Council agenda archive into canonical meeting dicts."""
    stats = _Stats()
    session = requests.Session()
    session.headers.update({"User-Agent": _USER_AGENT})

    logger.info(
        "Superior scrape started: url=%s allowed_hosts=%s flagged_hosts=%s tls_verify=default_true",
        calendar_url,
        sorted(_EMIT_HOSTS),
        list(_FLAGGED_HOST_SUFFIXES),
    )
    logger.info("Superior source field policy: meeting_time and meeting_location are not exposed by this archive")

    try:
        fetch = _fetch_text_bounded(session, calendar_url)
    except requests.RequestException as exc:
        logger.warning(
            "Superior fetch failed; architectural blocker for all meetings; returning []: %s",
            exc,
        )
        _log_summary(stats)
        return []

    cloudflare = _is_cloudflare_challenge(fetch)
    logger.info(
        "Superior fetch observed: status=%d final_url=%s bytes=%d cloudflare_challenge=%s",
        fetch.status_code,
        fetch.final_url,
        len(fetch.text.encode("utf-8", errors="replace")),
        cloudflare,
    )
    if cloudflare:
        logger.warning(
            "Superior HTTP status=%d Cloudflare challenge detected; missing-data scope=all meetings; returning []",
            fetch.status_code,
        )
        _log_summary(stats)
        return []
    if fetch.status_code != 200:
        logger.warning(
            "Superior non-200 HTTP status=%d; missing-data scope=all meetings; returning []",
            fetch.status_code,
        )
        _log_summary(stats)
        return []

    soup = BeautifulSoup(fetch.text, "html.parser")
    if _is_unexpected_html_blocker(soup, fetch.text):
        logger.warning("Superior unexpected HTML blocker detected; missing-data scope=all meetings; returning []")
        _log_summary(stats)
        return []

    _log_fingerprint_witness(soup, fetch.text)
    documents = _extract_documents(soup, fetch.final_url, stats)
    if not documents:
        logger.warning("Superior fetch produced 0 accepted document rows from accessible page")

    meetings = _group_documents(documents, stats)
    _log_summary(stats)
    return meetings


def _fetch_text_bounded(session: requests.Session, url: str) -> _FetchResult:
    start_host = (urlparse(url).hostname or "").lower()
    if not _source_host_allowed(start_host):
        raise ValueError(f"Superior parser called with disallowed host: {start_host}")

    with session.get(url, timeout=30, stream=True, allow_redirects=True) as response:
        final_host = (urlparse(response.url).hostname or "").lower()
        if not _source_host_allowed(final_host):
            raise ValueError(f"Redirect to disallowed host: {final_host} (started from {url})")

        body = bytearray()
        for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
            body.extend(chunk)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise ValueError(f"Response from {url} exceeded {_MAX_RESPONSE_BYTES} bytes")

        encoding = response.encoding or "utf-8"
        headers = {key.lower(): value for key, value in response.headers.items()}
        return _FetchResult(
            text=bytes(body).decode(encoding, errors="replace"),
            status_code=response.status_code,
            final_url=response.url,
            headers=headers,
        )


def _is_cloudflare_challenge(fetch: _FetchResult) -> bool:
    lowered = fetch.text[:20_000].lower()
    header_signal = fetch.headers.get("cf-mitigated", "").lower() == "challenge"
    body_signal = (
        "challenges.cloudflare.com" in lowered
        or "__cf_chl" in lowered
        or "<title>just a moment" in lowered
        or "enable javascript and cookies to continue" in lowered
    )
    return header_signal or body_signal


def _is_unexpected_html_blocker(soup: BeautifulSoup, html: str) -> bool:
    title = _clean_text(soup.title.get_text(" ", strip=True) if soup.title else "")
    lowered = html[:20_000].lower()
    if title.lower() in {"just a moment...", "access denied"}:
        logger.warning("Superior blocker title witnessed: title=%r", title)
        return True
    if "cf-browser-verification" in lowered or "cf-challenge" in lowered:
        logger.warning("Superior Cloudflare-like blocker token witnessed in HTML body")
        return True
    return False


def _log_fingerprint_witness(soup: BeautifulSoup, html: str) -> None:
    generator = soup.find("meta", attrs={"name": re.compile(r"^generator$", re.IGNORECASE)})
    generator_content = generator.get("content", "") if generator else ""
    html_lower = html.lower()
    indicators = {
        "meta_generator": generator_content,
        "html_contains_joomla": "joomla" in html_lower,
        "com_content_url": "/components/com_content/" in html_lower or "option=com_content" in html_lower,
        "municipal_archive_tokens": "town council" in html_lower and ".pdf" in html_lower,
    }
    if (
        "joomla" in generator_content.lower()
        or indicators["html_contains_joomla"]
        or indicators["com_content_url"]
        or indicators["municipal_archive_tokens"]
    ):
        logger.info("Superior Joomla/custom archive fingerprint witnessed: %s", indicators)
    else:
        logger.warning(
            "Superior fingerprint warning: expected Joomla or municipal archive indicators; observed=%s",
            indicators,
        )


def _extract_documents(soup: BeautifulSoup, base_url: str, stats: _Stats) -> list[_Document]:
    documents: list[_Document] = []
    anchors = soup.find_all("a", href=True)
    stats.links_seen = len(anchors)
    logger.info("Superior page structure witnessed: total_anchor_links=%d", stats.links_seen)

    if not anchors:
        logger.warning("Superior page structure has zero anchor links; expected Joomla article archive links")

    for link_index, anchor in enumerate(anchors, start=1):
        raw_href = anchor.get("href", "")
        link_text = _clean_text(anchor.get_text(" ", strip=True))
        context_text = _nearby_context_text(anchor)
        evidence = f"link_index={link_index} text={link_text!r} href={raw_href!r} context={context_text!r}"

        if not _looks_like_document_link(raw_href, link_text):
            logger.info("Superior link dropped as non-document: %s", evidence)
            continue

        stats.document_candidates_seen += 1
        document_url = _emit_url(raw_href, base_url, "document_url", evidence, stats)
        if document_url == "":
            stats.drop("url_rejected")
            logger.warning("Superior candidate document row dropped after URL rejection: %s", evidence)
            continue

        meeting_date = _choose_date(link_text, raw_href, context_text, evidence, stats)
        if meeting_date == "":
            stats.drop("date_unparseable_or_ambiguous")
            continue

        field_name, priority = _classify_document(link_text, raw_href, context_text, evidence, stats)
        if field_name == "":
            stats.drop("document_type_unclassified")
            continue

        title, cancelled = _build_title(link_text, raw_href, context_text)
        document = _Document(
            meeting_date=meeting_date,
            field_name=field_name,
            url=document_url,
            title=title,
            cancelled=cancelled,
            priority=priority,
            evidence=evidence,
        )
        stats.document_links_accepted += 1
        documents.append(document)
        logger.info(
            "Superior document accepted: date=%s field=%s url=%s title=%r cancelled=%s evidence=%s",
            document.meeting_date,
            document.field_name,
            document.url,
            document.title,
            document.cancelled,
            document.evidence,
        )

    logger.info(
        "Superior document extraction counters: links_seen=%d document_candidates=%d accepted_documents=%d dropped_rows=%d",
        stats.links_seen,
        stats.document_candidates_seen,
        stats.document_links_accepted,
        stats.rows_dropped,
    )
    return documents


def _looks_like_document_link(href: str, text: str) -> bool:
    evidence = unquote(f"{href} {text}").lower()
    return any(extension in evidence for extension in _DOCUMENT_EXTENSIONS)


def _emit_url(href: str, base_url: str, field: str, context: str, stats: _Stats) -> str:
    if not href:
        _warn_url_rejection(field, href, "empty_href", context, stats)
        return ""

    stripped = href.strip()
    lowered = stripped.lower()
    if lowered.startswith("//"):
        _warn_url_rejection(field, href, "scheme_relative_not_allowed", context, stats)
        return ""
    if lowered.startswith(_BAD_SCHEMES):
        _warn_url_rejection(field, href, "bad_scheme", context, stats)
        return ""

    absolute = urljoin(base_url, stripped)
    parsed = urlparse(absolute)
    if parsed.scheme != "https":
        _warn_url_rejection(field, href, f"scheme_not_https:{parsed.scheme}", context, stats)
        return ""

    host = (parsed.hostname or "").lower()
    if _host_flagged(host):
        _warn_url_rejection(field, href, f"flagged_host:{host}", context, stats)
        return ""
    if not _emit_host_allowed(host):
        _warn_url_rejection(field, href, f"host_not_allowed:{host}", context, stats)
        return ""

    logger.info("Superior URL accepted: field=%s url=%s context=%s", field, absolute, context)
    return absolute


def _warn_url_rejection(field: str, href: str, reason: str, context: str, stats: _Stats) -> None:
    stats.url_rejections[reason] += 1
    logger.warning(
        "Superior URL rejected: field=%s reason=%s rejected_url=%r context=%s",
        field,
        reason,
        href,
        context,
    )


def _source_host_allowed(host: str) -> bool:
    normalized = host.lower().split(":")[0]
    return normalized in _SOURCE_HOSTS and not _host_flagged(normalized)


def _emit_host_allowed(host: str) -> bool:
    normalized = host.lower().split(":")[0]
    return normalized in _EMIT_HOSTS and not _host_flagged(normalized)


def _host_flagged(host: str) -> bool:
    normalized = host.lower().split(":")[0]
    return any(normalized == suffix or normalized.endswith("." + suffix) for suffix in _FLAGGED_HOST_SUFFIXES)


def _nearby_context_text(anchor: BeautifulSoup) -> str:
    for name in ("li", "p", "tr"):
        parent = anchor.find_parent(name)
        if parent is not None:
            text = _clean_text(parent.get_text(" ", strip=True))
            if len(text) <= 300:
                return text
    return ""


def _choose_date(text: str, href: str, context: str, evidence: str, stats: _Stats) -> str:
    sources = {
        "text": _extract_dates(text, "text", evidence),
        "href": _extract_dates(unquote(href), "href", evidence),
        "context": _extract_dates(context, "context", evidence) if context and context != text else [],
    }
    populated = {source: dates for source, dates in sources.items() if dates}
    if not populated:
        logger.warning("Superior date extraction failed: no parseable date found; evidence=%s", evidence)
        return ""

    source_sets = {source: set(dates) for source, dates in populated.items()}
    if len(source_sets) == 1:
        source, dates = next(iter(source_sets.items()))
        if len(dates) == 1:
            chosen = next(iter(dates))
            logger.info("Superior date accepted from single source: source=%s date=%s evidence=%s", source, chosen, evidence)
            return chosen
        logger.warning("Superior date extraction ambiguous within %s: dates=%s evidence=%s", source, sorted(dates), evidence)
        return ""

    agreed_dates = set.intersection(*source_sets.values())
    if len(agreed_dates) == 1:
        chosen = next(iter(agreed_dates))
        logger.info(
            "Superior date accepted by cross-source agreement: date=%s sources=%s evidence=%s",
            chosen,
            {source: sorted(dates) for source, dates in source_sets.items()},
            evidence,
        )
        return chosen
    if agreed_dates:
        logger.warning("Superior date extraction had multiple agreed dates=%s evidence=%s", sorted(agreed_dates), evidence)
        return ""

    logger.warning(
        "Superior date source disagreement; row dropped: sources=%s evidence=%s",
        {source: sorted(dates) for source, dates in source_sets.items()},
        evidence,
    )
    return ""


def _extract_dates(value: str, source: str, evidence: str) -> list[str]:
    cleaned = _clean_text(unquote(value))[:500]
    dates: list[str] = []

    for match in _ISO_DATE_RE.finditer(cleaned):
        year_token, month_token, day_token = match.groups()
        parsed = _date_from_parts(year_token, month_token, day_token, match.group(0), source, evidence)
        if parsed:
            dates.append(parsed)

    for match in _US_DATE_RE.finditer(cleaned):
        month_token, day_token, year_token = match.groups()
        parsed = _date_from_parts(year_token, month_token, day_token, match.group(0), source, evidence)
        if parsed:
            dates.append(parsed)

    for match in _MONTH_DATE_RE.finditer(cleaned):
        month_name, day_token, year_token = match.groups()
        month = str(_MONTHS[month_name[:3].lower()])
        parsed = _date_from_parts(year_token, month, day_token, match.group(0), source, evidence)
        if parsed:
            dates.append(parsed)

    return sorted(set(dates))


def _date_from_parts(year_token: str, month_token: str, day_token: str, original: str, source: str, evidence: str) -> str:
    try:
        parsed = date(int(year_token), int(month_token), int(day_token))
    except ValueError:
        logger.warning(
            "Superior date token rejected as invalid: source=%s token=%r evidence=%s",
            source,
            original,
            evidence,
        )
        return ""
    return parsed.isoformat()


def _classify_document(text: str, href: str, context: str, evidence: str, stats: _Stats) -> tuple[str, int]:
    combined = _word_text(f"{text} {href} {context}")
    has_minutes = _word_present(combined, "minutes")
    has_minute = _word_present(combined, "minute")
    has_packet = _word_present(combined, "packet")
    has_agenda = _word_present(combined, "agenda")
    has_amended = _word_present(combined, "amended") or _word_present(combined, "revised")

    if has_minutes or has_minute:
        logger.info("Superior document classified as minutes_url by row vocabulary; evidence=%s", evidence)
        return "minutes_url", 40 + int(has_amended)
    if has_packet:
        logger.info("Superior document classified as agenda_packet_url by row vocabulary; evidence=%s", evidence)
        return "agenda_packet_url", 35 + int(has_amended)
    if has_agenda:
        logger.info("Superior document classified as agenda_url by row vocabulary; evidence=%s", evidence)
        return "agenda_url", 30 + int(has_amended)

    logger.warning(
        "Superior document vocabulary not mapped to canonical URL fields; row dropped: vocabulary=%r evidence=%s",
        combined[:250],
        evidence,
    )
    return "", 0


def _build_title(text: str, href: str, context: str) -> tuple[str, bool]:
    combined = _word_text(f"{text} {href} {context}")
    cancelled = bool(_CANCELLED_RE.search(combined[:500]))
    if "work session" in combined:
        title = "Town Council Work Session"
    elif "special" in combined:
        title = "Special Town Council Meeting"
    else:
        title = "Town Council Meeting"
    if cancelled:
        title = f"Cancelled {title}"
    return _clean_text(title), cancelled


def _group_documents(documents: list[_Document], stats: _Stats) -> list[dict]:
    groups: dict[str, _MeetingGroup] = {}

    for document in documents:
        group = groups.setdefault(document.meeting_date, _MeetingGroup(meeting_date=document.meeting_date))
        group.evidence.append(document.evidence)
        if document.cancelled:
            group.cancelled = True
            group.title = document.title
        elif group.title == "Town Council Meeting" and document.title != "Town Council Meeting":
            group.title = document.title

        existing = getattr(group, document.field_name)
        existing_priority = group.field_priorities.get(document.field_name, -1)
        if existing and existing_priority > document.priority:
            logger.warning(
                "Superior duplicate field skipped by lower priority: date=%s field=%s kept_url=%s dropped_url=%s evidence=%s",
                document.meeting_date,
                document.field_name,
                existing,
                document.url,
                document.evidence,
            )
            continue
        if existing and existing_priority <= document.priority:
            logger.warning(
                "Superior duplicate field replaced by equal/higher priority: date=%s field=%s old_url=%s new_url=%s evidence=%s",
                document.meeting_date,
                document.field_name,
                existing,
                document.url,
                document.evidence,
            )

        setattr(group, document.field_name, document.url)
        group.field_priorities[document.field_name] = document.priority

    meetings: list[dict] = []
    for meeting_date in sorted(groups.keys(), reverse=True):
        group = groups[meeting_date]
        stats.groups_seen += 1
        meeting = _build_meeting(group)
        stats.rows_accepted += 1
        logger.info(
            "Superior meeting emitted: date=%s fields=%s evidence=%s",
            meeting_date,
            meeting,
            group.evidence,
        )
        meetings.append(meeting)

    return meetings


def _build_meeting(group: _MeetingGroup) -> dict:
    status = _derive_status(group.title, group.agenda_url, group.minutes_url, group.agenda_packet_url)
    meeting = {
        "meeting_title": _clean_text(group.title),
        "meeting_date": group.meeting_date,
        "meeting_time": "",
        "meeting_location": "",
        "meeting_status": status,
        "agenda_url": group.agenda_url,
        "minutes_url": group.minutes_url,
        "video_url": "",
        "agenda_packet_url": group.agenda_packet_url,
        "ecomment_url": "",
        "meeting_id": "",
    }
    if tuple(meeting.keys()) != _SCHEMA_KEYS:
        raise ValueError(f"Superior schema key order drifted: {tuple(meeting.keys())}")
    if any(value is None or not isinstance(value, str) for value in meeting.values()):
        raise ValueError(f"Superior schema value type drifted for date={group.meeting_date}: {meeting}")
    return meeting


def _derive_status(title: str, agenda_url: str, minutes_url: str, agenda_packet_url: str) -> str:
    if _CANCELLED_RE.search(title):
        logger.info("Superior status derived as Cancelled from meeting_title=%r", title)
        return "Cancelled"
    if minutes_url:
        logger.info("Superior status derived as Minutes Available from same-row minutes_url=%s", minutes_url)
        return "Minutes Available"
    if agenda_url or agenda_packet_url:
        logger.info(
            "Superior status derived as Agenda Available from same-row agenda evidence: agenda_url=%s packet_url=%s",
            agenda_url,
            agenda_packet_url,
        )
        return "Agenda Available"
    logger.info("Superior status derived as Scheduled from absence of agenda/minutes/cancelled evidence")
    return "Scheduled"


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
        "Superior scrape summary: links_seen=%d document_candidates_seen=%d accepted_documents=%d "
        "rows_accepted=%d rows_dropped=%d groups_seen=%d drop_reasons=%s url_rejections=%s",
        stats.links_seen,
        stats.document_candidates_seen,
        stats.document_links_accepted,
        stats.rows_accepted,
        stats.rows_dropped,
        stats.groups_seen,
        dict(stats.drop_reasons),
        dict(stats.url_rejections),
    )


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)
    results = scrape_calendar(_DEFAULT_URL)
    print(json.dumps(results, indent=2))
    print(f"Found {len(results)} meetings", file=sys.stderr)
