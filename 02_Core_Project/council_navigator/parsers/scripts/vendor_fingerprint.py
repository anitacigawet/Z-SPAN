#!/usr/bin/env python3
"""vendor_fingerprint.py — Recon-2: deterministic civic-vendor fingerprinter.

Identifies the civic-software vendor behind a city's website (Granicus,
Legistar, CivicPlus, CivicClerk, Swagit, Destiny, Hyland, NovusAgenda,
IQM2, Municode, CivicWeb, etc.) using URL-subdomain + HTML-signature
regex rules. Replaces the LLM "calendar_format" classification step the
per-city Sonnet recon currently does.

Direct application of D-085 ("stop using LLMs where authoritative source
exists") at the recon layer, per the 2026-06-14 RECON_SWARM_AUDIT (Action
2). Becomes a hard precondition on the per-city Sonnet contract: vendor
is determined deterministically; LLM only runs when the fingerprinter
returns `unknown`.

How it works:
  1. URL-only fast path — regex on host + path. ~95% of vendor-hosted
     municipal calendars sit at a `*.<vendor>.com` subdomain or use a
     well-known URL path (e.g. `/AgendaCenter`). Returns immediately
     with `confidence: high`.
  2. HTML fallback — if the URL is at the city's own domain, one GET +
     regex over the HTML for `<script src=...>`, `<iframe src=...>`,
     `<meta name="generator">`, and tracked third-party asset paths.
     Returns `confidence: medium`.
  3. If nothing matches → `vendor: unknown`; the caller routes to the
     Sonnet fallback per the audit.

Usage:
    python3.11 vendor_fingerprint.py --url https://kingman.granicus.com/
    python3.11 vendor_fingerprint.py --url <url> --json
    python3.11 vendor_fingerprint.py --url <url> --no-fetch
    python3.11 vendor_fingerprint.py --batch
    python3.11 vendor_fingerprint.py --batch --json

References:
    01_Project_Overview/RECON_SWARM_AUDIT_2026-06-14.md (Action 2)
    01_Project_Overview/DECISIONS.md#d-085
    Wappalyzer fingerprint OSS forks:
      - https://github.com/tunetheweb/wappalyzer
      - https://github.com/projectdiscovery/wappalyzergo
    BuiltWith Granicus tracker (4,684 US customers):
      - https://trends.builtwith.com/mx/Granicus/United-States
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    sys.exit("vendor_fingerprint.py needs requests. Install: pip install requests")


_SCRIPT_DIR = Path(__file__).resolve().parent
_PARSERS_DIR = _SCRIPT_DIR.parent
DEFAULT_PARSER_INDEX = _PARSERS_DIR / "parser_index.json"

USER_AGENT = (
    "Z-SPAN/recon-2 vendor-fingerprint (+https://github.com/anitacigawet/Z-SPAN)"
)
DEFAULT_TIMEOUT = 10  # seconds; one shot, no retries — fingerprinter is a probe

# ---------------------------------------------------------------------------
# Vendor enum + rule sets
# ---------------------------------------------------------------------------

# Stable vendor identifiers — used as the `vendor` key in the result dict
# and (eventually) as the canonical `calendar_format` value in parser_index.
VENDORS = {
    "granicus",       # Granicus + variants (RSS, OpenCities) — *.granicus.com
    "legistar",       # Legistar (Granicus product, distinct subdomain) — *.legistar.com
    "civicplus",      # CivicPlus / CivicEngage Agenda Center — civicplus.com paths + /AgendaCenter
    "civicclerk",     # CivicClerk + portal.civicclerk.com — *.civicclerk.com
    "primegov",       # PrimeGov (Granicus-acquired distinct product line) — *.primegov.com + /api/v2/PublicPortal/ (added 2026-06-17 per V0 round-3 Prescott Valley finding — see CODEX_PER_CITY_RECON_ROUND3_2026-06-17.md)
    "swagit",         # Swagit video / SwagitAdmin — swagit.com
    "destiny",        # Destiny Hosted / DestinyAgendaQuick — destinyhosted.com
    "hyland",         # Hyland AgendaOnline / OnBase — *.hylandcloud.com
    "novusagenda",    # NovusAgenda — *.novusagenda.com
    "iqm2",           # IQM2 (legacy Granicus product) — *.iqm2.com
    "municode",       # Municode Meetings — *.municodemeetings.com
    "civicweb",       # CivicWeb iCompass / Diligent — *.civicweb.net
    "tribe_events",   # The Events Calendar (WordPress plugin) — /wp-json/tribe/events
    "streamline",     # Streamline (getstreamline.com) civic-engagement SaaS for small districts/towns — non-SPA, server-rendered "Traction" framework; signatures: "Powered by Streamline" footer + planBrand:streamline JS + /assets/traction/ path (added 2026-06-17 per V0 chunk-4 Fredonia finding F12 — see COCONINO_CLOSE_2026-06-17.md)
    "m1downloadlist", # m1downloadlist WordPress plugin used for small-town council document archives — non-SPA, server-rendered query-string state; signatures: /wp-content/plugins/m1downloadlist/ + m1dll_filelist class + ?m1dll_index_get= query param (added 2026-06-17 per V0 chunk-5 Huachuca City finding F17 — see COCHISE_CLOSE_2026-06-17.md). NOTE: such sites often ALSO load The Events Calendar (tribe_events) site-wide as WordPress global assets — the tribe_events signal is a site-wide false-positive for the document-archive view; verify view-level rendering before trusting tribe_events for a council-doc calendar_url.
    "tablepress",     # TablePress WordPress plugin (750k+ install base) used for small-town council meeting-table rendering — non-SPA, server-rendered HTML table; signatures: /wp-content/plugins/tablepress/ + tablepress-id-N class + tablepress-css handle (added 2026-06-18 per V0 chunk-6 Taylor finding F20 — see NAVAJO_CLOSE_2026-06-18.md). Generic table renderer rather than purpose-built civic vendor (parallel to m1downloadlist); justified by routing requirement (HTML table scrape with tablepress-N anchors). F17 site-wide false-positive disambiguation applies — verify view-level rendering before trusting tribe_events sitewide assets.
    "unknown",
}

# ---------------------------------------------------------------------------
# URL rules (fast path; no HTTP required)
#
# Each rule: (vendor, regex, method, evidence_template, description)
# Walked in order; first match wins. High-confidence subdomain rules first;
# medium-confidence path rules after.
# ---------------------------------------------------------------------------

_URL_RULES: list[tuple[str, re.Pattern[str], str, str, str]] = [
    # ----- High-confidence: vendor-hosted subdomain -----
    (
        "legistar",
        re.compile(r"^https?://(?:[^/]+\.)?legistar\.com(?::\d+)?(?:[/?#]|$)", re.IGNORECASE),
        "url_subdomain",
        "legistar.com subdomain",
        "Legistar — Granicus product on its own subdomain (phoenix.legistar.com etc.)",
    ),
    (
        "granicus",
        re.compile(r"^https?://(?:[^/]+\.)?granicus\.com(?::\d+)?(?:[/?#]|$)", re.IGNORECASE),
        "url_subdomain",
        "granicus.com subdomain",
        "Granicus (ViewPublisher / RSS / OpenCities)",
    ),
    (
        "civicclerk",
        re.compile(
            r"^https?://(?:[^/]+\.)?(?:portal\.)?civicclerk\.com(?::\d+)?(?:[/?#]|$)",
            re.IGNORECASE,
        ),
        "url_subdomain",
        "civicclerk.com subdomain",
        "CivicClerk portal (e.g. prescottaz.portal.civicclerk.com)",
    ),
    (
        "civicplus",
        re.compile(r"^https?://(?:[^/]+\.)?civicplus\.com(?::\d+)?(?:[/?#]|$)", re.IGNORECASE),
        "url_subdomain",
        "civicplus.com subdomain",
        "CivicPlus direct domain",
    ),
    (
        "hyland",
        re.compile(r"^https?://(?:[^/]+\.)?hylandcloud\.com(?::\d+)?(?:[/?#]|$)", re.IGNORECASE),
        "url_subdomain",
        "hylandcloud.com subdomain",
        "Hyland AgendaOnline / OnBase",
    ),
    (
        "hyland",
        re.compile(r"^https?://(?:[^/]+\.)?databankcloud\.com(?::\d+)?(?:[/?#]|$)", re.IGNORECASE),
        "url_subdomain",
        "databankcloud.com subdomain (Hyland)",
        "Hyland-operated Databank subdomain (added 2026-06-19 per V0 chunk-8 Gilbert finding F27 — see MARICOPA_CLOSE_2026-06-19.md)",
    ),
    (
        "destiny",
        re.compile(r"^https?://(?:[^/]+\.)?destinyhosted\.com(?::\d+)?(?:[/?#]|$)", re.IGNORECASE),
        "url_subdomain",
        "destinyhosted.com subdomain",
        "Destiny Hosted / AgendaQuick",
    ),
    (
        "novusagenda",
        re.compile(r"^https?://(?:[^/]+\.)?novusagenda\.com(?::\d+)?(?:[/?#]|$)", re.IGNORECASE),
        "url_subdomain",
        "novusagenda.com subdomain",
        "NovusAgenda",
    ),
    (
        "iqm2",
        re.compile(r"^https?://(?:[^/]+\.)?iqm2\.com(?::\d+)?(?:[/?#]|$)", re.IGNORECASE),
        "url_subdomain",
        "iqm2.com subdomain",
        "IQM2 (legacy Granicus product)",
    ),
    (
        "municode",
        re.compile(
            r"^https?://(?:[^/]+\.)?municodemeetings\.com(?::\d+)?(?:[/?#]|$)", re.IGNORECASE
        ),
        "url_subdomain",
        "municodemeetings.com subdomain",
        "Municode Meetings",
    ),
    (
        "civicweb",
        re.compile(r"^https?://(?:[^/]+\.)?civicweb\.net(?::\d+)?(?:[/?#]|$)", re.IGNORECASE),
        "url_subdomain",
        "civicweb.net subdomain",
        "CivicWeb iCompass / Diligent",
    ),
    (
        "swagit",
        re.compile(r"^https?://(?:[^/]+\.)?swagit\.com(?::\d+)?(?:[/?#]|$)", re.IGNORECASE),
        "url_subdomain",
        "swagit.com subdomain",
        "Swagit (video + SwagitAdmin)",
    ),
    (
        "primegov",
        re.compile(r"^https?://(?:[^/]+\.)?primegov\.com(?::\d+)?(?:[/?#]|$)", re.IGNORECASE),
        "url_subdomain",
        "primegov.com subdomain",
        "PrimeGov (Granicus-acquired SPA portal; /api/v2/PublicPortal/ OData endpoints)",
    ),
    # ----- Medium-confidence: well-known URL paths on the city's own domain -----
    (
        "primegov",
        re.compile(r"^https?://[^/]+/api/v2/PublicPortal/", re.IGNORECASE),
        "url_path",
        "/api/v2/PublicPortal/ API path",
        "PrimeGov OData endpoint pattern (e.g. prescottvalley.primegov.com/api/v2/PublicPortal/ListUpcomingMeetings)",
    ),
    (
        "civicplus",
        re.compile(r"^https?://[^/]+/AgendaCenter(?:/|$|\?)", re.IGNORECASE),
        "url_path",
        "/AgendaCenter path",
        "CivicPlus AgendaCenter (standard URL pattern on city domains)",
    ),
    (
        "tribe_events",
        re.compile(
            r"^https?://[^/]+/wp-json/tribe/events(?:/|$|\?)", re.IGNORECASE
        ),
        "url_path",
        "/wp-json/tribe/events path",
        "The Events Calendar (WordPress plugin) — JSON API",
    ),
]

# ---------------------------------------------------------------------------
# HTML rules (slow path; one HTTP GET required)
#
# Walked in order on the response body if URL rules didn't match. Each rule
# is (vendor, regex, method, evidence_template). The regex matches against
# the raw HTML.
# ---------------------------------------------------------------------------

_HTML_RULES: list[tuple[str, re.Pattern[str], str, str]] = [
    (
        "granicus",
        re.compile(
            r"""(?:src|href|action)\s*=\s*["'][^"']*\.granicus\.com""", re.IGNORECASE
        ),
        "html_asset",
        "granicus.com asset reference in HTML",
    ),
    (
        "legistar",
        re.compile(
            r"""(?:src|href|action)\s*=\s*["'][^"']*\.legistar\.com""", re.IGNORECASE
        ),
        "html_asset",
        "legistar.com asset reference in HTML",
    ),
    (
        "civicplus",
        re.compile(
            r"""(?:src|href|action)\s*=\s*["'][^"']*civicplus\.com""", re.IGNORECASE
        ),
        "html_asset",
        "civicplus.com asset reference in HTML",
    ),
    (
        "civicplus",
        re.compile(
            r"<meta\s+name=['\"]generator['\"]\s+content=['\"][^'\"]*CivicPlus",
            re.IGNORECASE,
        ),
        "html_meta",
        "CivicPlus <meta generator> tag",
    ),
    (
        "civicclerk",
        re.compile(
            r"""(?:src|href|action)\s*=\s*["'][^"']*civicclerk\.com""", re.IGNORECASE
        ),
        "html_asset",
        "civicclerk.com asset reference in HTML",
    ),
    (
        "swagit",
        re.compile(
            r"""(?:src|href|action)\s*=\s*["'][^"']*swagit\.com""", re.IGNORECASE
        ),
        "html_asset",
        "swagit.com asset (video player or admin)",
    ),
    (
        "primegov",
        re.compile(
            r"""(?:src|href|action)\s*=\s*["'][^"']*primegov\.com""", re.IGNORECASE
        ),
        "html_asset",
        "primegov.com asset reference (typically iframe embedding the portal)",
    ),
    (
        "destiny",
        re.compile(
            r"""(?:src|href|action)\s*=\s*["'][^"']*destinyhosted\.com""",
            re.IGNORECASE,
        ),
        "html_asset",
        "destinyhosted.com asset reference in HTML",
    ),
    (
        "hyland",
        re.compile(
            r"""(?:src|href|action)\s*=\s*["'][^"']*hylandcloud\.com""", re.IGNORECASE
        ),
        "html_asset",
        "hylandcloud.com asset reference in HTML",
    ),
    (
        "hyland",
        re.compile(
            r"""(?:src|href|action)\s*=\s*["'][^"']*databankcloud\.com""", re.IGNORECASE
        ),
        "html_asset",
        "databankcloud.com asset reference in HTML (Hyland — F27)",
    ),
    (
        "hyland",
        re.compile(r"""OnBase[\s_-]*(?:Agenda[\s_-]*Online|Logo)""", re.IGNORECASE),
        "html_asset",
        "OnBase Agenda Online / Logo text marker (Hyland — F27)",
    ),
    (
        "hyland",
        re.compile(r"""Hyland\s+Software,?\s*Inc\.?""", re.IGNORECASE),
        "html_asset",
        "Hyland Software, Inc. copyright marker (F27)",
    ),
    (
        "novusagenda",
        re.compile(
            r"""(?:src|href|action)\s*=\s*["'][^"']*novusagenda\.com""", re.IGNORECASE
        ),
        "html_asset",
        "novusagenda.com asset reference in HTML",
    ),
    (
        "iqm2",
        re.compile(
            r"""(?:src|href|action)\s*=\s*["'][^"']*iqm2\.com""", re.IGNORECASE
        ),
        "html_asset",
        "iqm2.com asset reference in HTML",
    ),
    (
        "municode",
        re.compile(
            r"""(?:src|href|action)\s*=\s*["'][^"']*municode""", re.IGNORECASE
        ),
        "html_asset",
        "municode asset reference in HTML",
    ),
    (
        "civicweb",
        re.compile(
            r"""(?:src|href|action)\s*=\s*["'][^"']*civicweb\.net""", re.IGNORECASE
        ),
        "html_asset",
        "civicweb.net asset reference in HTML",
    ),
    (
        "tribe_events",
        re.compile(
            r"/wp-content/plugins/the-events-calendar", re.IGNORECASE
        ),
        "html_asset",
        "The Events Calendar plugin path",
    ),
    (
        "streamline",
        re.compile(
            r"""Powered by Streamline|planBrand["']?\s*:\s*["']streamline|/assets/traction/application-|streamline\.imgix\.net""",
            re.IGNORECASE,
        ),
        "html_asset",
        "Streamline civic SaaS signature (footer 'Powered by Streamline' / planBrand JS / Traction asset path / imgix CDN)",
    ),
    (
        "m1downloadlist",
        re.compile(
            r"""/wp-content/plugins/m1downloadlist/|m1dll_filelist|m1downloadlist-css|[?&]m1dll_index_get=|[?&]m1dll_subdirpath=""",
            re.IGNORECASE,
        ),
        "html_asset",
        "m1downloadlist WordPress plugin signature (plugin asset path / m1dll_filelist DOM / m1dll query params) — small-town council doc archive",
    ),
    (
        "tablepress",
        re.compile(
            r"""/wp-content/plugins/tablepress/|tablepress-id-\d+|tablepress-css""",
            re.IGNORECASE,
        ),
        "html_asset",
        "TablePress WordPress plugin signature (plugin asset path / tablepress-id-N class / tablepress-css handle) — small-town council table-rendered calendars",
    ),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fingerprint_url(url: str) -> Optional[dict]:
    """URL-only fingerprint. Returns the result dict or None if no rule matches.

    Fast path — no HTTP. Always called first; if it returns a hit, no fetch
    happens. Suitable for batch classification of a parser_index.
    """
    if not url:
        return None
    for vendor, pattern, method, evidence, description in _URL_RULES:
        if pattern.search(url):
            confidence = "high" if method == "url_subdomain" else "medium"
            return {
                "vendor": vendor,
                "confidence": confidence,
                "method": method,
                "evidence": evidence,
                "description": description,
                "url_checked": url,
            }
    return None


