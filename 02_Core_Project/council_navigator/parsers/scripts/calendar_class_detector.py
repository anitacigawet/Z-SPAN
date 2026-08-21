"""S-036 V1-complete — deterministic calendar-class pre-classifier.

Runs BEFORE any Haiku invocation to determine the extraction strategy needed
for a city's calendar URL. The classification is a cheap, pure-HTML check:

  Class A — server-rendered default view; WebFetch + Haiku extraction works.
  Class B — postback-gated, empty default view; needs a headless browser
            (Chrome MCP per [[browser-chrome-not-edge]] — never Edge) before
            Haiku can see anything to extract.

The Legistar A/B split was identified empirically in the 2026-06-14 Maricopa
re-recon (see `parsers/RECON_FINDINGS_2026-06-14_maricopa.md`). The signal
combination that's reliably diagnostic of Class B:

  1. "No records were found" (or equivalent empty-state text) in the page
  2. Empty `<tbody>` — no meeting rows in the calendar table on static load
  3. ASP.NET WebForms postback controls (`__doPostBack`, `__VIEWSTATE`)

All three together = Class B with high confidence. ASP.NET controls present
plus a populated tbody = Class A (the typical Legistar shape). Anything else
returns `unknown` rather than risking a false dispatch.

Architecturally: this classifier is vendor-keyed at the entry point so future
Granicus / CivicPlus / generic-HTML classifiers slot in next to the Legistar
function without renames. The per-run signals dict is the seed of the V3
auto-classifier training data — every classification is a labeled example.

Per S-036 § V1-complete remaining work, item 1. No LLM spend; pure HTML/regex.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, Literal, Optional

import requests
from bs4 import BeautifulSoup


logger = logging.getLogger(__name__)


CalendarClass = Literal["class_a", "class_b", "unknown"]


# Class-B signals (from RECON_FINDINGS_2026-06-14_maricopa.md, Glendale canary).
_NO_RECORDS_PATTERNS = (
    re.compile(r"no records were found", re.IGNORECASE),
    re.compile(r"no\s+items?\s+found", re.IGNORECASE),
)

_POSTBACK_DOPOSTBACK = re.compile(r"__doPostBack", re.IGNORECASE)
_POSTBACK_VIEWSTATE = re.compile(r"__VIEWSTATE", re.IGNORECASE)

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; ZSPAN-calendar-classifier/1.0; "
    "+civic transparency tooling)"
)
_DEFAULT_FETCH_TIMEOUT_S = 30
_POPULATED_TBODY_MIN_ROWS = 3

# F-4 (2026-06-14): years older than this offset from the threshold year
# trigger the "archive-only candidate" flag. Glendale's Legistar shows
# only 2016-2017 content (newest meeting 2017-06-27); the city migrated
# off the platform years ago. Routine extraction of these pages would
# waste Haiku quota on archival content with no production value.
ARCHIVE_AGE_THRESHOLD_YEARS = 2

# M/D/YYYY pattern Legistar uses for meeting-date cells. The year is the
# captured group; bounds (1990 < year < 2100) prevent matching IDs,
# building numbers, or agenda-item codes that look like 4-digit years.
_LEGISTAR_DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}/(\d{4})\b")


@dataclass
class ClassificationResult:
    """The output of a single classification run.

    `signals` carries the per-check booleans so downstream tooling (the V3
    auto-classifier dataset; the operator dashboard if added later) can train
    on or audit the raw evidence, not just the verdict.

    `latest_meeting_year` is the max year extracted from meeting-row dates in
    the rgMasterTable (post-F-4). When set AND below the operator's threshold,
    the page is an "archive-only candidate" — the city likely migrated off
    the vendor's platform and routine LLM extraction would waste quota on
    historical content. The classifier surfaces the signal; callers (wrapper,
    V3) decide what to do with it.
    """
    calendar_class: CalendarClass
    signals: Dict[str, object] = field(default_factory=dict)
    reasoning: str = ""
    vendor: str = "legistar"
    latest_meeting_year: Optional[int] = None
    archive_only_candidate: bool = False


def fetch_html(url: str, timeout_s: int = _DEFAULT_FETCH_TIMEOUT_S) -> Optional[str]:
    """Fetch a calendar page's HTML. Returns None on any HTTP failure.

    Uses a polite User-Agent that identifies the project. No retry — the
    classifier is the cheap pre-flight; the caller decides what to do on
    fetch failure (typically: log + skip + move on to the next city).
    """
    try:
        resp = requests.get(
            url,
            timeout=timeout_s,
            headers={"User-Agent": _DEFAULT_USER_AGENT},
        )
    except requests.RequestException as exc:
        logger.warning("calendar_class_detector: fetch failed for %s: %s", url, exc)
        return None
    if resp.status_code != 200:
        logger.warning(
            "calendar_class_detector: %s returned HTTP %d", url, resp.status_code
        )
        return None
    return resp.text


def _has_no_records_text(html: str) -> bool:
    return any(p.search(html) for p in _NO_RECORDS_PATTERNS)


def _has_postback_controls(html: str) -> bool:
    """Both __doPostBack AND __VIEWSTATE present — the typical ASP.NET WebForms shape."""
    return bool(_POSTBACK_DOPOSTBACK.search(html)) and bool(_POSTBACK_VIEWSTATE.search(html))


def _extract_latest_meeting_year(html: str) -> Optional[int]:
    """Find the max year in M/D/YYYY date cells inside `<table class="rgMasterTable">`.

    Returns None when no rgMasterTable is present or no date-shaped strings
    are found. Bounds the year to 1990-2099 so building numbers, agenda IDs,
    or other 4-digit tokens that happen to look like years can't be picked
    up accidentally. The Legistar rgMasterTable scoping further isolates the
    search to actual meeting rows (date pickers and JS strings elsewhere on
    the page are ignored).
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        logger.warning("calendar_class_detector: BeautifulSoup parse failed: %s", exc)
        return None

    years: list = []
    for master in soup.find_all("table", class_="rgMasterTable"):
        for match in _LEGISTAR_DATE_RE.finditer(master.get_text(" ", strip=True)):
            year = int(match.group(1))
            if 1990 <= year <= 2099:
                years.append(year)
    return max(years) if years else None


