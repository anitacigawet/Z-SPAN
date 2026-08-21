"""Bounded current-month-forward parser for Casa Grande's CivicPlus calendar."""

from __future__ import annotations

from datetime import date, datetime
import logging
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, Tag

from polite_http import make_session

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://www.casagrandeaz.gov/Calendar.aspx?CID=27"
LIST_HOSTS = {"casagrandeaz.gov", "www.casagrandeaz.gov"}
DOCUMENT_HOSTS = LIST_HOSTS | {"public.destinyhosted.com"}
VIDEO_HOSTS = LIST_HOSTS | {
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
    "www.youtu.be",
}
MAX_LIST_BYTES = 2_000_000
MAX_DETAIL_BYTES = 1_000_000
MAX_CURRENT_EVENTS = 50
_CANCEL_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _build_list_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    if parsed.scheme != "https" or _host(raw_url) not in LIST_HOSTS:
        raise ValueError("Casa Grande calendar URL must use HTTPS on the official city host")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if query.get("CID") != ["27"]:
        raise ValueError("Casa Grande calendar URL must identify the City Council calendar (CID=27)")
    query["showPastEvents"] = ["true"]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True), fragment=""))


def _fetch_text_bounded(session, url: str, allowed_hosts: set[str], max_bytes: int) -> str:
    try:
        response_context = session.get(url, timeout=30, stream=True, allow_redirects=True)
    except requests.exceptions.SSLError:
        logger.warning("health_empty_kind=source_blocked")
        logger.warning("Casa Grande official source failed verified TLS: url=%s", url)
        raise
    with response_context as response:
        if getattr(response, "status_code", None) in {401, 403, 429}:
            logger.warning("health_empty_kind=source_blocked")
        response.raise_for_status()
        if urlparse(response.url).scheme != "https" or _host(response.url) not in allowed_hosts:
            raise RuntimeError(f"Casa Grande request redirected to a disallowed host: {response.url}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > max_bytes:
                raise RuntimeError(f"Casa Grande response exceeded the {max_bytes}-byte safety cap")
        logger.info("Casa Grande fetched %s bytes from %s", len(body), response.url)
        return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _clean_text(node: Tag | None) -> str:
    if node is None:
        return ""
    return BeautifulSoup(node.decode_contents(), "html.parser").get_text(" ", strip=True)


def _parse_event_id(href: str) -> str:
    values = parse_qs(urlparse(href).query).get("EID", [])
    if len(values) != 1 or not values[0].isdigit():
        raise RuntimeError(f"Casa Grande event detail link has no usable EID: {href!r}")
    return values[0]


def _parse_start(raw: str, event_id: str) -> datetime:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(
            f"Casa Grande event {event_id} has an unparseable schema.org startDate: {raw!r}"
        ) from exc


def _parse_calendar_list(
    html: str,
    base_url: str,
    cutoff: date,
    *,
    max_current_events: int = MAX_CURRENT_EVENTS,
) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    event_nodes = soup.select('div[itemtype="http://schema.org/Event"]')
    if not event_nodes:
        page_text = soup.get_text(" ", strip=True).lower()
        if "no events" in page_text or "no calendar items" in page_text:
            logger.warning("Casa Grande calendar proved an explicit vendor empty state")
            return []
        raise RuntimeError("Casa Grande calendar lost its schema.org Event fingerprint")

    candidates: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    historical = 0
    for node in event_nodes:
        row = node.find_parent("li")
        title_node = node.find(attrs={"itemprop": "name"}, recursive=False)
        start_node = node.find(attrs={"itemprop": "startDate"}, recursive=False)
        detail_link = row.find("a", id=re.compile(r"^eventTitle_"), href=True) if row else None
        if row is None or title_node is None or start_node is None or detail_link is None:
            raise RuntimeError("Casa Grande schema.org Event row is missing title, date, or detail-link evidence")

        detail_url = urljoin(base_url, detail_link["href"])
        if urlparse(detail_url).scheme != "https" or _host(detail_url) not in LIST_HOSTS:
            raise RuntimeError(f"Casa Grande event detail URL left the official city host: {detail_url}")
        event_id = _parse_event_id(detail_url)
        if event_id in seen_ids:
            raise RuntimeError(f"Casa Grande calendar emitted duplicate event ID {event_id}")
        seen_ids.add(event_id)

        starts_at = _parse_start(start_node.get_text(strip=True), event_id)
        if starts_at.date() < cutoff:
            historical += 1
            continue

        location_scope = node.find(attrs={"itemprop": "location"})
        location_node = (
            location_scope.find(attrs={"itemprop": "name"}) if location_scope is not None else None
        )
        title = _clean_text(title_node)
        if not title:
            raise RuntimeError(f"Casa Grande event {event_id} has an empty title")
        candidates.append(
            {
                "meeting_title": title,
                "meeting_date": starts_at.date().isoformat(),
                "meeting_time": starts_at.strftime("%I:%M %p").lstrip("0"),
                "meeting_location": _clean_text(location_node),
                "meeting_id": event_id,
                "detail_url": detail_url,
            }
        )

    if len(candidates) > max_current_events:
        raise RuntimeError(
            "Casa Grande current-month detail fan-out exceeded the "
            f"{max_current_events}-request safety cap ({len(candidates)} candidates)"
        )
    logger.info(
        "Casa Grande list audit: rows_seen=%s historical_dropped=%s current_candidates=%s",
        len(event_nodes),
        historical,
        len(candidates),
    )
    return candidates


def _emit_url(raw_url: str, base_url: str, allowed_hosts: set[str], field: str) -> str:
    absolute = urljoin(base_url, raw_url.strip())
    if urlparse(absolute).scheme != "https" or _host(absolute) not in allowed_hosts:
        logger.warning("Casa Grande dropped %s URL %r: HTTPS host is not allowlisted", field, raw_url)
        return ""
    return absolute


def _document_field(anchor: Tag) -> str:
    label = anchor.get_text(" ", strip=True).lower()
    parsed = urlparse(anchor.get("href", ""))
    query = parse_qs(parsed.query)
    dsp = (query.get("dsp") or [""])[0].lower()
    if "minute" in label:
        return "minutes_url"
    if "packet" in label or dsp in {"ap", "pa", "pak", "packet"}:
        return "agenda_packet_url"
    if "video" in label or "recording" in label:
        return "video_url"
    if "agenda" in label or (parsed.path.lower().endswith("agenda_publish.cfm") and dsp == "ag"):
        return "agenda_url"
    if _host(anchor.get("href", "")) in VIDEO_HOSTS and (
        parsed.path == "/watch" or parsed.path.startswith("/embed/") or _host(anchor["href"]) in {"youtu.be", "www.youtu.be"}
    ):
        return "video_url"
    return ""


def _parse_detail_documents(html: str, detail_url: str, event_id: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    event_details = soup.select_one("div.eventDetails")
    if event_details is None:
        raise RuntimeError(f"Casa Grande event {event_id} lost its eventDetails fingerprint")
    panel = event_details.find_parent("div", id=re.compile(r"contentUpdatePanel$")) or event_details
    anchors: list[Tag] = []
    anchors.extend(panel.select("a.agendaDownload[href]"))
    anchors.extend(panel.select('div[itemprop="description"] a[href]'))
    anchors.extend(panel.select("ul.documentsList a[href]"))

    documents = {
        "agenda_url": "",
        "minutes_url": "",
        "video_url": "",
        "agenda_packet_url": "",
    }
    seen_anchor_urls: set[str] = set()
    for anchor in anchors:
        raw_url = anchor.get("href", "")
        if not raw_url or raw_url in seen_anchor_urls:
            continue
        seen_anchor_urls.add(raw_url)
        field = _document_field(anchor)
        if not field:
            logger.warning(
                "Casa Grande event %s exposed an unclassified detail link: label=%r href=%r",
                event_id,
                anchor.get_text(" ", strip=True),
                raw_url,
            )
            continue
        allowed_hosts = VIDEO_HOSTS if field == "video_url" else DOCUMENT_HOSTS
        emitted = _emit_url(raw_url, detail_url, allowed_hosts, field)
        if not emitted:
            continue
        if documents[field] and documents[field] != emitted:
            logger.warning(
                "Casa Grande event %s exposed multiple %s values; retaining the first: %r",
                event_id,
                field,
                emitted,
            )
            continue
        documents[field] = emitted
    logger.info(
        "Casa Grande event %s document evidence: agenda=%s packet=%s minutes=%s video=%s",
        event_id,
        bool(documents["agenda_url"]),
        bool(documents["agenda_packet_url"]),
        bool(documents["minutes_url"]),
        bool(documents["video_url"]),
    )
    return documents


def _status(title: str, documents: dict[str, str]) -> str:
    if _CANCEL_RE.search(title):
        return "Cancelled"
    if documents["minutes_url"]:
        return "Minutes Available"
    if documents["agenda_url"] or documents["agenda_packet_url"]:
        return "Agenda Available"
    return "Scheduled"


def scrape_calendar(url: str = DEFAULT_URL) -> list[dict[str, str]]:
    """Return Casa Grande council meetings from this calendar month forward."""
    list_url = _build_list_url(url)
    cutoff = date.today().replace(day=1)
    with make_session() as session:
        list_html = _fetch_text_bounded(session, list_url, LIST_HOSTS, MAX_LIST_BYTES)
        candidates = _parse_calendar_list(list_html, list_url, cutoff)
        if not candidates:
            logger.warning("health_empty_kind=confirmed_empty")
            logger.warning(
                "Casa Grande City Council calendar witnessed zero current-month-forward events"
            )
        meetings: list[dict[str, str]] = []
        for candidate in candidates:
            detail_html = _fetch_text_bounded(
                session,
                candidate["detail_url"],
                LIST_HOSTS,
                MAX_DETAIL_BYTES,
            )
            documents = _parse_detail_documents(
                detail_html,
                candidate["detail_url"],
                candidate["meeting_id"],
            )
            meetings.append(
                {
                    "meeting_title": candidate["meeting_title"],
                    "meeting_date": candidate["meeting_date"],
                    "meeting_time": candidate["meeting_time"],
                    "meeting_location": candidate["meeting_location"],
                    "meeting_status": _status(candidate["meeting_title"], documents),
                    "agenda_url": documents["agenda_url"],
                    "minutes_url": documents["minutes_url"],
                    "video_url": documents["video_url"],
                    "agenda_packet_url": documents["agenda_packet_url"],
                    "ecomment_url": "",
                    "meeting_id": candidate["meeting_id"],
                }
            )
        logger.info("Casa Grande scrape completed: meetings_emitted=%s", len(meetings))
        return meetings
