"""
quote_align — align an extracted quote's text to a Whisper word-level
transcript, producing per-word timestamps for the synced-transcript
karaoke player.
==========================================================================

T-009 Phase 0b (per `DECISIONS.md § D-040` and `D-042`). Consumes:

  * The cleaned/extracted quote text from `member_quotes.quote_text` (a
    string produced by LLM extraction + the gpt-4o-mini quote
    cleaner). Typically 50-200 words, ~95%+ verbatim to what was said.
  * The Whisper word-level transcript stored in
    `notebook_outputs.content` for the same meeting (`transcript_words`
    output type). A dict `{words: [{word, start, end}, ...], ...}`
    covering the entire meeting.

Produces a JSON-serializable list of `{word, start_ms, end_ms}` rows —
one per display-token in the quote — that's stored on
`member_quotes.word_timings`.

The output drives the Cast page karaoke UI (Phase 0c): each word
renders as a `<span>` with `data-start-ms` / `data-end-ms`; an
animation-frame loop polls `audio.currentTime` and applies an
`.is-active` class to whichever span's range contains the cursor.

Algorithm
---------

Token-level sequence alignment via `difflib.SequenceMatcher`:

  1. Tokenize the quote into DISPLAY tokens (whitespace-split, preserves
     capitalization and adjacent punctuation). Each display token is the
     unit that gets a timing.
  2. For each display token, derive a NORMALIZED form (lowercase,
     alphanumeric only) for matching. Display tokens with no
     alphanumeric content (e.g., a standalone `--`) are still emitted
     but get interpolated timings.
  3. Tokenize the Whisper word array similarly.
  4. Run SequenceMatcher over the normalized arrays to find matching
     blocks. SequenceMatcher's longest-common-subsequence approach
     anchors the quote to its actual occurrence in the transcript.
  5. For each quote token matched to a Whisper word, copy the Whisper
     word's `start` / `end` (converted to ms).
  6. For unmatched quote tokens (cleaner stripped fillers Whisper kept,
     Whisper misheard a word, etc.), linearly interpolate timings from
     the nearest matched neighbors. This ensures EVERY display token
     gets a timing, which the karaoke UI requires for smooth cursor
     flow.

Quality empirically (m101091 quote id=19): 95.3% direct match, ~0.001s
to align 64 tokens against 11,717 Whisper words. The interpolation step
handles the remaining 4.7% well enough for word-level highlight
precision — at conversational speech rates (~150 wpm = 400 ms/word),
the interpolation error is bounded by ~200 ms either side, which is
imperceptible at karaoke playback speeds.

What this module does NOT do
----------------------------

- Speaker diarization. The Whisper transcript doesn't distinguish
  speakers; we assume the quote was attributed correctly by the
  upstream extraction (T-007 / T-008's `member_quotes_topic` prompt).
  Alignment errors WILL surface when the quote is misattributed (since
  the matched audio won't be from the claimed speaker), but that's a
  detection signal, not a feature.
- Cross-meeting search. Only aligns within one Whisper transcript at a
  time. Each meeting's transcript_words is independent.
- Hallucination detection. If a quote contains words that don't appear
  anywhere in the Whisper transcript, alignment falls through to pure
  interpolation — the result still produces timings but they'll be
  wrong. The fuzzy-match-against-Whisper layer from `D-041` is a
  separate concern that catches this; it could be layered here later.
"""
from __future__ import annotations

import logging
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)


_WORD_RE = re.compile(r"[a-z0-9']+")
_NUMBERED_DECISION_RE = re.compile(r"^\s*\d{1,2}[.)]\s+", re.MULTILINE)

_NUMBER_EQUIVALENTS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "thirteen": "13",
    "fourteen": "14",
    "fifteen": "15",
    "sixteen": "16",
    "seventeen": "17",
    "eighteen": "18",
    "nineteen": "19",
    "twenty": "20",
}


@dataclass(frozen=True)
class QuoteAlignmentEvidence:
    """Confidence-bearing result for a constrained quote-anchor attempt.

    ``align_quote`` intentionally keeps its karaoke-oriented return contract.
    This additive result is for citation anchors, where interpolated timings
    must never masquerade as evidence and repeated phrases must be surfaced.
    """

    success: bool
    reason: str
    quote_tokens: int
    direct_matches: int
    coverage: float
    unique: bool
    uniqueness: str
    comparable_matches: tuple[dict, ...]
    candidate_count: int
    in_window_candidate_count: int
    best_candidate_start_seconds: Optional[float]
    best_candidate_end_seconds: Optional[float]
    best_candidate_direct_matches: int
    best_candidate_coverage: float
    best_candidate_distance_seconds: Optional[float]
    window_start_seconds: float
    window_end_seconds: float
    start_seconds: Optional[float]
    matched_word_index: Optional[int]
    matched_end_word_index: Optional[int]
    timings: Optional[list[dict]]


@dataclass(frozen=True)
class _EvidenceWord:
    normalized: str
    start_seconds: float
    end_seconds: float
    original_index: int
    row: dict
    quarantined: bool


@dataclass(frozen=True)
class _EvidenceCandidate:
    coverage: float
    direct_matches: int
    first_seconds: float
    last_seconds: float
    first_word_position: int
    last_word_position: int
    direct_start_position: int
    direct_start_quote_index: int
    segment_start: int
    segment_end: int


def _is_quarantined_word(row: dict) -> bool:
    if row.get("quarantined") is True:
        return True
    annotation = row.get("quarantine")
    return isinstance(annotation, dict) and bool(annotation.get("reason"))


def _normalize_token(s: str) -> str:
    """Return the lowercased, apostrophe-preserving alphanumeric core of
    `s`. Returns "" if `s` has no alphanumeric content (e.g., standalone
    punctuation like `--`).
    """
    if not s:
        return ""
    m = _WORD_RE.findall(s.lower())
    return m[0] if m else ""


def _normalize_anchor_token(s: str) -> str:
    """Normalize an anchor token, treating spoken and digit numerals alike."""
    normalized = _normalize_token(s)
    return _NUMBER_EQUIVALENTS.get(normalized, normalized)


def _split_display_tokens(text: str) -> list[str]:
    """Split `text` into display tokens via whitespace. Preserves
    capitalization and adjacent punctuation ("Hello," is one display
    token). Empty tokens are filtered.
    """
    return [t for t in text.split() if t]


