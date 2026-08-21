#!/usr/bin/env python3.11
"""refresh_city_calendars — Phase H H-3 weekly calendar-refresh job.

For each city with a `meeting_patterns[]` field in its
`city_intelligence/<slug>.json`, this script:

  1. Scrapes the city's calendar via the existing parser (parser_loader
     + the city-specific scrape_calendar function in parser_index.json).
  2. Normalizes the scrape and caches it via database.cache_meetings()
     (UPSERT path — preserves meeting IDs across re-scrapes per D-038).
  3. For each pattern in meeting_patterns[]:
       - Projects expected meeting dates over the next N days using
         pattern_projection.get_upcoming_meetings_from_patterns / project_pattern.
       - Filters the freshly-scraped meetings to the same window + body match.
       - Reconciles: match_status ∈ {match, drift, partial, no_data}.
       - Writes a row to pattern_health via database.record_pattern_health().
  4. Returns a per-city summary so the orchestrator can surface drift
     events in its heartbeat (H-4 follow-up extends this with explicit
     escalation when match_status='drift' persists for N consecutive
     refreshes).

Cities WITHOUT meeting_patterns[] are skipped silently — they fall back
to the existing scrape-only path until they're curated (per H-5's
backward-compat).

Scheduling: this is designed to run weekly (Mondays at 6am is the
recommended cadence per the H-3 plan). The orchestrator routine
(agents/orchestrator.routine.md) includes a case that recommends
running this job when the last refresh per pattern is >7 days old.

Body matching:
  Each pattern's `meeting_type` (e.g. "City Council") is matched against
  the scraped meeting's `meeting_title` (e.g. "City Council - May 06,
  2025" OR "Regular City Council Meeting") via case-insensitive
  substring containment. This handles both clean per-body naming
  (Kingman) and lumped-with-session-type naming (Bullhead's "Regular
  City Council Meeting" + variants — all match the "City Council"
  pattern_type substring). H-4 follow-up could add a per-pattern
  `matches_titles` regex for cities with funky parser naming.

Usage:
    python3.11 scripts/refresh_city_calendars.py                # all cities with patterns
    python3.11 scripts/refresh_city_calendars.py --city Kingman # one city
    python3.11 scripts/refresh_city_calendars.py --dry-run      # no DB writes
    python3.11 scripts/refresh_city_calendars.py --days-ahead 60

Exit codes:
    0: success (refresh ran, pattern_health updated)
    2: input error (unknown city, no patterns found, etc.)
    3: scrape failed for one or more cities
    4: pattern_health write failed
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Make parsers/ importable when invoked from cwd=parsers/ or parsers/scripts/.
_PARSERS_DIR = Path(__file__).resolve().parent.parent
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

from database import (  # noqa: E402
    cache_meetings,
    get_newly_drifted_patterns,
    record_pattern_health,
)
from meeting_patterns import validate_meeting_patterns  # noqa: E402
from parser_loader import load_parser_index, scrape_city_calendar  # noqa: E402
from pattern_projection import project_pattern  # noqa: E402
from normalize import normalize_meeting_fields  # noqa: E402

logger = logging.getLogger(__name__)

_CITY_INTELLIGENCE_DIR = _PARSERS_DIR.parent / "city_intelligence"


def _city_slug(city_name: str) -> str:
    return city_name.strip().lower().replace(" ", "_")


def _list_cities_with_patterns() -> List[Tuple[str, List[Dict]]]:
    """Return [(city_name, patterns), ...] for every city_intelligence JSON
    that has a non-empty meeting_patterns[] field."""
    if not _CITY_INTELLIGENCE_DIR.is_dir():
        return []
    out: List[Tuple[str, List[Dict]]] = []
    for path in sorted(_CITY_INTELLIGENCE_DIR.glob("*.json")):
        try:
            city = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("could not read %s; skipping", path.name)
            continue
        patterns = city.get("meeting_patterns")
        if not isinstance(patterns, list) or not patterns:
            continue
        canonical_name = city.get("canonical_name") or path.stem.replace("_", " ").title()
        out.append((canonical_name, patterns))
    return out


def _load_patterns_for(city_name: str) -> Optional[List[Dict]]:
    """Direct file lookup; returns None if file or field is missing."""
    path = _CITY_INTELLIGENCE_DIR / f"{_city_slug(city_name)}.json"
    if not path.is_file():
        return None
    try:
        city = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    patterns = city.get("meeting_patterns")
    return patterns if isinstance(patterns, list) else None


# ─────────────────────────────────────────────────────────────────
# Reconciliation
# ─────────────────────────────────────────────────────────────────


def _parse_scraped_date(s: Optional[str]) -> Optional[date]:
    """Best-effort parse of a scraped meeting_date string. Normalized
    output of normalize_meeting_fields() is YYYY-MM-DD but some parsers
    (Lake Havasu) emit M/D/YYYY — handle both."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%-m/%-d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _body_match(pattern_meeting_type: str, meeting_title: str) -> bool:
    """Case-insensitive substring containment. Handles both clean per-body
    naming (Kingman: "City Council") and session-type variants (Bullhead:
    "Regular City Council Meeting" matches "City Council")."""
    if not pattern_meeting_type or not meeting_title:
        return False
    return pattern_meeting_type.lower() in meeting_title.lower()


