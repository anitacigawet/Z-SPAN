"""Neutral fleet-agent bearer-token auth — RR-8 SEC-AUTH-1/2/3.

A small, dependency-light helper so `api_server.py` can gate the handful of
routes reachable by BOTH the owner (browser, session cookie) and the headless
fleet agents (localhost, `ZSPAN_AGENT_STATE_TOKEN` bearer) — without importing
the DB-heavy worker blueprint.

Design converged in the session-56 (2026-07-11) Claude↔Codex design review:
  * The server token is a REQUIRED security dependency for the agent path.
    Unset ⇒ 503 (unavailable), never a silent allow, never opt-in like the
    edge token — an unset credential guarding live mutation routes must mean
    "agent access unavailable", not "feature disabled".
  * Constant-time compare (`hmac.compare_digest`); never `compare_digest(x,
    expected or "")`; resolve `expected` first and bail if absent.
  * The `X-Zspan-Agent-Role` header stays ATTRIBUTION-ONLY. One shared token
    authenticates "some fleet process", not a specific role — cross-agent
    separation would need scoped/per-role tokens, not more header checks.

The owner short-circuit lives in `api_server._require_owner_or_agent_token`
(it needs `_current_user_from_cookie` / `is_owner_email`); this module owns
only the token half so it stays free of Flask-app + DB imports.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_TOKEN_ENV = "ZSPAN_AGENT_STATE_TOKEN"
_SETTINGS_KEY = "zspan_agent_state_token"


def resolve_agent_token() -> Optional[str]:
    """The configured fleet token: env first, then `user_settings.json`.
    Returns None when neither is set (→ the agent path is unavailable)."""
    env = os.environ.get(_TOKEN_ENV, "").strip()
    if env:
        return env
    try:
        settings_path = Path(__file__).with_name("user_settings.json")
        if settings_path.is_file():
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            tok = (data.get(_SETTINGS_KEY) or "").strip()
            return tok or None
    except Exception as e:  # never let a settings read error masquerade as auth
        logger.warning("agent_auth: could not read %s (%s)", _SETTINGS_KEY, e)
    return None


def bearer_header() -> dict:
    """Client-side convenience for the headless fleet action wrappers: the
    ``Authorization`` header they attach so the owner-or-token routes
    authenticate. Returns ``{}`` when no token is configured — the ungated
    sibling commands (promote / reject / process / …) still run, and the gated
    routes fail closed with the server's own 401. Server and clients resolve
    the same token through the SAME ``resolve_agent_token`` so they can't drift.
    """
    tok = resolve_agent_token()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def check_agent_bearer(request) -> Tuple[bool, Optional[int], Optional[str]]:
    """Validate the request's `Authorization: Bearer <token>` against the
    configured fleet token.

    Returns (ok, status, message):
      * (True, None, None)                          — valid bearer
      * (False, 503, 'server_agent_token_not_configured') — server has no token
      * (False, 401, '<reason>')                    — missing / malformed / mismatch

    Never treats "unset on both sides" as a match; never sends the caller past
    a missing server token.
    """
    expected = resolve_agent_token()
    if not expected:
        return False, 503, "server_agent_token_not_configured"
    raw = (request.headers.get("Authorization") or "").strip()
    if len(raw) < 8 or raw[:7].lower() != "bearer ":
        return False, 401, "missing or malformed bearer token"
    presented = raw[7:].strip()
    if not presented:
        return False, 401, "empty bearer token"
    if not hmac.compare_digest(presented, expected):
        return False, 401, "invalid bearer token"
    return True, None, None
