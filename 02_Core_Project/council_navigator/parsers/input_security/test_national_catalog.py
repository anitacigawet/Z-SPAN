from __future__ import annotations

import unittest

import national_catalog


class NationalCatalogRosterTests(unittest.TestCase):
    def test_imported_roster_is_complete_and_pinned(self):
        roster = national_catalog.load_roster()
        self.assertEqual(roster["schema_version"], national_catalog.SCHEMA_VERSION)
        self.assertEqual(roster["catalog_commit"], "5010d18366ef1f4fa37e326fa4e269493ffb2943")
        self.assertEqual(roster["source_count"], 38_705)
        self.assertEqual(roster["projection_count"], 38_713)
        self.assertEqual(len(roster["states"]), 56)

    def test_state_roster_retains_exact_record_link_and_route_alias(self):
        arizona = national_catalog.state_roster("az")
        self.assertIsNotNone(arizona)
        self.assertEqual(len(arizona["places"]), 122)
        st_johns = next(
            place
            for place in arizona["places"]
            if place["source_id"] == "us-az-st-johns--primary-meeting-source"
        )
        self.assertEqual(st_johns["route_name"], "St. Johns")
        self.assertEqual(st_johns["county_name"], "Apache County")
        self.assertEqual(
            national_catalog.catalog_record_url(
                st_johns,
                commit="5010d18366ef1f4fa37e326fa4e269493ffb2943",
            ),
            "https://github.com/anitacigawet/national-civics-catalog/blob/"
            "5010d18366ef1f4fa37e326fa4e269493ffb2943/data/states/az/"
            "sources.jsonl?plain=1#L98",
        )
        self.assertEqual(
            national_catalog.contribution_url(st_johns, state_code="AZ"),
            "/public-api/catalog/contribute/"
            "us-az-st-johns--primary-meeting-source.md?state=AZ",
        )

    def test_ai_handoff_is_listing_specific_and_has_no_git_fallback(self):
        arizona = national_catalog.state_roster("AZ")
        st_johns = next(
            place for place in arizona["places"]
            if place["source_id"] == "us-az-st-johns--primary-meeting-source"
        )
        handoff = national_catalog.contribution_handoff_markdown(
            arizona,
            st_johns,
            commit="5010d18366ef1f4fa37e326fa4e269493ffb2943",
        )
        self.assertIn("# Help add St. Johns", handoff)
        self.assertIn("Ask these questions one at a time", handoff)
        self.assertIn('"source_id": "us-az-st-johns--primary-meeting-source"', handoff)
        self.assertIn("If Git or GitHub CLI is unavailable", handoff)
        self.assertIn("issues/new?template=source-correction.yml", handoff)

    def test_national_projection_preserves_multi_state_and_multi_county_truth(self):
        arizona = national_catalog.state_roster("AZ")
        new_mexico = national_catalog.state_roster("NM")
        utah = national_catalog.state_roster("UT")
        source_id = "us-navajo-nation-council--primary-meeting-source"
        self.assertTrue(any(row["source_id"] == source_id for row in arizona["places"]))
        self.assertTrue(any(row["source_id"] == source_id for row in new_mexico["places"]))
        self.assertTrue(any(row["source_id"] == source_id for row in utah["places"]))
        winkelman = [
            row for row in arizona["places"]
            if row["source_id"] == "us-az-winkelman--primary-meeting-source"
        ]
        self.assertEqual(
            {row["county_name"] for row in winkelman},
            {"Gila County", "Pinal County"},
        )

    def test_shared_source_handoff_uses_projection_state_and_canonical_record(self):
        commit = "5010d18366ef1f4fa37e326fa4e269493ffb2943"
        source_id = "us-navajo-nation-council--primary-meeting-source"
        for code, state_name in (("NM", "New Mexico"), ("UT", "Utah")):
            with self.subTest(state=code):
                state = national_catalog.state_roster(code)
                place = next(
                    row for row in state["places"]
                    if row["source_id"] == source_id
                )
                self.assertEqual(place["file_state_code"], "AZ")
                self.assertEqual(
                    national_catalog.contribution_url(place, state_code=code),
                    f"/public-api/catalog/contribute/{source_id}.md?state={code}",
                )
                self.assertIn(
                    "/data/states/az/sources.jsonl?plain=1#L116",
                    national_catalog.catalog_record_url(place, commit=commit),
                )
                handoff = national_catalog.contribution_handoff_markdown(
                    state, place, commit=commit,
                )
                self.assertIn(f'"state": "{state_name}"', handoff)
                self.assertIn(
                    "/data/states/az/sources.jsonl?plain=1#L116",
                    handoff,
                )


if __name__ == "__main__":
    unittest.main()
