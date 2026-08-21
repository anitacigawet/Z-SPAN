"""S-036 V1-complete — field-sanity / URL HEAD-check output gate.

Sits between `parse_haiku_response()` and `normalize_meetings()` in the
Haiku scraper wrapper. The agent's output is structurally valid JSON but
the field values can still be wrong:

  - **URL hallucinations.** The V0 Lake Havasu probe fabricated a
    `video_url` as the calendar page URL — the agent self-flagged it, but
    a silent hallucination class is what this gate exists to catch
    mechanically. HEAD-check each extracted URL; clear non-2xx URLs and
    record in caveats.

  - **Date format drift.** Schema mandates ISO `YYYY-MM-DD` but Haiku
    might emit `M/D/YYYY` or `Month D, YYYY` on edge cases. Validate the
    format; clear non-conforming values and record in caveats.

  - **Plausible-time sanity.** Reject obviously-junk time values that
    don't match `H:MM AM/PM` or `HH:MM` patterns.

The gate's contract: take Haiku's parsed response dict, return a sanitized
copy + a sanity report. The sanitized dict is what flows into
`normalize_meetings()` and ultimately the cache. The sanity report becomes
part of `_invocation` metadata so downstream consumers (V3 classifier
dataset, operator dashboards) can see exactly what was caught.

Network budget per D-107 spirit: extracted URLs are external-site touches,
so the gate caps total HEAD checks per invocation, applies a 200ms
inter-request delay, and uses a short per-check timeout. The discipline is
"verify, don't crawl" — we touch each extracted URL once with HEAD only.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests


logger = logging.getLogger(__name__)


# Per-invocation network budget. 300 HEAD checks comfortably covers a
# 100-meeting archival page like Glendale's Legistar (≈3 URL fields per
# meeting); at INTER_REQUEST_DELAY_S below this is ~60s worst case, still
# acceptable. The cap exists so a runaway extraction with thousands of
# fabricated URLs can't grind on HEAD requests for an hour. Calibrated up
# from the original 50 after the 2026-06-14 Maricopa validation found
# Glendale's archival pages skip-over-budget at 50 (finding F-3).
MAX_HEAD_CHECKS_PER_INVOCATION = 300

# 200ms between HEAD requests — keeps the external footprint small without
# making the gate too slow for the typical extraction.
INTER_REQUEST_DELAY_S = 0.2

# Per-check HTTP timeout. Most municipal sites respond inside 2s; 5s catches
# the rest without making the gate noticeably slow.
HEAD_CHECK_TIMEOUT_S = 5

_USER_AGENT = (
    "Mozilla/5.0 (compatible; ZSPAN-field-sanity/1.0; "
    "+civic transparency tooling)"
)

# Schema fields that hold URLs we want to verify mechanically. The order
# matters only for the deterministic-budget order; all four are checked.
_URL_FIELDS = ("agenda_url", "minutes_url", "video_url", "ecomment_url")

# Strict ISO date check — exactly YYYY-MM-DD; reject everything else so
# the cache never stores ambiguous formats.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Plausible time formats: "H:MM AM/PM" / "HH:MM AM/PM" / "HH:MM" 24-hour.
# Empty string is also valid (means "no time on the source").
_PLAUSIBLE_TIME_RE = re.compile(
    r"^\s*(?:"
    r"\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)"   # 7:00 PM, 12:30 am
    r"|\d{1,2}:\d{2}"                      # 19:00 (24-hour)
    r")\s*$"
)


@dataclass
class SanityReport:
    """Per-invocation summary of what the gate caught + cleared.

    Surfaced as `_invocation.sanity_report` in the wrapper's output so
    downstream consumers (operator dashboards, V3 classifier dataset) can
    see the full mechanical-verification trail without re-running it.

    Distinguishes three HEAD-check outcomes per finding F-1 (2026-06-14
    Maricopa validation):
      - `head_checks_passed`     — 2xx/3xx; URL kept
      - `head_checks_failed`     — 4xx/5xx; URL cleared (verified bad)
      - `head_checks_unverified` — Timeout/ConnectionError/etc.; URL kept
        + caveat added (the server didn't speak, which doesn't prove the
        URL is wrong — YouTube's edge rejects HEAD from non-browser UAs,
        which is the case that motivated this distinction)
    """
    head_checks_attempted: int = 0
    head_checks_passed: int = 0
    head_checks_failed: int = 0
    head_checks_unverified: int = 0
    head_checks_skipped_over_budget: int = 0
    urls_cleared: List[Dict] = field(default_factory=list)  # {meeting_index, field, url, reason}
    urls_unverified: List[Dict] = field(default_factory=list)  # {meeting_index, field, url, reason}
    dates_cleared: List[Dict] = field(default_factory=list)  # {meeting_index, original_value, reason}
    times_cleared: List[Dict] = field(default_factory=list)  # {meeting_index, original_value, reason}

    def to_dict(self) -> Dict:
        return {
            "head_checks_attempted": self.head_checks_attempted,
            "head_checks_passed": self.head_checks_passed,
            "head_checks_failed": self.head_checks_failed,
            "head_checks_unverified": self.head_checks_unverified,
            "head_checks_skipped_over_budget": self.head_checks_skipped_over_budget,
            "urls_cleared": self.urls_cleared,
            "urls_unverified": self.urls_unverified,
            "dates_cleared": self.dates_cleared,
            "times_cleared": self.times_cleared,
        }


# HEAD-check outcomes (post-F-1 fix). Three-state instead of binary
# pass/fail so "the server didn't speak" is distinct from "the server
# said 404" — the former keeps the URL, the latter clears it.
HEAD_RESULT_PASS = "pass"            # 2xx / 3xx — URL kept
HEAD_RESULT_BAD = "bad"              # 4xx / 5xx — URL cleared (verified bad)
HEAD_RESULT_UNVERIFIED = "unverified" # Timeout / ConnectionError — URL kept + caveat


def _head_check_url(url: str, timeout_s: int = HEAD_CHECK_TIMEOUT_S) -> Tuple[str, str]:
    """Return (result, reason) per the three-state HEAD verdict above.

    Per F-1 (2026-06-14 Maricopa validation): YouTube's edge rejects HEAD
    requests from non-browser User-Agents — Mesa's 4 real YouTube
    `video_url` fields all failed with ConnectionError in the original
    binary policy, silently destroying real data. ConnectionError doesn't
    prove the URL is wrong; it just proves the server didn't speak HEAD
    to a Python User-Agent. Promote that to "unverified" and keep the URL.
    """
    if not url:
        return (HEAD_RESULT_PASS, "")  # nothing to check; empty is valid
    try:
        resp = requests.head(
            url,
            timeout=timeout_s,
            allow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        )
    except requests.Timeout:
        return (HEAD_RESULT_UNVERIFIED, f"timeout after {timeout_s}s")
    except requests.ConnectionError as exc:
        return (HEAD_RESULT_UNVERIFIED, f"connection error: {type(exc).__name__}")
    except requests.RequestException as exc:
        # Other request-layer errors (SSLError, InvalidURL, etc.) — these
        # are server-doesn't-speak class, treat as unverified.
        return (HEAD_RESULT_UNVERIFIED, f"request error: {type(exc).__name__}")
    if 200 <= resp.status_code < 400:
        return (HEAD_RESULT_PASS, "")
    return (HEAD_RESULT_BAD, f"HTTP {resp.status_code}")


def _is_valid_iso_date(value: str) -> bool:
    if not value:
        return True  # empty is valid; means "no date on source"
    return bool(_ISO_DATE_RE.match(value))


def _is_plausible_time(value: str) -> bool:
    if not value:
        return True  # empty is valid
    return bool(_PLAUSIBLE_TIME_RE.match(value))


def apply_field_sanity(
    parsed_response: Dict,
    *,
    skip_head_checks: bool = False,
) -> Tuple[Dict, SanityReport]:
    """Apply the field-sanity gate to a Haiku parsed response.

    Returns (sanitized_response, sanity_report). The sanitized response is
    a copy with cleared URLs / dates / times replaced by empty strings; the
    original Haiku response is NOT mutated. The sanity report carries the
    full per-clear audit trail.

    Args:
        parsed_response: The dict returned by `parse_haiku_response()`. Must
            have a `meetings` list to do per-meeting work; otherwise the
            gate is a no-op for the input.
        skip_head_checks: When True, skip all network HEAD checks (faster;
            used in tests). Date + time sanity still apply.

    Per-field policy:
        - URLs: HEAD-check each. On 4xx/5xx/timeout/connection-error, clear
          the URL to "" and record in caveats. Per-invocation cap.
        - Dates: validate strict ISO YYYY-MM-DD. Clear non-conforming.
        - Times: validate plausible format. Clear non-conforming.

    The gate is permissive on empty values — empty strings always pass.
    """
    sanitized = dict(parsed_response)
    meetings_in = parsed_response.get("meetings")
    if not isinstance(meetings_in, list):
        return (sanitized, SanityReport())

    report = SanityReport()
    sanitized_meetings: List[Dict] = []
    head_checks_used = 0

    for idx, meeting in enumerate(meetings_in):
        if not isinstance(meeting, dict):
            sanitized_meetings.append(meeting)
            continue

        meeting_copy = dict(meeting)

        # ── URL HEAD-checks ────────────────────────────────────────────
        for fld in _URL_FIELDS:
            url = meeting_copy.get(fld)
            if not url:
                continue
            if skip_head_checks:
                continue
            if head_checks_used >= MAX_HEAD_CHECKS_PER_INVOCATION:
                report.head_checks_skipped_over_budget += 1
                continue
            if head_checks_used > 0:
                time.sleep(INTER_REQUEST_DELAY_S)
            head_checks_used += 1
            report.head_checks_attempted += 1
            result, reason = _head_check_url(url)
            if result == HEAD_RESULT_PASS:
                report.head_checks_passed += 1
            elif result == HEAD_RESULT_BAD:
                # Verified bad (HTTP 4xx/5xx) — clear the URL.
                report.head_checks_failed += 1
                report.urls_cleared.append({
                    "meeting_index": idx,
                    "field": fld,
                    "url": url,
                    "reason": reason,
                })
                meeting_copy[fld] = ""
            else:
                # Unverified (Timeout / ConnectionError / etc.) — server
                # didn't speak HEAD; that's not a verdict on the URL itself
                # (per F-1, YouTube's edge rejects HEAD from non-browser
                # User-Agents). Keep URL, record so the operator sees it.
                report.head_checks_unverified += 1
                report.urls_unverified.append({
                    "meeting_index": idx,
                    "field": fld,
                    "url": url,
                    "reason": reason,
                })

        # ── ISO date sanity ────────────────────────────────────────────
        date_val = meeting_copy.get("meeting_date", "")
        if isinstance(date_val, str) and not _is_valid_iso_date(date_val):
            report.dates_cleared.append({
                "meeting_index": idx,
                "original_value": date_val,
                "reason": "not strict ISO YYYY-MM-DD",
            })
            meeting_copy["meeting_date"] = ""

        # ── Plausible-time sanity ──────────────────────────────────────
        time_val = meeting_copy.get("meeting_time", "")
        if isinstance(time_val, str) and not _is_plausible_time(time_val):
            report.times_cleared.append({
                "meeting_index": idx,
                "original_value": time_val,
                "reason": "not H:MM AM/PM or HH:MM",
            })
            meeting_copy["meeting_time"] = ""

        sanitized_meetings.append(meeting_copy)

    sanitized["meetings"] = sanitized_meetings

    # Push concise human-readable summaries into the agent's `caveats` list
    # so the operator sees the headlines without needing to expand the
    # full sanity_report. The full per-finding detail stays in the report.
    any_to_report = (
        report.urls_cleared or report.urls_unverified
        or report.dates_cleared or report.times_cleared
        or report.head_checks_skipped_over_budget
    )
    if any_to_report:
        caveats = list(sanitized.get("caveats") or [])
        if report.urls_cleared:
            caveats.append(
                f"field-sanity: cleared {len(report.urls_cleared)} "
                f"URL(s) on HTTP-error verdict; see sanity_report.urls_cleared"
            )
        if report.urls_unverified:
            caveats.append(
                f"field-sanity: {len(report.urls_unverified)} URL(s) "
                f"could not be HEAD-verified (server didn't speak); URLs "
                f"kept; see sanity_report.urls_unverified"
            )
        if report.dates_cleared:
            caveats.append(
                f"field-sanity: cleared {len(report.dates_cleared)} "
                f"non-ISO date(s); see sanity_report.dates_cleared"
            )
        if report.times_cleared:
            caveats.append(
                f"field-sanity: cleared {len(report.times_cleared)} "
                f"implausible time(s); see sanity_report.times_cleared"
            )
        if report.head_checks_skipped_over_budget:
            caveats.append(
                f"field-sanity: skipped HEAD checks on "
                f"{report.head_checks_skipped_over_budget} URL(s) — "
                f"per-invocation budget of "
                f"{MAX_HEAD_CHECKS_PER_INVOCATION} hit"
            )
        sanitized["caveats"] = caveats

    return (sanitized, report)
