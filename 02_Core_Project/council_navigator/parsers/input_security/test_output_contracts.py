"""D-164 output-contract registry and publication-floor tests."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


_CORE_PROJECT_DIR = Path(__file__).resolve().parents[3]
_COUNCIL_NAVIGATOR_DIR = _CORE_PROJECT_DIR / "council_navigator"
_PARSERS_DIR = _CORE_PROJECT_DIR / "council_navigator" / "parsers"
for _path in (_CORE_PROJECT_DIR, _COUNCIL_NAVIGATOR_DIR, _PARSERS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from parsers import database
from parsers import quote_align

# The worker's legacy backend seam imports this module as top-level
# ``database``. Keep the test on one module instance so database.py's existing
# initialization hook does not run twice during discovery.
sys.modules["database"] = database

from zspan_pipeline import worker
from zspan_pipeline.output_contracts import (
    CONTRIBUTION_CONTRACT,
    FLAGSHIP_PRODUCTION_CONTRACT,
    HONEST_EMPTY_OUTPUTS,
    PUBLICATION_CONTRACT,
)


_EXPECTED_FLAGSHIP_OUTPUTS = {
    "episode_tagline",
    "synopsis",
    "newsletter",
    "key_decisions",
    "community_calls_to_action",
    "whats_next",
    "council_sentiment",
    "tracked_claims",
}

_ALIGNMENT_ABSENT = object()


def _write_sidecar(
    preview_root: str,
    meeting_id: int,
    sidecar: str,
    payload: object,
) -> None:
    path = Path(preview_root) / f"m{meeting_id}_{sidecar}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")


def _cited_key_decisions(decision_count: int, cited: set[int]) -> str:
    seconds = (495, 1_330, 2_462, 3_330, 4_325)
    lines: list[str] = []
    for index in range(1, decision_count + 1):
        total = seconds[(index - 1) % len(seconds)]
        hours, remainder = divmod(total, 3_600)
        minutes, secs = divmod(remainder, 60)
        locator = f" [at {hours}:{minutes:02d}:{secs:02d}]" if index in cited else ""
        lines.append(f"{index}. Decision {index} sentence{locator}.")
    return "\n\n".join(lines)


def _excerpt_fixture(decision_count: int, *, legacy: bool = False):
    words: list[dict] = []
    alignment: list[dict] = []
    decisions: list[dict] = []
    for index in range(1, decision_count + 1):
        base = len(words)
        start = index * 1_000.0
        words.extend([
            {"word": f"item-{index}", "start": start, "end": start + 1.0},
            {"word": "introduced", "start": start + 1.0, "end": start + 2.0},
            {"word": "motion", "start": start + 400.0, "end": start + 401.0},
            {"word": "carried", "start": start + 401.0, "end": start + 402.0},
        ])
        item = {
            "matched_word_index": base,
            "best_candidate_end_seconds": start + 2.0,
        }
        action = {
            "matched_word_index": base + 2,
            "best_candidate_end_seconds": start + 402.0,
        }
        if not legacy:
            item["matched_end_word_index"] = base + 1
            action["matched_end_word_index"] = base + 3
        alignment.append({
            "output_index": index,
            "source": "two_part_quote",
            "item_evidence": item,
            "action_evidence": action,
        })
        spans = quote_align.materialize_transcript_excerpt(words, item, action)
        decisions.append({"index": index, "verbatim_spans": spans})
    sidecar = {
        "prose_output": _cited_key_decisions(
            decision_count, set(range(1, decision_count + 1)),
        ),
        "prose_list_count": decision_count,
        "citation_alignment": alignment,
        "decisions": decisions,
    }
    if not legacy:
        sidecar["citation_modality"] = quote_align.TRANSCRIPT_EXCERPT_MODALITY
    return words, sidecar


class _NoCloseConnection:
    """Keep a shared in-memory fixture alive across readiness sub-checks."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __getattr__(self, name: str) -> object:
        return getattr(self._conn, name)

    def close(self) -> None:
        return None


