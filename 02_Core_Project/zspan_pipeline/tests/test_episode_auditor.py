"""Offline tests for the audit-only episode observer."""
from __future__ import annotations

import atexit
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_IMPORT_TEMP_DIR = tempfile.TemporaryDirectory()
atexit.register(_IMPORT_TEMP_DIR.cleanup)
_IMPORT_DB_PATH = Path(_IMPORT_TEMP_DIR.name) / "import-isolation.db"
with mock.patch.dict(os.environ, {"ZSPAN_DB_PATH": str(_IMPORT_DB_PATH)}):
    from zspan_pipeline import episode_auditor as auditor
    from zspan_pipeline import qdrant_synthesizer
    import database


def _word(word: str, start: float, end: float | None = None) -> dict:
    return {
        "word": word,
        "start": start,
        "end": start + 0.5 if end is None else end,
    }


def _inputs(
    *,
    outputs: dict[str, str] | None = None,
    words: tuple[dict, ...] | None = None,
) -> auditor.AuditInputs:
    output_map = outputs or {"synopsis": "A supported synopsis."}
    transcript_words = words or (
        _word("water", 0.0),
        _word("contract", 1.0),
        _word("passed", 2.0, 3.0),
    )
    return auditor.AuditInputs(
        meeting_id=7,
        meeting={
            "city_name": "Mesa",
            "meeting_title": "City Council",
            "meeting_date": "2026-07-28",
        },
        outputs=output_map,
        output_row_ids={name: index for index, name in enumerate(output_map, 1)},
        missing_outputs=tuple(
            name for name in auditor.OUTPUT_ORDER if name not in output_map
        ),
        transcript_words=transcript_words,
        outputs_snapshot_hash=auditor.compute_outputs_snapshot_hash(output_map),
    )


def _deterministic_empty() -> dict:
    return {
        "entropy": {"status": "completed"},
        "entity_consistency": {
            "status": "completed",
            "variant_collisions": [],
        },
        "locator_existence": {"status": "completed", "out_of_range": []},
        "quote_existence": {
            "status": "completed",
            "llm_evidence_not_found": [],
        },
        "provenance": {"status": "uncheckable"},
        "valid_empty": {"status": "completed", "valid_empty": []},
    }


def _synopsis_with_anchor_audit(
    resolution_state: str,
    *,
    anchors_total: int,
    failures_count: int,
) -> str:
    aligned_count = anchors_total - failures_count
    if aligned_count < 0:
        raise ValueError("failures_count cannot exceed anchors_total")
    payload = {
        "resolution_state": resolution_state,
        "anchors_total": anchors_total,
        "aligned": [
            {"ordinal": index}
            for index in range(1, aligned_count + 1)
        ],
        "failures": [
            {"reason": f"failure-{index}"}
            for index in range(failures_count)
        ],
    }
    return (
        "A supported synopsis.\n\n"
        "<!-- synopsis_anchor_audit v1\n"
        f"{json.dumps(payload)}\n"
        "audit -->"
    )


class PromptAndRenderingTests(unittest.TestCase):
    def test_prompt_contains_validated_template_outputs_and_order(self):
        outputs = {
            "community_calls_to_action": "calls",
            "synopsis": "summary",
            "episode_tagline": "tag",
            "key_decisions": "decisions",
        }
        prompt = auditor.build_audit_prompt(
            {
                "city_name": "Mesa",
                "meeting_title": "Regular Meeting",
                "meeting_date": "2026-07-28",
            },
            outputs,
            ["[0:00:00] Good evening"],
        )
        self.assertIn(
            "Your mission: protect the integrity of a public civic record.",
            auditor.AUDIT_PROMPT_TEMPLATE,
        )
        self.assertIn("=== OUTPUTS UNDER AUDIT ===", prompt)
        self.assertIn("[0:00:00] Good evening", prompt)
        positions = [
            prompt.index(f"--- OUTPUT: {name} ---")
            for name in (
                "episode_tagline",
                "synopsis",
                "key_decisions",
                "community_calls_to_action",
            )
        ]
        self.assertEqual(positions, sorted(positions))

    def test_prompt_contains_proposed_fix_addendum_and_v2_version(self):
        self.assertIn(
            "The before text must be an exact copy from the output under audit "
            "— never paraphrase, trim punctuation, or normalize it.",
            auditor.AUDIT_PROMPT_TEMPLATE,
        )
        self.assertEqual(auditor.AUDITOR_VERSION, "episode-auditor-v2")

    def test_timecode_renderer_buckets_and_formats_over_one_hour(self):
        lines = auditor.render_timecoded_transcript(
            [
                _word("zero", 0.2),
                _word("same", 14.9),
                _word("next", 15.0),
                _word("hour", 3605.0),
            ]
        )
        self.assertEqual(lines[0], "[0:00:00] zero same")
        self.assertEqual(lines[1], "[0:00:15] next")
        self.assertEqual(lines[2], "[1:00:00] hour")