def _has_populated_calendar_table(html: str) -> bool:
    """True if the page has a populated meeting list, not just incidental tbodies.

    Legistar uses Telerik RadGrid for the calendar; the meeting list lives in a
    `<table class="rgMasterTable">`. When present, only that table's tbody
    rows are counted — and rows whose text matches the empty-state patterns
    ("No records were found", etc.) are explicitly excluded so Glendale's
    single empty-state row doesn't masquerade as a populated calendar.

    Fallback (no `rgMasterTable` found): use a generic "any tbody with N+
    populated rows" heuristic so the function still works on synthetic test
    HTML and on future non-Telerik vendor pages.

    Calibrated against the 2026-06-14 Maricopa recon: Phoenix 10 rows + Mesa
    14 rows in `rgMasterTable` (Class A); Glendale 1 row that's the empty-
    state text (Class B).
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        logger.warning("calendar_class_detector: BeautifulSoup parse failed: %s", exc)
        return False

    master_tables = soup.find_all("table", class_="rgMasterTable")
    if master_tables:
        for table in master_tables:
            for tbody in table.find_all("tbody"):
                meeting_rows = [
                    row for row in tbody.find_all("tr")
                    if row.get_text(strip=True)
                    and not _has_no_records_text(row.get_text(strip=True))
                ]
                if meeting_rows:
                    return True
        return False

    for tbody in soup.find_all("tbody"):
        content_rows = [
            row for row in tbody.find_all("tr") if row.get_text(strip=True)
        ]
        if len(content_rows) >= _POPULATED_TBODY_MIN_ROWS:
            return True
    return False


def detect_calendar_class(
    url_or_html: str,
    *,
    is_html: bool = False,
    vendor: str = "legistar",
    timeout_s: int = _DEFAULT_FETCH_TIMEOUT_S,
    archive_threshold_year: Optional[int] = None,
) -> ClassificationResult:
    """Classify a calendar page into the extraction class the Haiku scraper needs.

    Args:
        url_or_html: Calendar URL (default) OR raw HTML (when `is_html=True`).
        is_html: If True, treat the first argument as already-fetched HTML.
        vendor: The calendar vendor. Currently only "legistar" is implemented;
            other vendors return `class_a` as a permissive default with an
            "unknown vendor" reasoning string (the existing Class-A path is
            the safer dispatch when we have no signals).
        timeout_s: HTTP fetch timeout in seconds; ignored when `is_html=True`.

    Returns:
        ClassificationResult with the verdict + signals dict + human-readable
        reasoning string. Reasoning is intentionally plain-language so it
        renders directly in operator UIs without translation (D-054).
    """
    if vendor != "legistar":
        return ClassificationResult(
            calendar_class="unknown",
            signals={"vendor_supported": False},
            reasoning=(
                f"classifier currently only handles vendor='legistar'; "
                f"got vendor={vendor!r}. Returning unknown so the caller "
                f"decides the dispatch."
            ),
            vendor=vendor,
        )

    if is_html:
        html: Optional[str] = url_or_html
    else:
        html = fetch_html(url_or_html, timeout_s=timeout_s)
        if html is None:
            return ClassificationResult(
                calendar_class="unknown",
                signals={"fetch_failed": True},
                reasoning=(
                    f"could not fetch {url_or_html} (network error or non-200 "
                    f"response); classification skipped"
                ),
                vendor=vendor,
            )

    no_records = _has_no_records_text(html)
    postback_controls = _has_postback_controls(html)
    populated_calendar_table = _has_populated_calendar_table(html)
    latest_meeting_year = _extract_latest_meeting_year(html)

    # F-4 archive-only candidate: max year present in rgMasterTable is older
    # than the operator's threshold (default: classifier doesn't apply the
    # check unless the caller supplies a threshold). When triggered, the
    # caller decides whether to skip extraction or just log + proceed.
    archive_only_candidate = (
        latest_meeting_year is not None
        and archive_threshold_year is not None
        and latest_meeting_year < archive_threshold_year
    )

    signals: Dict[str, object] = {
        "no_records_text": no_records,
        "postback_controls": postback_controls,
        "populated_calendar_table": populated_calendar_table,
        "latest_meeting_year": latest_meeting_year,
        "archive_only_candidate": archive_only_candidate,
    }

    archive_suffix = (
        f" — ARCHIVE-ONLY CANDIDATE (newest meeting {latest_meeting_year} "
        f"is older than threshold {archive_threshold_year}); city likely "
        f"migrated off Legistar, routine extraction would waste quota"
        if archive_only_candidate else ""
    )

    # Class B: all three Class-B signals present together.
    if no_records and postback_controls and not populated_calendar_table:
        return ClassificationResult(
            calendar_class="class_b",
            signals=signals,
            reasoning=(
                "empty calendar tbody + 'no records were found' text + "
                "ASP.NET postback controls — Class B (postback-gated; "
                "needs Chrome MCP to render before Haiku can extract)"
                + archive_suffix
            ),
            vendor=vendor,
            latest_meeting_year=latest_meeting_year,
            archive_only_candidate=archive_only_candidate,
        )

    # Class A: postback controls (Legistar's ASP.NET shape) + populated calendar table.
    if postback_controls and populated_calendar_table:
        return ClassificationResult(
            calendar_class="class_a",
            signals=signals,
            reasoning=(
                "ASP.NET postback controls + populated calendar tbody — "
                "Class A (server-rendered; WebFetch + Haiku will work)"
                + archive_suffix
            ),
            vendor=vendor,
            latest_meeting_year=latest_meeting_year,
            archive_only_candidate=archive_only_candidate,
        )

    # Anything else: don't false-dispatch. The wrapper's caller can decide
    # whether to try the existing Class-A path opportunistically or escalate.
    return ClassificationResult(
        calendar_class="unknown",
        signals=signals,
        reasoning=(
            "signals don't cleanly match Class A or Class B — page may be a "
            "non-Legistar variant, partially rendered, or behind an unexpected "
            "structure. Caller decides dispatch."
            + archive_suffix
        ),
        vendor=vendor,
        latest_meeting_year=latest_meeting_year,
        archive_only_candidate=archive_only_candidate,
    )


def main(argv: Optional[list] = None) -> int:
    """CLI entry point. Classify a calendar URL and print the result."""
    parser = argparse.ArgumentParser(
        description=(
            "Detect the extraction-class (A/B/unknown) a calendar URL needs "
            "for the Haiku HTML scraper. Pure HTML check; no LLM spend."
        )
    )
    parser.add_argument("url", help="Calendar URL to classify.")
    parser.add_argument(
        "--vendor",
        default="legistar",
        help="Calendar vendor (currently only 'legistar' is implemented).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human-readable text.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=_DEFAULT_FETCH_TIMEOUT_S,
        help=f"HTTP fetch timeout in seconds (default {_DEFAULT_FETCH_TIMEOUT_S}).",
    )
    parser.add_argument(
        "--archive-threshold-year",
        type=int,
        default=None,
        help=(
            "When the max meeting year in the page is below this threshold, "
            "the result is flagged as archive-only candidate (F-4). Default: "
            "no archive check. Typical production value: "
            f"current-year minus {ARCHIVE_AGE_THRESHOLD_YEARS}."
        ),
    )
    args = parser.parse_args(argv)

    result = detect_calendar_class(
        args.url, vendor=args.vendor, timeout_s=args.timeout,
        archive_threshold_year=args.archive_threshold_year,
    )

    if args.json:
        print(json.dumps(
            {
                "calendar_class": result.calendar_class,
                "vendor": result.vendor,
                "signals": result.signals,
                "reasoning": result.reasoning,
                "latest_meeting_year": result.latest_meeting_year,
                "archive_only_candidate": result.archive_only_candidate,
            },
            indent=2,
        ))
    else:
        print(f"URL:       {args.url}")
        print(f"Vendor:    {result.vendor}")
        print(f"Class:     {result.calendar_class}")
        print(f"Signals:   {result.signals}")
        print(f"Reasoning: {result.reasoning}")
        if result.latest_meeting_year is not None:
            print(f"Latest meeting year: {result.latest_meeting_year}")
        if result.archive_only_candidate:
            print(f"Archive-only candidate: yes")

    return 0


if __name__ == "__main__":
    sys.exit(main())
