"""Repeat-safety tests for the institutional role-identity migration."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


_PARSERS_DIR = Path(__file__).resolve().parents[1]
_COUNCIL_NAVIGATOR_DIR = _PARSERS_DIR.parent
for _path in (_COUNCIL_NAVIGATOR_DIR, _PARSERS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from parsers import database


_MIGRATION_PATH = (
    _PARSERS_DIR / "scripts" / "migrations" / "20260722_zspan_role_identity.py"
)
_SPEC = importlib.util.spec_from_file_location("zspan_role_identity_migration", _MIGRATION_PATH)
assert _SPEC and _SPEC.loader
_MIGRATION = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MIGRATION)


class RoleIdentityMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = str(Path(self.temp_dir.name) / "migration.db")
        with mock.patch.object(database, "DB_PATH", self.database_path):
            database.init_db()
            conn = database.get_connection()
            try:
                city_id = conn.execute(
                    "INSERT INTO cities (name, county, state) VALUES (?, ?, ?)",
                    ("Migration City", "Test County", "Arizona"),
                ).lastrowid
                owner_email = "legacy.owner@example.test"
                self.user_id = conn.execute(
                    """
                    INSERT INTO users (google_sub, email, display_name)
                    VALUES ('migration-owner', ?, 'Legacy Owner')
                    """,
                    (owner_email,),
                ).lastrowid
                conn.execute(
                    """
                    INSERT INTO meetings (
                        id, city_id, city_name, county, state, meeting_title,
                        meeting_date, meeting_status, is_published,
                        published_at, published_by, publish_notes
                    ) VALUES (
                        801, ?, 'Migration City', 'Test County', 'Arizona',
                        'Migration Meeting', '2026-07-20', 'Scheduled', 1,
                        '2026-07-21 10:00:00', ?,
                        'Reviewed by Legacy Alias and Verifier Alias'
                    )
                    """,
                    (city_id, owner_email),
                )
                self.work_order_id = conn.execute(
                    """
                    INSERT INTO work_orders (
                        meeting_id, state, approved_at, approved_by
                    ) VALUES (801, 'completed', '2026-07-21 09:00:00', ?)
                    """,
                    (owner_email,),
                ).lastrowid
                conn.execute(
                    """
                    INSERT INTO quote_verifications (
                        work_order_id, meeting_id, quote_id, verified_at,
                        verified_by
                    ) VALUES (?, 801, 'quote-migration',
                              '2026-07-21 09:05:00', 'Verifier Alias')
                    """,
                    (self.work_order_id,),
                )
                conn.execute(
                    """
                    INSERT INTO meetings (
                        id, city_id, city_name, county, state, meeting_title,
                        meeting_date, meeting_status, published_by
                    ) VALUES (
                        802, ?, 'Migration City', 'Test County', 'Arizona',
                        'Legacy Token Source', '2026-07-19', 'Scheduled',
                        'Legacy Alias'
                    )
                    """,
                    (city_id,),
                )
                conn.commit()
            finally:
                conn.close()

    def _snapshot(self) -> dict:
        import sqlite3

        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        try:
            return {
                "counts": tuple(
                    conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in (
                        "meetings",
                        "work_orders",
                        "quote_verifications",
                        "operator_review_events",
                        "users",
                    )
                ),
                "meetings": [
                    tuple(row)
                    for row in conn.execute(
                        """
                        SELECT id, published_at, published_by, publish_notes,
                               created_at, updated_at
                        FROM meetings ORDER BY id
                        """
                    ).fetchall()
                ],
                "work_orders": [
                    tuple(row)
                    for row in conn.execute(
                        """
                        SELECT id, approved_at, approved_by, created_at, updated_at
                        FROM work_orders ORDER BY id
                        """
                    ).fetchall()
                ],
                "verifications": [
                    tuple(row)
                    for row in conn.execute(
                        """
                        SELECT id, verified_at, verified_by
                        FROM quote_verifications ORDER BY id
                        """
                    ).fetchall()
                ],
                "events": [
                    tuple(row)
                    for row in conn.execute(
                        """
                        SELECT event_key, action, meeting_id, work_order_id,
                               actor_user_id, occurred_at, created_at
                        FROM operator_review_events ORDER BY id
                        """
                    ).fetchall()
                ],
                "users": [
                    tuple(row)
                    for row in conn.execute(
                        """
                        SELECT id, google_sub, email, display_name, created_at,
                               last_seen_at
                        FROM users ORDER BY id
                        """
                    ).fetchall()
                ],
            }
        finally:
            conn.close()

    def test_migration_is_repeat_safe_and_preserves_timestamps(self):
        users_before = self._snapshot()["users"]
        first_report = _MIGRATION.migrate(self.database_path)
        after_first = self._snapshot()
        second_report = _MIGRATION.migrate(self.database_path)
        after_second = self._snapshot()

        self.assertEqual(first_report["events_inserted"], 2)
        self.assertEqual(second_report["events_inserted"], 0)
        self.assertEqual(after_first, after_second)
        self.assertEqual(after_second["users"], users_before)
        self.assertTrue(
            all(row[2] == "Z-SPAN" for row in after_second["meetings"])
        )
        self.assertEqual(after_second["work_orders"][0][2], "Z-SPAN")
        self.assertEqual(after_second["verifications"][0][2], "Z-SPAN")
        self.assertIn("Z-SPAN", after_second["meetings"][0][3])
        self.assertNotIn("Legacy Alias", after_second["meetings"][0][3])
        self.assertNotIn("Verifier Alias", after_second["meetings"][0][3])
        self.assertEqual(
            {event[4] for event in after_second["events"]}, {self.user_id}
        )


if __name__ == "__main__":
    unittest.main()
