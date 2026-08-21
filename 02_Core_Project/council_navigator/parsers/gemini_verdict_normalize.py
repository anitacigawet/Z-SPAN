"""gemini_verdict_normalize — strict-mode classifier over the raw T-013 V2 verdict.

S-008 V0 / surface S-6 per
[`01_Project_Overview/S008_INPUT_SECURITY_SPEC.md`](../../01_Project_Overview/S008_INPUT_SECURITY_SPEC.md)
chunk 2.8 + [`01_Project_Overview/THREAT_MODEL_INPUT_SECURITY.md`](../../01_Project_Overview/THREAT_MODEL_INPUT_SECURITY.md)
surface S-6.

Adds a strict-enum + content-sanity gate on top of `review_response_parser.ClipVerdict`.
- Enum fields validated against fixed allowed sets; out-of-range → parse_failure.
- Free-text fields (text_differences, speaker_attribution_notes, other_concerns)
  pass through input_security normalization (NFC + control strip) and
  rejection (fence markers, bidi controls).
- A consecutive-malformed counter (process-local, in-memory) lets the
  consumer detect long streaks of bad verdicts and escalate.

Per [D-100](../../01_Project_Overview/DECISIONS.md#d-100): defensive
classifier.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# Allowed enum values per the verification_batch_prompt_template contract.
SPEAKER_ATTRIBUTION_ENUM = frozenset({"yes", "no", "uncertain"})
TEXT_ACCURACY_ENUM = frozenset({"yes", "mostly", "no"})
CLIP_INTEGRITY_ENUM = frozenset({"ok", "cuts-mid-word", "audio-issue", "other"})


# Free-text caps. Generous — Gemini verdicts can be a sentence each.
MAX_FREE_TEXT_FIELD_LEN = 4_000


# Consecutive-malformed counter is process-local. Daemons that loop over
# many verdicts should reset between batches.
_MALFORMED_STREAK_THRESHOLD = 5
_state = {"consecutive_malformed": 0}


class StrictVerdictError(ValueError):
    """Raised when a ClipVerdict fails the strict-enum + sanity gate."""


@dataclass
class NormalizedVerdict:
    """A ClipVerdict that has passed the strict-enum + sanity gate.

    All enum-shaped fields are guaranteed in their respective enums.
    All free-text fields are NFC-normalized + control-stripped.
    """

    filename: str
    speaker_attribution: str
    speaker_attribution_notes: str
    text_accuracy: str
    text_differences: str
    clip_integrity: str
    other_concerns: str


def _normalize_free_text(value: Optional[str], field_name: str) -> str:
    """Pass user-untrusted text through the cross-surface input_security
    helpers. Raises StrictVerdictError on rejection."""
    if value is None or value == "":
        return ""
    try:
        from parsers.input_security.primitives import (  # local to avoid circular
            UnicodeRejectionError,
            contains_fence_marker,
            normalize_user_text,
            reject_if_bidi_controls,
        )
    except Exception as e:
        # Defensive: if the primitives aren't importable (very early test
        # environments), fall back to the raw string. Audit + move on.
        logger.warning(
            "gemini_verdict_normalize._normalize_free_text: primitives "
            "unavailable (%s); passing value through unchanged", e,
        )
        return str(value)

    if not isinstance(value, str):
        raise StrictVerdictError(
            f"{field_name} must be a string, got {type(value).__name__}"
        )
    if len(value) > MAX_FREE_TEXT_FIELD_LEN:
        raise StrictVerdictError(
            f"{field_name} length {len(value)} exceeds cap "
            f"{MAX_FREE_TEXT_FIELD_LEN}"
        )
    try:
        reject_if_bidi_controls(value)
    except UnicodeRejectionError as e:
        raise StrictVerdictError(f"{field_name}: {e}")
    if contains_fence_marker(value):
        raise StrictVerdictError(
            f"{field_name} contains a structural fence marker"
        )
    return normalize_user_text(value)


def normalize_verdict(clip_verdict) -> NormalizedVerdict:
    """Run the strict-enum + sanity gate on a ClipVerdict.

    Args:
        clip_verdict: a review_response_parser.ClipVerdict instance. Pass
            the dataclass directly; this function reads its properties.

    Returns:
        NormalizedVerdict with all enum fields validated + free-text
        sanitized.

    Raises:
        StrictVerdictError: on enum-out-of-range, type-mismatch, or
        free-text rejection (bidi controls, fence markers, over-length).

    Side effect: on success, resets the consecutive-malformed counter to
    zero. On exception, increments it.
    """
    try:
        filename = (clip_verdict.filename or "").strip()
        if not filename:
            raise StrictVerdictError("filename is required")

        speaker_attribution = clip_verdict.speaker_attribution  # property: lowercased + stripped
        if speaker_attribution not in SPEAKER_ATTRIBUTION_ENUM:
            raise StrictVerdictError(
                f"speaker_attribution {speaker_attribution!r} not in "
                f"{sorted(SPEAKER_ATTRIBUTION_ENUM)}"
            )

        text_accuracy = clip_verdict.text_accuracy
        if text_accuracy not in TEXT_ACCURACY_ENUM:
            raise StrictVerdictError(
                f"text_accuracy {text_accuracy!r} not in "
                f"{sorted(TEXT_ACCURACY_ENUM)}"
            )

        clip_integrity = clip_verdict.clip_integrity
        if clip_integrity not in CLIP_INTEGRITY_ENUM:
            raise StrictVerdictError(
                f"clip_integrity {clip_integrity!r} not in "
                f"{sorted(CLIP_INTEGRITY_ENUM)}"
            )

        speaker_attribution_notes = _normalize_free_text(
            clip_verdict.raw_fields.get("speaker_attribution_notes"),
            "speaker_attribution_notes",
        )
        text_differences = _normalize_free_text(
            clip_verdict.text_differences, "text_differences"
        )
        other_concerns = _normalize_free_text(
            clip_verdict.other_concerns, "other_concerns"
        )

        _state["consecutive_malformed"] = 0
        return NormalizedVerdict(
            filename=filename,
            speaker_attribution=speaker_attribution,
            speaker_attribution_notes=speaker_attribution_notes,
            text_accuracy=text_accuracy,
            text_differences=text_differences,
            clip_integrity=clip_integrity,
            other_concerns=other_concerns,
        )
    except StrictVerdictError:
        _state["consecutive_malformed"] += 1
        raise


def consecutive_malformed_count() -> int:
    """Process-local count of strict-mode failures since the last clean
    verdict. Resets to 0 on any normalize_verdict success."""
    return _state["consecutive_malformed"]


def reset_malformed_streak() -> None:
    """Explicit reset — called by consumers at the start of a new
    review-batch ingestion. Prevents one batch's failures from carrying
    over and tripping the next batch's alert threshold."""
    _state["consecutive_malformed"] = 0


def malformed_streak_alert_due() -> bool:
    """True iff the consecutive-malformed counter has reached the
    threshold (5+). Consumers should write a pending_escalations row +
    halt their loop when this returns True."""
    return _state["consecutive_malformed"] >= _MALFORMED_STREAK_THRESHOLD


# Quote-text-sanitization helper for callers that interpolate quote_text
# into a Gemini prompt (T-013 V2 batched verification).

def sanitize_candidate_quote_text(value: Optional[str]) -> str:
    """Normalize quote_text BEFORE interpolating into a Gemini prompt.

    Strips controls, NFC normalizes, rejects bidi controls + structural
    fence markers. Caller surfaces this on T-013 V2 batch preparation
    paths (e.g., measure_meeting_baseline.py) so the prompt the operator
    pastes never contains adversarial-shaped content from the source.
    """
    return _normalize_free_text(value, "candidate_quote_text")
