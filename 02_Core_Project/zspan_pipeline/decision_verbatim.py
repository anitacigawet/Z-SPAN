"""Deterministic verbatim outcome spans for Key Decisions.

The display prose is already synthesized; this module only identifies short
vote/adoption wording which is both literally present in that prose and
grounded in a strong S-133 moment in one of the retrieved evidence chunks.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from .neutrality_audit.deterministic import (
    SIGNATURES,
    Transcript,
    cluster_vote_moments,
    scan_anchors,
)


_COMPACT_OUTCOMES: tuple[tuple[str, str], ...] = (
    (r"\bmotion\s+(?:carries|passes|passed|fails|failed|is\s+approved)\b", "carries"),
    (r"\b(?:carries|passes)\s+unanimously\b", "carries"),
    (r"\bmotion\s+is\s+(?:tabled|withdrawn|approved|denied)\b", "outcome_generic"),
    (r"\bby\s+a\s+vote\s+of\b", "outcome_generic"),
    (r"\bwithout\s+objection\b|\bso\s+ordered\b", "so_ordered"),
    (r"\bapproved\s+unanimously\b|\bunanimously\s+approved\b", "approved_unanimously"),
)
_RESULT_VERB_RE = re.compile(r"\b(?:approved|adopted|denied|tabled)\b", re.IGNORECASE)
_TALLY_SIGNATURES = tuple(
    (sig["id"], re.compile(sig["pattern"], re.IGNORECASE))
    for sig in SIGNATURES
    if sig["id"] in {"spoken_tally", "tally"}
)


@dataclass(frozen=True)
class _Candidate:
    text: str
    char_start: int
    char_end: int
    signature_id: str
    priority: int


def _literal_pattern(text: str) -> re.Pattern[str]:
    """Case-insensitive literal wording with whitespace-only normalization."""
    parts = re.split(r"\s+", text.strip())
    return re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, parts)) + r"(?!\w)", re.IGNORECASE)


def _candidates(display_text: str) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    for pattern, signature_id in _COMPACT_OUTCOMES:
        for match in re.finditer(pattern, display_text, re.IGNORECASE):
            candidates.append(_Candidate(
                match.group(0), match.start(), match.end(), signature_id, 3,
            ))
    for signature_id, pattern in _TALLY_SIGNATURES:
        for match in pattern.finditer(display_text):
            candidates.append(_Candidate(
                match.group(0), match.start(), match.end(), signature_id, 2,
            ))
    for match in _RESULT_VERB_RE.finditer(display_text):
        candidates.append(_Candidate(
            match.group(0), match.start(), match.end(), "", 1,
        ))

    # Prefer the longest/most distinctive candidate when, for example,
    # "approved unanimously" also produces the lone verb "approved".
    candidates.sort(key=lambda c: (-c.priority, c.char_start, -(c.char_end - c.char_start)))
    selected: list[_Candidate] = []
    for candidate in candidates:
        if any(
            candidate.char_start < existing.char_end
            and candidate.char_end > existing.char_start
            for existing in selected
        ):
            continue
        selected.append(candidate)
    return sorted(selected, key=lambda c: c.char_start)


def _chunk_value(chunk: Any, name: str, default: Any) -> Any:
    if isinstance(chunk, dict):
        return chunk.get(name, default)
    return getattr(chunk, name, default)


def _ground_in_chunk(candidate: _Candidate, body: str) -> str | None:
    """Return the S-133 signature ID when the literal is in a strong moment."""
    transcript = Transcript.from_words(body.split())
    anchors = scan_anchors(transcript)
    moments = cluster_vote_moments(anchors, transcript)
    if not moments:
        return None

    for literal_match in _literal_pattern(candidate.text).finditer(body):
        for signature in SIGNATURES:
            if candidate.signature_id and signature["id"] != candidate.signature_id:
                continue
            if not candidate.signature_id and signature["strength"] != "strong":
                continue
            for signature_match in re.finditer(signature["pattern"], body, re.IGNORECASE):
                if not (
                    signature_match.start() <= literal_match.start()
                    and signature_match.end() >= literal_match.end()
                ):
                    continue
                signature_wi = transcript.word_index(signature_match.start())
                if any(
                    moment.start_wi <= signature_wi <= moment.end_wi
                    for moment in moments
                ):
                    return signature["id"]
    return None


def extract_verbatim_spans(
    display_text: str,
    chunks: Iterable[Any],
) -> list[dict[str, str | int | float]]:
    """Return literal, strong-S-133-grounded spans in final display text.

    ``chunks`` may contain ``RetrievedChunk`` objects or dict-shaped test
    fixtures. Character offsets always address ``display_text`` exactly.
    """
    spans: list[dict[str, str | int | float]] = []
    evidence_chunks = list(chunks)
    if not display_text or not evidence_chunks:
        return spans

    for candidate in _candidates(display_text):
        for chunk in evidence_chunks:
            body = str(_chunk_value(chunk, "body", "") or "")
            signature_id = _ground_in_chunk(candidate, body)
            if signature_id is None:
                continue
            spans.append({
                "text": candidate.text,
                "char_start": candidate.char_start,
                "char_end": candidate.char_end,
                "start_seconds": float(_chunk_value(chunk, "start_seconds", 0.0)),
                "chunk_index": int(_chunk_value(chunk, "chunk_index", 0)),
                "signature_id": signature_id,
            })
            break
    return spans
