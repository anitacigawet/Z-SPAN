"""DIV-010 — the publish-readiness gate fails CLOSED on a readiness-check
crash. A readiness-service fault (Qdrant down, a bug in check_publish_readiness,
etc.) must NOT let a WO reach `completed`, which would assert a completeness the
check never confirmed. The produced outputs are already persisted, so a crash
fails the WO retryably instead.

Run directly (no pytest dependency), from the 02_Core_Project dir with the
project venv active:
    python -m unittest zspan_pipeline.tests.test_finalize_work_order
"""
from __future__ import annotations

import unittest
from unittest import mock

from zspan_pipeline import worker


class TestFinalizeWorkOrder(unittest.TestCase):
    def _run(self, *, readiness, sidecar_failure_reason=None):
        """Call _finalize_work_order with check_publish_readiness stubbed to
        either raise (pass an Exception) or return a verdict dict, and
        update_work_order_state captured. Returns (result, calls)."""
        calls: list = []
        uws = mock.patch.object(
            worker, "update_work_order_state",
            side_effect=lambda wo_id, state, **k: calls.append((wo_id, state, k)),
        )
        if isinstance(readiness, BaseException):
            rd = mock.patch("database.check_publish_readiness", side_effect=readiness)
        else:
            rd = mock.patch("database.check_publish_readiness", return_value=readiness)
        with uws, rd:
            result = worker._finalize_work_order(
                42, 100,
                sidecar_failure_reason=sidecar_failure_reason,
                output_count=8,
            )
        return result, calls

    def test_readiness_crash_fails_closed_not_completed(self):
        # The core DIV-010 assertion: a crash must NOT return "completed".
        result, calls = self._run(readiness=RuntimeError("qdrant unreachable"))
        self.assertEqual(result, "failed")
        self.assertEqual(len(calls), 1)
        wo_id, state, kw = calls[0]
        self.assertEqual((wo_id, state), (42, "failed"))
        self.assertTrue(kw.get("increment_retry"))
        self.assertIn("readiness_check_error", kw.get("error", ""))

    def test_crash_preserves_sidecar_failure_reason(self):
        result, calls = self._run(
            readiness=ValueError("boom"),
            sidecar_failure_reason="sidecar_pipeline crashed: x",
        )
        self.assertEqual(result, "failed")
        self.assertIn("sidecar_pipeline crashed", calls[0][2].get("error", ""))

    def test_not_ready_verdict_fails(self):
        result, calls = self._run(
            readiness={"ready": False, "reasons": ["missing synopsis"]})
        self.assertEqual(result, "failed")
        self.assertIn("incomplete_outputs", calls[0][2].get("error", ""))
        self.assertIn("missing synopsis", calls[0][2].get("error", ""))

    def test_ready_verdict_completes(self):
        result, calls = self._run(readiness={"ready": True, "reasons": []})
        self.assertEqual(result, "completed")
        self.assertEqual((calls[0][0], calls[0][1]), (42, "completed"))
        self.assertIsNone(calls[0][2].get("error"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
