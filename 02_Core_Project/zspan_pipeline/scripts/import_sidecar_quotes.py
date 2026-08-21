#!/usr/bin/env python3
"""import_sidecar_quotes — one-shot backfill from `.preview/m<id>.json` sidecars into the canonical `quotes` DB table.

Fixes the gap surfaced session-32 (2026-07-04): the sidecar_pipeline.py
extraction stage writes quotes to `.preview/m<id>.json` with full
word_timings + video_timestamp_seconds, but nothing was importing them
into the `quotes` table that BroadcastPage's karaoke reads. Meetings
processed after the D-143 subsystem retirement (2026-07-01) landed
with sidecar quotes but zero DB rows — the highlights disappeared.

Behavior:
  - Enumerates every `.preview/m<id>.json` file
  - Parses the `quotes[]` array, reconstructs ExtractedQuote objects via
    the existing from_dict() adapter
  - Calls persist_extracted_quotes for each meeting (idempotent UPSERT —
    verification state on existing rows is preserved by save_quotes_batch)
  - Runs the word-timing alignment pass so the karaoke seek fires
  - Prints a per-meeting summary + a session total

Idempotent — safe to re-run. Skips meetings whose sidecar has zero
quotes (nothing to import). Skips meetings whose city_name can't be
resolved from the DB.

Usage:
    .venv-worker/bin/python \\
        -m zspan_pipeline.scripts.import_sidecar_quotes            # dry-run
    .venv-worker/bin/python \\
        -m zspan_pipeline.scripts.import_sidecar_quotes --apply    # write
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Resolve project layout — this file is at
# `02_Core_Project/zspan_pipeline/scripts/import_sidecar_quotes.py`.
# PREVIEW_DIR lives at repo-root `.preview/`.
_THIS = Path(__file__).resolve()
PROJECT_ROOT = _THIS.parents[3]
PREVIEW_DIR = PROJECT_ROOT / ".preview"

# Make zspan_pipeline + parsers importable.
_ZSPAN_PARENT = _THIS.parents[2]  # 02_Core_Project/
sys.path.insert(0, str(_ZSPAN_PARENT))
sys.path.insert(0, str(_ZSPAN_PARENT / "council_navigator" / "parsers"))

from zspan_pipeline.qdrant_quote_extractor import (
    ExtractedQuote,
    persist_extracted_quotes,
)
from database import get_connection


logger = logging.getLogger("import_sidecar_quotes")


def _meeting_city(meeting_id: int) -> str | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT city_name FROM meetings WHERE id = ?", (meeting_id,),
        ).fetchone()
        return row["city_name"] if row else None
    finally:
        conn.close()


def _load_sidecar_quotes(sidecar_path: Path) -> tuple[int | None, list[ExtractedQuote]]:
    """Parse one sidecar file. Returns (meeting_id, quotes).

    Skips gracefully on malformed files — logs the failure and returns
    (None, []).
    """
    try:
        raw = json.loads(sidecar_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("skip %s — parse failed: %s", sidecar_path.name, exc)
        return None, []

    meeting_id = raw.get("meeting_id")
    if not isinstance(meeting_id, int):
        logger.warning("skip %s — no meeting_id", sidecar_path.name)
        return None, []

    raw_quotes = raw.get("quotes") or []
    if not isinstance(raw_quotes, list):
        return meeting_id, []

    quotes: list[ExtractedQuote] = []
    for q in raw_quotes:
        if not isinstance(q, dict):
            continue
        try:
            quotes.append(ExtractedQuote.from_dict(q))
        except Exception as exc:
            logger.warning(
                "  meeting=%d — skipping malformed quote: %s", meeting_id, exc,
            )
    return meeting_id, quotes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write to the DB. Without this, runs dry (parses + counts only).",
    )
    parser.add_argument(
        "--meeting-id",
        type=int,
        default=None,
        help="Import only this meeting_id (default: all sidecars).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not PREVIEW_DIR.exists():
        logger.error("PREVIEW_DIR does not exist: %s", PREVIEW_DIR)
        return 1

    # Only the top-level m<id>.json files — the *_decisions.json /
    # *_recusals.json / *_routing.json sidecars are separate stages
    # without a quotes[] array to import.
    sidecars = sorted(
        p for p in PREVIEW_DIR.glob("m*.json")
        if p.name.count("_") == 0  # excludes m<id>_decisions.json etc.
    )
    if args.meeting_id is not None:
        sidecars = [p for p in sidecars if p.name == f"m{args.meeting_id}.json"]

    if not sidecars:
        logger.warning("no matching sidecars in %s", PREVIEW_DIR)
        return 0

    logger.info(
        "found %d sidecar(s) — mode: %s",
        len(sidecars), "APPLY" if args.apply else "DRY-RUN",
    )

    total_saved = 0
    total_updated = 0
    total_skipped = 0
    processed_meetings = 0
    skipped_no_quotes = 0
    skipped_no_city = 0

    for sidecar_path in sidecars:
        meeting_id, quotes = _load_sidecar_quotes(sidecar_path)
        if meeting_id is None:
            continue

        if not quotes:
            skipped_no_quotes += 1
            continue

        city_name = _meeting_city(meeting_id)
        if not city_name:
            logger.warning(
                "meeting=%d — city_name not found in DB, skipping", meeting_id,
            )
            skipped_no_city += 1
            continue

        if not args.apply:
            logger.info(
                "  meeting=%d city=%s — would import %d quote(s)",
                meeting_id, city_name, len(quotes),
            )
            continue

        try:
            stats = persist_extracted_quotes(
                meeting_id=meeting_id,
                city_name=city_name,
                quotes=quotes,
                align_word_timings=True,
            )
        except Exception as exc:
            logger.exception(
                "meeting=%d city=%s — persist failed: %s",
                meeting_id, city_name, exc,
            )
            continue

        saved = stats.get("saved", 0)
        updated = stats.get("updated", 0)
        skipped = stats.get("skipped_invalid", 0)
        total_saved += saved
        total_updated += updated
        total_skipped += skipped
        processed_meetings += 1
        logger.info(
            "  meeting=%d city=%s — saved=%d updated=%d skipped=%d "
            "misses=%d alignment=%s",
            meeting_id, city_name,
            saved, updated, skipped,
            stats.get("member_lookup_misses", 0),
            stats.get("alignment"),
        )

    print()
    print("═══ Summary ═══")
    print(f"Sidecars scanned:          {len(sidecars)}")
    print(f"Sidecars with zero quotes: {skipped_no_quotes}")
    print(f"Skipped (no city_name):    {skipped_no_city}")
    if args.apply:
        print(f"Meetings processed:        {processed_meetings}")
        print(f"Quotes newly saved:        {total_saved}")
        print(f"Quotes updated (UPSERT):   {total_updated}")
        print(f"Quotes skipped (invalid):  {total_skipped}")
    else:
        print(f"Mode: DRY-RUN (re-run with --apply to write)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
