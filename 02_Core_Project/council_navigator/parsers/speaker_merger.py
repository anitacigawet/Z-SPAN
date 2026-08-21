"""Phase 2 D3 — merge whisper word-level transcripts with pyannote speaker turns.

Whisper produces a flat list of words: [{word, start, end}, ...].
pyannote produces a list of speaker turns: [{start, end, speaker_label}, ...].

For each word, this module computes which speaker label owns the word
based on the majority-of-duration rule:
  - For each turn that overlaps with [word.start, word.end], compute
    the overlap duration.
  - The speaker whose turn holds the largest share of the word's
    duration wins.
  - If no single speaker holds at least OVERLAP_THRESHOLD (60% default)
    of the word's duration, the word is labelled "OVERLAP" — the
    genuine cross-talk case (mayor cutting off councilor mid-word).
  - If no turn overlaps the word at all (gap between turns), label
    "UNKNOWN" — pyannote skipped the word's audio (e.g. silence
    misclassification).

Returns a new list of word dicts with `speaker_id` added; doesn't mutate
the input. Order preserved.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# A speaker must hold this fraction of the word's duration to claim it.
# Below this, the word is labelled "OVERLAP" — genuine cross-talk.
OVERLAP_THRESHOLD = 0.6

# Sentinel labels.
OVERLAP_LABEL = "OVERLAP"
UNKNOWN_LABEL = "UNKNOWN"


def _overlap_seconds(a_start: float, a_end: float,
                     b_start: float, b_end: float) -> float:
    """Overlap duration between two intervals; 0 if disjoint."""
    lo = max(a_start, b_start)
    hi = min(a_end, b_end)
    return max(0.0, hi - lo)


def merge_words_with_speakers(
    words: List[Dict[str, Any]],
    turns: List[Dict[str, Any]],
    *,
    overlap_threshold: float = OVERLAP_THRESHOLD,
) -> List[Dict[str, Any]]:
    """Inject `speaker_id` into each word per the majority-of-duration rule.

    Args:
        words: whisper output — list of {word, start, end} dicts.
        turns: pyannote output — list of {start, end, speaker_label} dicts.
        overlap_threshold: minimum fraction of word duration a single
            speaker must hold to win attribution. Below → OVERLAP_LABEL.

    Returns:
        New list of dicts: each entry is the input word dict copied + a
        `speaker_id` key added. Original list is not mutated.

    Performance: O(W × T). For typical council meetings (W~25k words,
    T~500 turns) that's ~12.5M comparisons → ~0.5s in pure Python. If
    this becomes a hotspot, sort turns by start_time and binary-search
    per word — the current impl is the readable reference.
    """
    if not turns:
        # Pyannote returned no turns — fall back to UNKNOWN for every word.
        logger.warning(
            "merge_words_with_speakers: no diarization turns provided; "
            "tagging all %d words as %s", len(words), UNKNOWN_LABEL,
        )
        return [
            {**w, "speaker_id": UNKNOWN_LABEL} for w in words
        ]

    out: List[Dict[str, Any]] = []
    overlap_count = 0
    unknown_count = 0
    speaker_counts: Dict[str, int] = {}

    for word in words:
        w_start = float(word.get("start", 0.0))
        w_end = float(word.get("end", w_start))
        w_duration = max(1e-6, w_end - w_start)  # epsilon to avoid /0

        # Tally overlap per speaker_label across all candidate turns.
        per_speaker_overlap: Dict[str, float] = {}
        for turn in turns:
            t_start = float(turn["start"])
            t_end = float(turn["end"])
            # Early-out for turns clearly outside the word window.
            if t_end < w_start or t_start > w_end:
                continue
            ov = _overlap_seconds(w_start, w_end, t_start, t_end)
            if ov <= 0:
                continue
            label = turn["speaker_label"]
            per_speaker_overlap[label] = per_speaker_overlap.get(label, 0.0) + ov

        if not per_speaker_overlap:
            # No turn intersected this word — pyannote skipped this slice.
            speaker_id = UNKNOWN_LABEL
            unknown_count += 1
        else:
            # Find the speaker with the largest overlap share.
            winner_label = max(per_speaker_overlap, key=per_speaker_overlap.get)
            winner_share = per_speaker_overlap[winner_label] / w_duration
            if winner_share >= overlap_threshold:
                speaker_id = winner_label
            else:
                speaker_id = OVERLAP_LABEL
                overlap_count += 1

        speaker_counts[speaker_id] = speaker_counts.get(speaker_id, 0) + 1
        out.append({**word, "speaker_id": speaker_id})

    if speaker_counts:
        logger.info(
            "merge_words_with_speakers: %d words across %d speaker_ids "
            "(overlap=%d, unknown=%d) — %s",
            len(words),
            sum(1 for k in speaker_counts if k not in (OVERLAP_LABEL, UNKNOWN_LABEL)),
            overlap_count,
            unknown_count,
            {k: v for k, v in sorted(speaker_counts.items())},
        )

    return out


def collapse_to_turn_runs(
    speaker_words: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Collapse a per-word speaker-labelled list into per-turn runs.

    Adjacent words with the same speaker_id are merged into a single
    turn-run dict: `{speaker_label, start, end, text}`. Used by the
    Qdrant indexer (D4) to render compact per-turn-within-chunk payloads
    + by the extractor prompt (D5) to render `SPEAKER_03: "..."` blocks.

    Example: [{w: "Hi", spk: A}, {w: "Bob", spk: A}, {w: "Yes", spk: B}]
        → [{speaker_label: A, start: 0, end: 1.2, text: "Hi Bob"},
           {speaker_label: B, start: 1.3, end: 1.7, text: "Yes"}]
    """
    runs: List[Dict[str, Any]] = []
    current = None
    for word in speaker_words:
        spk = word.get("speaker_id", UNKNOWN_LABEL)
        text = (word.get("word") or "").strip()
        if not text:
            continue
        w_start = float(word.get("start", 0.0))
        w_end = float(word.get("end", w_start))

        if current is None or current["speaker_label"] != spk:
            if current is not None:
                runs.append(current)
            current = {
                "speaker_label": spk,
                "start": w_start,
                "end": w_end,
                "text": text,
            }
        else:
            current["end"] = w_end
            current["text"] = f"{current['text']} {text}"

    if current is not None:
        runs.append(current)

    return runs
