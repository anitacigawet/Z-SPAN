"""Unit tests for deterministic Key Decisions verbatim spans."""
from __future__ import annotations

import unittest

from zspan_pipeline.decision_verbatim import extract_verbatim_spans


def _chunk(body: str, *, start_seconds: float = 42.0, chunk_index: int = 7) -> dict:
    return {
        "body": body,
        "start_seconds": start_seconds,
        "chunk_index": chunk_index,
    }


class ExtractVerbatimSpansTests(unittest.TestCase):
    def test_motion_carries_hit(self) -> None:
        display = "The motion carries, authorizing the street project."
        spans = extract_verbatim_spans(
            display,
            [_chunk("All those in favor say aye. The motion carries. Next item.")],
        )
        self.assertEqual([span["text"] for span in spans], ["motion carries"])
        self.assertEqual(display[spans[0]["char_start"]:spans[0]["char_end"]], spans[0]["text"])
        self.assertEqual(spans[0]["signature_id"], "carries")

    def test_approved_unanimously_hit(self) -> None:
        display = "The contract was approved unanimously."
        spans = extract_verbatim_spans(
            display,
            [_chunk("Following discussion, the contract was approved unanimously.")],
        )
        self.assertEqual([span["text"] for span in spans], ["approved unanimously"])

    def test_bare_tally_without_strong_anchor_is_not_a_hit(self) -> None:
        spans = extract_verbatim_spans(
            "The term was extended seven to zero years.",
            [_chunk("The estimated range changed from seven to zero years.")],
        )
        self.assertEqual(spans, [])

    def test_generic_motion_is_not_a_hit(self) -> None:
        spans = extract_verbatim_spans(
            "The motion concerned the agenda item.",
            [_chunk("A motion and a second were discussed before the vote.")],
        )
        self.assertEqual(spans, [])

    def test_lone_result_verb_requires_and_uses_a_strong_frame(self) -> None:
        display = "The council adopted the budget."
        spans = extract_verbatim_spans(
            display,
            [_chunk("After the hearing, the council adopted the budget.")],
        )
        self.assertEqual([span["text"] for span in spans], ["adopted"])
        self.assertEqual(spans[0]["signature_id"], "adopt_instrument")

    def test_numeric_and_spoken_tallies_are_not_equivalent(self) -> None:
        display = "The permit passed 7-0."
        spans = extract_verbatim_spans(
            display,
            [
                _chunk("The motion carries seven to zero."),
                _chunk("The motion carries 7-0.", chunk_index=8),
            ],
        )
        self.assertEqual([span["text"] for span in spans], ["7-0"])
        self.assertEqual(spans[0]["chunk_index"], 8)


if __name__ == "__main__":
    unittest.main()
