"""
parsers.node_timing — Backfill `audio_offset_seconds` and
`audio_duration_seconds` on transcript_nodes by fuzzy-matching each
node's representative text against the persisted Whisper word array.

Why: LLM extraction doesn't surface absolute audio timestamps in its
JSON output. The save_*_batch helpers (motions / votes / seconds /
agenda_transitions) therefore land rows with audio_offset=NULL. SPEC
build sequence item 6 (token coloring on the transcript) needs an
audio range per node so each transcript word can be colored by its
owning IR node. This module supplies those ranges.

Algorithm:
  1. For each node missing audio_offset_seconds, pick the most-verbatim
     text field for its kind:
       - Motion          → motion_text
       - AgendaTransition → transition_text
       - Second          → second_text (but too short to anchor alone —
                            sequential pass uses parent Motion's range)
       - Vote            → not fuzzy-aligned (summary is paraphrased);
                            sequential pass places it right after the
                            matched Motion
  2. Run SequenceMatcher (via quote_align.align_quote) against the
     transcript_words array to find the dominant matching cluster.
  3. Set audio_offset = first matched word's start_s,
     audio_duration = last matched word's end_s - audio_offset.
  4. Sequential pass for Vote + Second uses the responds_to edges
     created by edge_inference.py — Second sits between Motion and Vote,
     Vote sits ~1s after Motion's end (placeholder range until a future
     pass searches for "carries" / "passes" / "motion fails" markers).

Idempotent: a node whose audio_offset is already set is skipped unless
`force=True` is passed.

CLI:
    python3.11 -m parsers.node_timing --meeting-id 101091
    python3.11 -m parsers.node_timing --meeting-id 101091 --verbose
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from database import get_connection  # noqa: E402
from quote_align import align_quote  # noqa: E402


# Confidence threshold for accepting an alignment. Below this, the
# matched cluster isn't tight enough — likely a paraphrase or a wrong
# anchor. Skipping is safer than recording a wrong range that would
# mislead the token-coloring layer.
_MIN_ALIGNMENT_COVERAGE = 0.40

# Placeholder Vote duration when we don't know how long the chair's
# announcement took. The Vote's "moment" is the announcement of the
# outcome, which is typically a single 1-3 second sentence. Pick 1s
# so the colored span in the transcript reads as a marker, not a region.
_PLACEHOLDER_VOTE_DURATION_SEC = 1.0

# Placeholder Second duration — the actual second utterance is one
# word, ~0.3-0.6s. 0.6s is generous.
_PLACEHOLDER_SECOND_DURATION_SEC = 0.6


def _node_alignment_text(node: Dict) -> Optional[str]:
    """Return the most-verbatim text field for the node, suitable for
    fuzzy-aligning against transcript_words. Different node types have
    different "what was actually said in the transcript" fields:

        Motion         → typed_fields.motion_text
        Second         → typed_fields.second_text  (too short to anchor
                         alone — see sequential pass)
        AgendaTransition → typed_fields.transition_text
        Vote           → None (summary is paraphrased; handled by
                         sequential pass)
        Commit_P       → claim_text from typed_fields (the backfilled
                         Commit_P rows carry claim_text in their
                         typed_fields, but Commit_P typically already
                         has word_timings on its tracked_claim
                         projection — usually skip this branch)
    """
    nt = node.get("node_type")
    tf = node.get("typed_fields") or {}
    if nt == "Motion":
        return (tf.get("motion_text") or "").strip() or None
    if nt == "Second":
        return (tf.get("second_text") or "").strip() or None
    if nt == "AgendaTransition":
        return (tf.get("transition_text") or "").strip() or None
    if nt == "Commit_P":
        # Backfilled Commit_P rows from hand-seeded claims carry
        # claim_text in typed_fields. Production Commit_P (extracted by
        # the pipeline via tracked_claims.md) usually has word_timings on
        # the tracked_claim projection — the frontend reads those
        # directly. This path is for completeness.
        return (tf.get("claim_text") or "").strip() or None
    return None


def _load_transcript_words(meeting_id: int) -> Optional[List[Dict]]:
    """Pull the persisted Whisper word array for the meeting from
    notebook_outputs.transcript_words. Returns None when the row
    doesn't exist or content isn't valid JSON."""
    conn = get_connection()
    row = conn.execute(
        "SELECT content FROM notebook_outputs "
        "WHERE meeting_id = ? AND output_type = 'transcript_words' "
        "AND content IS NOT NULL AND content != ''",
        (meeting_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    try:
        payload = json.loads(row["content"])
    except (json.JSONDecodeError, TypeError):
        return None
    words = payload.get("words")
    return words if isinstance(words, list) else None


def _load_nodes_needing_timing(
    cursor, meeting_id: int, force: bool
) -> List[Dict]:
    """Load transcript_nodes that need timing backfill. When force is
    True, return ALL nodes; otherwise skip those that already have
    audio_offset_seconds populated."""
    where_extra = "" if force else "AND audio_offset_seconds IS NULL"
    rows = cursor.execute(
        f"""
        SELECT id, ordinal, node_type, typed_fields, transcript_span_text,
               audio_offset_seconds, audio_duration_seconds, parent_node_id
        FROM transcript_nodes
        WHERE meeting_id = ?
        {where_extra}
        ORDER BY node_type, ordinal
        """,
        (meeting_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["typed_fields"] = json.loads(d["typed_fields"]) if d["typed_fields"] else {}
        except (json.JSONDecodeError, TypeError):
            d["typed_fields"] = {}
        out.append(d)
    return out


def _coverage(timings: List[Dict], target_text_word_count: int) -> float:
    """Fraction of the alignment's display tokens that were anchored to
    Whisper (not interpolated). Higher is better; <0.40 typically means
    the text is paraphrased and the alignment placed it via interpolation
    of a few weak anchors."""
    if not timings or target_text_word_count == 0:
        return 0.0
    # align_quote returns per-display-token rows; for our purposes the
    # input was N words long. We approximate coverage as alignment-
    # row-count / N. (align_quote's own internal coverage isn't
    # exposed.)
    return min(1.0, len(timings) / target_text_word_count)


def _align_node_text(
    text: str, whisper_words: List[Dict]
) -> Optional[Tuple[float, float]]:
    """Fuzzy-align `text` against `whisper_words` and return
    (start_seconds, end_seconds) of the matched range. Returns None if
    alignment is too weak to trust."""
    timings = align_quote(text, whisper_words)
    if not timings:
        return None
    # First/last word's start/end from the alignment row stream.
    starts = [t["start_ms"] / 1000.0 for t in timings if t.get("start_ms")]
    ends = [t["end_ms"] / 1000.0 for t in timings if t.get("end_ms")]
    if not starts or not ends:
        return None
    return (min(starts), max(ends))


def _slice_words_to_window(
    whisper_words: List[Dict],
    window_start_sec: float,
    window_end_sec: float,
) -> List[Dict]:
    """Return only the Whisper words whose timestamps fall within the
    window. The returned list keeps absolute timestamps in each row's
    `start` / `end` fields — alignment results remain in absolute time."""
    out = []
    for w in whisper_words:
        ws = float(w.get("start") or 0.0)
        if window_start_sec <= ws <= window_end_sec:
            out.append(w)
    return out


def backfill_timings(
    meeting_id: int, *, dry_run: bool = False, force: bool = False
) -> Dict:
    """Backfill audio_offset_seconds + audio_duration_seconds for the
    meeting's transcript_nodes.

    Pipeline (chronologically dependent — order matters):
      1. Time AgendaTransitions via transition_text alignment against
         the full transcript. Each transition's text is generally
         distinctive (chair-named item titles).
      2. Time Commit_P nodes (already-aligned tracked_claims projection
         carries word_timings; same range as the projection).
      3. Time Motions, CONSTRAINED to the parent AgendaTransition's
         range plus a tail (typically the discussion period before the
         vote). Without this constraint, "so moved" (which appears 5+
         times in a meeting) maps every Motion to the same position.
      4. Time Seconds via sequential placement right after their
         responds_to Motion (second_text is single word; ambiguity
         identical to "so moved" issue).
      5. Time Votes via sequential placement right after their
         responds_to Motion.

    Returns: {by_type, total_timed, no_transcript}
    """
    whisper_words = _load_transcript_words(meeting_id)
    if not whisper_words:
        return {
            "no_transcript": True,
            "by_type": {},
            "total_timed": 0,
        }

    conn = get_connection()
    cursor = conn.cursor()
    nodes = _load_nodes_needing_timing(cursor, meeting_id, force=force)

    if not nodes:
        conn.close()
        return {
            "no_transcript": False,
            "by_type": {},
            "total_timed": 0,
        }

    by_type: Dict[str, Dict[str, int]] = {}
    pass1_results: Dict[int, Tuple[float, float]] = {}  # node_id → (start, end)

    # === Stage 1: AgendaTransitions ===
    # Time these first because Motions need their parent transition's
    # range as a search window. Each transition's transition_text is
    # generally distinctive across the meeting (chair-named items).
    transitions_sorted_by_ordinal: List[Dict] = []
    for node in nodes:
        nt = node["node_type"]
        by_type.setdefault(nt, {"timed": 0, "skipped_no_text": 0, "skipped_no_match": 0})
        if nt == "AgendaTransition":
            transitions_sorted_by_ordinal.append(node)
    transitions_sorted_by_ordinal.sort(key=lambda n: n["ordinal"])

    for node in transitions_sorted_by_ordinal:
        text = _node_alignment_text(node)
        if not text:
            by_type["AgendaTransition"]["skipped_no_text"] += 1
            continue
        match = _align_node_text(text, whisper_words)
        if not match:
            by_type["AgendaTransition"]["skipped_no_match"] += 1
            continue
        pass1_results[node["id"]] = match

    # Build a map: ordered list of timed transitions → for parent-
    # window computation, we know "Motion's parent ends at this
    # transition; next transition starts at THIS time" — that delimits
    # the search window.
    timed_transitions = [
        (t["id"], pass1_results[t["id"]])
        for t in transitions_sorted_by_ordinal
        if t["id"] in pass1_results
    ]
    timed_transitions.sort(key=lambda x: x[1][0])  # by start_s
    # Helper: for a given AgendaTransition id, return (window_start, window_end)
    # where window_end is the start of the NEXT transition (or +inf for the last).
    def _transition_window(transition_id: int) -> Optional[Tuple[float, float]]:
        for i, (tid, (s, e)) in enumerate(timed_transitions):
            if tid == transition_id:
                # End of window = start of next transition (or +inf)
                if i + 1 < len(timed_transitions):
                    next_start = timed_transitions[i + 1][1][0]
                    return (s, next_start)
                return (s, s + 600.0)  # 10 min tail on the last transition
        return None

    # === Stage 2: Commit_P ===
    # Commit_P typed_fields carries claim_text. The corresponding
    # tracked_claims row has word_timings already aligned (see
    # quote_align.align_tracked_claims_for_meeting). Pull those instead
    # of re-aligning — same positions, cheaper.
    for node in nodes:
        if node["node_type"] != "Commit_P":
            continue
        tf = node.get("typed_fields") or {}
        tc_id = tf.get("tracked_claim_id")
        if tc_id is None:
            # Try claim_text alignment as a fallback
            text = (tf.get("claim_text") or "").strip()
            if not text:
                by_type["Commit_P"]["skipped_no_text"] += 1
                continue
            match = _align_node_text(text, whisper_words)
            if not match:
                by_type["Commit_P"]["skipped_no_match"] += 1
                continue
            pass1_results[node["id"]] = match
            continue
        # Pull word_timings from tracked_claims
        row = cursor.execute(
            "SELECT word_timings FROM tracked_claims WHERE id = ?",
            (tc_id,),
        ).fetchone()
        if not row or not row["word_timings"]:
            by_type["Commit_P"]["skipped_no_match"] += 1
            continue
        try:
            wt = json.loads(row["word_timings"])
        except (json.JSONDecodeError, TypeError):
            wt = []
        if not wt:
            by_type["Commit_P"]["skipped_no_match"] += 1
            continue
        starts = [w["start_ms"] / 1000.0 for w in wt if isinstance(w.get("start_ms"), (int, float))]
        ends = [w["end_ms"] / 1000.0 for w in wt if isinstance(w.get("end_ms"), (int, float))]
        if not starts or not ends:
            by_type["Commit_P"]["skipped_no_match"] += 1
            continue
        pass1_results[node["id"]] = (min(starts), max(ends))

    # === Stage 3: Motions (constrained by parent AgendaTransition) ===
    for node in nodes:
        if node["node_type"] != "Motion":
            continue
        text = _node_alignment_text(node)
        if not text:
            by_type["Motion"]["skipped_no_text"] += 1
            continue
        parent_id = node.get("parent_node_id")
        # Re-fetch parent_node_id since _load_nodes_needing_timing
        # didn't SELECT it — query directly.
        if parent_id is None:
            parent_row = cursor.execute(
                "SELECT parent_node_id FROM transcript_nodes WHERE id = ?",
                (node["id"],),
            ).fetchone()
            parent_id = parent_row["parent_node_id"] if parent_row else None
        window = _transition_window(parent_id) if parent_id else None
        if window:
            constrained = _slice_words_to_window(whisper_words, window[0], window[1])
            # If the window is too small (< 20 words), fall back to full
            # transcript so the alignment has enough anchors.
            if len(constrained) < 20:
                constrained = whisper_words
        else:
            constrained = whisper_words
        match = _align_node_text(text, constrained)
        if not match:
            by_type["Motion"]["skipped_no_match"] += 1
            continue
        pass1_results[node["id"]] = match

    # Pass 2: sequential placement for Vote + Second using responds_to edges
    # Load the responds_to edges for this meeting's votes and seconds.
    node_ids = [n["id"] for n in nodes]
    if not node_ids:
        conn.close()
        return {
            "no_transcript": False,
            "by_type": by_type,
            "total_timed": 0,
        }
    placeholders = ",".join("?" * len(node_ids))
    edge_rows = cursor.execute(
        f"""
        SELECT source_node_id, target_node_id, edge_type
        FROM transcript_edges
        WHERE edge_type = 'responds_to'
        AND source_node_id IN ({placeholders})
        """,
        node_ids,
    ).fetchall()
    # source (Vote or Second) → target (Motion)
    source_to_motion: Dict[int, int] = {}
    for er in edge_rows:
        source_to_motion[er["source_node_id"]] = er["target_node_id"]

    for node in nodes:
        nt = node["node_type"]
        if nt not in ("Vote", "Second"):
            continue
        motion_id = source_to_motion.get(node["id"])
        if motion_id is None:
            by_type[nt]["skipped_no_match"] += 1
            continue
        # The Motion's timing — either from pass 1 or from existing DB
        # (a previous run may have timed it).
        if motion_id in pass1_results:
            m_start, m_end = pass1_results[motion_id]
        else:
            row = cursor.execute(
                "SELECT audio_offset_seconds, audio_duration_seconds "
                "FROM transcript_nodes WHERE id = ?",
                (motion_id,),
            ).fetchone()
            if (
                not row
                or row["audio_offset_seconds"] is None
                or row["audio_duration_seconds"] is None
            ):
                by_type[nt]["skipped_no_match"] += 1
                continue
            m_start = float(row["audio_offset_seconds"])
            m_end = m_start + float(row["audio_duration_seconds"])
        # Second sits between Motion's end and Motion's end + 5s (a
        # comfortable window for the second utterance to fall in).
        # Vote sits right after that (Motion's end + 5s for the
        # placeholder; future work could search for "motion carries"
        # markers to refine).
        if nt == "Second":
            start_s = m_end + 0.1
            end_s = start_s + _PLACEHOLDER_SECOND_DURATION_SEC
        else:  # Vote
            # If a Second was placed in this run, sit after it;
            # otherwise sit right after Motion's end.
            start_s = m_end + 1.0
            end_s = start_s + _PLACEHOLDER_VOTE_DURATION_SEC
        pass1_results[node["id"]] = (start_s, end_s)

    # Persist
    total_timed = 0
    for node_id, (start_s, end_s) in pass1_results.items():
        duration = max(0.1, end_s - start_s)
        nt_lookup = next((n["node_type"] for n in nodes if n["id"] == node_id), None)
        if dry_run:
            logger.info(
                "node_timing (dry-run): %s id=%s → [%.2fs, %.2fs] dur=%.2fs",
                nt_lookup, node_id, start_s, end_s, duration,
            )
            total_timed += 1
            if nt_lookup:
                by_type[nt_lookup]["timed"] += 1
            continue
        cursor.execute(
            "UPDATE transcript_nodes SET audio_offset_seconds = ?, "
            "audio_duration_seconds = ? WHERE id = ?",
            (start_s, duration, node_id),
        )
        total_timed += 1
        if nt_lookup:
            by_type[nt_lookup]["timed"] += 1
        logger.info(
            "node_timing: %s id=%s timed at [%.2fs, %.2fs] dur=%.2fs",
            nt_lookup, node_id, start_s, end_s, duration,
        )

    if not dry_run:
        conn.commit()
    conn.close()
    return {
        "no_transcript": False,
        "by_type": by_type,
        "total_timed": total_timed,
    }


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill audio timings for compiler transcript_nodes.",
    )
    parser.add_argument("--meeting-id", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--force", action="store_true",
        help="Recompute even when audio_offset_seconds is already set.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    result = backfill_timings(
        args.meeting_id, dry_run=args.dry_run, force=args.force,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