class ResponseParsingTests(unittest.TestCase):
    def test_response_parser_counts_all_sections(self):
        response = """## FINDINGS
1. CONFIRMED: first.
2) SUSPECTED: second.
## OPEN_FINDINGS
1. One open item.
## SUGGESTIONS
1. One suggestion.
## VERDICT — publishable-with-noted-flags
"""
        parsed = auditor.parse_audit_response(response)
        self.assertEqual(parsed["run_status"], "complete")
        self.assertEqual(parsed["verdict"], "flags")
        self.assertEqual(len(parsed["findings"]), 2)
        self.assertEqual(len(parsed["open_findings"]), 1)
        self.assertEqual(len(parsed["suggestions"]), 1)
        self.assertEqual(
            parsed["verdict_line"], "publishable-with-noted-flags"
        )

    def test_response_parser_counts_validated_prototype_format(self):
        response = """## FINDINGS
**Finding 1** — Families 1 + 5 — CONFIRMED
The output says Copperfield approved a glass orchard, but the record says it
only scheduled a workshop.

---

**Finding 2** — Family 2 — SUSPECTED
A time marker points to discussion of a painted footbridge instead of the
claimed lantern contract.

---

**Finding 3** — Family 3 — CONFIRMED
The summary identifies a private resident by a fictional membership number.

---

**Finding 4** — Family 4 — CONFIRMED
A repeated phrase about clockwork fountains appears as a settled fact.

---

**Finding 5** — Family 6 — SUSPECTED
The newsletter calls the imaginary pebble-road vote reckless.

## OPEN_FINDINGS
**O1 — The invented harbor district is described inconsistently.**

**O2 — A fictional exhibit label lacks source context.**

**O3 — The synthetic budget unit may confuse readers.**

## SUGGESTIONS
**1. Anchor imaginary intersection names to their source passages.**

**2. Keep fictional vote totals consistent across outputs.**

**3. Explain when a synthetic record is incomplete.**

**4. Separate invented staff remarks from public comment.**

**5. Preserve certainty labels in the review display.**

**6. Add a check for contradictory fantasy-currency amounts.**

## VERDICT
**Publishable-with-noted-flags.**
"""
        parsed = auditor.parse_audit_response(response)
        self.assertEqual(parsed["run_status"], "complete")
        self.assertEqual(parsed["verdict"], "flags")
        self.assertEqual(len(parsed["findings"]), 5)
        self.assertEqual(len(parsed["open_findings"]), 3)
        self.assertEqual(len(parsed["suggestions"]), 6)
        self.assertTrue(parsed["verdict_line"])

    def test_nonempty_unstructured_findings_use_hard_floor(self):
        response = """## FINDINGS
The fictional moon-garden summary conflicts with the synthetic meeting record,
but the response did not format this observation as a numbered item.
## OPEN_FINDINGS
none
## SUGGESTIONS
none
## VERDICT
**Publishable-with-noted-flags.**
"""
        parsed = auditor.parse_audit_response(response)
        self.assertEqual(parsed["run_status"], "complete")
        self.assertEqual(parsed["verdict"], "flags")
        self.assertEqual(len(parsed["findings"]), 1)
        self.assertIn("moon-garden", parsed["findings"][0])

    def test_garbage_response_is_incomplete_parse_failure(self):
        parsed = auditor.parse_audit_response("unstructured model chatter")
        self.assertEqual(parsed["run_status"], "parse_failed")
        self.assertEqual(parsed["verdict"], "incomplete")
        self.assertNotEqual(parsed["verdict"], "no_catches")

    def test_two_proposed_fixes_and_no_safe_proposal_are_parsed(self):
        response = """## FINDINGS
1. CONFIRMED: Two localized corrections are supported.
**PROPOSED_FIX**
target_output: newsletter
before: <<<The vote was 9-0.>>>
after: <<<The vote was 8-1.>>>
fix_rationale: Correct the roll-call tally.
PROPOSED_FIX
target_output: synopsis
before: <<<The motion failed.>>>
after: <<<The motion passed.>>>
fix_rationale: Correct the outcome.
2. CONFIRMED: The item requires restructuring.
**NO_SAFE_PROPOSAL** — Removing the whole item requires operator judgment.
## OPEN_FINDINGS
none
## SUGGESTIONS
none
## VERDICT
needs-human-review
"""
        parsed = auditor.parse_audit_response(response)
        self.assertEqual(parsed["run_status"], "complete")
        self.assertEqual(
            [proposal["id"] for proposal in parsed["proposals"]],
            ["p1.1", "p1.2"],
        )
        first = parsed["proposals"][0]
        self.assertEqual(first["finding_number"], 1)
        self.assertEqual(first["target_output"], "newsletter")
        self.assertEqual(first["before"], "The vote was 9-0.")
        self.assertEqual(first["after"], "The vote was 8-1.")
        self.assertEqual(
            first["fix_rationale"],
            "Correct the roll-call tally.",
        )
        self.assertTrue(first["delimiters_ok"])
        self.assertTrue(first["parse_ok"])
        self.assertEqual(
            parsed["no_safe_proposals"],
            [
                {
                    "finding_number": 2,
                    "reason": (
                        "Removing the whole item requires operator judgment."
                    ),
                }
            ],
        )

    def test_malformed_proposed_fix_is_recorded_without_parse_failure(self):
        response = """FINDINGS
1. CONFIRMED: A correction is needed.
PROPOSED_FIX
target_output: newsletter
before: the current text
fix_rationale: The replacement was omitted.
OPEN_FINDINGS
none
SUGGESTIONS
none
VERDICT
needs-human-review
"""
        parsed = auditor.parse_audit_response(response)
        self.assertEqual(parsed["run_status"], "complete")
        self.assertEqual(len(parsed["proposals"]), 1)
        proposal = parsed["proposals"][0]
        self.assertFalse(proposal["parse_ok"])
        self.assertFalse(proposal["delimiters_ok"])
        self.assertIn("PROPOSED_FIX", proposal["raw_block"])

    def test_proposed_fix_without_delimiters_uses_multiline_fallback(self):
        response = """FINDINGS
1. CONFIRMED: A correction is needed.
PROPOSED_FIX
target_output: newsletter
before: The current first line
continues here.
after: The corrected first line
continues here.
fix_rationale: Correct the supported wording.
OPEN_FINDINGS
none
SUGGESTIONS
none
VERDICT
needs-human-review
"""
        parsed = auditor.parse_audit_response(response)
        proposal = parsed["proposals"][0]
        self.assertTrue(proposal["parse_ok"])
        self.assertFalse(proposal["delimiters_ok"])
        self.assertEqual(
            proposal["before"],
            "The current first line\ncontinues here.",
        )
        self.assertEqual(
            proposal["after"],
            "The corrected first line\ncontinues here.",
        )