def reconcile_pattern(
    pattern: Dict,
    scraped_meetings: List[Dict],
    window_start: date,
    window_end: date,
) -> Tuple[str, Optional[str], List[str], List[str]]:
    """Compare a pattern's projected dates against the scraped meetings.

    Returns: (match_status, drift_notes, expected_dates, actually_scraped_dates).
    """
    expected_dates = [
        m["datetime"].date().isoformat()
        for m in project_pattern(pattern, window_start, window_end)
    ]

    pattern_type = pattern.get("meeting_type") or ""
    scraped_for_body: List[str] = []
    for m in scraped_meetings:
        if not _body_match(pattern_type, m.get("meeting_title") or ""):
            continue
        d = _parse_scraped_date(m.get("meeting_date"))
        if d is None:
            continue
        if window_start <= d <= window_end:
            scraped_for_body.append(d.isoformat())

    # Dedup scraped dates — Bullhead style "Regular + Special on same day"
    # collapses to one entry per date (we only care whether the body met
    # that day, not how many sessions it held).
    scraped_dates = sorted(set(scraped_for_body))
    expected_set = set(expected_dates)
    scraped_set = set(scraped_dates)

    if not expected_dates and not scraped_dates:
        # Pattern projects nothing in this window (e.g. months_of_year
        # filter excludes this window OR pattern is adhoc) AND scrape
        # found nothing for this body either. Honest signal: nothing to
        # reconcile yet.
        return ("no_data", "Pattern projects no instances in window + scrape returned none for this body", [], [])

    if not expected_dates:
        # Pattern is adhoc OR filtered out by months_of_year for this
        # window. Scrape may still show meetings; they're informational
        # but don't contribute to drift detection.
        notes = f"Pattern is adhoc/out-of-cadence-window; scrape shows {len(scraped_dates)} instance(s) (informational)"
        return ("no_data", notes, [], scraped_dates)

    if not scraped_dates:
        # Pattern expects meetings; scrape returned nothing for this body.
        # Could be parser-coverage gap OR genuine "city's calendar is
        # empty for this window." Either way it's a real signal.
        notes = (
            f"Pattern projected {len(expected_dates)} instance(s) but scrape returned 0 — "
            "either the calendar is empty for this window OR the parser doesn't capture this body"
        )
        return ("no_data", notes, expected_dates, [])

    matched = expected_set & scraped_set
    missing = expected_set - matched
    unexpected = scraped_set - matched

    if not missing and not unexpected:
        return ("match", None, expected_dates, scraped_dates)

    # Bucket drift severity:
    #   - More than half of expected dates missing → drift
    #   - Any partial overlap (some match, some don't) → partial
    if len(missing) > len(expected_dates) / 2:
        notes_parts = []
        if missing:
            notes_parts.append(
                f"missing: {', '.join(sorted(missing))}"
            )
        if unexpected:
            notes_parts.append(
                f"unexpected: {', '.join(sorted(unexpected))}"
            )
        return ("drift", " · ".join(notes_parts), expected_dates, scraped_dates)

    notes_parts = []
    if missing:
        notes_parts.append(f"missing: {', '.join(sorted(missing))}")
    if unexpected:
        notes_parts.append(f"unexpected: {', '.join(sorted(unexpected))}")
    return ("partial", " · ".join(notes_parts), expected_dates, scraped_dates)


# ─────────────────────────────────────────────────────────────────
# Per-city refresh
# ─────────────────────────────────────────────────────────────────


