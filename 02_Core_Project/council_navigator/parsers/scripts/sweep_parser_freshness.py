"""
sweep_parser_freshness.py — F28 sweep across parser_index vendor classes.

Originally V0 2026-06-19 (Legistar only, per chunk-8 Goodyear). Extended
2026-06-19 with Granicus + CivicClerk sibling probes. Emits a structured
report identifying:

  - `stale_archive` cities  — the F28 failure class. API returns 200 +
    structurally-valid data, but most-recent event is past the
    max_age_months threshold. These are silently emitting stale or empty
    parser data and need operator-side intervention (re-recon to find the
    current substrate, or removal from parser_index).

  - `bad_jurisdiction` cities — the API returns a 4xx, meaning the
    parser_index calendar URL points at a vendor subdomain that the
    vendor itself doesn't recognize. Likely the city was never on that
    vendor or migrated off so completely the subdomain was reclaimed.

  - `empty_response` cities — HTTP 200 with zero events. Could be
    legitimate (council off-season) or stale-archive of a different
    shape (jurisdiction renamed inside the vendor namespace).

  - `needs_alternate_probe` cities — calendar_url is at a city-owned
    domain rather than the vendor's subdomain, so this probe can't reach
    the vendor's freshness API. Examples: Granicus OpenCities + Swagit
    admin layered on *.gov; CivicClerk AgendaCenter / Calendar.aspx
    layered on *.gov. These need a different probe shape (Phase 2).

  - `fresh` cities — currently-active vendor instances. Good.

  - `probe_error` cities — request raised an exception OR the response
    didn't parse cleanly. Worth a manual look.

Usage:

    .venv-worker/bin/python3.11 \\  # from the repo root
        02_Core_Project/council_navigator/parsers/scripts/sweep_parser_freshness.py

    # JSON over all three vendors (default)
    ./sweep_parser_freshness.py

    # Human-readable table
    ./sweep_parser_freshness.py --format=table

    # One vendor only
    ./sweep_parser_freshness.py --vendor=granicus
    ./sweep_parser_freshness.py --vendor=civicclerk
    ./sweep_parser_freshness.py --vendor=legistar

PrimeGov sibling shipped 2026-06-22. parser_index has zero PrimeGov
production entries today (the vendor enum surfaced in AZ recon round-3
with Prescott Valley — pvaz Granicus subdomain is dead + PrimeGov is the
live platform — but no parser_index row exists yet). The probe is
verified against Prescott Valley as the canonical PrimeGov instance and
returns `fresh` when ListUpcomingMeetings carries publishDate signals.
See `check_primegov_freshness` for the V0 limitations (no archive
endpoint exposed; can't disambiguate recess from abandonment).

DOES NOT mutate parser_index.json — operator decides remediation per-city.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

# Make sibling vendor_fingerprint importable when run as a script.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from vendor_fingerprint import (  # noqa: E402
    check_civicclerk_freshness,
    check_civicengage_agendacenter_freshness,
    check_granicus_freshness,
    check_legistar_freshness,
    check_primegov_freshness,
    discover_granicus_subdomain,
)


def _legistar_jurisdiction_from_url(url: str) -> str | None:
    """Extract the Legistar jurisdiction slug from a calendar URL.

    Examples:
      phoenix.legistar.com/Calendar.aspx              → 'phoenix'
      glendale-az.legistar.com/Calendar.aspx          → 'glendale-az'
      lakehavasucity.legistar.com/Feed.ashx?M=...     → 'lakehavasucity'

    Returns None if the URL doesn't look like a Legistar subdomain.
    """
    if not url:
        return None
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return None
    if not host.endswith(".legistar.com"):
        return None
    return host[:-len(".legistar.com")]


def sweep_legistar(parser_index_path: Path) -> list[dict]:
    """Sweep all Legistar-classified cities in parser_index.json.

    Returns a list of result dicts, one per Legistar city, each carrying:
      vendor: 'legistar'
      city: parser_index key
      calendar_format: the parser_index calendar_format value (raw)
      calendar_url: the parser_index calendar_url
      jurisdiction: derived Legistar subdomain
      ... + all fields from check_legistar_freshness
    """
    with parser_index_path.open() as f:
        idx = json.load(f)

    results = []
    for city, info in idx.items():
        fmt = (info.get("calendar_format") or "").lower()
        if "legistar" not in fmt:
            continue
        cal_url = info.get("calendar_url") or ""
        jurisdiction = _legistar_jurisdiction_from_url(cal_url)
        if not jurisdiction:
            results.append({
                "vendor": "legistar",
                "city": city,
                "calendar_format": fmt,
                "calendar_url": cal_url,
                "jurisdiction": None,
                "freshness_status": "non_legistar_subdomain",
                "error": "calendar_url does not point at *.legistar.com",
            })
            continue
        probe = check_legistar_freshness(jurisdiction)
        probe["vendor"] = "legistar"
        probe["city"] = city
        probe["calendar_format"] = fmt
        probe["calendar_url"] = cal_url
        results.append(probe)
    return results


def sweep_granicus(parser_index_path: Path) -> list[dict]:
    """Sweep all Granicus-classified cities in parser_index.json.

    Matches any calendar_format containing 'granicus' (covers `granicus`,
    `granicus rss`, `granicus (rss feed)`, `granicus opencities`,
    `swagit/granicus (swagitadmin)`).

    Phase 1 path: URLs at *.granicus.com use check_granicus_freshness directly.

    Phase 2 path (added 2026-06-19): non-*.granicus.com URLs (the OpenCities /
    Swagit-admin Granicus-on-.gov shape) try discover_granicus_subdomain
    first — scan the .gov HTML for a `*.granicus.com` reference + probe
    common view_ids. If found, run the normal Granicus probe against the
    discovered URL. If not found (WAF-blocked .gov, no granicus reference
    in HTML), return needs_alternate_probe with a sub-reason.
    """
    with parser_index_path.open() as f:
        idx = json.load(f)

    results = []
    for city, info in idx.items():
        fmt = (info.get("calendar_format") or "").lower()
        if "granicus" not in fmt:
            continue
        cal_url = info.get("calendar_url") or ""

        is_direct = ".granicus.com" in urlparse(cal_url).netloc.lower()
        if is_direct:
            probe = check_granicus_freshness(cal_url)
        else:
            discovered = discover_granicus_subdomain(cal_url)
            if discovered is not None:
                host, vid = discovered
                discovery_url = f"https://{host}/ViewPublisherRSS.php?view_id={vid}"
                probe = check_granicus_freshness(discovery_url)
                probe["phase2_discovered_url"] = discovery_url
            else:
                probe = {
                    "jurisdiction": None,
                    "api_url": None,
                    "http_status": None,
                    "most_recent_event_date": None,
                    "age_days": None,
                    "is_fresh": None,
                    "freshness_status": "needs_alternate_probe",
                    "error": (
                        "Granicus-on-.gov shell URL; .gov fetch failed or "
                        "no *.granicus.com reference in HTML (likely WAF-blocked)"
                    ),
                }

        probe["vendor"] = "granicus"
        probe["city"] = city
        probe["calendar_format"] = fmt
        probe["calendar_url"] = cal_url
        results.append(probe)
    return results


def sweep_civicclerk(parser_index_path: Path) -> list[dict]:
    """Sweep all CivicClerk-classified cities in parser_index.json.

    Matches calendar_format containing 'civicclerk' (covers `civicclerk`,
    `civicclerk (custom html)`, `civicclerk (custom html/selenium)`,
    `civicclerk (api)`, `civicplus/civicclerk (agenda center)`).

    URL-pattern dispatch (added 2026-06-19 — Phase 2):
      *.portal.civicclerk.com → check_civicclerk_freshness (OData /Events)
      */agendacenter or */AgendaCenter → check_civicengage_agendacenter_freshness
          (CivicPlus CivicEngage HTML scrape via aria-label dates)
      */Calendar.aspx?CID=N → needs_alternate_probe_js (JS-rendered;
          static HTML carries no dates; Phase 3 browser-render work)
      Other → try Granicus discovery as last fallback (Eloy-shape — parser_index
          calls it CivicClerk but underlying surface is Granicus). If
          discovery finds a *.granicus.com reference, run Granicus probe;
          otherwise needs_alternate_probe.
    """
    with parser_index_path.open() as f:
        idx = json.load(f)

    results = []
    for city, info in idx.items():
        fmt = (info.get("calendar_format") or "").lower()
        if "civicclerk" not in fmt:
            continue
        cal_url = info.get("calendar_url") or ""
        cal_url_lower = cal_url.lower()
        parsed = urlparse(cal_url)
        host = parsed.netloc.lower()

        if host.endswith(".portal.civicclerk.com"):
            probe = check_civicclerk_freshness(cal_url)
            probe["vendor"] = "civicclerk"
        elif "/agendacenter" in cal_url_lower:
            probe = check_civicengage_agendacenter_freshness(cal_url)
            probe["vendor"] = "civicengage_agendacenter"
        elif "calendar.aspx" in cal_url_lower:
            probe = {
                "jurisdiction": None,
                "api_url": cal_url,
                "http_status": None,
                "most_recent_event_date": None,
                "age_days": None,
                "is_fresh": None,
                "freshness_status": "needs_alternate_probe_js",
                "error": (
                    "CivicEngage Calendar.aspx pattern — meeting dates "
                    "rendered by JavaScript; static HTML carries no dates. "
                    "Phase 3 browser-render work to probe."
                ),
            }
            probe["vendor"] = "civicengage_calendar"
        else:
            # Last-fallback Granicus discovery (Eloy-shape: parser_index
            # classifies as CivicClerk but the .gov page references a
            # *.granicus.com subdomain that actually hosts the data).
            discovered = discover_granicus_subdomain(cal_url)
            if discovered is not None:
                host, vid = discovered
                discovery_url = f"https://{host}/ViewPublisherRSS.php?view_id={vid}"
                probe = check_granicus_freshness(discovery_url)
                probe["phase2_discovered_url"] = discovery_url
                probe["vendor"] = "granicus"
                probe["phase2_misclassified_as"] = "civicclerk"
            else:
                probe = {
                    "jurisdiction": None,
                    "api_url": None,
                    "http_status": None,
                    "most_recent_event_date": None,
                    "age_days": None,
                    "is_fresh": None,
                    "freshness_status": "needs_alternate_probe",
                    "error": (
                        "Non-portal civicclerk classification with no recognized "
                        "URL pattern (AgendaCenter / Calendar.aspx / Granicus "
                        "discovery all fell through)"
                    ),
                }
                probe["vendor"] = "civicclerk"

        probe["city"] = city
        probe["calendar_format"] = fmt
        probe["calendar_url"] = cal_url
        results.append(probe)
    return results


def sweep_primegov(parser_index_path: Path) -> list[dict]:
    """Sweep all PrimeGov-classified cities in parser_index.json.

    Matches calendar_format containing 'primegov'. Phase 1 path: URLs at
    *.primegov.com use check_primegov_freshness directly. Phase 2 path
    (parser_index rows where calendar_url is the .gov iframe parent rather
    than the inner .primegov.com subdomain) returns needs_alternate_probe
    pending SPA introspection to locate the embedded portal.

    PrimeGov shipped 2026-06-22 with V0 limitations documented in
    check_primegov_freshness: ListUpcomingMeetings is the only public
    endpoint, so freshness reflects scheduled meetings (forward-active),
    not archive completeness.
    """
    with parser_index_path.open() as f:
        idx = json.load(f)

    results = []
    for city, info in idx.items():
        fmt = (info.get("calendar_format") or "").lower()
        if "primegov" not in fmt:
            continue
        cal_url = info.get("calendar_url") or ""
        host = urlparse(cal_url).netloc.lower()
        if host.endswith(".primegov.com"):
            probe = check_primegov_freshness(cal_url)
        else:
            probe = {
                "jurisdiction": None,
                "api_url": None,
                "http_status": None,
                "most_recent_event_date": None,
                "age_days": None,
                "is_fresh": None,
                "freshness_status": "needs_alternate_probe",
                "error": (
                    "PrimeGov classification on non-*.primegov.com host (likely "
                    "iframe-embedded portal on a .gov domain); needs SPA "
                    "introspection to locate the inner primegov subdomain"
                ),
            }
        probe["vendor"] = "primegov"
        probe["city"] = city
        probe["calendar_format"] = fmt
        probe["calendar_url"] = cal_url
        results.append(probe)
    return results


_VENDOR_SWEEPERS = {
    "legistar": sweep_legistar,
    "granicus": sweep_granicus,
    "civicclerk": sweep_civicclerk,
    "primegov": sweep_primegov,
}


def sweep_all(parser_index_path: Path) -> list[dict]:
    """Run all available vendor sweeps and return the concatenated result."""
    out = []
    for vendor, sweeper in _VENDOR_SWEEPERS.items():
        out.extend(sweeper(parser_index_path))
    return out


def render_table(results: list[dict]) -> str:
    rows = []
    rows.append(
        f"{'Vendor':<11}  {'City':<24}  {'Jurisdiction':<22}  "
        f"{'Status':<22}  {'Most Recent':<12}  {'Age (days)':>10}"
    )
    rows.append("-" * 113)
    # Sort: stale first, then bad_jurisdiction, then empty, then probe_error,
    # then probe_blocked / needs_alternate_probe variants, then fresh
    order = {
        "stale_archive": 0,
        "bad_jurisdiction": 1,
        "empty_response": 2,
        "probe_error": 3,
        "probe_blocked": 4,
        "needs_alternate_probe": 5,
        "needs_alternate_probe_js": 5,
        "non_legistar_subdomain": 6,
        "fresh": 7,
    }
    for r in sorted(
        results,
        key=lambda r: (order.get(r.get("freshness_status"), 99), r.get("vendor", ""), r.get("city", "")),
    ):
        rows.append(
            f"{r.get('vendor', '?'):<11}  "
            f"{r.get('city', '?'):<24}  "
            f"{(r.get('jurisdiction') or '-'):<22}  "
            f"{r.get('freshness_status', '?'):<22}  "
            f"{(r.get('most_recent_event_date') or '-'):<12}  "
            f"{(r.get('age_days') if r.get('age_days') is not None else '-'):>10}"
        )
    return "\n".join(rows)


def render_summary(results: list[dict]) -> str:
    """Per-vendor counts of each freshness status — quick scan of the landscape."""
    from collections import Counter
    out = []
    by_vendor: dict[str, list[dict]] = {}
    for r in results:
        by_vendor.setdefault(r.get("vendor", "?"), []).append(r)
    for vendor in sorted(by_vendor):
        rows = by_vendor[vendor]
        counts = Counter(r.get("freshness_status", "?") for r in rows)
        parts = [f"{status}={n}" for status, n in counts.most_common()]
        out.append(f"  {vendor:<12} ({len(rows)} parsers): " + ", ".join(parts))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="F28 freshness sweep of parser_index.")
    ap.add_argument(
        "--parser-index",
        default=str(_HERE.parent / "parser_index.json"),
        help="Path to parser_index.json.",
    )
    ap.add_argument(
        "--vendor",
        choices=sorted(_VENDOR_SWEEPERS) + ["all"],
        default="all",
        help="Restrict the sweep to one vendor (default: all).",
    )
    ap.add_argument("--format", choices=["json", "table", "summary"], default="json")
    args = ap.parse_args()

    parser_index_path = Path(args.parser_index)
    if args.vendor == "all":
        results = sweep_all(parser_index_path)
    else:
        results = _VENDOR_SWEEPERS[args.vendor](parser_index_path)

    if args.format == "table":
        print(render_table(results))
    elif args.format == "summary":
        print(render_summary(results))
    else:
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
