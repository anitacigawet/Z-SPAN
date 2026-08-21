"""Unit tests for the interactive per-output approval gate."""
from __future__ import annotations

import io
import os
import unittest
from unittest import mock

from zspan_cli import approval
from zspan_cli.pipeline import RetrievedChunk


def _chunk(text: str = "Transcript evidence") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_index=7,
        text=text,
        start_seconds=65.0,
        end_seconds=75.0,
        score=0.875,
    )


def _approval_kwargs(**overrides):
    values = {
        "output_type": "synopsis",
        "chunk_index": 1,
        "chunk_total": 4,
        "retrieval_query": "What happened in this meeting?",
        "retrieved_chunks": [_chunk()],
        "canonical_prompt": "Summarize the record.",
        "full_envelope": "Complete provider envelope.",
        "provider": "openai",
        "model": "test-model",
        "key_fingerprint_str": "sk-t...1234",
    }
    values.update(overrides)
    return values


class TestApproval(unittest.TestCase):
    def test_should_prompt_env_var_bypasses(self):
        with mock.patch.dict(
            os.environ, {approval.YES_TO_ALL_ENV_VAR: "1"}, clear=False
        ):
            self.assertFalse(approval.should_prompt(False))

    def test_should_prompt_env_empty_string_not_bypass(self):
        with mock.patch.dict(
            os.environ, {approval.YES_TO_ALL_ENV_VAR: ""}, clear=False
        ):
            self.assertTrue(approval.should_prompt(False))

    def test_should_prompt_cli_flag_bypasses(self):
        with mock.patch.dict(
            os.environ, {approval.YES_TO_ALL_ENV_VAR: ""}, clear=False
        ):
            self.assertFalse(approval.should_prompt(True))

    def test_strip_display_ansi_removes_color_codes(self):
        self.assertEqual(
            approval.strip_display_ansi("\x1b[31mRED\x1b[0m"),
            "RED",
        )

    def test_strip_display_ansi_removes_clear_screen(self):
        self.assertEqual(
            approval.strip_display_ansi("\x1b[2J\x1b[Hhello"),
            "hello",
        )

    def test_prompt_decision_answers(self):
        cases = (
            ("y", approval.ApprovalDecision.PROCEED),
            ("YES", approval.ApprovalDecision.PROCEED),
            ("n", approval.ApprovalDecision.SKIP),
            ("", approval.ApprovalDecision.SKIP),
            ("a", approval.ApprovalDecision.ABORT_ALL),
            ("Q", approval.ApprovalDecision.ABORT_ALL),
        )
        for answer, expected in cases:
            with self.subTest(answer=answer):
                self.assertIs(
                    approval.prompt_decision(lambda _prompt, value=answer: value),
                    expected,
                )

    def test_prompt_decision_retries_then_defaults_to_skip(self):
        answers = iter(["what", "?", "help", "..."])
        prompts = []

        def input_fn(prompt):
            prompts.append(prompt)
            return next(answers)

        self.assertIs(
            approval.prompt_decision(input_fn),
            approval.ApprovalDecision.SKIP,
        )
        self.assertEqual(len(prompts), 4)

    def test_prompt_decision_eof_aborts(self):
        def raise_eof(_prompt):
            raise EOFError

        self.assertIs(
            approval.prompt_decision(raise_eof),
            approval.ApprovalDecision.ABORT_ALL,
        )

    def test_prompt_decision_oserror_aborts(self):
        def raise_oserror(_prompt):
            raise OSError("stdin is captured")

        self.assertIs(
            approval.prompt_decision(raise_oserror),
            approval.ApprovalDecision.ABORT_ALL,
        )

    def test_approve_chunk_yes_to_all_bypasses(self):
        input_fn = mock.Mock()
        with mock.patch.object(approval, "render_chunk_review") as render:
            decision = approval.approve_chunk(
                **_approval_kwargs(),
                yes_to_all=True,
                input_fn=input_fn,
            )
        self.assertIs(decision, approval.ApprovalDecision.PROCEED)
        render.assert_not_called()
        input_fn.assert_not_called()

    def test_render_shows_envelope_verbatim(self):
        out = io.StringIO()
        approval.render_chunk_review(
            **_approval_kwargs(full_envelope="MARKER_XYZ_UNIQUE"),
            out=out,
        )
        self.assertIn("MARKER_XYZ_UNIQUE", out.getvalue())

    def test_render_strips_ansi_from_transcript(self):
        out = io.StringIO()
        approval.render_chunk_review(
            **_approval_kwargs(
                retrieved_chunks=[_chunk("before \x1b[31mRED\x1b[0m after")]
            ),
            out=out,
        )
        rendered = out.getvalue()
        self.assertNotIn("\x1b[31m", rendered)
        self.assertIn("before RED after", rendered)

    def test_render_full_envelope_ansi_stripped_but_send_bytes_untouched(self):
        full_envelope = "before \x1b[31mRED\x1b[0m after"
        original = full_envelope
        out = io.StringIO()
        approval.render_chunk_review(
            **_approval_kwargs(full_envelope=full_envelope),
            out=out,
        )
        rendered = out.getvalue()
        self.assertNotIn("\x1b[31m", rendered)
        self.assertIn("before RED after", rendered)
        self.assertIs(full_envelope, original)
        self.assertEqual(full_envelope, "before \x1b[31mRED\x1b[0m after")


if __name__ == "__main__":
    unittest.main()
