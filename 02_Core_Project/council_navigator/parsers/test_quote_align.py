#!/usr/bin/env python3.11
# -*- coding: utf-8 -*-
"""
Unit tests for the `quote_align` alignment core.

Alignment maps a quote's words onto the Whisper transcript's word-level timings —
it underpins the karaoke highlight, the "Watch at <m:ss>" deep-links, and the
proof-clip boundaries. The subtle, bug-prone part is the dominant-cluster filter:
a common phrase ("I think") recurs across a 77-minute transcript, and without the
filter a spurious distant match drags a quote's apparent duration over the whole
meeting (the "quote 26" canary from development). These tests pin that filter plus
the happy path, the None cases, and the interpolation — synthetic transcripts, no
real DB, no new dependency.

Run:
    cd 02_Core_Project/council_navigator/parsers
    python3.11 test_quote_align.py
"""
from __future__ import annotations

import unittest

from quote_align import (
    _interpolate_unmatched,
    _normalize_token,
    _split_display_tokens,
    align_quote,
)


def _w(word: str, start: float, end: float) -> dict:
    return {"word": word, "start": start, "end": end}


class NormalizeTokenTest(unittest.TestCase):
    def test_lowercases(self):
        self.assertEqual(_normalize_token("FOX"), "fox")

    def test_strips_adjacent_punctuation(self):
        self.assertEqual(_normalize_token("Hello,"), "hello")

    def test_apostrophe_preserved(self):
        # Docstring promises apostrophe-preserving (so contractions survive).
        self.assertEqual(_normalize_token("it's"), "it's")

    def test_punctuation_only_is_empty(self):
        self.assertEqual(_normalize_token("--"), "")
        self.assertEqual(_normalize_token(""), "")


class SplitDisplayTokensTest(unittest.TestCase):
    def test_preserves_caps_and_punctuation(self):
        self.assertEqual(_split_display_tokens("Hello, World"), ["Hello,", "World"])

    def test_collapses_extra_whitespace(self):
        self.assertEqual(_split_display_tokens("a   b\tc"), ["a", "b", "c"])

    def test_empty_string(self):
        self.assertEqual(_split_display_tokens("   "), [])


class InterpolateUnmatchedTest(unittest.TestCase):
    def test_no_matches_is_all_zero(self):
        self.assertEqual(_interpolate_unmatched(2, {}), [(0.0, 0.0), (0.0, 0.0)])

    def test_lead_copy_and_tail(self):
        # One matched token in the middle; the leading + trailing tokens get
        # interpolated around it, monotonically.
        out = _interpolate_unmatched(3, {1: (2.0, 3.0)})
        self.assertEqual(out[1], (2.0, 3.0))            # matched copied verbatim
        self.assertEqual(out, [(1.5, 2.0), (2.0, 3.0), (3.0, 3.5)])
        # monotonic non-decreasing starts
        self.assertTrue(out[0][0] <= out[1][0] <= out[2][0])


class AlignQuoteHappyPathTest(unittest.TestCase):
    def test_verbatim_subphrase_maps_to_whisper_times(self):
        whisper = [
            _w("the", 5.0, 5.4),
            _w("quick", 5.5, 5.9),
            _w("brown", 6.0, 6.4),
            _w("fox", 6.5, 6.9),
        ]
        out = align_quote("quick brown fox", whisper)
        self.assertIsNotNone(out)
        self.assertEqual(
            out,
            [
                {"word": "quick", "start_ms": 5500, "end_ms": 5900},
                {"word": "brown", "start_ms": 6000, "end_ms": 6400},
                {"word": "fox", "start_ms": 6500, "end_ms": 6900},
            ],
        )

    def test_display_tokens_keep_caps_and_punctuation(self):
        whisper = [_w("water", 1.0, 1.4), _w("rights", 1.5, 1.9)]
        out = align_quote("Water rights,", whisper)
        self.assertIsNotNone(out)
        self.assertEqual([t["word"] for t in out], ["Water", "rights,"])
        # but timings come from the normalized match
        self.assertEqual(out[0]["start_ms"], 1000)
        self.assertEqual(out[1]["end_ms"], 1900)


