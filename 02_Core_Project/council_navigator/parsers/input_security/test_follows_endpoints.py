"""Session-103 product-slice2 — /api/follows public-plane HTTP tests.

Sibling of `test_account_system.py` (which exercises the helper layer).
This module exercises the Flask endpoints themselves — the un-hardened
`_require_user()` gate, city canonicalization, the session-104 topic
follow guard, the per-user cap, and cross-user isolation.
"""

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
from parsers import account_system


class _FollowsEndpointBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        db_path = str(Path(self.temp_dir.name) / "follows.db")
        self.db_patch = mock.patch.object(database, "DB_PATH", db_path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        database.init_db()
        database.init_librarian_gate_events_schema()

        # Seed the canonical city so the city-canonicalization branch has
        # a real row to resolve against; the DB stores full-name state
        # ("Arizona") per resolve_city_state, exactly as production does.
        conn = database.get_connection()
        try:
            conn.execute(
                "INSERT INTO cities (name, county, state) "
                "VALUES ('Kingman', 'Mohave County', 'Arizona')"
            )
            conn.commit()
        finally:
            conn.close()

        self.user_a = account_system.upsert_user_from_google(
            google_sub="ua-sub", email="a@example.com",
        )
        self.user_b = account_system.upsert_user_from_google(
            google_sub="ub-sub", email="b@example.com",
        )

        api_server.app.config.update(TESTING=True)
        self.client = api_server.app.test_client()

    def _as(self, user):
        """Patch the session-cookie resolver so the test client presents
        as `user` for the duration of the block."""
        return mock.patch.object(
            api_server, "_current_user_from_cookie", return_value=user
        )


class AnonymousReturns401(_FollowsEndpointBase):
    def test_get_401_anonymous(self):
        with self._as(None):
            r = self.client.get("/api/follows")
            city_topics = self.client.get(
                "/api/follows/city-topics/Kingman"
            )
            replace_topics = self.client.put(
                "/api/follows/city-topics/Kingman",
                json={"tag_ids": ["data_centers"]},
            )
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json()["error"], "sign-in required")
        self.assertEqual(city_topics.status_code, 401)
        self.assertEqual(replace_topics.status_code, 401)

    def test_post_401_anonymous(self):
        with self._as(None):
            r = self.client.post(
                "/api/follows",
                json={"target_type": "city", "target_key": "Kingman"},
            )
        self.assertEqual(r.status_code, 401)

    def test_delete_401_anonymous(self):
        with self._as(None):
            r = self.client.delete(
                "/api/follows",
                json={"target_type": "city", "target_key": "Kingman"},
            )
        self.assertEqual(r.status_code, 401)


