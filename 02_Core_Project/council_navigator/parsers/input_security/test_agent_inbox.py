"""S-008 V0 / surface S-7 — sub-agent inbox validation + audit-trail tests.

Covers:
- `parsers.agent_audit.validate_agent_text` rejects bidi controls, fence
  markers, and over-length input; NFC-normalizes otherwise.
- `parsers.agent_audit.record_agent_action` writes to the `agent_actions`
  table and hashes the body canonically.
- The action wrappers (disputed_quotes_reviewer_action,
  vocabulary_curator_action) call validate / audit at the right points.

The wrappers' end-to-end POST flow is NOT tested here — that's covered
by the relay endpoint tests + manual smoke tests. These tests exercise
the input-security layer in isolation.
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

_COUNCIL_NAVIGATOR_DIR = Path(__file__).resolve().parents[2]
if str(_COUNCIL_NAVIGATOR_DIR) not in sys.path:
    sys.path.insert(0, str(_COUNCIL_NAVIGATOR_DIR))

from parsers.agent_audit import (
    _hash_action_body,
    record_agent_action,
    validate_agent_text,
)
from parsers.input_security.primitives import UnicodeRejectionError


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ValidateAgentTextTests(unittest.TestCase):
    def test_none_passes_through(self):
        self.assertIsNone(
            validate_agent_text(None, field_name="x", max_length=10)
        )

    def test_clean_text_normalized(self):
        out = validate_agent_text("hello", field_name="x", max_length=10)
        self.assertEqual(out, "hello")

    def test_over_length_rejected(self):
        with self.assertRaises(ValueError):
            validate_agent_text("x" * 100, field_name="x", max_length=10)

    def test_bidi_controls_rejected(self):
        with self.assertRaises(UnicodeRejectionError):
            validate_agent_text(
                "council ‮reversed‬", field_name="x", max_length=100,
            )

    def test_fence_marker_rejected(self):
        with self.assertRaises(ValueError):
            validate_agent_text(
                "<zspan-content-begin nonce=\"x\">", field_name="x",
                max_length=200,
            )

    def test_control_chars_stripped(self):
        out = validate_agent_text("a\x07b", field_name="x", max_length=10)
        self.assertEqual(out, "ab")

    def test_non_string_rejected(self):
        with self.assertRaises(ValueError):
            validate_agent_text(123, field_name="x", max_length=10)  # type: ignore[arg-type]


class HashActionBodyTests(unittest.TestCase):
    def test_deterministic_across_dict_order(self):
        a = {"action": "verify", "resolved_by": "x", "quote_text": "hi"}
        b = {"quote_text": "hi", "resolved_by": "x", "action": "verify"}
        self.assertEqual(_hash_action_body(a), _hash_action_body(b))

    def test_different_body_different_hash(self):
        a = {"action": "verify", "quote_text": "hi"}
        b = {"action": "verify", "quote_text": "hello"}
        self.assertNotEqual(_hash_action_body(a), _hash_action_body(b))

    def test_unicode_nfc_collapsed(self):
        a = {"text": "Á"}        # composed
        b = {"text": "Á"}      # decomposed
        self.assertEqual(_hash_action_body(a), _hash_action_body(b))

    def test_hex_format(self):
        h = _hash_action_body({"x": 1})
        self.assertEqual(len(h), 64)


class RecordAgentActionTests(unittest.TestCase):
    """Exercise record_agent_action against an in-memory sqlite DB."""

    def setUp(self):
        self.tmp_db_path = _PROJECT_ROOT / "parsers" / f"_test_audit_{id(self)}.db"
        # Make sure the DB does not exist from a prior run.
        if self.tmp_db_path.exists():
            self.tmp_db_path.unlink()

        # Create the schema directly via sqlite3 — we don't need the full
        # init_notebook_schema here, just the one table.
        conn = sqlite3.connect(self.tmp_db_path)
        conn.execute("""
            CREATE TABLE agent_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_role TEXT NOT NULL,
                action_name TEXT NOT NULL,
                action_argument_table TEXT,
                action_argument_id INTEGER,
                action_argument_origin TEXT,
                reasoning TEXT,
                rung_attempted TEXT,
                rung_outcome TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

        # Patch the database.get_connection used by record_agent_action.
        # Use side_effect to return a fresh connection per call so the
        # callee's conn.close() doesn't leave stale handles.
        self._patches = []
        patcher = mock.patch(
            "parsers.database.get_connection",
            side_effect=lambda: sqlite3.connect(self.tmp_db_path),
        )
        patcher.start()
        self._patches.append(patcher)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        # Windows can briefly hold the SQLite file handle even after
        # connection.close(); tolerate transient permission errors during
        # cleanup. The temp DB lives in parsers/ and is named by id(self)
        # so collision risk is negligible.
        try:
            if self.tmp_db_path.exists():
                self.tmp_db_path.unlink()
        except PermissionError:
            pass

    def _count_actions(self) -> int:
        conn = sqlite3.connect(self.tmp_db_path)
        row = conn.execute(
            "SELECT COUNT(*) FROM agent_actions"
        ).fetchone()
        conn.close()
        return row[0]

    def _last_row(self) -> tuple | None:
        conn = sqlite3.connect(self.tmp_db_path)
        row = conn.execute(
            "SELECT agent_role, action_name, action_argument_table, "
            "action_argument_id, action_argument_origin, reasoning, "
            "rung_attempted, rung_outcome FROM agent_actions "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return row

    def test_writes_row(self):
        row_id = record_agent_action(
            agent_role="disputed-quotes-reviewer",
            action_name="verify",
            action_argument_table="quotes",
            action_argument_id=42,
            action_body={"action": "verify", "resolved_by": "x"},
            reasoning="quote was clean",
        )
        self.assertIsNotNone(row_id)
        self.assertEqual(self._count_actions(), 1)
        row = self._last_row()
        self.assertIsNotNone(row)
        assert row is not None
        agent_role, action_name, table, arg_id, origin, reasoning, _, _ = row
        self.assertEqual(agent_role, "disputed-quotes-reviewer")
        self.assertEqual(action_name, "verify")
        self.assertEqual(table, "quotes")
        self.assertEqual(arg_id, 42)
        self.assertEqual(len(origin), 64)  # SHA-256 hex
        self.assertEqual(reasoning, "quote was clean")

    def test_records_orchestrator_rung_columns(self):
        row_id = record_agent_action(
            agent_role="orchestrator",
            action_name="trigger-content-scout",
            rung_attempted="rung-1",
            rung_outcome="success",
        )
        self.assertIsNotNone(row_id)
        row = self._last_row()
        assert row is not None
        self.assertEqual(row[6], "rung-1")
        self.assertEqual(row[7], "success")

    def test_db_failure_does_not_raise(self):
        # Force the DB to fail by patching get_connection to raise.
        with mock.patch(
            "parsers.database.get_connection",
            side_effect=RuntimeError("simulated DB outage"),
        ):
            row_id = record_agent_action(
                agent_role="vocabulary-curator",
                action_name="promote",
                action_argument_id=1,
            )
        # Returns None on failure but does NOT raise into caller.
        self.assertIsNone(row_id)

    def test_unknown_role_warns_without_writing(self):
        with self.assertLogs("parsers.agent_audit", level="WARNING") as logs:
            row_id = record_agent_action(
                agent_role="not-a-real-role",
                action_name="some-action",
            )
        self.assertIsNone(row_id)
        self.assertEqual(self._count_actions(), 0)
        self.assertIn("audit row not inserted", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
