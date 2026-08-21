"""Google sign-in maintenance switch and session-secret rotation tests."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


_COUNCIL_NAVIGATOR_DIR = Path(__file__).resolve().parents[2]
_PARSERS_DIR = _COUNCIL_NAVIGATOR_DIR / "parsers"
for _path in (_COUNCIL_NAVIGATOR_DIR, _PARSERS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from parsers import database, google_oauth

sys.modules["database"] = database

import env_config
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


MAINTENANCE_BODY = {
    "status": "maintenance",
    "message": "Sign-in is temporarily paused. Check back soon.",
}


class SigninMaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        api_server.app.config.update(TESTING=True)
        self.client = api_server.app.test_client()
        env_config.signin_enabled.cache_clear()
        self.addCleanup(env_config.signin_enabled.cache_clear)

    def test_google_login_returns_stable_maintenance_response(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"ZSPAN_SIGNIN_ENABLED": "false"},
        ):
            env_config.signin_enabled.cache_clear()
            response = self.client.get("/api/auth/google/login")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), MAINTENANCE_BODY)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.content_type, "application/json")

    def test_google_callback_is_inert_and_clears_state_cookie(self) -> None:
        self.client.set_cookie(
            google_oauth.OAUTH_STATE_COOKIE_NAME,
            "pre-maintenance-state",
        )
        with (
            mock.patch.dict(
                os.environ,
                {"ZSPAN_SIGNIN_ENABLED": "false"},
            ),
            mock.patch.object(
                api_server,
                "exchange_code",
                side_effect=AssertionError("Google token endpoint was touched"),
            ) as exchange_code,
        ):
            env_config.signin_enabled.cache_clear()
            response = self.client.get(
                "/api/auth/google/callback?code=provider-code&state=state"
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), MAINTENANCE_BODY)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.content_type, "application/json")
        exchange_code.assert_not_called()
        state_cookie = response.headers["Set-Cookie"]
        self.assertIn(f"{google_oauth.OAUTH_STATE_COOKIE_NAME}=;", state_cookie)
        self.assertIn("Max-Age=0", state_cookie)

    def test_auth_me_surfaces_disabled_and_default_enabled_states(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"ZSPAN_SIGNIN_ENABLED": "false"},
        ):
            env_config.signin_enabled.cache_clear()
            disabled = self.client.get("/api/auth/me")

        self.assertEqual(disabled.status_code, 200)
        self.assertEqual(disabled.get_json(), {
            "authenticated": False,
            "user": None,
            "sign_in_enabled": False,
        })

        user = SimpleNamespace(
            id=42,
            email="signed-in@example.test",
            display_name="Signed In",
            avatar_url=None,
            role="light",
        )
        with (
            mock.patch.dict(
                os.environ,
                {"ZSPAN_SIGNIN_ENABLED": "false"},
            ),
            mock.patch.object(
                api_server,
                "_current_user_from_cookie",
                return_value=user,
            ),
            mock.patch.object(
                api_server,
                "is_owner_email",
                return_value=False,
            ),
            mock.patch.object(
                api_server,
                "is_operator_search_principal",
                return_value=False,
            ),
            mock.patch.object(
                api_server,
                "get_user_librarian_access",
                return_value="none",
            ),
            mock.patch.object(
                api_server,
                "list_follows",
                return_value=[],
            ),
        ):
            env_config.signin_enabled.cache_clear()
            authenticated = self.client.get("/api/auth/me")

        self.assertEqual(authenticated.status_code, 200)
        self.assertTrue(authenticated.get_json()["authenticated"])
        self.assertFalse(authenticated.get_json()["sign_in_enabled"])

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ZSPAN_SIGNIN_ENABLED", None)
            env_config.signin_enabled.cache_clear()
            enabled = self.client.get("/api/auth/me")

        self.assertEqual(enabled.status_code, 200)
        self.assertEqual(enabled.get_json(), {
            "authenticated": False,
            "user": None,
            "sign_in_enabled": True,
        })

    def test_env_session_secret_invalidates_json_signed_cookie(self) -> None:
        with mock.patch.object(
            google_oauth,
            "load_user_settings",
            return_value={"jwt_session_signing_secret": "json-fallback-secret"},
        ):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ZSPAN_SESSION_SECRET", None)
                json_signed = google_oauth.mint_session_token(42)
                self.assertIsNotNone(
                    google_oauth.verify_session_token(json_signed)
                )

            with mock.patch.dict(
                os.environ,
                {"ZSPAN_SESSION_SECRET": "env-backed-secret"},
            ):
                self.assertIsNone(
                    google_oauth.verify_session_token(json_signed)
                )
                env_signed = google_oauth.mint_session_token(42)
                self.assertIsNotNone(
                    google_oauth.verify_session_token(env_signed)
                )


if __name__ == "__main__":
    unittest.main()