def _interpolate_unmatched(
    n_tokens: int,
    matched: dict[int, tuple[float, float]],
) -> list[tuple[float, float]]:
    """Fill timings for every position 0..n_tokens-1.

    For positions in `matched`, copy the (start, end) tuple.
    For unmatched positions, find the nearest matched neighbor on each
    side and linearly interpolate. Edge cases:

    - All before first match: spread between (first_match_start - small,
      first_match_start) so the cursor reaches them just before the
      first matched word.
    - All after last match: spread between (last_match_end, last_match_end
      + small) so the cursor advances past them after the last matched
      word.
    - No matches at all: return zero-duration timings at 0.
    """
    if not matched:
        return [(0.0, 0.0)] * n_tokens

    sorted_indices = sorted(matched.keys())
    out: list[tuple[float, float]] = [(0.0, 0.0)] * n_tokens

    # First pass: copy matched positions
    for i in sorted_indices:
        out[i] = matched[i]

    # Second pass: interpolate gaps
    first_matched = sorted_indices[0]
    last_matched = sorted_indices[-1]

    # Gap before the first match
    if first_matched > 0:
        first_start, _ = matched[first_matched]
        # Allocate up to 0.5s before the first match for the leading
        # unmatched tokens, but never go negative.
        lead_time = min(0.5, first_start)
        per_token = lead_time / first_matched if first_matched > 0 else 0
        for i in range(first_matched):
            t_start = max(0.0, first_start - lead_time + per_token * i)
            t_end = max(0.0, first_start - lead_time + per_token * (i + 1))
            out[i] = (t_start, t_end)

    # Gaps between matches
    for prev_idx, next_idx in zip(sorted_indices[:-1], sorted_indices[1:]):
        if next_idx - prev_idx <= 1:
            continue  # no gap
        _, prev_end = matched[prev_idx]
        next_start, _ = matched[next_idx]
        gap_duration = max(0.0, next_start - prev_end)
        n_in_gap = next_idx - prev_idx - 1
        per_token = gap_duration / (n_in_gap + 1)
        for j in range(n_in_gap):
            i = prev_idx + 1 + j
            t_start = prev_end + per_token * j
            t_end = prev_end + per_token * (j + 1)
            out[i] = (t_start, t_end)

    # Tail after the last match
    if last_matched < n_tokens - 1:
        _, last_end = matched[last_matched]
        tail_time = 0.5
        per_token = tail_time / (n_tokens - 1 - last_matched)
        for j in range(n_tokens - 1 - last_matched):
            i = last_matched + 1 + j
            t_start = last_end + per_token * j
            t_end = last_end + per_token * (j + 1)
            out[i] = (t_start, t_end)

    return out


TRANSCRIPT_EXCERPT_MODALITY = "transcript_excerpt_v1"
TRANSCRIPT_EXCERPT_SOURCE = "item_quote_to_action_quote"
TRANSCRIPT_EXCERPT_MAX_GAP_SECONDS = 300.0
TRANSCRIPT_EXCERPT_COMPLETE_LABEL = "Verbatim transcript excerpt — complete"
TRANSCRIPT_EXCERPT_ELIDED_LABEL = (
    "Verbatim transcript excerpts — middle omitted"
)


def _source_word(row: Mapping[str, Any], index: int) -> tuple[str, float, float]:
    token = row.get("word")
    start = row.get("start")
    end = row.get("end")
    if not isinstance(token, str):
        raise ValueError(f"transcript word {index} has no string word token")
    if (
        not isinstance(start, (int, float))
        or isinstance(start, bool)
        or not math.isfinite(float(start))
        or float(start) < 0
    ):
        raise ValueError(f"transcript word {index} has invalid start timestamp")
    if (
        not isinstance(end, (int, float))
        or isinstance(end, bool)
        or not math.isfinite(float(end))
        or float(end) < float(start)
    ):
        raise ValueError(f"transcript word {index} has invalid end timestamp")
    return token, float(start), float(end)


def _legacy_end_word_index(
    transcript_words: Sequence[Mapping[str, Any]],
    start_word_index: int,
    evidence: Mapping[str, Any],
) -> int | None:
    """Resolve old alignment evidence whose end boundary was timestamp-only."""
    end_seconds = evidence.get("best_candidate_end_seconds")
    if (
        not isinstance(end_seconds, (int, float))
        or isinstance(end_seconds, bool)
        or not math.isfinite(float(end_seconds))
    ):
        return None
    matches: list[int] = []
    for index in range(start_word_index, len(transcript_words)):
        try:
            _token, _start, word_end = _source_word(transcript_words[index], index)
        except ValueError:
            continue
        if math.isclose(word_end, float(end_seconds), rel_tol=0.0, abs_tol=0.001):
            matches.append(index)
        if word_end > float(end_seconds) + 0.001:
            break
    # Whisper can emit a zero-duration trailing token with the same end time
    # as the preceding word. The evidence candidate's last position is the
    # later match, so select the final adjacent timestamp match.
    return matches[-1] if matches else None


def _evidence_word_range(
    transcript_words: Sequence[Mapping[str, Any]],
    evidence: Mapping[str, Any],
    evidence_name: str,
) -> tuple[int, int]:
    start = evidence.get("start_word_index", evidence.get("matched_word_index"))
    end = evidence.get(
        "end_word_index",
        evidence.get("matched_end_word_index"),
    )
    if not isinstance(start, int) or isinstance(start, bool):
        raise ValueError(f"{evidence_name} has no valid start word index")
    if end is None:
        end = _legacy_end_word_index(transcript_words, start, evidence)
    if not isinstance(end, int) or isinstance(end, bool):
        raise ValueError(f"{evidence_name} has no valid end word index")
    if start < 0 or end < start or end >= len(transcript_words):
        raise ValueError(
            f"{evidence_name} word range is out of bounds or reversed: "
            f"{start}..{end}"
        )
    return start, end


def _format_elapsed(seconds: float) -> str:
    total_milliseconds = round(seconds * 1000)
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"


def _excerpt_span(
    transcript_words: Sequence[Mapping[str, Any]],
    start_word_index: int,
    end_word_index: int,
    *,
    label: str,
    structure: str,
    omission_marker: str,
) -> dict[str, Any]:
    rows = transcript_words[start_word_index:end_word_index + 1]
    tokens = [
        _source_word(row, start_word_index + offset)[0]
        for offset, row in enumerate(rows)
    ]
    _first_token, start_seconds, _first_end = _source_word(
        transcript_words[start_word_index], start_word_index,
    )
    _last_token, _last_start, end_seconds = _source_word(
        transcript_words[end_word_index], end_word_index,
    )
    return {
        "text": " ".join(tokens),
        "start_word_index": start_word_index,
        "end_word_index": end_word_index,
        "start_seconds": start_seconds,
        "end_seconds": end_seconds,
        "source": TRANSCRIPT_EXCERPT_SOURCE,
        "label": label,
        "structure": structure,
        "omission_marker": omission_marker,
    }


