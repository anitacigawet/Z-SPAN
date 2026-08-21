"""St. Johns source-state parser with explicit legacy and replacement blockers."""

from __future__ import annotations

from datetime import date, datetime
import json
import logging
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup
import requests

from polite_http import make_session

logger = logging.getLogger(__name__)

LEGACY_URL = "https://www.sjaz.us/meetings-agendas/"
REPLACEMENT_URL = "https://www.stjohnsaz.gov/events"
INPUT_HOSTS = {"sjaz.us", "www.sjaz.us", "stjohnsaz.gov", "www.stjohnsaz.gov"}
LEGACY_HOSTS = {"sjaz.us", "www.sjaz.us"}
REPLACEMENT_HOSTS = {"stjohnsaz.gov", "www.stjohnsaz.gov"}
MAX_RESPONSE_BYTES = 2_000_000
_CANCEL_RE = re.compile(r"\bcancel(?:l?ed|l?ation)\b", re.IGNORECASE)
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _validate_input(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or _host(url) not in INPUT_HOSTS:
        raise ValueError("St. Johns source URL must use HTTPS on an official city host")


def _fetch_bounded(session, url: str, allowed_hosts: set[str]) -> str:
    with session.get(url, timeout=30, stream=True, allow_redirects=True) as response:
        response.raise_for_status()
        if urlparse(response.url).scheme != "https" or _host(response.url) not in allowed_hosts:
            raise RuntimeError(f"St. Johns source redirected to a disallowed host: {response.url}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise RuntimeError(
                    f"St. Johns response exceeded the {MAX_RESPONSE_BYTES}-byte safety cap"
                )
        logger.info("St. Johns fetched %s bytes from %s", len(body), response.url)
        return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _is_account_suspended(html: str) -> bool:
    lowered = html[:50_000].lower()
    return (
        "account suspended" in lowered
        or "this account has been suspended" in lowered
        or "cgi-sys/suspendedpage.cgi" in lowered
    )


def _is_client_challenge(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
    lowered = html[:50_000].lower()
    return (
        title == "client challenge"
        or "client challenge" in lowered
        or "_cf_chl_opt" in lowered
        or "challenges.cloudflare.com" in lowered
    )


def _json_events(soup: BeautifulSoup) -> list[dict]:
    events: list[dict] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(script.string or script.get_text())
        except json.JSONDecodeError:
            logger.warning("St. Johns replacement exposed malformed JSON-LD")
            continue
        values = payload if isinstance(payload, list) else [payload]
        for value in values:
            if not isinstance(value, dict):
                continue
            graph = value.get("@graph")
            candidates = graph if isinstance(graph, list) else [value]
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                event_type = candidate.get("@type")
                if event_type == "Event" or (
                    isinstance(event_type, list) and "Event" in event_type
                ):
                    events.append(candidate)
    return events


def _parse_start(raw: object) -> tuple[date, str]:
    if not isinstance(raw, str):
        raise RuntimeError("St. Johns structured event has no string startDate")
    if _DATE_ONLY_RE.fullmatch(raw):
        try:
            return date.fromisoformat(raw), ""
        except ValueError as exc:
            raise RuntimeError(f"St. Johns structured event has invalid startDate: {raw!r}") from exc
    try:
        starts_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"St. Johns structured event has invalid startDate: {raw!r}") from exc
    return starts_at.date(), starts_at.strftime("%I:%M %p").lstrip("0")


def _location_name(raw: object) -> str:
    if isinstance(raw, str):
        return BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    if isinstance(raw, dict):
        name = raw.get("name")
        if isinstance(name, str):
            return BeautifulSoup(name, "html.parser").get_text(" ", strip=True)
    return ""


def _parse_replacement(html: str, cutoff: date) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    events = _json_events(soup)
    if not events:
        page_text = soup.get_text(" ", strip=True).lower()
        if "no events" in page_text or "no upcoming events" in page_text:
            logger.warning("health_empty_kind=confirmed_empty")
            logger.warning("St. Johns replacement proved an explicit current event-empty state")
            return []
        raise RuntimeError(
            "St. Johns replacement is accessible but exposes no server-rendered Event evidence"
        )

    meetings: list[dict[str, str]] = []
    current_non_council = 0
    historical = 0
    for event in events:
        name_raw = event.get("name")
        if not isinstance(name_raw, str) or not name_raw.strip():
            raise RuntimeError("St. Johns structured event has no title")
        title = BeautifulSoup(name_raw, "html.parser").get_text(" ", strip=True)
        meeting_date, meeting_time = _parse_start(event.get("startDate"))
        if meeting_date < cutoff:
            historical += 1
            continue
        if "council" not in title.lower():
            current_non_council += 1
            continue
        meetings.append(
            {
                "meeting_title": title,
                "meeting_date": meeting_date.isoformat(),
                "meeting_time": meeting_time,
                "meeting_location": _location_name(event.get("location")),
                "meeting_status": "Cancelled" if _CANCEL_RE.search(title) else "Scheduled",
                "agenda_url": "",
                "minutes_url": "",
                "video_url": "",
                "agenda_packet_url": "",
                "ecomment_url": "",
                "meeting_id": "",
            }
        )
    logger.info(
        "St. Johns replacement audit: structured_events=%s historical=%s "
        "current_non_council=%s emitted=%s",
        len(events),
        historical,
        current_non_council,
        len(meetings),
    )
    if not meetings:
        logger.warning("health_empty_kind=confirmed_empty")
        logger.warning(
            "St. Johns replacement proved an honest current council empty from %s structured events",
            len(events),
        )
    return meetings


def scrape_calendar(calendar_url: str = LEGACY_URL) -> list[dict[str, str]]:
    """Use the deterministic replacement domain or fail loudly on its active blocker."""
    _validate_input(calendar_url)
    legacy_state = "not_probed"
    cutoff = date.today().replace(day=1)
    with make_session() as session:
        try:
            legacy_html = _fetch_bounded(session, LEGACY_URL, LEGACY_HOSTS)
        except requests.exceptions.SSLError as exc:
            legacy_state = f"verified_tls_failed:{exc}"
            logger.warning(
                "St. Johns legacy sjaz.us source is blocked before content: TLS verification failed; "
                "the retired source's Account Suspended state cannot be safely fetched"
            )
        except requests.RequestException as exc:
            legacy_state = f"transport_failed:{exc}"
            logger.warning("St. Johns legacy sjaz.us source transport failed: %s", exc)
        else:
            if _is_account_suspended(legacy_html):
                legacy_state = "account_suspended"
                logger.warning(
                    "St. Johns legacy sjaz.us source explicitly reports Account Suspended; "
                    "it is not an honest successful zero"
                )
            else:
                legacy_state = "unexpected_legacy_content"
                logger.warning(
                    "St. Johns legacy source no longer exposes the expected Account Suspended marker"
                )

        try:
            replacement_html = _fetch_bounded(session, REPLACEMENT_URL, REPLACEMENT_HOSTS)
        except requests.RequestException as exc:
            logger.warning("health_empty_kind=source_blocked")
            raise RuntimeError(
                f"St. Johns has no usable official source: legacy={legacy_state}; "
                f"replacement transport failed={exc}"
            ) from exc
        if _is_client_challenge(replacement_html):
            logger.warning("health_empty_kind=source_blocked")
            raise RuntimeError(
                "St. Johns has no usable server-side source: legacy="
                f"{legacy_state}; deterministic replacement stjohnsaz.gov is behind Client Challenge"
            )
        return _parse_replacement(replacement_html, cutoff)
