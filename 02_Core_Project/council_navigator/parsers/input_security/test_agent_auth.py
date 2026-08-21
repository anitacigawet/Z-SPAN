"""RR-8 SEC-AUTH-1/2/3 — fleet-agent bearer gate tests.

`agent_auth.check_agent_bearer` is the token half of the owner-OR-agent-token
gate on the handful of routes reachable by BOTH the owner (browser cookie) and
the headless fleet (localhost bearer). These tests pin the security contract
the session-56 Claude<->Codex design review converged on:

  * The `X-Zspan-Agent-Role` header is ATTRIBUTION-ONLY — it must never
    authorize anything on its own (the review's forged-role-header case).
  * An unset server token is 503 (unavailable), never a silent allow.
  * Bearer compare is exact + constant-time; missing/malformed/mismatch -> 401.
  * `bearer_header()` (the client companion) attaches the token when
    configured and degrades to {} when not — server and clients resolve the
    same token through the SAME `resolve_agent_token`.

Static-ish unit tests against a minimal fake request — no running Flask.
Part of the S-008 input-security suite; run via
`scripts/run_input_security_tests.py`.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path


_PARSERS_DIR = Path(__file__).resolve().parents[1]


def _load_agent_auth():
    path = _PARSERS_DIR / "agent_auth.py"
    if not path.exists():
        raise unittest.SkipTest(f"{path} not found")
    spec = importlib.util.spec_from_file_location("agent_auth", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not build spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["agent_auth"] = module
    spec.loader.exec_module(module)
    return module


agent_auth = _load_agent_auth()

_TEST_TOKEN = "test-agent-token-abc123-def456"


class _FakeHeaders:
    """Case-insensitive header lookup, like a Flask request.headers."""

    def __init__(self, data):
        self._d = {str(k).lower(): v for k, v in (data or {}).items()}

    def get(self, key, default=None):
        return self._d.get(str(key).lower(), default)


class _FakeRequest:
    """Only `.headers.get(...)` is exercised by check_agent_bearer."""

    def __init__(self, headers=None):
        self.headers = _FakeHeaders(headers)


class AgentBearerGateTests(unittest.TestCase):
    def setUp(self):
        # Deterministic server token via env (resolve_agent_token prefers env).
        self._saved = os.environ.get("ZSPAN_AGENT_STATE_TOKEN")
        os.environ["ZSPAN_AGENT_STATE_TOKEN"] = _TEST_TOKEN

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("ZSPAN_AGENT_STATE_TOKEN", None)
        else:
            os.environ["ZSPAN_AGENT_STATE_TOKEN"] = self._saved

    def test_no_auth_header_rejected(self):
        ok, status, _msg = agent_auth.check_agent_bearer(_FakeRequest())
        self.assertFalse(ok)
        self.assertEqual(status, 401)

    def test_forged_role_header_without_bearer_is_rejected(self):
        # THE forged-header case: X-Zspan-Agent-Role is attribution-only and must
        # NOT authorize on its own — no matter how privileged the role claims
        # to be — when no valid bearer accompanies it.
        for role in ("owner", "operator", "admin", "pipeline-operator"):
            with self.subTest(role=role):
                req = _FakeRequest({"X-Zspan-Agent-Role": role})
                ok, status, _msg = agent_auth.check_agent_bearer(req)
                self.assertFalse(ok, f"role {role!r} must not authorize")
                self.assertEqual(status, 401)

    def test_forged_role_plus_wrong_bearer_still_rejected(self):
        req = _FakeRequest(
            {"X-Zspan-Agent-Role": "owner", "Authorization": "Bearer nope"}
        )
        ok, status, _msg = agent_auth.check_agent_bearer(req)
        self.assertFalse(ok)
        self.assertEqual(status, 401)

    def test_wrong_bearer_rejected(self):
        req = _FakeRequest({"Authorization": "Bearer wrong-token-value"})
        ok, status, _msg = agent_auth.check_agent_bearer(req)
        self.assertFalse(ok)
        self.assertEqual(status, 401)

    def test_malformed_authorization_rejected(self):
        # Wrong scheme, empty token, bare token without a scheme — all 401.
        for value in ("Token abc", "Bearer", "Bearer ", "Basic xyz", _TEST_TOKEN):
            with self.subTest(value=value):
                req = _FakeRequest({"Authorization": value})
                ok, status, _msg = agent_auth.check_agent_bearer(req)
                self.assertFalse(ok, f"{value!r} must be rejected")
                self.assertEqual(status, 401)

    def test_correct_bearer_accepted(self):
        req = _FakeRequest({"Authorization": f"Bearer {_TEST_TOKEN}"})
        ok, status, msg = agent_auth.check_agent_bearer(req)
        self.assertTrue(ok)
        self.assertIsNone(status)
        self.assertIsNone(msg)

    def test_bearer_scheme_is_case_insensitive(self):
        # raw[:7].lower() == "bearer " — the scheme token may be any case.
        req = _FakeRequest({"Authorization": f"bEaReR {_TEST_TOKEN}"})
        ok, _status, _msg = agent_auth.check_agent_bearer(req)
        self.assertTrue(ok)

    def test_server_token_unset_returns_503_not_allow(self):
        # An unset server credential guarding live routes must mean
        # "agent access unavailable" (503), never a silent allow.
        original = agent_auth.resolve_agent_token
        agent_auth.resolve_agent_token = lambda: None
        try:
            req = _FakeRequest({"Authorization": f"Bearer {_TEST_TOKEN}"})
            ok, status, msg = agent_auth.check_agent_bearer(req)
            self.assertFalse(ok)
            self.assertEqual(status, 503)
            self.assertEqual(msg, "server_agent_token_not_configured")
        finally:
            agent_auth.resolve_agent_token = original


class ResolveAndHeaderTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("ZSPAN_AGENT_STATE_TOKEN")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("ZSPAN_AGENT_STATE_TOKEN", None)
        else:
            os.environ["ZSPAN_AGENT_STATE_TOKEN"] = self._saved

    def test_resolve_prefers_env(self):
        os.environ["ZSPAN_AGENT_STATE_TOKEN"] = "env-wins-000"
        self.assertEqual(agent_auth.resolve_agent_token(), "env-wins-000")

    def test_bearer_header_attaches_when_configured(self):
        os.environ["ZSPAN_AGENT_STATE_TOKEN"] = "hdr-token-xyz"
        self.assertEqual(
            agent_auth.bearer_header(),
            {"Authorization": "Bearer hdr-token-xyz"},
        )

    def test_bearer_header_empty_when_unconfigured(self):
        # Degrades to {} so ungated sibling commands still run; the gated
        # routes then fail closed on the server's own 401 (never a bare
        # "Bearer " that could confuse the wire).
        original = agent_auth.resolve_agent_token
        agent_auth.resolve_agent_token = lambda: None
        try:
            self.assertEqual(agent_auth.bearer_header(), {})
        finally:
            agent_auth.resolve_agent_token = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