class DeterministicWrapperTests(unittest.TestCase):
    def _run_with_synopsis(self, synopsis: str) -> dict:
        inputs = _inputs(outputs={"synopsis": synopsis})
        with (
            mock.patch.object(auditor, "_load_roster", return_value=[]),
            mock.patch.object(
                auditor,
                "_load_local_chunk_starts",
                return_value=([], ""),
            ),
        ):
            return auditor.run_deterministic_wrapper(
                inputs,
                [],
                preview_dir=Path(_IMPORT_TEMP_DIR.name),
            )

    def test_deterministic_flag_counts_synopsis_anchor_degraded_state(self):
        result = self._run_with_synopsis(
            _synopsis_with_anchor_audit(
                "degraded",
                anchors_total=8,
                failures_count=3,
            )
        )

        synopsis_anchor = result["synopsis_anchor"]
        self.assertEqual(synopsis_anchor["resolution_state"], "degraded")
        self.assertEqual(len(synopsis_anchor["flags"]), 1)
        flag = synopsis_anchor["flags"][0]
        self.assertEqual(flag["reason"], "synopsis_anchor_failure:degraded")
        self.assertEqual(flag["anchors_total"], 8)
        self.assertEqual(flag["failures_count"], 3)
        self.assertEqual(auditor.count_deterministic_flags(result), 1)

    def test_deterministic_flag_counts_synopsis_anchor_nonconforming_state(self):
        result = self._run_with_synopsis(
            _synopsis_with_anchor_audit(
                "nonconforming",
                anchors_total=1,
                failures_count=1,
            )
        )

        synopsis_anchor = result["synopsis_anchor"]
        self.assertEqual(len(synopsis_anchor["flags"]), 1)
        self.assertEqual(
            synopsis_anchor["flags"][0]["reason"],
            "synopsis_anchor_failure:nonconforming",
        )
        self.assertEqual(auditor.count_deterministic_flags(result), 1)

    def test_deterministic_flag_zero_for_resolved_synopsis(self):
        result = self._run_with_synopsis(
            _synopsis_with_anchor_audit(
                "resolved",
                anchors_total=3,
                failures_count=0,
            )
        )

        synopsis_anchor = result["synopsis_anchor"]
        self.assertEqual(synopsis_anchor["resolution_state"], "resolved")
        self.assertEqual(synopsis_anchor["flags"], [])
        self.assertEqual(auditor.count_deterministic_flags(result), 0)

    def test_deterministic_flag_zero_for_synopsis_without_audit_block(self):
        result = self._run_with_synopsis("A legacy synopsis.")

        synopsis_anchor = result["synopsis_anchor"]
        self.assertEqual(synopsis_anchor["status"], "not_applicable")
        self.assertEqual(synopsis_anchor["resolution_state"], "absent")
        self.assertEqual(synopsis_anchor["flags"], [])
        self.assertEqual(auditor.count_deterministic_flags(result), 0)

    def test_synopsis_anchor_audit_metadata_is_not_scanned_as_public_prose(self):
        payload = {
            "resolution_state": "resolved",
            "anchors_total": 1,
            "aligned": [
                {
                    "quote": "Jame Smith approved the contract",
                    "canonical_citation": "[at 0:10:00]",
                }
            ],
            "failures": [],
        }
        synopsis = (
            "James Smith approved the contract [at 0:00:01].\n\n"
            "<!-- synopsis_anchor_audit v1\n"
            f"{json.dumps(payload)}\n"
            "audit -->"
        )

        result = self._run_with_synopsis(synopsis)

        self.assertEqual(
            result["entity_consistency"]["variant_collisions"],
            [],
        )
        self.assertEqual(result["locator_existence"]["out_of_range"], [])
        self.assertEqual(result["synopsis_anchor"]["flags"], [])

    def test_synopsis_anchor_audit_marker_inside_quote_does_not_shadow_block(self):
        payload = {
            "resolution_state": "resolved",
            "anchors_total": 1,
            "aligned": [
                {"quote": "literal <!-- synopsis_anchor_audit v1 marker"}
            ],
            "failures": [],
        }
        synopsis = (
            "Supported synopsis.\n\n"
            "<!-- synopsis_anchor_audit v1\n"
            f"{json.dumps(payload, indent=2)}\n"
            "audit -->"
        )

        result = auditor.check_synopsis_anchor_audit({"synopsis": synopsis})

        self.assertEqual(result["resolution_state"], "resolved")
        self.assertEqual(result["flags"], [])

    def test_synopsis_anchor_audit_inconsistent_counts_is_uncheckable(self):
        payload = {
            "resolution_state": "resolved",
            "anchors_total": 3,
            "aligned": [{"ordinal": 1}],
            "failures": [],
        }
        synopsis = (
            "Supported synopsis.\n\n"
            "<!-- synopsis_anchor_audit v1\n"
            f"{json.dumps(payload)}\n"
            "audit -->"
        )

        result = auditor.check_synopsis_anchor_audit({"synopsis": synopsis})

        self.assertEqual(result["resolution_state"], "uncheckable")
        self.assertEqual(
            result["flags"][0]["reason"],
            "synopsis_anchor_failure:uncheckable",
        )

    def test_entity_variant_collision_is_flagged(self):
        outputs = {
            "synopsis": "Update: Vicki Zumwalt addressed the item.",
            "newsletter": "Update: Vicky Zumwalt addressed the item.",
        }
        with mock.patch.object(auditor, "_load_roster", return_value=[]):
            result = auditor.check_entity_consistency(outputs, "Mesa")
        pairs = [
            set(collision["spellings"])
            for collision in result["variant_collisions"]
        ]
        self.assertIn({"Vicki Zumwalt", "Vicky Zumwalt"}, pairs)
        collision = next(
            collision
            for collision in result["variant_collisions"]
            if set(collision["spellings"])
            == {"Vicki Zumwalt", "Vicky Zumwalt"}
        )
        self.assertEqual(collision["kind"], "FLAG")

    def test_single_token_variant_is_observation_not_flag(self):
        outputs = {
            "synopsis": "Staff member Ben spoke.",
            "newsletter": "Staff member Ken spoke.",
        }
        with mock.patch.object(auditor, "_load_roster", return_value=[]):
            result = auditor.check_entity_consistency(outputs, "Mesa")

        self.assertEqual(result["variant_collisions"], [])
        self.assertEqual(len(result["single_token_variants"]), 1)
        observation = result["single_token_variants"][0]
        self.assertEqual(set(observation["spellings"]), {"Ben", "Ken"})
        self.assertEqual(observation["kind"], "OBSERVATION")
        self.assertEqual(
            observation["classification"],
            "single_token_variant",
        )
        self.assertEqual(
            auditor.count_deterministic_flags(
                {"entity_consistency": result}
            ),
            0,
        )

    def test_locator_existence_flags_only_out_of_range(self):
        with mock.patch.object(
            auditor,
            "_load_local_chunk_starts",
            return_value=([], ""),
        ):
            result = auditor.check_locator_existence(
                7,
                {
                    "synopsis": (
                        "Supported [at 0:00:05]. "
                        "Impossible [at 0:10:00]."
                    )
                },
                [_word("end", 59.0, 60.0)],
            )
        self.assertEqual(result["citations_checked"], 2)
        self.assertEqual(len(result["out_of_range"]), 1)
        self.assertEqual(result["out_of_range"][0]["seconds"], 600)

    def test_quote_existence_matches_punctuated_transcript_evidence(self):
        words = (
            _word("thats", 0.0),
            _word("going", 1.0),
            _word("to", 2.0),
            _word("complete", 3.0),
            _word("a", 4.0),
            _word("DCR", 5.0),
        )
        result = auditor.check_quote_existence(
            findings=[
                'Transcript evidence: "That\'s going to complete a DCR."',
            ],
            transcript_words=words,
            outputs={},
        )
        self.assertEqual(len(result["quotes_checked"]), 1)
        self.assertEqual(
            result["quotes_checked"][0]["matched_in"],
            "transcript",
        )
        self.assertEqual(result["llm_evidence_not_found"], [])

    def test_quote_existence_matches_output_text(self):
        result = auditor.check_quote_existence(
            findings=[
                'Newsletter text: “release of funds is in progress.”',
            ],
            transcript_words=(_word("unrelated", 0.0),),
            outputs={"newsletter": "Release of funds is in progress."},
        )
        self.assertEqual(len(result["quotes_checked"]), 1)
        self.assertEqual(result["quotes_checked"][0]["matched_in"], "outputs")
        self.assertEqual(result["llm_evidence_not_found"], [])

    def test_quote_existence_does_not_extract_bold_labels(self):
        result = auditor.check_quote_existence(
            findings=[
                "**Ground truth** identifies a mismatch in the record.",
            ],
            transcript_words=(_word("record", 0.0),),
            outputs={},
        )
        self.assertEqual(result["quotes_checked"], [])
        self.assertEqual(result["llm_evidence_not_found"], [])

    def test_quote_existence_matches_elided_quote_fragments(self):
        words = tuple(
            _word(word, float(index))
            for index, word in enumerate(
                "the council approved the water contract after discussion "
                "the project will begin construction next month".split()
            )
        )
        result = auditor.check_quote_existence(
            findings=[
                '"the council approved the water contract ... '
                'the project will begin construction next month"',
            ],
            transcript_words=words,
            outputs={},
        )
        self.assertEqual(len(result["quotes_checked"]), 1)
        self.assertEqual(
            result["quotes_checked"][0]["matched_in"],
            "transcript",
        )
        self.assertEqual(result["llm_evidence_not_found"], [])

    def test_quote_existence_flags_fully_fabricated_sentence(self):
        result = auditor.check_quote_existence(
            findings=[
                '"the airport bond failed by an overwhelming margin"',
            ],
            transcript_words=(_word("unrelated", 0.0),),
            outputs={"newsletter": "No airport financing was discussed."},
        )
        self.assertEqual(len(result["quotes_checked"]), 1)
        self.assertEqual(
            result["quotes_checked"][0]["matched_in"],
            "none",
        )
        self.assertEqual(len(result["llm_evidence_not_found"]), 1)
        self.assertEqual(
            result["llm_evidence_not_found"][0]["quote"],
            "the airport bond failed by an overwhelming margin",
        )

    def test_valid_empty_ccta_is_not_a_flag(self):
        result = auditor.check_valid_empty(
            {"community_calls_to_action": "[]"}
        )
        self.assertIn(
            "community_calls_to_action",
            result["valid_empty"],
        )
        self.assertEqual(result["flags_count"], 0)

    def test_absent_provenance_is_uncheckable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = auditor.check_provenance(
                7,
                preview_dir=Path(temp_dir),
            )
        self.assertEqual(result["status"], "uncheckable")
        self.assertEqual(result["reason"], "provenance_absent")

    def test_default_provenance_dir_is_synthesizer_preview_dir(self):
        self.assertEqual(
            auditor.PREVIEW_DIR,
            qdrant_synthesizer.PREVIEW_DIR,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            preview_dir = Path(temp_dir)
            path = preview_dir / "m7_synthesis_provenance.json"
            path.write_text(
                json.dumps(
                    {
                        "synopsis": {
                            "prompt_sha256": "abc",
                            "retrieved_chunk_ids": [3, 8],
                        }
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(auditor, "PREVIEW_DIR", preview_dir):
                result = auditor.check_provenance(7)

        self.assertEqual(result["status"], "recorded")
        self.assertEqual(
            result["outputs"]["synopsis"],
            {
                "prompt_sha256_present": True,
                "chunk_ids_present": True,
            },
        )

    def test_list_shaped_provenance_records_hash_and_chunks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "m7_synthesis_provenance.json"
            path.write_text(
                json.dumps(
                    {
                        "outputs": [
                            {
                                "output_type": "synopsis",
                                "prompt_sha256": "abc",
                                "chunk_ids": ["chunk-1"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            result = auditor.check_provenance(
                7,
                preview_dir=Path(temp_dir),
            )
        self.assertEqual(result["status"], "recorded")
        self.assertEqual(
            result["outputs"]["synopsis"],
            {
                "prompt_sha256_present": True,
                "chunk_ids_present": True,
            },
        )


class ProposalValidationTests(unittest.TestCase):
    @staticmethod
    def _proposal(
        *,
        target_output: str = "newsletter",
        before: str,
        after: str,
    ) -> dict:
        return {
            "id": "p1.1",
            "finding_number": 1,
            "target_output": target_output,
            "before": before,
            "after": after,
            "fix_rationale": "Evidence-supported correction.",
            "delimiters_ok": True,
            "parse_ok": True,
        }

    def _validate(
        self,
        proposal: dict,
        *,
        outputs: dict[str, str],
        words: tuple[dict, ...] | None = None,
    ) -> dict:
        inputs = _inputs(outputs=outputs, words=words)
        with mock.patch.object(auditor, "_load_roster", return_value=[]):
            return auditor.validate_proposals(
                [proposal],
                inputs,
                inputs.transcript_words,
            )[0]

    def test_happy_path_validates_and_records_every_check(self):
        before = "Council approved the water contract [at 0:00:01]."
        proposal = self._proposal(
            before=before,
            after="Council adopted the water contract [at 0:00:01].",
        )
        result = self._validate(
            proposal,
            outputs={"newsletter": before},
        )
        self.assertTrue(result["validated"])
        self.assertTrue(all(result["checks"].values()))
        self.assertEqual(
            set(result["checks"]),
            {
                "parse_ok",
                "target_known",
                "before_unique",
                "changed",
                "structure_ok",
                "citations_ok",
                "no_new_evidence_tokens",
                "delta_bounded",
                "no_new_flags",
            },
        )

    def test_duplicate_before_fails_uniqueness(self):
        proposal = self._proposal(before="same", after="corrected")
        result = self._validate(
            proposal,
            outputs={"newsletter": "same and same"},
        )
        self.assertFalse(result["validated"])
        self.assertFalse(result["checks"]["before_unique"])

    def test_unsupported_dollar_amount_fails_evidence_tokens(self):
        proposal = self._proposal(
            before="Council approved the plan.",
            after="Council approved the $900 plan.",
        )
        result = self._validate(
            proposal,
            outputs={"newsletter": "Council approved the plan."},
        )
        self.assertFalse(result["checks"]["no_new_evidence_tokens"])
        self.assertIn(
            "unsupported_token: $900",
            result["validation_errors"],
        )

    def test_dropped_citation_fails_citation_check(self):
        proposal = self._proposal(
            before="Council approved [at 0:00:01].",
            after="Council approved.",
        )
        result = self._validate(
            proposal,
            outputs={"newsletter": "Council approved [at 0:00:01]."},
        )
        self.assertFalse(result["checks"]["citations_ok"])
        self.assertIn("citation_lost", result["validation_errors"])

    def test_json_breakage_fails_structure_check(self):
        content = '[{"claim": "approved"}]'
        proposal = self._proposal(
            target_output="tracked_claims",
            before='"approved"',
            after='"approved}',
        )
        result = self._validate(
            proposal,
            outputs={"tracked_claims": content},
        )
        self.assertFalse(result["checks"]["structure_ok"])
        self.assertIn("structure_invalid", result["validation_errors"])

    def test_new_name_variant_fails_no_new_flags(self):
        proposal = self._proposal(
            before="Staff addressed the item.",
            after="Vicky Zumwalt addressed the item.",
        )
        words = (
            _word("Vicky", 0.0),
            _word("Zumwalt", 1.0),
            _word("addressed", 2.0, 3.0),
        )
        result = self._validate(
            proposal,
            outputs={
                "newsletter": "Staff addressed the item.",
                "synopsis": "Update: Vicki Zumwalt addressed the item.",
            },
            words=words,
        )
        self.assertFalse(result["checks"]["no_new_flags"])
        self.assertIn(
            "new_entity_variant_collision",
            result["validation_errors"],
        )

    def test_apply_gate_is_output_specific(self):
        proposals = [
            self._proposal(
                target_output="key_decisions",
                before="motion passed",
                after="motion was adopted",
            ),
            {
                **self._proposal(
                    before="vote passed",
                    after="vote was adopted",
                ),
                "id": "p2.1",
                "finding_number": 2,
            },
        ]
        inputs = _inputs(
            outputs={
                "key_decisions": "motion passed",
                "newsletter": "vote passed",
            }
        )
        with mock.patch.object(auditor, "_load_roster", return_value=[]):
            results = auditor.validate_proposals(
                proposals,
                inputs,
                inputs.transcript_words,
            )
        self.assertTrue(results[0]["apply_gated"])
        self.assertFalse(results[1]["apply_gated"])


class AuditExecutionTests(unittest.TestCase):
    def test_dry_run_never_calls_claude_or_persists(self):
        inputs = _inputs()
        with (
            mock.patch.object(
                auditor,
                "_load_meeting_outputs",
                return_value=(
                    inputs.meeting,
                    dict(inputs.outputs),
                    dict(inputs.output_row_ids),
                    inputs.missing_outputs,
                ),
            ),
            mock.patch.object(
                auditor.local_vector_store,
                "load_transcript_words",
                return_value={"words": list(inputs.transcript_words)},
            ),
            mock.patch.object(
                auditor,
                "run_deterministic_wrapper",
                return_value=_deterministic_empty(),
            ),
            mock.patch.object(
                auditor,
                "synthesize_via_claude_p",
            ) as synthesize,
            mock.patch.object(
                auditor,
                "save_episode_audit_run",
            ) as save_run,
        ):
            result = auditor.run_episode_audit(7, dry_run=True)
        self.assertEqual(result["run_status"], "dry_run")
        synthesize.assert_not_called()
        save_run.assert_not_called()

    def test_runtime_failure_is_incomplete_and_persisted(self):
        inputs = _inputs()
        clean_response = mock.Mock(side_effect=RuntimeError("claude unavailable"))
        with (
            mock.patch.object(
                auditor,
                "_load_meeting_outputs",
                return_value=(
                    inputs.meeting,
                    dict(inputs.outputs),
                    dict(inputs.output_row_ids),
                    inputs.missing_outputs,
                ),
            ),
            mock.patch.object(
                auditor.local_vector_store,
                "load_transcript_words",
                return_value={"words": list(inputs.transcript_words)},
            ),
            mock.patch.object(
                auditor,
                "synthesize_via_claude_p",
                clean_response,
            ),
            mock.patch.object(
                auditor,
                "run_deterministic_wrapper",
                return_value=_deterministic_empty(),
            ),
            mock.patch.object(
                auditor,
                "save_episode_audit_run",
            ) as save_run,
        ):
            result = auditor.run_episode_audit(7)
        self.assertEqual(result["run_status"], "runtime_failed")
        self.assertEqual(result["verdict"], "incomplete")
        self.assertNotEqual(result["verdict"], "no_catches")
        save_run.assert_called_once()
        self.assertEqual(
            save_run.call_args.kwargs["run_status"],
            "runtime_failed",
        )
        self.assertEqual(
            save_run.call_args.kwargs["verdict"],
            "incomplete",
        )

    def test_shared_call_uses_pinned_model_effort_and_no_timeout(self):
        inputs = _inputs()
        response = """FINDINGS — none
OPEN_FINDINGS — none
SUGGESTIONS — none
VERDICT — publishable-as-is
"""
        with (
            mock.patch.object(
                auditor,
                "_load_meeting_outputs",
                return_value=(
                    inputs.meeting,
                    dict(inputs.outputs),
                    dict(inputs.output_row_ids),
                    inputs.missing_outputs,
                ),
            ),
            mock.patch.object(
                auditor.local_vector_store,
                "load_transcript_words",
                return_value={"words": list(inputs.transcript_words)},
            ),
            mock.patch.object(
                auditor,
                "synthesize_via_claude_p",
                return_value=response,
            ) as synthesize,
            mock.patch.object(
                auditor,
                "run_deterministic_wrapper",
                return_value=_deterministic_empty(),
            ),
            mock.patch.object(auditor, "save_episode_audit_run"),
        ):
            result = auditor.run_episode_audit(7)
        self.assertEqual(result["run_status"], "complete")
        self.assertEqual(result["verdict"], "no_catches")
        synthesize.assert_called_once()
        self.assertEqual(
            synthesize.call_args.kwargs,
            {
                "model": auditor.AUDIT_MODEL_ID,
                "effort": "max",
                "timeout_seconds": auditor.NO_TIMEOUT,
            },
        )

    def test_run_validates_and_persists_parsed_proposals(self):
        before = "Council approved the water contract."
        inputs = _inputs(outputs={"newsletter": before})
        response = f"""FINDINGS
1. CONFIRMED: The verb needs correction.
PROPOSED_FIX
target_output: newsletter
before: <<<{before}>>>
after: <<<Council adopted the water contract.>>>
fix_rationale: Match the transcript outcome.
OPEN_FINDINGS
none
SUGGESTIONS
none
VERDICT
publishable-with-noted-flags
"""
        with (
            mock.patch.object(
                auditor,
                "_load_meeting_outputs",
                return_value=(
                    inputs.meeting,
                    dict(inputs.outputs),
                    dict(inputs.output_row_ids),
                    inputs.missing_outputs,
                ),
            ),
            mock.patch.object(
                auditor.local_vector_store,
                "load_transcript_words",
                return_value={"words": list(inputs.transcript_words)},
            ),
            mock.patch.object(
                auditor,
                "synthesize_via_claude_p",
                return_value=response,
            ),
            mock.patch.object(
                auditor,
                "run_deterministic_wrapper",
                return_value=_deterministic_empty(),
            ),
            mock.patch.object(auditor, "_load_roster", return_value=[]),
            mock.patch.object(
                auditor,
                "save_episode_audit_run",
            ) as save_run,
        ):
            result = auditor.run_episode_audit(7)
        self.assertEqual(result["proposals_validated_count"], 1)
        proposal = result["report"]["llm"]["proposals"][0]
        self.assertTrue(proposal["validated"])
        persisted = json.loads(save_run.call_args.kwargs["report_json"])
        self.assertTrue(persisted["llm"]["proposals"][0]["validated"])


class SynthesisEffortSeamTests(unittest.TestCase):
    def _command_for(self, **kwargs) -> list[str]:
        completed = mock.Mock(returncode=0, stdout="ok", stderr="")
        with (
            mock.patch.object(
                qdrant_synthesizer.shutil,
                "which",
                return_value="/bin/echo",
            ),
            mock.patch.object(
                qdrant_synthesizer.subprocess,
                "run",
                return_value=completed,
            ) as run,
        ):
            qdrant_synthesizer.synthesize_via_claude_p(
                "prompt",
                timeout_seconds=1,
                **kwargs,
            )
        return run.call_args.args[0]

    def test_effort_is_appended_only_when_opted_in(self):
        base_command = self._command_for()
        effort_command = self._command_for(effort="max")
        self.assertNotIn("--effort", base_command)
        self.assertEqual(effort_command[:-2], base_command)
        self.assertEqual(effort_command[-2:], ["--effort", "max"])


class EpisodeAuditDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "audit.db"

        def connect():
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            return conn

        self.connection_patch = mock.patch.object(
            database,
            "get_connection",
            side_effect=connect,
        )
        self.connection_patch.start()
        database.init_episode_audit_runs_schema()

    def tearDown(self):
        self.connection_patch.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def _fields(run_id: str, report_json: str) -> dict:
        return {
            "run_id": run_id,
            "meeting_id": 7,
            "outputs_snapshot_hash": "snapshot",
            "auditor_version": auditor.AUDITOR_VERSION,
            "prompt_sha256": "prompt",
            "model": auditor.AUDIT_MODEL_ID,
            "effort": "max",
            "run_status": "complete",
            "verdict": "no_catches",
            "findings_count": 0,
            "open_findings_count": 0,
            "suggestions_count": 0,
            "deterministic_flags_count": 0,
            "report_json": report_json,
            "started_at_utc": "2026-07-28T00:00:00Z",
            "duration_seconds": 1.25,
        }

    def test_schema_idempotent_replace_and_latest_report_parsing(self):
        database.init_episode_audit_runs_schema()
        database.save_episode_audit_run(
            **self._fields("first", json.dumps({"value": 1}))
        )
        database.save_episode_audit_run(
            **self._fields("second", json.dumps({"value": 2}))
        )
        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM episode_audit_runs"
            ).fetchone()[0]
        self.assertEqual(count, 1)
        latest = database.get_latest_episode_audit_run(7)
        self.assertIsNotNone(latest)
        self.assertEqual(latest["run_id"], "second")
        self.assertEqual(latest["report"], {"value": 2})

    def test_corrupt_report_json_is_returned_without_raising(self):
        database.save_episode_audit_run(
            **self._fields("corrupt", "{not-json")
        )
        latest = database.get_latest_episode_audit_run(7)
        self.assertIsNone(latest["report"])
        self.assertEqual(latest["report_json_raw"], "{not-json")


if __name__ == "__main__":
    unittest.main()
