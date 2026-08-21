#!/usr/bin/env python3.11
"""
One-shot Whisper transcript test for a single meeting.

Used to validate the T-009 Phase 0a pipeline end-to-end on ONE meeting
before turning Whisper on for the full pilot (per `DECISIONS.md § D-041`).
Reuses the same `_fetch_transcript_words` code path the worker calls — so
if this succeeds, the regular worker will too.

Usage:
    cd 02_Core_Project
    python -m zspan_pipeline.scripts.test_whisper_one_meeting --meeting-id 101087

What it does:
  1. Looks up the meeting in meetings_cache.db; checks it has a YouTube URL.
  2. Calls _fetch_transcript_words directly (the same function the
     worker invokes for transcript_words in OUTPUT_TYPE_REGISTRY).
  3. Reports word count, duration, first 10 words, and the row that
     landed in notebook_outputs.

Cost: ~$0.006/min × meeting duration. Typical Kingman council meeting
is 1-2 hr, so ~$0.50-1.00. If the audio file exceeds 25 MB (the OpenAI
Whisper hard limit; ~3 hr at the worstaudio bitrate), the call returns
a WhisperFileTooLargeError and we know to build the ffmpeg-transcode
fallback before scaling.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

# Make the bridge package importable
_BRIDGE_PARENT = Path(__file__).resolve().parent.parent.parent
if str(_BRIDGE_PARENT) not in sys.path:
    sys.path.insert(0, str(_BRIDGE_PARENT))

# Make `parsers/` importable
_PARSERS_DIR = _BRIDGE_PARENT / "council_navigator" / "parsers"
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

from database import get_connection  # noqa: E402
from zspan_pipeline.fetcher import _fetch_transcript_words  # noqa: E402


async def run(meeting_id: int) -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT m.id, m.city_name, m.meeting_title, m.meeting_date,
               m.notebook_id, m.video_url,
               COALESCE(wo.youtube_video_url, m.video_url) AS effective_url,
               wo.id AS wo_id, wo.state AS wo_state
        FROM meetings m
        LEFT JOIN work_orders wo ON wo.meeting_id = m.id
        WHERE m.id = ?
        """,
        (meeting_id,),
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        print(f"ERROR: no meeting with id={meeting_id}", file=sys.stderr)
        return 1

    print("=" * 68)
    print(f"  Test Whisper end-to-end · meeting_id={meeting_id}")
    print("=" * 68)
    print(f"  City      : {row['city_name']}")
    print(f"  Title     : {row['meeting_title']}")
    print(f"  Date      : {row['meeting_date']}")
    print(f"  Notebook  : {row['notebook_id'] or '(none)'}")
    print(f"  Source URL: {row['effective_url'] or '(none)'}")
    print(f"  WO        : #{row['wo_id']} ({row['wo_state']})")
    print()

    if not row["effective_url"]:
        print("ERROR: meeting has no YouTube URL (work_orders.youtube_video_url + meetings.video_url both null).")
        print("Hint (per D-138): run python3.11 parsers/scripts/haiku_match_videos.py --city <name> --apply for autonomous URL assignment. Manual paste is REMOVED.")
        return 2

    notebook_id = row["notebook_id"] or "test-whisper-no-notebook"
    if not row["notebook_id"]:
        print(
            "NOTE: meeting has no notebook_id; using a placeholder for "
            "the notebook_outputs row. The transcript will still persist "
            "by meeting_id, which is the canonical key."
        )
        print()

    print("Calling _fetch_transcript_words (downloads audio + transcribes)…")
    t0 = time.time()
    result = await _fetch_transcript_words(meeting_id, notebook_id, "transcript_words")
    elapsed = time.time() - t0

    print(f"\nElapsed: {elapsed:.1f}s")
    print(f"Status:  {result.get('status')}")
    if result.get("status") != "ok":
        print(f"Error:   {result.get('error') or result.get('note')}")
        return 3

    print(f"Words:   {result.get('word_count')}")
    print(f"Audio :  {result.get('duration_seconds'):.1f}s "
          f"({result.get('duration_seconds', 0) / 60:.1f} min)")
    print()

    # Re-read the persisted row to confirm + show first 10 words.
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT content, generated_at FROM notebook_outputs
        WHERE meeting_id = ? AND output_type = 'transcript_words'
        """,
        (meeting_id,),
    )
    out_row = cur.fetchone()
    conn.close()

    if not out_row or not out_row["content"]:
        print("WARN: no notebook_outputs row found after success — pipeline state may be off.")
        return 4

    try:
        payload = json.loads(out_row["content"])
    except json.JSONDecodeError as e:
        print(f"WARN: stored content is not valid JSON: {e}")
        return 5

    words = payload.get("words", [])
    print(f"Stored: notebook_outputs.content, length={len(out_row['content'])} bytes, generated_at={out_row['generated_at']}")
    print()
    print(f"First {min(10, len(words))} words:")
    for w in words[:10]:
        print(f"  {w['start']:6.2f}-{w['end']:6.2f}  {w['word']}")

    print()
    print("Cost estimate (whisper-1 @ $0.006/min):")
    minutes = (result.get("duration_seconds") or 0) / 60
    print(f"  ${minutes * 0.006:.3f}")
    print()
    print("Done. If this looks right, the worker can be turned on for the rest of the pilot.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--meeting-id",
        type=int,
        required=True,
        help="meetings.id of the target meeting (use audit_quote_timestamps for hints).",
    )
    args = parser.parse_args()
    return asyncio.run(run(args.meeting_id))


if __name__ == "__main__":
    sys.exit(main())
