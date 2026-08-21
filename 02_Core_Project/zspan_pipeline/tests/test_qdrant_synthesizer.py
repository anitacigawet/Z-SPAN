"""Focused configuration tests for the Qdrant synthesis subprocess."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from zspan_pipeline import qdrant_synthesizer


class QdrantSynthesizerProvenanceTests(unittest.TestCase):
    def test_writes_merges_and_recovers_from_corrupt_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            preview_dir = Path(temp_dir)
            artifact_path = preview_dir / "m42_synthesis_provenance.json"

            with mock.patch.object(
                qdrant_synthesizer,
                "PREVIEW_DIR",
                preview_dir,
            ):
                written_path = (
                    qdrant_synthesizer.record_synthesis_provenance(
                        meeting_id=42,
                        output_type="synopsis",
                        prompt="exact prompt α",
                        model_id="claude-test-model",
                        retrieved_chunk_ids=[3, 8],
                    )
                )
                qdrant_synthesizer.record_synthesis_provenance(
                    meeting_id=42,
                    output_type="newsletter",
                    prompt="second prompt",
                    model_id="claude-test-model",
                    retrieved_chunk_ids=[5],
                )

                self.assertEqual(written_path, artifact_path)
                merged = json.loads(artifact_path.read_text(encoding="utf-8"))
                self.assertEqual(set(merged), {"synopsis", "newsletter"})
                synopsis = merged["synopsis"]
                self.assertEqual(synopsis["output_type"], "synopsis")
                self.assertEqual(
                    synopsis["prompt_sha256"],
                    hashlib.sha256("exact prompt α".encode("utf-8")).hexdigest(),
                )
                self.assertEqual(synopsis["prompt_char_count"], 14)
                self.assertEqual(synopsis["model_id"], "claude-test-model")
                self.assertEqual(synopsis["retrieved_chunk_ids"], [3, 8])
                self.assertEqual(synopsis["evidence_mode"], "complete_transcript")
                recorded_at = datetime.fromisoformat(
                    synopsis["recorded_at"].replace("Z", "+00:00")
                )
                self.assertIsNotNone(recorded_at.tzinfo)

                artifact_path.write_text("{not json", encoding="utf-8")
                with self.assertLogs(
                    qdrant_synthesizer.logger,
                    level="WARNING",
                ):
                    qdrant_synthesizer.record_synthesis_provenance(
                        meeting_id=42,
                        output_type="key_decisions",
                        prompt="replacement prompt",
                        model_id="claude-test-model",
                        retrieved_chunk_ids=[13],
                    )

            recovered = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(set(recovered), {"key_decisions"})
            self.assertEqual(
                recovered["key_decisions"]["retrieved_chunk_ids"],
                [13],
            )


class QdrantSynthesizerTimeoutTests(unittest.TestCase):
    def test_configured_timeout_reaches_claude_subprocess(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {qdrant_synthesizer.SYNTHESIS_TIMEOUT_ENV: "1234.5"},
            ),
            mock.patch.object(
                qdrant_synthesizer.shutil,
                "which",
                return_value="/bin/sh",
            ),
            mock.patch.object(
                qdrant_synthesizer.subprocess,
                "run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout="generated output\n",
                    stderr="",
                ),
            ) as run,
        ):
            output = qdrant_synthesizer.synthesize_via_claude_p("full prompt")

        self.assertEqual(output, "generated output")
        self.assertEqual(run.call_args.kwargs["timeout"], 1234.5)
        command = run.call_args.args[0]
        self.assertEqual(
            command,
            [
                "/bin/sh",
                "-p",
                "--model",
                qdrant_synthesizer.SONNET_MODEL_ID,
                "--output-format",
                "text",
                "--tools",
                "",
            ],
        )

    def test_default_timeout_matches_decisions_ceiling(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {qdrant_synthesizer.SYNTHESIS_TIMEOUT_ENV: ""},
            ),
            mock.patch.object(
                qdrant_synthesizer.shutil,
                "which",
                return_value="/bin/sh",
            ),
            mock.patch.object(
                qdrant_synthesizer.subprocess,
                "run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout="generated output",
                    stderr="",
                ),
            ) as run,
        ):
            qdrant_synthesizer.synthesize_via_claude_p("full prompt")

        self.assertEqual(
            run.call_args.kwargs["timeout"],
            qdrant_synthesizer.DEFAULT_SYNTHESIS_TIMEOUT_SECONDS,
        )
        self.assertEqual(run.call_args.kwargs["timeout"], 900.0)

    def test_long_output_budget_reaches_claude_subprocess(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {
                    qdrant_synthesizer.CLAUDE_MAX_OUTPUT_TOKENS_ENV: "",
                },
            ),
            mock.patch.object(
                qdrant_synthesizer.shutil,
                "which",
                return_value="/bin/sh",
            ),
            mock.patch.object(
                qdrant_synthesizer.subprocess,
                "run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout="generated output",
                    stderr="",
                ),
            ) as run,
        ):
            qdrant_synthesizer.synthesize_via_claude_p(
                "full prompt", max_output_tokens=64_000
            )

        self.assertEqual(
            run.call_args.kwargs["env"][
                qdrant_synthesizer.CLAUDE_MAX_OUTPUT_TOKENS_ENV
            ],
            "64000",
        )

    def test_operator_long_output_budget_is_preserved(self) -> None:
        with mock.patch.dict(
            os.environ,
            {qdrant_synthesizer.CLAUDE_MAX_OUTPUT_TOKENS_ENV: "96000"},
        ):
            env = qdrant_synthesizer._sanitized_synth_env()

        self.assertEqual(
            env[qdrant_synthesizer.CLAUDE_MAX_OUTPUT_TOKENS_ENV],
            "96000",
        )

    def test_invalid_long_output_budget_fails_before_subprocess(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive integer"):
            qdrant_synthesizer.synthesize_via_claude_p(
                "full prompt", max_output_tokens=0
            )

    def test_failed_cli_preserves_stdout_and_stderr_for_caller_artifacts(self) -> None:
        with (
            mock.patch.object(
                qdrant_synthesizer.shutil,
                "which",
                return_value="/bin/sh",
            ),
            mock.patch.object(
                qdrant_synthesizer.subprocess,
                "run",
                return_value=SimpleNamespace(
                    returncode=1,
                    stdout='{"partial": true',
                    stderr="diagnostic text",
                ),
            ),
        ):
            with self.assertRaises(qdrant_synthesizer.ClaudePError) as raised:
                qdrant_synthesizer.synthesize_via_claude_p("full prompt")

        self.assertEqual(raised.exception.returncode, 1)
        self.assertEqual(raised.exception.stdout, '{"partial": true')
        self.assertEqual(raised.exception.stderr, "diagnostic text")


class FlagshipFallbackTests(unittest.TestCase):
    def test_agy_limit_auth_and_unavailable_shapes_are_classified(self):
        cases = {
            "RESOURCE_EXHAUSTED: quota exceeded (HTTP 429)": (
                qdrant_synthesizer.ACCOUNT_LIMIT
            ),
            "Not logged in; OAuth token expired (status=401)": (
                qdrant_synthesizer.AUTH_FAILURE
            ),
            "Requested model is overloaded; no capacity for model": (
                qdrant_synthesizer.MODEL_UNAVAILABLE
            ),
            "provider unavailable: gateway timeout (HTTP 503)": (
                qdrant_synthesizer.TIMEOUT
            ),
            "jetski: no output produced; command permission auto-denied": (
                qdrant_synthesizer.UNKNOWN_FAILURE
            ),
        }
        for diagnostic, expected in cases.items():
            with self.subTest(diagnostic=diagnostic):
                self.assertEqual(
                    qdrant_synthesizer.classify_agy_failure(diagnostic),
                    expected,
                )

        self.assertEqual(
            qdrant_synthesizer._failure_from_agy_output(
                "RESOURCE_EXHAUSTED: quota exceeded"
            ),
            qdrant_synthesizer.ACCOUNT_LIMIT,
        )
        self.assertIsNone(
            qdrant_synthesizer._failure_from_agy_output(
                "The council discussed a vendor quota exceeded last month."
            )
        )

    def test_opus_is_active_while_fable_and_sonnet_are_not(self):
        active_models = {
            qdrant_synthesizer.GEMINI_PRIMARY_MODEL_ID,
            qdrant_synthesizer.GEMINI_BACKUP_MODEL_ID,
            qdrant_synthesizer.OPUS_BACKSTOP_MODEL_ID,
        }
        self.assertNotIn("claude-fable-5", active_models)
        self.assertIn("claude-opus-4-6", active_models)
        self.assertNotIn(qdrant_synthesizer.SONNET_MODEL_ID, active_models)

    def test_primary_invocation_is_explicit_gemini_pro_high(self):
        with mock.patch.object(
            qdrant_synthesizer,
            "_synthesize_via_gemini",
            return_value="answer",
        ) as synthesize:
            result = qdrant_synthesizer.generate_with_fallback("prompt")

        self.assertEqual(
            result.model_id,
            qdrant_synthesizer.GEMINI_PRIMARY_MODEL_ID,
        )
        synthesize.assert_called_once_with(
            "TASK INPUT:\nprompt",
            model_id=qdrant_synthesizer.GEMINI_PRIMARY_MODEL_ID,
        )
        self.assertEqual(
            [attempt.as_dict() for attempt in result.attempts],
            [
                {
                    "provider": "google",
                    "model_id": qdrant_synthesizer.GEMINI_PRIMARY_MODEL_ID,
                    "failure_class": None,
                }
            ],
        )

    def test_model_unavailable_uses_opus_before_flash(self):
        with (
            mock.patch.object(
                qdrant_synthesizer,
                "_synthesize_via_gemini",
                side_effect=qdrant_synthesizer.GeminiPError(
                    qdrant_synthesizer.MODEL_UNAVAILABLE,
                    "Pro capacity unavailable",
                ),
            ) as gemini,
            mock.patch.object(
                qdrant_synthesizer,
                "_attempt_claude_generation",
                return_value=("opus answer", None, ""),
            ) as opus,
        ):
            result = qdrant_synthesizer.generate_with_fallback("prompt")

        self.assertEqual(gemini.call_count, 1)
        opus.assert_called_once_with(
            "prompt",
            model_id=qdrant_synthesizer.OPUS_BACKSTOP_MODEL_ID,
            timeout_seconds=None,
            max_output_tokens=None,
            output_json_schema=None,
            system_prompt=None,
        )
        self.assertEqual(result.model_id, qdrant_synthesizer.OPUS_BACKSTOP_MODEL_ID)
        self.assertEqual(
            [attempt.model_id for attempt in result.attempts],
            [
                qdrant_synthesizer.GEMINI_PRIMARY_MODEL_ID,
                qdrant_synthesizer.OPUS_BACKSTOP_MODEL_ID,
            ],
        )
        self.assertEqual(
            [attempt.failure_class for attempt in result.attempts],
            [qdrant_synthesizer.MODEL_UNAVAILABLE, None],
        )

    def test_flash_runs_last_when_pro_and_opus_are_model_unavailable(self):
        with (
            mock.patch.object(
                qdrant_synthesizer,
                "_synthesize_via_gemini",
                side_effect=[
                    qdrant_synthesizer.GeminiPError(
                        qdrant_synthesizer.MODEL_UNAVAILABLE,
                        "Pro capacity unavailable",
                    ),
                    "flash answer",
                ],
            ) as gemini,
            mock.patch.object(
                qdrant_synthesizer,
                "_attempt_claude_generation",
                return_value=(
                    None,
                    qdrant_synthesizer.MODEL_UNAVAILABLE,
                    "Opus unavailable",
                ),
            ) as opus,
        ):
            result = qdrant_synthesizer.generate_with_fallback("prompt")

        self.assertEqual(
            [call.kwargs["model_id"] for call in gemini.call_args_list],
            [
                qdrant_synthesizer.GEMINI_PRIMARY_MODEL_ID,
                qdrant_synthesizer.GEMINI_BACKUP_MODEL_ID,
            ],
        )
        self.assertEqual(opus.call_count, 1)
        self.assertEqual(result.model_id, qdrant_synthesizer.GEMINI_BACKUP_MODEL_ID)
        self.assertEqual(
            [attempt.model_id for attempt in result.attempts],
            [
                qdrant_synthesizer.GEMINI_PRIMARY_MODEL_ID,
                qdrant_synthesizer.OPUS_BACKSTOP_MODEL_ID,
                qdrant_synthesizer.GEMINI_BACKUP_MODEL_ID,
            ],
        )

    def test_spent_opus_circuit_skips_to_flash_for_pro_model_failure(self):
        with (
            mock.patch.object(
                qdrant_synthesizer,
                "_synthesize_via_gemini",
                side_effect=[
                    qdrant_synthesizer.GeminiPError(
                        qdrant_synthesizer.MODEL_UNAVAILABLE,
                        "Pro capacity unavailable",
                    ),
                    "flash answer",
                ],
            ) as gemini,
            mock.patch.object(
                qdrant_synthesizer,
                "_attempt_claude_generation",
            ) as opus,
            qdrant_synthesizer.work_order_generation_scope(711) as state,
        ):
            state.opus_attempted = True
            result = qdrant_synthesizer.generate_with_fallback("prompt")

        opus.assert_not_called()
        self.assertEqual(
            [call.kwargs["model_id"] for call in gemini.call_args_list],
            [
                qdrant_synthesizer.GEMINI_PRIMARY_MODEL_ID,
                qdrant_synthesizer.GEMINI_BACKUP_MODEL_ID,
            ],
        )
        self.assertEqual(result.model_id, qdrant_synthesizer.GEMINI_BACKUP_MODEL_ID)

    def test_opus_backstop_uses_explicit_max_effort(self):
        with mock.patch.object(
            qdrant_synthesizer,
            "synthesize_via_claude_p",
            return_value="opus answer",
        ) as synthesize:
            content, failure_class, detail = (
                qdrant_synthesizer._attempt_claude_generation(
                    "prompt",
                    model_id=qdrant_synthesizer.OPUS_BACKSTOP_MODEL_ID,
                    timeout_seconds=None,
                    max_output_tokens=None,
                    output_json_schema=None,
                    system_prompt=None,
                )
            )

        self.assertEqual((content, failure_class, detail), ("opus answer", None, ""))
        synthesize.assert_called_once_with(
            "prompt",
            model=qdrant_synthesizer.OPUS_BACKSTOP_MODEL_ID,
            timeout_seconds=None,
            max_output_tokens=None,
            output_json_schema=None,
            effort="max",
            system_prompt=None,
        )

    def test_account_limit_skips_flash_and_uses_opus_backstop(self):
        with (
            mock.patch.object(
                qdrant_synthesizer,
                "_synthesize_via_gemini",
                side_effect=qdrant_synthesizer.GeminiPError(
                    qdrant_synthesizer.ACCOUNT_LIMIT,
                    "quota exceeded",
                ),
            ) as gemini,
            mock.patch.object(
                qdrant_synthesizer,
                "_attempt_claude_generation",
                return_value=("opus answer", None, ""),
            ) as opus,
        ):
            result = qdrant_synthesizer.generate_with_fallback("prompt")

        self.assertEqual(gemini.call_count, 1)
        self.assertEqual(
            gemini.call_args.kwargs["model_id"],
            qdrant_synthesizer.GEMINI_PRIMARY_MODEL_ID,
        )
        opus.assert_called_once_with(
            "prompt",
            model_id=qdrant_synthesizer.OPUS_BACKSTOP_MODEL_ID,
            timeout_seconds=None,
            max_output_tokens=None,
            output_json_schema=None,
            system_prompt=None,
        )
        self.assertEqual(result.model_id, qdrant_synthesizer.OPUS_BACKSTOP_MODEL_ID)

    def test_transient_skips_flash_without_blind_retry_and_uses_opus(self):
        with (
            mock.patch.object(
                qdrant_synthesizer,
                "_synthesize_via_gemini",
                side_effect=qdrant_synthesizer.GeminiPError(
                    qdrant_synthesizer.TRANSIENT_NETWORK,
                    "provider unavailable",
                ),
            ) as gemini,
            mock.patch.object(
                qdrant_synthesizer,
                "_attempt_claude_generation",
                return_value=("opus answer", None, ""),
            ) as opus,
        ):
            result = qdrant_synthesizer.generate_with_fallback("prompt")

        self.assertEqual(gemini.call_count, 1)
        self.assertEqual(opus.call_count, 1)
        self.assertEqual(result.model_id, qdrant_synthesizer.OPUS_BACKSTOP_MODEL_ID)

    def test_unknown_failure_fails_closed_without_model_descent(self):
        with (
            mock.patch.object(
                qdrant_synthesizer,
                "_synthesize_via_gemini",
                side_effect=qdrant_synthesizer.GeminiPError(
                    qdrant_synthesizer.UNKNOWN_FAILURE,
                    "unexpected",
                ),
            ) as gemini,
            mock.patch.object(
                qdrant_synthesizer,
                "_attempt_claude_generation",
            ) as opus,
        ):
            with self.assertRaises(qdrant_synthesizer.GenerationPausedError) as raised:
                qdrant_synthesizer.generate_with_fallback("prompt")

        self.assertEqual(gemini.call_count, 1)
        opus.assert_not_called()
        self.assertEqual(
            raised.exception.failure_class,
            qdrant_synthesizer.UNKNOWN_FAILURE,
        )

    def test_work_order_caches_google_account_wall_and_spends_opus_once(self):
        with (
            mock.patch.object(
                qdrant_synthesizer,
                "_synthesize_via_gemini",
                side_effect=qdrant_synthesizer.GeminiPError(
                    qdrant_synthesizer.ACCOUNT_LIMIT,
                    "quota exceeded",
                ),
            ) as gemini,
            mock.patch.object(
                qdrant_synthesizer,
                "_attempt_claude_generation",
                return_value=("one rescued artifact", None, ""),
            ) as opus,
            qdrant_synthesizer.work_order_generation_scope(812) as state,
        ):
            first = qdrant_synthesizer.generate_with_fallback("artifact one")
            with self.assertRaises(qdrant_synthesizer.GenerationPausedError) as raised:
                qdrant_synthesizer.generate_with_fallback("artifact two")

        self.assertEqual(first.model_id, qdrant_synthesizer.OPUS_BACKSTOP_MODEL_ID)
        self.assertEqual(gemini.call_count, 1)
        self.assertEqual(opus.call_count, 1)
        self.assertEqual(
            state.provider_account_failures,
            {"google": qdrant_synthesizer.ACCOUNT_LIMIT},
        )
        self.assertTrue(state.opus_attempted)
        self.assertEqual(
            raised.exception.failure_class,
            qdrant_synthesizer.OPUS_WORK_ORDER_BUDGET_EXHAUSTED,
        )

    def test_work_order_scope_propagates_into_asyncio_to_thread(self):
        async def observe_state():
            return await asyncio.to_thread(
                qdrant_synthesizer._WORK_ORDER_GENERATION_STATE.get
            )

        with qdrant_synthesizer.work_order_generation_scope(913) as state:
            observed = asyncio.run(observe_state())

        self.assertIs(observed, state)

    def test_gemini_command_matches_proven_contract_and_has_minimal_env(self):
        for model_id in (
            qdrant_synthesizer.GEMINI_PRIMARY_MODEL_ID,
            qdrant_synthesizer.GEMINI_BACKUP_MODEL_ID,
        ):
            with self.subTest(model_id=model_id):
                command = qdrant_synthesizer._build_gemini_command(
                    "full prompt",
                    agy_bin="/usr/local/bin/agy",
                    model_id=model_id,
                )
                self.assertEqual(
                    command,
                    [
                        "/usr/local/bin/agy",
                        "-p",
                        "full prompt",
                        "--model",
                        model_id,
                        "--effort",
                        "high",
                        "--sandbox",
                        "--output-format",
                        "text",
                        "--print-timeout",
                        "15m",
                    ],
                )
                self.assertNotIn("--dangerously-skip-permissions", command)
                self.assertNotIn("--permissions", command)

        with mock.patch.dict(
            os.environ,
            {
                "HOME": "/tmp/home",
                "PATH": "/bin",
                "OPENAI_API_KEY": "secret",
                "UNRELATED": "value",
            },
            clear=True,
        ):
            self.assertEqual(
                qdrant_synthesizer._minimal_agy_env(),
                {"HOME": "/tmp/home", "PATH": "/bin"},
            )

    def test_oversized_gemini_prompt_fails_typed_without_truncation(self):
        prompt = "é" * 200_001
        with self.assertRaises(qdrant_synthesizer.GeminiPError) as raised:
            qdrant_synthesizer._build_gemini_command(prompt, agy_bin="/bin/agy")

        self.assertEqual(
            raised.exception.failure_class,
            qdrant_synthesizer.PROMPT_TOO_LARGE_FOR_GEMINI,
        )

    def test_failed_opus_backstop_preserves_full_attempt_trail(self):
        with (
            mock.patch.object(
                qdrant_synthesizer,
                "_synthesize_via_gemini",
                side_effect=qdrant_synthesizer.GeminiPError(
                    qdrant_synthesizer.AUTH_FAILURE,
                    "not logged in",
                ),
            ),
            mock.patch.object(
                qdrant_synthesizer,
                "_attempt_claude_generation",
                return_value=(
                    None,
                    qdrant_synthesizer.ACCOUNT_LIMIT,
                    "session limit",
                ),
            ),
        ):
            with self.assertRaises(qdrant_synthesizer.GenerationPausedError) as raised:
                qdrant_synthesizer.generate_with_fallback("prompt")

        self.assertEqual(
            raised.exception.failure_class,
            qdrant_synthesizer.ACCOUNT_LIMIT,
        )
        self.assertEqual(
            [attempt.model_id for attempt in raised.exception.attempts],
            [
                qdrant_synthesizer.GEMINI_PRIMARY_MODEL_ID,
                qdrant_synthesizer.OPUS_BACKSTOP_MODEL_ID,
            ],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
