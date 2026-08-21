#!/usr/bin/env python3.11
"""enqueue_repository_candidates — V0 seed for the D-095 repository gate.

Walks `notebook_outputs` rows produced by the bridge pipeline and
bulk-creates `repository_assets` rows at status='pending_owner_review'
for the operator's repository queue. Idempotent against
`UNIQUE(source_type, source_id, asset_type)` — re-running picks up
newly-produced outputs without disturbing existing queue rows.

Per [D-095 § What this commits to](../../../01_Project_Overview/DECISIONS.md#d-095)
+ [D-006](../../../01_Project_Overview/DECISIONS.md#d-006): every
asset destined for the repository goes through the owner-approval
gate before reaching creators. This script populates the gate from
the existing notebook_outputs corpus so the operator queue is non-empty
when V1-Repo-1 ships.

The follow-up chunk wires worker.py to auto-enqueue new outputs as
they land; this script is the one-shot seed.

Usage::

    python3.11 parsers/scripts/enqueue_repository_candidates.py
    python3.11 parsers/scripts/enqueue_repository_candidates.py --dry-run
    python3.11 parsers/scripts/enqueue_repository_candidates.py --city Kingman
    python3.11 parsers/scripts/enqueue_repository_candidates.py --meeting-id 101091

Output: counts of (already_queued, newly_enqueued, skipped_unmapped)
+ a one-line summary per enqueued row when not --dry-run.

Per [D-100](../../../01_Project_Overview/DECISIONS.md#d-100):
defensive seed script. No LLM calls. Pure SQL walk + helper calls.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Make `parsers` importable when run from project root or from parsers/
_THIS = Path(__file__).resolve()
_PARSERS_DIR = _THIS.parents[1]
_NAVIGATOR = _PARSERS_DIR.parent
if str(_NAVIGATOR) not in sys.path:
    sys.path.insert(0, str(_NAVIGATOR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("enqueue_repository_candidates")

# Late imports so the sys.path adjustment above takes effect first.
from parsers import database  # noqa: E402
from parsers.repository_gate import (  # noqa: E402
    NOTEBOOK_OUTPUT_TO_ASSET_TYPE as _OUTPUT_TYPE_TO_ASSET,
    enqueue_repository_asset,
    is_legacy_notebooklm_artifact,
)

# _OUTPUT_TYPE_TO_ASSET is the canonical mapping from
# notebook_outputs.output_type → repository_assets.asset_type, shared
# with the worker.py auto-enqueue hook via repository_gate. Output types
# not in the mapping (episode_tagline, episode_tags,
# council_sentiment, suggested_questions, member_attendance,
# transcript_words, tracked_claims, quotes) are internal display
# strings or raw structured data that creators consume indirectly.


def _content_preview(content: str | None, limit: int = 200) -> str | None:
    if not content:
        return None
    flat = " ".join(content.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bulk-enqueue notebook_outputs rows into the "
                    "D-095 repository deposit gate at pending_owner_review.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be enqueued; do not write.",
    )
    parser.add_argument(
        "--city", default=None,
        help="Restrict to a single city by name (case-sensitive).",
    )
    parser.add_argument(
        "--meeting-id", type=int, default=None,
        help="Restrict to a single meeting by meetings.id.",
    )
    args = parser.parse_args(argv)

    conn = database.get_connection()
    cursor = conn.cursor()

    where_clauses = [
        "no.output_type IN ({})".format(
            ",".join("?" * len(_OUTPUT_TYPE_TO_ASSET))
        ),
        "no.error IS NULL",
    ]
    params: list[object] = list(_OUTPUT_TYPE_TO_ASSET.keys())
    if args.city is not None:
        where_clauses.append("m.city_name = ?")
        params.append(args.city)
    if args.meeting_id is not None:
        where_clauses.append("m.id = ?")
        params.append(args.meeting_id)

    cursor.execute(
        f"""
        SELECT no.id, no.meeting_id, no.notebook_id, no.output_type,
               no.content, no.generated_at, m.city_name, m.meeting_date,
               m.meeting_title
          FROM notebook_outputs no
          JOIN meetings m ON m.id = no.meeting_id
         WHERE {" AND ".join(where_clauses)}
         ORDER BY no.generated_at DESC
        """,
        params,
    )
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        logger.info("No eligible notebook_outputs rows to enqueue.")
        return 0

    already_queued = 0
    newly_enqueued = 0
    skipped_legacy = 0
    errors = 0

    for row in rows:
        output_id = row["id"]
        output_type = row["output_type"]
        asset_type = _OUTPUT_TYPE_TO_ASSET.get(output_type)
        if asset_type is None:
            continue  # defensive — WHERE clause already excludes these

        preview = _content_preview(row["content"])

        # Pre-D-126 legacy meta-response gate (added 2026-06-21
        # per brainstorm-audit F3). The list_pending_review_assets layer
        # already filters these at read time (commit ff99f5d) — this is
        # the matching write-time guard so the DB doesn't accumulate
        # hidden rows. Per D-126 the entire pre-V1-RAG-3
        # content class is architecturally retired; rows with that
        # content shape have no V1 path AND the Creator Network
        # downstream surface that would consume them is itself V1+
        # roadmap — there's no scenario where enqueueing them helps.
        if is_legacy_notebooklm_artifact(preview):
            skipped_legacy += 1
            continue

        metadata = {
            "output_type": output_type,
            "notebook_id": row["notebook_id"],
            "generated_at": row["generated_at"],
            "city": row["city_name"],
            "meeting_date": row["meeting_date"],
            "meeting_title": row["meeting_title"],
            "preview": preview,
        }

        label = (
            f"output#{output_id} {output_type!r} → asset_type={asset_type} "
            f"({row['city_name']} · {row['meeting_date']})"
        )

        if args.dry_run:
            logger.info("[dry-run] would enqueue %s", label)
            newly_enqueued += 1
            continue

        try:
            asset = enqueue_repository_asset(
                source_type="notebook_output",
                source_id=output_id,
                source_meeting_id=row["meeting_id"],
                asset_type=asset_type,
                asset_metadata=metadata,
                initial_status="pending_owner_review",
            )
            # enqueue_repository_asset returns the existing row if the
            # UNIQUE tuple collides. Distinguish via queued_at: if it
            # matches an existing earlier timestamp, we're idempotent
            # no-op. The simplest signal is the row's repository_status:
            # if it's not pending_owner_review, this run didn't create
            # it; if it IS, queued_at < just-now means a prior run did.
            # We don't track that precisely; the count below treats
            # everything as enqueued (collisions show up as 'already'
            # only when queue_at < this script's start which we don't
            # measure). For V0 this granularity is sufficient — the
            # log line distinguishes.
            logger.info(
                "enqueued (or already present) asset#%d %s status=%s",
                asset.id, label, asset.repository_status,
            )
            newly_enqueued += 1
        except Exception as exc:  # pragma: no cover — defensive
            logger.error("failed to enqueue %s: %s", label, exc)
            errors += 1

    logger.info(
        "Done. processed=%d enqueued_or_present=%d skipped_legacy_notebooklm=%d errors=%d",
        len(rows), newly_enqueued, skipped_legacy, errors,
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
