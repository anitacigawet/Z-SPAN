"""Deterministic transcript-excerpt contract tests for Key Decisions."""

from __future__ import annotations

import copy
import unittest

from council_navigator.parsers import quote_align


def _word(token: str, start: float, end: float) -> dict:
    return {"word": token, "start": start, "end": end}


class TranscriptExcerptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.words = [
            _word("Lake", 8.0, 9.0),
            _word("Kavasu,", 9.0, 10.0),
            _word("NO-cleanup", 20.0, 21.0),
            _word("middle", 22.0, 23.0),
            _word("motion", 310.0, 310.5),
            _word("carried.", 310.5, 311.0),
        ]
        self.item = {"matched_word_index": 0, "matched_end_word_index": 1}
        self.action = {"matched_word_index": 4, "matched_end_word_index": 5}

    def test_gap_exactly_300_seconds_is_contiguous_and_token_exact(self):
        first = quote_align.materialize_transcript_excerpt(
            self.words, self.item, self.action,
        )
        second = quote_align.materialize_transcript_excerpt(
            self.words, self.item, self.action,
        )

        self.assertEqual(first, second)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["structure"], "contiguous")
        self.assertEqual(
            first[0]["label"],
            "Verbatim transcript excerpt — complete",
        )
        self.assertEqual(
            first[0]["text"],
            "Lake Kavasu, NO-cleanup middle motion carried.",
        )
        self.assertEqual(first[0]["start_word_index"], 0)
        self.assertEqual(first[0]["end_word_index"], 5)

    def test_gap_just_above_threshold_is_elided_with_exact_arithmetic(self):
        words = copy.deepcopy(self.words)
        words[4]["start"] = 310.001
        words[4]["end"] = 310.5
        spans = quote_align.materialize_transcript_excerpt(
            words, self.item, self.action,
        )

        self.assertEqual(len(spans), 2)
        self.assertEqual([span["text"] for span in spans], [
            "Lake Kavasu,",
            "motion carried.",
        ])
        self.assertEqual(
            spans[0]["label"],
            "Verbatim transcript excerpts — middle omitted",
        )
        self.assertEqual(
            spans[0]["omission_marker"],
            "[Transcript omitted between verbatim passages: "
            "2 words · 00:05:00.001 elapsed]",
        )
        self.assertEqual(spans[0]["omission_marker"], spans[1]["omission_marker"])

    def test_validation_rejects_text_index_and_range_corruption(self):
        spans = quote_align.materialize_transcript_excerpt(
            self.words, self.item, self.action,
        )
        altered = copy.deepcopy(spans)
        altered[0]["text"] = altered[0]["text"].replace("Kavasu", "Havasu")
        altered[0]["end_word_index"] = 99
        errors = quote_align.validate_transcript_excerpt_spans(
            self.words, altered, self.item, self.action,
        )
        self.assertIn("span_1_text_mismatch", errors)
        self.assertIn("span_1_end_word_index_mismatch", errors)

        with self.assertRaisesRegex(ValueError, "out of bounds or reversed"):
            quote_align.materialize_transcript_excerpt(
                self.words,
                {"matched_word_index": 2, "matched_end_word_index": 1},
                self.action,
            )

    def test_legacy_sidecar_materializes_only_with_both_anchors(self):
        sidecar = {
            "prose_list_count": 1,
            "prose_output": "1. Approved it [at 0:05:10].",
            "citation_alignment": [{
                "output_index": 1,
                "source": "two_part_quote",
                "item_evidence": self.item,
                "action_evidence": self.action,
            }],
            "decisions": [{"index": 1, "verbatim_spans": []}],
        }
        derived = quote_align.materialize_legacy_decision_excerpts(
            sidecar, self.words,
        )
        self.assertEqual(
            derived["citation_modality"],
            quote_align.TRANSCRIPT_EXCERPT_MODALITY,
        )
        self.assertNotEqual(derived, sidecar)
        self.assertEqual(sidecar["decisions"][0]["verbatim_spans"], [])

        missing = copy.deepcopy(sidecar)
        del missing["citation_alignment"][0]["action_evidence"]
        self.assertNotIn(
            "citation_modality",
            quote_align.materialize_legacy_decision_excerpts(missing, self.words),
        )


if __name__ == "__main__":
    unittest.main()
