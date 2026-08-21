"""Decision-sidecar citation integration tests."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

# neutrality_audit's package initializer imports its optional HTTP extraction
# backend even though these tests exercise only deterministic sidecar code.
# Keep the offline unit test independent of that runtime dependency.
if "requests" not in sys.modules:
    try:
        __import__("requests")
    except ModuleNotFoundError:
        sys.modules["requests"] = ModuleType("requests")

from zspan_pipeline import sidecar_pipeline


def _generation(content: str):
    synthesizer = sidecar_pipeline.qdrant_synthesizer
    return synthesizer.GenerationResult(
        content=content,
        model_id=synthesizer.GEMINI_PRIMARY_MODEL_ID,
        attempts=(
            synthesizer.GenerationAttempt(
                "google",
                synthesizer.GEMINI_PRIMARY_MODEL_ID,
                None,
            ),
        ),
    )


def _words(start: float, text: str) -> list[dict]:
    return [
        {"word": word, "start": start + index * 0.25, "end": start + index * 0.25 + 0.2}
        for index, word in enumerate(text.split())
    ]


class DecisionsSidecarCitationTests(unittest.TestCase):
    def _produce(
        self,
        preview_dir: Path,
        generated: str,
        *,
        transcript_words: list[dict] | None = None,
    ) -> Path:
        chunk = SimpleNamespace(
            body="Council considered road, water, lighting, and grant contracts.",
            chunk_index=4,
            start_seconds=80.0,
            end_seconds=500.0,
        )
        transcript = {
            "words": transcript_words
            if transcript_words is not None
            else (
                _words(
                    100.0,
                    "item one discussion of possible action to approve the road contract",
                )
                + _words(
                    120.0,
                    "make a motion to approve line item one road contract as stated",
                )
            )
        }
        with (
            mock.patch.object(sidecar_pipeline, "PREVIEW_DIR", preview_dir),
            mock.patch.object(
                sidecar_pipeline.qdrant_synthesizer,
                "load_canonical_prompt",
                return_value="prompt",
            ),
            mock.patch.object(
                sidecar_pipeline.qdrant_synthesizer,
                "load_complete_meeting_chunks",
                return_value=[chunk],
            ),
            mock.patch.object(
                sidecar_pipeline.qdrant_synthesizer,
                "build_synthesis_prompt",
                return_value="synthesis prompt",
            ),
            mock.patch.object(
                sidecar_pipeline.qdrant_synthesizer,
                "generate_with_fallback",
                return_value=_generation(generated),
            ),
            mock.patch.object(
                sidecar_pipeline,
                "_load_transcript_words",
                return_value=transcript,
            ),
            mock.patch.object(
                sidecar_pipeline,
                "_apply_corrections",
                side_effect=lambda _city, text: text,
            ),
        ):
            return sidecar_pipeline.produce_decisions_sidecar(42, "Test City")

    def test_prose_is_aligned_and_validated_before_write(self):
        generated = (
            "1. <core>Approved the road contract</core> [at 0:01:40].\n\n"
            '<!-- audit [{"index": 1, "news_values": [], "rationale": "Routine", '
            '"item_quote": "item one discussion of possible action to approve the road contract", '
            '"action_quote": "make a motion to approve line item one road contract as stated"}] audit -->'
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertLogs(sidecar_pipeline.logger, level="INFO"):
                path = self._produce(Path(tmp), generated)
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(
            payload["prose_output"],
            "1. <core>Approved the road contract</core>.",
        )
        self.assertEqual(payload["citation_modality"], "transcript_excerpt_v1")
        self.assertEqual(payload["evidence_mode"], "complete_transcript")
        self.assertEqual(
            payload["model_id"],
            sidecar_pipeline.qdrant_synthesizer.GEMINI_PRIMARY_MODEL_ID,
        )
        self.assertEqual(
            payload["decisions"][0]["verbatim_spans"][0]["text"],
            "item one discussion of possible action to approve the road contract "
            "make a motion to approve line item one road contract as stated",
        )
        self.assertEqual(payload["prose_list_count"], 1)
        self.assertEqual(
            payload["citation_alignment"][0]["source"],
            "two_part_quote",
        )
        self.assertFalse(payload["citation_alignment"][0]["lower_confidence"])
        self.assertEqual(payload["citation_omissions"], [])

    def test_missing_locator_fails_closed_without_writing_sidecar(self):
        generated = "1. <core>Approved the road contract</core>."
        with tempfile.TemporaryDirectory() as tmp:
            preview = Path(tmp)
            with (
                self.assertLogs(sidecar_pipeline.logger, level="WARNING"),
                self.assertRaisesRegex(RuntimeError, "citation alignment failed"),
            ):
                self._produce(preview, generated)
            self.assertFalse((preview / "m42_decisions.json").exists())

    def test_partial_failure_writes_survivors_and_syncs_audit(self):
        transcripts: list[dict] = []
        decisions: list[str] = []
        audits: list[dict] = []
        names = ("road", "water", "lighting", "grant")
        for index, name in enumerate(names, start=1):
            intro_start = 80.0 + index * 80.0
            action_start = intro_start + 30.0
            item_quote = (
                f"item {index} discussion of possible action to approve the {name} contract"
            )
            action_quote = (
                f"make a motion to approve line item {index} {name} contract as stated"
            )
            transcripts += _words(intro_start, item_quote)
            transcripts += _words(action_start, action_quote)
            decisions.append(
                f"{index}. <core>Approved the {name} contract</core> "
                f"[at 0:{int(intro_start // 60):02d}:{int(intro_start % 60):02d}]."
            )
            audits.append(
                {
                    "index": index,
                    "news_values": [],
                    "rationale": name,
                    "item_quote": item_quote,
                    "action_quote": (
                        "this action quote was never spoken"
                        if index == 2
                        else action_quote
                    ),
                }
            )
        generated = "\n\n".join(decisions) + (
            "\n\n<!-- audit " + json.dumps(audits) + " audit -->"
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = self._produce(
                Path(tmp),
                generated,
                transcript_words=transcripts,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["prose_list_count"], 3)
        self.assertEqual(
            [line.split(".", 1)[0] for line in payload["prose_output"].splitlines() if line],
            ["1", "2", "3"],
        )
        self.assertEqual(
            [entry["rationale"] for entry in payload["audit_json"]],
            ["road", "lighting", "grant"],
        )
        self.assertEqual(
            [entry["index"] for entry in payload["audit_json"]],
            [1, 2, 3],
        )
        self.assertEqual(payload["citation_omissions"][0]["index"], 2)
        self.assertEqual(
            [entry["source_index"] for entry in payload["citation_alignment"]],
            [1, 3, 4],
        )

    def test_total_failure_escalates_without_writing_sidecar(self):
        generated = (
            "1. <core>Approved the road contract</core> [at 0:01:40].\n\n"
            '<!-- audit [{"index": 1, "news_values": [], "rationale": "Road", '
            '"item_quote": "item one discussion of possible action to approve the road contract", '
            '"action_quote": "this action was never spoken"}] audit -->'
        )
        with tempfile.TemporaryDirectory() as tmp:
            preview = Path(tmp)
            with self.assertRaisesRegex(
                RuntimeError,
                "alignment failed for every decision",
            ):
                self._produce(preview, generated)
            self.assertFalse((preview / "m42_decisions.json").exists())

    def test_legacy_fallback_cannot_enter_new_excerpt_modality(self):
        generated = (
            "1. <core>Approved the road contract</core> [at 0:01:40]."
        )
        with tempfile.TemporaryDirectory() as tmp:
            preview = Path(tmp)
            with self.assertRaisesRegex(RuntimeError, "requires two-part anchors"):
                self._produce(preview, generated)
            self.assertFalse((preview / "m42_decisions.json").exists())

    def test_quote_anchored_chunk_miss_generates_with_observation(self):
        item_quote = "item one discussion of possible action on the road contract"
        action_quote = "make a motion to approve the road contract as stated"
        generated = (
            "1. <core>Approved the road contract</core> [at 0:08:00].\n\n"
            "<!-- audit "
            + json.dumps(
                [
                    {
                        "index": 1,
                        "news_values": [],
                        "rationale": "Road",
                        "item_quote": item_quote,
                        "action_quote": action_quote,
                    }
                ]
            )
            + " audit -->"
        )
        transcript = _words(620.0, item_quote) + _words(650.0, action_quote)

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertLogs(sidecar_pipeline.logger, level="WARNING"):
                path = self._produce(
                    Path(tmp),
                    generated,
                    transcript_words=transcript,
                )
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["prose_list_count"], 1)
        self.assertNotIn("[at ", payload["prose_output"])
        self.assertEqual(payload["citation_omissions"], [])
        self.assertEqual(
            payload["citation_observations"][0]["reason"],
            "quote_anchored_outside_retrieved_chunks",
        )
        self.assertEqual(
            payload["citation_alignment"][0]["source"],
            "two_part_quote",
        )

    def test_any_surviving_signature_fallback_blocks_new_modality(self):
        generated = (
            "1. <core>Approved the road contract</core> [at 0:01:40].\n\n"
            "2. <core>Appointed a planning commissioner</core> [at 0:33:20]."
        )
        transcript = _words(
            100.0,
            "make a motion to approve the road contract as stated",
        ) + _words(
            2_000.0,
            "we have decided to ask Jordan Lee to fill the planning commission vacancy",
        )

        with tempfile.TemporaryDirectory() as tmp:
            preview = Path(tmp)
            with self.assertRaisesRegex(RuntimeError, "requires two-part anchors"):
                self._produce(
                    preview,
                    generated,
                    transcript_words=transcript,
                )
            self.assertFalse((preview / "m42_decisions.json").exists())

    def test_all_fallback_chunk_misses_escalate(self):
        generated = (
            "1. <core>Appointed a planning commissioner</core> [at 0:33:20]."
        )
        transcript = _words(
            2_000.0,
            "we have decided to ask Jordan Lee to fill the planning commission vacancy",
        )

        with tempfile.TemporaryDirectory() as tmp:
            preview = Path(tmp)
            with self.assertRaisesRegex(
                RuntimeError,
                "alignment failed for every decision",
            ):
                self._produce(
                    preview,
                    generated,
                    transcript_words=transcript,
                )
            self.assertFalse((preview / "m42_decisions.json").exists())


if __name__ == "__main__":
    unittest.main()
