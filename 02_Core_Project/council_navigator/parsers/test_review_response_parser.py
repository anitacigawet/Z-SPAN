#!/usr/bin/env python3.11
# -*- coding: utf-8 -*-
"""
Unit tests for `review_response_parser` — the D-043 verification-parsing logic.

This module is the LAST step of the triple-source verification chain: it turns
Gemini Pro's structured RESPONSE.md into per-clip verdicts (verified / disputed /
rejected) and the mechanical `"X" should be "Y"` text corrections. A silent
regression here would mislabel verdicts or misapply corrections on the civic
record — exactly the kind of correctness the chain exists to protect. So the
pure logic is pinned here (stdlib unittest, no new dependency).

Run:
    cd 02_Core_Project/council_navigator/parsers
    python3.11 test_review_response_parser.py
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from review_response_parser import (
    ClipVerdict,
    apply_substitutions,
    classify_decision,
    extract_substitutions,
    parse_response_file,
)


def _verdict(**fields) -> ClipVerdict:
    """Build a ClipVerdict from raw field values (as Gemini would emit them)."""
    return ClipVerdict(filename="clip.mp4", raw_fields=dict(fields))


class ExtractSubstitutionsTest(unittest.TestCase):
    def test_straight_quotes(self):
        self.assertEqual(
            extract_substitutions('"POSOS systems" should be "POS systems"'),
            [("POSOS systems", "POS systems")],
        )

    def test_curly_quotes(self):
        # Gemini sometimes emits typographic quotes; both must parse.
        self.assertEqual(
            extract_substitutions("“Annie Divine” should be “Andy Devine”"),
            [("Annie Divine", "Andy Devine")],
        )

    def test_multiple_semicolon_separated(self):
        td = '"counselor Sy" should be "Counselor Stehly"; "Bee Street" should be "Beale Street"'
        self.assertEqual(
            extract_substitutions(td),
            [("counselor Sy", "Counselor Stehly"), ("Bee Street", "Beale Street")],
        )

    def test_case_insensitive_should_be(self):
        self.assertEqual(
            extract_substitutions('"foo" SHOULD BE "bar"'), [("foo", "bar")]
        )

    def test_prose_yields_nothing(self):
        # Non-substitution prose must not produce a spurious correction.
        self.assertEqual(
            extract_substitutions("Transcript merges dialogue from three speakers"), []
        )

    def test_empty_and_none(self):
        self.assertEqual(extract_substitutions(""), [])
        self.assertEqual(extract_substitutions(None), [])  # type: ignore[arg-type]

    def test_identical_wrong_and_right_skipped(self):
        self.assertEqual(extract_substitutions('"same" should be "same"'), [])


class ClassifyDecisionTest(unittest.TestCase):
    def test_speaker_no_is_rejected(self):
        self.assertEqual(
            classify_decision(_verdict(speaker_attribution="no", text_accuracy="yes",
                                       clip_integrity="ok")),
            "rejected",
        )

    def test_text_no_is_rejected(self):
        self.assertEqual(
            classify_decision(_verdict(speaker_attribution="yes", text_accuracy="no",
                                       clip_integrity="ok")),
            "rejected",
        )

    def test_rejected_takes_precedence_over_bad_clip(self):
        # speaker=no wins even when the clip is also flagged.
        self.assertEqual(
            classify_decision(_verdict(speaker_attribution="no", text_accuracy="yes",
                                       clip_integrity="cuts-mid-word")),
            "rejected",
        )

    def test_bad_clip_overrides_otherwise_clean(self):
        # A suspect clip is disputed even with a yes/yes verdict (clip check
        # comes before the yes/yes → verified path).
        self.assertEqual(
            classify_decision(_verdict(speaker_attribution="yes", text_accuracy="yes",
                                       clip_integrity="audio-issue")),
            "disputed",
        )

    def test_uncertain_speaker_is_disputed(self):
        self.assertEqual(
            classify_decision(_verdict(speaker_attribution="uncertain", text_accuracy="yes",
                                       clip_integrity="ok")),
            "disputed",
        )

    def test_mostly_with_substitution_is_verified(self):
        self.assertEqual(
            classify_decision(_verdict(speaker_attribution="yes", text_accuracy="mostly",
                                       text_differences='"POSOS" should be "POS"',
                                       clip_integrity="ok")),
            "verified",
        )

    def test_mostly_with_disfluency_prose_is_verified(self):
        # Filler/false-start/disfluency smoothing is exactly what the cleaner
        # already does — accept it without a substitution pattern.
        for prose in (
            "filler words removed",
            "a false start was trimmed",
            "stutter smoothed out",
            "minor disfluencies removed",
        ):
            with self.subTest(prose=prose):
                self.assertEqual(
                    classify_decision(_verdict(speaker_attribution="yes",
                                               text_accuracy="mostly",
                                               text_differences=prose,
                                               clip_integrity="ok")),
                    "verified",
                )

    def test_mostly_with_unfixable_prose_is_disputed(self):
        # A real content difference we can't mechanically apply → human review.
        self.assertEqual(
            classify_decision(_verdict(speaker_attribution="yes", text_accuracy="mostly",
                                       text_differences="missing the measurement '1100 ft'",
                                       clip_integrity="ok")),
            "disputed",
        )

    def test_clean_yes_yes_is_verified(self):
        self.assertEqual(
            classify_decision(_verdict(speaker_attribution="yes", text_accuracy="yes",
                                       clip_integrity="ok")),
            "verified",
        )

    def test_empty_fields_fall_back_to_disputed(self):
        self.assertEqual(classify_decision(_verdict()), "disputed")


class ApplySubstitutionsTest(unittest.TestCase):
    def test_simple_replace_logs_count(self):
        new_text, log = apply_substitutions(
            "the POSOS systems are down", [("POSOS", "POS")]
        )
        self.assertEqual(new_text, "the POS systems are down")
        self.assertEqual(log, [{"from": "POSOS", "to": "POS", "count": 1}])

    def test_no_match_is_logged_count_zero(self):
        # Gemini sometimes paraphrases the "wrong" string so it isn't verbatim;
        # the audit trail must record that we found no match (count 0).
        new_text, log = apply_substitutions("untouched text", [("Foo", "Bar")])
        self.assertEqual(new_text, "untouched text")
        self.assertEqual(log, [{"from": "Foo", "to": "Bar", "count": 0}])

    def test_multiple_occurrences_counted(self):
        new_text, log = apply_substitutions("aa and aa", [("aa", "bb")])
        self.assertEqual(new_text, "bb and bb")
        self.assertEqual(log[0]["count"], 2)

    def test_sequential_substitutions(self):
        new_text, log = apply_substitutions(
            "x then y", [("x", "1"), ("y", "2")]
        )
        self.assertEqual(new_text, "1 then 2")
        self.assertEqual(len(log), 2)

    def test_empty_substitutions_noop(self):
        new_text, log = apply_substitutions("unchanged", [])
        self.assertEqual(new_text, "unchanged")
        self.assertEqual(log, [])


_RESPONSE_WITH_TWO_CLIPS = """# Batch 02 review
- **Response received:** 2026-05-16 14:30

