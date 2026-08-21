#!/usr/bin/env python3.11
"""
backfill_vocabulary_from_audits — seed city_vocabulary_corrections from
existing member_quotes.gemini_correction_notes audit JSONs.

T-017 Layer 2 one-off backfill. The V3 ingest hook (shipped same chunk)
populates the table going forward; this script populates it for V3 runs
that pre-date the hook (e.g. m101091's 2026-05-16 review session).

For each audited quote it parses the audit's `raw_gemini_verdict.text_differences`
field and runs `extract_substitutions` over it. Every `"X" should be "Y"`
pattern Gemini surfaced gets upserted into `city_vocabulary_corrections`
for the quote's city — INCLUDING substitutions surfaced on disputed or
rejected quotes (the dictionary cares about word-level intelligence,
which is decoupled from the per-quote decision).

Idempotency caveat: re-running bumps applied_count on previously-seen
(city, wrong) pairs because we don't track per-source provenance inside
the dictionary row. Operator contract for V1: run this once per audit
backlog. T-018 promotion may add stricter provenance tracking later.

Usage:
    cd 02_Core_Project
    python3.11 -m zspan_pipeline.scripts.backfill_vocabulary_from_audits
    python3.11 -m zspan_pipeline.scripts.backfill_vocabulary_from_audits --dry-run
    python3.11 -m zspan_pipeline.scripts.backfill_vocabulary_from_audits --city Kingman
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

_PARSERS_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "council_navigator"
    / "parsers"
)
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

from database import get_connection, upsert_vocabulary_correction  # noqa: E402
from review_response_parser import extract_substitutions  # noqa: E402


def _iter_audited_quotes(city: str | None) -> list[dict]:
    """Yield rows of (id, city_name, gemini_correction_notes) for every
    member_quote whose audit JSON is populated. Optionally filter by city."""
    conn = get_connection()
    where = "WHERE mq.gemini_correction_notes IS NOT NULL"
    params: list = []
    if city:
        where += " AND m.city_name = ?"
        params.append(city)
    rows = conn.execute(
        f"""
        SELECT mq.id AS quote_id,
               mq.meeting_id,
               m.city_name,
               mq.gemini_correction_notes
        FROM member_quotes mq
        JOIN meetings m ON m.id = mq.meeting_id
        {where}
        ORDER BY mq.id ASC
        """,
        params,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", help="Only backfill for this city (default: all)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing.")
    args = parser.parse_args()

    rows = _iter_audited_quotes(args.city)
    if not rows:
        print("No audited quotes found.")
        return 0

    print("=" * 64)
    print(f"  T-017 Layer 2 backfill ({'dry-run' if args.dry_run else 'live'})")
    print("=" * 64)
    print(f"  Audited quotes scanned: {len(rows)}")
    if args.city:
        print(f"  Filter city: {args.city}")
    print()

    total_substitutions = 0
    total_inserted = 0
    total_bumped = 0
    skipped_no_diffs = 0
    per_city_counts: dict[str, int] = {}

    for row in rows:
        quote_id = row["quote_id"]
        city = row["city_name"]
        try:
            notes = json.loads(row["gemini_correction_notes"])
        except (json.JSONDecodeError, TypeError):
            print(f"  quote_id={quote_id:4d}  WARN bad audit JSON; skipping")
            continue

        text_differences = (
            (notes.get("raw_gemini_verdict") or {}).get("text_differences") or ""
        )
        subs = extract_substitutions(text_differences)
        if not subs:
            skipped_no_diffs += 1
            continue

        source_file = notes.get("source_response_file")
        print(f"  quote_id={quote_id:4d}  city={city:18s}  {len(subs)} subs:")
        for wrong, right in subs:
            total_substitutions += 1
            if args.dry_run:
                print(f"    DRY  {wrong!r} -> {right!r}")
                continue
            try:
                result = upsert_vocabulary_correction(
                    city_name=city, wrong=wrong, right=right,
                    source_response_file=source_file,
                )
            except ValueError as e:
                print(f"    SKIP {wrong!r} -> {right!r} ({e})")
                continue
            if result["was_new"]:
                total_inserted += 1
                tag = "NEW"
            else:
                total_bumped += 1
                tag = f"BUMP applied_count={result['applied_count']}"
            print(f"    {tag:24s} {wrong!r} -> {right!r}")
            per_city_counts[city] = per_city_counts.get(city, 0) + 1

    print()
    print("Summary:")
    print(f"  Audited quotes scanned:       {len(rows)}")
    print(f"  Quotes with no Gemini diffs:  {skipped_no_diffs}")
    print(f"  Substitutions observed:       {total_substitutions}")
    if not args.dry_run:
        print(f"  Inserted (new):               {total_inserted}")
        print(f"  Bumped (already known):       {total_bumped}")
        if per_city_counts:
            print("  Per-city totals:")
            for c, n in sorted(per_city_counts.items()):
                print(f"    {c:20s} {n}")
    else:
        print("  (dry-run — no DB writes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
