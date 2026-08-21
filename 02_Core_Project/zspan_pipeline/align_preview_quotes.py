"""Word-align the preview-sidecar quotes against the meeting's
transcript_words so karaoke renders correctly when the new-discipline
quotes promote to the production view.

Per operator direction 2026-06-24, the karaoke rendering IS the cited
proof — that's its entire point. Without word_timings the SyncedQuote
component falls back to plain text — losing the citation infrastructure
that makes Z-SPAN's quote panel verifiable. This script runs the
existing parsers/quote_align.py align_quote() function over each
preview quote in .preview/m<id>.json and writes the resulting
word_timings back into the sidecar in place.

Usage:
    python -m zspan_pipeline.align_preview_quotes --meeting-id 103753
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
PREVIEW_DIR = REPO_ROOT / ".preview"
PARSERS_DIR = REPO_ROOT / "02_Core_Project" / "council_navigator" / "parsers"


def align_preview_quotes_for_meeting(meeting_id: int) -> dict:
    sidecar_path = PREVIEW_DIR / f"m{meeting_id}.json"
    if not sidecar_path.exists():
        raise FileNotFoundError(f"Quote sidecar missing: {sidecar_path}")
    data = json.loads(sidecar_path.read_text())
    quotes = data.get("quotes", [])
    if not quotes:
        logger.info("No quotes to align; sidecar unchanged")
        return data

    # Import the alignment + DB shim from parsers/
    sys.path.insert(0, str(PARSERS_DIR))
    from quote_align import align_quote  # type: ignore
    from database import get_connection  # type: ignore

    # Pull transcript_words from notebook_outputs. The bridge persists
    # the Mac Whisper output here under output_type='transcript_words'.
    t0 = time.time()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT content FROM notebook_outputs "
            "WHERE meeting_id = ? AND output_type = 'transcript_words'",
            (meeting_id,),
        )
        row = cursor.fetchone()
    finally:
        conn.close()

    if not row or not row["content"]:
        logger.error(
            "No transcript_words found for meeting=%d. Whisper transcription "
            "needs to have completed for this meeting before alignment can run.",
            meeting_id,
        )
        sys.exit(2)

    transcript_payload = json.loads(row["content"])
    # Mac Whisper output shape: list of segments each with `words` array,
    # OR flat list of word dicts depending on the bridge writer version.
    if isinstance(transcript_payload, list):
        # Heuristic: nested-segments shape if first element has "words"
        if transcript_payload and isinstance(transcript_payload[0], dict) and "words" in transcript_payload[0]:
            whisper_words = []
            for seg in transcript_payload:
                for w in seg.get("words", []) or []:
                    whisper_words.append({
                        "word": w.get("word", "").strip(),
                        "start": float(w.get("start", 0.0) or 0.0),
                        "end": float(w.get("end", 0.0) or 0.0),
                    })
        else:
            # Flat shape
            whisper_words = [{
                "word": w.get("word", "").strip(),
                "start": float(w.get("start", 0.0) or 0.0),
                "end": float(w.get("end", 0.0) or 0.0),
            } for w in transcript_payload]
    elif isinstance(transcript_payload, dict):
        # Some bridges wrap in {"words": [...]} or {"segments": [...]}
        if "words" in transcript_payload:
            whisper_words = [{
                "word": w.get("word", "").strip(),
                "start": float(w.get("start", 0.0) or 0.0),
                "end": float(w.get("end", 0.0) or 0.0),
            } for w in transcript_payload["words"]]
        elif "segments" in transcript_payload:
            whisper_words = []
            for seg in transcript_payload["segments"]:
                for w in seg.get("words", []) or []:
                    whisper_words.append({
                        "word": w.get("word", "").strip(),
                        "start": float(w.get("start", 0.0) or 0.0),
                        "end": float(w.get("end", 0.0) or 0.0),
                    })
        else:
            logger.error("Unrecognized transcript_words shape (dict keys: %s)", list(transcript_payload.keys()))
            sys.exit(3)
    else:
        logger.error("transcript_words is not a list or dict; got %s", type(transcript_payload).__name__)
        sys.exit(3)

    whisper_words = [w for w in whisper_words if w["word"]]
    logger.info("Loaded %d Whisper words for meeting=%d", len(whisper_words), meeting_id)

    aligned_count = 0
    failed_count = 0
    for q in quotes:
        text = q.get("quote_text") or ""
        if not text.strip():
            continue
        timings = align_quote(text, whisper_words)
        if timings:
            q["word_timings"] = timings
            aligned_count += 1
        else:
            q["word_timings"] = None
            failed_count += 1

    elapsed = time.time() - t0
    data["align_elapsed_seconds"] = elapsed
    data["align_aligned_count"] = aligned_count
    data["align_failed_count"] = failed_count

    sidecar_path.write_text(json.dumps(data, indent=2))
    logger.info(
        "Done in %.1fs: %d aligned, %d failed (sidecar updated in place)",
        elapsed, aligned_count, failed_count,
    )
    return data


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(description="Word-align preview quotes for karaoke rendering")
    parser.add_argument("--meeting-id", type=int, required=True)
    args = parser.parse_args()
    align_preview_quotes_for_meeting(args.meeting_id)


if __name__ == "__main__":
    main()
