"""Prompt-boundary coverage for both live prompt-loader consumers."""

from pathlib import Path
import unittest

from zspan_pipeline import (
    fetcher,
    qdrant_quote_extractor,
    quote_router_runner,
    rag_search,
    report_generator,
)
from zspan_pipeline.neutrality_audit.extraction import load_votes_instructions
from zspan_pipeline.prompt_loader import (
    MODEL_CONTENT_END,
    MODEL_CONTENT_START,
    load_prompt_with_meta,
)
from zspan_pipeline.qdrant_synthesizer import load_canonical_prompt
from zspan_pipeline.scripts import build_review_queue


PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
INSTRUCTION_HEADINGS = (
    "## Instructions",
    "## STRUCTURAL GUIDANCE",
    "## DESIGN BLOCK",
)
NON_PROMPT_DOCS = {"PROMPT_REVIEW_LEDGER.md", "README.md"}


class PromptLoaderTests(unittest.TestCase):
    def test_key_decisions_delivers_round_four_anchor(self):
        _, body = load_prompt_with_meta("key_decisions.md")

        self.assertIn("## Round 2 addendum", body)
        self.assertIn("## Round 3 addendum", body)
        self.assertIn("## Round 4 addendum", body)
        self.assertIn("`item_quote`", body)
        self.assertIn("`action_quote`", body)
        self.assertNotIn(MODEL_CONTENT_END, body)

    def test_explicit_boundary_excludes_operator_material(self):
        _, community_body = load_prompt_with_meta(
            "community_calls_to_action.md"
        )
        _, truth_packet_body = load_prompt_with_meta("truth_packet.md")
        _, financial_body = load_prompt_with_meta("financial_infographic.md")
        _, report_body = load_prompt_with_meta("report_decisions.md")

        self.assertNotIn("What James should refine", community_body)
        self.assertNotIn("Why these fields (for the prompt-reader", truth_packet_body)
        self.assertNotIn("What James should refine", truth_packet_body)
        self.assertNotIn("Migration / wiring context", truth_packet_body)
        self.assertTrue(financial_body.startswith("```"))
        self.assertNotIn("What to iterate on", financial_body)
        self.assertNotIn("What James should refine", report_body)

        sidecar_body = load_canonical_prompt("community_calls_to_action")
        self.assertNotIn("What James should refine", sidecar_body)
        self.assertNotIn(MODEL_CONTENT_END, sidecar_body)

    def test_every_instruction_file_has_boundary_and_loads(self):
        instruction_files = []
        for path in sorted(PROMPTS_DIR.glob("*.md")):
            raw = path.read_text(encoding="utf-8")
            if any(
                line.startswith(INSTRUCTION_HEADINGS)
                for line in raw.splitlines()
            ):
                instruction_files.append(path)
                self.assertEqual(
                    raw.count(MODEL_CONTENT_END),
                    1,
                    f"{path.name} must have exactly one model-content boundary",
                )
                _, body = load_prompt_with_meta(path.name)
                self.assertTrue(body.strip(), f"{path.name} loaded an empty prompt")
                self.assertNotIn(MODEL_CONTENT_END, body)

        self.assertTrue(instruction_files)

    def test_every_prompt_file_has_explicit_end_boundary(self):
        prompt_files = [
            path
            for path in sorted(PROMPTS_DIR.glob("*.md"))
            if path.name not in NON_PROMPT_DOCS
        ]
        for path in prompt_files:
            raw = path.read_text(encoding="utf-8")
            self.assertEqual(
                raw.count(MODEL_CONTENT_END),
                1,
                f"{path.name} must have exactly one model-content boundary",
            )

        financial_raw = (PROMPTS_DIR / "financial_infographic.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(financial_raw.count(MODEL_CONTENT_START), 1)

        suggested_meta, suggested_body = load_prompt_with_meta(
            "suggested_questions.md"
        )
        self.assertIsInstance(suggested_meta.get("questions"), list)
        self.assertFalse(suggested_body)

    def test_loader_returns_well_formed_text_for_every_markdown_file(self):
        for path in sorted(PROMPTS_DIR.glob("*.md")):
            with self.subTest(prompt=path.name):
                meta, body = load_prompt_with_meta(path.name)
                self.assertIsInstance(meta, dict)
                self.assertIsInstance(body, str)
                if path.name == "suggested_questions.md":
                    self.assertIsInstance(meta.get("questions"), list)
                    self.assertFalse(body)
                else:
                    self.assertTrue(body.strip())

    def test_fetcher_registry_receives_well_formed_prompts(self):
        for output_type, (prompt_filename, strategy) in (
            fetcher.OUTPUT_TYPE_REGISTRY.items()
        ):
            if prompt_filename is None:
                self.assertEqual(strategy, "transcript_words")
                continue
            with self.subTest(output_type=output_type):
                meta, body = fetcher.load_prompt_with_meta(prompt_filename)
                self.assertIsInstance(meta, dict)
                if strategy == "qdrant_synthesize_multi":
                    self.assertIsInstance(meta.get("questions"), list)
                else:
                    self.assertTrue(body.strip())
                self.assertNotIn(MODEL_CONTENT_END, body)

    def test_neutrality_consumer_receives_complete_votes_prompt(self):
        body = load_votes_instructions()

        self.assertTrue(body.startswith("You are scanning a council-meeting"))
        self.assertIn('"vote_result"', body)
        self.assertIn("### Mental check before emitting each vote", body)
        self.assertNotIn(MODEL_CONTENT_END, body)

    def test_specialized_prompt_loaders_honor_explicit_boundaries(self):
        loaded = (
            qdrant_quote_extractor._load_extraction_prompt(),
            quote_router_runner._load_prompt(),
            rag_search.load_prompt_template(),
            report_generator.load_section_prompt("report_decisions")[0],
            build_review_queue._load_batch_prompt_template(),
            build_review_queue._load_prompt_template(),
        )
        for body in loaded:
            self.assertNotIn(MODEL_CONTENT_START, body)
            self.assertNotIn(MODEL_CONTENT_END, body)

        self.assertNotIn("What James should refine", loaded[3])


if __name__ == "__main__":
    unittest.main(verbosity=2)
