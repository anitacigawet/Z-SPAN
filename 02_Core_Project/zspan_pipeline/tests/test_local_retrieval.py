"""Offline tests for the shared flagship/CLI local retrieval path."""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from zspan_cli.zspan_cli import local_retrieval as core
from zspan_pipeline import local_vector_store, qdrant_synthesizer


class _FakeEmbedder:
    def embed(self, texts):
        for index, _text in enumerate(texts):
            vector = np.zeros(core.VECTOR_DIM, dtype=np.float32)
            vector[index % 2] = float(index + 2)
            yield vector


class SharedCoreTests(unittest.TestCase):
    def test_normalized_384_vectors_chunking_and_cosine_top_k(self):
        words = [
            {"word": token, "start": float(i), "end": float(i) + 0.5}
            for i, token in enumerate("alpha beta gamma delta epsilon".split())
        ]
        chunks = core.chunk_transcript(
            words,
            token_counter=lambda _word: 1,
            exact=True,
            target_tokens=3,
            overlap_tokens=1,
        )
        self.assertEqual(chunks[0].text, "alpha beta gamma")
        self.assertEqual((chunks[0].word_start_index, chunks[0].word_end_index), (0, 3))

        previous = core._EMBEDDER
        core._EMBEDDER = _FakeEmbedder()
        try:
            matrix = core.embed_texts([chunk.text for chunk in chunks], progress=lambda _m: None)
        finally:
            core._EMBEDDER = previous
        self.assertEqual(matrix.shape, (len(chunks), 384))
        np.testing.assert_allclose(np.linalg.norm(matrix, axis=1), np.ones(len(chunks)))
        top = core.top_k_cosine(matrix, matrix[1], k=2)
        self.assertEqual(top[0][0], 1)
        self.assertAlmostEqual(top[0][1], 1.0)

    def test_constants_match_retired_flagship_indexer_pins(self):
        script = (
            Path(__file__).resolve().parents[2]
            / "council_navigator"
            / "parsers"
            / "scripts"
            / "index_meeting_to_qdrant.py"
        )
        spec = importlib.util.spec_from_file_location("flagship_indexer_pins", script)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self.assertEqual(core.EMBED_MODEL_NAME, module.EMBED_MODEL_NAME)
        self.assertEqual(core.VECTOR_DIM, module.VECTOR_DIM)
        self.assertEqual(core.CHUNK_TOKEN_TARGET, module.CHUNK_TOKEN_TARGET)
        self.assertEqual(core.CHUNK_TOKEN_OVERLAP, module.CHUNK_TOKEN_OVERLAP)


