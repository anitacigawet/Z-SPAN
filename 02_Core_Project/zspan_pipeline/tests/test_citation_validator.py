"""Offline tests for key-decision locator validation and alignment."""

from __future__ import annotations

import unittest

from council_navigator.parsers.quote_align import align_quote_with_evidence
from zspan_pipeline.citation_validator import (
    CITATION_RE,
    align_decision_citations,
    allowed_seconds,
    format_citation,
    parse_citations,
    resolve_inline_verbatim_anchors,
    serialize_verbatim_anchor_resolution,
    split_numbered_items,
    strip_audit_block,
    validate_inline_citations,
)


def _phrase(start: float, text: str) -> list[dict]:
    return [
        {"word": word, "start": start + index * 0.4, "end": start + index * 0.4 + 0.3}
        for index, word in enumerate(text.split())
    ]


class CitationGrammarTests(unittest.TestCase):
    def test_combined_grammar_is_locked(self):
        self.assertEqual(
            CITATION_RE.pattern,
            r"\[at (?:(?P<hours>0|[1-9]\d*):(?P<hour_minutes>[0-5]\d):"
            r"(?P<hour_seconds>[0-5]\d)|(?P<legacy_minutes>\d{1,3}):"
            r"(?P<legacy_seconds>[0-5]\d))\]",
        )

    def test_parse_accepts_canonical_and_legacy_long_meeting_shapes(self):
        text = "Canonical [at 3:24:38], legacy [at 204:38], short [at 8:15]."
        citations = parse_citations(text)

        self.assertEqual(
            [citation.raw for citation in citations],
            ["[at 3:24:38]", "[at 204:38]", "[at 8:15]"],
        )
        self.assertEqual(
            [citation.total_seconds for citation in citations],
            [12_278, 12_278, 495],
        )
        self.assertEqual(
            [citation.canonical for citation in citations],
            [True, False, False],
        )

    def test_parse_rejects_invalid_or_noncanonical_shapes(self):
        text = "[at 01:02:03] [at 1:2:03] [at 1:60:00] [at 8:5] [at 8:99]"
        # Leading-zero hours are intentionally not canonical; none of these
        # malformed shapes may become a seek target.
        self.assertEqual(parse_citations(text), [])

    def test_format_always_writes_canonical_hours(self):
        self.assertEqual(format_citation(495.9), "[at 0:08:15]")
        self.assertEqual(format_citation(12_278.9), "[at 3:24:38]")


class NumberedItemTests(unittest.TestCase):
    def test_splits_markup_blank_lines_and_strips_audit(self):
        text = """Preamble to ignore.

1. <core>Approved the item</core> [at 0:08:15].

   <nuance>Subject to review.</nuance>

2) **Awarded the contract** [at 12:09].

<!-- audit [{"decision": 1}] audit -->
"""
        items = split_numbered_items(text)

        self.assertEqual(len(items), 2)
        self.assertIn("<core>Approved the item</core>", items[0])
        self.assertEqual(items[1], "**Awarded the contract** [at 12:09].")
        self.assertNotIn("audit", items[1])

    def test_empty_and_unnumbered_text_return_no_items(self):
        self.assertEqual(split_numbered_items(""), [])
        self.assertEqual(split_numbered_items("No decisions were made."), [])

    def test_plain_trailing_comment_is_tolerated(self):
        text = "1. Approved [at 0:01:00].\n<!-- internal metadata -->"
        self.assertEqual(strip_audit_block(text), "1. Approved [at 0:01:00].")


