"""Inline citation validation and deterministic key-decision alignment.

Canonical generations use ``[at H:MM:SS]``.  The legacy flat-minute
``[at MM:SS]`` form remains readable so already-generated meetings do not
lose their seek chips.  A fallback citation is trustworthy only when it falls
inside the meeting's timed range and inside, or close to, one of the transcript
chunks supplied to synthesis.  A quote-aligned citation has independent
transcript evidence, so callers may retain a chunk miss as an observation.

The primary alignment pass deliberately does not call another LLM.  Synthesis
supplies a distinctive item-introduction quote and a later action quote; both
are confidence-checked against timed transcript words.  The coarse locator
anchors the action search, then the item introduction resolves to the latest
qualifying occurrence in the full transcript prefix before that action.  The
older outcome-signature scanner is retained only as a logged,
lower-confidence fallback for legacy outputs that provide no quote anchors.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping, Sequence

from council_navigator.parsers.quote_align import (
    QuoteAlignmentEvidence,
    align_quote_with_evidence,
)


logger = logging.getLogger(__name__)


CANONICAL_CITATION_RE = re.compile(
    r"\[at (?P<hours>0|[1-9]\d*):(?P<hour_minutes>[0-5]\d):"
    r"(?P<hour_seconds>[0-5]\d)\]"
)
LEGACY_CITATION_RE = re.compile(
    r"\[at (?P<legacy_minutes>\d{1,3}):(?P<legacy_seconds>[0-5]\d)\]"
)
CITATION_RE = re.compile(
    r"\[at (?:(?P<hours>0|[1-9]\d*):(?P<hour_minutes>[0-5]\d):"
    r"(?P<hour_seconds>[0-5]\d)|(?P<legacy_minutes>\d{1,3}):"
    r"(?P<legacy_seconds>[0-5]\d))\]"
)
_VERBATIM_ANCHOR_RE = re.compile(r'\[at "(?P<quote>[^\r\n]+?)"\]')
_ANCHOR_MARKER_RE = re.compile(r"\[at\b", flags=re.IGNORECASE)

_AUDIT_BLOCK_RE = re.compile(
    r"<!--\s*audit\b.*?\baudit\s*-->",
    flags=re.IGNORECASE | re.DOTALL,
)
_TRAILING_COMMENT_RE = re.compile(r"<!--.*?-->\s*$", flags=re.DOTALL)
_NUMBERED_ITEM_RE = re.compile(r"^[^\S\r\n]*(\d{1,2})[.)][^\S\r\n]+", re.MULTILINE)
_TOKEN_RE = re.compile(r"[a-z0-9]+")

# A word-precise locator may land just outside a retrieved chunk because the
# chunker splits overlapping word windows.  Three minutes covers one ordinary
# neighboring chunk while still rejecting arbitrary moments elsewhere in a
# long meeting.
DEFAULT_NEAR_CHUNK_SECONDS = 180.0
MAX_ALIGNMENT_DISTANCE_SECONDS = 1_200.0
ACTION_ANCHOR_WINDOW_SECONDS = 360.0
ANCHOR_MIN_COVERAGE = 0.75
# Spending the coarse locator's window protection requires near-verbatim
# evidence.  At 90%, one globally comparable occurrence may outrank the model
# timestamp; the ordinary in-window path retains its more tolerant floor.
UNIQUE_ACTION_MIN_COVERAGE = 0.90
_CONTEXT_WORDS = 300
_VERBATIM_ANCHOR_PADDING_SECONDS = 2.0

_STOPWORDS = {
    "about", "after", "again", "against", "also", "and", "appointed",
    "approved", "approving", "authorized", "awarded", "because", "been",
    "before", "being", "between", "council", "decision", "directed", "for",
    "from", "have", "into", "item", "meeting", "motion", "of", "on", "or",
    "resolution", "that", "the", "their", "this", "through", "to", "under",
    "vote", "voted", "was", "were", "with", "would",
}

# Legacy fallback only: pattern, base score, appointment-only.  These phrases
# are never consulted when synthesis supplied an anchor record, even when that
# record fails alignment.
_OUTCOME_SIGNATURES: tuple[tuple[tuple[str, ...], float, bool], ...] = (
    (("we", "have", "decided", "to", "ask"), 92.0, True),
    (("decided", "to", "ask"), 88.0, True),
    (("move", "to", "appoint"), 86.0, True),
    (("motion", "to", "appoint"), 84.0, True),
    (("make", "a", "motion", "that", "we", "approve"), 82.0, False),
    (("make", "a", "motion", "to", "approve"), 80.0, False),
    (("make", "a", "motion"), 76.0, False),
    (("motion", "to", "approve"), 72.0, False),
    (("motion", "that", "approving"), 70.0, False),
    (("the", "motion", "carries"), 62.0, False),
    (("motion", "carries"), 60.0, False),
    (("all", "those", "in", "favor"), 54.0, False),
    (("so", "moved"), 46.0, False),
)


@dataclass(frozen=True)
class Citation:
    hours: int
    minutes: int
    seconds: int
    total_seconds: int
    raw: str
    start: int
    end: int
    canonical: bool


@dataclass
class ValidationReport:
    state: str
    decisions_total: int
    covered_indices: list[int]
    uncovered_indices: list[int]
    citations_total: int
    member_citations: int
    unknown_citations: list[str]
    nonmember_observations: list[dict[str, Any]]
    per_decision: list[dict[str, Any]]


@dataclass
class AlignmentReport:
    text: str
    decisions_total: int
    aligned_indices: list[int]
    failures: list[dict[str, Any]]
    per_decision: list[dict[str, Any]]
    index_map: list[dict[str, int]]


@dataclass(frozen=True)
class VerbatimAnchorResolution:
    text: str
    state: str  # "resolved" | "degraded" | "nonconforming" | "uncheckable"
    anchors_total: int
    aligned: tuple[dict[str, Any], ...]
    failures: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class _TimedToken:
    token: str
    start_seconds: float


@dataclass(frozen=True)
class _AlignmentCandidate:
    start_seconds: float
    signature: str
    score: float
    keyword_score: float


def parse_citations(text: str) -> list[Citation]:
    """Return every supported inline citation and its text span."""
    citations: list[Citation] = []
    for match in CITATION_RE.finditer(text):
        if match.group("hours") is not None:
            hours = int(match.group("hours"))
            minute_in_hour = int(match.group("hour_minutes"))
            seconds = int(match.group("hour_seconds"))
            total_seconds = hours * 3600 + minute_in_hour * 60 + seconds
            canonical = True
        else:
            legacy_minutes = int(match.group("legacy_minutes"))
            seconds = int(match.group("legacy_seconds"))
            total_seconds = legacy_minutes * 60 + seconds
            hours = total_seconds // 3600
            minute_in_hour = (total_seconds % 3600) // 60
            canonical = False
        citations.append(
            Citation(
                hours=hours,
                # Preserve the restored validator's flat-minute attribute for
                # callers that inspected it before canonical H:MM:SS existed.
                minutes=total_seconds // 60,
                seconds=seconds,
                total_seconds=total_seconds,
                raw=match.group(0),
                start=match.start(),
                end=match.end(),
                canonical=canonical,
            )
        )
    return citations


def format_citation(total_seconds: float) -> str:
    """Format a timed word start in the canonical ``[at H:MM:SS]`` shape."""
    if not math.isfinite(total_seconds) or total_seconds < 0:
        raise ValueError(f"invalid citation time: {total_seconds!r}")
    whole_seconds = int(total_seconds)
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"[at {hours}:{minutes:02d}:{seconds:02d}]"


def strip_audit_block(text: str) -> str:
    """Remove audit comments and tolerate a generic trailing comment."""
    without_audit = _AUDIT_BLOCK_RE.sub("", text)
    return _TRAILING_COMMENT_RE.sub("", without_audit).rstrip()


def split_numbered_items(text: str) -> list[str]:
    """Split a numbered-list response into decision bodies."""
    body = strip_audit_block(text)
    matches = list(_NUMBERED_ITEM_RE.finditer(body))
    if not matches:
        return []

    items: list[str] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        items.append(body[match.end():end].strip())
    return items


def allowed_seconds(chunk_start_seconds: Iterable[float]) -> set[int]:
    """Backward-compatible helper for legacy chunk-start callers/tests."""
    return {int(start_seconds) for start_seconds in chunk_start_seconds}


def chunk_time_ranges(chunks: Iterable[Any]) -> list[tuple[float, float]]:
    """Normalize retrieved chunks, DB rows, or ``(start, end)`` tuples."""
    ranges: list[tuple[float, float]] = []
    for chunk in chunks:
        if isinstance(chunk, (int, float)) and not isinstance(chunk, bool):
            # Legacy callers passed chunk starts and the prompt rendered them
            # by flooring fractional seconds.
            start = end = float(int(chunk))
        elif isinstance(chunk, (tuple, list)) and len(chunk) >= 2:
            start, end = float(chunk[0]), float(chunk[1])
        elif isinstance(chunk, dict):
            start = float(chunk["start_seconds"])
            end = float(chunk["end_seconds"])
        else:
            start = float(chunk.start_seconds)
            end = float(chunk.end_seconds)
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
            raise ValueError(f"invalid chunk time range: {(start, end)!r}")
        ranges.append((start, end))
    return ranges


def _citation_is_member(
    total_seconds: int,
    ranges: Sequence[tuple[float, float]],
    near_seconds: float,
) -> bool:
    if not ranges:
        return False
    meeting_start = min(start for start, _ in ranges)
    meeting_end = max(end for _, end in ranges)
    if not meeting_start <= total_seconds <= meeting_end:
        return False
    return any(
        start - near_seconds <= total_seconds <= end + near_seconds
        for start, end in ranges
    )


def validate_inline_citations(
    text: str,
    chunks: Iterable[Any],
    *,
    near_seconds: float = DEFAULT_NEAR_CHUNK_SECONDS,
    membership_observation_reasons: Mapping[int, str] | None = None,
) -> ValidationReport:
    """Validate per-decision coverage and timed-chunk membership.

    ``membership_observation_reasons`` identifies decisions whose citations
    have an independent transcript anchor.  Their chunk misses remain visible
    in ``nonmember_observations`` but are not validation failures.  Callers
    must establish that evidence before granting the exception; the default
    remains fail-closed membership validation.
    """
    decisions = split_numbered_items(text)
    ranges = chunk_time_ranges(chunks)
    # Numeric-only input is the restored validator's chunk-start API.  Keep
    # exact membership for that legacy call shape; ranged input gets the new
    # within/near validation required by word-precise offsets.
    ranged_input = any(end > start for start, end in ranges)
    membership_margin = near_seconds if ranged_input else 0.0
    covered_indices: list[int] = []
    uncovered_indices: list[int] = []
    unknown_citations: list[str] = []
    nonmember_observations: list[dict[str, Any]] = []
    per_decision: list[dict[str, Any]] = []
    citations_total = 0
    member_citations = 0

    for index, decision in enumerate(decisions, start=1):
        citations = parse_citations(decision)
        membership = [
            _citation_is_member(citation.total_seconds, ranges, membership_margin)
            for citation in citations
        ]
        if citations:
            covered_indices.append(index)
        else:
            uncovered_indices.append(index)

        citations_total += len(citations)
        member_citations += sum(membership)
        observation_reason = (
            membership_observation_reasons.get(index)
            if membership_observation_reasons is not None
            else None
        )
        decision_observations: list[dict[str, Any]] = []
        for citation, is_member in zip(citations, membership):
            if is_member:
                continue
            if observation_reason:
                observation = {
                    "index": index,
                    "citation": citation.raw,
                    "total_seconds": citation.total_seconds,
                    "reason": observation_reason,
                }
                nonmember_observations.append(observation)
                decision_observations.append(observation)
            else:
                unknown_citations.append(citation.raw)
        per_decision.append(
            {
                "index": index,
                "citations": [citation.raw for citation in citations],
                "member": membership,
                "nonmember_observations": decision_observations,
            }
        )

    decisions_total = len(decisions)
    if decisions_total == 0:
        state = "no_decisions_extracted"
    elif not uncovered_indices and not unknown_citations:
        state = "valid"
    else:
        state = "errored"

    return ValidationReport(
        state=state,
        decisions_total=decisions_total,
        covered_indices=covered_indices,
        uncovered_indices=uncovered_indices,
        citations_total=citations_total,
        member_citations=member_citations,
        unknown_citations=unknown_citations,
        nonmember_observations=nonmember_observations,
        per_decision=per_decision,
    )


def _timed_tokens(transcript_words: Sequence[dict[str, Any]]) -> list[_TimedToken]:
    tokens: list[_TimedToken] = []
    for word in transcript_words:
        if not isinstance(word, dict):
            continue
        quarantine = word.get("quarantine")
        if word.get("quarantined") is True or (
            isinstance(quarantine, dict) and quarantine.get("reason")
        ):
            continue
        raw = word.get("word", word.get("text"))
        start = word.get("start")
        if not isinstance(raw, str) or not isinstance(start, (int, float)):
            continue
        if isinstance(start, bool) or not math.isfinite(float(start)) or float(start) < 0:
            continue
        for token in _TOKEN_RE.findall(raw.lower()):
            tokens.append(_TimedToken(token=token, start_seconds=float(start)))
    if not tokens:
        raise ValueError("transcript_words contains no usable timed words")
    return tokens


def _decision_keywords(decision: str) -> set[str]:
    without_markup = re.sub(r"</?(?:core|nuance)>", " ", decision)
    without_markup = re.sub(r"\*\*", "", without_markup)
    without_markup = CITATION_RE.sub(" ", without_markup)
    return {
        token
        for token in _TOKEN_RE.findall(without_markup.lower())
        if token not in _STOPWORDS and (len(token) >= 4 or token.isdigit())
    }


def _keyword_overlap_score(keywords: set[str], context: set[str]) -> float:
    score = 0.0
    for token in keywords & context:
        if token.isdigit():
            score += 5.0
        elif len(token) >= 8:
            score += 3.0
        elif len(token) >= 6:
            score += 2.0
        else:
            score += 1.0
    return score


def _find_alignment_candidate(
    decision: str,
    coarse_seconds: int,
    tokens: Sequence[_TimedToken],
    ranges: Sequence[tuple[float, float]],
    *,
    require_chunk_membership: bool = True,
) -> _AlignmentCandidate | None:
    normalized = [token.token for token in tokens]
    keywords = _decision_keywords(decision)
    appointment_decision = bool(
        {"appoint", "appointed", "appointment", "commission", "vacancy"}
        & set(_TOKEN_RE.findall(decision.lower()))
    )
    candidates: list[_AlignmentCandidate] = []

    for pattern, base_score, appointment_only in _OUTCOME_SIGNATURES:
        if appointment_only and not appointment_decision:
            continue
        width = len(pattern)
        for index in range(0, len(tokens) - width + 1):
            if tuple(normalized[index:index + width]) != pattern:
                continue
            candidate_seconds = tokens[index].start_seconds
            distance = abs(candidate_seconds - coarse_seconds)
            if distance > MAX_ALIGNMENT_DISTANCE_SECONDS:
                continue
            if require_chunk_membership and not _citation_is_member(
                int(candidate_seconds),
                ranges,
                DEFAULT_NEAR_CHUNK_SECONDS,
            ):
                continue
            left = max(0, index - _CONTEXT_WORDS)
            right = min(len(tokens), index + width + _CONTEXT_WORDS)
            context = set(normalized[left:right])
            keyword_score = _keyword_overlap_score(keywords, context)
            if keyword_score < 2.0 and not (appointment_only and appointment_decision):
                # A coarse locator already inside the same local action
                # neighborhood is independent evidence for a strong motion-
                # introduction signature.  This covers long discussions where
                # the item name was spoken many minutes before the final motion.
                if distance > DEFAULT_NEAR_CHUNK_SECONDS or base_score < 70.0:
                    continue
            score = base_score + keyword_score * 4.0 - distance / 90.0
            candidates.append(
                _AlignmentCandidate(
                    start_seconds=candidate_seconds,
                    signature=" ".join(pattern),
                    score=score,
                    keyword_score=keyword_score,
                )
            )

    if not candidates:
        return None
    candidates.sort(key=lambda candidate: (-candidate.score, candidate.start_seconds))
    best = candidates[0]
    # Do not guess between two materially different outcome moments whose
    # evidence scores are effectively tied.
    if len(candidates) > 1:
        runner_up = candidates[1]
        if (
            abs(best.start_seconds - runner_up.start_seconds) > 20.0
            and best.score - runner_up.score < 2.0
        ):
            return None
    return best


def _evidence_for_audit(evidence: QuoteAlignmentEvidence) -> dict[str, Any]:
    """Serialize confidence evidence without duplicating karaoke timings."""
    payload = asdict(evidence)
    payload.pop("timings", None)
    return payload


def _chunk_value(chunk: Any, name: str, default: Any = None) -> Any:
    if isinstance(chunk, Mapping):
        return chunk.get(name, default)
    return getattr(chunk, name, default)


def _normalize_anchor_whitespace(value: str) -> str:
    """Collapse whitespace while preserving exact casing and punctuation."""
    return " ".join(value.split())


def _model_visible_chunk_surfaces(chunk: Any) -> tuple[str, ...]:
    """Return only transcript text that Sonnet saw for one retrieved chunk."""
    speaker_turns = _chunk_value(chunk, "speaker_turns")
    if speaker_turns:
        if isinstance(speaker_turns, (str, bytes)) or not isinstance(
            speaker_turns,
            Sequence,
        ):
            return ()
        surfaces: list[str] = []
        for turn in speaker_turns:
            if not isinstance(turn, Mapping):
                continue
            turn_text = turn.get("text")
            if isinstance(turn_text, str):
                surfaces.append(turn_text)
        # The prompt formatter replaces ``body`` whenever speaker turns are
        # truthy.  Never accept a quote from the hidden body in this branch.
        return tuple(surfaces)

    body = _chunk_value(chunk, "body")
    if isinstance(body, str) and body:
        return (body,)
    stored_text = _chunk_value(chunk, "text")
    if isinstance(stored_text, str):
        return (stored_text,)
    return (body,) if isinstance(body, str) else ()


def _chunk_window(chunk: Any) -> tuple[float, float] | None:
    raw_start = _chunk_value(chunk, "start_seconds")
    raw_end = _chunk_value(chunk, "end_seconds")
    if (
        isinstance(raw_start, bool)
        or not isinstance(raw_start, (int, float))
        or isinstance(raw_end, bool)
        or not isinstance(raw_end, (int, float))
    ):
        return None
    start_seconds = float(raw_start)
    end_seconds = float(raw_end)
    if (
        not math.isfinite(start_seconds)
        or not math.isfinite(end_seconds)
        or start_seconds < 0
        or end_seconds < start_seconds
    ):
        return None
    return start_seconds, end_seconds


def _slice_anchor_transcript_words(
    transcript_words: Sequence[Mapping[str, Any]],
    *,
    start_seconds: float,
    end_seconds: float,
) -> list[dict[str, Any]]:
    """Return timed words overlapping a small padded chunk window."""
    lower = max(0.0, start_seconds - _VERBATIM_ANCHOR_PADDING_SECONDS)
    upper = end_seconds + _VERBATIM_ANCHOR_PADDING_SECONDS
    sliced: list[dict[str, Any]] = []
    for row in transcript_words:
        if not isinstance(row, Mapping):
            continue
        raw_start = row.get("start")
        if (
            isinstance(raw_start, bool)
            or not isinstance(raw_start, (int, float))
            or not math.isfinite(float(raw_start))
            or float(raw_start) < 0
        ):
            continue
        word_start = float(raw_start)
        raw_end = row.get("end")
        word_end = (
            float(raw_end)
            if isinstance(raw_end, (int, float))
            and not isinstance(raw_end, bool)
            and math.isfinite(float(raw_end))
            and float(raw_end) >= word_start
            else word_start
        )
        if word_end >= lower and word_start <= upper:
            sliced.append(dict(row))
    return sliced


def _has_usable_transcript_words(
    transcript_words: Sequence[Mapping[str, Any]],
) -> bool:
    for row in transcript_words:
        if not isinstance(row, Mapping):
            continue
        raw_word = row.get("word", row.get("text"))
        raw_start = row.get("start")
        if (
            isinstance(raw_word, str)
            and raw_word.strip()
            and isinstance(raw_start, (int, float))
            and not isinstance(raw_start, bool)
            and math.isfinite(float(raw_start))
            and float(raw_start) >= 0
        ):
            return True
    return False


def _malformed_anchor_span(text: str, start: int) -> tuple[int, int]:
    closing_bracket = text.find("]", start)
    newline = text.find("\n", start)
    if closing_bracket >= 0 and (newline < 0 or closing_bracket < newline):
        return start, closing_bracket + 1
    if newline >= 0:
        return start, newline
    return start, len(text)


def _anchor_failure_record(
    *,
    ordinal: int,
    source_span: tuple[int, int],
    raw_anchor: str,
    quote: str,
    reason: str,
    **evidence: Any,
) -> dict[str, Any]:
    return {
        "ordinal": ordinal,
        "source_span": source_span,
        "raw_anchor": raw_anchor,
        "quote": quote,
        "reason": reason,
        **evidence,
    }


def _rewrite_verbatim_anchors(
    text: str,
    replacements: Sequence[tuple[int, int, str]],
) -> str:
    parts: list[str] = []
    cursor = 0
    for start, end, citation in replacements:
        parts.append(text[cursor:start])
        parts.append(citation)
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def resolve_inline_verbatim_anchors(
    text: str,
    chunks: Sequence[Any],
    transcript_words: Sequence[Mapping[str, Any]],
    *,
    min_words: int = 3,
    max_words: int = 20,
    atomic: bool = True,
) -> VerbatimAnchorResolution:
    """Resolve model-emitted verbatim anchors to word-timed citations.

    Exact-copy validation uses the same surface shown to synthesis: diarized
    speaker-turn text replaces the otherwise-visible chunk body.  Each quote
    is then aligned only inside matching retrieved chunk windows.  Expected
    model or evidence variance is represented in the result state; invalid
    programmer arguments still raise.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if isinstance(chunks, (str, bytes)) or not isinstance(chunks, Sequence):
        raise TypeError("chunks must be a sequence")
    if isinstance(transcript_words, (str, bytes)) or not isinstance(
        transcript_words,
        Sequence,
    ):
        raise TypeError("transcript_words must be a sequence")
    if (
        isinstance(min_words, bool)
        or not isinstance(min_words, int)
        or min_words < 1
    ):
        raise ValueError("min_words must be a positive integer")
    if (
        isinstance(max_words, bool)
        or not isinstance(max_words, int)
        or max_words < min_words
    ):
        raise ValueError("max_words must be an integer >= min_words")
    if not isinstance(atomic, bool):
        raise TypeError("atomic must be a boolean")

    quote_matches = list(_VERBATIM_ANCHOR_RE.finditer(text))
    quotes_by_start = {match.start(): match for match in quote_matches}
    direct_citations = parse_citations(text)
    direct_by_start = {citation.start: citation for citation in direct_citations}
    marker_matches = list(_ANCHOR_MARKER_RE.finditer(text))

    if not marker_matches:
        logger.warning("verbatim anchor resolution nonconforming: zero anchors")
        return VerbatimAnchorResolution(
            text=text,
            state="nonconforming",
            anchors_total=0,
            aligned=(),
            failures=(),
        )

    transcript_usable = _has_usable_transcript_words(transcript_words)
    aligned: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    replacements: list[tuple[int, int, str]] = []
    saw_nonconforming = False
    saw_uncheckable = False
    saw_degraded = False

    for ordinal, marker in enumerate(marker_matches, start=1):
        marker_start = marker.start()
        direct = direct_by_start.get(marker_start)
        if direct is not None:
            saw_nonconforming = True
            failures.append(
                _anchor_failure_record(
                    ordinal=ordinal,
                    source_span=(direct.start, direct.end),
                    raw_anchor=direct.raw,
                    quote="",
                    reason="direct_timestamp_bypass",
                    timestamp_seconds=direct.total_seconds,
                )
            )
            continue

        match = quotes_by_start.get(marker_start)
        if match is None:
            saw_nonconforming = True
            malformed_span = _malformed_anchor_span(text, marker_start)
            failures.append(
                _anchor_failure_record(
                    ordinal=ordinal,
                    source_span=malformed_span,
                    raw_anchor=text[malformed_span[0]:malformed_span[1]],
                    quote="",
                    reason="malformed_verbatim_anchor",
                )
            )
            continue

        quote = match.group("quote")
        normalized_quote = _normalize_anchor_whitespace(quote)
        quote_word_count = len(normalized_quote.split())
        base = {
            "ordinal": ordinal,
            "source_span": (match.start(), match.end()),
            "raw_anchor": match.group(0),
            "quote": quote,
        }
        if not min_words <= quote_word_count <= max_words:
            saw_degraded = True
            failures.append(
                _anchor_failure_record(
                    **base,
                    reason="quote_word_count_out_of_bounds",
                    word_count=quote_word_count,
                    min_words=min_words,
                    max_words=max_words,
                )
            )
            continue

        visible_surface_seen = False
        matching_chunks: list[tuple[Any, dict[str, Any], float, float]] = []
        invalid_matching_chunks: list[dict[str, Any]] = []
        for retrieval_ordinal, chunk in enumerate(chunks, start=1):
            surfaces = _model_visible_chunk_surfaces(chunk)
            if surfaces:
                visible_surface_seen = True
            if not any(
                normalized_quote in _normalize_anchor_whitespace(surface)
                for surface in surfaces
            ):
                continue

            raw_chunk_index = _chunk_value(chunk, "chunk_index")
            chunk_index = (
                raw_chunk_index
                if isinstance(raw_chunk_index, int)
                and not isinstance(raw_chunk_index, bool)
                else None
            )
            window = _chunk_window(chunk)
            if window is None:
                invalid_matching_chunks.append(
                    {
                        "retrieval_ordinal": retrieval_ordinal,
                        "chunk_index": chunk_index,
                        "raw_start_seconds": repr(
                            _chunk_value(chunk, "start_seconds")
                        ),
                        "raw_end_seconds": repr(
                            _chunk_value(chunk, "end_seconds")
                        ),
                        "reason": "invalid_chunk_time_range",
                    }
                )
                continue
            start_seconds, end_seconds = window
            descriptor = {
                "retrieval_ordinal": retrieval_ordinal,
                "chunk_index": chunk_index,
                "start_seconds": start_seconds,
                "end_seconds": end_seconds,
            }
            matching_chunks.append((chunk, descriptor, start_seconds, end_seconds))

        if not matching_chunks:
            if invalid_matching_chunks or not visible_surface_seen:
                saw_uncheckable = True
                reason = "retrieved_chunk_surface_unusable"
                if invalid_matching_chunks:
                    reason = "matching_chunk_window_unusable"
            else:
                saw_degraded = True
                reason = "quote_not_in_retrieved_chunks"
            failures.append(
                _anchor_failure_record(
                    **base,
                    reason=reason,
                    invalid_matching_chunks=tuple(invalid_matching_chunks),
                )
            )
            continue

        matching_chunk_indices = tuple(
            descriptor["chunk_index"]
            for _, descriptor, _, _ in matching_chunks
            if descriptor["chunk_index"] is not None
        )
        matching_chunk_ranges = tuple(
            {
                "chunk_index": descriptor["chunk_index"],
                "start_seconds": start_seconds,
                "end_seconds": end_seconds,
            }
            for _, descriptor, start_seconds, end_seconds in matching_chunks
        )
        if not transcript_usable:
            saw_uncheckable = True
            failures.append(
                _anchor_failure_record(
                    **base,
                    reason="transcript_words_unusable",
                    matching_chunk_indices=matching_chunk_indices,
                    matching_chunk_ranges=matching_chunk_ranges,
                )
            )
            continue

        chunk_evidence: list[dict[str, Any]] = []
        successful: list[tuple[str, QuoteAlignmentEvidence, dict[str, Any]]] = []
        ambiguous = False
        alignment_uncheckable = False
        for _, descriptor, start_seconds, end_seconds in matching_chunks:
            words_slice = _slice_anchor_transcript_words(
                transcript_words,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
            )
            try:
                evidence = align_quote_with_evidence(
                    quote,
                    words_slice,
                    window_start_seconds=start_seconds,
                    window_end_seconds=end_seconds,
                )
            except (TypeError, ValueError) as exc:
                alignment_uncheckable = True
                chunk_evidence.append(
                    {
                        **descriptor,
                        "reason": "alignment_input_unusable",
                        "detail": str(exc),
                    }
                )
                continue

            serialized_evidence = _evidence_for_audit(evidence)
            evidence_record = {
                **descriptor,
                "alignment_evidence": serialized_evidence,
            }
            chunk_evidence.append(evidence_record)
            if evidence.reason in {"non_unique_in_window", "selection_tie"}:
                ambiguous = True
            if evidence.reason == "empty_transcript":
                alignment_uncheckable = True
            if evidence.success and evidence.start_seconds is not None:
                citation = format_citation(evidence.start_seconds)
                evidence_record["canonical_citation"] = citation
                evidence_record["aligned_start_seconds"] = evidence.start_seconds
                successful.append((citation, evidence, evidence_record))

        if ambiguous:
            saw_degraded = True
            failures.append(
                _anchor_failure_record(
                    **base,
                    reason="quote_alignment_ambiguous",
                    matching_chunk_indices=matching_chunk_indices,
                    matching_chunk_ranges=matching_chunk_ranges,
                    chunk_evidence=tuple(chunk_evidence),
                )
            )
            continue

        citations = {citation for citation, _, _ in successful}
        if len(citations) > 1:
            saw_degraded = True
            failures.append(
                _anchor_failure_record(
                    **base,
                    reason="quote_aligned_to_distinct_moments",
                    canonical_citations=tuple(sorted(citations)),
                    matching_chunk_indices=matching_chunk_indices,
                    matching_chunk_ranges=matching_chunk_ranges,
                    chunk_evidence=tuple(chunk_evidence),
                )
            )
            continue

        if not successful:
            if alignment_uncheckable:
                saw_uncheckable = True
                reason = "quote_alignment_uncheckable"
            else:
                saw_degraded = True
                reason = "quote_alignment_failed"
            failures.append(
                _anchor_failure_record(
                    **base,
                    reason=reason,
                    alignment_reasons=tuple(
                        record.get("alignment_evidence", {}).get(
                            "reason",
                            record.get("reason", "unknown"),
                        )
                        for record in chunk_evidence
                    ),
                    matching_chunk_indices=matching_chunk_indices,
                    matching_chunk_ranges=matching_chunk_ranges,
                    chunk_evidence=tuple(chunk_evidence),
                )
            )
            continue

        citation = next(iter(citations))
        _, selected_evidence, _ = min(
            successful,
            key=lambda item: (
                item[1].start_seconds
                if item[1].start_seconds is not None
                else math.inf,
                item[2]["retrieval_ordinal"],
            ),
        )
        assert selected_evidence.start_seconds is not None
        aligned_record = {
            **base,
            "canonical_citation": citation,
            "start_seconds": selected_evidence.start_seconds,
            "matching_chunk_indices": matching_chunk_indices,
            "matching_chunk_ranges": matching_chunk_ranges,
            "alignment_evidence": _evidence_for_audit(selected_evidence),
            "chunk_evidence": tuple(chunk_evidence),
        }
        aligned.append(aligned_record)
        replacements.append((match.start(), match.end(), citation))

    if saw_nonconforming:
        state = "nonconforming"
    elif saw_uncheckable:
        state = "uncheckable"
    elif saw_degraded or failures:
        state = "degraded"
    else:
        state = "resolved"

    resolved_text = text
    if replacements and (state == "resolved" or not atomic):
        resolved_text = _rewrite_verbatim_anchors(text, replacements)

    for failure in failures:
        logger.warning(
            "verbatim anchor unresolved ordinal=%d reason=%s evidence=%s",
            failure["ordinal"],
            failure["reason"],
            failure,
        )
    for record in aligned:
        logger.info(
            "verbatim anchor aligned ordinal=%d citation=%s evidence=%s",
            record["ordinal"],
            record["canonical_citation"],
            record,
        )

    return VerbatimAnchorResolution(
        text=resolved_text,
        state=state,
        anchors_total=len(marker_matches),
        aligned=tuple(aligned),
        failures=tuple(failures),
    )