class SignedInUserCanManageOwnFollows(_FollowsEndpointBase):
    def test_add_list_remove_roundtrip(self):
        with self._as(self.user_a):
            add = self.client.post(
                "/api/follows",
                json={"target_type": "city", "target_key": "Kingman"},
            )
            self.assertEqual(add.status_code, 200)
            self.assertTrue(add.get_json()["added"])

            listed = self.client.get("/api/follows").get_json()
            keys = {(f["target_type"], f["target_key"]) for f in listed["follows"]}
            self.assertIn(("city", "Kingman"), keys)
            rm = self.client.delete(
                "/api/follows",
                json={"target_type": "city", "target_key": "Kingman"},
            )
            self.assertEqual(rm.status_code, 200)
            self.assertTrue(rm.get_json()["removed"])

    def test_add_idempotent(self):
        with self._as(self.user_a):
            first = self.client.post(
                "/api/follows",
                json={"target_type": "city", "target_key": "Kingman"},
            ).get_json()
            second = self.client.post(
                "/api/follows",
                json={"target_type": "city", "target_key": "Kingman"},
            ).get_json()
        self.assertTrue(first["added"])
        self.assertFalse(second["added"])

    def test_remove_cleans_up_legacy_mixed_case_city_follow(self):
        account_system.follow_add(self.user_a.id, "city", "kInGmAn")
        account_system.set_city_topics(
            self.user_a.id,
            "Kingman",
            ["data_centers"],
        )

        with self._as(self.user_a):
            removed = self.client.delete(
                "/api/follows",
                json={"target_type": "city", "target_key": "kingman"},
            )

        self.assertEqual(removed.status_code, 200)
        self.assertTrue(removed.get_json()["removed"])
        self.assertEqual(removed.get_json()["follows"], [])
        self.assertEqual(removed.get_json()["city_topics"], {})
        self.assertEqual(account_system.list_follows(self.user_a.id), [])

    def test_city_topics_replace_hydrate_isolate_and_unfollow_cascade(self):
        with self._as(self.user_a):
            self.client.post(
                "/api/follows",
                json={"target_type": "city", "target_key": "kingman"},
            )
            replaced = self.client.put(
                "/api/follows/city-topics/kingman",
                json={
                    "tag_ids": [
                        "DATA_CENTERS",
                        "data_centers",
                        "invalid",
                        "other",
                    ]
                },
            )
            self.assertEqual(replaced.status_code, 200)
            self.assertEqual(replaced.get_json(), {
                "success": True,
                "city_key": "Kingman",
                "tag_ids": ["data_centers"],
            })
            specific = self.client.get(
                "/api/follows/city-topics/Kingman"
            ).get_json()
            hydrated = self.client.get("/api/follows").get_json()
        self.assertEqual(specific["tag_ids"], ["data_centers"])
        self.assertEqual(
            hydrated["city_topics"],
            {"Kingman": ["data_centers"]},
        )

        with self._as(self.user_b):
            isolated = self.client.get(
                "/api/follows/city-topics/Kingman"
            ).get_json()
        self.assertEqual(isolated["tag_ids"], [])

        with (
            self._as(self.user_a),
            mock.patch.object(
                api_server,
                "clear_city_topics",
                side_effect=RuntimeError("simulated cleanup failure"),
            ),
            self.assertRaises(RuntimeError),
        ):
            self.client.delete(
                "/api/follows",
                json={"target_type": "city", "target_key": "Kingman"},
            )
        self.assertEqual(len(account_system.list_follows(self.user_a.id)), 1)
        self.assertEqual(
            account_system.list_city_topics(self.user_a.id),
            {"Kingman": ["data_centers"]},
        )

        with self._as(self.user_a):
            cleared = self.client.put(
                "/api/follows/city-topics/Kingman",
                json={"tag_ids": []},
            ).get_json()
            self.assertEqual(cleared["tag_ids"], [])
            self.client.put(
                "/api/follows/city-topics/Kingman",
                json={"tag_ids": ["water_rights"]},
            )
            removed = self.client.delete(
                "/api/follows",
                json={"target_type": "city", "target_key": "Kingman"},
            ).get_json()
        self.assertTrue(removed["removed"])
        self.assertEqual(removed["city_topics"], {})
        self.assertEqual(account_system.list_city_topics(self.user_a.id), {})


class CanonicalizationAndValidation(_FollowsEndpointBase):
    def test_city_canonicalizes_casing(self):
        """`kingman` (any case) resolves to the stored canonical `Kingman`
        so two users following different casings hit the same target."""
        with self._as(self.user_a):
            r = self.client.post(
                "/api/follows",
                json={"target_type": "city", "target_key": "kingman"},
            ).get_json()
        self.assertTrue(r["added"])
        stored = {(f["target_type"], f["target_key"]) for f in r["follows"]}
        self.assertIn(("city", "Kingman"), stored)
        self.assertNotIn(("city", "kingman"), stored)

    def test_unknown_city_rejected_400(self):
        with self._as(self.user_a):
            r = self.client.post(
                "/api/follows",
                json={"target_type": "city", "target_key": "Atlantis"},
            )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "unknown_city")

    def test_topic_post_disabled_and_existing_row_hidden_from_list(self):
        account_system.follow_add(
            self.user_a.id,
            "topic",
            "data_centers",
        )
        with self._as(self.user_a):
            listed = self.client.get("/api/follows").get_json()
            self.assertEqual(listed["follows"], [])
            r = self.client.post(
                "/api/follows",
                json={"target_type": "topic", "target_key": "data_centers"},
            )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json(), {
            "success": False,
            "error": "topic_follows_disabled",
            "detail": (
                "Global topic follows are disabled; follow the city instead."
            ),
        })
        self.assertEqual(
            account_system.list_follows(self.user_a.id)[0]["target_type"],
            "topic",
        )

    def test_unknown_topic_also_uses_disabled_guard(self):
        with self._as(self.user_a):
            r = self.client.post(
                "/api/follows",
                json={"target_type": "topic", "target_key": "spaceflight"},
            )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "topic_follows_disabled")
        self.assertEqual(
            r.get_json()["detail"],
            "Global topic follows are disabled; follow the city instead.",
        )


