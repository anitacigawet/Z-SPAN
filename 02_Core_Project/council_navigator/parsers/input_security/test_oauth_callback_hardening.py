"""OAuth callback redirect and Google-claim hardening tests."""

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


class SafeNextPathTests(unittest.TestCase):
    def test_safe_next_path_rejects_backslash(self) -> None:
        self.assertEqual(api_server._safe_next_path("/\\evil"), "/")
        self.assertEqual(api_server._safe_next_path("/foo/\\bar"), "/")

    def test_safe_next_path_rejects_control_chars(self) -> None:
        self.assertEqual(api_server._safe_next_path("/foo\x00bar"), "/")
        self.assertEqual(api_server._safe_next_path("/foo\x1fbar"), "/")
        self.assertEqual(api_server._safe_next_path("/foo\x7fbar"), "/")

    def test_safe_next_path_rejects_netloc(self) -> None:
        self.assertEqual(api_server._safe_next_path("//evil"), "/")
        self.assertEqual(api_server._safe_next_path("http://evil"), "/")
        self.assertEqual(api_server._safe_next_path("/foo://bar"), "/")
        self.assertEqual(api_server._safe_next_path("///evil"), "/")
        self.assertEqual(api_server._safe_next_path("/%5Cevil"), "/")
        self.assertEqual(api_server._safe_next_path("/%2Fevil"), "/")

    def test_safe_next_path_accepts_clean_relative(self) -> None:
        self.assertEqual(
            api_server._safe_next_path("/dashboard"),
            "/dashboard",
        )
        self.assertEqual(
            api_server._safe_next_path("/dashboard?tab=1"),
            "/dashboard?tab=1",
        )


class OAuthCallbackHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        api_server.app.config.update(TESTING=True)
        self.client = api_server.app.test_client()
        self.client.set_cookie(
            api_server.OAUTH_STATE_COOKIE_NAME,
            "signed-oauth-state",
        )

    def _call_callback(
        self,
        userinfo: dict[str, object],
        *,
        owner_emails: frozenset[str] = frozenset(),
        owner_sub_allowlist: frozenset[str] = frozenset(),
    ):
        user = SimpleNamespace(id=42, role="light")
        with (
            mock.patch.object(api_server, "signin_enabled", return_value=True),
            mock.patch.object(
                api_server,
                "verify_oauth_state_cookie",
                return_value={
                    "code_verifier": "verifier",
                    "next": "/dashboard",
                },
            ),
            mock.patch.object(
                api_server,
                "compute_redirect_uri",
                return_value="https://zspan.org/api/auth/google/callback",
            ),
            mock.patch.object(
                api_server,
                "exchange_code",
                return_value={"access_token": "access-token"},
            ),
            mock.patch.object(
                api_server,
                "fetch_userinfo",
                return_value=userinfo,
            ),
            mock.patch.object(
                api_server,
                "get_owner_emails",
                return_value=set(owner_emails),
            ),
            mock.patch.object(
                api_server,
                "OWNER_GOOGLE_SUB_ALLOWLIST",
                owner_sub_allowlist,
            ),
            mock.patch.object(
                api_server,
                "upsert_user_from_google",
                return_value=user,
            ) as upsert_user,
            mock.patch.object(
                api_server,
                "mint_session_token",
                return_value="session-token",
            ),
        ):
            response = self.client.get(
                "/api/auth/google/callback?code=provider-code&state=state"
            )
        return response, upsert_user

    def test_oauth_callback_rejects_unverified_email(self) -> None:
        response, upsert_user = self._call_callback({
            "sub": "123",
            "email": "owner@example.com",
            "email_verified": False,
        })

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "email_not_verified")
        upsert_user.assert_not_called()

    def test_oauth_callback_rejects_missing_email_verified(self) -> None:
        response, upsert_user = self._call_callback({
            "sub": "123",
            "email": "owner@example.com",
        })

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "email_not_verified")
        upsert_user.assert_not_called()

    def test_oauth_callback_accepts_verified_email(self) -> None:
        response, upsert_user = self._call_callback({
            "sub": "123",
            "email": "viewer@example.com",
            "email_verified": True,
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/dashboard")
        upsert_user.assert_called_once_with(
            google_sub="123",
            email="viewer@example.com",
            display_name=None,
            avatar_url=None,
        )
        cookies = "\n".join(response.headers.getlist("Set-Cookie"))
        self.assertIn(
            f"{api_server.SESSION_COOKIE_NAME}=session-token",
            cookies,
        )

    def test_oauth_callback_sub_mismatch_rejects_when_allowlist_set(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"ZSPAN_OWNER_GOOGLE_SUB_ALLOWLIST": "approved_sub_1"},
        ):
            owner_sub_allowlist = (
                api_server._load_owner_google_sub_allowlist()
            )

        response, upsert_user = self._call_callback(
            {
                "sub": "attacker_sub",
                "email": "owner@example.com",
                "email_verified": True,
            },
            owner_emails=frozenset({"owner@example.com"}),
            owner_sub_allowlist=owner_sub_allowlist,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "owner_sub_mismatch")
        upsert_user.assert_not_called()

    def test_oauth_callback_sub_allowlist_empty_logs_but_accepts(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ZSPAN_OWNER_GOOGLE_SUB_ALLOWLIST", None)
            owner_sub_allowlist = (
                api_server._load_owner_google_sub_allowlist()
            )

        with self.assertLogs(api_server.app.logger, level="WARNING") as logs:
            response, upsert_user = self._call_callback(
                {
                    "sub": "any-owner-sub",
                    "email": "owner@example.com",
                    "email_verified": True,
                },
                owner_emails=frozenset({"owner@example.com"}),
                owner_sub_allowlist=owner_sub_allowlist,
            )

        self.assertEqual(response.status_code, 302)
        upsert_user.assert_called_once()
        self.assertTrue(any(
            "trusted without Google sub verification" in entry
            and "ZSPAN_OWNER_GOOGLE_SUB_ALLOWLIST is empty" in entry
            for entry in logs.output
        ))


if __name__ == "__main__":
    unittest.main()
