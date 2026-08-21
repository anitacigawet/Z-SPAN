"""Offline coverage for transcript anomaly decisions and backfill."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np

from zspan_cli.zspan_cli import local_retrieval as core
from zspan_pipeline import local_vector_store, worker
from zspan_pipeline.scripts.backfill_transcript_quarantine import backfill_meeting
from zspan_pipeline.transcript_quarantine import (
    DEFAULT_ENTROPY_CONFIG,
    TRANSCRIPT_METADATA_KEY,
    apply_degenerate_span_quarantine,
    is_quarantined_word,
    profile_token_entropy,
)


MACHINE_PHRASE = "City of Lake Havasu Council Meeting Arizona".split()

LABELED_ENTROPY_WINDOWS = {
    127696: (
        ["you"] * 60,
        0.0,
        True,
    ),
    127900: (
        "Havasu City Arizona".split()
        + "City of Lake Havasu".split() * 14
        + ["City"],
        2.086735649746,
        True,
    ),
    127899: (
        ("City of Lake Havasu City Council Meeting in Lake Havasu City Arizona ")
        .split()
        * 5,
        2.855388542208,
        True,
    ),
    109650: (
        """that, you would, that, that's what I, I'm not, I'm not so much,
        I'm not, I'm not, I, you know, but I'm going to say, but I'm, I'm
        going to do you, and I, I'd say, I'm, so I'm, you know, that I, you
        know, that I, but I'm not, that's my, but I'm just, I, you know, meh,
        that,""".split(),
        4.296404087557,
        False,
    ),
}


def _timed_words(tokens: list[str], *, machine_timing: bool) -> list[dict]:
    words: list[dict] = []
    cursor = 0.0
    phrase_length = len(MACHINE_PHRASE)
    for index, token in enumerate(tokens):
        if machine_timing and index and index % phrase_length == 0:
            cursor += 10.0
        words.append({"word": token, "start": cursor, "end": cursor + 0.2})
        cursor += 0.4
    return words


def _machine_transcript() -> dict:
    tokens = ["ordinary", "opening"] + MACHINE_PHRASE * 4 + ["real", "business"]
    words = _timed_words(tokens, machine_timing=True)
    # Keep the timing gaps inside the repeated region, not before it.
    for index, word in enumerate(words):
        if index < 2:
            word["start"] = index * 0.4
            word["end"] = index * 0.4 + 0.2
        else:
            shifted = _timed_words(tokens[2:], machine_timing=True)[index - 2]
            word["start"] = shifted["start"] + 1.0
            word["end"] = shifted["end"] + 1.0
    return {"words": words, "duration_seconds": words[-1]["end"]}


class DetectorTests(unittest.TestCase):
    def test_entropy_scores_four_labeled_regions(self):
        for meeting_id, (tokens, expected_entropy, should_fire) in (
            LABELED_ENTROPY_WINDOWS.items()
        ):
            with self.subTest(meeting_id=meeting_id):
                self.assertEqual(len(tokens), 60)
                evidence = profile_token_entropy(
                    _timed_words(tokens, machine_timing=False)
                )

                self.assertEqual(evidence["windows_evaluated"], 1)
                self.assertAlmostEqual(
                    evidence["min_entropy_bits"], expected_entropy, places=5
                )
                self.assertEqual(
                    evidence["profile"][0]["signal_fired"], should_fire
                )
                self.assertEqual(bool(evidence["regions"]), should_fire)
                self.assertEqual(
                    evidence["thresholds"]["low_entropy_threshold_bits"],
                    DEFAULT_ENTROPY_CONFIG.low_entropy_threshold_bits,
                )

    def test_machine_repetition_is_annotated_without_changing_raw_words(self):
        transcript = _machine_transcript()
        raw_fields = [dict(word) for word in transcript["words"]]

        result = apply_degenerate_span_quarantine(transcript)

        self.assertTrue(result.detector_ran)
        self.assertEqual(result.quarantined_word_count, len(MACHINE_PHRASE) * 4)
        self.assertEqual(len(result.spans), 1)
        self.assertEqual(result.spans[0]["signals_fired"], ["repetition"])
        self.assertEqual(
            result.spans[0]["decision"], "quarantine_repetition"
        )
        for before, after in zip(raw_fields, transcript["words"]):
            self.assertEqual(after["word"], before["word"])
            self.assertEqual(after["start"], before["start"])
            self.assertEqual(after["end"], before["end"])
        quarantined = [
            word for word in transcript["words"] if is_quarantined_word(word)
        ]
        self.assertEqual(len(quarantined), len(MACHINE_PHRASE) * 4)
        self.assertEqual(
            transcript[TRANSCRIPT_METADATA_KEY]["status"], "completed"
        )

    def test_m109650_shaped_two_copy_stumble_remains_retrievable(self):
        tokens = LABELED_ENTROPY_WINDOWS[109650][0]
        transcript = {"words": _timed_words(tokens, machine_timing=False)}

        result = apply_degenerate_span_quarantine(transcript)

        self.assertTrue(result.detector_ran)
        self.assertEqual(result.quarantined_word_count, 0)
        self.assertEqual(result.entropy_only_region_count, 0)
        self.assertFalse(
            any(is_quarantined_word(word) for word in transcript["words"])
        )
        self.assertEqual(
            transcript[TRANSCRIPT_METADATA_KEY]["decision_summary"],
            {
                "corroborated_repetition_spans": 0,
                "repetition_only_spans": 0,
                "entropy_only_review_regions": 0,
                "review_required": False,
            },
        )

    def test_entropy_only_hit_is_flagged_without_quarantine(self):
        transcript = {
            "words": _timed_words(["you"] * 60, machine_timing=False),
        }

        result = apply_degenerate_span_quarantine(transcript)

        self.assertEqual(result.quarantined_word_count, 0)
        self.assertEqual(result.corroborated_span_count, 0)
        self.assertEqual(result.entropy_only_region_count, 1)
        self.assertEqual(result.entropy_regions[0]["min_entropy_bits"], 0.0)
        self.assertEqual(
            result.entropy_regions[0]["decision"], "flag_for_review"
        )
        self.assertFalse(
            any(is_quarantined_word(word) for word in transcript["words"])
        )

    def test_spatial_agreement_marks_high_confidence_quarantine(self):
        transcript = {
            "words": _timed_words(["you"] * 60, machine_timing=True),
        }

        result = apply_degenerate_span_quarantine(transcript)

        self.assertEqual(result.corroborated_span_count, 1)
        self.assertEqual(result.entropy_only_region_count, 0)
        self.assertEqual(
            result.spans[0]["signals_fired"],
            ["repetition", "low_token_entropy"],
        )
        self.assertEqual(
            result.spans[0]["decision"], "quarantine_high_confidence"
        )
        self.assertTrue(
            all(is_quarantined_word(word) for word in transcript["words"])
        )

        first_pass = json.dumps(transcript, sort_keys=True)
        second = apply_degenerate_span_quarantine(transcript)
        self.assertFalse(second.changed)
        self.assertEqual(json.dumps(transcript, sort_keys=True), first_pass)


class BackfillAndIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "meetings_cache.db"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE notebook_outputs (
                    meeting_id INTEGER NOT NULL,
                    output_type TEXT NOT NULL,
                    content TEXT,
                    error TEXT,
                    UNIQUE(meeting_id, output_type)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO notebook_outputs (
                    meeting_id, output_type, content, error
                ) VALUES (77, 'transcript_words', ?, NULL)
                """,
                (json.dumps(_machine_transcript()),),
            )

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _vectors(texts, *, progress):
        _ = progress
        vectors = np.zeros((len(texts), core.VECTOR_DIM), dtype=np.float32)
        for index in range(len(texts)):
            vectors[index, index] = 1.0
        return vectors

    def _index(self, meeting_id: int, *, db_path: Path | str) -> int:
        return worker.index_meeting_locally(
            meeting_id,
            db_path=db_path,
            token_counter=lambda _word: 1,
            exact_tokenizer=True,
            embedding_fn=self._vectors,
        )

    def test_quarantined_text_is_absent_from_every_retrieval_chunk(self):
        self._index(77, db_path=self.db_path)

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT text, start_seconds, end_seconds
                FROM local_retrieval_chunks
                WHERE meeting_id = 77
                ORDER BY chunk_index
                """
            ).fetchall()
        indexed_text = " ".join(row[0] for row in rows)
        self.assertNotIn("City of Lake Havasu", indexed_text)
        self.assertIn("ordinary opening", indexed_text)
        self.assertIn("real business", indexed_text)
        self.assertEqual(len(rows), 2)
        self.assertLess(rows[0][2], rows[1][1])

    def test_backfill_is_idempotent_and_does_not_reindex_twice(self):
        index_calls: list[int] = []

        def counted_index(meeting_id: int, *, db_path: Path | str) -> int:
            index_calls.append(meeting_id)
            return self._index(meeting_id, db_path=db_path)

        first = backfill_meeting(
            77,
            db_path=self.db_path,
            dry_run=False,
            index_fn=counted_index,
        )
        with sqlite3.connect(self.db_path) as conn:
            content_after_first = conn.execute(
                """
                SELECT content FROM notebook_outputs
                WHERE meeting_id=77 AND output_type='transcript_words'
                """
            ).fetchone()[0]
        second = backfill_meeting(
            77,
            db_path=self.db_path,
            dry_run=False,
            index_fn=counted_index,
        )
        with sqlite3.connect(self.db_path) as conn:
            content_after_second = conn.execute(
                """
                SELECT content FROM notebook_outputs
                WHERE meeting_id=77 AND output_type='transcript_words'
                """
            ).fetchone()[0]

        self.assertTrue(first.transcript_changed)
        self.assertTrue(first.reindexed)
        self.assertFalse(second.transcript_changed)
        self.assertFalse(second.index_was_stale)
        self.assertFalse(second.reindexed)
        self.assertEqual(index_calls, [77])
        self.assertEqual(content_after_first, content_after_second)


if __name__ == "__main__":
    unittest.main()
