"""Title-suffix twin-meeting merge tests (D-164/PI-5 data hygiene)."""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path


_CORE_PROJECT_DIR = Path(__file__).resolve().parents[3]
_COUNCIL_NAVIGATOR_DIR = _CORE_PROJECT_DIR / "council_navigator"
_PARSERS_DIR = _COUNCIL_NAVIGATOR_DIR / "parsers"
for _path in (_CORE_PROJECT_DIR, _COUNCIL_NAVIGATOR_DIR, _PARSERS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from parsers import database

sys.modules["database"] = database


def _scratch_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE meetings (
            id INTEGER PRIMARY KEY,
            city_name TEXT, state TEXT,
            meeting_date TEXT, meeting_title TEXT,
            is_published INTEGER DEFAULT 0,
            public_id TEXT UNIQUE,
            updated_at TIMESTAMP
        );
        CREATE TABLE meeting_public_id_aliases (
            alias_public_id TEXT PRIMARY KEY,
            canonical_meeting_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE work_orders (
            id INTEGER PRIMARY KEY,
            meeting_id INTEGER NOT NULL UNIQUE,
            state TEXT
        );
        CREATE TABLE notebook_outputs (
            meeting_id INTEGER, output_type TEXT, content TEXT,
            UNIQUE(meeting_id, output_type)
        );
        CREATE TABLE quotes (
            id INTEGER PRIMARY KEY, meeting_id INTEGER, quote TEXT
        );
        """
    )
    return conn


def _add_meeting(conn, mid, title, date="2026-05-05", pub=0, pid=None):
    conn.execute(
        "INSERT INTO meetings (id, city_name, state, meeting_date,"
        " meeting_title, is_published, public_id) VALUES (?,?,?,?,?,?,?)",
        (mid, "Kingman", "Arizona", date, title, pub, pid or f"m_{'x' * 20}{mid:02d}"),
    )


class TwinMergeTests(unittest.TestCase):
    def test_basic_merge_keeps_published_suffixed_row_under_clean_title(self):
        conn = _scratch_conn()
        _add_meeting(conn, 1, "City Council - May 05, 2026", pub=1, pid="m_" + "a" * 22)
        _add_meeting(conn, 2, "City Council", pub=0, pid="m_" + "b" * 22)
        conn.execute("INSERT INTO work_orders (meeting_id, state) VALUES (1, 'complete')")
        conn.execute("INSERT INTO work_orders (meeting_id, state) VALUES (2, 'awaiting_video')")

        merged = database._merge_title_suffix_twin_meetings(conn)
        self.assertEqual(merged, 1)

        rows = conn.execute("SELECT * FROM meetings").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 1)
        self.assertEqual(rows[0]["meeting_title"], "City Council")
        self.assertEqual(rows[0]["is_published"], 1)

        wos = conn.execute("SELECT meeting_id, state FROM work_orders").fetchall()
        self.assertEqual(len(wos), 1)
        self.assertEqual((wos[0]["meeting_id"], wos[0]["state"]), (1, "complete"))

        alias = conn.execute(
            "SELECT canonical_meeting_id FROM meeting_public_id_aliases"
            " WHERE alias_public_id = ?",
            ("m_" + "b" * 22,),
        ).fetchone()
        self.assertEqual(alias["canonical_meeting_id"], 1)

        self.assertEqual(database._merge_title_suffix_twin_meetings(conn), 0)

    def test_keep_selection_flips_when_clean_row_is_published(self):
        conn = _scratch_conn()
        _add_meeting(conn, 1, "City Council - May 05, 2026", pub=0, pid="m_" + "a" * 22)
        _add_meeting(conn, 2, "City Council", pub=1, pid="m_" + "b" * 22)

        self.assertEqual(database._merge_title_suffix_twin_meetings(conn), 1)
        rows = conn.execute("SELECT * FROM meetings").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 2)
        self.assertEqual(rows[0]["meeting_title"], "City Council")
        alias = conn.execute(
            "SELECT canonical_meeting_id FROM meeting_public_id_aliases"
            " WHERE alias_public_id = ?",
            ("m_" + "a" * 22,),
        ).fetchone()
        self.assertEqual(alias["canonical_meeting_id"], 2)

    def test_both_processed_keeps_published_side_without_doubling_content(self):
        conn = _scratch_conn()
        _add_meeting(conn, 1, "City Council - May 05, 2026", pub=1, pid="m_" + "a" * 22)
        _add_meeting(conn, 2, "City Council", pub=0, pid="m_" + "b" * 22)
        conn.executemany(
            "INSERT INTO notebook_outputs (meeting_id, output_type, content)"
            " VALUES (?,?,?)",
            [(1, "synopsis", "keep"), (1, "key_decisions", "keep"),
             (2, "synopsis", "twin")],
        )
        conn.executemany(
            "INSERT INTO quotes (meeting_id, quote) VALUES (?,?)",
            [(1, "kq1"), (1, "kq2"), (2, "tq1")],
        )

        self.assertEqual(database._merge_title_suffix_twin_meetings(conn), 1)
        outputs = conn.execute(
            "SELECT meeting_id, output_type, content FROM notebook_outputs"
        ).fetchall()
        self.assertEqual(len(outputs), 2)
        self.assertTrue(all(o["meeting_id"] == 1 and o["content"] == "keep" for o in outputs))
        quotes = conn.execute("SELECT meeting_id FROM quotes").fetchall()
        self.assertEqual(len(quotes), 2)
        self.assertTrue(all(q["meeting_id"] == 1 for q in quotes))

    def test_content_repoints_when_keep_side_has_none(self):
        conn = _scratch_conn()
        _add_meeting(conn, 1, "City Council - May 05, 2026", pub=1, pid="m_" + "a" * 22)
        _add_meeting(conn, 2, "City Council", pub=0, pid="m_" + "b" * 22)
        conn.execute("INSERT INTO work_orders (meeting_id, state) VALUES (2, 'pending')")

        self.assertEqual(database._merge_title_suffix_twin_meetings(conn), 1)
        wos = conn.execute("SELECT meeting_id, state FROM work_orders").fetchall()
        self.assertEqual(len(wos), 1)
        self.assertEqual((wos[0]["meeting_id"], wos[0]["state"]), (1, "pending"))

    def test_venue_and_topic_suffixes_never_merge(self):
        conn = _scratch_conn()
        _add_meeting(conn, 1, "City Council - Zoom Video", pid="m_" + "a" * 22)
        _add_meeting(conn, 2, "City Council", pid="m_" + "b" * 22)
        _add_meeting(conn, 3, "Budget Workshop - Special Session",
                     date="2026-05-06", pid="m_" + "c" * 22)
        _add_meeting(conn, 4, "Budget Workshop", date="2026-05-06", pid="m_" + "d" * 22)

        self.assertEqual(database._merge_title_suffix_twin_meetings(conn), 0)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0], 4
        )

    def test_date_suffix_must_match_meeting_date(self):
        conn = _scratch_conn()
        _add_meeting(conn, 1, "City Council - May 05, 2026",
                     date="2026-05-06", pid="m_" + "a" * 22)
        _add_meeting(conn, 2, "City Council", date="2026-05-06", pid="m_" + "b" * 22)

        self.assertEqual(database._merge_title_suffix_twin_meetings(conn), 0)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0], 2
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
