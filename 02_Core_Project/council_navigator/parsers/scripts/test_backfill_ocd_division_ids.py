"""Recon-4 — tests for backfill_ocd_division_ids.py.

Unit tests for slug normalization + OCD lookup loading + idempotent backfill
behavior. Uses tempdir fixtures so the live parser_index.json + city_intelligence/
aren't touched.

Run via:
    python3.11 test_backfill_ocd_division_ids.py
or:
    python3.11 -m unittest scripts.test_backfill_ocd_division_ids
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PARSERS = _HERE.parent
if str(_PARSERS) not in sys.path:
    sys.path.insert(0, str(_PARSERS))

from scripts import backfill_ocd_division_ids as bf  # type: ignore  # noqa: E402


# ---------------------------------------------------------------------------
# Slug normalization
# ---------------------------------------------------------------------------


class TestSlug(unittest.TestCase):
    def test_simple_lowercase(self):
        self.assertEqual(bf.slug("Phoenix"), "phoenix")

    def test_two_word_city_underscored(self):
        self.assertEqual(bf.slug("Bullhead City"), "bullhead_city")

    def test_three_word_city(self):
        self.assertEqual(bf.slug("Lake Havasu City"), "lake_havasu_city")

    def test_punctuation_stripped(self):
        self.assertEqual(bf.slug("St. Johns"), "st_johns")

    def test_apostrophe_stripped(self):
        self.assertEqual(bf.slug("Coeur d'Alene"), "coeur_dalene")

    def test_hyphen_becomes_underscore(self):
        self.assertEqual(bf.slug("Winston-Salem"), "winston_salem")

    def test_multiple_spaces_collapsed(self):
        self.assertEqual(bf.slug("  Lake   Havasu  City  "), "lake_havasu_city")

    def test_no_internal_double_underscores(self):
        # "St. Johns" — the period AND the space would each yield "_", potentially "__".
        self.assertNotIn("__", bf.slug("St. Johns"))


# ---------------------------------------------------------------------------
# OCD CSV loader
# ---------------------------------------------------------------------------


_FAKE_CSV = (
    "id,name,sameAs,sameAsNote,validThrough,census_geoid,census_geoid_12,"
    "census_geoid_14,openstates_district,placeholder_id,sch_dist_stateid,"
    "state_id,validFrom\n"
    "ocd-division/country:us/state:az/place:phoenix,Phoenix city,,,,place-0455000,,,,,,,\n"
    "ocd-division/country:us/state:az/place:bullhead_city,Bullhead City city,,,,place-0408890,,,,,,,\n"
    "ocd-division/country:us/state:az/place:st_johns,St. Johns city,,,,place-0464210,,,,,,,\n"
    "ocd-division/country:us/state:az/county:maricopa,Maricopa County,,,,place-04013,,,,,,,\n"
)


class TestLoadOcdLookup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.csv = Path(self.tmp.name) / "state-az.csv"
        self.csv.write_text(_FAKE_CSV)

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_returns_place_rows_only(self):
        lookup = bf.load_ocd_lookup(self.csv)
        # 3 place entries × at least 1 key each (slug + name variants).
        self.assertIn("phoenix", lookup)
        self.assertIn("bullhead_city", lookup)
        self.assertIn("st_johns", lookup)
        # County rows are skipped — no `maricopa` key from county:.
        # (If "maricopa" appears it must be from a hypothetical place row,
        # which the fixture doesn't have.)
        for key, val in lookup.items():
            self.assertIn("/place:", val)

    def test_lookup_values_are_full_ocd_ids(self):
        lookup = bf.load_ocd_lookup(self.csv)
        self.assertEqual(
            lookup["phoenix"], "ocd-division/country:us/state:az/place:phoenix"
        )

    def test_name_variant_matches_through_slug_normalization(self):
        # The CSV has "St. Johns city" as the name; the slug code strips
        # both the period AND the unit-type suffix so 'St. Johns' resolves.
        lookup = bf.load_ocd_lookup(self.csv)
        self.assertEqual(
            lookup[bf.slug("St. Johns")],
            "ocd-division/country:us/state:az/place:st_johns",
        )


# ---------------------------------------------------------------------------
# Parser-index backfill (with tempfile fixture)
# ---------------------------------------------------------------------------


_FAKE_PARSER_INDEX = {
    "Phoenix": {
        "city": "Phoenix",
        "county": "Maricopa County",
        "calendar_url": "https://phoenix.legistar.com/Calendar.aspx",
        "parser_file": "phoenix_parser.py",
        "status": "success",
    },
    "St. Johns": {
        "city": "St. Johns",
        "county": "Apache County",
        "calendar_url": "https://www.sjaz.us/meetings-agendas/",
        "parser_file": "st_johns_parser.py",
        "status": "success",
    },
    "Nowhere": {
        "city": "Nowhere",
        "county": "Apache County",
        "calendar_url": "https://example.com/",
        "parser_file": "nowhere_parser.py",
        "status": "success",
    },
}


class TestBackfillParserIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pi_path = Path(self.tmp.name) / "parser_index.json"
        self.pi_path.write_text(json.dumps(_FAKE_PARSER_INDEX, indent=2))
        # Build the lookup the way the real script does — from a fixture CSV.
        csv_path = Path(self.tmp.name) / "state-az.csv"
        csv_path.write_text(_FAKE_CSV)
        self.lookup = bf.load_ocd_lookup(csv_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_does_not_write(self):
        before = self.pi_path.read_text()
        summary = bf.backfill_parser_index(self.pi_path, self.lookup, dry_run=True)
        after = self.pi_path.read_text()
        self.assertEqual(before, after)
        self.assertTrue(summary["dry_run"])

    def test_write_updates_file(self):
        summary = bf.backfill_parser_index(self.pi_path, self.lookup, dry_run=False)
        self.assertFalse(summary["dry_run"])
        with self.pi_path.open() as f:
            updated = json.load(f)
        self.assertEqual(
            updated["Phoenix"]["ocd_division_id"],
            "ocd-division/country:us/state:az/place:phoenix",
        )
        self.assertEqual(
            updated["St. Johns"]["ocd_division_id"],
            "ocd-division/country:us/state:az/place:st_johns",
        )
        # Unmatched gets null, not missing key.
        self.assertIn("ocd_division_id", updated["Nowhere"])
        self.assertIsNone(updated["Nowhere"]["ocd_division_id"])

    def test_unmatched_reported_in_summary(self):
        summary = bf.backfill_parser_index(self.pi_path, self.lookup, dry_run=True)
        self.assertEqual(summary["matched"], 2)
        self.assertEqual(summary["unmatched_count"], 1)
        self.assertIn("Nowhere", summary["unmatched"])

    def test_idempotency_already_set_count_increments_on_second_run(self):
        bf.backfill_parser_index(self.pi_path, self.lookup, dry_run=False)
        summary2 = bf.backfill_parser_index(self.pi_path, self.lookup, dry_run=False)
        self.assertEqual(summary2["already_set"], 2)
        self.assertEqual(summary2["matched"], 2)


# ---------------------------------------------------------------------------
# city_intelligence backfill (with tempdir fixture)
# ---------------------------------------------------------------------------


_FAKE_CITY_INTEL_KINGMAN = {
    "canonical_name": "Phoenix",  # using Phoenix so it matches our fake CSV
    "county": "Maricopa",
    "state": "Arizona",
    "council": {"seats": 9},
}

_FAKE_CITY_INTEL_NOWHERE = {
    "canonical_name": "Nowhere",
    "county": "Apache",
    "state": "Arizona",
    "council": {"seats": 5},
}


class TestBackfillCityIntelligence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ci_dir = Path(self.tmp.name) / "city_intelligence"
        self.ci_dir.mkdir()
        (self.ci_dir / "phoenix.json").write_text(
            json.dumps(_FAKE_CITY_INTEL_KINGMAN, indent=2)
        )
        (self.ci_dir / "nowhere.json").write_text(
            json.dumps(_FAKE_CITY_INTEL_NOWHERE, indent=2)
        )
        csv_path = Path(self.tmp.name) / "state-az.csv"
        csv_path.write_text(_FAKE_CSV)
        self.lookup = bf.load_ocd_lookup(csv_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_matched_file_gets_ocd_id(self):
        bf.backfill_city_intelligence(self.ci_dir, self.lookup, dry_run=False)
        with (self.ci_dir / "phoenix.json").open() as f:
            data = json.load(f)
        self.assertEqual(
            data["ocd_division_id"],
            "ocd-division/country:us/state:az/place:phoenix",
        )

    def test_unmatched_file_gets_null(self):
        bf.backfill_city_intelligence(self.ci_dir, self.lookup, dry_run=False)
        with (self.ci_dir / "nowhere.json").open() as f:
            data = json.load(f)
        self.assertIn("ocd_division_id", data)
        self.assertIsNone(data["ocd_division_id"])

    def test_dry_run_does_not_write(self):
        before = (self.ci_dir / "phoenix.json").read_text()
        bf.backfill_city_intelligence(self.ci_dir, self.lookup, dry_run=True)
        after = (self.ci_dir / "phoenix.json").read_text()
        self.assertEqual(before, after)

    def test_idempotency(self):
        bf.backfill_city_intelligence(self.ci_dir, self.lookup, dry_run=False)
        s2 = bf.backfill_city_intelligence(self.ci_dir, self.lookup, dry_run=False)
        # Matched files were already set on the second run.
        self.assertEqual(s2["already_set"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