def refresh_city(
    city_name: str,
    patterns: List[Dict],
    days_ahead: int,
    dry_run: bool,
) -> Dict:
    """Scrape + cache + reconcile for one city. Returns a summary dict."""
    logger.info("[%s] %d pattern(s); window = +%d days", city_name, len(patterns), days_ahead)

    # Validate the patterns once before doing any work — surface schema
    # issues immediately so the operator catches them before they
    # propagate to the pattern_health table.
    ok, errs = validate_meeting_patterns(patterns)
    if not ok:
        return {
            "city": city_name,
            "ok": False,
            "error": "meeting_patterns schema validation failed",
            "validation_errors": errs[:5],
        }

    # Step 1: scrape the calendar.
    try:
        scraped_raw = scrape_city_calendar(city_name)
    except Exception as exc:
        return {
            "city": city_name,
            "ok": False,
            "error": f"scrape failed: {type(exc).__name__}: {exc}",
        }
    scraped = [normalize_meeting_fields(m) for m in (scraped_raw or [])]
    logger.info("[%s] scrape returned %d meeting(s)", city_name, len(scraped))

    # Step 2: cache to the meetings table (UPSERT path).
    index = load_parser_index()
    county = index.get(city_name, {}).get("county", "Unknown")
    if not dry_run:
        try:
            cache_meetings(city_name, county, scraped)
        except Exception as exc:
            return {
                "city": city_name,
                "ok": False,
                "error": f"cache write failed: {type(exc).__name__}: {exc}",
            }

    # Step 3: reconcile each pattern.
    window_start = date.today()
    window_end = window_start + timedelta(days=days_ahead)
    pattern_reports: List[Dict] = []
    for p in patterns:
        match_status, drift_notes, expected, scraped_dates = reconcile_pattern(
            p, scraped, window_start, window_end,
        )
        pattern_id = p.get("pattern_id") or "<unknown>"
        report = {
            "pattern_id": pattern_id,
            "meeting_type": p.get("meeting_type"),
            "match_status": match_status,
            "expected_count": len(expected),
            "scraped_count": len(scraped_dates),
            "drift_notes": drift_notes,
        }
        pattern_reports.append(report)

        if dry_run:
            continue

        try:
            record_pattern_health(
                city_name=city_name,
                state=index.get(city_name, {}).get("state", "Arizona"),
                pattern_id=pattern_id,
                window_start=window_start.isoformat(),
                window_end=window_end.isoformat(),
                expected_next=json.dumps(expected),
                actually_scraped=json.dumps(scraped_dates),
                match_status=match_status,
                drift_notes=drift_notes,
            )
        except Exception as exc:
            report["health_write_error"] = str(exc)

    return {
        "city": city_name,
        "ok": True,
        "scraped_count": len(scraped),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "pattern_reports": pattern_reports,
    }


# ─────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────


