"""D-172 CLI auth broker and generation-provenance security tests."""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlparse


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
        mock.patch.object(database, "DB_PATH", str(Path(_import_temp_dir) / "import.db")),
        mock.patch.object(slack_listener, "start_listener_thread"),
    ):
        from parsers import api_server


MEETING_PUBLIC_ID = "m_" + "A" * 22
UNKNOWN_PUBLIC_ID = "m_" + "Z" * 22
VERIFIER = "V" * 43
CHALLENGE = base64.urlsafe_b64encode(
    hashlib.sha256(VERIFIER.encode("ascii")).digest()
).rstrip(b"=").decode("ascii")
STATE = "S" * 24
VIDEO_URL = "https://www.youtube.com/watch?v=private-intake-test"


def _canonical_sha256(value) -> str:
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


class CliAuthSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_patch = mock.patch.object(
            database, "DB_PATH", str(Path(self.temp_dir.name) / "cli-auth.db")
        )
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        database.init_db()
        self.user_id, self.meeting_id = self._seed_user_and_meeting()
        api_server.app.config.update(TESTING=True)
        self.client = api_server.app.test_client()

    def _seed_user_and_meeting(self) -> tuple[int, int]:
        conn = database.get_connection()
        try:
            user_id = conn.execute(
                "INSERT INTO users (google_sub, email, display_name) VALUES (?, ?, ?)",
                ("google-test", "person@example.test", "Test Person"),
            ).lastrowid
            city_id = conn.execute(
                "INSERT INTO cities (name, county, state) VALUES (?, ?, ?)",
                ("Test City", "Test County", "Arizona"),
            ).lastrowid
            meeting_id = conn.execute(
                """
                INSERT INTO meetings (
                    public_id, city_id, city_name, county, state,
                    meeting_title, meeting_date, meeting_status, video_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    MEETING_PUBLIC_ID,
                    city_id,
                    "Test City",
                    "Test County",
                    "Arizona",
                    "Test Council Meeting",
                    "2026-07-16",
                    "Scheduled",
                    VIDEO_URL,
                ),
            ).lastrowid
            conn.commit()
            return int(user_id), int(meeting_id)
        finally:
            conn.close()

    def _seed_code(
        self,
        code: str,
        *,
        challenge: str = CHALLENGE,
        expired: bool = False,
    ) -> None:
        expires = datetime.now(timezone.utc) + (
            timedelta(seconds=-1 if expired else 120)
        )
        conn = database.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO cli_auth_codes (
                    code_hash, user_id, loopback_port, cli_state,
                    code_challenge, expires_at
                ) VALUES (?, ?, 4567, ?, ?, ?)
                """,
                (
                    hashlib.sha256(code.encode("ascii")).hexdigest(),
                    self.user_id,
                    STATE,
                    challenge,
                    expires.isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _seed_token(self, token: str, *, expired: bool = False, revoked: bool = False) -> None:
        expires = datetime.now(timezone.utc) + timedelta(days=-1 if expired else 10)
        conn = database.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO cli_tokens (
                    token_hash, user_id, expires_at, revoked_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    hashlib.sha256(token.encode("ascii")).hexdigest(),
                    self.user_id,
                    expires.isoformat(timespec="seconds"),
                    datetime.now(timezone.utc).isoformat() if revoked else None,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _auth(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def _payload(**overrides):
        payload = {
            "meeting_public_id": MEETING_PUBLIC_ID,
            "output_type": "synopsis",
            "provider": "anthropic",
            "model": "claude-sonnet",
            "content_sha256": "a" * 64,
            "idempotency_key": "I" * 24,
        }
        payload.update(overrides)
        return payload

    @staticmethod
    def _contribution_payload(**overrides):
        transcript_core = {
            "source_url": VIDEO_URL,
            "duration_seconds": 2.0,
            "language": "en",
            "transcriber": "faster-whisper-local",
            "model": "small.en",
            "words": [
                {"word": "Council", "start": 0.0, "end": 0.8},
                {"word": "opened", "start": 0.8, "end": 1.5},
            ],
        }
        transcript = {**transcript_core, "sha256": _canonical_sha256(transcript_core)}
        outputs = []
        for output_type in api_server._CLI_CONTRIBUTION_OUTPUT_ORDER:
            content = f"content for {output_type}"
            gate_status = "observed_clean"
            outputs.append({
                "output_type": output_type,
                "content": content,
                "provider": "openai",
                "model": "gpt-test",
                "gate_status": gate_status,
                "gate_log": json.dumps({"status": gate_status}),
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            })
        core = {
            "schema_version": api_server._CLI_CONTRIBUTION_SCHEMA,
            "meeting_public_id": MEETING_PUBLIC_ID,
            "transcript": transcript,
            "outputs": outputs,
        }
        payload = {
            **core,
            "idempotency_key": "P" * 24,
            "payload_sha256": _canonical_sha256(core),
        }
        payload.update(overrides)
        return payload

    def test_start_validation_matrix_and_cookie_redirect(self):
        invalid_queries = [
            f"port=80&state={STATE}&challenge={CHALLENGE}",
            f"port=99999&state={STATE}&challenge={CHALLENGE}",
            f"port=nope&state={STATE}&challenge={CHALLENGE}",
            f"port=4567&state=short&challenge={CHALLENGE}",
            f"port=4567&state={'!' * 24}&challenge={CHALLENGE}",
            f"port=4567&state={STATE}&challenge={'A' * 42}",
            f"port=4567&state={STATE}&challenge={'A' * 44}",
        ]
        for query in invalid_queries:
            with self.subTest(query=query):
                self.assertEqual(self.client.get(f"/api/auth/cli/start?{query}").status_code, 400)

        with mock.patch.object(api_server, "_sign_envelope", return_value="signed.payload"):
            response = self.client.get(
                f"/api/auth/cli/start?port=4567&state={STATE}&challenge={CHALLENGE}"
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"],
            "/api/auth/google/login?next=/api/auth/cli/finish",
        )
        cookie = response.headers["Set-Cookie"]
        self.assertIn("zspan_cli_auth=signed.payload", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn("Path=/", cookie)

    def test_finish_get_requires_both_session_and_transient_cookie(self):
        payload = {"port": 4567, "cli_state": STATE, "challenge": CHALLENGE}
        with (
            mock.patch.object(api_server, "_current_user_from_cookie", return_value=None),
            mock.patch.object(api_server, "_verify_cli_auth_cookie", return_value=payload),
        ):
            self.assertEqual(self.client.get("/api/auth/cli/finish").status_code, 400)
        user = SimpleNamespace(email="person@example.test")
        with (
            mock.patch.object(api_server, "_current_user_from_cookie", return_value=user),
            mock.patch.object(api_server, "_verify_cli_auth_cookie", return_value=None),
        ):
            self.assertEqual(self.client.get("/api/auth/cli/finish").status_code, 400)

    def test_finish_get_escapes_identity_and_has_bodyless_form(self):
        user = SimpleNamespace(email='<script>alert("x")</script>@example.test')
        payload = {"port": 4567, "cli_state": STATE, "challenge": CHALLENGE}
        with (
            mock.patch.object(api_server, "_current_user_from_cookie", return_value=user),
            mock.patch.object(api_server, "_verify_cli_auth_cookie", return_value=payload),
        ):
            response = self.client.get("/api/auth/cli/finish")
        text = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn('<meta name="robots" content="noindex">', text)
        self.assertNotIn("<input", text)

    def test_finish_post_mints_hashed_code_and_redirects_only_to_validated_loopback(self):
        raw_code = "Q" * 43
        user = SimpleNamespace(id=self.user_id, email="person@example.test")
        payload = {"port": 4567, "cli_state": STATE, "challenge": CHALLENGE}
        with (
            mock.patch.object(api_server, "_current_user_from_cookie", return_value=user),
            mock.patch.object(api_server, "_verify_cli_auth_cookie", return_value=payload),
            mock.patch.object(api_server.secrets, "token_urlsafe", return_value=raw_code),
        ):
            response = self.client.post("/api/auth/cli/finish")
        self.assertEqual(response.status_code, 302)
        parsed = urlparse(response.headers["Location"])
        self.assertEqual((parsed.scheme, parsed.hostname, parsed.port), ("http", "127.0.0.1", 4567))
        self.assertEqual(parse_qs(parsed.query), {"code": [raw_code], "state": [STATE]})
        self.assertNotIn(raw_code, response.get_data(as_text=True))
        conn = database.get_connection()
        try:
            row = conn.execute("SELECT * FROM cli_auth_codes").fetchone()
            self.assertEqual(
                row["code_hash"], hashlib.sha256(raw_code.encode("ascii")).hexdigest()
            )
            self.assertNotIn(raw_code, json.dumps(dict(row), default=str))
        finally:
            conn.close()

    def test_cancel_valid_and_absent(self):
        self.assertEqual(self.client.get("/api/auth/cli/cancel").status_code, 400)
        payload = {"port": 4567, "cli_state": STATE, "challenge": CHALLENGE}
        with mock.patch.object(api_server, "_verify_cli_auth_cookie", return_value=payload):
            response = self.client.get("/api/auth/cli/cancel")
        self.assertEqual(response.status_code, 302)
        parsed = urlparse(response.headers["Location"])
        self.assertEqual((parsed.hostname, parsed.port, parsed.path), ("127.0.0.1", 4567, "/callback"))
        self.assertEqual(parse_qs(parsed.query), {"error": ["cancelled"], "state": [STATE]})

    def test_exchange_happy_path_and_replay(self):
        code = "C" * 43
        self._seed_code(code)
        response = self.client.post(
            "/api/auth/cli/exchange", json={"code": code, "code_verifier": VERIFIER}
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertRegex(body["token"], r"^[A-Za-z0-9_-]+$")
        self.assertEqual(body["account"]["email"], "person@example.test")
        replay = self.client.post(
            "/api/auth/cli/exchange", json={"code": code, "code_verifier": VERIFIER}
        )
        self.assertEqual(replay.status_code, 400)
        self.assertEqual(replay.get_json(), {"error": "invalid or expired code"})

    def test_exchange_wrong_verifier_does_not_burn_code(self):
        code = "D" * 43
        self._seed_code(code)
        wrong = self.client.post(
            "/api/auth/cli/exchange",
            json={"code": code, "code_verifier": "W" * 43},
        )
        self.assertEqual(wrong.status_code, 400)
        correct = self.client.post(
            "/api/auth/cli/exchange", json={"code": code, "code_verifier": VERIFIER}
        )
        self.assertEqual(correct.status_code, 200)

    def test_exchange_expired_malformed_and_oversized_fail_identically(self):
        code = "E" * 43
        self._seed_code(code, expired=True)
        cases = [
            {"code": code, "code_verifier": VERIFIER},
            {"code": "!", "code_verifier": VERIFIER},
            {"code": "A" * 129, "code_verifier": VERIFIER},
            {"code": "A" * 43, "code_verifier": "short"},
            {"code": "A" * 43, "code_verifier": "A" * 129},
        ]
        bodies = []
        for payload in cases:
            response = self.client.post("/api/auth/cli/exchange", json=payload)
            self.assertEqual(response.status_code, 400)
            bodies.append(response.get_json())
        non_object = self.client.post("/api/auth/cli/exchange", json=["not", "an", "object"])
        self.assertEqual(non_object.status_code, 400)
        self.assertEqual(bodies, [{"error": "invalid or expired code"}] * len(cases))

    def test_bearer_failures_are_identical_across_protected_routes(self):
        self._seed_token("expired-token", expired=True)
        self._seed_token("revoked-token", revoked=True)
        cases = [
            {},
            self._auth("unknown-token"),
            self._auth("expired-token"),
            self._auth("revoked-token"),
        ]
        for headers in cases:
            for method, path, kwargs in (
                (self.client.get, "/api/auth/cli/me", {}),
                (self.client.post, "/api/auth/cli/revoke", {}),
                (self.client.post, "/api/generations/register", {"json": self._payload()}),
                (
                    self.client.post,
                    "/api/contributions/submit",
                    {"json": self._contribution_payload()},
                ),
            ):
                with self.subTest(headers=headers, path=path):
                    response = method(path, headers=headers, **kwargs)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.get_json(), {"error": "cli auth required"})

    def test_revoke_succeeds_once_then_token_is_dead_everywhere(self):
        token = "live-token"
        self._seed_token(token)
        self.assertEqual(
            self.client.post("/api/auth/cli/revoke", headers=self._auth(token)).get_json(),
            {"ok": True},
        )
        for method, path, kwargs in (
            (self.client.post, "/api/auth/cli/revoke", {}),
            (self.client.get, "/api/auth/cli/me", {}),
            (self.client.post, "/api/generations/register", {"json": self._payload()}),
            (
                self.client.post,
                "/api/contributions/submit",
                {"json": self._contribution_payload()},
            ),
        ):
            response = method(path, headers=self._auth(token), **kwargs)
            self.assertEqual(response.status_code, 401)

    def test_register_validation_unknown_meeting_and_rate_limit(self):
        token = "register-token"
        self._seed_token(token)
        bad_payloads = [
            self._payload(meeting_public_id="bad"),
            self._payload(output_type="transcript_words"),
            self._payload(provider=""),
            self._payload(provider="p" * 65),
            self._payload(model=""),
            self._payload(model="m" * 65),
            self._payload(content_sha256="A" * 64),
            self._payload(idempotency_key="short"),
            self._payload(idempotency_key="!" * 24),
        ]
        for payload in bad_payloads:
            response = self.client.post(
                "/api/generations/register", headers=self._auth(token), json=payload
            )
            self.assertEqual(response.status_code, 400)
        self.assertEqual(
            self.client.post(
                "/api/generations/register", headers=self._auth(token), json=[]
            ).status_code,
            400,
        )
        unknown = self.client.post(
            "/api/generations/register",
            headers=self._auth(token),
            json=self._payload(meeting_public_id=UNKNOWN_PUBLIC_ID),
        )
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.get_json(), {"error": "unknown meeting"})

        conn = database.get_connection()
        try:
            alphabet = database.WATERMARK_BASE32_ALPHABET
            for index in range(120):
                number = index
                chars = []
                for _ in range(8):
                    chars.append(alphabet[number % 32])
                    number //= 32
                conn.execute(
                    """
                    INSERT INTO cli_generations (
                        generation_public_id, ribbon_token, user_id,
                        meeting_public_id, output_type, provider, model,
                        content_sha256, idempotency_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "g_" + f"{index:022d}",
                        "".join(chars),
                        self.user_id,
                        MEETING_PUBLIC_ID,
                        "episode_tagline",
                        "p",
                        "m",
                        f"{index:064x}",
                        f"rate{index:020d}",
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        limited = self.client.post(
            "/api/generations/register", headers=self._auth(token), json=self._payload()
        )
        self.assertEqual(limited.status_code, 429)
        self.assertIn("Retry-After", limited.headers)

    def test_register_idempotency_dedup_and_supersession(self):
        token = "registration-token"
        self._seed_token(token)
        headers = self._auth(token)
        first = self.client.post(
            "/api/generations/register", headers=headers, json=self._payload()
        )
        self.assertEqual(first.status_code, 200)
        first_body = first.get_json()
        self.assertFalse(first_body["replayed"])
        replay = self.client.post(
            "/api/generations/register", headers=headers, json=self._payload()
        ).get_json()
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["generation_public_id"], first_body["generation_public_id"])

        conflict = self.client.post(
            "/api/generations/register",
            headers=headers,
            json=self._payload(content_sha256="b" * 64),
        )
        self.assertEqual(conflict.status_code, 409)

        content_dedup = self.client.post(
            "/api/generations/register",
            headers=headers,
            json=self._payload(idempotency_key="J" * 24, provider="other"),
        ).get_json()
        self.assertTrue(content_dedup["replayed"])
        self.assertEqual(content_dedup["generation_public_id"], first_body["generation_public_id"])

        second = self.client.post(
            "/api/generations/register",
            headers=headers,
            json=self._payload(idempotency_key="K" * 24, content_sha256="c" * 64),
        ).get_json()
        self.assertFalse(second["replayed"])
        self.assertEqual(second["superseded_previous"], first_body["generation_public_id"])
        conn = database.get_connection()
        try:
            old = conn.execute(
                "SELECT status, superseded_by FROM cli_generations WHERE generation_public_id = ?",
                (first_body["generation_public_id"],),
            ).fetchone()
            self.assertEqual((old["status"], old["superseded_by"]), ("superseded", second["generation_public_id"]))
        finally:
            conn.close()

    def test_private_contribution_is_stored_unpublished_and_replays(self):
        token = "private-contribution-token"
        self._seed_token(token)
        payload = self._contribution_payload()
        first = self.client.post(
            "/api/contributions/submit",
            headers=self._auth(token),
            json=payload,
        )
        self.assertEqual(first.status_code, 200)
        first_body = first.get_json()
        self.assertFalse(first_body["replayed"])
        self.assertFalse(first_body["published"])
        self.assertEqual(first_body["status"], "received_unverified")
        self.assertEqual(first_body["payload_sha256"], payload["payload_sha256"])
        replay = self.client.post(
            "/api/contributions/submit",
            headers=self._auth(token),
            json=payload,
        )
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.get_json()["replayed"])
        self.assertEqual(
            replay.get_json()["submission_public_id"], first_body["submission_public_id"]
        )

        conn = database.get_connection()
        try:
            contribution = conn.execute("SELECT * FROM cli_contributions").fetchone()
            outputs = conn.execute(
                "SELECT * FROM cli_contribution_outputs ORDER BY output_type"
            ).fetchall()
            notebook_count = conn.execute(
                "SELECT COUNT(*) FROM notebook_outputs WHERE meeting_id = ?",
                (self.meeting_id,),
            ).fetchone()[0]
            meeting = conn.execute(
                "SELECT is_published, published_at FROM meetings WHERE id = ?",
                (self.meeting_id,),
            ).fetchone()
            approved_count = conn.execute(
                "SELECT COUNT(*) FROM work_orders WHERE meeting_id = ? AND approved_at IS NOT NULL",
                (self.meeting_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(contribution["status"], "received_unverified")
        self.assertEqual(len(outputs), 4)
        self.assertEqual(notebook_count, 0)
        self.assertFalse(meeting["is_published"])
        self.assertIsNone(meeting["published_at"])
        self.assertEqual(approved_count, 0)

    def test_private_contribution_rejects_tampering_and_idempotency_conflict(self):
        token = "private-tamper-token"
        self._seed_token(token)
        headers = self._auth(token)
        original = self._contribution_payload()
        self.assertEqual(
            self.client.post(
                "/api/contributions/submit", headers=headers, json=original
            ).status_code,
            200,
        )
        changed = self._contribution_payload()
        changed["transcript"]["words"][0]["word"] = "Changed"
        changed_core = {
            key: changed[key]
            for key in ("schema_version", "meeting_public_id", "transcript", "outputs")
        }
        changed["transcript"]["sha256"] = _canonical_sha256({
            key: changed["transcript"][key]
            for key in changed["transcript"] if key != "sha256"
        })
        changed["payload_sha256"] = _canonical_sha256(changed_core)
        conflict = self.client.post(
            "/api/contributions/submit", headers=headers, json=changed
        )
        self.assertEqual(conflict.status_code, 409)

        bad_source = self._contribution_payload(idempotency_key="Q" * 24)
        bad_source["transcript"]["source_url"] = "https://example.test/other"
        bad_source["transcript"]["sha256"] = _canonical_sha256({
            key: bad_source["transcript"][key]
            for key in bad_source["transcript"] if key != "sha256"
        })
        bad_source["payload_sha256"] = _canonical_sha256({
            key: bad_source[key]
            for key in ("schema_version", "meeting_public_id", "transcript", "outputs")
        })
        rejected = self.client.post(
            "/api/contributions/submit", headers=headers, json=bad_source
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("does not match", rejected.get_json()["error"])

    def test_private_contribution_envelope_is_strict_and_bounded(self):
        token = "private-envelope-token"
        self._seed_token(token)
        headers = self._auth(token)
        wrong_type = self.client.post(
            "/api/contributions/submit",
            headers={**headers, "Content-Type": "text/plain"},
            data="{}",
        )
        self.assertEqual(wrong_type.status_code, 415)
        duplicate = self.client.post(
            "/api/contributions/submit",
            headers={**headers, "Content-Type": "application/json"},
            data='{"meeting_public_id":"x","meeting_public_id":"y"}',
        )
        self.assertEqual(duplicate.status_code, 400)
        with mock.patch.object(api_server, "_CLI_CONTRIBUTION_MAX_BYTES", 32):
            oversized = self.client.post(
                "/api/contributions/submit",
                headers={**headers, "Content-Type": "application/json"},
                data="{" + " " * 40 + "}",
            )
        self.assertEqual(oversized.status_code, 413)

    def test_watermark_lookup_flagship_cli_superseded_deleted_and_miss(self):
        conn = database.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO notebook_outputs (
                    meeting_id, notebook_id, output_type, prompt_version
                ) VALUES (?, 'legacy', 'key_decisions', 'v1')
                """,
                (self.meeting_id,),
            )
            conn.execute(
                """
                INSERT INTO cli_generations (
                    generation_public_id, ribbon_token, user_id,
                    meeting_public_id, output_type, provider, model,
                    content_sha256, idempotency_key, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "g_" + "A" * 22,
                    "BBBBBBBB",
                    self.user_id,
                    MEETING_PUBLIC_ID,
                    "synopsis",
                    "anthropic",
                    "sonnet",
                    "a" * 64,
                    "L" * 24,
                    "superseded",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        derived = database.derive_watermark_token(self.meeting_id, "key_decisions")
        flagship = self.client.get(f"/api/watermark-lookup/{derived}").get_json()
        self.assertTrue(flagship["exists"])
        self.assertFalse(flagship["authenticated"])
        self.assertTrue(flagship["legacy"])
        self.assertEqual(flagship["source"], "legacy_flagship")
        expected_legacy = {
            "token", "exists", "source", "row_id", "meeting_id", "output_type",
            "meeting_title", "city_name", "prompt_version", "generated_at",
            "authenticated", "legacy", "note",
        }
        self.assertEqual(set(flagship), expected_legacy)
        self.assertIn("publicly reproducible legacy identifier", flagship["note"])

        cli = self.client.get("/api/watermark-lookup/bbbbbbbb").get_json()
        self.assertEqual(cli["source"], "cli_generation")
        self.assertEqual(cli["status"], "superseded")
        self.assertEqual(cli["account_state"], "active")
        self.assertEqual(cli["meeting"]["public_id"], MEETING_PUBLIC_ID)
        encoded = json.dumps(cli).lower()
        for forbidden in ("email", "user_id", "display_name"):
            self.assertNotIn(forbidden, encoded)

        conn = database.get_connection()
        try:
            conn.execute("DELETE FROM users WHERE id = ?", (self.user_id,))
            conn.commit()
        finally:
            conn.close()
        deleted = self.client.get("/api/watermark-lookup/BBBBBBBB").get_json()
        self.assertEqual(deleted["account_state"], "deleted")
        miss = self.client.get("/api/watermark-lookup/ZZZZZZZZ").get_json()
        self.assertEqual(
            set(miss), {"token", "exists", "authenticated", "legacy", "note"}
        )
        self.assertFalse(miss["exists"])

    def test_flagship_publish_mints_unique_idempotent_registry_tokens(self):
        output_types = sorted(database.FLAGSHIP_RIBBON_OUTPUT_TYPES)
        conn = database.get_connection()
        try:
            for output_type in output_types:
                conn.execute(
                    """
                    INSERT INTO notebook_outputs (
                        meeting_id, notebook_id, output_type, content
                    ) VALUES (?, 'flagship', ?, ?)
                    """,
                    (self.meeting_id, output_type, f"content:{output_type}"),
                )
            conn.commit()
        finally:
            conn.close()

        verdict = {
            "ready": True,
            "publishable": True,
            "reasons": [],
            "publish_blockers": [],
        }
        with mock.patch.object(
            database, "check_publish_readiness", return_value=verdict
        ):
            first_publish = database.publish_meeting(
                self.meeting_id,
                "Test Owner",
                publisher_user_id=self.user_id,
            )
            self.assertIsNotNone(first_publish)

            conn = database.get_connection()
            try:
                first_rows = conn.execute(
                    """
                    SELECT ribbon_token, notebook_output_id, output_type, user_id
                    FROM flagship_generations
                    WHERE meeting_id = ? ORDER BY output_type
                    """,
                    (self.meeting_id,),
                ).fetchall()
            finally:
                conn.close()

            self.assertEqual(len(first_rows), len(output_types))
            self.assertEqual(
                {row["output_type"] for row in first_rows}, set(output_types)
            )
            self.assertEqual(
                len({row["ribbon_token"] for row in first_rows}),
                len(output_types),
            )
            self.assertTrue(all(row["user_id"] == self.user_id for row in first_rows))

            payload = database.get_meeting_with_notebook(self.meeting_id)
            self.assertIsNotNone(payload)
            for row in first_rows:
                output = payload["notebook_outputs"][row["output_type"]]
                self.assertEqual(output["ribbon_token"], row["ribbon_token"])
                self.assertEqual(output["registration_state"], "registered")

            lookup = self.client.get(
                f"/api/watermark-lookup/{first_rows[0]['ribbon_token']}"
            ).get_json()
            self.assertTrue(lookup["exists"])
            self.assertTrue(lookup["authenticated"])
            self.assertFalse(lookup["legacy"])
            self.assertEqual(lookup["source"], "flagship_generation")
            self.assertIn("screenshot content itself is not authenticated", lookup["note"])

            database.publish_meeting(
                self.meeting_id,
                "Test Owner",
                publisher_user_id=self.user_id,
            )

        conn = database.get_connection()
        try:
            republished_rows = conn.execute(
                """
                SELECT ribbon_token, notebook_output_id, output_type, user_id
                FROM flagship_generations
                WHERE meeting_id = ? ORDER BY output_type
                """,
                (self.meeting_id,),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(
            [tuple(row) for row in republished_rows],
            [tuple(row) for row in first_rows],
        )

    def test_ribbon_mint_length_alphabet_and_all_collision_redraws(self):
        conn = database.get_connection()
        try:
            token = database.mint_cli_ribbon_token(conn.cursor())
            self.assertEqual(len(token), 8)
            self.assertTrue(set(token) <= set(database.WATERMARK_BASE32_ALPHABET))
            conn.execute(
                """
                INSERT INTO cli_generations (
                    generation_public_id, ribbon_token, user_id,
                    meeting_public_id, output_type, provider, model,
                    content_sha256, idempotency_key
                ) VALUES (?, 'AAAAAAAA', ?, ?, 'synopsis', 'p', 'm', ?, ?)
                """,
                ("g_" + "C" * 22, self.user_id, MEETING_PUBLIC_ID, "c" * 64, "M" * 24),
            )
            conn.commit()
            with mock.patch.object(
                database.secrets,
                "choice",
                side_effect=list("AAAAAAAA" + "BBBBBBBB"),
            ):
                self.assertEqual(database.mint_cli_ribbon_token(conn.cursor()), "BBBBBBBB")

            flagship_output_id = conn.execute(
                """
                INSERT INTO notebook_outputs (
                    meeting_id, notebook_id, output_type
                ) VALUES (?, 'n', 'key_decisions')
                """,
                (self.meeting_id,),
            ).lastrowid
            conn.execute(
                """
                INSERT INTO flagship_generations (
                    ribbon_token, notebook_output_id, meeting_id, output_type,
                    user_id
                ) VALUES ('DDDDDDDD', ?, ?, 'key_decisions', ?)
                """,
                (flagship_output_id, self.meeting_id, self.user_id),
            )
            conn.commit()
            with mock.patch.object(
                database.secrets,
                "choice",
                side_effect=list("DDDDDDDD" + "EEEEEEEE"),
            ):
                self.assertEqual(
                    database.mint_cli_ribbon_token(conn.cursor()), "EEEEEEEE"
                )

            derived = database.derive_watermark_token(self.meeting_id, "synopsis")
            conn.execute(
                "INSERT INTO notebook_outputs (meeting_id, notebook_id, output_type) VALUES (?, 'n', 'synopsis')",
                (self.meeting_id,),
            )
            conn.commit()
            with mock.patch.object(
                database.secrets,
                "choice",
                side_effect=list(derived + "CCCCCCCC"),
            ):
                self.assertEqual(database.mint_cli_ribbon_token(conn.cursor()), "CCCCCCCC")
        finally:
            conn.close()

    def test_client_has_no_public_metadata_token_derivation(self):
        client_root = _COUNCIL_NAVIGATOR_DIR / "client" / "src"
        sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in client_root.rglob("*")
            if path.suffix in {".ts", ".tsx"}
        )
        self.assertNotIn("tokenForOutput", sources)
        self.assertNotIn("zspan-output:", sources)

    def test_raw_code_is_not_echoed_on_failure_or_bearer_responses(self):
        raw_code = "raw-secret-code"
        failure = self.client.post(
            "/api/auth/cli/exchange",
            json={"code": raw_code, "code_verifier": VERIFIER},
        )
        self.assertNotIn(raw_code, failure.get_data(as_text=True))
        raw_token = "raw-secret-token"
        self._seed_token(raw_token)
        me = self.client.get("/api/auth/cli/me", headers=self._auth(raw_token))
        self.assertNotIn(raw_token, me.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
