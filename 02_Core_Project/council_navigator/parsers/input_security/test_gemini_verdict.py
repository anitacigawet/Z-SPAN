"""S-008 V0 / surface S-6 — Gemini verdict strict-mode normalizer tests.

Exercises `parsers.gemini_verdict_normalize`:
- normalize_verdict accepts canonical clip verdicts.
- normalize_verdict raises StrictVerdictError on enum-out-of-range,
  empty filename, bidi controls, fence markers, over-length free text.
- consecutive_malformed_count tracks strict failures + resets on success.
- malformed_streak_alert_due fires at the 5+ threshold.
- sanitize_candidate_quote_text reuses the same gate on quote_text.

Per [D-100](../../../../01_Project_Overview/DECISIONS.md#d-100), test
inputs are negative-test cases for the strict-mode classifier.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass, field

from parsers.gemini_verdict_normalize import (
    NormalizedVerdict,
    StrictVerdictError,
    consecutive_malformed_count,
    malformed_streak_alert_due,
    normalize_verdict,
    reset_malformed_streak,
    sanitize_candidate_quote_text,
)


@dataclass
class FakeClipVerdict:
    """Stand-in for review_response_parser.ClipVerdict — the strict-mode
    normalizer reads attributes via properties, so we replicate that shape
    here without importing the full review_response_parser dependency tree."""

    filename: str
    raw_fields: dict = field(default_factory=dict)

    @property
    def speaker_attribution(self) -> str:
        return (self.raw_fields.get("speaker_attribution") or "").strip().lower()

    @property
    def text_accuracy(self) -> str:
        return (self.raw_fields.get("text_accuracy") or "").strip().lower()

    @property
    def text_differences(self) -> str:
        return (self.raw_fields.get("text_differences") or "").strip()

    @property
    def clip_integrity(self) -> str:
        return (self.raw_fields.get("clip_integrity") or "").strip().lower()

    @property
    def other_concerns(self) -> str:
        return (self.raw_fields.get("other_concerns") or "").strip()


def _clean_verdict(filename: str = "clip001.mp3") -> FakeClipVerdict:
    return FakeClipVerdict(
        filename=filename,
        raw_fields={
            "speaker_attribution": "yes",
            "speaker_attribution_notes": "Speaker is mayor.",
            "text_accuracy": "yes",
            "text_differences": "none",
            "clip_integrity": "ok",
            "other_concerns": "none",
        },
    )


class StrictNormalizeTests(unittest.TestCase):
    def setUp(self):
        reset_malformed_streak()

    def test_clean_verdict_normalizes(self):
        out = normalize_verdict(_clean_verdict())
        self.assertIsInstance(out, NormalizedVerdict)
        self.assertEqual(out.filename, "clip001.mp3")
        self.assertEqual(out.speaker_attribution, "yes")
        self.assertEqual(out.text_accuracy, "yes")
        self.assertEqual(out.clip_integrity, "ok")

    def test_missing_filename_rejected(self):
        v = _clean_verdict("")
        with self.assertRaises(StrictVerdictError):
            normalize_verdict(v)

    def test_unknown_speaker_attribution_rejected(self):
        v = _clean_verdict()
        v.raw_fields["speaker_attribution"] = "maybe"
        with self.assertRaises(StrictVerdictError):
            normalize_verdict(v)

    def test_unknown_text_accuracy_rejected(self):
        v = _clean_verdict()
        v.raw_fields["text_accuracy"] = "kinda"
        with self.assertRaises(StrictVerdictError):
            normalize_verdict(v)

    def test_unknown_clip_integrity_rejected(self):
        v = _clean_verdict()
        v.raw_fields["clip_integrity"] = "broken-record"
        with self.assertRaises(StrictVerdictError):
            normalize_verdict(v)

    def test_bidi_in_text_differences_rejected(self):
        v = _clean_verdict()
        v.raw_fields["text_differences"] = "something ‮ reversed"
        with self.assertRaises(StrictVerdictError):
            normalize_verdict(v)

    def test_fence_marker_in_other_concerns_rejected(self):
        v = _clean_verdict()
        v.raw_fields["other_concerns"] = "<zspan-content-begin nonce=\"x\">"
        with self.assertRaises(StrictVerdictError):
            normalize_verdict(v)

    def test_over_length_text_differences_rejected(self):
        v = _clean_verdict()
        v.raw_fields["text_differences"] = "x" * 100_000
        with self.assertRaises(StrictVerdictError):
            normalize_verdict(v)

    def test_normalized_text_strips_controls(self):
        v = _clean_verdict()
        v.raw_fields["text_differences"] = "a\x07b"
        out = normalize_verdict(v)
        self.assertEqual(out.text_differences, "ab")

    def test_empty_optional_fields_become_empty_strings(self):
        v = _clean_verdict()
        v.raw_fields.pop("other_concerns", None)
        out = normalize_verdict(v)
        self.assertEqual(out.other_concerns, "")


class MalformedStreakTests(unittest.TestCase):
    def setUp(self):
        reset_malformed_streak()

    def test_clean_keeps_streak_at_zero(self):
        normalize_verdict(_clean_verdict())
        self.assertEqual(consecutive_malformed_count(), 0)
        self.assertFalse(malformed_streak_alert_due())

    def test_failure_increments_streak(self):
        v = _clean_verdict()
        v.raw_fields["speaker_attribution"] = "wat"
        with self.assertRaises(StrictVerdictError):
            normalize_verdict(v)
        self.assertEqual(consecutive_malformed_count(), 1)

    def test_streak_resets_on_success(self):
        v_bad = _clean_verdict()
        v_bad.raw_fields["speaker_attribution"] = "wat"
        for _ in range(3):
            with self.assertRaises(StrictVerdictError):
                normalize_verdict(v_bad)
        self.assertEqual(consecutive_malformed_count(), 3)
        normalize_verdict(_clean_verdict())
        self.assertEqual(consecutive_malformed_count(), 0)

    def test_alert_threshold(self):
        v = _clean_verdict()
        v.raw_fields["speaker_attribution"] = "wat"
        for _ in range(4):
            with self.assertRaises(StrictVerdictError):
                normalize_verdict(v)
        self.assertFalse(malformed_streak_alert_due())  # 4 < 5
        with self.assertRaises(StrictVerdictError):
            normalize_verdict(v)
        self.assertTrue(malformed_streak_alert_due())  # 5 >= 5

    def test_reset_explicit(self):
        v_bad = _clean_verdict()
        v_bad.raw_fields["speaker_attribution"] = "wat"
        for _ in range(5):
            with self.assertRaises(StrictVerdictError):
                normalize_verdict(v_bad)
        reset_malformed_streak()
        self.assertEqual(consecutive_malformed_count(), 0)
        self.assertFalse(malformed_streak_alert_due())


class QuoteTextSanitizationTests(unittest.TestCase):
    def test_clean_quote_text_normalized(self):
        out = sanitize_candidate_quote_text(
            "I move that we adopt the consent agenda."
        )
        self.assertEqual(out, "I move that we adopt the consent agenda.")

    def test_bidi_in_quote_text_rejected(self):
        with self.assertRaises(StrictVerdictError):
            sanitize_candidate_quote_text("a ‮ b")

    def test_fence_marker_in_quote_text_rejected(self):
        with self.assertRaises(StrictVerdictError):
            sanitize_candidate_quote_text(
                "<zspan-content-begin nonce=\"x\">"
            )

    def test_empty_quote_text_returns_empty(self):
        self.assertEqual(sanitize_candidate_quote_text(""), "")
        self.assertEqual(sanitize_candidate_quote_text(None), "")


if __name__ == "__main__":
    unittest.main()
