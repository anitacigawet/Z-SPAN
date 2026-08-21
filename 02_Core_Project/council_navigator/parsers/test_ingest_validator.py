"""Tests for ingest_validator — the front-door ingest gate.

Pure, offline, deterministic: fixtures are the exact fabricated rows the stub
parsers emitted (Benson/Clifton/Eagar), plus wall shells and real listings.
Tier 2 (paid LLM) is never invoked — every test passes allow_llm=False or
relies on the env flag being off. No network.

Run: python3 test_ingest_validator.py   (from the parsers/ dir)
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ingest_validator as iv  # noqa: E402


# ── fixtures (normalized field names — what cache_meetings actually sees) ──
BENSON = [
    {"meeting_title": "Sample Meeting 1", "meeting_date": "2026-01-01", "meeting_status": "Scheduled"},
    {"meeting_title": "Sample Meeting 2", "meeting_date": "2026-02-01", "meeting_status": "Scheduled"},
]
CLIFTON = [
    {"meeting_title": "Sample Meeting 1", "meeting_date": "2026-01-01", "meeting_status": "Sample"},
    {"meeting_title": "Sample Meeting 2", "meeting_date": "2026-02-01", "meeting_status": "Sample"},
]
EAGAR = [
    {"meeting_title": "Sample Meeting - JavaScript Required",
     "meeting_date": "2026-01-01", "meeting_status": "Sample"},
]
WALL = [
    {"meeting_title": "Just a moment...", "meeting_date": ""},
    {"meeting_title": "Please enable JavaScript to continue", "meeting_date": ""},
]
REAL = [
    {"meeting_title": "Regular City Council Meeting", "meeting_date": "2026-03-04", "meeting_status": "Scheduled"},
    {"meeting_title": "Special Work Session — Budget", "meeting_date": "2026-03-11", "meeting_status": "Scheduled"},
    {"meeting_title": "Planning & Zoning Commission", "meeting_date": "2026-03-18", "meeting_status": "Scheduled"},
]


class TestRejectsFabrication(unittest.TestCase):
    def test_empty_listing_is_honest_empty_not_rejected(self):
        v = iv.validate_listing([], "Nowhere")
        self.assertEqual(v.status, "empty")
        self.assertEqual(v.accepted_count, 0)
        self.assertEqual(v.rejected_count, 0)

    def test_benson_stub_fully_rejected(self):
        v = iv.validate_listing(BENSON, "Benson", allow_llm=False)
        self.assertEqual(v.status, "rejected")
        self.assertEqual(v.accepted_count, 0)
        self.assertEqual(v.rejected_count, 2)

    def test_clifton_stub_fully_rejected(self):
        v = iv.validate_listing(CLIFTON, "Clifton", allow_llm=False)
        self.assertEqual(v.status, "rejected")
        self.assertEqual(v.accepted_count, 0)

    def test_eagar_javascript_required_rejected(self):
        # This is meeting id 102764's exact row — the one James found cached.
        v = iv.validate_listing(EAGAR, "Eagar", allow_llm=False)
        self.assertEqual(v.status, "rejected")
        self.assertEqual(v.accepted_count, 0)
        # rejected with a marker reason (the "sample meeting" marker fires first)
        self.assertIn("sample", v.rejected[0][1].lower())

    def test_wall_shell_rejected(self):
        v = iv.validate_listing(WALL, "Somewhere", allow_llm=False)
        self.assertEqual(v.status, "rejected")
        self.assertEqual(v.accepted_count, 0)

    def test_empty_title_row_rejected(self):
        v = iv.validate_listing([{"meeting_title": "   "}], "X", allow_llm=False)
        self.assertEqual(v.status, "rejected")


class TestAcceptsReal(unittest.TestCase):
    def test_real_listing_all_accepted(self):
        v = iv.validate_listing(REAL, "Kingman", allow_llm=False)
        self.assertEqual(v.status, "ok")
        self.assertEqual(v.accepted_count, 3)
        self.assertEqual(v.rejected_count, 0)

    def test_mixed_keeps_real_drops_fabrication(self):
        v = iv.validate_listing(REAL + [EAGAR[0]], "Kingman", allow_llm=False)
        self.assertEqual(v.status, "ok")
        self.assertEqual(v.accepted_count, 3)
        self.assertEqual(v.rejected_count, 1)
        titles = [iv.title_of(m) for m in v.accepted]
        self.assertNotIn("Sample Meeting - JavaScript Required", titles)


class TestUncertainFailOpen(unittest.TestCase):
    def test_uniform_synthetic_is_uncertain_not_rejected_when_tier2_off(self):
        uniform = [
            {"meeting_title": "City Council Meeting", "meeting_date": "2026-05-05"},
            {"meeting_title": "City Council Meeting", "meeting_date": "2026-05-05"},
        ]
        v = iv.validate_listing(uniform, "SomeCity", allow_llm=False)
        self.assertEqual(v.status, "uncertain")
        # Fail-open: the rows are still accepted (downstream is the backstop).
        self.assertEqual(v.accepted_count, 2)

    def test_tier2_never_fires_when_disabled(self):
        # allow_llm=False must not make any network call even on a suspicious
        # listing; if it tried, _llm_listing_check would need requests + a key.
        uniform = [{"meeting_title": "Meeting", "meeting_date": "2026-06-01"},
                   {"meeting_title": "Meeting", "meeting_date": "2026-06-01"}]
        v = iv.validate_listing(uniform, "X", allow_llm=False)
        self.assertEqual(v.status, "uncertain")
        self.assertEqual(v.tier, "deterministic")


class TestFieldAccess(unittest.TestCase):
    def test_raw_field_names_pre_normalize(self):
        raw = [{"Meeting Title/Name": "Sample Meeting 1", "Meeting Status": "Sample"}]
        v = iv.validate_listing(raw, "Clifton", allow_llm=False)
        self.assertEqual(v.status, "rejected")

    def test_status_marker_rejects_even_with_ok_title(self):
        row = [{"meeting_title": "Council Session", "meeting_status": "placeholder"}]
        v = iv.validate_listing(row, "X", allow_llm=False)
        self.assertEqual(v.status, "rejected")


class TestLearnedMarkers(unittest.TestCase):
    def test_explicit_learned_marker_rejects(self):
        rows = [{"meeting_title": "Weird Vendor Landing Page"}]
        v = iv.validate_listing(rows, "X", allow_llm=False,
                                learned_markers=("weird vendor landing",))
        self.assertEqual(v.status, "rejected")

    def test_record_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            store = Path(d) / "learned.json"
            orig = iv._LEARNED_STORE
            try:
                iv._LEARNED_STORE = store
                iv.record_learned_marker("Fake Council Portal", source="test")
                loaded = iv.load_learned_markers()
                self.assertIn("fake council portal", loaded)
                # persisted lowercased + deduped
                iv.record_learned_marker("fake council portal")
                data = json.loads(store.read_text())
                self.assertEqual(data["title_markers"].count("fake council portal"), 1)
            finally:
                iv._LEARNED_STORE = orig


class TestTier2Config(unittest.TestCase):
    def test_llm_disabled_by_default(self):
        os.environ.pop("ZSPAN_INGEST_LLM_CHECK", None)
        self.assertFalse(iv.llm_enabled())

    def test_llm_flag_parsing(self):
        for on in ("1", "true", "YES", "on"):
            os.environ["ZSPAN_INGEST_LLM_CHECK"] = on
            self.assertTrue(iv.llm_enabled())
        os.environ["ZSPAN_INGEST_LLM_CHECK"] = "0"
        self.assertFalse(iv.llm_enabled())
        os.environ.pop("ZSPAN_INGEST_LLM_CHECK", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