def fingerprint_html(url: str, html: str) -> Optional[dict]:
    """HTML fingerprint. Returns the result dict or None if no rule matches.

    Called when URL rules don't match. Always `confidence: medium` — HTML
    matches can be false positives if a page links to a vendor without
    being hosted on it.
    """
    if not html:
        return None
    for vendor, pattern, method, evidence in _HTML_RULES:
        m = pattern.search(html)
        if m:
            return {
                "vendor": vendor,
                "confidence": "medium",
                "method": method,
                "evidence": evidence,
                "description": f"HTML signature: {evidence}",
                "url_checked": url,
                "match_excerpt": _safe_excerpt(html, m.start(), m.end()),
            }
    return None


def _check_liveness(url: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """HEAD probe to test whether a URL responds with a non-definitively-dead status.

    Used by ``fingerprint_vendor`` as a liveness check for high-confidence
    URL-subdomain matches — guards against legacy/dead vendor subdomains
    that still URL-pattern-match but no longer serve calendar data (e.g.
    a city that migrated from Granicus to PrimeGov but kept the
    pvaz.granicus.com subdomain reachable as a 404 page).

    Added 2026-06-17 per V0 round-3 Prescott Valley finding — see
    `01_Project_Overview/CODEX_PER_CITY_RECON_ROUND3_2026-06-17.md`. The
    same-day refinement (the 405-handling) was empirically derived
    immediately after the initial fix when verification surfaced that
    PrimeGov API endpoints return 405 to HEAD (Method Not Allowed); the
    URL exists, the method just isn't supported. Definitively-dead status
    codes (404, 410) trigger downgrade; method-related codes (405, 501)
    + auth-related codes (401, 403) + transient server errors (5xx other
    than 501) do NOT downgrade — they pass the original match through to
    the caller for evidence-led handling.

    Returns:
        Dict with keys:
            alive (bool): True unless the status code is definitively-dead
                (404, 410) OR the request itself failed (connection error,
                DNS failure, timeout).
            status (int|None): the final HTTP status code (after redirects),
                or None if the request itself failed.
            error (str): empty if the request succeeded; type+message
                otherwise.
    """
    # Status codes that definitively mean "this URL is gone / never existed."
    # Other 4xx codes (401 / 403 / 405 / 451) are NOT in this set — they
    # indicate the URL exists but is method- or auth-constrained, not dead.
    # 5xx codes (except 501) are transient server errors, not URL-deadness.
    _DEFINITELY_DEAD = {404, 410}

    try:
        resp = requests.head(
            url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=True,
        )
        return {
            "alive": resp.status_code not in _DEFINITELY_DEAD,
            "status": resp.status_code,
            "error": "",
        }
    except requests.RequestException as e:
        return {
            "alive": False,
            "status": None,
            "error": f"{type(e).__name__}: {e}",
        }


def check_legistar_freshness(
    jurisdiction: str,
    *,
    max_age_months: int = 6,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """F28 freshness probe for Legistar (added 2026-06-19 per V0 chunk-8 Goodyear).

    Probes the public Legistar OData API to read the most-recent event date
    for a jurisdiction. Returns a dict suitable for emission into a
    `freshness_status` field on the discovery JSON OR a sweep report row.

    Why this exists: Goodyear's Legistar instance (`webapi.legistar.com/v1/
    goodyear/events`) returns HTTP 200 with valid OData JSON shape but is
    frozen at 2021-02-02 — five years stale. The Goodyear .gov has migrated
    to OpenMedia + .gov calendar, but the fingerprinter would still classify
    `goodyear.legistar.com/Calendar.aspx` as `legistar` based on URL alone.
    This produces "succeeded-empty" failure mode per the project-wide F8
    rule: the API technically responds, just with no current data.

    The pattern is structural to the SaaS-migration class of vendor changes
    (the Prescott Valley Granicus→PrimeGov pattern is the parallel; sibling
    Granicus/CivicClerk/PrimeGov probes are TODO).

    Args:
        jurisdiction: the Legistar API jurisdiction slug. For
            `phoenix.legistar.com/Calendar.aspx` this is `phoenix`. For
            `glendale-az.legistar.com/Calendar.aspx` this is `glendale-az`.
            Derive from the Legistar subdomain (everything before
            `.legistar.com`).
        max_age_months: how many months stale to tolerate before flagging.
            Default 6 — wider than a typical council cadence (monthly +
            committees) so we don't false-flag during recess windows but
            tight enough to catch the Goodyear / Glendale shape.
        timeout: HTTP timeout in seconds.

    Returns:
        Dict with keys:
            jurisdiction (str): the input.
            api_url (str): the probed Legistar OData endpoint.
            http_status (int|None): HTTP status of the probe.
            most_recent_event_date (str|None): ISO date of the freshest
                event, or None if no data.
            age_days (int|None): days between today and most_recent_event_date.
            is_fresh (bool|None): True if age_days <= max_age_months*30; None
                if no data / probe failed.
            freshness_status (str): one of:
                `fresh`            — events within max_age window (good)
                `stale_archive`    — events exist but oldest is past max_age
                                     (the Goodyear / Glendale pattern)
                `empty_response`   — HTTP 200 but zero events returned
                `bad_jurisdiction` — HTTP 4xx (invalid jurisdiction slug)
                `probe_error`      — request raised an exception
            error (str): empty on success, exception message otherwise.
    """
    api_url = f"https://webapi.legistar.com/v1/{jurisdiction}/events?$orderby=EventDate+desc&$top=1"
    out = {
        "jurisdiction": jurisdiction,
        "api_url": api_url,
        "http_status": None,
        "most_recent_event_date": None,
        "age_days": None,
        "is_fresh": None,
        "freshness_status": "probe_error",
        "error": "",
    }
    try:
        import json as _json
        from datetime import datetime, timezone
        resp = requests.get(
            api_url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        out["http_status"] = resp.status_code
        if 400 <= resp.status_code < 500:
            out["freshness_status"] = "bad_jurisdiction"
            out["error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
            return out
        if resp.status_code != 200:
            out["freshness_status"] = "probe_error"
            out["error"] = f"unexpected HTTP {resp.status_code}"
            return out
        data = resp.json()
        if not data or not isinstance(data, list) or len(data) == 0:
            out["freshness_status"] = "empty_response"
            return out
        event_date_raw = data[0].get("EventDate")
        if not event_date_raw:
            out["freshness_status"] = "empty_response"
            return out
        # EventDate shape: '2021-02-02T00:00:00' — ISO without TZ, naive
        try:
            event_dt = datetime.fromisoformat(event_date_raw[:19])
        except ValueError:
            out["freshness_status"] = "probe_error"
            out["error"] = f"unparseable EventDate {event_date_raw!r}"
            return out
        now = datetime.utcnow()
        age_days = (now - event_dt).days
        out["most_recent_event_date"] = event_date_raw[:10]
        out["age_days"] = age_days
        max_age_days = max_age_months * 30
        out["is_fresh"] = age_days <= max_age_days
        out["freshness_status"] = "fresh" if out["is_fresh"] else "stale_archive"
        return out
    except requests.RequestException as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out


def check_granicus_freshness(
    calendar_url: str,
    *,
    max_age_months: int = 6,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """F28 sibling freshness probe for Granicus *.granicus.com subdomains.

    Granicus does NOT expose a public OData API equivalent to Legistar's
    `webapi.legistar.com/v1/<jurisdiction>/events`. The RSS feed is the
    most-reliable public freshness signal — every Granicus deployment
    publishes one at `https://{host}/RSSFeed.aspx?view_id={view_id}&mode=Meetings`,
    populated with the same agenda/minutes the public ViewPublisher.php
    page renders, sorted newest first. The first <item><pubDate> is the
    most-recent meeting/agenda publication.

    URL discovery:
        host       — derived from the subdomain (e.g. `kingman.granicus.com`).
        view_id    — parsed from the calendar_url's query string (`view_id=N`).
                     Both `ViewPublisher.php` (HTML) and `ViewPublisherRSS.php`
                     (RSS) variants carry view_id; some parser_index rows
                     store the RSS-style URL directly, others store the HTML
                     view URL. The probe always normalizes to RSS.

    For Granicus-products-on-city-domain (OpenCities, Swagit admin layered
    on a *.gov host — Marana, Oro Valley shape) the URL won't be at
    *.granicus.com, so this probe returns `needs_alternate_probe` rather
    than try to guess; those deployments need their own probe (Phase 2).

    Returns the same dict schema as check_legistar_freshness:
        jurisdiction (str)       — Granicus subdomain (host minus `.granicus.com`)
        api_url (str)            — probed RSS endpoint
        http_status (int|None)
        most_recent_event_date (str|None) — ISO date of first <item><pubDate>
        age_days (int|None)
        is_fresh (bool|None)
        freshness_status (str)   — fresh / stale_archive / empty_response /
                                   bad_jurisdiction / probe_error /
                                   needs_alternate_probe
        error (str)
    """
    import re
    import xml.etree.ElementTree as ET
    from datetime import datetime
    from email.utils import parsedate_to_datetime

    out = {
        "jurisdiction": None,
        "api_url": None,
        "http_status": None,
        "most_recent_event_date": None,
        "age_days": None,
        "is_fresh": None,
        "freshness_status": "probe_error",
        "error": "",
    }

    try:
        host = urlparse(calendar_url).netloc.lower()
    except Exception:
        host = ""
    if not host.endswith(".granicus.com"):
        out["freshness_status"] = "needs_alternate_probe"
        out["error"] = (
            "calendar_url is not at *.granicus.com — likely Granicus OpenCities "
            "or Swagit admin layered on a city-owned domain; needs a different probe"
        )
        return out
    out["jurisdiction"] = host[: -len(".granicus.com")]

    m = re.search(r"[?&]view_id=(\d+)", calendar_url)
    if not m:
        out["freshness_status"] = "probe_error"
        out["error"] = "no view_id query parameter on calendar_url"
        return out
    view_id = m.group(1)
    rss_url = f"https://{host}/ViewPublisherRSS.php?view_id={view_id}"
    out["api_url"] = rss_url

    try:
        resp = requests.get(
            rss_url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml"},
        )
        out["http_status"] = resp.status_code
        if 400 <= resp.status_code < 500:
            out["freshness_status"] = "bad_jurisdiction"
            out["error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
            return out
        if resp.status_code != 200:
            out["freshness_status"] = "probe_error"
            out["error"] = f"unexpected HTTP {resp.status_code}"
            return out
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            out["freshness_status"] = "probe_error"
            out["error"] = f"unparseable RSS: {e}"
            return out
        # Granicus RSS shape: <rss><channel><item><pubDate>...</pubDate>...</item>
        item = root.find(".//item")
        if item is None:
            out["freshness_status"] = "empty_response"
            return out
        pubdate_el = item.find("pubDate")
        if pubdate_el is None or not (pubdate_el.text or "").strip():
            out["freshness_status"] = "empty_response"
            return out
        try:
            pubdate_dt = parsedate_to_datetime(pubdate_el.text.strip())
        except (TypeError, ValueError) as e:
            out["freshness_status"] = "probe_error"
            out["error"] = f"unparseable pubDate {pubdate_el.text!r}: {e}"
            return out
        if pubdate_dt.tzinfo is not None:
            pubdate_dt = pubdate_dt.replace(tzinfo=None)
        now = datetime.utcnow()
        age_days = (now - pubdate_dt).days
        out["most_recent_event_date"] = pubdate_dt.strftime("%Y-%m-%d")
        out["age_days"] = age_days
        max_age_days = max_age_months * 30
        out["is_fresh"] = age_days <= max_age_days
        out["freshness_status"] = "fresh" if out["is_fresh"] else "stale_archive"
        return out
    except requests.RequestException as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out


def check_civicclerk_freshness(
    calendar_url: str,
    *,
    max_age_months: int = 6,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """F28 sibling freshness probe for CivicClerk *.portal.civicclerk.com sites.

    Modern CivicClerk portals expose an OData API parallel to Legistar:
        https://{slug}.api.civicclerk.com/v1/Events?$orderby=startDateTime+desc&$top=1
    The portal URL `{slug}.portal.civicclerk.com/` maps to the API
    `{slug}.api.civicclerk.com/v1/Events`. Same OData semantics as Legistar
    — read the most-recent event's startDateTime, compare against threshold.

    Endpoint discovered 2026-06-19 by introspecting the SPA bundle at
    `/assets/index-*.js` (the `.api.civicclerk.com/v1` + `/Events` constants
    plus the documented OData $orderby/$top params). The LAUNCH_GATES note
    that referenced `/Meetings` was wrong — CivicClerk's modern surface
    uses /Events not /Meetings.

    URL discovery:
        slug — derived from the portal subdomain (e.g. `pageaz.portal.civicclerk.com`
               → `pageaz`).

    For AgendaCenter (`*.gov/AgendaCenter`) and Calendar.aspx (`*.gov/Calendar.aspx?CID=N`)
    URL patterns also classified as `civicclerk` in some parser_index rows,
    returns `needs_alternate_probe` — those are CivicPlus CivicEngage
    deployments on city-owned domains, not the *.portal.civicclerk.com
    OData line.

    Returns the same dict schema as check_legistar_freshness.
    """
    from datetime import datetime

    out = {
        "jurisdiction": None,
        "api_url": None,
        "http_status": None,
        "most_recent_event_date": None,
        "age_days": None,
        "is_fresh": None,
        "freshness_status": "probe_error",
        "error": "",
    }

    try:
        host = urlparse(calendar_url).netloc.lower()
    except Exception:
        host = ""
    if not host.endswith(".portal.civicclerk.com"):
        out["freshness_status"] = "needs_alternate_probe"
        out["error"] = (
            "calendar_url is not at *.portal.civicclerk.com — likely a "
            "CivicEngage AgendaCenter or Calendar.aspx deployment on a "
            "city-owned domain; needs a different probe"
        )
        return out
    slug = host[: -len(".portal.civicclerk.com")]
    out["jurisdiction"] = slug
    api_url = (
        f"https://{slug}.api.civicclerk.com/v1/Events"
        f"?$orderby=startDateTime+desc&$top=1"
    )
    out["api_url"] = api_url

    try:
        resp = requests.get(
            api_url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        out["http_status"] = resp.status_code
        if 400 <= resp.status_code < 500:
            out["freshness_status"] = "bad_jurisdiction"
            out["error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
            return out
        if resp.status_code != 200:
            out["freshness_status"] = "probe_error"
            out["error"] = f"unexpected HTTP {resp.status_code}"
            return out
        data = resp.json()
        # CivicClerk OData wraps results in {"value": [...]} (standard OData),
        # but some deployments return the bare list. Handle both.
        events = data.get("value") if isinstance(data, dict) else data
        if not events or not isinstance(events, list) or len(events) == 0:
            out["freshness_status"] = "empty_response"
            return out
        start_dt_raw = (
            events[0].get("startDateTime")
            or events[0].get("StartDateTime")
            or events[0].get("eventDate")
        )
        if not start_dt_raw:
            out["freshness_status"] = "empty_response"
            return out
        try:
            # CivicClerk StartDateTime shape varies: '2026-06-09T17:00:00Z' or
            # '2026-06-09T17:00:00' (no TZ). Strip Z if present for fromisoformat.
            iso_clean = start_dt_raw[:19].replace("Z", "")
            event_dt = datetime.fromisoformat(iso_clean)
        except ValueError:
            out["freshness_status"] = "probe_error"
            out["error"] = f"unparseable startDateTime {start_dt_raw!r}"
            return out
        now = datetime.utcnow()
        age_days = (now - event_dt).days
        out["most_recent_event_date"] = start_dt_raw[:10]
        out["age_days"] = age_days
        max_age_days = max_age_months * 30
        out["is_fresh"] = age_days <= max_age_days
        out["freshness_status"] = "fresh" if out["is_fresh"] else "stale_archive"
        return out
    except requests.RequestException as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out


# F28 Phase 2 layered-vendor probes — added 2026-06-19 after the V1 probes
# surfaced 8 cities at `needs_alternate_probe` (vendor products layered on
# city-owned .gov domains rather than vendor subdomains). Each Phase 2
# probe extends one V1 probe with a discovery step OR is its own HTML-scrape.

# Browser-style User-Agent. CivicEngage AgendaCenter rejects bot-style UAs
# (returns HTTP 403); the .gov sites that pass the granicus-discovery
# fetch also expect a real browser UA. Used by the Phase 2 paths only.
_UA_BROWSER = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0 Safari/537.36"
)


def discover_granicus_subdomain(
    gov_url: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> Optional[tuple]:
    """For a Granicus-on-.gov shell URL, find the underlying Granicus subdomain
    + a working view_id by scanning the .gov HTML for `*.granicus.com` references
    + probing common view_ids until one returns RSS with items.

    Returns (host, view_id) tuple if discovered, None otherwise. The host
    includes the full `*.granicus.com` form (e.g. `eloyaz.granicus.com`);
    view_id is the int that returned a populated RSS feed.

    Used as a fallback within sweep_granicus when calendar_url isn't at
    *.granicus.com directly — Eloy's parser_index URL points at the .gov
    shell but the data lives at `eloyaz.granicus.com/ViewPublisherRSS.php?view_id=1`.

    Does NOT raise on failure — returns None so the caller can downgrade
    to `needs_alternate_probe` or `probe_blocked` without exception flow.

    Implementation note: probes view_ids 1-5 sequentially. Granicus
    deployments typically host their primary council feed at view_id=1
    (default) with auxiliary feeds at 2-5; the small sweep covers the
    common case without spending real bandwidth on speculative discovery.
    """
    import re
    import xml.etree.ElementTree as ET

    try:
        resp = requests.get(
            gov_url,
            timeout=timeout,
            headers={"User-Agent": _UA_BROWSER},
            allow_redirects=True,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    m = re.search(r"([a-z0-9][a-z0-9-]*\.granicus\.com)", resp.text, re.IGNORECASE)
    if not m:
        return None
    host = m.group(1).lower()
    for vid in (1, 2, 3, 4, 5):
        rss_url = f"https://{host}/ViewPublisherRSS.php?view_id={vid}"
        try:
            rss_resp = requests.get(
                rss_url,
                timeout=timeout,
                headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml"},
            )
        except requests.RequestException:
            continue
        if rss_resp.status_code != 200:
            continue
        try:
            root = ET.fromstring(rss_resp.content)
        except ET.ParseError:
            continue
        if root.find(".//item") is not None:
            return (host, vid)
    return None


def check_civicengage_agendacenter_freshness(
    calendar_url: str,
    *,
    max_age_months: int = 6,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """F28 Phase 2 freshness probe for CivicEngage AgendaCenter HTML.

    CivicPlus CivicEngage (formerly the CivicPlus Agenda Center product
    line) renders Agenda Center pages server-side with meeting dates
    exposed via `aria-label="Agenda for <Month Day, Year>"` (and sibling
    "Minutes for", "Packet for", "Notes for") on each row's heading.

    The probe fetches the URL with a browser User-Agent (required —
    CivicEngage returns HTTP 403 to default bot UAs), regex-extracts all
    aria-label dates, picks the latest, classifies against the threshold.

    Returns the same dict schema as check_legistar_freshness, with
    freshness_status one of: fresh, stale_archive, empty_response,
    bad_jurisdiction (URL 404), probe_blocked (HTTP 4xx other than 404 —
    WAF), probe_error.

    Tested 2026-06-19 against Bisbee/Douglas/Safford. The aria-label
    convention is consistent across all three CivicEngage AgendaCenter
    deployments observed in AZ.
    """
    import re
    from datetime import datetime

    out = {
        "jurisdiction": None,
        "api_url": calendar_url,
        "http_status": None,
        "most_recent_event_date": None,
        "age_days": None,
        "is_fresh": None,
        "freshness_status": "probe_error",
        "error": "",
    }

    try:
        host = urlparse(calendar_url).netloc.lower()
        out["jurisdiction"] = host
    except Exception:
        pass

    try:
        resp = requests.get(
            calendar_url,
            timeout=timeout,
            headers={"User-Agent": _UA_BROWSER},
            allow_redirects=True,
        )
    except requests.RequestException as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out

    out["http_status"] = resp.status_code
    if resp.status_code == 404:
        out["freshness_status"] = "bad_jurisdiction"
        out["error"] = "HTTP 404 — calendar URL no longer exists"
        return out
    if 400 <= resp.status_code < 500:
        out["freshness_status"] = "probe_blocked"
        out["error"] = f"HTTP {resp.status_code} — likely WAF/auth block"
        return out
    if resp.status_code != 200:
        out["freshness_status"] = "probe_error"
        out["error"] = f"unexpected HTTP {resp.status_code}"
        return out

    matches = re.findall(
        r'aria-label="(?:Agenda|Minutes|Packet|Notes|Recording|Video) for '
        r'([A-Z][a-z]+ \d{1,2},?\s*\d{4})"',
        resp.text,
    )
    if not matches:
        out["freshness_status"] = "empty_response"
        out["error"] = "no aria-label dates found in AgendaCenter HTML"
        return out

    parsed_dates = []
    for m in matches:
        cleaned = m.strip().replace(",", "").replace("  ", " ")
        for fmt in ("%B %d %Y",):
            try:
                parsed_dates.append(datetime.strptime(cleaned, fmt))
                break
            except ValueError:
                continue
    if not parsed_dates:
        out["freshness_status"] = "probe_error"
        out["error"] = f"could not parse any of {len(matches)} aria-label dates"
        return out

    most_recent = max(parsed_dates)
    now = datetime.utcnow()
    age_days = (now - most_recent).days
    out["most_recent_event_date"] = most_recent.strftime("%Y-%m-%d")
    out["age_days"] = age_days
    max_age_days = max_age_months * 30
    out["is_fresh"] = age_days <= max_age_days
    out["freshness_status"] = "fresh" if out["is_fresh"] else "stale_archive"
    return out


def check_primegov_freshness(
    calendar_url: str,
    *,
    max_age_months: int = 6,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """F28 sibling freshness probe for PrimeGov (Granicus-acquired SPA portal).

    PrimeGov exposes a JSON-RPC-style public API at:
        https://{jurisdiction}.primegov.com/api/v2/PublicPortal/ListUpcomingMeetings

    Sibling endpoints (`ListArchivedMeetings`, `ListPastMeetings`, etc.) all
    return HTTP 404 — only ListUpcomingMeetings is publicly exposed at the
    Public Portal route. That means this probe can only verify the vendor's
    FORWARD activity (scheduled meetings), not its archive freshness.

    Response shape (verified 2026-06-22 against
    `prescottvalley.primegov.com/api/v2/PublicPortal/ListUpcomingMeetings`):

        [
          {
            "id": 1371,
            "dateTime": "2026-06-25T17:30:00",
            "title": "Town Council Regular Meeting",
            "documentList": [
              {"publishDate": "2026-06-19T23:04:18.723", ...},
              ...
            ]
          },
          ...
        ]

    Freshness signal: latest publishDate across all documentList entries.
    This is the most-recent-activity signal (when the city last published an
    agenda/packet for an upcoming meeting). If the list is non-empty but
    documents have no publishDates, fall back to the soonest upcoming
    dateTime — that's a scheduled-meeting signal of vendor activity.

    Honest limitation: a city that has NO upcoming meetings (council recess,
    pre-summer break, vendor abandonment) returns empty + freshness_status =
    `empty_response`. Without an archive endpoint, this probe cannot
    distinguish recess from abandonment; that's a Phase 2 enhancement
    requiring SPA introspection to find the archive route.

    URL acceptance: takes either the full PrimeGov public-portal URL
    (https://{j}.primegov.com/portal/...) OR an embed/iframe URL pointing
    at the portal. Subdomain is the jurisdiction key.

    Returns the same dict schema as check_legistar_freshness; freshness_status
    one of: fresh, stale_archive (unreachable for PrimeGov — see limitation
    above; reserved if archive endpoint is added later), empty_response,
    bad_jurisdiction, probe_error, needs_alternate_probe.
    """
    from datetime import datetime

    out = {
        "jurisdiction": None,
        "api_url": None,
        "http_status": None,
        "most_recent_event_date": None,
        "age_days": None,
        "is_fresh": None,
        "freshness_status": "probe_error",
        "error": "",
    }

    try:
        host = urlparse(calendar_url).netloc.lower()
    except Exception:
        host = ""
    if not host.endswith(".primegov.com"):
        out["freshness_status"] = "needs_alternate_probe"
        out["error"] = (
            "calendar_url is not at *.primegov.com — likely an iframe-embedded "
            "portal living on a .gov host; needs SPA introspection to locate "
            "the inner primegov subdomain (see discover_granicus_subdomain for "
            "the analog pattern)"
        )
        return out
    out["jurisdiction"] = host[: -len(".primegov.com")]
    api_url = f"https://{host}/api/v2/PublicPortal/ListUpcomingMeetings"
    out["api_url"] = api_url

    try:
        resp = requests.get(
            api_url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        out["http_status"] = resp.status_code
        if 400 <= resp.status_code < 500:
            out["freshness_status"] = "bad_jurisdiction"
            out["error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
            return out
        if resp.status_code != 200:
            out["freshness_status"] = "probe_error"
            out["error"] = f"unexpected HTTP {resp.status_code}"
            return out
        data = resp.json()
        if not isinstance(data, list):
            # PrimeGov returns {"message": "...", "messageDetail": "..."} on
            # endpoint-not-found AND 404 errors that bypass the 4xx branch
            # above (some PrimeGov deployments wrap 404s as 200+error-dict).
            out["freshness_status"] = "probe_error"
            if isinstance(data, dict) and "message" in data:
                out["error"] = f"PrimeGov error response: {data.get('message', '')[:200]}"
            else:
                out["error"] = f"unexpected response shape: {type(data).__name__}"
            return out
        if len(data) == 0:
            # No upcoming meetings scheduled. Could be a recess (fresh) or
            # vendor abandonment (stale). Can't disambiguate without an
            # archive endpoint.
            out["freshness_status"] = "empty_response"
            out["error"] = (
                "ListUpcomingMeetings returned empty; PrimeGov exposes no "
                "public archive endpoint, so recess vs. abandonment is "
                "indistinguishable from this probe alone"
            )
            return out

        # Scan all upcoming meetings' documentList for the most recent
        # publishDate (when the agenda was published — the actual vendor
        # activity signal). Fall back to the soonest upcoming dateTime if
        # documents have no publish dates.
        publish_dates = []
        upcoming_datetimes = []
        for meeting in data:
            if not isinstance(meeting, dict):
                continue
            dt_raw = meeting.get("dateTime")
            if dt_raw:
                try:
                    upcoming_datetimes.append(datetime.fromisoformat(dt_raw[:19]))
                except (ValueError, TypeError):
                    pass
            for doc in meeting.get("documentList") or []:
                if not isinstance(doc, dict):
                    continue
                pd_raw = doc.get("publishDate")
                if not pd_raw:
                    continue
                try:
                    publish_dates.append(datetime.fromisoformat(pd_raw[:19]))
                except (ValueError, TypeError):
                    pass

        now = datetime.utcnow()
        max_age_days = max_age_months * 30
        if publish_dates:
            most_recent = max(publish_dates)
            age_days = (now - most_recent).days
            out["most_recent_event_date"] = most_recent.strftime("%Y-%m-%d")
            out["age_days"] = age_days
            out["is_fresh"] = age_days <= max_age_days
            out["freshness_status"] = "fresh" if out["is_fresh"] else "stale_archive"
        elif upcoming_datetimes:
            # No published documents yet, but meetings are scheduled — vendor
            # is forward-active. Report the soonest upcoming as the signal;
            # age_days will be negative (future).
            soonest = min(upcoming_datetimes)
            age_days = (now - soonest).days  # negative for future
            out["most_recent_event_date"] = soonest.strftime("%Y-%m-%d")
            out["age_days"] = age_days
            out["is_fresh"] = True  # any upcoming meeting = active vendor
            out["freshness_status"] = "fresh"
        else:
            out["freshness_status"] = "empty_response"
            out["error"] = "upcoming meetings returned but no dates parseable"
        return out
    except requests.RequestException as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out


def fingerprint_vendor(
    url: str,
    html: Optional[str] = None,
    *,
    fetch: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
    verify_liveness: bool = True,
) -> dict:
    """Identify the civic-software vendor for a URL.

    Args:
        url: The URL to fingerprint. Calendar URL is preferred but home page
            also works (since vendor signatures show up site-wide).
        html: Pre-fetched HTML, optional. If provided, no HTTP GET happens.
        fetch: If True and no HTML provided and URL rules don't match, do
            a one-shot GET on the URL and apply HTML rules. Set False for
            offline / batch / test mode.
        timeout: HTTP timeout in seconds.
        verify_liveness: If True (default) and ``fetch`` is True, do a HEAD
            probe on high-confidence URL-subdomain matches before returning
            them. Guards against legacy/dead vendor subdomains that still
            URL-pattern-match but no longer serve calendar data (the
            Prescott Valley round-3 finding: pvaz.granicus.com matches
            granicus regex with confidence=high but HEAD returns 404 because
            the city migrated to PrimeGov). When the HEAD probe fails (4xx /
            5xx / connection error), the vendor is converted to `unknown`
            with a `stale_subdomain_hint` field preserving the original
            match for diagnostic purposes — caller routes to LLM inspection.

    Returns:
        Result dict with keys:
            vendor (str): one of VENDORS — `unknown` if nothing matched OR
                if the liveness check failed.
            confidence (str): `high` | `medium` | `none` (unknown).
            method (str): how the match was made
                (`url_subdomain` | `url_path` | `html_asset` | `html_meta`
                 | `none`).
            evidence (str): short description of what matched.
            description (str): longer description suitable for logs.
            url_checked (str): the URL passed in.
            fetch_error (str, optional): present only if an HTTP fetch was
                attempted and failed.
            http_status (int, optional): HTTP status code if a fetch happened.
            liveness_status (int, optional): present if a HEAD probe ran
                (high-confidence URL-subdomain match path).
            stale_subdomain_hint (str, optional): present when the liveness
                check downgraded a URL-subdomain match — names the vendor
                the subdomain matched against, for the LLM-fallback caller
                to surface in research_notes.
            stale_subdomain_status (int|None, optional): the HEAD status
                code that triggered the downgrade.
            match_excerpt (str, optional): for HTML matches, a short excerpt
                of the matched text.
    """
    # Pass 1: URL-only.
    hit = fingerprint_url(url)
    if hit is not None:
        # NEW (2026-06-17): liveness check for high-confidence URL-subdomain
        # matches. Round-3 Prescott Valley surfaced that dead vendor subdomains
        # still URL-pattern-match and silently propagate as confidence=high
        # classifications. HEAD-probe before returning; on 4xx/5xx, convert
        # to unknown + preserve the diagnostic hint for the LLM caller.
        if hit.get("confidence") == "high" and verify_liveness and fetch:
            liveness = _check_liveness(url, timeout=timeout)
            if liveness.get("alive"):
                hit["liveness_status"] = liveness.get("status")
                return hit
            # Dead URL — convert to unknown + preserve the vendor hint.
            return {
                "vendor": "unknown",
                "confidence": "none",
                "method": "none",
                "evidence": "",
                "description": (
                    "URL subdomain matched " + hit["vendor"]
                    + " pattern but HEAD probe returned "
                    + (str(liveness.get("status")) if liveness.get("status") is not None else "no response (" + liveness.get("error", "") + ")")
                    + " — likely platform migration. Route to LLM inspection; "
                    + "the city may now use a different vendor. Original subdomain match preserved as `stale_subdomain_hint`."
                ),
                "url_checked": url,
                "stale_subdomain_hint": hit["vendor"],
                "stale_subdomain_status": liveness.get("status"),
                "stale_subdomain_error": liveness.get("error", ""),
            }
        return hit

    # Pass 2: HTML — either pre-supplied or one-shot fetched.
    if html is None and fetch:
        try:
            resp = requests.get(
                url,
                timeout=timeout,
                headers={"User-Agent": USER_AGENT},
                allow_redirects=True,
            )
            html = resp.text
            http_status = resp.status_code
        except requests.RequestException as e:
            return {
                "vendor": "unknown",
                "confidence": "none",
                "method": "none",
                "evidence": "",
                "description": "URL rules did not match; HTTP fetch failed",
                "url_checked": url,
                "fetch_error": f"{type(e).__name__}: {e}",
            }
        hit = fingerprint_html(url, html or "")
        if hit is not None:
            hit["http_status"] = http_status
            return hit
        return {
            "vendor": "unknown",
            "confidence": "none",
            "method": "none",
            "evidence": "",
            "description": (
                "URL rules did not match; HTML rules did not match either "
                "— route to Sonnet recon"
            ),
            "url_checked": url,
            "http_status": http_status,
        }

    if html is not None:
        hit = fingerprint_html(url, html)
        if hit is not None:
            return hit

    return {
        "vendor": "unknown",
        "confidence": "none",
        "method": "none",
        "evidence": "",
        "description": (
            "URL rules did not match" + ("; no HTML supplied" if html is None else "; HTML rules did not match either")
        ),
        "url_checked": url,
    }


# ---------------------------------------------------------------------------
# Batch mode (validate against parser_index)
# ---------------------------------------------------------------------------


def fingerprint_parser_index(
    parser_index_path: Path,
    *,
    fetch: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Run the fingerprinter against every city in parser_index.json.

    Validation tool: compares the deterministic fingerprint against the
    LLM-written `calendar_format` field. Mismatches surface either (a) a
    rule gap in the fingerprinter or (b) a stale parser_index classification.

    Default `fetch=False` keeps it offline + fast for routine sanity checks;
    pass `fetch=True` for the full validation pass that also hits HTML rules.
    """
    with parser_index_path.open() as f:
        idx = json.load(f)

    results = []
    by_vendor: Counter[str] = Counter()
    by_method: Counter[str] = Counter()
    fetch_errors = 0

    for city, meta in sorted(idx.items()):
        cal_url = meta.get("calendar_url") or ""
        cal_format = meta.get("calendar_format") or ""
        if not cal_url:
            continue
        result = fingerprint_vendor(cal_url, fetch=fetch, timeout=timeout)
        results.append(
            {
                "city": city,
                "calendar_url": cal_url,
                "calendar_format_in_index": cal_format,
                **result,
            }
        )
        by_vendor[result["vendor"]] += 1
        by_method[result["method"]] += 1
        if result.get("fetch_error"):
            fetch_errors += 1

    return {
        "total": len(results),
        "fetched": fetch,
        "by_vendor": dict(by_vendor.most_common()),
        "by_method": dict(by_method.most_common()),
        "fetch_errors": fetch_errors,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_excerpt(s: str, start: int, end: int, ctx: int = 30) -> str:
    """Return a short excerpt of `s` around the [start:end] window."""
    a = max(0, start - ctx)
    b = min(len(s), end + ctx)
    snippet = s[a:b]
    snippet = re.sub(r"\s+", " ", snippet).strip()
    if len(snippet) > 160:
        snippet = snippet[:160] + "…"
    return snippet


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Deterministic civic-vendor fingerprinter (Recon-2).",
    )
    p.add_argument(
        "--url",
        help="Single URL to fingerprint. Mutually exclusive with --batch.",
    )
    p.add_argument(
        "--batch",
        action="store_true",
        help="Walk every city in parser_index.json and report coverage stats.",
    )
    p.add_argument(
        "--parser-index",
        type=Path,
        default=DEFAULT_PARSER_INDEX,
        help=f"Path to parser_index.json (default: {DEFAULT_PARSER_INDEX}).",
    )
    p.add_argument(
        "--no-fetch",
        action="store_true",
        help="Don't do an HTTP GET; URL-only mode (fast, lower coverage).",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT}).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human-readable summary.",
    )
    args = p.parse_args(argv)

    if args.url and args.batch:
        sys.exit("--url and --batch are mutually exclusive.")
    if not args.url and not args.batch:
        sys.exit("Need either --url or --batch.")

    fetch = not args.no_fetch

    if args.url:
        result = fingerprint_vendor(args.url, fetch=fetch, timeout=args.timeout)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"URL:        {args.url}")
            print(f"Vendor:     {result['vendor']}")
            print(f"Confidence: {result['confidence']}")
            print(f"Method:     {result['method']}")
            print(f"Evidence:   {result['evidence']}")
            if "fetch_error" in result:
                print(f"Fetch error: {result['fetch_error']}")
            if "http_status" in result:
                print(f"HTTP status: {result['http_status']}")
        return 0

    # Batch mode.
    summary = fingerprint_parser_index(
        args.parser_index, fetch=fetch, timeout=args.timeout
    )
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"parser_index.json cities probed: {summary['total']}")
        print(f"HTTP fetches enabled: {summary['fetched']}")
        print(f"Fetch errors: {summary['fetch_errors']}")
        print()
        print("By vendor:")
        for v, n in summary["by_vendor"].items():
            print(f"  {v}: {n}")
        print()
        print("By method:")
        for m, n in summary["by_method"].items():
            print(f"  {m}: {n}")
        print()
        unknowns = [r for r in summary["results"] if r["vendor"] == "unknown"]
        if unknowns:
            print(f"Unknown ({len(unknowns)}) — would route to Sonnet:")
            for r in unknowns[:20]:
                print(
                    f"  {r['city']:22s}  "
                    f"format-in-index={r['calendar_format_in_index'][:30]!r}  "
                    f"url={r['calendar_url'][:60]}"
                )
            if len(unknowns) > 20:
                print(f"  …and {len(unknowns) - 20} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
