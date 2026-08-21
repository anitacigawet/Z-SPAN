"""S-036 V1-complete — tests for the D-078 persisted invocation counter.

Each test uses a fresh temp SQLite DB so balance_ledger writes don't pollute
the real cache and tests are independent of each other.

Run via:
    python3.11 test_haiku_rate_limit.py
or:
    python3.11 -m unittest scripts.test_haiku_rate_limit
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

# Make sure parsers/ is on the path before importing.
_HERE = Path(__file__).resolve().parent
_PARSERS = _HERE.parent
if str(_PARSERS) not in sys.path:
    sys.path.insert(0, str(_PARSERS))

import database  # type: ignore
import haiku_rate_limit as hrl  # type: ignore


class _TempDbTestCase(unittest.TestCase):
    """Shared setup: each test gets a fresh tempfile DB pointed at by
    database.DB_PATH, initialized with the full schema."""

    def setUp(self):
        # Hold the fd open during the test; close + unlink in tearDown.
        fd, path = tempfile.mkstemp(suffix=".db", prefix="zspan_test_")
        os.close(fd)
        self._db_path = path
        self._orig_db_path = database.DB_PATH
        database.DB_PATH = path
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self._orig_db_path
        # Best-effort cleanup; if WAL files linger it's fine in tempdir.
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self._db_path + ext)
            except OSError:
                pass


class CeilingTests(_TempDbTestCase):
    """Cover the per-day invocation ceiling defense."""

    def test_clean_cold_start_passes(self):
        reservation = hrl.check_and_reserve_invocation()
        self.assertEqual(reservation.today_count_before_this_call, 0)
        self.assertGreater(reservation.started_at_epoch_s, 0)

    def test_under_ceiling_passes(self):
        # Pre-populate 5 invocations from today, well under the 50/day ceiling.
        now = int(time.time())
        for i in range(5):
            database.append_ledger_event(
                provider=hrl.PROVIDER,
                event_type=hrl.EVENT_TYPE,
                amount_cents=0,
                bucket_start_time=now - 600 - i,  # spread to avoid UNIQUE collision
                bucket_end_time=now - 580 - i,
                source="test",
            )
        reservation = hrl.check_and_reserve_invocation()
        self.assertEqual(reservation.today_count_before_this_call, 5)

    def test_at_ceiling_refuses(self):
        # Pre-populate MAX_INVOCATIONS_PER_DAY rows for today.
        now = int(time.time())
        for i in range(hrl.MAX_INVOCATIONS_PER_DAY):
            database.append_ledger_event(
                provider=hrl.PROVIDER,
                event_type=hrl.EVENT_TYPE,
                amount_cents=0,
                # Spread across the past few hours to avoid UNIQUE collisions
                # while still being inside today's UTC bucket.
                bucket_start_time=now - 7200 + i,
                bucket_end_time=now - 7100 + i,
                source="test",
            )
        with self.assertRaises(hrl.HaikuRateLimitError) as ctx:
            hrl.check_and_reserve_invocation()
        self.assertEqual(ctx.exception.reason_code, "daily_ceiling")
        self.assertEqual(ctx.exception.current_count, hrl.MAX_INVOCATIONS_PER_DAY)
        self.assertIn("daily ceiling hit", str(ctx.exception))

    def test_yesterday_rows_do_not_count(self):
        # Rows whose bucket_start_time is before today's UTC midnight don't
        # count toward today's ceiling — confirms the UTC-midnight rollover.
        yesterday_start = hrl._today_utc_start_epoch_s() - 86_400
        for i in range(hrl.MAX_INVOCATIONS_PER_DAY + 10):
            database.append_ledger_event(
                provider=hrl.PROVIDER,
                event_type=hrl.EVENT_TYPE,
                amount_cents=0,
                bucket_start_time=yesterday_start + i,
                bucket_end_time=yesterday_start + i + 30,
                source="test",
            )
        # Should pass — none of those rows are today.
        reservation = hrl.check_and_reserve_invocation()
        self.assertEqual(reservation.today_count_before_this_call, 0)


class CooldownTests(_TempDbTestCase):
    """Cover the wall-clock cooldown defense."""

    def test_recent_invocation_triggers_cooldown(self):
        now = int(time.time())
        # Last invocation ended 1 second ago — well within the 5s cooldown.
        database.append_ledger_event(
            provider=hrl.PROVIDER,
            event_type=hrl.EVENT_TYPE,
            amount_cents=0,
            bucket_start_time=now - 30,
            bucket_end_time=now - 1,
            source="test",
        )
        with self.assertRaises(hrl.HaikuRateLimitError) as ctx:
            hrl.check_and_reserve_invocation()
        self.assertEqual(ctx.exception.reason_code, "cooldown")

    def test_old_invocation_does_not_trigger_cooldown(self):
        now = int(time.time())
        # Last invocation ended 60s ago — past the 5s cooldown.
        database.append_ledger_event(
            provider=hrl.PROVIDER,
            event_type=hrl.EVENT_TYPE,
            amount_cents=0,
            bucket_start_time=now - 120,
            bucket_end_time=now - 60,
            source="test",
        )
        reservation = hrl.check_and_reserve_invocation()
        self.assertEqual(reservation.today_count_before_this_call, 1)

    def test_clock_skew_negative_elapsed_skips_cooldown(self):
        # If a previously-recorded bucket_end_time is in the future (clock
        # weirdness or out-of-order write), the cooldown check should NOT
        # block indefinitely.
        now = int(time.time())
        database.append_ledger_event(
            provider=hrl.PROVIDER,
            event_type=hrl.EVENT_TYPE,
            amount_cents=0,
            bucket_start_time=now + 60,
            bucket_end_time=now + 120,  # in the future
            source="test",
        )
        # Should pass — no exception raised.
        reservation = hrl.check_and_reserve_invocation()
        self.assertEqual(reservation.today_count_before_this_call, 1)


class RecordCompleteTests(_TempDbTestCase):
    """Cover the record_invocation_complete write path."""

    def test_writes_a_row_with_correct_shape(self):
        reservation = hrl.check_and_reserve_invocation()
        time.sleep(0.01)  # ensure end > start
        log_path = Path(tempfile.gettempdir()) / "haiku_test.jsonl"
        row_id = hrl.record_invocation_complete(
            reservation,
            exit_code=0,
            log_path=log_path,
            city="Phoenix",
            url="https://phoenix.legistar.com/Calendar.aspx",
        )
        self.assertIsNotNone(row_id)

        # Verify the row landed with the expected values.
        conn = database.get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM balance_ledger WHERE id = ?", (row_id,)
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["provider"], hrl.PROVIDER)
        self.assertEqual(row["event_type"], hrl.EVENT_TYPE)
        self.assertEqual(row["amount_cents"], 0)
        self.assertEqual(row["bucket_start_time"], reservation.started_at_epoch_s)
        self.assertIsNotNone(row["bucket_end_time"])
        self.assertGreaterEqual(row["bucket_end_time"], reservation.started_at_epoch_s)
        self.assertEqual(row["source"], "haiku_html_scrape.py")
        self.assertIn("city=Phoenix", row["notes"])
        self.assertIn("url=https://phoenix.legistar.com", row["notes"])
        self.assertIn("exit=0", row["notes"])
        # external_ref is platform-serialized; check it ends in the file basename.
        self.assertIsNotNone(row["external_ref"])
        self.assertTrue(row["external_ref"].endswith("haiku_test.jsonl"))

    def test_truncates_overlong_url_in_notes(self):
        reservation = hrl.check_and_reserve_invocation()
        long_url = "https://example.gov/" + "x" * 500
        row_id = hrl.record_invocation_complete(
            reservation, exit_code=0, url=long_url,
        )
        conn = database.get_connection()
        try:
            row = conn.execute(
                "SELECT notes FROM balance_ledger WHERE id = ?", (row_id,)
            ).fetchone()
        finally:
            conn.close()
        self.assertIn("...", row["notes"])
        # The recorded notes field should be substantially shorter than the
        # untruncated URL — sanity check the truncation actually fired.
        self.assertLess(len(row["notes"]), 300)

    def test_records_failure_exit_code(self):
        reservation = hrl.check_and_reserve_invocation()
        row_id = hrl.record_invocation_complete(
            reservation, exit_code=2, city="Glendale",
        )
        conn = database.get_connection()
        try:
            row = conn.execute(
                "SELECT notes FROM balance_ledger WHERE id = ?", (row_id,)
            ).fetchone()
        finally:
            conn.close()
        self.assertIn("exit=2", row["notes"])


class EndToEndFlowTest(_TempDbTestCase):
    """One full reserve → record cycle, then verify the count moves."""

    def test_reservation_then_record_increments_today_count(self):
        # Pre: empty.
        self.assertEqual(hrl._count_invocations_today(), 0)

        # Reserve + record.
        reservation = hrl.check_and_reserve_invocation()
        hrl.record_invocation_complete(reservation, exit_code=0)

        # Post: count moved by 1.
        self.assertEqual(hrl._count_invocations_today(), 1)


if __name__ == "__main__":
    unittest.main()
