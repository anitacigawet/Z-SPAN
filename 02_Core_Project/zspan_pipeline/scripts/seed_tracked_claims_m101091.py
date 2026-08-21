#!/usr/bin/env python3.11
"""
seed_tracked_claims_m101091 — hand-curated stub seed for the T-012 ledger.

While the `tracked_claims.md` prompt is being authored by James, this
script seeds 3 real claims from m101091 (Apr 21 Kingman City Council)
so the Accountability section on the Cast page + the City Ledger page
have data to render. Each claim is a verbatim selection from an existing
member_quote — the seed exercises the same alignment + karaoke path the
production extraction will use.

When the real prompt ships, the operator can either:
  (a) Re-run the extraction on m101091 to overwrite these (the batch
      save first DELETEs the meeting's rows), OR
  (b) Leave them as historical stubs and let the prompt populate
      future meetings.

The seed picks Jamie Scott Stehly's "extra staff" assurance as the
canonical example James named when proposing T-012 (the inspiration
for the entire layer).

Usage:
    cd 02_Core_Project
    python3.11 -m zspan_pipeline.scripts.seed_tracked_claims_m101091
    python3.11 -m zspan_pipeline.scripts.seed_tracked_claims_m101091 --dry-run
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

from database import save_tracked_claims_batch, get_connection  # noqa: E402
from quote_align import align_tracked_claims_for_meeting  # noqa: E402


MEETING_ID = 101091
CITY = "Kingman"


# Three hand-curated tracked claims drawn from m101091's verbatim quotes.
# These mirror what a well-tuned `tracked_claims.md` extraction would
# surface — but selected by hand so the operator has stable seed data
# while the prompt is being authored.
SEED_CLAIMS = [
    {
        # The canonical example — Jamie's confirmation that bringing in
        # extra staff for a security detail will NOT reduce normal
        # patrol coverage. T-012's inspiring case (James named this one).
        "speaker": "Jamie Scott Stehly",
        "claim_type": "assurance",
        "claim_text": (
            "And you're bringing in extra staff so it's not taking any "
            "officers off the streets that would normally be there during "
            "that time"
        ),
        "expected_outcome": (
            "Police-deployment data shows total patrol hours did not "
            "decrease during the security-detail window."
        ),
        "time_horizon_months": 6,
        "topic_tags": ["public_safety"],
        "confidence": "high",
        "context": (
            "Council Q&A with Kingman PD about staffing a security detail; "
            "Jamie confirms with staff that the detail won't pull officers "
            "off normal patrol."
        ),
    },
    {
        # Ken Watkins's motion to approve the ADA project — a positive
        # commitment to a specific scope ("from the stated streets of
        # Johnson to Michael"). Tracked because if the project doesn't
        # land or scope-creeps, the ledger should surface that.
        "speaker": "Ken Watkins",
        "claim_type": "commitment",
        "claim_text": (
            "I would make a motion I guess Carl is that what we want to do "
            "on this one yes sir okay that we accept staff recommendation "
            "of an ADA barrier removal project from the stated streets of "
            "Johnson to Michael"
        ),
        "expected_outcome": (
            "ADA barrier removal project completes across the Johnson-to-"
            "Michael stretch within the city's capital improvement timeline."
        ),
        "time_horizon_months": 12,
        "topic_tags": ["infrastructure"],
        "confidence": "high",
        "context": (
            "Motion to accept staff recommendation for ADA barrier removal "
            "along Stockton/Andy Devine corridor."
        ),
    },
    {
        # Jamie's explicit request for a follow-up at the next meeting on
        # the Beale Street streetscape dead-tree issue. Concrete enough
        # to track: by the next council meeting, is the report on the
        # agenda? If not, the assurance has aged past its horizon.
        "speaker": "Jamie Scott Stehly",
        "claim_type": "commitment",
        "claim_text": (
            "yes I would like to get a report at the next meeting on the "
            "dead trees that are on Beale Street in the new streetscape"
        ),
        "expected_outcome": (
            "The next regular Kingman City Council meeting agenda includes "
            "a staff report on the dead trees in the Beale Street streetscape."
        ),
        "time_horizon_months": 1,
        "topic_tags": ["infrastructure", "other"],
        "confidence": "high",
        "context": (
            "Beale Street streetscape discussion — Jamie publicly commits "
            "to surfacing the issue and requests a follow-up report."
        ),
    },
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without saving.")
    args = parser.parse_args()

    print("=" * 64)
    print(f"  Seed T-012 tracked_claims for m{MEETING_ID} "
          f"({'dry-run' if args.dry_run else 'live'})")
    print("=" * 64)

    # Sanity: meeting must exist.
    conn = get_connection()
    row = conn.execute(
        "SELECT id, city_name, meeting_title FROM meetings WHERE id = ?",
        (MEETING_ID,),
    ).fetchone()
    if not row:
        print(f"ERROR: no meeting with id={MEETING_ID}")
        conn.close()
        return 1
    print(f"  Meeting: {dict(row)}")
    print(f"  Claims to seed: {len(SEED_CLAIMS)}")
    for c in SEED_CLAIMS:
        print(f"    - [{c['claim_type']:11s}] {c['speaker']}: "
              f"{c['claim_text'][:70]}...")
    conn.close()

    if args.dry_run:
        print("\n(dry-run — no DB writes)")
        return 0

    result = save_tracked_claims_batch(MEETING_ID, CITY, SEED_CLAIMS)
    print(f"\n  save_tracked_claims_batch: {result}")

    # Try to align immediately so the karaoke works on first render.
    print("\n  Aligning against Whisper transcript...")
    stats = align_tracked_claims_for_meeting(MEETING_ID)
    print(f"  align stats: {stats}")

    # Show the seeded rows.
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT tc.id, cm.name AS speaker, tc.claim_type, tc.status,
               LENGTH(tc.word_timings) AS wt_chars
        FROM tracked_claims tc
        JOIN council_members cm ON cm.id = tc.member_id
        WHERE tc.meeting_id = ?
        ORDER BY tc.id
        """,
        (MEETING_ID,),
    ).fetchall()
    print()
    for r in rows:
        wt_state = "aligned" if r["wt_chars"] else "no_alignment"
        print(f"  id={r['id']:3d}  {r['speaker']:24s}  "
              f"{r['claim_type']:11s}  status={r['status']:9s}  {wt_state}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
