"""Per-IP limits and retired settings-control coverage."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


_COUNCIL_NAVIGATOR_DIR = Path(__file__).resolve().parents[2]
_PARSERS_DIR = _COUNCIL_NAVIGATOR_DIR / "parsers"
for _path in (_COUNCIL_NAVIGATOR_DIR, _PARSERS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from parsers import database

sys.modules["database"] = database

import slack_listener

with tempfile.TemporaryDirectory() as _import_temp_dir:
    with (
        mock.patch.object(
            database,
            "DB_PATH",
            str(Path(_import_temp_dir) / "import.db"),
        ),
        mock.patch.object(slack_listener, "start_listener_thread"),
    ):
        from parsers import api_server


class PublicRateLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        api_server.app.config.update(TESTING=True)
        self.client = api_server.app.test_client()
        api_server._reset_public_rate_limits_for_tests()
        api_server._reset_public_youtube_embed_cache_for_tests()

    def tearDown(self) -> None:
        api_server._reset_public_rate_limits_for_tests()
        api_server._reset_public_youtube_embed_cache_for_tests()

    # validate-key requires a signed-in account, so an anonymous probe's
    # non-limited response is 403 rather than 400 invalid-body. These tests
    # exercise the rate limiter; the 403 is just the pass-through status.
    _ANON_PROBE_STATUS = 403

    def _invalid_validation_request(self, remote_addr: str):
        return self.client.post(
            "/api/byok/validate-key",
            json={},
            environ_base={"REMOTE_ADDR": remote_addr},
        )

    def test_429_fires_after_route_threshold_with_retry_after(self) -> None:
        limit = api_server._PUBLIC_RATE_LIMITS["validate_key"]
        with mock.patch.object(api_server, "_public_rate_limit_now", return_value=100.0):
            for _ in range(limit):
                self.assertEqual(
                    self._invalid_validation_request("198.51.100.10").status_code,
                    self._ANON_PROBE_STATUS,
                )

            denied = self._invalid_validation_request("198.51.100.10")

        self.assertEqual(denied.status_code, 429)
        self.assertEqual(denied.headers.get("Retry-After"), "60")
        self.assertIn("Too many requests", denied.get_json()["error"])

    def test_client_ips_have_independent_budgets(self) -> None:
        limit = api_server._PUBLIC_RATE_LIMITS["validate_key"]
        with mock.patch.object(api_server, "_public_rate_limit_now", return_value=100.0):
            for _ in range(limit):
                self._invalid_validation_request("198.51.100.20")

            self.assertEqual(
                self._invalid_validation_request("198.51.100.20").status_code,
                429,
            )
            self.assertEqual(
                self._invalid_validation_request("198.51.100.21").status_code,
                self._ANON_PROBE_STATUS,
            )

    def test_window_decay_uses_mocked_monotonic_time(self) -> None:
        now = [100.0]
        limit = api_server._PUBLIC_RATE_LIMITS["validate_key"]
        with mock.patch.object(
            api_server,
            "_public_rate_limit_now",
            side_effect=lambda: now[0],
        ):
            for _ in range(limit):
                self._invalid_validation_request("198.51.100.30")
            self.assertEqual(
                self._invalid_validation_request("198.51.100.30").status_code,
                429,
            )

            now[0] += api_server._PUBLIC_RATE_LIMIT_WINDOW_SECONDS + 0.1
            self.assertEqual(
                self._invalid_validation_request("198.51.100.30").status_code,
                self._ANON_PROBE_STATUS,
            )

    def test_loopback_honors_proxy_header_non_loopback_ignores_it(self) -> None:
        with api_server.app.test_request_context(
            "/",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={"X-Zspan-Client-Ip": "203.0.113.40"},
        ):
            self.assertEqual(api_server._rate_limit_client_ip(), "203.0.113.40")

        with api_server.app.test_request_context(
            "/",
            environ_base={"REMOTE_ADDR": "198.51.100.40"},
            headers={"X-Zspan-Client-Ip": "203.0.113.40"},
        ):
            self.assertEqual(api_server._rate_limit_client_ip(), "198.51.100.40")

    def test_bucket_store_prunes_stale_entries_and_stays_capped(self) -> None:
        now = [100.0]
        with (
            mock.patch.object(api_server, "_PUBLIC_RATE_LIMIT_MAX_BUCKETS", 2),
            mock.patch.object(
                api_server,
                "_public_rate_limit_now",
                side_effect=lambda: now[0],
            ),
        ):
            for address in ("198.51.100.50", "198.51.100.51", "198.51.100.52"):
                with api_server.app.test_request_context(
                    "/", environ_base={"REMOTE_ADDR": address}
                ):
                    api_server._consume_public_rate_limit("verify_run")
            self.assertEqual(len(api_server._public_rate_limit_buckets), 2)

            now[0] += api_server._PUBLIC_RATE_LIMIT_WINDOW_SECONDS + 0.1
            with api_server.app.test_request_context(
                "/", environ_base={"REMOTE_ADDR": "198.51.100.53"}
            ):
                api_server._consume_public_rate_limit("verify_run")
            self.assertEqual(len(api_server._public_rate_limit_buckets), 1)

    def test_every_anonymous_catalog_route_is_rate_limited(self) -> None:
        public_paths = (
            "/v1/catalog/jurisdictions",
            "/v1/catalog/meetings",
            "/v1/catalog/meetings/m_AAAAAAAAAAAAAAAAAAAAAA",
            "/public-api/channels/tree",
            "/public-api/cities/Kingman/years",
            "/public-api/cities/Kingman/meetings",
            "/public-api/calendar/county/Mohave/meetings",
            "/public-api/calendar/search",
            "/public-api/calendar/stats",
            "/public-api/health",
            "/public-api/broadcasts/m_AAAAAAAAAAAAAAAAAAAAAA",
            "/public-api/broadcasts/m_AAAAAAAAAAAAAAAAAAAAAA/sim-queries",
            "/public-api/broadcasts/m_AAAAAAAAAAAAAAAAAAAAAA/sidecars/quotes",
            "/public-api/broadcasts/m_AAAAAAAAAAAAAAAAAAAAAA/citation",
            "/public-api/cast/Kingman",
            "/public-api/cast/Kingman/mayor",
            "/public-api/ledger/Kingman",
            "/public-api/guide",
            "/public-api/coverage",
            "/public-api/corrections",
            "/public-api/travelers",
            "/public-api/youtube/embed-check?video_id=dQw4w9WgXcQ",
        )
        with mock.patch.object(
            api_server,
            "_consume_public_rate_limit",
            return_value=(False, 17),
        ) as consume:
            for path in public_paths:
                with self.subTest(path=path):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 429)
                    self.assertEqual(response.headers.get("Retry-After"), "17")

        self.assertEqual(consume.call_count, len(public_paths))

    def test_calendar_search_pagination_is_bounded(self) -> None:
        with api_server.app.test_request_context(
            "/public-api/calendar/search?limit=9999&offset=999999"
        ):
            self.assertEqual(
                api_server._public_calendar_search_pagination(),
                (
                    api_server._PUBLIC_CALENDAR_SEARCH_MAX_LIMIT,
                    api_server._PUBLIC_CALENDAR_SEARCH_MAX_OFFSET,
                ),
            )

    def test_youtube_embed_check_caches_repeated_video_ids(self) -> None:
        upstream = mock.Mock(status_code=200)
        with mock.patch.object(
            api_server.requests,
            "get",
            return_value=upstream,
        ) as youtube_get:
            first = self.client.get(
                "/public-api/youtube/embed-check?video_id=dQw4w9WgXcQ"
            )
            second = self.client.get(
                "/public-api/youtube/embed-check?video_id=dQw4w9WgXcQ"
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(first.get_json()["embeddable"])
        youtube_get.assert_called_once()

    def test_member_rag_loopback_bypass_uses_forwarded_client_ip(self) -> None:
        with (
            mock.patch.object(
                api_server,
                "_current_user_from_cookie",
                return_value=None,
            ),
            mock.patch.object(
                api_server,
                "_resolve_rag_query_token",
                return_value="rag-secret",
            ),
        ):
            proxied_public = self.client.post(
                "/api/member-rag/Kingman/mayor",
                json={},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
                headers={"X-Zspan-Client-Ip": "203.0.113.55"},
            )
            direct_loopback = self.client.post(
                "/api/member-rag/Kingman/mayor",
                json={},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )

        self.assertEqual(proxied_public.status_code, 401)
        self.assertEqual(direct_loopback.status_code, 400)


class RetiredRateLimitSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        api_server.app.config.update(TESTING=True)
        self.client = api_server.app.test_client()

    def test_settings_get_does_not_echo_retired_keys(self) -> None:
        with (
            mock.patch.object(api_server, "_require_owner", return_value=(object(), None)),
            mock.patch.object(
                api_server,
                "load_user_settings",
                return_value={
                    "rate_limit_enabled": True,
                    "rate_limit_rps": 999,
                    "chat_mode": "direct",
                },
            ),
        ):
            response = self.client.get("/api/settings")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertNotIn("rate_limit_enabled", body)
        self.assertNotIn("rate_limit_rps", body)

    def test_settings_post_ignores_and_does_not_persist_retired_keys(self) -> None:
        saved: list[dict] = []
        with (
            mock.patch.object(api_server, "_require_owner", return_value=(object(), None)),
            mock.patch.object(
                api_server,
                "load_user_settings",
                return_value={
                    "rate_limit_enabled": True,
                    "rate_limit_rps": 999,
                    "chat_mode": "direct",
                },
            ),
            mock.patch.object(
                api_server,
                "save_user_settings",
                side_effect=lambda settings: saved.append(dict(settings)),
            ),
        ):
            response = self.client.post(
                "/api/settings",
                json={
                    "rate_limit_enabled": True,
                    "rate_limit_rps": 5000,
                    "chat_mode": "suggested",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(saved), 1)
        self.assertNotIn("rate_limit_enabled", saved[0])
        self.assertNotIn("rate_limit_rps", saved[0])
        self.assertEqual(saved[0]["chat_mode"], "suggested")


if __name__ == "__main__":
    unittest.main()