## Gemini response (paste Gemini's reply below this line)

## clip: quote_29__jamie.mp4

* speaker_attribution: yes
* speaker_attribution_notes: clearly Jamie
* text_accuracy: mostly
* text_differences: "Annie Divine" should be "Andy Devine"
* clip_integrity: ok
* other_concerns: none

## clip: quote_30__ken.mp4

* speaker_attribution: uncertain
* text_accuracy: yes
* clip_integrity: ok

## BATCH COMPLETE
"""

_RESPONSE_STUB = """# Batch 01 review
- **Response received:** _[REPLACE THIS with when you saved Gemini's reply]_

## Gemini response (paste Gemini's reply below this line)

(nothing pasted yet)
"""


class ParseResponseFileTest(unittest.TestCase):
    def _parse(self, content: str):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "RESPONSE.md"
            p.write_text(content, encoding="utf-8")
            return parse_response_file(p)

    def test_realistic_two_clip_response(self):
        parsed = self._parse(_RESPONSE_WITH_TWO_CLIPS)
        self.assertEqual(parsed.response_received, "2026-05-16 14:30")
        self.assertFalse(parsed.response_received_is_placeholder)
        self.assertTrue(parsed.has_batch_complete_marker)
        self.assertEqual(len(parsed.clips), 2)

        c0, c1 = parsed.clips
        self.assertEqual(c0.filename, "quote_29__jamie.mp4")
        self.assertEqual(c0.speaker_attribution, "yes")
        self.assertEqual(c0.text_accuracy, "mostly")
        self.assertEqual(
            extract_substitutions(c0.text_differences), [("Annie Divine", "Andy Devine")]
        )
        # End-to-end: the two clips classify as the chain intends.
        self.assertEqual(classify_decision(c0), "verified")   # mostly + substitution
        self.assertEqual(classify_decision(c1), "disputed")   # uncertain speaker

    def test_unfilled_stub_is_flagged_placeholder(self):
        parsed = self._parse(_RESPONSE_STUB)
        self.assertTrue(parsed.response_received_is_placeholder)
        self.assertFalse(parsed.has_batch_complete_marker)
        self.assertEqual(parsed.clips, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