def _json_ready(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_ready(item) for item in value]
    return value


def serialize_verbatim_anchor_resolution(
    resolution: VerbatimAnchorResolution,
) -> dict[str, Any]:
    """Return the version-independent JSON payload for synopsis auditing."""
    if not isinstance(resolution, VerbatimAnchorResolution):
        raise TypeError("resolution must be a VerbatimAnchorResolution")
    return {
        "resolution_state": resolution.state,
        "anchors_total": resolution.anchors_total,
        "aligned": _json_ready(resolution.aligned),
        "failures": _json_ready(resolution.failures),
    }


def align_decision_citations(
    text: str,
    transcript_words: Sequence[dict[str, Any]],
    chunks: Iterable[Any],
    *,
    anchors: Sequence[dict[str, Any]] | None = None,
) -> AlignmentReport:
    """Align decision citations through item-introduction and action quotes.

    New synthesis supplies ``item_quote`` + ``action_quote`` in each audit
    entry.  The action quote resolves nearest the coarse model locator inside
    a symmetric bounded window, unless one globally unique near-verbatim
    occurrence clears the stricter outside-window floor.  The item
    introduction then resolves to the latest qualifying occurrence in the
    full prefix ending at that action.  Decision numbering is never treated as
    transcript chronology.

    Legacy outputs with no anchor metadata may use the signature scanner as a
    visibly lower-confidence fallback.  Once an anchor record is supplied for
    a decision, any missing or unalignable quote fails that decision closed and
    the fallback is never consulted.
    """
    body = strip_audit_block(text)
    item_matches = list(_NUMBERED_ITEM_RE.finditer(body))
    tokens = _timed_tokens(transcript_words)
    ranges = chunk_time_ranges(chunks)
    transcript_ends: list[float] = []
    for word in transcript_words:
        if not isinstance(word, dict):
            continue
        start = word.get("start")
        if (
            not isinstance(start, (int, float))
            or isinstance(start, bool)
            or not math.isfinite(float(start))
        ):
            continue
        end = word.get("end")
        transcript_ends.append(
            float(end)
            if isinstance(end, (int, float))
            and not isinstance(end, bool)
            and math.isfinite(float(end))
            else float(start)
        )
    transcript_end = max(transcript_ends)
    aligned_indices: list[int] = []
    failures: list[dict[str, Any]] = []
    per_decision: list[dict[str, Any]] = []
    index_map: list[dict[str, int]] = []

    anchor_by_index: dict[int, dict[str, Any]] = {}
    if anchors is not None:
        for entry in anchors:
            if not isinstance(entry, dict):
                continue
            entry_index = entry.get("index")
            if isinstance(entry_index, int) and not isinstance(entry_index, bool):
                anchor_by_index.setdefault(entry_index, entry)

    decisions: list[dict[str, Any]] = []
    for list_index, item_match in enumerate(item_matches, start=1):
        item_end = (
            item_matches[list_index].start()
            if list_index < len(item_matches)
            else len(body)
        )
        decision = body[item_match.end():item_end].strip()
        citations = parse_citations(decision)
        row: dict[str, Any] = {
            "source_index": list_index,
            "decision": decision,
            "citations": citations,
            "anchor_entry": anchor_by_index.get(list_index),
        }
        decisions.append(row)

        if len(citations) != 1:
            reason = "missing_citation" if not citations else "multiple_citations"
            row["failure"] = {
                "index": list_index,
                "reason": reason,
                "citation_count": len(citations),
            }
            continue

        if anchors is None:
            continue

        entry = row["anchor_entry"]
        if entry is None:
            row["failure"] = {
                "index": list_index,
                "reason": "anchor_metadata_missing",
            }
            continue

        item_quote = entry.get("item_quote")
        action_quote = entry.get("action_quote")
        if not isinstance(item_quote, str) or not item_quote.strip():
            row["failure"] = {
                "index": list_index,
                "reason": "item_quote_missing",
            }
            continue
        if not isinstance(action_quote, str) or not action_quote.strip():
            row["failure"] = {
                "index": list_index,
                "reason": "action_quote_missing",
            }
            continue

        coarse_seconds = citations[0].total_seconds
        action_evidence = align_quote_with_evidence(
            action_quote,
            list(transcript_words),
            window_start_seconds=max(
                0.0,
                coarse_seconds - ACTION_ANCHOR_WINDOW_SECONDS,
            ),
            window_end_seconds=min(
                transcript_end,
                coarse_seconds + ACTION_ANCHOR_WINDOW_SECONDS,
            ),
            min_coverage=ANCHOR_MIN_COVERAGE,
            selection="nearest",
            reference_seconds=coarse_seconds,
            unique_outside_window_min_coverage=UNIQUE_ACTION_MIN_COVERAGE,
        )
        row["item_quote"] = item_quote
        row["action_quote"] = action_quote
        row["action_evidence"] = action_evidence
        if action_evidence.reason == "aligned_unique_outside_window":
            assert action_evidence.start_seconds is not None
            assert action_evidence.best_candidate_distance_seconds is not None
            disagreement = {
                "reason": "globally_unique_action_quote_outside_locator_window",
                "coarse_locator_seconds": float(coarse_seconds),
                "quote_start_seconds": action_evidence.start_seconds,
                "distance_seconds": action_evidence.best_candidate_distance_seconds,
                "window_seconds": ACTION_ANCHOR_WINDOW_SECONDS,
                "coverage_floor": UNIQUE_ACTION_MIN_COVERAGE,
                "coverage": action_evidence.coverage,
                "direct_matches": action_evidence.direct_matches,
                "quote_tokens": action_evidence.quote_tokens,
            }
            row["locator_disagreement"] = disagreement
            logger.warning(
                "key_decision action quote outranked coarse locator "
                "source_index=%d evidence=%s",
                list_index,
                disagreement,
            )
        if not action_evidence.success:
            row["failure"] = {
                "index": list_index,
                "reason": f"action_quote_{action_evidence.reason}",
                "action_quote": action_quote,
                "action_evidence": _evidence_for_audit(action_evidence),
            }

    # The resolved action is authoritative for chronology.  Search the whole
    # prefix so a long discussion cannot push the spoken item introduction out
    # of an arbitrary local window.  The latest best-band occurrence avoids an
    # early agenda read-through when the item is introduced again for action.
    for row in decisions:
        if "failure" in row or anchors is None:
            continue
        action_evidence: QuoteAlignmentEvidence = row["action_evidence"]
        assert action_evidence.start_seconds is not None
        item_evidence = align_quote_with_evidence(
            row["item_quote"],
            list(transcript_words),
            window_start_seconds=0.0,
            window_end_seconds=action_evidence.start_seconds,
            min_coverage=ANCHOR_MIN_COVERAGE,
            selection="latest",
            reference_seconds=action_evidence.start_seconds,
        )
        row["item_evidence"] = item_evidence
        if not item_evidence.success:
            row["failure"] = {
                "index": row["source_index"],
                "reason": f"item_quote_{item_evidence.reason}",
                "item_quote": row["item_quote"],
                "item_evidence": _evidence_for_audit(item_evidence),
                "action_evidence": _evidence_for_audit(action_evidence),
            }
            continue

        row["aligned_seconds"] = action_evidence.start_seconds
        row["source"] = "two_part_quote"
        row["confidence"] = "high"

    # A single spoken action cannot substantiate two distinct decisions.  Fail
    # every member of a duplicate group closed rather than assigning ownership
    # from list order, which is not a temporal signal.
    action_rows_by_word: dict[int, list[dict[str, Any]]] = {}
    if anchors is not None:
        for row in decisions:
            if "failure" in row:
                continue
            action_evidence = row.get("action_evidence")
            if (
                isinstance(action_evidence, QuoteAlignmentEvidence)
                and action_evidence.success
                and action_evidence.matched_word_index is not None
            ):
                action_rows_by_word.setdefault(
                    action_evidence.matched_word_index,
                    [],
                ).append(row)
    for matched_word_index, duplicate_rows in action_rows_by_word.items():
        if len(duplicate_rows) < 2:
            continue
        conflicting_indices = [row["source_index"] for row in duplicate_rows]
        for row in duplicate_rows:
            action_evidence = row["action_evidence"]
            row["failure"] = {
                "index": row["source_index"],
                "reason": "duplicate_action_occurrence",
                "action_word_index": matched_word_index,
                "action_start_seconds": action_evidence.start_seconds,
                "conflicting_indices": conflicting_indices,
                "action_evidence": _evidence_for_audit(action_evidence),
            }

    # Preserve the former next-item boundary only as an audit signal.  Actual
    # item times, not numbered output order, define the comparison, and a
    # conflict never removes an otherwise supported decision.
    if anchors is not None:
        anchored_rows = [
            row
            for row in decisions
            if "failure" not in row
            and isinstance(row.get("item_evidence"), QuoteAlignmentEvidence)
            and row["item_evidence"].success
        ]
        anchored_rows.sort(
            key=lambda row: (
                row["item_evidence"].start_seconds,
                row["source_index"],
            )
        )
        for row, next_row in zip(anchored_rows, anchored_rows[1:]):
            action_start = row["action_evidence"].start_seconds
            next_item_start = next_row["item_evidence"].start_seconds
            assert action_start is not None and next_item_start is not None
            if action_start < next_item_start:
                continue
            conflict = {
                "reason": "action_at_or_after_next_item_anchor",
                "item_start_seconds": row["item_evidence"].start_seconds,
                "action_start_seconds": action_start,
                "next_source_index": next_row["source_index"],
                "next_item_start_seconds": next_item_start,
            }
            row.setdefault("audit_conflicts", []).append(conflict)
            logger.warning(
                "key_decision citation temporal conflict source_index=%d "
                "evidence=%s",
                row["source_index"],
                conflict,
            )

    # Legacy-only fallback.  Crucially, an existing but bad anchor entry never
    # reaches this branch.
    if anchors is None:
        for row in decisions:
            if "failure" in row:
                continue
            citation: Citation = row["citations"][0]
            candidate = _find_alignment_candidate(
                row["decision"],
                citation.total_seconds,
                tokens,
                ranges,
            )
            if candidate is None:
                nonmember_candidate = _find_alignment_candidate(
                    row["decision"],
                    citation.total_seconds,
                    tokens,
                    ranges,
                    require_chunk_membership=False,
                )
                if (
                    nonmember_candidate is not None
                    and not _citation_is_member(
                        int(nonmember_candidate.start_seconds),
                        ranges,
                        DEFAULT_NEAR_CHUNK_SECONDS,
                    )
                ):
                    row["failure"] = {
                        "index": row["source_index"],
                        "reason": "fallback_citation_outside_retrieved_chunks",
                        "source": "outcome_signature_fallback",
                        "aligned_seconds": nonmember_candidate.start_seconds,
                        "signature": nonmember_candidate.signature,
                        "fallback_attempted": True,
                    }
                    continue
                row["failure"] = {
                    "index": row["source_index"],
                    "reason": "no_unambiguous_outcome_match",
                    "fallback_attempted": True,
                }
                continue
            row["aligned_seconds"] = candidate.start_seconds
            row["source"] = "outcome_signature_fallback"
            row["confidence"] = "lower"
            row["fallback"] = {
                "signature": candidate.signature,
                "keyword_score": candidate.keyword_score,
                "lower_confidence": True,
            }

    surviving_bodies: list[str] = []
    for row in decisions:
        source_index = row["source_index"]
        failure = row.get("failure")
        if failure is not None:
            failures.append(failure)
            per_decision.append(failure)
            logger.warning(
                "key_decision citation dropped source_index=%d reason=%s evidence=%s",
                source_index,
                failure["reason"],
                failure,
            )
            continue

        citation: Citation = row["citations"][0]
        canonical = format_citation(row["aligned_seconds"])
        decision = row["decision"]
        rewritten = (
            decision[:citation.start]
            + canonical
            + decision[citation.end:]
        )
        output_index = len(surviving_bodies) + 1
        surviving_bodies.append(f"{output_index}. {rewritten}")
        aligned_indices.append(source_index)
        index_map.append(
            {"source_index": source_index, "output_index": output_index}
        )

        result: dict[str, Any] = {
            "index": source_index,
            "source_index": source_index,
            "output_index": output_index,
            "raw": citation.raw,
            "aligned": canonical,
            "start_seconds": row["aligned_seconds"],
            "source": row["source"],
            "confidence": row["confidence"],
            "lower_confidence": row["confidence"] == "lower",
        }
        if row["source"] == "two_part_quote":
            result.update(
                {
                    "item_quote": row["item_quote"],
                    "action_quote": row["action_quote"],
                    "item_evidence": _evidence_for_audit(row["item_evidence"]),
                    "action_evidence": _evidence_for_audit(row["action_evidence"]),
                }
            )
            if row.get("locator_disagreement"):
                result["locator_disagreement"] = row["locator_disagreement"]
            if row.get("audit_conflicts"):
                result["audit_conflicts"] = row["audit_conflicts"]
        else:
            result["fallback"] = row["fallback"]
        per_decision.append(result)
        logger.info(
            "key_decision citation emitted source_index=%d output_index=%d "
            "citation=%s source=%s confidence=%s evidence=%s",
            source_index,
            output_index,
            canonical,
            row["source"],
            row["confidence"],
            result,
        )

    aligned_text = "\n\n".join(surviving_bodies)

    return AlignmentReport(
        text=aligned_text,
        decisions_total=len(item_matches),
        aligned_indices=aligned_indices,
        failures=failures,
        per_decision=per_decision,
        index_map=index_map,
    )
