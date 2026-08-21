"""D-164 anonymous /v1 catalog and work-order telemetry gate tests."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


_CORE_PROJECT_DIR = Path(__file__).resolve().parents[3]
_COUNCIL_NAVIGATOR_DIR = Path(__file__).resolve().parents[2]
_PARSERS_DIR = _COUNCIL_NAVIGATOR_DIR / "parsers"
_CLI_PROJECT_DIR = _CORE_PROJECT_DIR / "zspan_cli"
for _path in (
    _CLI_PROJECT_DIR,
    _COUNCIL_NAVIGATOR_DIR,
    _CORE_PROJECT_DIR,
    _PARSERS_DIR,
):
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
from zspan_cli import media as cli_media


_PUBLISHED_ID = "m_" + "A" * 22
_COMING_SOON_ID = "m_" + "B" * 22
_OTHER_CITY_ID = "m_" + "C" * 22
_ALIAS_ID = "m_" + "D" * 22
_UNKNOWN_ID = "m_" + "E" * 22


class V1CatalogEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        db_path = str(Path(self.temp_dir.name) / "catalog.db")
        self.db_patch = mock.patch.object(database, "DB_PATH", db_path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        # Neutralize the D-185 catalog state scope here so the pre-existing
        # wire-form / pagination / coverage / vocabulary tests below stay
        # focused on their original concern (cross-state seed data is part
        # of how they prove wire normalization). Scope enforcement itself
        # is exercised in V1CatalogStateScopeTests further down.
        self.scope_patch = mock.patch.object(
            api_server, "_PUBLIC_CATALOG_STATE_SCOPE", ""
        )
        self.scope_patch.start()
        self.addCleanup(self.scope_patch.stop)
        database.init_db()
        self._seed_catalog()

        api_server.app.config.update(TESTING=True)
        self.client = api_server.app.test_client()

    def _seed_catalog(self):
        conn = database.get_connection()
        try:
            # Seeds use PRODUCTION-form storage — full state names (what
            # resolve_city_state writes) and 12-hour scraped times — so these
            # tests prove the /v1 boundary NORMALIZES (postal codes, HH:MM)
            # rather than passing storage forms through (session-66 catch:
            # postal-code seeds masked exactly that divergence).
            alpha_city_id = conn.execute(
                """
                INSERT INTO cities (name, county, state)
                VALUES ('Alpha', 'Test County', 'Arizona')
                """
            ).lastrowid
            beta_city_id = conn.execute(
                """
                INSERT INTO cities (name, county, state)
                VALUES ('Beta', 'Other County', 'Nevada')
                """
            ).lastrowid
            conn.executemany(
                """
                INSERT INTO meetings (
                    id, public_id, city_id, city_name, county, state,
                    meeting_title, meeting_date, meeting_time,
                    meeting_location, meeting_status, agenda_url,
                    minutes_url, video_url, agenda_packet_url, is_published
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        101, _PUBLISHED_ID, alpha_city_id, "Alpha",
                        "Test County", "Arizona", "Published Council Meeting",
                        "2026-07-15", "6:00 PM", "Council Chambers",
                        "Minutes Available", "https://alpha.example/agenda",
                        "https://alpha.example/minutes",
                        "https://alpha.granicus.com/MediaPlayer.php?clip_id=1",
                        "https://alpha.example/packet", 1,
                    ),
                    (
                        102, _COMING_SOON_ID, alpha_city_id, "Alpha",
                        "Test County", "Arizona", "Awaiting Approval Meeting",
                        "2026-07-14", "5:00 PM", "Room 2",
                        "Agenda Available", "https://alpha.example/agenda-2",
                        "", "https://alpha.example/meeting.mp4", "", 1,
                    ),
                    (
                        103, _OTHER_CITY_ID, beta_city_id, "Beta",
                        "Other County", "Nevada", "Beta Planning Meeting",
                        "2025-01-02", "", "", "Scheduled", "", "", "", "", 0,
                    ),
                ],
            )
            conn.executemany(
                """
                INSERT INTO work_orders (
                    meeting_id, state, youtube_video_url, approved_at
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        101, "completed",
                        "https://www.youtube.com/watch?v=published",
                        "2026-07-13 10:00:00",
                    ),
                    (102, "completed", "", None),
                ],
            )
            conn.execute(
                """
                INSERT INTO meeting_public_id_aliases
                    (alias_public_id, canonical_meeting_id)
                VALUES (?, 101)
                """,
                (_ALIAS_ID,),
            )
            conn.commit()
        finally:
            conn.close()

    def assert_catalog_cache_header(self, response):
        self.assertEqual(
            response.headers.get("Cache-Control"),
            "public, max-age=300",
        )
        self.assertTrue(response.headers.get("Content-Type", "").startswith(
            "application/json"
        ))

    def test_jurisdictions_shape_counts_and_coverage_truth(self):
        response = self.client.get("/v1/catalog/jurisdictions")

        self.assertEqual(response.status_code, 200)
        self.assert_catalog_cache_header(response)
        payload = response.get_json()
        self.assertEqual(set(payload), {"states"})
        states = {row["state"]: row for row in payload["states"]}
        alpha = states["AZ"]["counties"][0]["cities"][0]
        beta = states["NV"]["counties"][0]["cities"][0]
        self.assertEqual(
            alpha,
            {"city": "Alpha", "meeting_count": 2, "covered": True},
        )
        self.assertEqual(
            beta,
            {"city": "Beta", "meeting_count": 1, "covered": False},
        )

    def test_list_filters_paginates_and_exposes_only_allowlisted_fields(self):
        with mock.patch.object(api_server, "_V1_CATALOG_PAGE_SIZE", 1):
            first = self.client.get(
                "/v1/catalog/meetings?state=az&county=Test%20County"
                "&city=Alpha&year=2026"
            )
            first_payload = first.get_json()
            second = self.client.get(
                "/v1/catalog/meetings",
                query_string={
                    "state": "AZ",
                    "county": "Test County",
                    "city": "Alpha",
                    "year": "2026",
                    "cursor": first_payload["next_cursor"],
                },
            )

        self.assertEqual(first.status_code, 200)
        self.assert_catalog_cache_header(first)
        self.assertEqual(first_payload["meetings"][0]["public_id"], _PUBLISHED_ID)
        self.assertTrue(first_payload["next_cursor"])
        self.assertEqual(second.status_code, 200)
        second_payload = second.get_json()
        self.assertEqual(second_payload["meetings"][0]["public_id"], _COMING_SOON_ID)
        self.assertEqual(second_payload["next_cursor"], "")

        expected_fields = set(api_server._V1_CATALOG_LIST_FIELDS)
        forbidden = {
            "id", "meeting_id", "notebook_id", "work_order_id",
            "priority", "retry_count", "last_error", "approved_at",
        }
        for row in first_payload["meetings"] + second_payload["meetings"]:
            self.assertEqual(set(row), expected_fields)
            self.assertFalse(set(row) & forbidden)

    def test_wire_vocabulary_normalizes_storage_forms(self):
        """zspan-catalog/1 serves postal states + 24h times regardless of the
        full-name / 12-hour forms the database actually stores (spec § 3;
        the boundary normalizes — storage is never migrated)."""
        listing = self.client.get(
            "/v1/catalog/meetings", query_string={"city": "Alpha"}
        ).get_json()
        by_id = {row["public_id"]: row for row in listing["meetings"]}
        self.assertEqual(by_id[_PUBLISHED_ID]["state"], "AZ")
        self.assertEqual(by_id[_PUBLISHED_ID]["time"], "18:00")
        self.assertEqual(by_id[_COMING_SOON_ID]["time"], "17:00")

        jurisdictions = self.client.get("/v1/catalog/jurisdictions").get_json()
        self.assertEqual(
            {node["state"] for node in jurisdictions["states"]}, {"AZ", "NV"}
        )

        # A client may echo back EITHER the wire form or the storage form.
        for state_param in ("AZ", "Arizona", "arizona"):
            rows = self.client.get(
                "/v1/catalog/meetings", query_string={"state": state_param}
            ).get_json()["meetings"]
            self.assertEqual(
                {row["public_id"] for row in rows},
                {_PUBLISHED_ID, _COMING_SOON_ID},
                f"state filter failed for {state_param!r}",
            )

        # Empty and unrecognized values pass through raw — never fabricated.
        self.assertEqual(api_server._v1_time_24h(""), "")
        self.assertEqual(api_server._v1_time_24h("noonish"), "noonish")
        self.assertEqual(api_server._v1_postal_state("Puerto Rico"), "Puerto Rico")
        self.assertEqual(api_server._v1_postal_state("nv"), "NV")
        self.assertEqual(api_server._v1_time_24h("12:00 AM"), "00:00")
        self.assertEqual(api_server._v1_time_24h("12 PM"), "12:00")
        # Two-letter garbage is NOT dressed up as a postal code.
        self.assertEqual(api_server._v1_postal_state("zz"), "zz")
        self.assertEqual(api_server._v1_postal_state("Zz"), "Zz")

    def test_jurisdictions_merge_mixed_storage_forms(self):
        """A state stored as BOTH 'Arizona' and 'AZ' must serve as ONE wire
        node — grouping happens on the normalized key, not raw storage."""
        conn = database.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO cities (name, county, state)
                VALUES ('Gamma', 'Test County', 'AZ')
                """
            )
            conn.commit()
        finally:
            conn.close()

        payload = self.client.get("/v1/catalog/jurisdictions").get_json()
        az_nodes = [n for n in payload["states"] if n["state"] == "AZ"]
        self.assertEqual(len(az_nodes), 1, "mixed storage forms must merge")
        az_cities = {
            city["city"]
            for county in az_nodes[0]["counties"]
            for city in county["cities"]
        }
        self.assertIn("Alpha", az_cities)
        self.assertIn("Gamma", az_cities)

    def test_jurisdictions_coverage_uses_visibility_and_jurisdiction_key(self):
        """DIV-009 + session-66: `covered` gates on the two-field public
        visibility predicate (not `is_published` alone) and is keyed by the
        full (state, county, city) triple (not city name alone)."""
        conn = database.get_connection()
        try:
            # "Delta" (Test County, AZ): its ONLY published meeting has no
            # approved work order → coming_soon. `is_published` alone would
            # call this covered; the visibility predicate must not.
            delta_city_id = conn.execute(
                "INSERT INTO cities (name, county, state) "
                "VALUES ('Delta', 'Test County', 'Arizona')"
            ).lastrowid
            delta_mid = conn.execute(
                "INSERT INTO meetings (public_id, city_id, city_name, county, "
                "state, meeting_title, meeting_date, is_published) "
                "VALUES (?, ?, 'Delta', 'Test County', 'Arizona', "
                "'Pending Review', '2026-08-01', 1)",
                ("m_" + "F" * 22, delta_city_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO work_orders (meeting_id, state, approved_at) "
                "VALUES (?, 'completed', NULL)",
                (delta_mid,),
            )
            # A SECOND "Alpha", in a DIFFERENT county, with NO published
            # meeting. The Test-County Alpha is covered (meeting 101 is
            # visible); this one must NOT inherit that coverage via a
            # city-name collision.
            second_alpha_city_id = conn.execute(
                "INSERT INTO cities (name, county, state) "
                "VALUES ('Alpha', 'Second County', 'Arizona')"
            ).lastrowid
            conn.execute(
                "INSERT INTO meetings (public_id, city_id, city_name, county, "
                "state, meeting_title, meeting_date, is_published) "
                "VALUES (?, ?, 'Alpha', 'Second County', 'Arizona', "
                "'Unpublished', '2026-08-02', 0)",
                ("m_" + "G" * 22, second_alpha_city_id),
            )
            conn.commit()
        finally:
            conn.close()

        payload = self.client.get("/v1/catalog/jurisdictions").get_json()
        az = next(n for n in payload["states"] if n["state"] == "AZ")
        by_county = {c["county"]: c for c in az["counties"]}

        def _city(county, city):
            return next(
                c for c in by_county[county]["cities"] if c["city"] == city
            )

        # coming_soon-only city → NOT covered (DIV-009).
        self.assertFalse(_city("Test County", "Delta")["covered"])
        # The two same-named Alphas resolve independently by county
        # (session-66): the one with a visible meeting is covered, the other
        # is not — no city-name collision.
        self.assertTrue(_city("Test County", "Alpha")["covered"])
        self.assertFalse(_city("Second County", "Alpha")["covered"])

    def test_detail_distinguishes_published_and_coming_soon(self):
        published = self.client.get(f"/v1/catalog/meetings/{_PUBLISHED_ID}")
        coming_soon = self.client.get(f"/v1/catalog/meetings/{_COMING_SOON_ID}")

        self.assertEqual(published.status_code, 200)
        self.assert_catalog_cache_header(published)
        published_row = published.get_json()
        self.assertEqual(published_row["availability"], "published")
        # The detail row shares _v1_catalog_list_row, so it must carry the
        # SAME normalized wire vocabulary as the list (postal state, 24h time)
        # — asserted here so the contract's detail half isn't untested.
        self.assertEqual(published_row["state"], "AZ")
        self.assertEqual(published_row["time"], "18:00")
        self.assertEqual(
            published_row["video_url"],
            "https://www.youtube.com/watch?v=published",
        )
        self.assertEqual(
            published_row["documents"],
            {
                "agenda_url": "https://alpha.example/agenda",
                "minutes_url": "https://alpha.example/minutes",
                "packet_url": "https://alpha.example/packet",
            },
        )
        self.assertEqual(
            published_row["local_processing"],
            {"status": "ready", "source_kind": "youtube"},
        )
        self.assertNotIn("id", published_row)
        self.assertNotIn("meeting_id", published_row)
        self.assertNotIn("notebook_id", published_row)

        self.assertEqual(coming_soon.status_code, 200)
        coming_soon_row = coming_soon.get_json()
        self.assertEqual(coming_soon_row["availability"], "coming_soon")
        self.assertEqual(
            coming_soon_row["local_processing"],
            {"status": "ready", "source_kind": "direct_media"},
        )

    def test_alias_adopts_canonical_public_id(self):
        response = self.client.get(f"/v1/catalog/meetings/{_ALIAS_ID}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["public_id"], _PUBLISHED_ID)

    def test_invalid_public_id_is_400_and_unknown_is_404(self):
        invalid = self.client.get("/v1/catalog/meetings/not-a-public-id")
        unknown = self.client.get(f"/v1/catalog/meetings/{_UNKNOWN_ID}")

        self.assertEqual(invalid.status_code, 400)
        self.assert_catalog_cache_header(invalid)
        self.assertEqual(unknown.status_code, 404)
        self.assert_catalog_cache_header(unknown)

    def test_work_order_telemetry_list_is_owner_only(self):
        with mock.patch.object(
            api_server, "_current_user_from_cookie", return_value=None
        ):
            anonymous = self.client.get("/api/work-orders")
        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(anonymous.get_json()["error"], "sign-in required")

        owner = SimpleNamespace(email="owner@example.com")
        with (
            mock.patch.object(
                api_server, "_current_user_from_cookie", return_value=owner
            ),
            mock.patch.object(api_server, "is_owner_email", return_value=True),
        ):
            authorized = self.client.get("/api/work-orders")
        self.assertEqual(authorized.status_code, 200)
        self.assertEqual(authorized.get_json()["count"], 2)


class V1CatalogStateScopeTests(unittest.TestCase):
    """D-185: the /v1/catalog/* surface serves only rows matching the
    deployment's configured state scope. Out-of-scope rows must not surface
    from the list, must not appear as jurisdictions, and must 404 on direct
    detail lookups (deep links respect the scope, unlike the date floor)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        db_path = str(Path(self.temp_dir.name) / "scope.db")
        self.db_patch = mock.patch.object(database, "DB_PATH", db_path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        database.init_db()
        self._seed_multi_state()

        api_server.app.config.update(TESTING=True)
        self.client = api_server.app.test_client()

    def _seed_multi_state(self):
        conn = database.get_connection()
        try:
            az_city_id = conn.execute(
                "INSERT INTO cities (name, county, state) "
                "VALUES ('Alpha', 'Test County', 'Arizona')"
            ).lastrowid
            nv_city_id = conn.execute(
                "INSERT INTO cities (name, county, state) "
                "VALUES ('Beta', 'Other County', 'Nevada')"
            ).lastrowid
            conn.executemany(
                "INSERT INTO meetings ("
                "id, public_id, city_id, city_name, county, state, "
                "meeting_title, meeting_date, is_published"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        201, _PUBLISHED_ID, az_city_id, "Alpha", "Test County",
                        "Arizona", "AZ Meeting", "2026-07-15", 1,
                    ),
                    (
                        202, _OTHER_CITY_ID, nv_city_id, "Beta", "Other County",
                        "Nevada", "NV Meeting", "2026-07-15", 1,
                    ),
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def _with_scope(self, value):
        return mock.patch.object(
            api_server, "_PUBLIC_CATALOG_STATE_SCOPE", value
        )

    def test_list_hides_out_of_scope_state_rows(self):
        with self._with_scope("Arizona"):
            no_filter = self.client.get("/v1/catalog/meetings").get_json()
            asked_nv = self.client.get(
                "/v1/catalog/meetings", query_string={"state": "Nevada"}
            ).get_json()
            asked_nv_postal = self.client.get(
                "/v1/catalog/meetings", query_string={"state": "NV"}
            ).get_json()

        ids = {row["public_id"] for row in no_filter["meetings"]}
        self.assertIn(_PUBLISHED_ID, ids)
        self.assertNotIn(_OTHER_CITY_ID, ids)
        # A client that explicitly asks for out-of-scope state gets an empty
        # list (server AND-composes scope with the user filter — no leak).
        self.assertEqual(asked_nv["meetings"], [])
        self.assertEqual(asked_nv_postal["meetings"], [])

    def test_jurisdictions_hides_out_of_scope_states(self):
        with self._with_scope("Arizona"):
            payload = self.client.get(
                "/v1/catalog/jurisdictions"
            ).get_json()
        states = {node["state"] for node in payload["states"]}
        self.assertEqual(states, {"AZ"})

    def test_detail_returns_404_for_out_of_scope_state(self):
        with self._with_scope("Arizona"):
            in_scope = self.client.get(
                f"/v1/catalog/meetings/{_PUBLISHED_ID}"
            )
            out_of_scope = self.client.get(
                f"/v1/catalog/meetings/{_OTHER_CITY_ID}"
            )
        self.assertEqual(in_scope.status_code, 200)
        self.assertEqual(out_of_scope.status_code, 404)
        self.assertEqual(
            out_of_scope.get_json(), {"error": "meeting not found"}
        )

    def test_scope_accepts_postal_form(self):
        # A deployment env-var set to 'AZ' (postal) matches DB rows stored
        # as 'Arizona' (full name) via the state-form expansion.
        with self._with_scope("AZ"):
            payload = self.client.get("/v1/catalog/meetings").get_json()
        ids = {row["public_id"] for row in payload["meetings"]}
        self.assertEqual(ids, {_PUBLISHED_ID})

    def test_empty_scope_returns_all_states(self):
        # The env-var disable path — a multi-state deployment (or a test) can
        # opt out and see every state again.
        with self._with_scope(""):
            payload = self.client.get(
                "/v1/catalog/jurisdictions"
            ).get_json()
        states = {node["state"] for node in payload["states"]}
        self.assertEqual(states, {"AZ", "NV"})


class CatalogClassifierParityTests(unittest.TestCase):
    def test_server_classifier_stays_in_lockstep_with_cli(self):
        self.assertEqual(
            api_server._CATALOG_YOUTUBE_HOST_SUFFIXES,
            cli_media._YOUTUBE_HOST_SUFFIXES,
        )
        self.assertEqual(
            api_server._CATALOG_DIRECT_MEDIA_EXTENSIONS,
            cli_media._DIRECT_MEDIA_EXTENSIONS,
        )
        self.assertEqual(
            api_server._CATALOG_VENDOR_PAGE_MARKERS,
            cli_media._VENDOR_PAGE_MARKERS,
        )

        corpus = {
            "https://youtube.com/watch?v=abc": "youtube",
            "https://youtu.be/abc": "youtube",
            "https://cdn.example.org/archive/meeting.mp4?download=1": "direct_media",
            "https://city.granicus.com/MediaPlayer.php?clip_id=12": "vendor_page",
            "": "unknown",
            "garbage": "unknown",
        }
        for url, expected in corpus.items():
            with self.subTest(url=url):
                self.assertEqual(cli_media.classify_video_url(url), expected)
                self.assertEqual(api_server.classify_catalog_video_url(url), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