def escalate_drifted_patterns(dry_run: bool) -> List[Dict]:
    """H-4: after the refresh has written pattern_health rows, find any
    newly-drifted patterns + fire a Slack escalation per pattern at
    severity=decision. Returns the list of patterns escalated (for the
    summary output).

    "Newly-drifted" means the most recent pattern_health row is
    `match_status='drift'` AND the prior row (if any) was NOT drift —
    captures the moment a pattern transitions INTO drift, so the operator
    gets one escalation per real drift onset rather than weekly
    duplicates.

    Returns an empty list when there are no newly-drifted patterns (the
    common, healthy case).
    """
    drifted = get_newly_drifted_patterns()
    if not drifted:
        return []

    # Lazy import — slack_notifier pulls in optional deps (requests, etc.)
    # we don't want to require for the dry-run / no-drift path.
    try:
        from slack_notifier import send_escalation
    except Exception as exc:  # pragma: no cover — defensive
        logger.error("could not import slack_notifier (%s); drift events queued only", exc)
        return []

    escalated: List[Dict] = []
    for d in drifted:
        city = d["city_name"]
        pattern_id = d["pattern_id"]
        notes = d.get("drift_notes") or "(no diff captured)"
        prior_status = d.get("prior_status") or "(first-ever refresh for this pattern)"

        summary = (
            f"Meeting pattern {city}:{pattern_id} drifted — "
            f"projection vs scrape diverged"
        )
        what_i_see = [
            f"window: {d['window_start']} → {d['window_end']}",
            f"diff: {notes}",
            f"prior refresh status: {prior_status}",
        ]
        what_id_do = [
            "If the city's meeting schedule actually changed → re-run the H-1 extraction workflow for this city to refresh the pattern",
            "If the scrape coverage looks broken → investigate the parser (check ParserDashboard for the city's status)",
            "If both look fine → check that the parser captures meetings in the projected window (some parsers only capture historical ones)",
        ]

        if dry_run:
            logger.info(
                "[escalate] DRY-RUN would escalate: %s pattern_id=%s",
                city, pattern_id,
            )
            escalated.append({
                "city_name": city,
                "pattern_id": pattern_id,
                "summary": summary,
                "delivered": False,
                "dry_run": True,
            })
            continue

        try:
            result = send_escalation(
                role="parser-custodian",
                severity="decision",
                summary=summary,
                what_i_see=what_i_see,
                what_id_do=what_id_do,
                audit_row=f"pattern_health.id={d['id']}",
            )
            escalated.append({
                "city_name": city,
                "pattern_id": pattern_id,
                "summary": summary,
                "delivered": getattr(result, "delivered_to_slack", False),
                "queued_locally": getattr(result, "queued_locally", False),
                "error": getattr(result, "error", None),
            })
            logger.info(
                "[escalate] %s pattern_id=%s delivered=%s",
                city, pattern_id,
                getattr(result, "delivered_to_slack", False),
            )
        except Exception as exc:
            logger.error(
                "[escalate] failed for %s pattern_id=%s: %s: %s",
                city, pattern_id, type(exc).__name__, exc,
            )
            escalated.append({
                "city_name": city,
                "pattern_id": pattern_id,
                "summary": summary,
                "delivered": False,
                "error": f"{type(exc).__name__}: {exc}",
            })

    return escalated


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="H-3 weekly calendar-refresh job")
    p.add_argument(
        "--city",
        help="Refresh one city by canonical name (e.g. 'Kingman'). "
             "Omitted: refreshes every city with meeting_patterns[].",
    )
    p.add_argument(
        "--days-ahead",
        type=int,
        default=30,
        help="Projection window (default: 30 days)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="No DB writes (no scrape cache update + no pattern_health rows + no Slack escalations).",
    )
    p.add_argument(
        "--no-escalate",
        action="store_true",
        help="Skip H-4 drift escalation (the refresh still writes pattern_health rows; "
             "use when smoke-testing the reconciliation logic without paging Slack).",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="More logging.",
    )
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # Resolve target cities.
    if args.city:
        patterns = _load_patterns_for(args.city)
        if patterns is None:
            print(f"ERROR: no city_intelligence file found for {args.city!r}", file=sys.stderr)
            return 2
        if not patterns:
            print(f"ERROR: {args.city!r} has no meeting_patterns[] curated yet", file=sys.stderr)
            return 2
        targets = [(args.city, patterns)]
    else:
        targets = _list_cities_with_patterns()
        if not targets:
            print("ERROR: no cities with meeting_patterns[] found in city_intelligence/", file=sys.stderr)
            return 2
        logger.info("refresh: %d cities with patterns", len(targets))

    # Run.
    summaries = []
    any_failure = False
    for city_name, patterns in targets:
        summary = refresh_city(city_name, patterns, args.days_ahead, args.dry_run)
        summaries.append(summary)
        if not summary.get("ok"):
            any_failure = True

    # H-4: after all pattern_health rows are written, find + escalate
    # newly-drifted patterns. Skipped on --dry-run (no DB writes happened
    # so the "newly-drifted" calculation would be incorrect) and on
    # --no-escalate (smoke-test mode).
    escalations: List[Dict] = []
    if not args.dry_run and not args.no_escalate:
        escalations = escalate_drifted_patterns(dry_run=False)
    elif args.dry_run:
        # In dry-run we still REPORT what would have escalated, based on
        # the pre-existing rows in the DB. This is the "what's currently
        # drifting" snapshot, useful for cron health checks.
        escalations = escalate_drifted_patterns(dry_run=True)

    # Print structured summary so the heartbeat/orchestrator can parse.
    output = {
        "refresh_at": datetime.now().isoformat(),
        "days_ahead": args.days_ahead,
        "dry_run": args.dry_run,
        "no_escalate": args.no_escalate,
        "cities_refreshed": len(summaries),
        "city_summaries": summaries,
        "escalations": escalations,
        "escalation_count": len(escalations),
    }
    print(json.dumps(output, indent=2, ensure_ascii=False, default=str))

    return 3 if any_failure else 0


if __name__ == "__main__":
    sys.exit(main())
