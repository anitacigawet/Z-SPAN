#!/usr/bin/env python3.11
"""normalize_meeting_dates — one-shot backfill of `meetings.meeting_date` to ISO.

Context: 891 rows in the cache had non-ISO meeting_date strings as of
2026-06-25 because the prior `normalize._to_iso_date()` silently no-op'd
when dateutil wasn't installed in the running Python env (F8 violation
caught during the 2026-06-25 overnight Maricopa orchestrator run). The
normalizer was rewritten to use stdlib-only logic for common formats +
loud-fail for unknown. This script applies that normalizer retroactively.

What it does:
  1. Backs up meetings_cache.db to meetings_cache.db.bak.<epoch> first
     (irreversible operation — safety net).
  2. Walks every row in `meetings` where meeting_date is non-NULL +
     non-ISO (matches NOT GLOB '????-??-??' AND NOT GLOB '????-??-??T*').
  3. For each, runs `normalize._to_iso_date(row.meeting_date)`.
  4. UPDATEs the row with the ISO form if the normalizer succeeded.
  5. Logs per-row before/after; per-city + total summaries at end.
  6. Any unparseable row gets logged with a WARNING and left as-is
     (caller can decide whether to delete, manually fix, or extend the
     normalizer's format list).

Usage:
    python3.11 parsers/scripts/normalize_meeting_dates.py --dry-run
    python3.11 parsers/scripts/normalize_meeting_dates.py --apply

--dry-run shows the per-row plan without writing. --apply writes after
backup. NO partial-state mid-run: each row is its own UPDATE so a
kill-mid-run leaves a consistent (mixed) DB rather than corruption.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

# Make parsers/ importable so we can use the normalizer
_PARSERS_DIR = Path(__file__).resolve().parent.parent
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

from normalize import _to_iso_date  # noqa: E402

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(levelname)s | %(message)s')

DB_PATH = _PARSERS_DIR / "meetings_cache.db"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Show plan; don't write")
    parser.add_argument("--apply", action="store_true", help="Actually write changes (after backup)")
    args = parser.parse_args()

    if not args.dry_run and not args.apply:
        print("Pass --dry-run to preview or --apply to commit. Aborting.")
        return 2

    if not DB_PATH.exists():
        print(f"DB not found at {DB_PATH}")
        return 1

    # Backup before any --apply touches data.
    if args.apply:
        epoch = int(time.time())
        backup_path = DB_PATH.with_suffix(f".db.bak.{epoch}")
        print(f"Backing up DB to {backup_path}")
        shutil.copy2(DB_PATH, backup_path)
        print(f"✅ Backup complete ({backup_path.stat().st_size / 1024 / 1024:.1f} MB)")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Find non-ISO rows (anything that doesn't match YYYY-MM-DD or YYYY-MM-DDT*)
    cur.execute("""
        SELECT id, city_name, meeting_date, meeting_title
        FROM meetings
        WHERE meeting_date IS NOT NULL
          AND meeting_date != ''
          AND meeting_date NOT GLOB '????-??-??'
          AND meeting_date NOT GLOB '????-??-??T*'
        ORDER BY city_name, id
    """)
    rows = cur.fetchall()
    total_non_iso = len(rows)
    print(f"\nFound {total_non_iso} non-ISO rows to process")

    # Group by city for reporting
    by_city: dict[str, dict] = defaultdict(lambda: {"converted": 0, "dup_deleted": 0, "unparseable": 0, "samples": []})
    converted_count = 0
    dup_deleted_count = 0
    unparseable_count = 0
    unparseable_samples: list[tuple[int, str, str]] = []

    for r in rows:
        city = r["city_name"] or "<no city>"
        original = r["meeting_date"]
        normalized = _to_iso_date(original)
        if normalized and len(normalized) == 10 and normalized[4] == '-':
            by_city[city]["samples"].append((r["id"], original, normalized)) if len(by_city[city]["samples"]) < 3 else None
            if args.apply:
                try:
                    cur.execute(
                        "UPDATE meetings SET meeting_date=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (normalized, r["id"]),
                    )
                    converted_count += 1
                    by_city[city]["converted"] += 1
                except sqlite3.IntegrityError:
                    # UNIQUE constraint on (city_name, state, meeting_date,
                    # meeting_title) means an ISO-format duplicate already
                    # exists for this meeting. Delete the non-ISO row — the
                    # ISO version is the canonical one.
                    cur.execute("DELETE FROM meetings WHERE id=?", (r["id"],))
                    dup_deleted_count += 1
                    by_city[city]["dup_deleted"] += 1
            else:
                # Dry-run: just count as converted; we don't know which would
                # collide without trying.
                converted_count += 1
                by_city[city]["converted"] += 1
        else:
            unparseable_count += 1
            by_city[city]["unparseable"] += 1
            unparseable_samples.append((r["id"], city, original))

    if args.apply:
        conn.commit()
        print(f"\n✅ Committed: {converted_count} UPDATEs + {dup_deleted_count} DELETEs (duplicate-of-ISO rows)")

    conn.close()

    # Per-city report
    print("\n=== Per-city outcome ===")
    print(f"{'City':25} {'updated':>8} {'dup-del':>8} {'unparse':>8}  sample (orig → ISO)")
    for city in sorted(by_city.keys()):
        d = by_city[city]
        sample_text = ""
        if d["samples"]:
            s = d["samples"][0]
            sample_text = f"  m{s[0]}: {s[1]!r} → {s[2]}"
        print(f"{city:25} {d['converted']:>8} {d['dup_deleted']:>8} {d['unparseable']:>8}{sample_text}")

    print("\n=== Summary ===")
    print(f"  Total non-ISO rows:    {total_non_iso}")
    print(f"  Converted to ISO:      {converted_count}")
    print(f"  Duplicate-of-ISO deleted: {dup_deleted_count}")
    print(f"  Unparseable (skipped): {unparseable_count}")
    print(f"  Mode:                  {'APPLY' if args.apply else 'DRY-RUN (no writes)'}")

    if unparseable_samples:
        print("\n⚠️  Unparseable samples (left as-is for manual triage):")
        for mid, city, orig in unparseable_samples[:25]:
            print(f"  m{mid} [{city}]: {orig!r}")
        if len(unparseable_samples) > 25:
            print(f"  ... + {len(unparseable_samples) - 25} more (full list in DB)")
        print("  Add the format to normalize._STDLIB_FORMATS if a legitimate variant.")

    return 0 if unparseable_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