class ValidationTests(unittest.TestCase):
    def test_legacy_chunk_start_api_keeps_flooring_behavior(self):
        self.assertEqual(allowed_seconds([495.7, 7_767.99]), {495, 7_767})
        report = validate_inline_citations(
            "1. Approved [at 8:15].",
            [495.7],
        )
        self.assertEqual(report.state, "valid")

    def test_word_precise_offsets_validate_inside_or_near_chunk(self):
        text = (
            "1. Approved [at 1:15:26].\n\n"
            "2. Appointed a commissioner [at 0:26:35]."
        )
        report = validate_inline_citations(
            text,
            [(4_416.9, 4_552.0), (812.5, 1_638.7)],
        )

        self.assertEqual(report.state, "valid")
        self.assertEqual(report.member_citations, 2)

    def test_missing_and_out_of_range_citations_are_errored(self):
        text = "1. Approved [at 3:24:38].\n\n2. Continued the hearing."
        report = validate_inline_citations(text, [(100.0, 500.0)])

        self.assertEqual(report.state, "errored")
        self.assertEqual(report.covered_indices, [1])
        self.assertEqual(report.uncovered_indices, [2])
        self.assertEqual(report.unknown_citations, ["[at 3:24:38]"])

    def test_independently_anchored_chunk_miss_is_an_observation(self):
        report = validate_inline_citations(
            "1. Approved [at 0:10:50].",
            [(80.0, 500.0)],
            membership_observation_reasons={
                1: "quote_anchored_outside_retrieved_chunks",
            },
        )

        self.assertEqual(report.state, "valid")
        self.assertEqual(report.unknown_citations, [])
        self.assertEqual(report.member_citations, 0)
        self.assertEqual(
            report.nonmember_observations,
            [
                {
                    "index": 1,
                    "citation": "[at 0:10:50]",
                    "total_seconds": 650,
                    "reason": "quote_anchored_outside_retrieved_chunks",
                }
            ],
        )

    def test_zero_decisions_has_forensic_state(self):
        report = validate_inline_citations("No numbered decisions.", [(0.0, 500.0)])
        self.assertEqual(report.state, "no_decisions_extracted")


