"""Resumability and timeout tests for the sidecar stage orchestrator."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from zspan_pipeline import sidecar_pipeline


class SidecarResumeTests(unittest.TestCase):
    meeting_id = 42
    city_name = "Test City"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.preview_dir = Path(self.temp_dir.name)
        self.preview_patch = mock.patch.object(
            sidecar_pipeline,
            "PREVIEW_DIR",
            self.preview_dir,
        )
        self.preview_patch.start()
        self.addCleanup(self.preview_patch.stop)

    def _write_quotes(self, *, aligned: bool = False, rewritten: bool = False) -> Path:
        quote = {
            "quote_text": "A completed quote",
            "selection_rationale": "Explains the vote",
        }
        payload = {
            "meeting_id": self.meeting_id,
            "extraction_started": "complete",
            "quotes": [quote],
        }
        if aligned:
            quote["word_timings"] = [{"word": "A", "start_ms": 0, "end_ms": 1}]
            payload.update({
                "align_elapsed_seconds": 0.1,
                "align_aligned_count": 1,
                "align_failed_count": 0,
            })
        if rewritten:
            payload.update({
                "rationale_rewrite_elapsed_seconds": 0.1,
                "rationale_rewritten_count": 1,
            })
        path = self.preview_dir / f"m{self.meeting_id}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _write_decisions(self) -> Path:
        path = self.preview_dir / f"m{self.meeting_id}_decisions.json"
        path.write_text(json.dumps({
            "meeting_id": self.meeting_id,
            "extraction_started": "complete",
            "prose_output": "1. Approved the item [at 0:01:00].",
        }), encoding="utf-8")
        return path

    def _write_routing(self) -> None:
        path = self.preview_dir / f"m{self.meeting_id}_routing.json"
        path.write_text(json.dumps({
            "meeting_id": self.meeting_id,
            "router_started": "complete",
            "routing": [],
        }), encoding="utf-8")

    def _write_recusals(self) -> None:
        path = self.preview_dir / f"m{self.meeting_id}_recusals.json"
        path.write_text(json.dumps({
            "meeting_id": self.meeting_id,
            "recusals": [],
        }), encoding="utf-8")

    def _downstream(self, module: str, meeting_id: int) -> None:
        self.assertEqual(meeting_id, self.meeting_id)
        if module.endswith("quote_router_runner"):
            self._write_routing()
        elif module.endswith("recusal_detector"):
            self._write_recusals()
        elif module.endswith("align_preview_quotes"):
            payload = json.loads(
                (self.preview_dir / f"m{self.meeting_id}.json").read_text()
            )
            payload["quotes"][0]["word_timings"] = [
                {"word": "A", "start_ms": 0, "end_ms": 1}
            ]
            payload.update({
                "align_elapsed_seconds": 0.1,
                "align_aligned_count": 1,
                "align_failed_count": 0,
            })
            (self.preview_dir / f"m{self.meeting_id}.json").write_text(
                json.dumps(payload), encoding="utf-8",
            )
        elif module.endswith("rationale_rewriter"):
            payload = json.loads(
                (self.preview_dir / f"m{self.meeting_id}.json").read_text()
            )
            payload.update({
                "rationale_rewrite_elapsed_seconds": 0.1,
                "rationale_rewritten_count": 1,
            })
            (self.preview_dir / f"m{self.meeting_id}.json").write_text(
                json.dumps(payload), encoding="utf-8",
            )
        else:  # pragma: no cover - catches a new unmodelled stage
            self.fail(f"unexpected downstream module: {module}")

    def test_resume_after_mid_pipeline_failure_does_not_redo_quotes(self):
        quote_calls = 0
        decision_calls = 0

        def produce_quotes(meeting_id: int, city_name: str) -> Path:
            nonlocal quote_calls
            self.assertEqual((meeting_id, city_name), (self.meeting_id, self.city_name))
            quote_calls += 1
            return self._write_quotes()

        def produce_decisions(meeting_id: int, city_name: str) -> Path:
            nonlocal decision_calls
            self.assertEqual((meeting_id, city_name), (self.meeting_id, self.city_name))
            decision_calls += 1
            if decision_calls == 1:
                raise TimeoutError("decisions timed out")
            return self._write_decisions()

        with (
            mock.patch.object(sidecar_pipeline, "produce_quotes_sidecar", produce_quotes),
            mock.patch.object(
                sidecar_pipeline,
                "produce_decisions_sidecar",
                produce_decisions,
            ),
            mock.patch.object(sidecar_pipeline, "_run_downstream", self._downstream),
        ):
            with self.assertRaises(sidecar_pipeline.PipelineIncompleteError):
                sidecar_pipeline.run_pipeline(self.meeting_id, self.city_name)

            first_state = json.loads(
                (self.preview_dir / "m42_pipeline_state.json").read_text()
            )
            self.assertEqual(first_state["stages"]["quotes"]["status"], "completed")
            self.assertEqual(first_state["stages"]["decisions"]["status"], "failed")
            self.assertEqual(first_state["stages"]["routing"]["status"], "not_reached")
            self.assertEqual(first_state["stages"]["recusals"]["status"], "completed")
            self.assertEqual(first_state["stages"]["alignment"]["status"], "completed")
            self.assertEqual(
                first_state["stages"]["rationale_rewrite"]["status"],
                "completed",
            )

            result = sidecar_pipeline.run_pipeline(self.meeting_id, self.city_name)

        self.assertEqual(quote_calls, 1)
        self.assertEqual(decision_calls, 2)
        self.assertEqual(result["outcome"], "complete")
        self.assertEqual(result["stages"]["quotes"], "skipped")
        self.assertEqual(result["stages"]["decisions"], "completed")

    def test_fully_completed_artifacts_are_skipped_idempotently(self):
        self._write_quotes(aligned=True, rewritten=True)
        self._write_decisions()
        self._write_routing()
        self._write_recusals()

        with (
            mock.patch.object(
                sidecar_pipeline,
                "produce_quotes_sidecar",
                side_effect=AssertionError("quotes should be skipped"),
            ),
            mock.patch.object(
                sidecar_pipeline,
                "produce_decisions_sidecar",
                side_effect=AssertionError("decisions should be skipped"),
            ),
            mock.patch.object(
                sidecar_pipeline,
                "_run_downstream",
                side_effect=AssertionError("downstream should be skipped"),
            ),
        ):
            first = sidecar_pipeline.run_pipeline(self.meeting_id, self.city_name)
            second = sidecar_pipeline.run_pipeline(self.meeting_id, self.city_name)

        self.assertEqual(first["outcome"], "complete")
        self.assertEqual(second["outcome"], "complete")
        self.assertEqual(set(second["stages"].values()), {"skipped"})
        state = json.loads((self.preview_dir / "m42_pipeline_state.json").read_text())
        self.assertEqual(state["run_number"], 2)

    def test_state_distinguishes_skipped_failed_and_not_reached(self):
        self._write_quotes(aligned=True, rewritten=True)
        self._write_recusals()

        with (
            mock.patch.object(
                sidecar_pipeline,
                "produce_decisions_sidecar",
                side_effect=RuntimeError("stage-specific failure"),
            ),
            mock.patch.object(
                sidecar_pipeline,
                "_run_downstream",
                side_effect=AssertionError("all reachable downstream artifacts exist"),
            ),
        ):
            with self.assertRaises(sidecar_pipeline.PipelineIncompleteError):
                sidecar_pipeline.run_pipeline(self.meeting_id, self.city_name)

        state = json.loads((self.preview_dir / "m42_pipeline_state.json").read_text())
        self.assertEqual(state["stages"]["quotes"]["status"], "skipped")
        self.assertEqual(state["stages"]["decisions"]["status"], "failed")
        self.assertIn("stage-specific failure", state["stages"]["decisions"]["reason"])
        self.assertEqual(state["stages"]["routing"]["status"], "not_reached")
        self.assertIn("requires completed", state["stages"]["routing"]["reason"])

    def test_decisions_timeout_env_override_reaches_claude_call(self):
        synthesizer = sidecar_pipeline.qdrant_synthesizer
        with (
            mock.patch.dict(
                os.environ,
                {sidecar_pipeline.DECISIONS_TIMEOUT_ENV: "1234.5"},
            ),
            mock.patch.object(synthesizer, "load_canonical_prompt", return_value="prompt"),
            mock.patch.object(
                synthesizer,
                "load_complete_meeting_chunks",
                return_value=[object()],
            ),
            mock.patch.object(synthesizer, "build_synthesis_prompt", return_value="built"),
            mock.patch.object(
                synthesizer,
                "generate_with_fallback",
                side_effect=RuntimeError("stop after observing timeout"),
            ) as generate,
        ):
            with self.assertRaisesRegex(RuntimeError, "observing timeout"):
                sidecar_pipeline.produce_decisions_sidecar(
                    self.meeting_id,
                    self.city_name,
                )

        self.assertEqual(generate.call_args.kwargs["timeout_seconds"], 1234.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