class PerUserCapAt409(_FollowsEndpointBase):
    def test_cap_returns_409_and_deletes_still_work(self):
        # Seed cap-many follows directly at the helper layer to avoid
        # spinning through 100 HTTP requests + 100 canonicalization DB hits.
        for i in range(account_system.FOLLOW_CAP_PER_USER):
            account_system.follow_add(self.user_a.id, "meeting", str(i))

        with self._as(self.user_a):
            # An enabled target beyond the cap still gets 409.
            r = self.client.post(
                "/api/follows",
                json={"target_type": "city", "target_key": "Kingman"},
            )
            self.assertEqual(r.status_code, 409)
            self.assertEqual(r.get_json()["error"], "follow_cap_exceeded")

            # Delete still allowed at cap.
            rm = self.client.delete(
                "/api/follows",
                json={"target_type": "meeting", "target_key": "0"},
            )
            self.assertEqual(rm.status_code, 200)
            self.assertTrue(rm.get_json()["removed"])


class CrossUserIsolation(_FollowsEndpointBase):
    def test_user_a_cannot_see_or_mutate_user_b_follows(self):
        # User B adds a follow directly.
        account_system.follow_add(self.user_b.id, "city", "Kingman")

        with self._as(self.user_a):
            listed = self.client.get("/api/follows").get_json()
            self.assertEqual(listed["follows"], [])

            # Deleting user B's follow while signed in as A must not
            # affect user B's list — server derives target scope from
            # the cookie principal, not the request body.
            rm = self.client.delete(
                "/api/follows",
                json={"target_type": "city", "target_key": "Kingman"},
            ).get_json()
            self.assertFalse(rm["removed"])

        # User B's follow still there.
        self.assertEqual(len(account_system.list_follows(self.user_b.id)), 1)


class WorkspaceReceiptsAreAccountScoped(_FollowsEndpointBase):
    def test_receipts_exclude_content_and_other_accounts(self):
        conn = database.get_connection()
        try:
            for public_id, ribbon, user_id, meeting_id, idem in (
                ("gen-a", "ribbon-a", self.user_a.id, "m_A", "idem-a"),
                ("gen-b", "ribbon-b", self.user_b.id, "m_B", "idem-b"),
            ):
                conn.execute(
                    """
                    INSERT INTO cli_generations (
                        generation_public_id, ribbon_token, user_id,
                        meeting_public_id, output_type, provider, model,
                        content_sha256, idempotency_key, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        public_id, ribbon, user_id, meeting_id, "synopsis",
                        "google", "gemini", "a" * 64, idem, "registered",
                    ),
                )
            conn.execute(
                """
                INSERT INTO librarian_gate_events (
                    event_id, user_id, query_hash, gate_version,
                    stencil_result, retrieval_run_id
                ) VALUES (?, ?, ?, ?, 'accepted', ?)
                """,
                ("event-a", self.user_a.id, "q" * 64, "test-v1", "run-a"),
            )
            conn.commit()
        finally:
            conn.close()

        with self._as(self.user_a):
            response = self.client.get("/api/workspace/receipts")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        payload = response.get_json()
        self.assertEqual(len(payload["receipts"]), 2)
        self.assertEqual(
            {receipt["public_id"] for receipt in payload["receipts"]},
            {"gen-a", "run-a"},
        )
        for receipt in payload["receipts"]:
            self.assertNotIn("content_sha256", receipt)
            self.assertNotIn("query_hash", receipt)

    def test_receipts_require_sign_in(self):
        with self._as(None):
            response = self.client.get("/api/workspace/receipts")
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