def _readiness_connection(
    meeting_id: int | None = 1,
    output_types: tuple[str, ...] = (),
    chunk_ranges: tuple[tuple[float, float], ...] = (
        (480.0, 540.0),
        (1_300.0, 1_370.0),
        (2_430.0, 2_500.0),
        (3_300.0, 3_370.0),
        (4_300.0, 4_360.0),
    ),
) -> _NoCloseConnection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE meetings (
            id INTEGER PRIMARY KEY,
            video_url TEXT
        );
        CREATE TABLE notebook_outputs (
            meeting_id INTEGER,
            output_type TEXT,
            content TEXT,
            error TEXT
        );
        CREATE TABLE quotes (
            meeting_id INTEGER
        );
        CREATE TABLE local_retrieval_chunks (
            meeting_id INTEGER,
            chunk_index INTEGER,
            text TEXT,
            start_seconds REAL,
            end_seconds REAL
        );
        """
    )
    if meeting_id is not None:
        conn.execute(
            "INSERT INTO meetings (id, video_url) VALUES (?, ?)",
            (meeting_id, "https://example.gov/meeting.mp4"),
        )
        conn.executemany(
            """
            INSERT INTO notebook_outputs
                (meeting_id, output_type, content, error)
            VALUES (?, ?, ?, NULL)
            """,
            [
                (meeting_id, output_type, "complete")
                for output_type in output_types
            ],
        )
        conn.executemany(
            """
            INSERT INTO local_retrieval_chunks
                (meeting_id, chunk_index, text, start_seconds, end_seconds)
            VALUES (?, ?, '', ?, ?)
            """,
            [
                (meeting_id, index, start, end)
                for index, (start, end) in enumerate(chunk_ranges)
            ],
        )
        conn.commit()
    return _NoCloseConnection(conn)


class OutputContractInvariantTests(unittest.TestCase):
    def test_publication_contract_is_producible(self):
        producible = FLAGSHIP_PRODUCTION_CONTRACT | {"transcript_words"}
        self.assertLessEqual(set(PUBLICATION_CONTRACT), producible)

    def test_honest_empty_outputs_are_not_floor_required(self):
        self.assertFalse(HONEST_EMPTY_OUTPUTS & set(PUBLICATION_CONTRACT))

    def test_display_cut_outputs_stay_produced_but_not_floor_required(self):
        display_cut = {"council_sentiment", "tracked_claims"}
        self.assertLessEqual(display_cut, FLAGSHIP_PRODUCTION_CONTRACT)
        self.assertFalse(display_cut & set(PUBLICATION_CONTRACT))

    def test_contribution_contract_is_flagship_producible(self):
        self.assertLessEqual(
            set(CONTRIBUTION_CONTRACT),
            FLAGSHIP_PRODUCTION_CONTRACT,
        )


class PublicationReadinessTests(unittest.TestCase):
    def test_exact_new_floor_is_publish_ready_without_display_cut_outputs(self):
        conn = _readiness_connection(1, PUBLICATION_CONTRACT)
        with tempfile.TemporaryDirectory() as tmp:
            _write_sidecar(
                tmp, 1, "decisions", {"prose_output": _cited_key_decisions(3, {1, 2, 3})}
            )
            with (
                mock.patch.dict(os.environ, {"ZSPAN_PREVIEW_ROOT": tmp}),
                mock.patch.object(database, "get_connection", return_value=conn),
            ):
                verdict = database.check_publish_readiness(1)

        self.assertTrue(verdict["ready"], verdict["reasons"])
        self.assertEqual(verdict["missing_outputs"], [])
        self.assertEqual(verdict["required_ok"], len(PUBLICATION_CONTRACT))
        self.assertEqual(verdict["required_total"], len(PUBLICATION_CONTRACT))
        self.assertTrue(verdict["citation_coverage"]["ok"])
        self.assertTrue(verdict["publishable"])
        self.assertEqual(verdict["publish_blockers"], [])

    def test_missing_meeting_has_publish_keys_and_is_not_publishable(self):
        conn = _readiness_connection(meeting_id=None)
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.dict(os.environ, {"ZSPAN_PREVIEW_ROOT": tmp}),
                mock.patch.object(database, "get_connection", return_value=conn),
            ):
                verdict = database.check_publish_readiness(404)

        self.assertIn("citation_coverage", verdict)
        self.assertIn("publishable", verdict)
        self.assertIn("publish_blockers", verdict)
        self.assertFalse(verdict["citation_coverage"]["ok"])
        self.assertFalse(verdict["publishable"])

    def test_ready_stays_true_when_uncited_decisions_block_publication(self):
        conn = _readiness_connection(1, PUBLICATION_CONTRACT)
        with tempfile.TemporaryDirectory() as tmp:
            _write_sidecar(
                tmp, 1, "decisions", {"prose_output": _cited_key_decisions(5, {1, 3, 4})}
            )
            with (
                mock.patch.dict(os.environ, {"ZSPAN_PREVIEW_ROOT": tmp}),
                mock.patch.object(database, "get_connection", return_value=conn),
            ):
                verdict = database.check_publish_readiness(1)

        self.assertTrue(verdict["ready"], verdict["reasons"])
        self.assertEqual(verdict["reasons"], [])
        self.assertEqual(verdict["missing_outputs"], [])
        self.assertFalse(verdict["publishable"])
        self.assertEqual(
            verdict["publish_blockers"],
            [
                "2 of 5 key decisions still have no citation — the record can't "
                "back them yet."
            ],
        )

    def test_quote_anchored_chunk_miss_is_publishable_observation(self):
        conn = _readiness_connection(2, PUBLICATION_CONTRACT)
        with tempfile.TemporaryDirectory() as tmp:
            _write_sidecar(
                tmp,
                2,
                "decisions",
                {
                    "prose_output": "1. Approved the item [at 3:25:00].",
                    "citation_alignment": [
                        {"output_index": 1, "source": "two_part_quote"}
                    ],
                },
            )
            with (
                mock.patch.dict(os.environ, {"ZSPAN_PREVIEW_ROOT": tmp}),
                mock.patch.object(database, "get_connection", return_value=conn),
            ):
                verdict = database.check_publish_readiness(2)

        self.assertTrue(verdict["ready"], verdict["reasons"])
        self.assertTrue(verdict["citation_coverage"]["ok"])
        self.assertTrue(verdict["publishable"])
        self.assertEqual(verdict["publish_blockers"], [])
        self.assertEqual(
            verdict["citation_coverage"]["citation_observations"][0]["reason"],
            "quote_anchored_outside_retrieved_chunks",
        )

    def test_fallback_chunk_miss_blocks_publication(self):
        conn = _readiness_connection(3, PUBLICATION_CONTRACT)
        with tempfile.TemporaryDirectory() as tmp:
            _write_sidecar(
                tmp,
                3,
                "decisions",
                {
                    "prose_output": "1. Approved the item [at 3:25:00].",
                    "citation_alignment": [
                        {
                            "output_index": 1,
                            "source": "outcome_signature_fallback",
                        }
                    ],
                },
            )
            with (
                mock.patch.dict(os.environ, {"ZSPAN_PREVIEW_ROOT": tmp}),
                mock.patch.object(database, "get_connection", return_value=conn),
            ):
                verdict = database.check_publish_readiness(3)

        self.assertTrue(verdict["ready"], verdict["reasons"])
        self.assertFalse(verdict["citation_coverage"]["ok"])
        self.assertFalse(verdict["publishable"])
        self.assertEqual(
            verdict["citation_coverage"]["unknown_citations"],
            ["[at 3:25:00]"],
        )
        self.assertEqual(len(verdict["publish_blockers"]), 1)


class CitationCoverageTests(unittest.TestCase):
    def _coverage(
        self,
        meeting_id: int,
        prose: str,
        *,
        routing: object | None = None,
        chunk_ranges: tuple[tuple[float, float], ...] | None = None,
        citation_alignment: object = _ALIGNMENT_ABSENT,
        sidecar_payload: dict | None = None,
        transcript_words: list[dict] | None = None,
    ) -> dict:
        ranges = chunk_ranges if chunk_ranges is not None else (
            (480.0, 540.0),
            (1_300.0, 1_370.0),
            (2_430.0, 2_500.0),
        )
        conn = _readiness_connection(meeting_id, (), ranges)
        with tempfile.TemporaryDirectory() as tmp:
            decisions_payload: dict[str, object] = (
                dict(sidecar_payload)
                if sidecar_payload is not None
                else {"prose_output": prose}
            )
            if citation_alignment is not _ALIGNMENT_ABSENT:
                decisions_payload["citation_alignment"] = citation_alignment
            _write_sidecar(tmp, meeting_id, "decisions", decisions_payload)
            if transcript_words is not None:
                conn.execute(
                    """
                    INSERT INTO notebook_outputs
                        (meeting_id, output_type, content, error)
                    VALUES (?, 'transcript_words', ?, NULL)
                    """,
                    (meeting_id, json.dumps({"words": transcript_words})),
                )
                conn.commit()
            if routing is not None:
                _write_sidecar(tmp, meeting_id, "routing", routing)
            with (
                mock.patch.dict(os.environ, {"ZSPAN_PREVIEW_ROOT": tmp}),
                mock.patch.object(database, "get_connection", return_value=conn),
            ):
                return database._citation_coverage(meeting_id)

    def test_fully_cited_decision_prose_is_covered(self):
        coverage = self._coverage(7, _cited_key_decisions(3, {1, 2, 3}))

        self.assertTrue(coverage["ok"])
        self.assertEqual(coverage["covered_indices"], [1, 2, 3])
        self.assertEqual(coverage["uncited_decisions"], [])
        self.assertEqual(coverage["unknown_citations"], [])

    def test_uncited_decisions_are_reported_from_prose(self):
        coverage = self._coverage(8, _cited_key_decisions(3, {1, 3}))

        self.assertFalse(coverage["ok"])
        self.assertEqual(coverage["covered_indices"], [1, 3])
        self.assertEqual(coverage["uncited_decisions"], [2])

    def test_zero_routed_discussion_quotes_does_not_affect_publishability(self):
        coverage = self._coverage(
            9,
            _cited_key_decisions(3, {1, 2, 3}),
            routing={"routing": [], "summary": {"decision_bound_count": 0}},
        )

        self.assertTrue(coverage["ok"])
        self.assertNotIn("routing_missing", coverage)

    def test_absent_routing_sidecar_does_not_affect_publishability(self):
        coverage = self._coverage(10, _cited_key_decisions(3, {1, 2, 3}))
        self.assertTrue(coverage["ok"])

    def test_explicit_no_decisions_is_fail_closed(self):
        coverage = self._coverage(11, "(No key decisions this meeting.)")

        self.assertFalse(coverage["ok"])
        self.assertEqual(coverage["decisions_total"], 0)
        self.assertTrue(coverage["no_decisions_extracted"])

    def test_locator_outside_retrieved_timed_range_is_rejected(self):
        coverage = self._coverage(
            12,
            "1. Approved the item [at 3:25:00].",
            citation_alignment=[
                {"output_index": 1, "source": "outcome_signature_fallback"}
            ],
        )

        self.assertFalse(coverage["ok"])
        self.assertEqual(coverage["unknown_citations"], ["[at 3:25:00]"])
        self.assertEqual(coverage["citation_observations"], [])

    def test_quote_anchored_locator_outside_chunks_is_observational(self):
        coverage = self._coverage(
            16,
            "1. Approved the item [at 3:25:00].",
            citation_alignment=[
                {"output_index": 1, "source": "two_part_quote"}
            ],
        )

        self.assertTrue(coverage["ok"])
        self.assertEqual(coverage["decisions_total"], 1)
        self.assertEqual(coverage["covered_indices"], [1])
        self.assertEqual(coverage["uncited_decisions"], [])
        self.assertEqual(coverage["unknown_citations"], [])
        self.assertEqual(
            coverage["citation_observations"][0]["reason"],
            "quote_anchored_outside_retrieved_chunks",
        )

    def test_legacy_sidecar_without_alignment_is_grandfathered_explicitly(self):
        with self.assertLogs(database.logger, level="WARNING") as logs:
            coverage = self._coverage(
                17,
                "1. Approved the item [at 3:25:00].",
            )

        self.assertTrue(coverage["ok"])
        self.assertEqual(coverage["unknown_citations"], [])
        self.assertEqual(
            coverage["citation_observations"][0]["reason"],
            "legacy_sidecar_without_citation_alignment",
        )
        self.assertTrue(
            any("legacy citation policy" in message for message in logs.output)
        )

    def test_legacy_alignment_without_source_is_grandfathered_explicitly(self):
        coverage = self._coverage(
            18,
            "1. Approved the item [at 3:25:00].",
            citation_alignment=[{"output_index": 1}],
        )

        self.assertTrue(coverage["ok"])
        self.assertEqual(
            coverage["citation_observations"][0]["reason"],
            "legacy_alignment_source_absent",
        )

    def test_missing_local_index_is_a_distinct_blocker(self):
        coverage = self._coverage(
            13,
            _cited_key_decisions(1, {1}),
            chunk_ranges=(),
        )

        self.assertFalse(coverage["ok"])
        self.assertTrue(coverage["index_missing"])

    def test_missing_or_unreadable_decisions_sidecar_fails_closed(self):
        conn = _readiness_connection(14)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "m14_decisions.json").write_text(
                "{not json", encoding="utf-8"
            )
            with (
                mock.patch.dict(os.environ, {"ZSPAN_PREVIEW_ROOT": tmp}),
                mock.patch.object(database, "get_connection", return_value=conn),
            ):
                coverage = database._citation_coverage(14)

        self.assertFalse(coverage["ok"])
        self.assertTrue(coverage["decisions_missing"])

    def test_new_modality_requires_exact_persisted_spans(self):
        words, sidecar = _excerpt_fixture(2)
        coverage = self._coverage(
            19,
            sidecar["prose_output"],
            sidecar_payload=sidecar,
            transcript_words=words,
        )
        self.assertTrue(coverage["ok"], coverage)
        self.assertEqual(coverage["covered_indices"], [1, 2])

        mutations = {
            "missing": lambda payload: payload["decisions"][0].pop("verbatim_spans"),
            "altered": lambda payload: payload["decisions"][0]["verbatim_spans"][0].__setitem__("text", "cleaned text"),
            "out_of_range": lambda payload: payload["decisions"][0]["verbatim_spans"][0].__setitem__("end_word_index", 999),
            "reversed": lambda payload: payload["decisions"][0]["verbatim_spans"][0].__setitem__("end_word_index", -1),
            "index_mismatch": lambda payload: payload["decisions"][0].__setitem__("index", 2),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                broken = json.loads(json.dumps(sidecar))
                mutate(broken)
                result = self._coverage(
                    20,
                    broken["prose_output"],
                    sidecar_payload=broken,
                    transcript_words=words,
                )
                self.assertFalse(result["ok"], result)
                self.assertTrue(result["span_validation_errors"], result)

    def test_new_modality_never_falls_back_to_valid_inline_locator(self):
        words, sidecar = _excerpt_fixture(1)
        sidecar["decisions"] = []
        coverage = self._coverage(
            21,
            sidecar["prose_output"],
            sidecar_payload=sidecar,
            transcript_words=words,
        )
        self.assertFalse(coverage["ok"])
        self.assertEqual(coverage["unknown_citations"], [])
        self.assertTrue(coverage["span_validation_errors"])

    def test_legacy_two_part_anchors_materialize_before_inline_gate(self):
        words, sidecar = _excerpt_fixture(2, legacy=True)
        sidecar["prose_output"] = "1. First.\n\n2. Second."
        coverage = self._coverage(
            22,
            sidecar["prose_output"],
            sidecar_payload=sidecar,
            transcript_words=words,
            chunk_ranges=(),
        )
        self.assertTrue(coverage["ok"], coverage)
        self.assertTrue(coverage["legacy_materialized"])
        self.assertFalse(coverage["index_missing"])

    def test_four_launch_meeting_legacy_fixtures_remain_publishable(self):
        launch_counts = {127696: 4, 127795: 4, 127900: 5, 127899: 1}
        for meeting_id, count in launch_counts.items():
            with self.subTest(meeting_id=meeting_id):
                words, sidecar = _excerpt_fixture(count, legacy=True)
                coverage = self._coverage(
                    meeting_id,
                    sidecar["prose_output"],
                    sidecar_payload=sidecar,
                    transcript_words=words,
                    chunk_ranges=(),
                )
                self.assertTrue(coverage["ok"], coverage)
                self.assertTrue(coverage["legacy_materialized"])
                self.assertEqual(coverage["covered_indices"], list(range(1, count + 1)))

    def test_unforeseen_exception_never_escapes(self):
        with (
            self.assertLogs(database.logger, level="ERROR"),
            mock.patch.object(
                database,
                "_preview_root_for_citation",
                side_effect=RuntimeError("structural surprise"),
            ),
        ):
            coverage = database._citation_coverage(15)

        self.assertFalse(coverage["ok"])
        self.assertTrue(coverage["malformed"])
        self.assertIsNone(coverage["decisions_total"])
        self.assertIn("index_missing", coverage)

class PublicationGateTests(unittest.TestCase):
    @staticmethod
    def _create_publish_database(directory: str) -> Path:
        database_path = Path(directory) / "publish.db"
        conn = sqlite3.connect(database_path)
        conn.executescript(
            """
            CREATE TABLE meetings (
                id INTEGER PRIMARY KEY,
                city_name TEXT,
                meeting_title TEXT,
                meeting_date TEXT,
                is_published INTEGER DEFAULT 0,
                published_at TEXT,
                published_by TEXT,
                publish_notes TEXT,
                updated_at TEXT
            );
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                email TEXT
            );
            CREATE TABLE notebook_outputs (
                id INTEGER PRIMARY KEY,
                meeting_id INTEGER NOT NULL,
                notebook_id TEXT NOT NULL,
                output_type TEXT NOT NULL
            );
            INSERT INTO users (id, email) VALUES (1, 'owner@example.test');
            INSERT INTO meetings (
                id, city_name, meeting_title, meeting_date, is_published,
                updated_at
            ) VALUES (
                1, 'Test City', 'Council Meeting', '2026-07-16', 0,
                CURRENT_TIMESTAMP
            );
            """
        )
        conn.commit()
        conn.close()
        return database_path

    @staticmethod
    def _open_publish_database(database_path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(database_path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_uncited_verdict_blocks_non_force_publish(self):
        verdict = {
            "ready": True,
            "publishable": False,
            "reasons": [],
            "publish_blockers": ["Citation coverage is incomplete."],
        }
        with mock.patch.object(
            database,
            "check_publish_readiness",
            return_value=verdict,
        ):
            with self.assertRaises(database.PublishNotReadyError) as raised:
                database.publish_meeting(
                    1, "operator", publisher_user_id=1, force=False
                )

        self.assertIs(raised.exception.verdict, verdict)
        self.assertIn("Citation coverage is incomplete.", str(raised.exception))

    def test_publishable_verdict_publishes(self):
        verdict = {
            "ready": True,
            "publishable": True,
            "reasons": [],
            "publish_blockers": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            database_path = self._create_publish_database(tmp)
            with (
                mock.patch.object(
                    database,
                    "check_publish_readiness",
                    return_value=verdict,
                ),
                mock.patch.object(
                    database,
                    "get_connection",
                    side_effect=lambda: self._open_publish_database(database_path),
                ),
            ):
                row = database.publish_meeting(
                    1, "operator", publisher_user_id=1, force=False
                )

        self.assertIsNotNone(row)
        self.assertEqual(row["is_published"], 1)
        self.assertEqual(row["published_by"], "Z-SPAN")

    def test_force_publish_bypasses_unpublishable_verdict(self):
        verdict = {
            "ready": True,
            "publishable": False,
            "reasons": [],
            "publish_blockers": ["Citation coverage is incomplete."],
        }
        with tempfile.TemporaryDirectory() as tmp:
            database_path = self._create_publish_database(tmp)
            with (
                mock.patch.object(
                    database,
                    "check_publish_readiness",
                    return_value=verdict,
                ) as readiness,
                mock.patch.object(
                    database,
                    "get_connection",
                    side_effect=lambda: self._open_publish_database(database_path),
                ),
            ):
                row = database.publish_meeting(
                    1, "operator", publisher_user_id=1, force=True
                )

        readiness.assert_not_called()
        self.assertIsNotNone(row)
        self.assertEqual(row["is_published"], 1)
        self.assertIsNone(row["publish_notes"])


class WorkerContractTests(unittest.TestCase):
    def test_worker_uses_exact_flagship_registry_object(self):
        self.assertIs(
            worker.V1_RAG3_OUTPUT_TYPES,
            FLAGSHIP_PRODUCTION_CONTRACT,
        )
        self.assertEqual(worker.V1_RAG3_OUTPUT_TYPES, _EXPECTED_FLAGSHIP_OUTPUTS)
        self.assertEqual(len(worker.V1_RAG3_OUTPUT_TYPES), 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
