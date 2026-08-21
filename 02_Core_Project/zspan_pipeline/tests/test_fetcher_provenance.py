"""Focused tests for provenance recording at the production fetcher seam."""

from __future__ import annotations

import atexit
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_IMPORT_TEMP_DIR = tempfile.TemporaryDirectory()
atexit.register(_IMPORT_TEMP_DIR.cleanup)
_IMPORT_DB_PATH = Path(_IMPORT_TEMP_DIR.name) / "import-isolation.db"
with mock.patch.dict(os.environ, {"ZSPAN_DB_PATH": str(_IMPORT_DB_PATH)}):
    from zspan_pipeline import citation_validator, fetcher, qdrant_synthesizer


def _chunk(chunk_index: int) -> qdrant_synthesizer.RetrievedChunk:
    return qdrant_synthesizer.RetrievedChunk(
        score=0.9,
        body=f"chunk {chunk_index}",
        chunk_index=chunk_index,
        start_seconds=float(chunk_index),
        end_seconds=float(chunk_index + 1),
        meeting_id=42,
        city="Kingman",
        county="Mohave",
        state="Arizona",
    )


def _transcript_words() -> list[dict]:
    return [
        {"word": "alpha", "start": 3.0, "end": 3.2},
        {"word": "beta", "start": 3.2, "end": 3.4},
        {"word": "gamma", "start": 3.4, "end": 3.6},
    ]


def _transcript_cache_row(words: list[dict] | None = None) -> dict:
    return {"content": json.dumps({"words": words or _transcript_words()})}


def _generation(
    content: str,
    *,
    model_id: str = qdrant_synthesizer.FLAGSHIP_MODEL_ID,
) -> qdrant_synthesizer.GenerationResult:
    return qdrant_synthesizer.GenerationResult(
        content=content,
        model_id=model_id,
        attempts=(
            qdrant_synthesizer.GenerationAttempt(
                "anthropic" if model_id.startswith("claude-") else "google",
                model_id,
                None,
            ),
        ),
    )


def _split_synopsis_anchor_audit(content: str) -> tuple[str, dict]:
    marker = "\n\n<!-- synopsis_anchor_audit v1\n"
    prose, encoded = content.rsplit(marker, 1)
    payload_text, suffix = encoded.rsplit("\naudit -->", 1)
    if suffix:
        raise AssertionError("unexpected content after synopsis anchor audit block")
    return prose, json.loads(payload_text)


class FetcherProvenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_exact_sent_prompt_model_and_chunk_indices(self) -> None:
        chunks = [_chunk(3), _chunk(8)]
        exact_prompt = "exact production prompt\nwith retrieved context"

        with (
            mock.patch.object(
                qdrant_synthesizer,
                "load_complete_meeting_chunks",
                return_value=chunks,
            ),
            mock.patch.object(
                qdrant_synthesizer,
                "build_synthesis_prompt",
                return_value=exact_prompt,
            ),
            mock.patch.object(
                qdrant_synthesizer,
                "generate_with_fallback",
                return_value=_generation(
                    "synthesized output",
                    model_id=qdrant_synthesizer.GEMINI_PRIMARY_MODEL_ID,
                ),
            ) as generate,
            mock.patch.object(
                qdrant_synthesizer,
                "record_synthesis_provenance",
            ) as record,
            mock.patch.object(
                fetcher,
                "_maybe_apply_city_corrections",
                side_effect=lambda _output_type, _meeting_id, answer: answer,
            ),
            mock.patch.object(
                fetcher,
                "is_output_already_present",
                return_value=_transcript_cache_row(),
            ),
            mock.patch.object(fetcher, "save_notebook_output") as save_output,
            mock.patch.object(fetcher, "_maybe_persist_member_output"),
        ):
            result = await fetcher._fetch_qdrant(
                42,
                "notebook-42",
                "synopsis",
                "synopsis.md",
                "canonical instructions",
            )

        self.assertEqual(result["status"], "ok")
        generate.assert_called_once_with(exact_prompt)
        record.assert_called_once_with(
            meeting_id=42,
            output_type="synopsis",
            prompt=exact_prompt,
            model_id=qdrant_synthesizer.GEMINI_PRIMARY_MODEL_ID,
            retrieved_chunk_ids=[3, 8],
            evidence_mode="complete_transcript",
            attempts=generate.return_value.attempts,
        )
        self.assertEqual(
            result["model_id"],
            qdrant_synthesizer.GEMINI_PRIMARY_MODEL_ID,
        )
        self.assertEqual(
            save_output.call_args.kwargs["prompt_version"],
            "v1-rag-3-gemini-3.1-pro-high",
        )

    async def test_recording_failure_does_not_fail_synthesis(self) -> None:
        with (
            mock.patch.object(
                qdrant_synthesizer,
                "load_complete_meeting_chunks",
                return_value=[_chunk(5)],
            ),
            mock.patch.object(
                qdrant_synthesizer,
                "build_synthesis_prompt",
                return_value="prompt",
            ),
            mock.patch.object(
                qdrant_synthesizer,
                "generate_with_fallback",
                return_value=_generation("synthesized output"),
            ),
            mock.patch.object(
                qdrant_synthesizer,
                "record_synthesis_provenance",
                side_effect=OSError("preview unavailable"),
            ),
            mock.patch.object(
                fetcher,
                "_maybe_apply_city_corrections",
                side_effect=lambda _output_type, _meeting_id, answer: answer,
            ),
            mock.patch.object(
                fetcher,
                "is_output_already_present",
                return_value=_transcript_cache_row(),
            ),
            mock.patch.object(fetcher, "save_notebook_output"),
            mock.patch.object(fetcher, "_maybe_persist_member_output"),
            self.assertLogs(fetcher.logger, level="WARNING") as logs,
        ):
            result = await fetcher._fetch_qdrant(
                42,
                "notebook-42",
                "synopsis",
                "synopsis.md",
                "canonical instructions",
            )

        self.assertEqual(result["status"], "ok")
        self.assertTrue(
            any("Could not record synthesis provenance" in line for line in logs.output)
        )


class FetcherQueryRetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_question_path_keeps_semantic_retrieval(self) -> None:
        with (
            mock.patch.object(
                qdrant_synthesizer,
                "retrieve_chunks",
                return_value=[_chunk(7)],
            ) as retrieve,
            mock.patch.object(
                qdrant_synthesizer,
                "load_complete_meeting_chunks",
            ) as load_complete,
            mock.patch.object(
                qdrant_synthesizer,
                "synthesize_via_claude_p",
                return_value="The council approved the contract.",
            ),
            mock.patch.object(fetcher, "_meeting_city", return_value=None),
            mock.patch.object(fetcher, "save_notebook_output"),
        ):
            result = await fetcher._fetch_qdrant_multi(
                42,
                "notebook-42",
                "suggested_questions",
                "suggested_questions.md",
                ["What happened to the contract?"],
            )

        self.assertEqual(result["status"], "ok")
        retrieve.assert_called_once_with(42, "What happened to the contract?")
        load_complete.assert_not_called()


class FetcherSynopsisAnchorTests(unittest.IsolatedAsyncioTestCase):
    async def _run_synopsis(
        self,
        raw_answer: str,
        *,
        resolution: citation_validator.VerbatimAnchorResolution | None = None,
        resolver_side_effect=None,
        corrector=None,
    ) -> dict:
        chunks = [_chunk(3)]
        words = _transcript_words()
        correction = corrector or (
            lambda _output_type, _meeting_id, answer: answer
        )
        with (
            mock.patch.object(
                qdrant_synthesizer,
                "load_complete_meeting_chunks",
                return_value=chunks,
            ),
            mock.patch.object(
                qdrant_synthesizer,
                "build_synthesis_prompt",
                return_value="prompt",
            ),
            mock.patch.object(
                qdrant_synthesizer,
                "generate_with_fallback",
                return_value=_generation(raw_answer),
            ),
            mock.patch.object(qdrant_synthesizer, "record_synthesis_provenance"),
            mock.patch.object(
                fetcher,
                "is_output_already_present",
                return_value=_transcript_cache_row(words),
            ) as load_output,
            mock.patch.object(
                citation_validator,
                "resolve_inline_verbatim_anchors",
                return_value=resolution,
                side_effect=resolver_side_effect,
            ) as resolve,
            mock.patch.object(
                fetcher,
                "_maybe_apply_city_corrections",
                side_effect=correction,
            ) as apply_corrections,
            mock.patch.object(fetcher, "save_notebook_output") as save_output,
            mock.patch.object(fetcher, "_maybe_persist_member_output"),
        ):
            result = await fetcher._fetch_qdrant(
                42,
                "notebook-42",
                "synopsis",
                "synopsis.md",
                "canonical instructions",
            )

        load_output.assert_called_once_with(42, "transcript_words")
        return {
            "result": result,
            "stored": save_output.call_args.kwargs["content"],
            "resolve": resolve,
            "apply_corrections": apply_corrections,
            "chunks": chunks,
            "words": words,
        }

    async def test_synopsis_all_success_stores_rewritten_text_with_audit_block(
        self,
    ) -> None:
        raw = (
            'First fact [at "alpha beta gamma"]. '
            'Second fact [at "delta epsilon zeta"]. '
            'Third fact [at "eta theta iota"].'
        )
        rewritten = (
            "First fact [at 0:00:03]. Second fact [at 0:01:04]. "
            "Third fact [at 0:02:05]."
        )
        aligned = tuple(
            {
                "ordinal": ordinal,
                "quote": quote,
                "canonical_citation": citation,
            }
            for ordinal, (quote, citation) in enumerate(
                (
                    ("alpha beta gamma", "[at 0:00:03]"),
                    ("delta epsilon zeta", "[at 0:01:04]"),
                    ("eta theta iota", "[at 0:02:05]"),
                ),
                start=1,
            )
        )
        resolution = citation_validator.VerbatimAnchorResolution(
            text=rewritten,
            state="resolved",
            anchors_total=3,
            aligned=aligned,
            failures=(),
        )

        run = await self._run_synopsis(raw, resolution=resolution)

        prose, audit = _split_synopsis_anchor_audit(run["stored"])
        self.assertEqual(run["result"]["status"], "ok")
        self.assertEqual(prose, rewritten)
        self.assertEqual(audit["resolution_state"], "resolved")
        self.assertEqual(audit["anchors_total"], 3)
        self.assertEqual(len(audit["aligned"]), 3)
        self.assertEqual(audit["failures"], [])
        run["resolve"].assert_called_once_with(raw, run["chunks"], run["words"])

    async def test_synopsis_one_anchor_failure_stores_raw_text_with_audit_block(
        self,
    ) -> None:
        raw = (
            'Supported fact [at "alpha beta gamma"]. '
            'Unsupported fact [at "words not in chunks"].'
        )
        resolution = citation_validator.VerbatimAnchorResolution(
            text=raw,
            state="degraded",
            anchors_total=2,
            aligned=({"ordinal": 1, "quote": "alpha beta gamma"},),
            failures=(
                {
                    "ordinal": 2,
                    "quote": "words not in chunks",
                    "reason": "not_in_retrieved_chunks",
                },
            ),
        )

        run = await self._run_synopsis(raw, resolution=resolution)

        prose, audit = _split_synopsis_anchor_audit(run["stored"])
        self.assertEqual(prose, raw)
        self.assertEqual(audit["resolution_state"], "degraded")
        self.assertEqual(audit["anchors_total"], 2)
        self.assertEqual(len(audit["aligned"]), 1)
        self.assertEqual(len(audit["failures"]), 1)
        self.assertEqual(
            audit["failures"][0]["reason"],
            "not_in_retrieved_chunks",
        )

    async def test_synopsis_zero_anchor_stores_raw_text_with_nonconforming_audit_block(
        self,
    ) -> None:
        raw = "A synopsis with no inline evidence anchors."
        resolution = citation_validator.VerbatimAnchorResolution(
            text=raw,
            state="nonconforming",
            anchors_total=0,
            aligned=(),
            failures=(),
        )

        run = await self._run_synopsis(raw, resolution=resolution)

        prose, audit = _split_synopsis_anchor_audit(run["stored"])
        self.assertEqual(prose, raw)
        self.assertEqual(audit["resolution_state"], "nonconforming")
        self.assertEqual(audit["anchors_total"], 0)
        self.assertEqual(audit["failures"], [])

    async def test_synopsis_city_corrections_apply_after_anchor_capture(self) -> None:
        raw = (
            'Annie Divine approved the item '
            '[at "Annie Divine approved the item"].'
        )
        rewritten = "Annie Divine approved the item [at 0:00:03]."
        resolution = citation_validator.VerbatimAnchorResolution(
            text=rewritten,
            state="resolved",
            anchors_total=1,
            aligned=(
                {
                    "ordinal": 1,
                    "quote": "Annie Divine approved the item",
                    "canonical_citation": "[at 0:00:03]",
                },
            ),
            failures=(),
        )
        events: list[tuple[str, str]] = []

        def resolve(text, _chunks, _words):
            events.append(("resolve", text))
            return resolution

        def correct(_output_type, _meeting_id, text):
            events.append(("correct", text))
            return text.replace("Annie Divine", "Andy Devine")

        run = await self._run_synopsis(
            raw,
            resolution=resolution,
            resolver_side_effect=resolve,
            corrector=correct,
        )

        prose, audit = _split_synopsis_anchor_audit(run["stored"])
        self.assertEqual(
            events,
            [("resolve", raw), ("correct", rewritten)],
        )
        self.assertEqual(prose, "Andy Devine approved the item [at 0:00:03].")
        self.assertEqual(
            audit["aligned"][0]["quote"],
            "Annie Divine approved the item",
        )

    async def test_synopsis_alignment_failure_never_raises(self) -> None:
        raw = 'Raw synopsis [at "alpha beta gamma"].'
        with self.assertLogs(fetcher.logger, level="WARNING") as logs:
            run = await self._run_synopsis(
                raw,
                resolver_side_effect=RuntimeError("resolver exploded"),
            )

        prose, audit = _split_synopsis_anchor_audit(run["stored"])
        self.assertEqual(run["result"]["status"], "ok")
        self.assertEqual(prose, raw)
        self.assertEqual(audit["resolution_state"], "uncheckable")
        self.assertEqual(audit["anchors_total"], 1)
        self.assertEqual(
            audit["failures"][0]["reason"],
            "resolver_internal_error",
        )
        self.assertTrue(
            any(
                "synopsis anchor resolution failed unexpectedly" in line
                for line in logs.output
            )
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
