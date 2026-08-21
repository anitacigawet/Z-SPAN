"""backfill_sidecars.py — produce .preview sidecars for already-V1-RAG-3-
processed meetings that pre-date the worker-side sidecar wiring.

Background: the worker now produces .preview/m<id>*.json sidecars as part
of normal WO processing (zspan_pipeline/sidecar_pipeline.py, wired into
worker.py 2026-06-24). But meetings that completed BEFORE that wiring
landed don't have sidecars — only m103753 had them, and only because they
were hand-produced during the design session.

This script catches them up. For each meeting with v1-rag-3 outputs cached
in notebook_outputs but no .preview/m<id>.json sidecar on disk, it runs
the full sidecar_pipeline.run_pipeline. Sequential per D-005 (single-flight
Sonnet calls, no parallel runs against `claude -p`).

Wall-clock + cost (empirically per m103753): ~30 min and ~$1-3 Sonnet
metering per meeting. Total backfill for the 11 Mohave meetings ≈ 5-6
hours + ~$15-30. Run in background; surface progress as each completes.

Usage:
    # Dry-run — show what would be processed, don't run anything:
    python -m zspan_pipeline.scripts.backfill_sidecars --dry-run

    # Backfill all V1-RAG-3-processed meetings that lack sidecars:
    python -m zspan_pipeline.scripts.backfill_sidecars --run

    # Backfill specific meeting(s) regardless of sidecar presence:
    python -m zspan_pipeline.scripts.backfill_sidecars --run \\
        --meeting-id 104714 --meeting-id 103983
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from pathlib import Path

# Make zspan_pipeline + parsers importable from any cwd.
_THIS_DIR = Path(__file__).resolve().parent
_BRIDGE_DIR = _THIS_DIR.parent
_REPO_ROOT = _BRIDGE_DIR.parent.parent
sys.path.insert(0, str(_BRIDGE_DIR.parent))
sys.path.insert(0, str(_REPO_ROOT / "02_Core_Project" / "council_navigator" / "parsers"))

from zspan_pipeline import sidecar_pipeline  # noqa: E402

logger = logging.getLogger(__name__)

DB_PATH = (
    _REPO_ROOT
    / "02_Core_Project"
    / "council_navigator"
    / "parsers"
    / "meetings_cache.db"
)
PREVIEW_DIR = _REPO_ROOT / ".preview"


def _list_v1_rag3_meetings() -> list[dict]:
    """Return all meetings with v1-rag-3 cached outputs, ordered by date desc."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT m.id AS meeting_id, m.city_name, m.meeting_title,
                   m.meeting_date
            FROM meetings m
            INNER JOIN notebook_outputs no
                ON no.meeting_id = m.id
                AND no.prompt_version LIKE 'v1-rag-3%'
            ORDER BY m.meeting_date DESC, m.id DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _has_sidecar(meeting_id: int) -> bool:
    """Sidecar-present check — m<id>.json + m<id>_decisions.json both exist."""
    quotes = PREVIEW_DIR / f"m{meeting_id}.json"
    decisions = PREVIEW_DIR / f"m{meeting_id}_decisions.json"
    return quotes.exists() and decisions.exists()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dry-run", action="store_true",
        help="List what would be backfilled; don't run anything.",
    )
    p.add_argument(
        "--run", action="store_true",
        help="Actually run the backfill. Required to make changes.",
    )
    p.add_argument(
        "--meeting-id", type=int, action="append",
        help="Specific meeting id(s) to backfill (override sidecar-presence check). "
             "Repeatable.",
    )
    p.add_argument(
        "--force", action="store_true",
        help="Re-run even when sidecars already exist.",
    )
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not args.dry_run and not args.run:
        p.error("Pass --dry-run or --run.")

    # Build candidate list.
    if args.meeting_id:
        # Operator named specific meetings — look them up + use directly.
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        try:
            placeholders = ",".join("?" * len(args.meeting_id))
            rows = conn.execute(
                f"SELECT id AS meeting_id, city_name, meeting_title, meeting_date "
                f"FROM meetings WHERE id IN ({placeholders})",
                args.meeting_id,
            ).fetchall()
            candidates = [dict(r) for r in rows]
        finally:
            conn.close()
    else:
        candidates = _list_v1_rag3_meetings()

    # Filter to those missing sidecars (unless --force).
    if not args.force:
        eligible = [c for c in candidates if not _has_sidecar(c["meeting_id"])]
    else:
        eligible = candidates

    skipped = [c for c in candidates if c not in eligible]

    logger.info(
        "backfill_sidecars: %d candidate(s), %d eligible, %d already have sidecars",
        len(candidates), len(eligible), len(skipped),
    )
    for c in candidates:
        marker = "→ RUN" if c in eligible else "  skip (sidecar exists)"
        logger.info(
            "  %s  m%s · %s · %s · %s",
            marker, c["meeting_id"], c.get("city_name", "?"),
            c.get("meeting_title", "?"), c.get("meeting_date", "?"),
        )

    if args.dry_run:
        logger.info("(dry-run — no work performed)")
        return 0

    if not eligible:
        logger.info("Nothing to do.")
        return 0

    started = time.monotonic()
    succeeded: list[int] = []
    failed: list[tuple[int, str]] = []

    for i, c in enumerate(eligible, start=1):
        mid = c["meeting_id"]
        city = c.get("city_name") or ""
        logger.info(
            "\n=== [%d/%d] meeting=%d city=%s — %s (%s) ===",
            i, len(eligible), mid, city,
            c.get("meeting_title", "?"), c.get("meeting_date", "?"),
        )
        try:
            sidecar_pipeline.run_pipeline(mid, city)
            succeeded.append(mid)
        except Exception as exc:
            logger.exception("meeting=%d FAILED: %s", mid, exc)
            failed.append((mid, str(exc)))

    elapsed = time.monotonic() - started
    logger.info(
        "\nbackfill_sidecars done in %.1fs (%.1f min) — %d succeeded, %d failed",
        elapsed, elapsed / 60.0, len(succeeded), len(failed),
    )
    if succeeded:
        logger.info("  ✅ succeeded: %s", succeeded)
    if failed:
        logger.warning("  ❌ failed:")
        for mid, msg in failed:
            logger.warning("     m%d: %s", mid, msg[:200])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
