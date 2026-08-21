#!/usr/bin/env python3.11
"""
Unit tests for the S-010 ingestion governor (`ingestion_governor.py`).

The governor is the pacing/rate brain of the low-hum machine: it decides whether
the operation is under today's ceiling and what's next to flow. That math is
safety-relevant (S-008 cares about it) and load-bearing for the autonomous
operator, so it's worth pinning. These tests use an in-memory SQLite fixture —
no real DB, no network, no new dependency (stdlib `unittest`, matching the
`test_all_parsers.py` script precedent).

Run:
    cd 02_Core_Project/council_navigator/parsers
    python3.11 test_ingestion_governor.py
"""
from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta

import ingestion_governor as gov

CITY = "Testville"


def _build_fixture() -> sqlite3.Connection:
    """An in-memory DB with only the columns the governor SELECTs.

    Scenario for CITY:
      m1  completed       (old completed_at)         -> processed
      m2  completed       (completed today)          -> processed + processed_today
      m3  pending         (2026-04-20)               -> ready_to_process
      m6  pending         (2026-05-01, newest)       -> ready_to_process + next_meeting
      m4  awaiting_video  (2026-04-10)               -> needs_video_url
      m5  skipped_too_old (2025-06-01)               -> excluded (NOT a candidate)
    Plus one OTHER-city completed row to prove the city filter holds.
    => processed=2, ready=2, needs_url=1, candidate=3, excluded=1, processed_today=1
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE meetings (id INTEGER PRIMARY KEY, city_name TEXT, "
        "meeting_date TEXT, meeting_title TEXT)"
    )
    cur.execute(
        "CREATE TABLE work_orders (id INTEGER PRIMARY KEY, meeting_id INTEGER, "
        "state TEXT, completed_at TEXT)"
    )

    today = datetime.utcnow().date().isoformat()
    old = (datetime.utcnow().date() - timedelta(days=40)).isoformat()

    meetings = [
        (1, CITY, "2026-03-01", "Old Council"),
        (2, CITY, "2026-04-01", "Recent Council"),
        (3, CITY, "2026-04-20", "Ready Council"),
        (6, CITY, "2026-05-01", "Newest Ready Council"),
        (4, CITY, "2026-04-10", "Awaiting Council"),
        (5, CITY, "2025-06-01", "Too-Old Council"),
        (99, "Otherville", "2026-04-15", "Other City Council"),
    ]
    work_orders = [
        (1, 1, "completed", f"{old} 12:00:00"),
        (2, 2, "completed", f"{today} 12:00:00"),
        (3, 3, "pending", None),
        (4, 6, "pending", None),
        (5, 4, "awaiting_video", None),
        (6, 5, "skipped_too_old", None),
        (7, 99, "completed", f"{today} 12:00:00"),  # other city — must be filtered out
    ]
    cur.executemany("INSERT INTO meetings VALUES (?,?,?,?)", meetings)
    cur.executemany("INSERT INTO work_orders VALUES (?,?,?,?)", work_orders)
    conn.commit()
    return conn


class GovernorBucketsTest(unittest.TestCase):
    def setUp(self):
        self.conn = _build_fixture()

    def tearDown(self):
        self.conn.close()

    def _meter(self, compute=1.0, review=1.0, city=CITY):
        return gov.compute_city_metering(city, compute, review, conn=self.conn)

    def test_progress_buckets(self):
        p = self._meter()["progress"]
        self.assertEqual(p["processed"], 2)
        self.assertEqual(p["ready_to_process"], 2)
        self.assertEqual(p["needs_video_url"], 1)
        self.assertEqual(p["candidate_unprocessed"], 3)  # ready + needs_url
        self.assertEqual(p["excluded_too_old"], 1)

    def test_skipped_too_old_is_not_a_candidate(self):
        # The 89-too-old-style archive must never count as work to flow (Option A).
        p = self._meter()["progress"]
        self.assertEqual(p["excluded_too_old"], 1)
        self.assertEqual(p["candidate_unprocessed"], p["ready_to_process"] + p["needs_video_url"])

    def test_city_filter_isolates(self):
        # The Otherville completed WO must not leak into Testville's counts.
        self.assertEqual(self._meter()["progress"]["processed"], 2)

    def test_processed_today(self):
        self.assertEqual(self._meter()["today"]["processed_today"], 1)

    def test_effective_rate_review_bound(self):
        c = self._meter(compute=3, review=1)["ceilings"]
        self.assertEqual(c["effective_per_day"], 1)
        self.assertEqual(c["bound_by"], "review")

    def test_effective_rate_compute_bound(self):
        c = self._meter(compute=1, review=5)["ceilings"]
        self.assertEqual(c["effective_per_day"], 1)
        self.assertEqual(c["bound_by"], "compute")

    def test_effective_rate_both_bound(self):
        c = self._meter(compute=2, review=2)["ceilings"]
        self.assertEqual(c["effective_per_day"], 2)
        self.assertEqual(c["bound_by"], "both")

    def test_room_and_under_ceiling(self):
        # processed_today=1. effective=2 -> room 1, under. effective=1 -> room 0, at limit.
        t2 = self._meter(compute=2, review=2)["today"]
        self.assertEqual(t2["room_today"], 1)
        self.assertTrue(t2["under_ceiling"])
        t1 = self._meter(compute=1, review=1)["today"]
        self.assertEqual(t1["room_today"], 0)
        self.assertFalse(t1["under_ceiling"])

    def test_days_to_drain(self):
        # candidate_unprocessed = 3.
        self.assertEqual(self._meter(compute=1, review=1)["days_to_drain"], 3)  # ceil(3/1)
        self.assertEqual(self._meter(compute=2, review=2)["days_to_drain"], 2)  # ceil(3/2)

    def test_zero_rate_edge(self):
        # Pausing the machine (0/day) must not divide-by-zero; drain is unknown.
        m = self._meter(compute=0, review=1)
        self.assertEqual(m["ceilings"]["effective_per_day"], 0)
        self.assertIsNone(m["days_to_drain"])
        self.assertEqual(m["today"]["room_today"], 0)
        self.assertFalse(m["today"]["under_ceiling"])

    def test_next_meeting_is_freshest_ready(self):
        # Two pending meetings; freshness-first picks the newest meeting_date (m6).
        nm = self._meter()["next_meeting"]
        self.assertIsNotNone(nm)
        self.assertEqual(nm["meeting_id"], 6)
        self.assertEqual(nm["meeting_date"], "2026-05-01")


class GovernorEmptyCityTest(unittest.TestCase):
    def setUp(self):
        self.conn = _build_fixture()

    def tearDown(self):
        self.conn.close()

    def test_unknown_city_is_all_zeros(self):
        m = gov.compute_city_metering("Nowhere", 1, 1, conn=self.conn)
        self.assertEqual(m["progress"]["processed"], 0)
        self.assertEqual(m["progress"]["candidate_unprocessed"], 0)
        self.assertEqual(m["days_to_drain"], 0)  # nothing to drain
        self.assertIsNone(m["next_meeting"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