def materialize_transcript_excerpt(
    transcript_words: Sequence[Mapping[str, Any]],
    item_evidence: Mapping[str, Any],
    action_evidence: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Materialize the locked item-to-action transcript excerpt contract.

    Text is solely a single-space presentation join of canonical transcript
    tokens. Inclusive word indices remain the source of truth.
    """
    if not transcript_words:
        raise ValueError("cannot materialize an excerpt from an empty transcript")
    item_start, item_end = _evidence_word_range(
        transcript_words, item_evidence, "item_evidence",
    )
    action_start, action_end = _evidence_word_range(
        transcript_words, action_evidence, "action_evidence",
    )
    if action_start < item_start or action_end < item_end:
        raise ValueError(
            "action evidence precedes the item evidence: "
            f"item={item_start}..{item_end} action={action_start}..{action_end}"
        )

    _item_token, _item_start_seconds, item_end_seconds = _source_word(
        transcript_words[item_end], item_end,
    )
    _action_token, action_start_seconds, _action_end_seconds = _source_word(
        transcript_words[action_start], action_start,
    )
    gap_seconds = action_start_seconds - item_end_seconds
    overlaps = action_start <= item_end
    if overlaps or gap_seconds <= TRANSCRIPT_EXCERPT_MAX_GAP_SECONDS:
        return [
            _excerpt_span(
                transcript_words,
                item_start,
                action_end,
                label=TRANSCRIPT_EXCERPT_COMPLETE_LABEL,
                structure="contiguous",
                omission_marker="",
            )
        ]

    omitted_words = action_start - item_end - 1
    marker = (
        "[Transcript omitted between verbatim passages: "
        f"{omitted_words} words · {_format_elapsed(gap_seconds)} elapsed]"
    )
    return [
        _excerpt_span(
            transcript_words,
            item_start,
            item_end,
            label=TRANSCRIPT_EXCERPT_ELIDED_LABEL,
            structure="elided",
            omission_marker=marker,
        ),
        _excerpt_span(
            transcript_words,
            action_start,
            action_end,
            label=TRANSCRIPT_EXCERPT_ELIDED_LABEL,
            structure="elided",
            omission_marker=marker,
        ),
    ]


def validate_transcript_excerpt_spans(
    transcript_words: Sequence[Mapping[str, Any]],
    spans: Any,
    item_evidence: Mapping[str, Any],
    action_evidence: Mapping[str, Any],
) -> list[str]:
    """Return exact-reconstruction errors for a persisted excerpt span set."""
    if not isinstance(spans, list):
        return ["verbatim_spans_missing_or_malformed"]
    try:
        expected = materialize_transcript_excerpt(
            transcript_words, item_evidence, action_evidence,
        )
    except (TypeError, ValueError) as exc:
        return [f"anchor_materialization_failed:{exc}"]
    if len(spans) != len(expected):
        return [f"span_count_mismatch:{len(spans)}!={len(expected)}"]

    errors: list[str] = []
    for span_index, (actual, canonical) in enumerate(zip(spans, expected), start=1):
        if not isinstance(actual, dict):
            errors.append(f"span_{span_index}_malformed")
            continue
        for field, expected_value in canonical.items():
            actual_value = actual.get(field)
            if isinstance(expected_value, float):
                matches = (
                    isinstance(actual_value, (int, float))
                    and not isinstance(actual_value, bool)
                    and float(actual_value) == expected_value
                )
            else:
                matches = actual_value == expected_value
            if not matches:
                errors.append(f"span_{span_index}_{field}_mismatch")
    return errors


def materialize_legacy_decision_excerpts(
    sidecar: Mapping[str, Any],
    transcript_words: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a copied legacy sidecar with derivable decision spans in memory.

    The function never mutates its inputs. It upgrades the response modality
    only when every numbered decision has a two-part anchor and materializes.
    """
    result = deepcopy(dict(sidecar))
    if result.get("citation_modality") == TRANSCRIPT_EXCERPT_MODALITY:
        return result
    alignment = result.get("citation_alignment")
    if not isinstance(alignment, list) or not alignment:
        return result

    alignment_by_index: dict[int, dict[str, Any]] = {}
    for entry in alignment:
        if not isinstance(entry, dict):
            continue
        index = entry.get("output_index", entry.get("index"))
        if isinstance(index, int) and not isinstance(index, bool):
            if index in alignment_by_index:
                return result
            alignment_by_index[index] = entry
    existing_decisions = result.get("decisions")
    if not isinstance(existing_decisions, list):
        existing_decisions = []
    decision_by_index: dict[int, dict[str, Any]] = {}
    for decision in existing_decisions:
        if not isinstance(decision, dict):
            continue
        index = decision.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        if index in decision_by_index:
            return result
        decision_by_index[index] = dict(decision)
    materialized: list[dict[str, Any]] = []
    for index in sorted(alignment_by_index):
        entry = alignment_by_index[index]
        item = entry.get("item_evidence")
        action = entry.get("action_evidence")
        if (
            entry.get("source") != "two_part_quote"
            or not isinstance(item, dict)
            or not isinstance(action, dict)
        ):
            return result
        try:
            spans = materialize_transcript_excerpt(transcript_words, item, action)
        except (TypeError, ValueError):
            return result
        decision = decision_by_index.get(index, {"index": index})
        decision["verbatim_spans"] = spans
        materialized.append(decision)

    if not materialized:
        return result
    expected_count = result.get("prose_list_count")
    if not isinstance(expected_count, int) or isinstance(expected_count, bool):
        prose = result.get("prose_output")
        expected_count = (
            len(_NUMBERED_DECISION_RE.findall(prose))
            if isinstance(prose, str)
            else 0
        )
    if expected_count != len(materialized):
        return result
    result["decisions"] = materialized
    result["citation_modality"] = TRANSCRIPT_EXCERPT_MODALITY
    return result


def materialize_missing_decision_excerpts(
    sidecar: Mapping[str, Any],
    transcript_words: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Fill response-only missing spans for either current or legacy data."""
    if sidecar.get("citation_modality") != TRANSCRIPT_EXCERPT_MODALITY:
        return materialize_legacy_decision_excerpts(sidecar, transcript_words)

    result = deepcopy(dict(sidecar))
    alignment = result.get("citation_alignment")
    decisions = result.get("decisions")
    if not isinstance(alignment, list):
        return result
    if not isinstance(decisions, list):
        decisions = []
    decision_by_index = {
        decision.get("index"): dict(decision)
        for decision in decisions
        if isinstance(decision, dict)
        and isinstance(decision.get("index"), int)
        and not isinstance(decision.get("index"), bool)
    }
    for entry in alignment:
        if not isinstance(entry, dict):
            continue
        index = entry.get("output_index", entry.get("index"))
        item = entry.get("item_evidence")
        action = entry.get("action_evidence")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or entry.get("source") != "two_part_quote"
            or not isinstance(item, dict)
            or not isinstance(action, dict)
        ):
            continue
        decision = decision_by_index.get(index, {"index": index})
        spans = decision.get("verbatim_spans")
        if not isinstance(spans, list) or not spans:
            try:
                decision["verbatim_spans"] = materialize_transcript_excerpt(
                    transcript_words, item, action,
                )
            except (TypeError, ValueError):
                continue
        decision_by_index[index] = decision
    result["decisions"] = [
        decision_by_index[index] for index in sorted(decision_by_index)
    ]
    return result


def align_quote(
    quote_text: str,
    whisper_words: list[dict],
    *,
    min_block_size: int = 2,
    cluster_window_seconds: float = 90.0,
) -> Optional[list[dict]]:
    """Align `quote_text` against `whisper_words`, return per-word timings.

    Args:
        quote_text: the cleaned quote text from `member_quotes.quote_text`.
        whisper_words: list of `{word: str, start: float, end: float}` rows
            from the Whisper word-level transcript.
        min_block_size: minimum size of a SequenceMatcher matching block
            that's considered a real anchor. Single-token matches are
            unreliable on a 12K-word transcript (every common word
            appears many times). Default 2 = at least a bigram anchors.
        cluster_window_seconds: tolerated distance (each side) from the
            LARGEST matching block. Discards spurious distant matches
            elsewhere in the meeting — common phrases like "I think"
            recur many times in a 77-minute transcript; without this
            filter, a 2-token match from 15 minutes earlier can drag the
            quote's apparent duration to 15+ minutes. Default 90s is
            generous (Kingman's longest single utterance in m101091 was
            ~95s) while still catching all the obvious outliers.

    Returns the alignment list (one row per display token) or None if
    alignment is impossible (empty inputs or zero match coverage).
    Empty `whisper_words` → None; empty `quote_text` → None.
    """
    if not quote_text or not quote_text.strip():
        return None
    if not whisper_words:
        return None

    display_tokens = _split_display_tokens(quote_text)
    if not display_tokens:
        return None

    # display_token_idx -> normalized form (only for tokens with content)
    normalized_pairs: list[tuple[int, str]] = []
    for di, tok in enumerate(display_tokens):
        norm = _normalize_token(tok)
        if norm:
            normalized_pairs.append((di, norm))

    if not normalized_pairs:
        # All-punctuation quote (degenerate case)
        return None

    quote_norm = [n for _, n in normalized_pairs]
    whisper_norm = [_normalize_token(w.get("word", "")) for w in whisper_words]

    sm = SequenceMatcher(a=quote_norm, b=whisper_norm, autojunk=False)
    all_blocks = [b for b in sm.get_matching_blocks() if b.size >= min_block_size]

    if not all_blocks:
        logger.warning(
            "align_quote: no matching blocks of size >= %d. Quote may be "
            "paraphrased or from a different meeting.",
            min_block_size,
        )
        return None

    # Dominant-cluster filter: anchor on the LARGEST matching block and
    # discard any block whose whisper-time falls outside a window around
    # it. Defeats the failure mode where a common bigram (e.g., "I think")
    # produces a spurious match elsewhere in the meeting that stretches
    # the quote's apparent duration over the entire transcript.
    largest = max(all_blocks, key=lambda b: b.size)
    anchor_start = float(whisper_words[largest.b].get("start") or 0.0)
    anchor_end = float(
        whisper_words[largest.b + largest.size - 1].get("end") or 0.0
    )
    window_lo = anchor_start - cluster_window_seconds
    window_hi = anchor_end + cluster_window_seconds

    filtered_blocks = []
    discarded = 0
    for b in all_blocks:
        b_start = float(whisper_words[b.b].get("start") or 0.0)
        if window_lo <= b_start <= window_hi:
            filtered_blocks.append(b)
        else:
            discarded += 1

    if discarded:
        logger.info(
            "align_quote: dominant cluster filter discarded %d outlier "
            "block(s) outside [%.1fs, %.1fs] window around the anchor.",
            discarded, window_lo, window_hi,
        )

    # Build matched: normalized-quote-index -> (start_s, end_s)
    matched_norm: dict[int, tuple[float, float]] = {}
    for b in filtered_blocks:
        for k in range(b.size):
            qi = b.a + k
            wi = b.b + k
            ww = whisper_words[wi]
            matched_norm[qi] = (
                float(ww.get("start") or 0.0),
                float(ww.get("end") or 0.0),
            )

    if not matched_norm:
        logger.warning(
            "align_quote: dominant cluster filter removed all blocks; "
            "alignment unreliable.",
        )
        return None

    # Translate normalized indices back to display indices, then
    # interpolate to cover every display token.
    matched_display: dict[int, tuple[float, float]] = {}
    for ni, (di, _) in enumerate(normalized_pairs):
        if ni in matched_norm:
            matched_display[di] = matched_norm[ni]

    timings = _interpolate_unmatched(len(display_tokens), matched_display)

    # Build the final output. Note: `word` is the DISPLAY token (with
    # capitalization + adjacent punctuation), not the normalized form,
    # so the karaoke UI can render the quote text verbatim.
    out: list[dict] = []
    for i, tok in enumerate(display_tokens):
        start_s, end_s = timings[i]
        out.append({
            "word": tok,
            "start_ms": int(round(start_s * 1000)),
            "end_ms": int(round(end_s * 1000)),
        })

    matched_count = len(matched_display)
    total = len(display_tokens)
    logger.info(
        "align_quote: %d/%d display tokens matched directly (%.0f%%); "
        "remainder interpolated.",
        matched_count, total, 100 * matched_count / total if total else 0,
    )

    return out


def _cluster_evidence_candidates(
    candidates: list[_EvidenceCandidate],
) -> list[_EvidenceCandidate]:
    """Collapse overlapping sliding windows that describe one occurrence."""
    ordered = sorted(
        candidates,
        key=lambda candidate: (
            candidate.first_word_position,
            -candidate.coverage,
            -candidate.direct_matches,
        ),
    )
    clusters: list[list[_EvidenceCandidate]] = []
    for candidate in ordered:
        for cluster in clusters:
            representative = cluster[0]
            # Sliding segments around one occurrence produce many overlapping
            # candidates.  Cluster by the directly matched start time, not by
            # broad segment overlap: a long segment can bridge two back-to-back
            # repeated motions and must not collapse them into one occurrence.
            same_occurrence = (
                abs(candidate.first_seconds - representative.first_seconds) <= 3.0
            )
            if same_occurrence:
                cluster.append(candidate)
                break
        else:
            clusters.append([candidate])

    return [
        max(
            cluster,
            key=lambda candidate: (
                candidate.coverage,
                candidate.direct_matches,
                -(candidate.last_word_position - candidate.first_word_position),
            ),
        )
        for cluster in clusters
    ]


def _find_evidence_candidates(
    quote_tokens: list[str],
    evidence_words: list[_EvidenceWord],
) -> list[_EvidenceCandidate]:
    """Find approximate occurrences without conflating repeated phrases."""
    if not quote_tokens or not evidence_words:
        return []

    candidate_starts: set[int] = set()
    transcript_tokens = [word.normalized for word in evidence_words]
    # Seed sliding alignment from the rarest matching trigrams (then bigrams,
    # then single tokens as a last resort).  The anchors are required to be
    # mostly verbatim, so this preserves recall above the coverage threshold
    # without evaluating every transcript position for every common word.
    for ngram_size in (3, 2, 1):
        if len(quote_tokens) < ngram_size:
            continue
        positions_by_ngram: dict[tuple[str, ...], list[int]] = {}
        for position in range(len(transcript_tokens) - ngram_size + 1):
            ngram = tuple(transcript_tokens[position:position + ngram_size])
            positions_by_ngram.setdefault(ngram, []).append(position)

        seeds: list[tuple[int, int, list[int]]] = []
        for quote_index in range(len(quote_tokens) - ngram_size + 1):
            ngram = tuple(quote_tokens[quote_index:quote_index + ngram_size])
            positions = positions_by_ngram.get(ngram)
            if positions:
                seeds.append((len(positions), quote_index, positions))
        if not seeds:
            continue

        for _, quote_index, positions in sorted(seeds)[:6]:
            for word_position in positions:
                expected = word_position - quote_index
                for offset in range(-2, 3):
                    start = expected + offset
                    if 0 <= start < len(evidence_words):
                        candidate_starts.add(start)
        break

    length_slack = max(3, min(8, len(quote_tokens) // 3))
    min_length = max(1, len(quote_tokens) - length_slack)
    max_length = len(quote_tokens) + length_slack
    candidates: list[_EvidenceCandidate] = []

    for segment_start in candidate_starts:
        for segment_length in range(min_length, max_length + 1):
            segment_end = min(len(evidence_words), segment_start + segment_length)
            if segment_end <= segment_start:
                continue
            segment = [
                word.normalized
                for word in evidence_words[segment_start:segment_end]
            ]
            matcher = SequenceMatcher(
                a=quote_tokens,
                b=segment,
                autojunk=False,
            )
            pairs: list[tuple[int, int]] = []
            for block in matcher.get_matching_blocks():
                for offset in range(block.size):
                    pairs.append(
                        (block.a + offset, segment_start + block.b + offset)
                    )
            if not pairs:
                continue

            direct_matches = len({quote_index for quote_index, _ in pairs})
            coverage = direct_matches / len(quote_tokens)
            first_word_position = min(word_position for _, word_position in pairs)
            last_word_position = max(word_position for _, word_position in pairs)
            direct_start_quote_index, direct_start_position = min(
                pairs,
                key=lambda pair: (pair[0], pair[1]),
            )
            candidates.append(
                _EvidenceCandidate(
                    coverage=coverage,
                    direct_matches=direct_matches,
                    first_seconds=evidence_words[first_word_position].start_seconds,
                    last_seconds=evidence_words[last_word_position].end_seconds,
                    first_word_position=first_word_position,
                    last_word_position=last_word_position,
                    direct_start_position=direct_start_position,
                    direct_start_quote_index=direct_start_quote_index,
                    segment_start=segment_start,
                    segment_end=segment_end,
                )
            )

    return _cluster_evidence_candidates(candidates)


def align_quote_with_evidence(
    quote_text: str,
    whisper_words: list[dict],
    *,
    window_start_seconds: float,
    window_end_seconds: float,
    min_coverage: float = 0.75,
    uniqueness_margin: float = 0.05,
    selection: str = "unique",
    reference_seconds: Optional[float] = None,
    unique_outside_window_min_coverage: Optional[float] = None,
) -> QuoteAlignmentEvidence:
    """Align a quote inside a time window and expose confidence evidence.

    Comparable occurrences elsewhere in the transcript are reported.  A
    repeated phrase may still resolve when exactly one occurrence is inside
    the caller's evidence window.  Callers may explicitly select the nearest
    occurrence to, or latest occurrence before, ``reference_seconds`` after
    the coverage gates pass.  A caller may explicitly permit one globally
    unique, higher-coverage occurrence outside the window; comparable global
    occurrences still fail closed.  The returned timestamp always comes from
    a directly matched Whisper token.  ``align_quote`` is called only after an
    occurrence passes these gates, preserving its existing public behavior
    while adding an honest wrapper for locator use.
    """
    if (
        not math.isfinite(window_start_seconds)
        or not math.isfinite(window_end_seconds)
        or window_start_seconds < 0
        or window_end_seconds < window_start_seconds
    ):
        raise ValueError(
            "invalid quote-alignment window: "
            f"{(window_start_seconds, window_end_seconds)!r}"
        )
    if not 0 < min_coverage <= 1:
        raise ValueError(f"min_coverage must be in (0, 1], got {min_coverage!r}")
    if not 0 <= uniqueness_margin < 1:
        raise ValueError(
            f"uniqueness_margin must be in [0, 1), got {uniqueness_margin!r}"
        )
    if (
        unique_outside_window_min_coverage is not None
        and not 0 < unique_outside_window_min_coverage <= 1
    ):
        raise ValueError(
            "unique_outside_window_min_coverage must be in (0, 1], got "
            f"{unique_outside_window_min_coverage!r}"
        )
    if selection not in {"unique", "nearest", "latest"}:
        raise ValueError(f"unsupported quote selection: {selection!r}")
    if selection != "unique":
        if (
            not isinstance(reference_seconds, (int, float))
            or isinstance(reference_seconds, bool)
            or not math.isfinite(float(reference_seconds))
            or float(reference_seconds) < 0
        ):
            raise ValueError(
                f"selection={selection!r} requires a non-negative "
                f"reference_seconds, got {reference_seconds!r}"
            )
        reference_seconds = float(reference_seconds)

    quote_tokens = [
        _normalize_anchor_token(token)
        for token in _split_display_tokens(quote_text)
        if _normalize_anchor_token(token)
    ]
    evidence_words: list[_EvidenceWord] = []
    for original_index, row in enumerate(whisper_words):
        if not isinstance(row, dict):
            continue
        raw = row.get("word", row.get("text", ""))
        start = row.get("start")
        if (
            not isinstance(raw, str)
            or not isinstance(start, (int, float))
            or isinstance(start, bool)
            or not math.isfinite(float(start))
            or float(start) < 0
        ):
            continue
        end = row.get("end")
        end_seconds = (
            float(end)
            if isinstance(end, (int, float))
            and not isinstance(end, bool)
            and math.isfinite(float(end))
            and float(end) >= float(start)
            else float(start)
        )
        normalized = _normalize_anchor_token(raw)
        if normalized:
            evidence_words.append(
                _EvidenceWord(
                    normalized=normalized,
                    start_seconds=float(start),
                    end_seconds=end_seconds,
                    original_index=original_index,
                    row=row,
                    quarantined=_is_quarantined_word(row),
                )
            )

    def _candidate_distance(candidate: _EvidenceCandidate) -> float:
        if reference_seconds is not None:
            return abs(candidate.first_seconds - reference_seconds)
        if candidate.first_seconds < window_start_seconds:
            return window_start_seconds - candidate.first_seconds
        if candidate.last_seconds > window_end_seconds:
            return candidate.last_seconds - window_end_seconds
        return 0.0

    def result(
        *,
        success: bool,
        reason: str,
        direct_matches: int = 0,
        coverage: float = 0.0,
        unique: bool = False,
        uniqueness: str = "unresolved",
        comparable_matches: tuple[dict, ...] = (),
        candidate_count: int = 0,
        in_window_candidate_count: int = 0,
        best_candidate: Optional[_EvidenceCandidate] = None,
        start_seconds: Optional[float] = None,
        matched_word_index: Optional[int] = None,
        matched_end_word_index: Optional[int] = None,
        timings: Optional[list[dict]] = None,
    ) -> QuoteAlignmentEvidence:
        return QuoteAlignmentEvidence(
            success=success,
            reason=reason,
            quote_tokens=len(quote_tokens),
            direct_matches=direct_matches,
            coverage=coverage,
            unique=unique,
            uniqueness=uniqueness,
            comparable_matches=comparable_matches,
            candidate_count=candidate_count,
            in_window_candidate_count=in_window_candidate_count,
            best_candidate_start_seconds=(
                best_candidate.first_seconds if best_candidate else None
            ),
            best_candidate_end_seconds=(
                best_candidate.last_seconds if best_candidate else None
            ),
            best_candidate_direct_matches=(
                best_candidate.direct_matches if best_candidate else 0
            ),
            best_candidate_coverage=(
                best_candidate.coverage if best_candidate else 0.0
            ),
            best_candidate_distance_seconds=(
                _candidate_distance(best_candidate) if best_candidate else None
            ),
            window_start_seconds=window_start_seconds,
            window_end_seconds=window_end_seconds,
            start_seconds=start_seconds,
            matched_word_index=matched_word_index,
            matched_end_word_index=matched_end_word_index,
            timings=timings,
        )

    if not quote_tokens:
        return result(success=False, reason="empty_quote")
    if not evidence_words:
        return result(success=False, reason="empty_transcript")

    all_candidates = _find_evidence_candidates(quote_tokens, evidence_words)
    if not all_candidates:
        return result(success=False, reason="no_direct_match")

    quarantined_candidates = [
        candidate
        for candidate in all_candidates
        if any(
            evidence_words[position].quarantined
            for position in range(
                candidate.first_word_position,
                candidate.last_word_position + 1,
            )
        )
    ]
    candidates = [
        candidate
        for candidate in all_candidates
        if candidate not in quarantined_candidates
    ]
    best_quarantined = (
        max(
            quarantined_candidates,
            key=lambda candidate: (
                candidate.coverage,
                candidate.direct_matches,
                -candidate.first_seconds,
            ),
        )
        if quarantined_candidates
        else None
    )
    best_clean_coverage = max(
        (candidate.coverage for candidate in candidates),
        default=0.0,
    )
    if (
        best_quarantined is not None
        and best_quarantined.coverage >= min_coverage
        and best_clean_coverage < min_coverage
    ):
        return result(
            success=False,
            reason="match_in_quarantined_span",
            direct_matches=best_quarantined.direct_matches,
            coverage=best_quarantined.coverage,
            candidate_count=len(all_candidates),
            best_candidate=best_quarantined,
        )
    if not candidates:
        assert best_quarantined is not None
        return result(
            success=False,
            reason="match_in_quarantined_span",
            direct_matches=best_quarantined.direct_matches,
            coverage=best_quarantined.coverage,
            candidate_count=len(all_candidates),
            best_candidate=best_quarantined,
        )

    def _best_by_coverage(
        pool: list[_EvidenceCandidate],
    ) -> _EvidenceCandidate:
        return max(
            pool,
            key=lambda candidate: (
                candidate.coverage,
                candidate.direct_matches,
                -_candidate_distance(candidate),
                -candidate.first_seconds,
            ),
        )

    def _comparable_rows(
        pool: list[_EvidenceCandidate],
        best_coverage: float,
    ) -> tuple[dict, ...]:
        floor = max(0.0, best_coverage - uniqueness_margin)
        comparable = [
            candidate for candidate in pool if candidate.coverage >= floor
        ]
        return tuple(
            {
                "start_seconds": candidate.first_seconds,
                "end_seconds": candidate.last_seconds,
                "direct_matches": candidate.direct_matches,
                "coverage": round(candidate.coverage, 4),
                "distance_seconds": round(_candidate_distance(candidate), 4),
                "in_window": candidate in in_window,
            }
            for candidate in sorted(
                comparable,
                key=lambda candidate: candidate.first_seconds,
            )
        )

    in_window = [
        candidate
        for candidate in candidates
        if window_start_seconds <= candidate.first_seconds <= window_end_seconds
        and candidate.last_seconds <= window_end_seconds
    ]
    best_global = _best_by_coverage(candidates)
    global_comparable_floor = max(
        min_coverage,
        best_global.coverage - uniqueness_margin,
    )
    globally_comparable_to_best = [
        candidate
        for candidate in candidates
        if candidate.coverage >= global_comparable_floor
    ]
    outside_override_allowed = (
        unique_outside_window_min_coverage is not None
        and best_global.coverage >= unique_outside_window_min_coverage
        and len(globally_comparable_to_best) == 1
        and best_global not in in_window
    )
    selected_outside_window = False
    if outside_override_allowed:
        best = best_global
        comparable_floor = global_comparable_floor
        comparable_rows = _comparable_rows(
            candidates,
            best.coverage,
        )
        selected_outside_window = True
    elif not in_window:
        comparable_rows = _comparable_rows(
            candidates,
            best_global.coverage,
        )
        return result(
            success=False,
            reason="no_match_in_window",
            direct_matches=best_global.direct_matches,
            coverage=best_global.coverage,
            comparable_matches=comparable_rows,
            candidate_count=len(candidates),
            in_window_candidate_count=0,
            best_candidate=best_global,
        )
    else:
        coverage_best = _best_by_coverage(in_window)
        comparable_rows = _comparable_rows(candidates, coverage_best.coverage)
        if coverage_best.coverage < min_coverage:
            return result(
                success=False,
                reason="coverage_below_threshold",
                direct_matches=coverage_best.direct_matches,
                coverage=coverage_best.coverage,
                comparable_matches=comparable_rows,
                candidate_count=len(candidates),
                in_window_candidate_count=len(in_window),
                best_candidate=coverage_best,
            )

        comparable_floor = max(
            min_coverage,
            coverage_best.coverage - uniqueness_margin,
        )
        comparable_in_window = [
            candidate
            for candidate in in_window
            if candidate.coverage >= comparable_floor
        ]
        if selection == "nearest":
            best = min(
                comparable_in_window,
                key=lambda candidate: (
                    _candidate_distance(candidate),
                    -candidate.coverage,
                    -candidate.direct_matches,
                    candidate.first_seconds,
                ),
            )
            equally_near = [
                candidate
                for candidate in comparable_in_window
                if math.isclose(
                    _candidate_distance(candidate),
                    _candidate_distance(best),
                    abs_tol=1e-6,
                )
            ]
            if len(equally_near) != 1:
                return result(
                    success=False,
                    reason="selection_tie",
                    direct_matches=best.direct_matches,
                    coverage=best.coverage,
                    comparable_matches=comparable_rows,
                    candidate_count=len(candidates),
                    in_window_candidate_count=len(in_window),
                    best_candidate=best,
                )
        elif selection == "latest":
            best = max(
                comparable_in_window,
                key=lambda candidate: (
                    candidate.first_seconds,
                    candidate.coverage,
                    candidate.direct_matches,
                ),
            )
        elif len(comparable_in_window) != 1:
            best = coverage_best
            return result(
                success=False,
                reason="non_unique_in_window",
                direct_matches=best.direct_matches,
                coverage=best.coverage,
                unique=False,
                uniqueness="unresolved",
                comparable_matches=comparable_rows,
                candidate_count=len(candidates),
                in_window_candidate_count=len(in_window),
                best_candidate=best,
            )
        else:
            best = comparable_in_window[0]

    direct_word = evidence_words[best.direct_start_position]
    selected_rows = [
        word.row
        for word in evidence_words[best.segment_start:best.segment_end]
        if not word.quarantined
    ]
    timings = align_quote(
        quote_text,
        selected_rows,
        min_block_size=1,
        cluster_window_seconds=max(
            1.0,
            window_end_seconds - window_start_seconds,
        ),
    )
    if timings is None:
        return result(
            success=False,
            reason="align_quote_rejected_selected_match",
            direct_matches=best.direct_matches,
            coverage=best.coverage,
            comparable_matches=comparable_rows,
            candidate_count=len(candidates),
            in_window_candidate_count=len(in_window),
            best_candidate=best,
        )

    globally_comparable = [
        candidate
        for candidate in candidates
        if candidate.coverage >= comparable_floor
    ]
    globally_unique = len(globally_comparable) == 1
    if selected_outside_window:
        uniqueness = "unique_outside_window"
    elif globally_unique:
        uniqueness = "unique"
    elif selection == "nearest":
        uniqueness = "resolved_by_nearest"
    elif selection == "latest":
        uniqueness = "resolved_by_latest"
    else:
        uniqueness = "resolved_by_window"
    return result(
        success=True,
        reason=(
            "aligned_unique_outside_window"
            if selected_outside_window
            else "aligned"
        ),
        direct_matches=best.direct_matches,
        coverage=best.coverage,
        unique=globally_unique,
        uniqueness=uniqueness,
        comparable_matches=comparable_rows,
        candidate_count=len(candidates),
        in_window_candidate_count=len(in_window),
        best_candidate=best,
        start_seconds=direct_word.start_seconds,
        matched_word_index=direct_word.original_index,
        matched_end_word_index=(
            evidence_words[best.last_word_position].original_index
        ),
        timings=timings,
    )


def align_meeting_quotes(
    meeting_id: int,
    db_connection=None,
) -> dict:
    """Align every `member_quotes` row for a meeting against that
    meeting's Whisper transcript, persisting `word_timings` to each.

    Idempotent: skips rows where `word_timings` is already populated AND
    non-empty unless `force=True` (not currently exposed; reprocess by
    NULL-ing the column manually if needed). Rows where alignment fails
    (no matching blocks) get `word_timings` set to NULL — these are
    candidates for the parallel-notebook verification path (D-041)
    since the canonical-transcript-anchored alignment couldn't place
    them.

    Returns a dict of stats:
        {
          "meeting_id": int,
          "skipped_already_aligned": int,
          "aligned": int,
          "failed": int,        # alignment returned None
          "no_transcript": bool, # meeting lacks transcript_words output
        }

    Safe to call on any meeting. If transcript_words isn't in
    `notebook_outputs` yet for this meeting, returns
    `{"no_transcript": True, ...zeros}` — no error, no DB write.
    Caller (typically the fetcher) is expected to call this whenever
    EITHER `transcript_words` OR `member_quotes_topic` lands for a
    meeting, since either landing might unblock alignment.
    """
    import json
    import sys
    from pathlib import Path

    # We don't take `get_connection` as a parameter to avoid an import
    # cycle — this module sits in parsers/ alongside database.py and
    # is imported by fetcher.py from zspan_pipeline/.
    if db_connection is None:
        # Lazy import to avoid pulling database into modules that just
        # want align_quote().
        _parsers_dir = Path(__file__).resolve().parent
        if str(_parsers_dir) not in sys.path:
            sys.path.insert(0, str(_parsers_dir))
        from database import get_connection
        conn = get_connection()
        owns_conn = True
    else:
        conn = db_connection
        owns_conn = False

    cur = conn.cursor()
    stats = {
        "meeting_id": meeting_id,
        "skipped_already_aligned": 0,
        "aligned": 0,
        "failed": 0,
        "no_transcript": False,
    }

    try:
        # Pull the Whisper transcript for this meeting.
        cur.execute(
            """
            SELECT content FROM notebook_outputs
            WHERE meeting_id = ? AND output_type = 'transcript_words'
              AND content IS NOT NULL AND content != ''
            """,
            (meeting_id,),
        )
        row = cur.fetchone()
        if not row:
            stats["no_transcript"] = True
            return stats

        try:
            payload = json.loads(row["content"])
            whisper_words = payload.get("words") or []
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(
                "align_meeting_quotes meeting=%s: transcript JSON parse "
                "failed (%s); aborting alignment.",
                meeting_id, e,
            )
            stats["no_transcript"] = True
            return stats

        if not whisper_words:
            stats["no_transcript"] = True
            return stats

        # Pull the unaligned quotes.
        cur.execute(
            """
            SELECT id, quote_text, word_timings
            FROM member_quotes
            WHERE meeting_id = ?
            """,
            (meeting_id,),
        )
        quotes = cur.fetchall()
        if not quotes:
            return stats

        for q in quotes:
            existing = q["word_timings"]
            if existing and existing.strip() not in ("", "null", "[]"):
                stats["skipped_already_aligned"] += 1
                continue

            timings = align_quote(q["quote_text"], whisper_words)
            if timings is None:
                stats["failed"] += 1
                continue

            cur.execute(
                "UPDATE member_quotes SET word_timings = ? WHERE id = ?",
                (json.dumps(timings, ensure_ascii=False), q["id"]),
            )
            stats["aligned"] += 1

        conn.commit()
    finally:
        if owns_conn:
            conn.close()

    logger.info(
        "align_meeting_quotes meeting=%s: aligned=%d failed=%d "
        "skipped_already=%d",
        meeting_id, stats["aligned"], stats["failed"],
        stats["skipped_already_aligned"],
    )
    return stats


_CITATION_RE = re.compile(r"\[\d+(?:,\s*\d+)*\]")


def _strip_citations(text: str) -> str:
    """Remove legacy citation markers like `[1]` or `[1, 2]` that
    appear inline in raw chat.ask outputs. The frontend already strips
    them at render time; alignment should match the displayed form, so
    we strip before computing per-word timings.
    """
    return _CITATION_RE.sub("", text or "").strip()


def align_council_quotes_for_meeting(
    meeting_id: int,
    db_connection=None,
) -> dict:
    """Align every council_quotes entry for a meeting against its Whisper
    transcript, persisting `word_timings` inline on each quote object
    within the notebook_outputs.content JSON.

    council_quotes (T-003) is stored as a JSON blob — not as structured
    DB rows — so the persistence pattern differs from
    `align_meeting_quotes`. We:

      1. Load the JSON content from notebook_outputs (may be wrapped in
         a `\`\`\`json ... \`\`\`` markdown fence).
      2. For each quote object, run `align_quote` on its
         citation-stripped text (citations like `[1]` would never match
         a Whisper word and slightly skew alignment if left in).
      3. Set `quote["word_timings"]` inline.
      4. Re-serialize, preserving the original fence wrapping so the
         downstream parser (`parseCouncilQuotes` in
         client/src/utils/councilQuotes.ts) keeps working.
      5. UPDATE notebook_outputs.content with the modified payload.

    Idempotent: skips quotes whose `word_timings` is already populated.
    Returns a stats dict similar to `align_meeting_quotes`.
    """
    import json
    import sys
    from pathlib import Path

    if db_connection is None:
        _parsers_dir = Path(__file__).resolve().parent
        if str(_parsers_dir) not in sys.path:
            sys.path.insert(0, str(_parsers_dir))
        from database import get_connection
        conn = get_connection()
        owns_conn = True
    else:
        conn = db_connection
        owns_conn = False

    cur = conn.cursor()
    stats = {
        "meeting_id": meeting_id,
        "skipped_already_aligned": 0,
        "aligned": 0,
        "failed": 0,
        "no_transcript": False,
        "no_council_quotes": False,
    }

    try:
        # Whisper transcript
        cur.execute(
            """
            SELECT content FROM notebook_outputs
            WHERE meeting_id = ? AND output_type = 'transcript_words'
              AND content IS NOT NULL AND content != ''
            """,
            (meeting_id,),
        )
        row = cur.fetchone()
        if not row:
            stats["no_transcript"] = True
            return stats
        try:
            transcript = json.loads(row["content"])
            whisper_words = transcript.get("words") or []
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(
                "align_council_quotes_for_meeting meeting=%s: transcript "
                "parse failed (%s)", meeting_id, e,
            )
            stats["no_transcript"] = True
            return stats
        if not whisper_words:
            stats["no_transcript"] = True
            return stats

        # council_quotes JSON
        cur.execute(
            """
            SELECT content FROM notebook_outputs
            WHERE meeting_id = ? AND output_type = 'council_quotes'
              AND content IS NOT NULL AND content != ''
            """,
            (meeting_id,),
        )
        cq_row = cur.fetchone()
        if not cq_row:
            stats["no_council_quotes"] = True
            return stats

        raw_content = cq_row["content"]

        # Detect markdown fence to preserve on output.
        fence_match = re.match(
            r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$",
            raw_content,
            re.DOTALL,
        )
        json_str = fence_match.group(1) if fence_match else raw_content
        had_fence = fence_match is not None

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.warning(
                "align_council_quotes_for_meeting meeting=%s: council_quotes "
                "JSON parse failed (%s); aborting", meeting_id, e,
            )
            stats["no_council_quotes"] = True
            return stats

        quotes = data.get("quotes")
        if not isinstance(quotes, list) or not quotes:
            stats["no_council_quotes"] = True
            return stats

        modified = False
        for q in quotes:
            if not isinstance(q, dict):
                continue
            text = q.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            existing = q.get("word_timings")
            if existing and isinstance(existing, list) and len(existing) > 0:
                stats["skipped_already_aligned"] += 1
                continue
            stripped = _strip_citations(text)
            timings = align_quote(stripped, whisper_words)
            if timings is None:
                stats["failed"] += 1
                continue
            q["word_timings"] = timings
            stats["aligned"] += 1
            modified = True

        if modified:
            new_json = json.dumps(data, ensure_ascii=False, indent=2)
            new_content = (
                f"```json\n{new_json}\n```" if had_fence else new_json
            )
            cur.execute(
                """
                UPDATE notebook_outputs
                SET content = ?, generated_at = CURRENT_TIMESTAMP
                WHERE meeting_id = ? AND output_type = 'council_quotes'
                """,
                (new_content, meeting_id),
            )
            conn.commit()
    finally:
        if owns_conn:
            conn.close()

    logger.info(
        "align_council_quotes_for_meeting meeting=%s: aligned=%d failed=%d "
        "skipped_already=%d", meeting_id, stats["aligned"], stats["failed"],
        stats["skipped_already_aligned"],
    )
    return stats


def align_tracked_claims_for_meeting(
    meeting_id: int,
    db_connection=None,
) -> dict:
    """Align every `tracked_claims` row for a meeting against that
    meeting's Whisper transcript, persisting `word_timings` to each.

    Sibling to `align_meeting_quotes` — same algorithm, different table.
    Tracked claims (T-012) ride the same karaoke surface as member_quotes
    so the marker-styled accountability UI can render verbatim audio
    of each forward-looking statement.

    Idempotent: skips rows where `word_timings` is already populated.
    No-op when transcript_words hasn't landed yet for this meeting.
    """
    import json
    import sys
    from pathlib import Path

    if db_connection is None:
        _parsers_dir = Path(__file__).resolve().parent
        if str(_parsers_dir) not in sys.path:
            sys.path.insert(0, str(_parsers_dir))
        from database import get_connection
        conn = get_connection()
        owns_conn = True
    else:
        conn = db_connection
        owns_conn = False

    cur = conn.cursor()
    stats = {
        "meeting_id": meeting_id,
        "skipped_already_aligned": 0,
        "aligned": 0,
        "failed": 0,
        "no_transcript": False,
    }

    try:
        cur.execute(
            """
            SELECT content FROM notebook_outputs
            WHERE meeting_id = ? AND output_type = 'transcript_words'
              AND content IS NOT NULL AND content != ''
            """,
            (meeting_id,),
        )
        row = cur.fetchone()
        if not row:
            stats["no_transcript"] = True
            return stats
        try:
            payload = json.loads(row["content"])
            whisper_words = payload.get("words") or []
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(
                "align_tracked_claims_for_meeting meeting=%s: transcript "
                "parse failed (%s)", meeting_id, e,
            )
            stats["no_transcript"] = True
            return stats
        if not whisper_words:
            stats["no_transcript"] = True
            return stats

        cur.execute(
            """
            SELECT id, claim_text, word_timings
            FROM tracked_claims
            WHERE meeting_id = ?
            """,
            (meeting_id,),
        )
        claims = cur.fetchall()
        if not claims:
            return stats

        for c in claims:
            existing = c["word_timings"]
            if existing and existing.strip() not in ("", "null", "[]"):
                stats["skipped_already_aligned"] += 1
                continue
            timings = align_quote(c["claim_text"], whisper_words)
            if timings is None:
                stats["failed"] += 1
                continue
            cur.execute(
                "UPDATE tracked_claims SET word_timings = ? WHERE id = ?",
                (json.dumps(timings, ensure_ascii=False), c["id"]),
            )
            stats["aligned"] += 1
        conn.commit()
    finally:
        if owns_conn:
            conn.close()

    logger.info(
        "align_tracked_claims_for_meeting meeting=%s: aligned=%d failed=%d "
        "skipped_already=%d", meeting_id, stats["aligned"], stats["failed"],
        stats["skipped_already_aligned"],
    )
    return stats


def align_quotes_for_meeting(
    meeting_id: int,
    db_connection=None,
) -> dict:
    """Align every row in the unified `quotes` table for a meeting against
    that meeting's Whisper transcript, persisting `word_timings` to each.

    Sibling to `align_meeting_quotes` (which targets the legacy
    `member_quotes` table) and `align_council_quotes_for_meeting` (which
    targets the legacy council_quotes JSON blob). Operates on the new
    canonical `quotes` table introduced by the Quotes Unification Refactor
    (2026-05-26) — see 01_Project_Overview/REFACTOR_QUOTES_UNIFICATION.md.

    Strips legacy citation markers (`[1]`, `[1, 2]`) before alignment
    so the per-word timings match the displayed form (the frontend strips
    citations at render time too).

    Idempotent: skips rows where `word_timings` is already populated and
    non-empty. Rows where V3 ingest NULLed word_timings (text correction)
    are eligible for re-alignment on the next pass — same shape as
    `align_meeting_quotes`.

    Returns a stats dict matching `align_meeting_quotes`:
        {
          "meeting_id": int,
          "skipped_already_aligned": int,
          "aligned": int,
          "failed": int,
          "no_transcript": bool,
        }

    If transcript_words isn't in notebook_outputs yet, returns
    `{"no_transcript": True, ...zeros}` — safe to call defensively after
    every quotes-extraction OR transcript-words-extraction event. The
    fetcher calls it from BOTH sides so whichever lands second unblocks
    alignment.
    """
    import json
    import sys
    from pathlib import Path

    if db_connection is None:
        _parsers_dir = Path(__file__).resolve().parent
        if str(_parsers_dir) not in sys.path:
            sys.path.insert(0, str(_parsers_dir))
        from database import get_connection
        conn = get_connection()
        owns_conn = True
    else:
        conn = db_connection
        owns_conn = False

    cur = conn.cursor()
    stats = {
        "meeting_id": meeting_id,
        "skipped_already_aligned": 0,
        "aligned": 0,
        "failed": 0,
        "no_transcript": False,
    }

    try:
        # Whisper transcript
        cur.execute(
            """
            SELECT content FROM notebook_outputs
            WHERE meeting_id = ? AND output_type = 'transcript_words'
              AND content IS NOT NULL AND content != ''
            """,
            (meeting_id,),
        )
        row = cur.fetchone()
        if not row:
            stats["no_transcript"] = True
            return stats
        try:
            payload = json.loads(row["content"])
            whisper_words = payload.get("words") or []
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(
                "align_quotes_for_meeting meeting=%s: transcript JSON parse "
                "failed (%s); aborting alignment.", meeting_id, e,
            )
            stats["no_transcript"] = True
            return stats
        if not whisper_words:
            stats["no_transcript"] = True
            return stats

        # Pull unaligned quotes from the new canonical table
        cur.execute(
            """
            SELECT id, quote_text, word_timings
            FROM quotes
            WHERE meeting_id = ?
            """,
            (meeting_id,),
        )
        quotes_rows = cur.fetchall()
        if not quotes_rows:
            return stats

        for q in quotes_rows:
            existing = q["word_timings"]
            if existing and existing.strip() not in ("", "null", "[]"):
                stats["skipped_already_aligned"] += 1
                continue

            # Strip legacy citation markers defensively — they'd never
            # match a Whisper word and would slightly skew alignment.
            quote_text = _strip_citations(q["quote_text"] or "")
            if not quote_text:
                stats["failed"] += 1
                continue

            timings = align_quote(quote_text, whisper_words)
            if timings is None:
                stats["failed"] += 1
                continue

            cur.execute(
                "UPDATE quotes SET word_timings = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (json.dumps(timings, ensure_ascii=False), q["id"]),
            )
            stats["aligned"] += 1

        conn.commit()
    finally:
        if owns_conn:
            conn.close()

    logger.info(
        "align_quotes_for_meeting meeting=%s: aligned=%d failed=%d "
        "skipped_already=%d", meeting_id, stats["aligned"], stats["failed"],
        stats["skipped_already_aligned"],
    )
    return stats


def alignment_coverage(timings: list[dict], whisper_words: list[dict]) -> float:
    """Return the fraction of display tokens that fall within the
    whisper transcript's actual time range. Useful as a quality check —
    if alignment coverage is low (e.g., 0.5), the quote may be
    misaligned (matched against the wrong audio segment).
    """
    if not timings or not whisper_words:
        return 0.0
    audio_end = max(float(w.get("end") or 0.0) for w in whisper_words)
    if audio_end <= 0:
        return 0.0
    in_range = sum(
        1 for t in timings
        if 0 <= t.get("start_ms", -1) / 1000 <= audio_end
    )
    return in_range / len(timings)
