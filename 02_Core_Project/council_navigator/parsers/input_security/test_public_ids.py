"""D-164 opaque meeting public-ID migration and resolver tests."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


_CORE_PROJECT_DIR = Path(__file__).resolve().parents[3]
_PARSERS_DIR = _CORE_PROJECT_DIR / "council_navigator" / "parsers"
for _path in (_CORE_PROJECT_DIR, _PARSERS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from parsers import database

sys.modules["database"] = database


def _new_backfill_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE meetings (
            id INTEGER PRIMARY KEY,
            public_id TEXT
        );
        CREATE UNIQUE INDEX idx_meetings_public_id ON meetings(public_id);
        CREATE TABLE meeting_public_id_aliases (
            alias_public_id TEXT PRIMARY KEY,
            canonical_meeting_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    return conn


class PublicIdGrammarTests(unittest.TestCase):
    def test_generated_ids_match_the_public_grammar(self):
        generated = [database.generate_public_id() for _ in range(100)]

        self.assertEqual(len(generated), len(set(generated)))
        for public_id in generated:
            self.assertEqual(len(public_id), 24)
            self.assertTrue(public_id.startswith("m_"))
            self.assertIsNotNone(database.PUBLIC_ID_RE.fullmatch(public_id))


class PublicIdBackfillTests(unittest.TestCase):
    def test_backfill_is_unique_idempotent_and_immutable(self):
        conn = _new_backfill_db()
        self.addCleanup(conn.close)
        conn.executemany(
            "INSERT INTO meetings (id, public_id) VALUES (?, NULL)",
            [(1,), (2,), (3,), (4,)],
        )

        self.assertEqual(database.ensure_meeting_public_ids(conn), 4)
        before = conn.execute(
            "SELECT id, public_id FROM meetings ORDER BY id"
        ).fetchall()
        self.assertEqual(len({row[1] for row in before}), 4)
        self.assertTrue(all(database.PUBLIC_ID_RE.fullmatch(row[1]) for row in before))

        self.assertEqual(database.ensure_meeting_public_ids(conn), 0)
        after = conn.execute(
            "SELECT id, public_id FROM meetings ORDER BY id"
        ).fetchall()
        self.assertEqual(after, before)

    def test_backfill_never_rewrites_a_non_null_public_id(self):
        conn = _new_backfill_db()
        self.addCleanup(conn.close)
        existing = "m_" + "A" * 22
        conn.executemany(
            "INSERT INTO meetings (id, public_id) VALUES (?, ?)",
            [(1, existing), (2, None)],
        )

        self.assertEqual(database.ensure_meeting_public_ids(conn), 1)
        rows = conn.execute(
            "SELECT id, public_id FROM meetings ORDER BY id"
        ).fetchall()
        self.assertEqual(rows[0][1], existing)
        self.assertNotEqual(rows[1][1], existing)


class PublicIdInsertTests(unittest.TestCase):
    def test_both_meeting_insert_paths_assign_and_preserve_public_ids(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "meetings.db")
            with mock.patch.object(database, "DB_PATH", db_path):
                database.init_db()

                scraped = {
                    "meeting_title": "Public ID Cache Test",
                    "meeting_date": "2026-07-13",
                    "meeting_time": "4:00 PM",
                    "meeting_status": "Scheduled",
                }
                verdict = SimpleNamespace(
                    accepted=[scraped],
                    rejected_count=0,
                    status="accepted",
                )
                with mock.patch(
                    "ingest_validator.validate_listing", return_value=verdict
                ):
                    database.cache_meetings(
                        "Public ID Test City", "Test County", [scraped], state="AZ"
                    )
                    database.cache_meetings(
                        "Public ID Test City", "Test County", [scraped], state="AZ"
                    )

                conn = database.get_connection()
                cache_ids = conn.execute(
                    "SELECT public_id FROM meetings WHERE meeting_title = ?",
                    (scraped["meeting_title"],),
                ).fetchall()
                conn.close()
                self.assertEqual(len(cache_ids), 1)
                cache_public_id = cache_ids[0][0]
                self.assertIsNotNone(database.PUBLIC_ID_RE.fullmatch(cache_public_id))

                payload = {
                    "id": 9001,
                    "city_name": "Flagship Test City",
                    "county": "Test County",
                    "state": "AZ",
                    "meeting_title": "Public ID Flagship Test",
                    "meeting_date": "2026-07-14",
                    "meeting_time": "5:00 PM",
                    "is_published": 0,
                }
                database.upsert_meeting_from_flagship_payload(payload)
                conn = database.get_connection()
                first = conn.execute(
                    "SELECT public_id FROM meetings WHERE id = 9001"
                ).fetchone()[0]
                conn.close()

                payload["meeting_time"] = "5:30 PM"
                database.upsert_meeting_from_flagship_payload(payload)
                conn = database.get_connection()
                second = conn.execute(
                    "SELECT public_id FROM meetings WHERE id = 9001"
                ).fetchone()[0]
                conn.close()

                self.assertIsNotNone(database.PUBLIC_ID_RE.fullmatch(first))
                self.assertEqual(second, first)


class PublicIdAliasTests(unittest.TestCase):
    def test_duplicate_merge_creates_alias_and_resolver_returns_canonical(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "meetings.db")
            with mock.patch.object(database, "DB_PATH", db_path):
                database.init_db()
                conn = database.get_connection()
                conn.execute("DROP INDEX idx_meetings_natural_key")
                city_id = conn.execute(
                    """
                    INSERT INTO cities (name, county, state)
                    VALUES ('Alias Test City', 'Test County', 'AZ')
                    """
                ).lastrowid
                canonical_public_id = database.generate_public_id()
                alias_public_id = database.generate_public_id()
                meeting_values = (
                    city_id,
                    "Alias Test City",
                    "Test County",
                    "AZ",
                    "Alias Test Meeting",
                    "2026-07-15",
                )
                conn.execute(
                    """
                    INSERT INTO meetings
                        (public_id, city_id, city_name, county, state,
                         meeting_title, meeting_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (canonical_public_id, *meeting_values),
                )
                conn.execute(
                    """
                    INSERT INTO meetings
                        (public_id, city_id, city_name, county, state,
                         meeting_title, meeting_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (alias_public_id, *meeting_values),
                )
                conn.commit()
                conn.close()

                database.init_db()

                canonical = database.get_meeting_public_record(canonical_public_id)
                resolved_alias = database.get_meeting_public_record(alias_public_id)

                self.assertIsNotNone(canonical)
                self.assertIsNotNone(resolved_alias)
                self.assertEqual(canonical["public_id"], canonical_public_id)
                self.assertEqual(canonical["canonical_public_id"], canonical_public_id)
                self.assertEqual(resolved_alias["public_id"], canonical_public_id)
                self.assertEqual(
                    resolved_alias["canonical_public_id"], canonical_public_id
                )

                conn = database.get_connection()
                alias_row = conn.execute(
                    """
                    SELECT canonical_meeting_id
                    FROM meeting_public_id_aliases
                    WHERE alias_public_id = ?
                    """,
                    (alias_public_id,),
                ).fetchone()
                canonical_id = conn.execute(
                    "SELECT id FROM meetings WHERE public_id = ?",
                    (canonical_public_id,),
                ).fetchone()[0]
                conn.close()
                self.assertEqual(alias_row[0], canonical_id)

        with mock.patch.object(database, "get_connection") as get_connection:
            self.assertIsNone(database.get_meeting_public_record("not-a-public-id"))
            get_connection.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