class AlignQuoteNoneCasesTest(unittest.TestCase):
    def test_empty_quote_returns_none(self):
        self.assertIsNone(align_quote("", [_w("a", 1.0, 1.1)]))
        self.assertIsNone(align_quote("   ", [_w("a", 1.0, 1.1)]))

    def test_empty_whisper_returns_none(self):
        self.assertIsNone(align_quote("anything here", []))

    def test_all_punctuation_quote_returns_none(self):
        self.assertIsNone(align_quote("-- ... ,", [_w("a", 1.0, 1.1)]))

    def test_no_matching_block_returns_none(self):
        # Quote shares no >=2 contiguous run with the transcript → unalignable.
        whisper = [_w("completely", 1.0, 1.4), _w("different", 1.5, 1.9)]
        self.assertIsNone(align_quote("zebra giraffe elephant", whisper))


class AlignQuoteClusterFilterTest(unittest.TestCase):
    # Two DISTINCT phrases ("the cat sat" / "the dog ran") separated in the
    # transcript by filler the quote doesn't contain — so SequenceMatcher yields
    # two separate matching blocks, which is the shape the cluster filter acts on.
    QUOTE = "the cat sat the dog ran"

    def test_distant_spurious_block_is_discarded(self):
        # Anchor phrase at ~10s; the second phrase recurs ~290s later. Without
        # the dominant-cluster filter the quote's tail would be placed at ~300s,
        # stretching its apparent duration across the whole meeting. With it, the
        # far block is discarded and the tail is interpolated near the anchor.
        whisper = [
            _w("the", 10.0, 10.4),
            _w("cat", 10.5, 10.9),
            _w("sat", 11.0, 11.4),
            _w("umm", 20.0, 20.4),   # filler — not in the quote
            _w("okay", 20.5, 20.9),
            _w("the", 300.0, 300.4),
            _w("dog", 300.5, 300.9),
            _w("ran", 301.0, 301.4),
        ]
        out = align_quote(self.QUOTE, whisper)
        self.assertIsNotNone(out)
        # The anchor ("the cat sat") keeps its real times.
        self.assertEqual(out[0]["start_ms"], 10000)
        self.assertEqual(out[2]["end_ms"], 11400)
        # The whole quote stays near the anchor — NOT dragged to ~300s.
        self.assertLess(out[-1]["end_ms"], 20_000)

    def test_nearby_block_within_window_is_kept(self):
        # Same shape but the second phrase is only ~30s away (inside the 90s
        # window) → both blocks kept, so it lands on its real (nearby) times.
        whisper = [
            _w("the", 10.0, 10.4),
            _w("cat", 10.5, 10.9),
            _w("sat", 11.0, 11.4),
            _w("umm", 20.0, 20.4),
            _w("okay", 20.5, 20.9),
            _w("the", 40.0, 40.4),
            _w("dog", 40.5, 40.9),
            _w("ran", 41.0, 41.4),
        ]
        out = align_quote(self.QUOTE, whisper)
        self.assertIsNotNone(out)
        self.assertEqual(out[-1]["start_ms"], 41000)  # "ran" kept its real time


class AlignQuoteInterpolationTest(unittest.TestCase):
    def test_middle_gap_is_interpolated_between_anchors(self):
        # Two anchor blocks ("alpha beta" + "gamma delta") with one unmatched
        # quote word ("mid") between them → "mid" gets a timing between the
        # blocks, monotonically.
        whisper = [
            _w("alpha", 1.0, 1.4),
            _w("beta", 1.5, 1.9),
            _w("xxx", 2.0, 2.4),   # transcript has a different middle word
            _w("gamma", 2.5, 2.9),
            _w("delta", 3.0, 3.4),
        ]
        out = align_quote("alpha beta mid gamma delta", whisper)
        self.assertIsNotNone(out)
        self.assertEqual(len(out), 5)
        mid = out[2]
        # interpolated strictly between beta.end (1.9s) and gamma.start (2.5s)
        self.assertGreaterEqual(mid["start_ms"], 1900)
        self.assertLessEqual(mid["end_ms"], 2500)
        # overall monotonic non-decreasing starts
        starts = [t["start_ms"] for t in out]
        self.assertEqual(starts, sorted(starts))


if __name__ == "__main__":
    unittest.main(verbosity=2)
