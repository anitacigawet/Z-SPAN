"""legistar_odata_helper — shared scraper for Legistar OData API.

Per D-085 (use the authoritative source) + D-125 (Legistar OData freshness
canon) + the vendor_fingerprint.check_legistar_freshness pattern. The
`webapi.legistar.com/v1/<jurisdiction>/events` endpoint returns structured
JSON with stable schema — paginates cleanly, no HTML parsing, no ASP.NET
postback simulation, no Playwright. Every Legistar-platform city should
use this instead of scraping Calendar.aspx HTML or the broken Excel export.

Worked example: Phoenix's Calendar.aspx Excel-export POST returned HTML
(not CSV) as of 2026-06-25 — the parser ran 1911 "successful" rows with
every field empty (F8 silent failure). Switching to OData returns 2,500+
events with full per-event metadata including EventDate, EventTime,
EventBodyName, EventLocation, EventInSiteURL, EventVideoPath, agenda
+ minutes file pointers.

Usage:

    from legistar_odata_helper import scrape_legistar_odata

    def scrape_calendar(url=None):
        # url is the public Calendar.aspx URL — we extract the
        # jurisdiction slug from it (the subdomain before .legistar.com).
        return scrape_legistar_odata(jurisdiction='phoenix', city_name='Phoenix')

Jurisdiction slug — the subdomain prefix of the Calendar.aspx URL:
    https://phoenix.legistar.com         → 'phoenix'
    https://glendale-az.legistar.com     → 'glendale-az'  (note the hyphen)
    https://mesa.legistar.com            → 'mesa'

Some jurisdictions vendor-migrated off Legistar AND left their Legistar
archive in place (Glendale shipped to a new platform in 2017 but
glendale-az.legistar.com still returns 200 with frozen 2017 data).
Callers should consult check_legistar_freshness() (in
scripts/vendor_fingerprint.py) to detect this — a 9-year-old most-recent
event means the city migrated and the OData archive is stale-known.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import requests

logger = logging.getLogger(__name__)

USER_AGENT = "Z-SPAN/1.0 (civic transparency; +https://zspan.org)"
DEFAULT_TIMEOUT = 30


def scrape_legistar_odata(
    jurisdiction: str,
    city_name: str,
    *,
    max_events: int = 2000,
    timeout: int = DEFAULT_TIMEOUT,
) -> list[dict]:
    """Scrape all events for a Legistar jurisdiction via the OData API.

    Returns a list of meeting dicts in the canonical schema
    (meeting_title, meeting_date, meeting_time, meeting_location,
    meeting_status, agenda_url, minutes_url, video_url,
    agenda_packet_url, ecomment_url, meeting_id).

    Args:
        jurisdiction: the OData slug (e.g. 'phoenix', 'mesa', 'glendale-az').
            Almost always matches the subdomain of the public
            Calendar.aspx URL.
        city_name: human-readable city name written into city_name field.
        max_events: cap on how many events to fetch (Legistar paginates;
            this is an upper bound across all pages).
        timeout: per-request HTTP timeout.

    Returns:
        list of meeting dicts. Empty list on any error (with WARNING
        logged — caller can decide whether to halt or treat as honest-
        empty; per F8, callers should check log output to disambiguate
        "no events" from "API error").
    """
    base_url = f"https://webapi.legistar.com/v1/{jurisdiction}/events"
    out: list[dict] = []
    page_size = 1000
    skip = 0

    while len(out) < max_events:
        # OData $top + $skip pagination, with $orderby for deterministic
        # ordering (newest first so a max_events cap returns the newest).
        url = f"{base_url}?$orderby=EventDate+desc&$top={page_size}&$skip={skip}"
        try:
            resp = requests.get(
                url,
                timeout=timeout,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
        except requests.RequestException as e:
            logger.warning("legistar_odata: %s page skip=%d request failed: %s", jurisdiction, skip, e)
            return out

        if resp.status_code != 200:
            logger.warning(
                "legistar_odata: %s returned HTTP %d at skip=%d — stopping pagination",
                jurisdiction, resp.status_code, skip,
            )
            return out

        try:
            page = resp.json()
        except ValueError:
            logger.warning("legistar_odata: %s returned non-JSON body at skip=%d", jurisdiction, skip)
            return out

        if not isinstance(page, list) or len(page) == 0:
            break  # exhausted

        for ev in page:
            out.append(_event_to_canonical(ev, city_name, jurisdiction))
            if len(out) >= max_events:
                break

        if len(page) < page_size:
            break  # last page (partial)
        skip += page_size

    logger.info("legistar_odata: %s — fetched %d events", jurisdiction, len(out))
    return out


def _event_to_canonical(ev: dict, city_name: str, jurisdiction: str) -> dict:
    """Convert a Legistar OData event dict to canonical meeting schema."""
    event_date = ev.get("EventDate")
    if event_date:
        # Shape: '2026-06-24T00:00:00' — ISO, naive. Take date-only.
        event_date = event_date[:10]

    # Build agenda/minutes URLs. Legistar returns file paths relative to
    # the public site; the public Calendar.aspx links pattern is:
    # https://<jurisdiction>.legistar.com/MeetingDetail.aspx?LEGID=N
    # EventAgendaFile/EventMinutesFile when present are direct file URLs.
    agenda_url = _make_url(ev.get("EventAgendaFile"), jurisdiction)
    minutes_url = _make_url(ev.get("EventMinutesFile"), jurisdiction)
    video_url = _make_url(ev.get("EventVideoPath"), jurisdiction)

    insite_url = ev.get("EventInSiteURL") or ""

    # Status: if EventAgendaStatusName is "Final" or there's a video,
    # treat as Past; else Scheduled.
    has_video = bool(video_url)
    has_minutes = ev.get("EventMinutesStatusName") in ("Final", "Tentative")
    meeting_status = "Past" if (has_video or has_minutes) else "Scheduled"

    return {
        "city_name": city_name,
        "meeting_title": ev.get("EventBodyName") or "",
        "meeting_date": event_date or "",
        "meeting_time": ev.get("EventTime") or "",
        "meeting_location": ev.get("EventLocation") or "",
        "meeting_status": meeting_status,
        "agenda_url": agenda_url or insite_url,  # fall back to InSite URL
        "minutes_url": minutes_url or "",
        "video_url": video_url or "",
        "agenda_packet_url": "",  # Legistar OData doesn't expose this directly
        "ecomment_url": "",
        "meeting_id": str(ev.get("EventId") or ""),
    }


def _make_url(maybe_url: Optional[str], jurisdiction: str) -> str:
    """Normalize Legistar file references to full URLs."""
    if not maybe_url:
        return ""
    s = str(maybe_url).strip()
    if not s:
        return ""
    if s.startswith("http://") or s.startswith("https://"):
        return s
    # Relative URL — prepend the jurisdiction base
    if s.startswith("/"):
        return f"https://{jurisdiction}.legistar.com{s}"
    return f"https://{jurisdiction}.legistar.com/{s}"
