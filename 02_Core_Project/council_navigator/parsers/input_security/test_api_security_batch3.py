"""Focused regression tests for audit batch-3 API hardening."""

from __future__ import annotations

import io
import os
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
            database,
            "DB_PATH",
            str(Path(_import_temp_dir) / "import.db"),
        ),
        mock.patch.object(slack_listener, "start_listener_thread"),
    ):
        from parsers import api_server


class PublicRouteCapTests(unittest.TestCase):
    def setUp(self) -> None:
        api_server.app.config.update(TESTING=True)
        self.client = api_server.app.test_client()
        api_server._reset_public_rate_limits_for_tests()

    def tearDown(self) -> None:
        api_server._reset_public_rate_limits_for_tests()

    def test_ribbon_upload_size_cap_returns_413_before_form_read(self):
        with mock.patch.object(api_server, "_RIBBON_UPLOAD_MAX_BYTES", 16):
            response = self.client.post(
                "/api/decode-ribbon-image",
                data=b"x" * 17,
                content_type="application/octet-stream",
                environ_base={"REMOTE_ADDR": "198.51.100.10"},
            )
        self.assertEqual(response.status_code, 413)
        self.assertIn("10 MiB", response.get_json()["error"])

    def test_ribbon_dimension_cap_returns_413_before_decoder(self):
        from PIL import Image

        image_bytes = io.BytesIO()
        Image.new("RGB", (8193, 1)).save(image_bytes, format="PNG")
        image_bytes.seek(0)
        with mock.patch(
            "zspan_pipeline.watermark_ribbon_decoder.decode_ribbon_bytes"
        ) as decoder:
            response = self.client.post(
                "/api/decode-ribbon-image",
                data={"image": (image_bytes, "too-wide.png")},
                content_type="multipart/form-data",
                environ_base={"REMOTE_ADDR": "198.51.100.11"},
            )
        self.assertEqual(response.status_code, 413)
        self.assertIn("8192", response.get_json()["error"])
        decoder.assert_not_called()

    def test_ribbon_route_rate_limit_is_five_per_minute(self):
        with mock.patch.object(
            api_server,
            "_public_rate_limit_now",
            return_value=100.0,
        ):
            for _ in range(5):
                response = self.client.post(
                    "/api/decode-ribbon-image",
                    environ_base={"REMOTE_ADDR": "198.51.100.12"},
                )
                self.assertEqual(response.status_code, 400)
            limited = self.client.post(
                "/api/decode-ribbon-image",
                environ_base={"REMOTE_ADDR": "198.51.100.12"},
            )
        self.assertEqual(limited.status_code, 429)

    @staticmethod
    def _heartbeat_result(count: int = 0) -> dict:
        return {
            "other_active": count,
            "sessions": [
                {
                    "client_kind": f"client-{index}",
                    "age_seconds": index,
                    "current_action": "testing",
                }
                for index in range(count)
            ],
        }

    def _heartbeat(self, payload: dict, *, remote_addr: str = "198.51.100.20"):
        return self.client.post(
            "/api/system/heartbeat",
            json=payload,
            environ_base={"REMOTE_ADDR": remote_addr},
        )

    def test_heartbeat_session_id_cap(self):
        with mock.patch.object(database, "heartbeat_session") as heartbeat:
            response = self._heartbeat({
                "session_id": "s" * 65,
                "client_kind": "web",
            })
        self.assertEqual(response.status_code, 400)
        heartbeat.assert_not_called()

    def test_heartbeat_client_kind_cap(self):
        with mock.patch.object(database, "heartbeat_session") as heartbeat:
            response = self._heartbeat({
                "session_id": "session",
                "client_kind": "c" * 33,
            })
        self.assertEqual(response.status_code, 400)
        heartbeat.assert_not_called()

    def test_heartbeat_current_action_cap(self):
        with mock.patch.object(database, "heartbeat_session") as heartbeat:
            response = self._heartbeat({
                "session_id": "session",
                "client_kind": "web",
                "current_action": "a" * 129,
            })
        self.assertEqual(response.status_code, 400)
        heartbeat.assert_not_called()

    def test_heartbeat_rate_limit_is_thirty_per_minute(self):
        payload = {"session_id": "session", "client_kind": "web"}
        with (
            mock.patch.object(
                database,
                "heartbeat_session",
                return_value=self._heartbeat_result(),
            ),
            mock.patch.object(
                api_server,
                "_public_rate_limit_now",
                return_value=100.0,
            ),
        ):
            for _ in range(30):
                self.assertEqual(self._heartbeat(payload).status_code, 200)
            limited = self._heartbeat(payload)
        self.assertEqual(limited.status_code, 429)

    def test_anonymous_heartbeat_sessions_are_capped_at_ten(self):
        with (
            mock.patch.object(
                database,
                "heartbeat_session",
                return_value=self._heartbeat_result(12),
            ),
            mock.patch.object(
                api_server,
                "_current_user_from_cookie",
                return_value=None,
            ),
        ):
            response = self._heartbeat({
                "session_id": "session",
                "client_kind": "web",
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.get_json()["sessions"]), 10)


class TrustedOriginTests(unittest.TestCase):
    def setUp(self) -> None:
        api_server.app.config.update(TESTING=True)
        self.client = api_server.app.test_client()

    def test_allowed_origin_passes(self):
        with mock.patch.dict(
            os.environ,
            {"ZSPAN_TRUSTED_ORIGINS": "https://trusted.example"},
            clear=False,
        ):
            response = self.client.post(
                "/api/auth/logout",
                headers={"Origin": "https://trusted.example"},
                environ_base={"REMOTE_ADDR": "198.51.100.30"},
            )
        self.assertEqual(response.status_code, 200)

    def test_untrusted_present_origin_is_403_even_on_loopback(self):
        response = self.client.post(
            "/api/auth/logout",
            headers={"Origin": "https://attacker.example"},
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "untrusted_origin")

    def test_missing_origin_only_passes_on_internal_proxy_hop(self):
        internal = self.client.post(
            "/api/auth/logout",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        external = self.client.post(
            "/api/auth/logout",
            environ_base={"REMOTE_ADDR": "198.51.100.31"},
        )
        self.assertEqual(internal.status_code, 200)
        self.assertEqual(external.status_code, 403)

    def test_session_cookie_is_strict_but_transient_cookie_stays_lax(self):
        with api_server.app.test_request_context():
            session_response = api_server.Response()
            api_server._set_cookie(
                session_response,
                api_server.SESSION_COOKIE_NAME,
                "session",
                60,
            )
            transient_response = api_server.Response()
            api_server._set_cookie(
                transient_response,
                "zspan_cli_auth",
                "transient",
                60,
            )
        self.assertIn("SameSite=Strict", session_response.headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", transient_response.headers["Set-Cookie"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