class LocalFlagshipStoreTests(unittest.TestCase):
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

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _vectors(texts, *, progress):
        _ = progress
        matrix = np.zeros((len(texts), core.VECTOR_DIM), dtype=np.float32)
        for index in range(len(texts)):
            matrix[index, index % core.VECTOR_DIM] = 1.0
        return matrix

    def _save_transcript(self, words):
        payload = json.dumps({"words": words, "duration_seconds": len(words)})
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO notebook_outputs (meeting_id, output_type, content, error)
                VALUES (7, 'transcript_words', ?, NULL)
                ON CONFLICT(meeting_id, output_type)
                DO UPDATE SET content=excluded.content, error=NULL
                """,
                (payload,),
            )

    def _seed_complete_index(self, words):
        transcript = {"words": words, "duration_seconds": len(words)}
        chunks = [
            core.Chunk(2, "third", 20.0, 29.0),
            core.Chunk(0, "first", 0.0, 9.0),
            core.Chunk(1, "second", 10.0, 19.0),
        ]
        vectors = np.zeros((3, core.VECTOR_DIM), dtype=np.float32)
        local_vector_store.replace_meeting_index(
            7,
            chunks,
            vectors,
            [None, None, None],
            transcript_sha256=local_vector_store.transcript_hash(transcript),
            embed_model=core.EMBED_MODEL_NAME,
            vector_dim=core.VECTOR_DIM,
            chunk_token_target=core.CHUNK_TOKEN_TARGET,
            chunk_token_overlap=core.CHUNK_TOKEN_OVERLAP,
            chunker_version=core.CHUNKER_VERSION,
            db_path=self.db_path,
        )

    def test_index_meeting_locally_atomically_replaces_without_duplicates(self):
        with mock.patch.dict(os.environ, {"ZSPAN_DB_PATH": str(self.db_path)}):
            from zspan_pipeline import worker

        words = [
            {
                "word": f"w{i}",
                "start": float(i),
                "end": float(i) + 0.5,
                "speaker_id": "SPEAKER_01" if i < 225 else "SPEAKER_02",
            }
            for i in range(450)
        ]
        self._save_transcript(words)
        count_first = worker.index_meeting_locally(
            7,
            db_path=self.db_path,
            token_counter=lambda _word: 1,
            exact_tokenizer=True,
            embedding_fn=self._vectors,
        )
        count_second = worker.index_meeting_locally(
            7,
            db_path=self.db_path,
            token_counter=lambda _word: 1,
            exact_tokenizer=True,
            embedding_fn=self._vectors,
        )
        self.assertEqual(count_first, count_second)
        with sqlite3.connect(self.db_path) as conn:
            stored_count = conn.execute(
                "SELECT COUNT(*) FROM local_retrieval_chunks WHERE meeting_id=7"
            ).fetchone()[0]
            index_count = conn.execute(
                "SELECT COUNT(*) FROM local_retrieval_indexes WHERE meeting_id=7"
            ).fetchone()[0]
        self.assertEqual(stored_count, count_second)
        self.assertEqual(index_count, 1)

        self._save_transcript(words[:20])
        replaced_count = worker.index_meeting_locally(
            7,
            db_path=self.db_path,
            token_counter=lambda _word: 1,
            exact_tokenizer=True,
            embedding_fn=self._vectors,
        )
        self.assertEqual(replaced_count, 1)
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM local_retrieval_chunks WHERE meeting_id=7"
                ).fetchone()[0],
                1,
            )

    def test_retrieve_chunks_preserves_speaker_turns_without_network(self):
        chunks = [
            core.Chunk(0, "opening", 0.0, 4.0),
            core.Chunk(1, "water contract vote", 5.0, 12.0),
        ]
        vectors = np.zeros((2, core.VECTOR_DIM), dtype=np.float32)
        vectors[0, 1] = 1.0
        vectors[1, 0] = 1.0
        turns = [
            None,
            [{
                "speaker_label": "SPEAKER_07",
                "start": 5.0,
                "end": 12.0,
                "text": "water contract vote",
            }],
        ]
        local_vector_store.replace_meeting_index(
            7,
            chunks,
            vectors,
            turns,
            transcript_sha256="seeded",
            embed_model=core.EMBED_MODEL_NAME,
            vector_dim=core.VECTOR_DIM,
            chunk_token_target=core.CHUNK_TOKEN_TARGET,
            chunk_token_overlap=core.CHUNK_TOKEN_OVERLAP,
            chunker_version=core.CHUNKER_VERSION,
            db_path=self.db_path,
        )

        query = np.zeros(core.VECTOR_DIM, dtype=np.float32)
        query[0] = 1.0
        with mock.patch.dict(os.environ, {"ZSPAN_DB_PATH": str(self.db_path)}), mock.patch.object(
            core, "embed_query", return_value=query,
        ):
            found = qdrant_synthesizer.retrieve_chunks(
                7, "contract", top_k=1, host="ignored", port=9999, token="ignored"
            )
        self.assertEqual(len(found), 1)
        self.assertIsInstance(found[0], qdrant_synthesizer.RetrievedChunk)
        self.assertEqual(found[0].chunk_index, 1)
        self.assertEqual(found[0].body, "water contract vote")
        self.assertEqual(found[0].speaker_turns, turns[1])

    def test_complete_loader_returns_every_chunk_once_in_chronological_order(self):
        words = [
            {"word": word, "start": float(index), "end": float(index + 1)}
            for index, word in enumerate(("first", "second", "third"))
        ]
        self._save_transcript(words)
        self._seed_complete_index(words)

        chunks = qdrant_synthesizer.load_complete_meeting_chunks(
            7,
            db_path=self.db_path,
        )

        self.assertEqual([chunk.chunk_index for chunk in chunks], [0, 1, 2])
        self.assertEqual([chunk.body for chunk in chunks], ["first", "second", "third"])

    def test_complete_loader_rejects_index_stale_against_canonical_transcript(self):
        original_words = [
            {"word": "original", "start": 0.0, "end": 1.0},
        ]
        self._save_transcript(original_words)
        self._seed_complete_index(original_words)
        self._save_transcript([
            {"word": "changed", "start": 0.0, "end": 1.0},
        ])

        with self.assertRaisesRegex(RuntimeError, "index is stale"):
            qdrant_synthesizer.load_complete_meeting_chunks(
                7,
                db_path=self.db_path,
            )


if __name__ == "__main__":
    unittest.main()
