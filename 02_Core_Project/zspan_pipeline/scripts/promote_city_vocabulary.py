#!/usr/bin/env python3.11
"""
promote_city_vocabulary — promote DB-table vocabulary corrections into
the city's canonical `whisper_vocabulary_hints` JSON (T-018).

This is the process layer that turns the live `city_vocabulary_corrections`
accumulation into curated, permanent intelligence. Every Gemini-surfaced
correction lands in the DB with `applied_count = 1`. When a correction
recurs in a later review session, applied_count bumps. Once it crosses
a threshold (default 2) — or the operator manually endorses it from
the Inbox — this script appends an entry into the city's JSON, where
the Whisper prompt builder + the city's prompt-level correction
directive both pick it up automatically going forward.

Usage:
    # Dry-run (default) — list candidates without writing:
    python3.11 -m zspan_pipeline.scripts.promote_city_vocabulary \\
        --city Kingman

    # Apply: promote every correction with applied_count >= threshold:
    python3.11 -m zspan_pipeline.scripts.promote_city_vocabulary \\
        --city Kingman --apply

    # Custom threshold:
    python3.11 -m zspan_pipeline.scripts.promote_city_vocabulary \\
        --city Kingman --threshold 3 --apply

    # Manually promote a single term (any applied_count, including 1):
    python3.11 -m zspan_pipeline.scripts.promote_city_vocabulary \\
        --city Kingman --term "Andy Devine" \\
        --category person --apply

Idempotent on re-run: corrections already promoted (`promoted_at IS NOT NULL`)
are skipped. The canonical JSON's `whisper_vocabulary_hints` array dedup-
checks by term so a manual promotion of a term that's already there is
a no-op rather than a duplicate.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PARSERS_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "council_navigator"
    / "parsers"
)
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

from database import (  # noqa: E402
    list_pending_promotions,
    mark_correction_promoted,
    append_whisper_vocabulary_hint,
    get_connection,
)


def _promote_one(
    correction: dict, category: str | None, promoted_by: str
) -> dict:
    """Promote a single correction: write to JSON + flag in DB. Returns
    a small summary dict for the report."""
    term = correction["right"]
    appended = append_whisper_vocabulary_hint(
        city_name=correction["city_name"],
        term=term,
        category=category,
        first_seen=correction.get("created_at"),
        source=correction.get("first_observed_response_file"),
        promoted_by=promoted_by,
    )
    db_row = mark_correction_promoted(
        correction_id=correction["id"],
        promoted_by=promoted_by,
    )
    return {
        "id": correction["id"],
        "term": term,
        "was_already_in_json": appended.get("_already_present", False) if appended else False,
        "db_marked_promoted": db_row is not None,
    }


def _resolve_manual_correction(city: str, term: str) -> dict | None:
    """Look up a correction row by `right` (the canonical spelling).
    Used when the operator passes --term to promote a one-off."""
    conn = get_connection()
    row = conn.execute(
        """
        SELECT id, city_name, wrong, right, applied_count, auto_apply,
               first_observed_response_file, last_applied_at, created_at,
               promoted_at, promoted_by
        FROM city_vocabulary_corrections
        WHERE city_name = ? AND LOWER(right) = LOWER(?)
        ORDER BY applied_count DESC, id ASC
        LIMIT 1
        """,
        (city, term),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", required=True, help="City name (canonical form, e.g. 'Kingman')")
    parser.add_argument("--threshold", type=int, default=2,
                        help="Minimum applied_count to auto-promote (default: 2)")
    parser.add_argument("--apply", action="store_true",
                        help="Write changes. Default is dry-run (preview only).")
    parser.add_argument("--term", default=None,
                        help="Manually promote a single term (by 'right' field). "
                             "Bypasses the threshold check.")
    parser.add_argument("--category", default=None,
                        help="Category for promoted entries (person/street/place/business/civic_term/event/other). "
                             "Optional but recommended for organization.")
    parser.add_argument("--promoted-by", default="cli",
                        help="String stamped on the promoted entries (default: 'cli')")
    args = parser.parse_args()

    mode = "live" if args.apply else "dry-run"
    print("=" * 64)
    print(f"  T-018 vocabulary promotion ({mode})")
    print("=" * 64)
    print(f"  City:      {args.city}")
    print(f"  Threshold: applied_count >= {args.threshold}")
    if args.term:
        print(f"  Manual:    --term {args.term!r}")
    print()

    # Manual single-term promotion path
    if args.term:
        c = _resolve_manual_correction(args.city, args.term)
        if c is None:
            print(f"ERROR: no city_vocabulary_corrections row found for "
                  f"city={args.city!r} right={args.term!r}.")
            print("       Either the term hasn't been observed yet, or the "
                  "spelling here doesn't match the DB.")
            return 1
        if c["promoted_at"] is not None:
            print(f"ALREADY PROMOTED: id={c['id']} term={c['right']!r} "
                  f"at {c['promoted_at']} by {c['promoted_by']!r}")
            return 0
        if not c["auto_apply"]:
            print(f"REJECTED (auto_apply=0): id={c['id']} term={c['right']!r}. "
                  "This term was previously rejected at the Inbox. "
                  "Re-enable in DB before promoting.")
            return 1
        print(f"Candidate: id={c['id']} applied_count={c['applied_count']} "
              f"wrong={c['wrong']!r} right={c['right']!r}")
        if not args.apply:
            print("\n(dry-run — pass --apply to promote)")
            return 0
        summary = _promote_one(c, args.category, args.promoted_by)
        print(f"\n  PROMOTED: {summary}")
        return 0

    # Bulk threshold-based promotion path
    candidates = list_pending_promotions(args.city, threshold=args.threshold)
    if not candidates:
        print("Nothing to promote — no auto_apply=1, not-yet-promoted "
              "corrections for this city.")
        return 0

    auto_qualifying = [c for c in candidates if c["meets_threshold"]]
    below_threshold = [c for c in candidates if not c["meets_threshold"]]

    print(f"Candidates (auto-promote eligible — applied_count >= {args.threshold}): "
          f"{len(auto_qualifying)}")
    for c in auto_qualifying:
        print(f"  id={c['id']:4d}  applied_count={c['applied_count']:2d}  "
              f"{c['wrong']!r} -> {c['right']!r}  "
              f"({c.get('first_observed_response_file') or 'no-source'})")
    if below_threshold:
        print(f"\nBelow threshold (manual-only — surface in Inbox): "
              f"{len(below_threshold)}")
        for c in below_threshold[:15]:
            print(f"  id={c['id']:4d}  applied_count={c['applied_count']:2d}  "
                  f"{c['wrong']!r} -> {c['right']!r}")
        if len(below_threshold) > 15:
            print(f"  ... ({len(below_threshold) - 15} more)")

    if not args.apply:
        print("\n(dry-run — pass --apply to promote the auto-eligible set)")
        return 0
    if not auto_qualifying:
        print("\nNothing to promote at this threshold. "
              "Use --term to manually promote one-offs.")
        return 0

    print(f"\nPromoting {len(auto_qualifying)} term(s)...")
    results = []
    for c in auto_qualifying:
        summary = _promote_one(c, args.category, args.promoted_by)
        results.append(summary)
        status = "OK"
        if summary["was_already_in_json"]:
            status = "ALREADY_IN_JSON (DB flagged)"
        print(f"  id={summary['id']:4d}  term={summary['term']!r:30s}  {status}")

    print()
    print(f"Summary: {len(results)} promoted; "
          f"{sum(1 for r in results if r['was_already_in_json'])} were "
          "already in the JSON (no-op append, DB flagged).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