class VerbatimAnchorResolutionTests(unittest.TestCase):
    @staticmethod
    def _chunk(
        body: str,
        *,
        chunk_index: int,
        start_seconds: float,
        end_seconds: float,
        speaker_turns: list[dict] | None = None,
    ) -> dict:
        return {
            "body": body,
            "chunk_index": chunk_index,
            "start_seconds": start_seconds,
            "end_seconds": end_seconds,
            "speaker_turns": speaker_turns,
        }

    def test_resolve_inline_verbatim_anchors_resolved_state(self):
        first_quote = "The motion carried six to one."
        second_quote = "Council approved the regional water contract."
        text = (
            f'The motion passed [at "{first_quote}"] and the contract advanced '
            f'[at "{second_quote}"].'
        )
        chunks = [
            self._chunk(
                first_quote,
                chunk_index=7,
                start_seconds=9.0,
                end_seconds=20.0,
            ),
            self._chunk(
                second_quote,
                chunk_index=8,
                start_seconds=29.0,
                end_seconds=40.0,
            ),
        ]
        words = _phrase(10.0, first_quote) + _phrase(30.0, second_quote)

        result = resolve_inline_verbatim_anchors(text, chunks, words)

        self.assertEqual(result.state, "resolved")
        self.assertEqual(result.anchors_total, 2)
        self.assertEqual(result.failures, ())
        self.assertEqual(len(result.aligned), 2)
        self.assertEqual(
            result.text,
            "The motion passed [at 0:00:10] and the contract advanced "
            "[at 0:00:30].",
        )
        self.assertEqual(
            [record["canonical_citation"] for record in result.aligned],
            ["[at 0:00:10]", "[at 0:00:30]"],
        )
        self.assertNotIn("timings", result.aligned[0]["alignment_evidence"])
        serialized = serialize_verbatim_anchor_resolution(result)
        self.assertEqual(serialized["resolution_state"], "resolved")
        self.assertIsInstance(serialized["aligned"], list)
        self.assertIsInstance(serialized["aligned"][0]["source_span"], list)

    def test_resolve_inline_verbatim_anchors_diarized_chunk_speaker_turns(self):
        quote = "The motion carried six to one."
        text = f'The motion passed [at "{quote}"].'
        chunk = self._chunk(
            "Hidden undiarized body does not contain the copied phrase.",
            chunk_index=7,
            start_seconds=9.0,
            end_seconds=20.0,
            speaker_turns=[
                {
                    "speaker_label": "SPEAKER_01",
                    "start": 10.0,
                    "end": 12.0,
                    "text": quote,
                }
            ],
        )

        result = resolve_inline_verbatim_anchors(
            text,
            [chunk],
            _phrase(10.0, quote),
        )

        self.assertEqual(result.state, "resolved")
        self.assertEqual(result.text, "The motion passed [at 0:00:10].")
        self.assertEqual(result.aligned[0]["matching_chunk_indices"], (7,))

    def test_resolve_inline_verbatim_anchors_direct_timestamp_is_nonconforming(self):
        text = "The motion passed [at 12:34]."

        result = resolve_inline_verbatim_anchors(text, [], [])

        self.assertEqual(result.state, "nonconforming")
        self.assertEqual(result.anchors_total, 1)
        self.assertEqual(result.text, text)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.failures[0]["reason"], "direct_timestamp_bypass")
        self.assertEqual(result.failures[0]["raw_anchor"], "[at 12:34]")
        self.assertEqual(result.failures[0]["timestamp_seconds"], 754)

    def test_resolve_inline_verbatim_anchors_zero_anchors_is_nonconforming(self):
        text = "The council discussed the item without making a decision."

        result = resolve_inline_verbatim_anchors(text, [], [])

        self.assertEqual(result.state, "nonconforming")
        self.assertEqual(result.anchors_total, 0)
        self.assertEqual(result.text, text)
        self.assertEqual(result.aligned, ())
        self.assertEqual(result.failures, ())

    def test_resolve_inline_verbatim_anchors_missing_quote_is_degraded(self):
        spoken = "The motion carried six to one."
        missing = "These exact words were never retrieved."
        text = f'The council acted [at "{missing}"].'
        chunks = [
            self._chunk(
                spoken,
                chunk_index=7,
                start_seconds=9.0,
                end_seconds=20.0,
            )
        ]

        result = resolve_inline_verbatim_anchors(
            text,
            chunks,
            _phrase(10.0, spoken),
        )

        self.assertEqual(result.state, "degraded")
        self.assertEqual(result.text, text)
        self.assertEqual(result.aligned, ())
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(
            result.failures[0]["reason"],
            "quote_not_in_retrieved_chunks",
        )
        self.assertEqual(result.failures[0]["quote"], missing)

    def test_resolve_inline_verbatim_anchors_ambiguous_alignment_is_degraded(self):
        quote = "The motion carried six to one."
        text = f'The motion passed [at "{quote}"].'
        chunks = [
            self._chunk(
                quote,
                chunk_index=7,
                start_seconds=9.0,
                end_seconds=20.0,
            ),
            self._chunk(
                quote,
                chunk_index=8,
                start_seconds=29.0,
                end_seconds=40.0,
            ),
        ]
        words = _phrase(10.0, quote) + _phrase(30.0, quote)

        result = resolve_inline_verbatim_anchors(text, chunks, words)

        self.assertEqual(result.state, "degraded")
        self.assertEqual(result.text, text)
        self.assertEqual(len(result.failures), 1)
        failure = result.failures[0]
        self.assertEqual(failure["reason"], "quote_aligned_to_distinct_moments")
        self.assertEqual(
            failure["canonical_citations"],
            ("[at 0:00:10]", "[at 0:00:30]"),
        )
        self.assertEqual(len(failure["chunk_evidence"]), 2)

    def test_resolve_inline_verbatim_anchors_atomic_all_or_nothing_rewrite(self):
        good_quote = "The motion carried six to one."
        bad_quote = "These exact words were never retrieved."
        text = (
            f'The motion passed [at "{good_quote}"] but another claim '
            f'failed [at "{bad_quote}"].'
        )
        chunks = [
            self._chunk(
                good_quote,
                chunk_index=7,
                start_seconds=9.0,
                end_seconds=20.0,
            )
        ]

        result = resolve_inline_verbatim_anchors(
            text,
            chunks,
            _phrase(10.0, good_quote),
        )

        self.assertEqual(result.state, "degraded")
        self.assertEqual(result.text, text)
        self.assertEqual(len(result.aligned), 1)
        self.assertEqual(result.aligned[0]["canonical_citation"], "[at 0:00:10]")
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.failures[0]["ordinal"], 2)

    def test_resolve_inline_verbatim_anchors_word_bounds(self):
        too_short = "only two"
        too_long = " ".join(f"word{index}" for index in range(1, 22))
        text = (
            f'First claim [at "{too_short}"]. Second claim '
            f'[at "{too_long}"].'
        )

        result = resolve_inline_verbatim_anchors(text, [], [])

        self.assertEqual(result.state, "degraded")
        self.assertEqual(result.anchors_total, 2)
        self.assertEqual(result.text, text)
        self.assertEqual([row["word_count"] for row in result.failures], [2, 21])
        self.assertTrue(
            all(row["reason"] == "quote_word_count_out_of_bounds" for row in result.failures)
        )
        self.assertTrue(all(row["min_words"] == 3 for row in result.failures))
        self.assertTrue(all(row["max_words"] == 20 for row in result.failures))

    def test_resolve_inline_verbatim_anchors_overlap_dedup(self):
        quote = "The motion carried six to one."
        text = f'The motion passed [at "{quote}"].'
        chunks = [
            self._chunk(
                quote,
                chunk_index=7,
                start_seconds=9.5,
                end_seconds=20.0,
            ),
            self._chunk(
                quote,
                chunk_index=8,
                start_seconds=10.0,
                end_seconds=39.8,
            ),
        ]

        result = resolve_inline_verbatim_anchors(
            text,
            chunks,
            _phrase(10.0, quote),
        )

        self.assertEqual(result.state, "resolved")
        self.assertEqual(result.text, "The motion passed [at 0:00:10].")
        self.assertEqual(len(result.aligned), 1)
        self.assertEqual(result.aligned[0]["matching_chunk_indices"], (7, 8))
        self.assertEqual(len(result.aligned[0]["matching_chunk_ranges"]), 2)
        self.assertEqual(len(result.aligned[0]["chunk_evidence"]), 2)
        self.assertTrue(
            all(
                "timings" not in row["alignment_evidence"]
                for row in result.aligned[0]["chunk_evidence"]
            )
        )


