"""Regression tests for channel-tree jurisdiction merging and legacy seeding."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


_CORE_PROJECT_DIR = Path(__file__).resolve().parents[3]
_COUNCIL_NAVIGATOR_DIR = Path(__file__).resolve().parents[2]
_PARSERS_DIR = _COUNCIL_NAVIGATOR_DIR / "parsers"
for _path in (_COUNCIL_NAVIGATOR_DIR, _CORE_PROJECT_DIR, _PARSERS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from parsers import database

sys.modules["database"] = database

import slack_listener

with tempfile.TemporaryDirectory() as _import_temp_dir:
    with (
        mock.patch.object(
            database, "DB_PATH", str(Path(_import_temp_dir) / "import.db")
        ),
        mock.patch.object(slack_listener, "start_listener_thread"),
    ):
        from parsers import api_server


class ChannelsCityDedupTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        db_path = str(Path(self.temp_dir.name) / "channels.db")
        self.db_patch = mock.patch.object(database, "DB_PATH", db_path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        database.init_db()

        api_server.app.config.update(TESTING=True)
        self.client = api_server.app.test_client()

    def _seed_mohave_twins(self):
        conn = database.get_connection()
        try:
            city_ids = {}
            for city in ("Bullhead City", "Kingman", "Lake Havasu City"):
                city_ids[city] = conn.execute(
                    """
                    INSERT INTO cities (name, county, state)
                    VALUES (?, 'Mohave County', 'Arizona')
                    """,
                    (city,),
                ).lastrowid

            rows = []
            meeting_id = 1
            for city in city_ids:
                for county, notebook_id in (
                    ("Mohave", f"broadcast-{city}"),
                    ("Mohave County", ""),
                ):
                    rows.append(
                        (
                            meeting_id,
                            city_ids[city],
                            city,
                            county,
                            "Arizona",
                            f"{city} Council",
                            f"2026-07-{meeting_id:02d}",
                            notebook_id,
                        )
                    )
                    meeting_id += 1
            conn.executemany(
                """
                INSERT INTO meetings (
                    id, city_id, city_name, county, state, meeting_title,
                    meeting_date, notebook_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
        finally:
            conn.close()

    def test_mohave_raw_groups_and_roster_variants_merge_once(self):
        self._seed_mohave_twins()
        coverage_shaped_roster = {
            city: {
                "city": city,
                "county": "mohave county",
                "state": "az",
                "city_lat": 35.0,
                "city_lng": -114.0,
            }
            for city in ("Bullhead City", "Kingman", "Lake Havasu City")
        }

        with mock.patch.object(
            api_server, "load_parser_index", return_value=coverage_shaped_roster
        ):
            response = self.client.get("/api/channels/tree")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(len(payload["states"]), 1)
        arizona = payload["states"][0]
        self.assertEqual(arizona["state"], "Arizona")
        self.assertEqual(len(arizona["counties"]), 1)
        mohave = arizona["counties"][0]
        self.assertEqual(mohave["county"], "Mohave")
        self.assertEqual(
            [city["name"] for city in mohave["cities"]],
            ["Bullhead City", "Kingman", "Lake Havasu City"],
        )
        for city in mohave["cities"]:
            self.assertEqual(city["meeting_count"], 2)
            self.assertEqual(city["broadcast_count"], 1)
            self.assertEqual(city["status"], "live")
            self.assertEqual((city["lat"], city["lng"]), (35.0, -114.0))

    def test_channel_identity_normalizes_prod_and_registry_spellings(self):
        self.assertEqual(
            api_server._channel_city_identity(
                "Arizona", "Mohave", "Lake Havasu City"
            ),
            api_server._channel_city_identity(
                "az", "mohave COUNTY", "  lake  havasu city "
            ),
        )

    def test_public_tree_uses_the_same_normalized_merge(self):
        connection = mock.MagicMock()
        connection.execute.return_value.fetchall.return_value = [
            {
                "state": "Arizona",
                "county": "Mohave",
                "city_name": "Kingman",
                "meeting_count": 98,
                "broadcast_count": 5,
                "last_meeting": "2026-07-20",
                "first_meeting": "2024-01-01",
            },
            {
                "state": "az",
                "county": "mohave County",
                "city_name": "kingman",
                "meeting_count": 18,
                "broadcast_count": 0,
                "last_meeting": "2026-07-13",
                "first_meeting": "2023-01-01",
            },
        ]
        with mock.patch.object(
            api_server, "get_connection", return_value=connection
        ):
            response = self.client.get("/public-api/channels/tree")

        self.assertEqual(response.status_code, 200)
        cities = response.get_json()["states"][0]["counties"][0]["cities"]
        self.assertEqual(len(cities), 1)
        self.assertEqual(cities[0]["name"], "Kingman")
        self.assertEqual(cities[0]["meeting_count"], 116)
        self.assertEqual(cities[0]["broadcast_count"], 5)
        self.assertEqual(cities[0]["status"], "live")
        self.assertEqual(cities[0]["first_meeting"], "2023-01-01")
        connection.close.assert_called_once_with()

    def test_public_tree_includes_roster_only_city_as_scaffold(self):
        conn = database.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO cities (name, county, state, status)
                VALUES ('Sedona', 'Coconino County', 'Arizona', 'inactive')
                """
            )
            conn.commit()
        finally:
            conn.close()

        response = self.client.get("/public-api/channels/tree")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        arizona = next(
            state for state in payload["states"] if state["state"] == "Arizona"
        )
        coconino = next(
            county
            for county in arizona["counties"]
            if county["county"] == "Coconino"
        )
        self.assertEqual(coconino["cities"], [{
            "source_id": "",
            "name": "Sedona",
            "place_type": "municipality",
            "route_name": "Sedona",
            "source_status": "unverified",
            "contribution_url": "",
            "meeting_count": 0,
            "broadcast_count": 0,
            "status": "scaffold",
            "last_meeting": "",
            "first_meeting": "",
            "lat": None,
            "lng": None,
        }])

    def test_state_scoped_tree_uses_national_catalog_place_records(self):
        response = self.client.get("/public-api/channels/tree?state=NY")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual([state["state"] for state in payload["states"]], ["New York"])
        places = [
            place
            for county in payload["states"][0]["counties"]
            for place in county["cities"]
        ]
        county_sources = [
            source
            for county in payload["states"][0]["counties"]
            for source in county["sources"]
        ]
        self.assertEqual(len(places), 1_524)
        self.assertEqual(len(county_sources), 57)
        self.assertEqual(payload["states"][0]["statewide_sources"], [])
        self.assertEqual(payload["states"][0]["regional_sources"], [])
        self.assertTrue(all(place["status"] == "scaffold" for place in places))
        self.assertTrue(all(place["source_status"] == "needs_source" for place in places))
        self.assertTrue(all(place["route_name"] == "" for place in places))
        self.assertTrue(all(
            place["contribution_url"].startswith(
                "/public-api/catalog/contribute/"
            ) and place["contribution_url"].endswith("?state=NY")
            for place in places
        ))

    def test_state_county_and_regional_sources_are_not_city_rows(self):
        response = self.client.get("/public-api/channels/tree?state=AZ")
        self.assertEqual(response.status_code, 200)
        arizona = response.get_json()["states"][0]
        self.assertEqual(arizona["statewide_sources"], [])
        self.assertIn(
            "Navajo Nation",
            [source["name"] for source in arizona["regional_sources"]],
        )
        self.assertNotIn(
            "Statewide and regional",
            [county["county"] for county in arizona["counties"]],
        )
        mohave = next(
            county for county in arizona["counties"]
            if county["county"] == "Mohave County"
        )
        self.assertEqual(
            [source["name"] for source in mohave["sources"]],
            ["Mohave County"],
        )
        self.assertNotIn(
            "Mohave County",
            [place["name"] for place in mohave["cities"]],
        )

    def test_shared_regional_source_handoff_stays_in_selected_state(self):
        source_id = "us-navajo-nation-council--primary-meeting-source"
        for code, state_name in (("NM", "New Mexico"), ("UT", "Utah")):
            with self.subTest(state=code):
                response = self.client.get(
                    f"/public-api/channels/tree?state={code}"
                )
                self.assertEqual(response.status_code, 200)
                state = response.get_json()["states"][0]
                navajo = next(
                    source for source in state["regional_sources"]
                    if source["source_id"] == source_id
                )
                self.assertEqual(
                    navajo["contribution_url"],
                    f"/public-api/catalog/contribute/{source_id}.md?state={code}",
                )

                handoff_response = self.client.get(navajo["contribution_url"])
                self.assertEqual(handoff_response.status_code, 200)
                handoff = handoff_response.get_data(as_text=True)
                self.assertIn(f'"state": "{state_name}"', handoff)
                self.assertIn(
                    "/data/states/az/sources.jsonl?plain=1#L116",
                    handoff,
                )

    def test_listing_handoff_route_returns_markdown_not_raw_jsonl(self):
        response = self.client.get(
            "/public-api/catalog/contribute/"
            "us-az-census-government-183707--primary-meeting-source.md?state=AZ"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/markdown")
        body = response.get_data(as_text=True)
        self.assertIn("# Help add Mohave County", body)
        self.assertIn("copy-and-paste report", body)
        self.assertIn("source-correction.yml", body)

    def test_legacy_coverage_seed_preserves_status_and_fills_only_missing(self):
        conn = database.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO cities (
                    name, county, state, status, created_at, updated_at
                ) VALUES (
                    'Kingman', 'Mohave County', 'Arizona', 'active',
                    '2026-05-05 23:37:04', '2026-07-20 00:00:00'
                )
                """
            )
            conn.execute(
                """
                INSERT INTO cities (name, county, state, status)
                VALUES ('Bullhead City', 'Mohave County', 'Arizona', NULL)
                """
            )
            conn.commit()
        finally:
            conn.close()

        legacy_coverage = """{
          "cities": [
            {"city": "Kingman", "county": "Mohave County", "state": "az", "coverage": "live"},
            {"city": "Bullhead City", "county": "Mohave County", "state": "az", "coverage": "scaffold"}
          ]
        }"""
        with (
            mock.patch.object(database.os.path, "exists", return_value=True),
            mock.patch("builtins.open", mock.mock_open(read_data=legacy_coverage)),
        ):
            database._populate_cities_from_coverage_index()

        conn = database.get_connection()
        try:
            kingman = conn.execute(
                "SELECT status, updated_at FROM cities WHERE name = 'Kingman'"
            ).fetchone()
            bullhead = conn.execute(
                "SELECT status FROM cities WHERE name = 'Bullhead City'"
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(dict(kingman), {
            "status": "active",
            "updated_at": "2026-07-20 00:00:00",
        })
        self.assertEqual(bullhead["status"], "inactive")


if __name__ == "__main__":
    unittest.main()
