"""Profile cached transcripts for anomalies and rebuild stale indexes.

No audio is read and Whisper is never invoked.  The command updates only the
existing ``notebook_outputs.transcript_words`` JSON and, unless requested
otherwise, rebuilds a stale local retrieval index from the annotated words.

Examples::

    python -m zspan_pipeline.scripts.backfill_transcript_quarantine --dry-run \
        --meeting-id 127899 --meeting-id 127900 --meeting-id 127696

    python -m zspan_pipeline.scripts.backfill_transcript_quarantine --run \
        --meeting-id 127899 --meeting-id 127900 --meeting-id 127696
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from zspan_pipeline import local_vector_store
from zspan_pipeline.transcript_quarantine import (
    apply_degenerate_span_quarantine,
    log_quarantine_result,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackfillOutcome:
    meeting_id: int
    detector_ran: bool
    transcript_changed: bool
    quarantined_word_count: int
    span_count: int
    entropy_region_count: int
    entropy_only_review_region_count: int
    corroborated_span_count: int
    index_was_stale: bool
    reindexed: bool
    dry_run: bool


def _meeting_ids_with_transcripts(db_path: Path | str) -> list[int]:
    with local_vector_store.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT meeting_id
            FROM notebook_outputs
            WHERE output_type = 'transcript_words'
              AND error IS NULL
              AND content IS NOT NULL
            ORDER BY meeting_id
            """
        ).fetchall()
    return [int(row["meeting_id"]) for row in rows]


def backfill_meeting(
    meeting_id: int,
    *,
    db_path: Path | str,
    dry_run: bool,
    reindex: bool = True,
    index_fn: Optional[Callable[..., int]] = None,
) -> BackfillOutcome:
    """Run one idempotent annotation/re-index decision for a cached meeting."""
    transcript = local_vector_store.load_transcript_words(
        meeting_id, db_path=db_path,
    )
    quarantine = apply_degenerate_span_quarantine(transcript)
    log_quarantine_result(meeting_id, quarantine)
    index_was_stale = not local_vector_store.meeting_index_matches_transcript(
        meeting_id, transcript, db_path=db_path,
    )

    reindexed = False
    if dry_run:
        logger.info(
            "backfill transcript quarantine meeting=%d dry_run=True "
            "would_change=%s index_was_stale=%s would_reindex=%s",
            meeting_id,
            quarantine.changed,
            index_was_stale,
            reindex and index_was_stale,
        )
    else:
        if quarantine.changed:
            local_vector_store.save_transcript_words(
                meeting_id, transcript, db_path=db_path,
            )
        if reindex and index_was_stale:
            if index_fn is None:
                from zspan_pipeline.worker import index_meeting_locally

                index_fn = index_meeting_locally
            chunk_count = index_fn(meeting_id, db_path=db_path)
            reindexed = True
            logger.info(
                "backfill transcript quarantine meeting=%d reindexed=True "
                "chunks=%d",
                meeting_id,
                chunk_count,
            )
        else:
            logger.info(
                "backfill transcript quarantine meeting=%d reindexed=False "
                "reason=%s",
                meeting_id,
                (
                    "reindex_disabled"
                    if not reindex
                    else "index_already_matches_annotated_transcript"
                ),
            )

    return BackfillOutcome(
        meeting_id=meeting_id,
        detector_ran=quarantine.detector_ran,
        transcript_changed=quarantine.changed,
        quarantined_word_count=quarantine.quarantined_word_count,
        span_count=len(quarantine.spans),
        entropy_region_count=len(quarantine.entropy_regions),
        entropy_only_review_region_count=quarantine.entropy_only_region_count,
        corroborated_span_count=quarantine.corroborated_span_count,
        index_was_stale=index_was_stale,
        reindexed=reindexed,
        dry_run=dry_run,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--run", action="store_true")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--meeting-id", type=int, action="append")
    selection.add_argument("--all-transcripts", action="store_true")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=local_vector_store.DEFAULT_DB_PATH,
    )
    parser.add_argument(
        "--no-reindex",
        action="store_true",
        help="Persist annotations without rebuilding stale local indexes.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    meeting_ids = (
        _meeting_ids_with_transcripts(args.db_path)
        if args.all_transcripts
        else sorted(set(args.meeting_id or []))
    )
    logger.info(
        "backfill transcript quarantine selected=%d dry_run=%s reindex=%s db=%s",
        len(meeting_ids),
        args.dry_run,
        not args.no_reindex,
        args.db_path,
    )

    failed = 0
    for meeting_id in meeting_ids:
        try:
            backfill_meeting(
                meeting_id,
                db_path=args.db_path,
                dry_run=args.dry_run,
                reindex=not args.no_reindex,
            )
        except Exception:
            failed += 1
            logger.exception(
                "backfill transcript quarantine meeting=%d failed",
                meeting_id,
            )
    logger.info(
        "backfill transcript quarantine completed selected=%d failed=%d",
        len(meeting_ids),
        failed,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