class WordPreciseAlignmentTests(unittest.TestCase):
    def test_four_observed_outcome_forms_snap_near_ground_truth(self):
        words = []
        words += _phrase(
            1_595.0,
            "we have decided to ask Vicki Zumwalt to fill this commission vacancy",
        )
        words += _phrase(
            2_350.0,
            "make a motion to approve resolution 5623 Bull Mountain grant application",
        )
        words += _phrase(
            4_526.0,
            "make a motion that we approve Kimley Horn 99775 traffic study agreement",
        )
        words += _phrase(
            6_283.0,
            "make a motion to approve police recruitment retention incentives 7500",
        )
        prose = (
            "1. <core>Approved the $99,775 Kimley Horn traffic study agreement</core> "
            "[at 1:13:36].\n\n"
            "2. <core>Approved police recruitment retention incentives of $7,500</core> "
            "[at 1:42:19].\n\n"
            "3. <core>Approved Resolution 5623 for the Bull Mountain grant</core> "
            "[at 0:37:10].\n\n"
            "4. <core>Appointed Vicki Zumwalt to the commission vacancy</core> "
            "[at 0:13:32]."
        )
        chunks = [
            (812.5, 1_638.7),
            (2_114.4, 2_550.0),
            (4_416.9, 4_552.0),
            (6_139.8, 6_534.4),
        ]

        report = align_decision_citations(prose, words, chunks)

        self.assertEqual(report.failures, [])
        self.assertEqual(report.aligned_indices, [1, 2, 3, 4])
        self.assertTrue(
            all(item["lower_confidence"] for item in report.per_decision)
        )
        aligned = [
            citation.total_seconds for citation in parse_citations(report.text)
        ]
        self.assertEqual(aligned, [4_526, 6_283, 2_350, 1_595])
        self.assertEqual(
            validate_inline_citations(report.text, chunks).state,
            "valid",
        )

    def test_no_outcome_signature_fails_without_guessing(self):
        prose = "1. Approved the road contract [at 0:01:40]."
        words = _phrase(100.0, "staff described the road contract and costs")

        report = align_decision_citations(prose, words, [(90.0, 140.0)])

        self.assertEqual(report.aligned_indices, [])
        self.assertEqual(report.failures[0]["reason"], "no_unambiguous_outcome_match")
        self.assertEqual(report.text, "")

    def test_item_anchor_prevents_preceding_vote_off_by_one(self):
        words = []
        words += _phrase(
            100.0,
            "make a motion to accept line item four as stated motion carries",
        )
        words += _phrase(
            110.0,
            "item five item number five discussion of possible action to approve "
            "the River Fund Transportation Assistant Grant with River Fund Inc",
        )
        words += _phrase(
            140.0,
            "I'd like to make a motion to accept line item 5 as stated",
        )
        words += _phrase(146.0, "please cast your votes motion carries")
        prose = (
            "1. <core>Approved the River Fund transportation grant</core> "
            "for **$50,000** [at 0:01:52]."
        )
        anchors = [
            {
                "index": 1,
                "item_quote": (
                    "item five item number five discussion of possible action to "
                    "approve the River Fund Transportation Assistant Grant with "
                    "River Fund Inc"
                ),
                # Number-word normalization must match Whisper's digit form.
                "action_quote": (
                    "I'd like to make a motion to accept line item five as stated"
                ),
            }
        ]

        report = align_decision_citations(
            prose,
            words,
            [(95.0, 155.0)],
            anchors=anchors,
        )

        self.assertEqual(report.failures, [])
        self.assertEqual(parse_citations(report.text)[0].total_seconds, 140)
        self.assertEqual(report.per_decision[0]["source"], "two_part_quote")
        self.assertGreaterEqual(
            report.per_decision[0]["action_evidence"]["coverage"],
            0.99,
        )

    def test_repeated_action_resolves_nearest_coarse_locator(self):
        words = []
        words += _phrase(
            200.0,
            "item seven discussion of possible action to approve the street lights",
        )
        repeated = "I'd like to make a motion to accept the line item as stated"
        words += _phrase(230.0, repeated)
        words += _phrase(260.0, repeated)
        prose = "1. Approved the street lights [at 0:03:22]."
        anchors = [
            {
                "index": 1,
                "item_quote": (
                    "item seven discussion of possible action to approve the street lights"
                ),
                "action_quote": repeated,
            }
        ]

        report = align_decision_citations(
            prose,
            words,
            [(195.0, 280.0)],
            anchors=anchors,
        )

        self.assertEqual(report.failures, [])
        self.assertEqual(parse_citations(report.text)[0].total_seconds, 230)
        action_evidence = report.per_decision[0]["action_evidence"]
        self.assertEqual(action_evidence["uniqueness"], "resolved_by_nearest")
        self.assertEqual(len(action_evidence["comparable_matches"]), 2)

    def test_two_part_action_anchor_refuses_quarantined_occurrence(self):
        item_quote = "item seven consideration of the water contract"
        action_quote = "motion to approve the water contract as presented"
        words = _phrase(80.0, item_quote) + _phrase(100.0, action_quote)
        for word in words[len(item_quote.split()):]:
            word["quarantine"] = {
                "reason": "degenerate_repetition",
                "detector_version": "degenerate-repetition-v1",
                "span_id": 0,
            }

        report = align_decision_citations(
            "1. Approved the water contract [at 0:01:40].",
            words,
            [(70.0, 120.0)],
            anchors=[{
                "index": 1,
                "item_quote": item_quote,
                "action_quote": action_quote,
            }],
        )

        self.assertEqual(report.text, "")
        self.assertEqual(report.aligned_indices, [])
        self.assertEqual(
            report.failures[0]["reason"],
            "action_quote_match_in_quarantined_span",
        )

    def test_globally_unique_action_outside_window_is_accepted_and_audited(self):
        item_quote = (
            "item twelve consideration of the Vicki Zumwalt partial term appointment"
        )
        action_quote = (
            "tonight we have decided decided to ask Vicki Zumwalt to fill this "
            "partial term appointment which will start July 2026 this month and "
            "end in December 2020 wait 28"
        )
        words = _phrase(1_400.0, item_quote) + _phrase(1_596.04, action_quote)
        prose = "1. Appointed Vicki Zumwalt [at 0:13:32]."

        with self.assertLogs(
            "zspan_pipeline.citation_validator",
            level="WARNING",
        ):
            report = align_decision_citations(
                prose,
                words,
                [(800.0, 1_610.0)],
                anchors=[
                    {
                        "index": 1,
                        "item_quote": item_quote,
                        "action_quote": action_quote,
                    }
                ],
            )

        self.assertEqual(report.failures, [])
        self.assertEqual(parse_citations(report.text)[0].total_seconds, 1_596)
        evidence = report.per_decision[0]
        self.assertEqual(evidence["confidence"], "high")
        self.assertEqual(
            evidence["action_evidence"]["reason"],
            "aligned_unique_outside_window",
        )
        self.assertEqual(
            evidence["action_evidence"]["uniqueness"],
            "unique_outside_window",
        )
        self.assertEqual(evidence["action_evidence"]["candidate_count"], 1)
        self.assertEqual(
            evidence["locator_disagreement"],
            {
                "reason": "globally_unique_action_quote_outside_locator_window",
                "coarse_locator_seconds": 812.0,
                "quote_start_seconds": 1_596.04,
                "distance_seconds": 784.04,
                "window_seconds": 360.0,
                "coverage_floor": 0.90,
                "coverage": 1.0,
                "direct_matches": len(action_quote.split()),
                "quote_tokens": len(action_quote.split()),
            },
        )

    def test_comparable_global_action_keeps_window_selection(self):
        item_quote = "item seven consideration of the street lighting contract"
        action_quote = "motion to approve the street lighting contract as presented"
        words = []
        words += _phrase(300.0, item_quote)
        words += _phrase(330.0, action_quote)
        words += _phrase(1_000.0, action_quote)
        prose = "1. Approved the street lighting contract [at 0:05:40]."

        report = align_decision_citations(
            prose,
            words,
            [(290.0, 1_010.0)],
            anchors=[
                {
                    "index": 1,
                    "item_quote": item_quote,
                    "action_quote": action_quote,
                }
            ],
        )

        self.assertEqual(report.failures, [])
        self.assertEqual(parse_citations(report.text)[0].total_seconds, 330)
        evidence = report.per_decision[0]
        comparable = evidence["action_evidence"]["comparable_matches"]
        self.assertEqual(len(comparable), 2)
        self.assertEqual(
            [match["in_window"] for match in comparable],
            [True, False],
        )
        self.assertEqual(
            evidence["action_evidence"]["uniqueness"],
            "resolved_by_nearest",
        )
        self.assertNotIn("locator_disagreement", evidence)

    def test_low_coverage_unique_action_outside_window_fails_closed(self):
        item_quote = "agenda item twelve staff presentation and council deliberation"
        action_quote = (
            "motion to approve the regional wastewater treatment plant engineering "
            "services contract"
        )
        partial_action = (
            "motion to approve the wastewater treatment plant engineering contract"
        )
        words = _phrase(900.0, item_quote) + _phrase(1_000.0, partial_action)
        prose = "1. Approved the engineering contract [at 0:01:40]."

        report = align_decision_citations(
            prose,
            words,
            [(90.0, 1_010.0)],
            anchors=[
                {
                    "index": 1,
                    "item_quote": item_quote,
                    "action_quote": action_quote,
                }
            ],
        )

        self.assertEqual(report.aligned_indices, [])
        self.assertEqual(
            report.failures[0]["reason"],
            "action_quote_no_match_in_window",
        )
        action_evidence = report.failures[0]["action_evidence"]
        self.assertEqual(action_evidence["candidate_count"], 1)
        self.assertGreaterEqual(action_evidence["coverage"], 0.75)
        self.assertLess(action_evidence["coverage"], 0.90)
        self.assertIsNone(action_evidence["start_seconds"])

    def test_supplied_unalignable_quote_never_uses_signature_fallback(self):
        words = []
        words += _phrase(
            300.0,
            "item eight discussion of possible action to approve the water contract",
        )
        words += _phrase(
            330.0,
            "make a motion to approve the water contract motion carries",
        )
        prose = "1. Approved the water contract [at 0:05:02]."
        anchors = [
            {
                "index": 1,
                "item_quote": (
                    "item eight discussion of possible action to approve the water contract"
                ),
                "action_quote": "words that were never spoken at this meeting",
            }
        ]

        report = align_decision_citations(
            prose,
            words,
            [(295.0, 345.0)],
            anchors=anchors,
        )

        self.assertEqual(report.aligned_indices, [])
        self.assertTrue(report.failures[0]["reason"].startswith("action_quote_"))
        self.assertNotIn("fallback", report.failures[0])

    def test_kingman_item_introduction_thirty_minutes_before_action_aligns(self):
        item_quote = (
            "item twelve public hearing and consideration of the Kingman "
            "crossing infrastructure agreement"
        )
        action_quote = (
            "I move to approve the Kingman crossing infrastructure agreement"
        )
        words = _phrase(600.0, item_quote) + _phrase(2_400.0, action_quote)
        prose = "1. Approved the Kingman crossing agreement [at 0:39:55]."

        report = align_decision_citations(
            prose,
            words,
            [(590.0, 2_410.0)],
            anchors=[
                {
                    "index": 1,
                    "item_quote": item_quote,
                    "action_quote": action_quote,
                }
            ],
        )

        self.assertEqual(report.failures, [])
        self.assertEqual(parse_citations(report.text)[0].total_seconds, 2_400)
        evidence = report.per_decision[0]
        self.assertEqual(evidence["item_evidence"]["start_seconds"], 600.0)
        self.assertEqual(
            evidence["item_evidence"]["window_end_seconds"],
            2_400.0,
        )

    def test_agenda_readthrough_does_not_bound_later_actions(self):
        item_one = (
            "item one clerk reads the airport lease amendment into the agenda"
        )
        item_two = "item two consideration of the neighborhood drainage contract"
        action_one = "motion to approve the airport lease amendment as presented"
        action_two = "motion to award the neighborhood drainage contract"
        words = []
        words += _phrase(100.0, item_one)
        words += _phrase(300.0, item_two)
        words += _phrase(1_000.0, action_two)
        words += _phrase(2_000.0, action_one)
        prose = (
            "1. Approved the airport lease amendment [at 0:33:18].\n\n"
            "2. Awarded the drainage contract [at 0:16:38]."
        )
        anchors = [
            {"index": 1, "item_quote": item_one, "action_quote": action_one},
            {"index": 2, "item_quote": item_two, "action_quote": action_two},
        ]

        report = align_decision_citations(
            prose,
            words,
            [(90.0, 2_010.0)],
            anchors=anchors,
        )

        self.assertEqual(report.failures, [])
        self.assertEqual(report.aligned_indices, [1, 2])
        self.assertEqual(
            [citation.total_seconds for citation in parse_citations(report.text)],
            [2_000, 1_000],
        )
        self.assertEqual(
            report.per_decision[0]["audit_conflicts"][0]["reason"],
            "action_at_or_after_next_item_anchor",
        )
        self.assertNotIn("audit_conflicts", report.per_decision[1])

    def test_non_chronological_numbered_decisions_all_align(self):
        early_item = "item four discussion of the library roof repair"
        early_action = "motion to approve the library roof repair"
        late_item = "item nine discussion of the wastewater pump replacement"
        late_action = "motion to approve the wastewater pump replacement"
        words = []
        words += _phrase(400.0, early_item)
        words += _phrase(450.0, early_action)
        words += _phrase(1_800.0, late_item)
        words += _phrase(1_850.0, late_action)
        prose = (
            "1. Approved the wastewater pump replacement [at 0:30:48].\n\n"
            "2. Approved the library roof repair [at 0:07:28]."
        )
        anchors = [
            {"index": 1, "item_quote": late_item, "action_quote": late_action},
            {"index": 2, "item_quote": early_item, "action_quote": early_action},
        ]

        report = align_decision_citations(
            prose,
            words,
            [(390.0, 1_860.0)],
            anchors=anchors,
        )

        self.assertEqual(report.failures, [])
        self.assertEqual(report.aligned_indices, [1, 2])
        self.assertEqual(
            [citation.total_seconds for citation in parse_citations(report.text)],
            [1_850, 450],
        )

    def test_duplicate_action_occurrence_fails_both_decisions_closed(self):
        item_one = "airport hangar lease renewal consideration"
        item_two = "downtown facade grant award consideration"
        shared_action = "motion to approve the consent item as presented"
        words = []
        words += _phrase(100.0, item_one)
        words += _phrase(120.0, item_two)
        words += _phrase(200.0, shared_action)
        prose = (
            "1. Approved the hangar lease [at 0:03:18].\n\n"
            "2. Approved the facade grant [at 0:03:22]."
        )
        anchors = [
            {"index": 1, "item_quote": item_one, "action_quote": shared_action},
            {"index": 2, "item_quote": item_two, "action_quote": shared_action},
        ]

        report = align_decision_citations(
            prose,
            words,
            [(90.0, 210.0)],
            anchors=anchors,
        )

        self.assertEqual(report.aligned_indices, [])
        self.assertEqual(report.text, "")
        self.assertEqual(
            [failure["reason"] for failure in report.failures],
            ["duplicate_action_occurrence", "duplicate_action_occurrence"],
        )
        self.assertEqual(report.failures[0]["conflicting_indices"], [1, 2])
        self.assertEqual(
            report.failures[0]["action_word_index"],
            report.failures[1]["action_word_index"],
        )

    def test_absent_and_out_of_window_quotes_have_distinct_honest_evidence(self):
        spoken = "motion to approve the regional transit agreement"
        words = _phrase(100.0, spoken)

        absent = align_quote_with_evidence(
            "words genuinely absent from this transcript",
            words,
            window_start_seconds=400.0,
            window_end_seconds=500.0,
            selection="nearest",
            reference_seconds=450.0,
        )
        outside = align_quote_with_evidence(
            spoken,
            words,
            window_start_seconds=400.0,
            window_end_seconds=500.0,
            selection="nearest",
            reference_seconds=450.0,
        )

        self.assertEqual(absent.reason, "no_direct_match")
        self.assertEqual(absent.candidate_count, 0)
        self.assertEqual(absent.direct_matches, 0)
        self.assertEqual(absent.coverage, 0.0)
        self.assertIsNone(absent.best_candidate_start_seconds)
        self.assertIsNone(absent.best_candidate_end_seconds)
        self.assertEqual(absent.best_candidate_direct_matches, 0)
        self.assertEqual(absent.best_candidate_coverage, 0.0)
        self.assertIsNone(absent.best_candidate_distance_seconds)

        self.assertEqual(outside.reason, "no_match_in_window")
        self.assertGreater(outside.candidate_count, 0)
        self.assertEqual(outside.in_window_candidate_count, 0)
        self.assertEqual(outside.direct_matches, len(spoken.split()))
        self.assertEqual(outside.coverage, 1.0)
        self.assertEqual(outside.best_candidate_start_seconds, 100.0)
        self.assertEqual(outside.best_candidate_end_seconds, 102.7)
        self.assertEqual(
            outside.best_candidate_direct_matches,
            len(spoken.split()),
        )
        self.assertEqual(outside.best_candidate_coverage, 1.0)
        self.assertEqual(outside.best_candidate_distance_seconds, 350.0)
        self.assertTrue(outside.comparable_matches)
        self.assertIsNone(outside.start_seconds)


if __name__ == "__main__":
    unittest.main()
