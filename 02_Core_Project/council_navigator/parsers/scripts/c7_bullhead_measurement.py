#!/usr/bin/env python3.11
"""c7_bullhead_measurement — V1-Consensus-1 C7 one-shot Bullhead-trio + CC measurement.

One-shot script. Ran 2026-06-22 after operator authorization to drain
the 142 unpolished V1-RAG-3 quotes through the polish + consensus
pipeline. Result: 142/142 polished cleanly + 0 polish-rejections fired
(see V1_CONSENSUS_1_SPEC.md § Shipping notes for the full empirical
breakdown). Kept in tree as the worked-example artifact for the C7
measurement; future per-batch measurements should use the generic
`measure_consensus_pipeline.py` instead of re-running this script.

Meetings in scope:
  m103223  Bullhead City — Special Council Executive Session 6/2  (45 quotes)
  m103224  Bullhead City — Regular Council 6/2                    (38 quotes)
  m103225  Bullhead City — Regular Council 5/19                   (54 quotes)
  m103983  Colorado City — JUNE PLANNING COMMISSION                (5 quotes)

Total: 142 quotes. Pre-compute fires polish_for_display per quote; the
C4 wiring routes any polish-rejection events to correction_pending_review
with city + meeting + first-token diff + phonetic-variant detection.

Output: full pre-compute stats + post-run measurement read. JSON to stdout.
"""
from __future__ import annotations

import json
import os
import sys
import time

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARSERS_DIR = os.path.dirname(_THIS_DIR)
if _PARSERS_DIR not in sys.path:
    sys.path.insert(0, _PARSERS_DIR)

from database import init_db, get_connection, list_pending_review_rows  # noqa: E402
from quote_display_precompute import precompute_display_cache_for_quote_ids  # noqa: E402


MEETINGS = [
    ("Bullhead City — Special Executive Session 6/2", 103223),
    ("Bullhead City — Regular Council 6/2", 103224),
    ("Bullhead City — Regular Council 5/19", 103225),
    ("Colorado City — June Planning Commission", 103983),
]


def main() -> int:
    init_db()

    # Snapshot pending_review BEFORE — Kingman + Bullhead + Colorado City
    conn = get_connection()
    pending_before = conn.execute(
        "SELECT COUNT(*) AS n FROM correction_pending_review WHERE status='pending'"
    ).fetchone()["n"]
    conn.close()

    # Gather quote IDs across all meetings
    quote_ids = []
    conn = get_connection()
    try:
        for label, mid in MEETINGS:
            ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM quotes WHERE meeting_id = ? "
                    "AND quote_text IS NOT NULL "
                    "AND quote_text_display IS NULL "
                    "ORDER BY id",
                    (mid,),
                ).fetchall()
            ]
            quote_ids.extend(ids)
            print(
                f"  {label}: {len(ids)} unpolished quotes queued",
                file=sys.stderr,
                flush=True,
            )
    finally:
        conn.close()

    print(
        f"\nTotal unpolished quotes queued: {len(quote_ids)}",
        file=sys.stderr,
        flush=True,
    )

    if not quote_ids:
        print(json.dumps({"requested": 0, "msg": "nothing to do"}, indent=2))
        return 0

    print("Firing precompute (parallel polish via Codex CLI)...",
          file=sys.stderr, flush=True)
    t0 = time.monotonic()
    result = precompute_display_cache_for_quote_ids(quote_ids)
    elapsed = time.monotonic() - t0
    result["wall_clock_seconds"] = round(elapsed, 1)

    # Snapshot pending_review AFTER
    conn = get_connection()
    pending_after_total = conn.execute(
        "SELECT COUNT(*) AS n FROM correction_pending_review WHERE status='pending'"
    ).fetchone()["n"]
    new_pending = pending_after_total - pending_before
    by_meeting_new = {}
    for label, mid in MEETINGS:
        n = conn.execute(
            """
            SELECT COUNT(*) AS n FROM correction_pending_review
            WHERE meeting_id = ? AND status='pending'
            """,
            (mid,),
        ).fetchone()["n"]
        by_meeting_new[label] = n

    # Sample the new pending rows
    sample_rows = conn.execute(
        """
        SELECT id, meeting_id, city_name, wrong_token, right_token,
               is_phonetic_variant, original_text, polished_proposal
        FROM correction_pending_review
        WHERE meeting_id IN (?, ?, ?, ?)
        ORDER BY id DESC
        LIMIT 20
        """,
        (103223, 103224, 103225, 103983),
    ).fetchall()
    sample = [
        {
            "id": r["id"],
            "meeting_id": r["meeting_id"],
            "city_name": r["city_name"],
            "wrong_token": r["wrong_token"],
            "right_token": r["right_token"],
            "is_phonetic_variant": bool(r["is_phonetic_variant"]),
            "original_excerpt": (r["original_text"] or "")[:120],
            "polished_excerpt": (r["polished_proposal"] or "")[:120],
        }
        for r in sample_rows
    ]
    conn.close()

    print(
        json.dumps(
            {
                "precompute_stats": result,
                "pending_review_delta": {
                    "before": pending_before,
                    "after_total": pending_after_total,
                    "new_rows_this_run": new_pending,
                    "by_meeting": by_meeting_new,
                },
                "sample_pending_rows": sample,
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
