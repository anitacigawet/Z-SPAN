"""google_oauth — Google OAuth 2.0 (authorization code + PKCE + state)
plus stateless HMAC-signed session JWTs for the Z-SPAN light-account flow.

Per [`ACCOUNT_SYSTEM_SPEC.md`](../../01_Project_Overview/ACCOUNT_SYSTEM_SPEC.md)
chunk 2. Deps: stdlib + `requests` (already in requirements.txt). No new
dependencies.

Flow summary:
    1. `/api/auth/google/login` calls `build_consent_url()` after stashing
       a signed transient cookie carrying `{state, code_verifier, next}`
       so the callback can verify CSRF + complete PKCE without server-side
       session state.
    2. Google → `/api/auth/google/callback?code&state` — the route reads
       the transient cookie via `verify_oauth_state_cookie()`, asserts
       state-match, calls `exchange_code()` + `fetch_userinfo()`, then
       `upsert_user_from_google()` (account_system helper), and finally
       `mint_session_token()` placed in a long-lived signed cookie.
    3. `/api/auth/me` reads the session cookie via
       `verify_session_token()`, looks the user up, returns the
       authenticated principal.
    4. `/api/auth/logout` clears the session cookie.

All cookies are HMAC-SHA256 signed using `ZSPAN_SESSION_SECRET` when set.
Local/self-hosted environments can fall back to a secret persisted to
`user_settings.json` (auto-generated on first use via
`get_or_create_jwt_secret()` so the operator never types it in).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Optional, Tuple
from urllib.parse import urlencode, urlparse

import requests

from env_config import load_user_settings, save_user_settings

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

OAUTH_SCOPES = "openid email profile"

# Cookie names — namespaced so they don't collide with other site cookies.
SESSION_COOKIE_NAME = "zspan_session"
OAUTH_STATE_COOKIE_NAME = "zspan_oauth_state"

# Lifetimes.
SESSION_TTL_SECONDS = 30 * 24 * 3600  # 30 days
OAUTH_STATE_TTL_SECONDS = 10 * 60  # 10 minutes — covers Google consent screen


# ── Settings accessors ────────────────────────────────────────────────


def get_oauth_client_credentials() -> Tuple[str, str]:
    """Return (client_id, client_secret) from user_settings.json, falling
    back to GOOGLE_OAUTH_WEB_CLIENT_ID / GOOGLE_OAUTH_WEB_CLIENT_SECRET
    environment variables when the settings-file value is empty.

    The env-var path is the flagship-deploy story (Railway container has
    no user_settings.json — the file is gitignored + per-machine); the
    settings-file path is the local-dev / self-host default.

    Raises RuntimeError if BOTH sources are empty — the operator must
    place the values via the Google Cloud Console one-time setup per the
    SPEC's James's-human-API-steps section. Empty strings count as
    missing.
    """
    settings = load_user_settings()
    client_id = (settings.get("google_oauth_web_client_id") or "").strip()
    client_secret = (settings.get("google_oauth_web_client_secret") or "").strip()
    if not client_id:
        client_id = (os.environ.get("GOOGLE_OAUTH_WEB_CLIENT_ID") or "").strip()
    if not client_secret:
        client_secret = (os.environ.get("GOOGLE_OAUTH_WEB_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        raise RuntimeError(
            "google_oauth_web_client_id / google_oauth_web_client_secret missing "
            "from user_settings.json AND the GOOGLE_OAUTH_WEB_CLIENT_ID / "
            "GOOGLE_OAUTH_WEB_CLIENT_SECRET env vars — set either path per "
            "ACCOUNT_SYSTEM_SPEC.md § James's human-API steps."
        )
    return client_id, client_secret


def get_owner_email() -> str:
    """Return the canonical (primary) operator/owner email, lowercased.

    Kept as the singular accessor for callers that need ONE email to
    display or log. Multi-owner membership checks should use
    `get_owner_emails()` / `is_owner_email()` instead.

    Resolution: `owner_emails` (list) → legacy `owner_email` (str) →
    env OWNER_EMAIL. Returns "" when no owner identity is configured
    (a deployment without owner config simply has no owner).
    """
    emails = get_owner_emails()
    if emails:
        # Sets are unordered; sort so display strings stay stable.
        return sorted(emails)[0]
    return ""


def get_owner_emails() -> set[str]:
    """Return the set of all configured operator/owner emails (lowercased).

    Per V1-Polish-2 (2026-06-14) the operator identity is unified to
    the Google-OAuth principal whose email is in this set, superseding
    the old local-dev-auto-owner default in the (now-retired)
    `useFlagshipUser` hook. Multi-owner support (2026-06-21) lets the
    operator hold owner access on more than one account.

    Sources (UNION):
      - `owner_emails` list in user_settings.json (preferred new key)
      - `owner_email` str in user_settings.json (legacy single-owner)
      - env OWNER_EMAIL (single)
    If none configured, the set is EMPTY: a deployment without owner
    config has no owner (owner-gated surfaces deny everyone). Cloud
    deployments without a settings file MUST set OWNER_EMAIL.
    """
    settings = load_user_settings()
    out: set[str] = set()

    raw_list = settings.get("owner_emails") or []
    if isinstance(raw_list, list):
        for entry in raw_list:
            if isinstance(entry, str) and entry.strip():
                out.add(entry.strip().lower())

    legacy = (settings.get("owner_email") or "").strip()
    if legacy:
        out.add(legacy.lower())

    # Comma-separated so cloud deployments (no settings file) can carry
    # the same multi-owner set as local user_settings.json.
    env_email = os.environ.get("OWNER_EMAIL", "").strip()
    if env_email:
        for part in env_email.split(","):
            if part.strip():
                out.add(part.strip().lower())

    if not out:
        logger.warning(
            "no owner identity configured (owner_emails/owner_email/"
            "OWNER_EMAIL all absent) — owner-gated surfaces will deny everyone"
        )
    return out


def is_owner_email(email: Optional[str]) -> bool:
    """True iff `email` is in the configured set of operator/owner
    emails (case-insensitive). Anonymous / non-owner → False.
    """
    if not email:
        return False
    return email.strip().lower() in get_owner_emails()


def get_operator_search_allowlist() -> set[str]:
    """Return the set of emails permitted to use V1.5-OperatorSearch-1.

    Wider than `get_owner_emails` by intent: per the handoff spec
    (2026-06-25), the operator-search test cohort can include trusted
    secondary accounts beyond the canonical owner principal. The
    feature is operator-only at the V2-public-query-gate-sidestep
    level (no public user touches it, no untrusted input — per
    [D-137](../../../01_Project_Overview/DECISIONS.md#d-137)); the
    allowlist exists so testing from a secondary account doesn't
    require swapping into the canonical owner session.

    Sources (UNION over `get_owner_emails`):
      - owner emails (always included — the gate widens, never narrows)
      - `operator_search_allowlist` list in user_settings.json
      - env OPERATOR_SEARCH_ALLOWLIST (comma-separated)
    """
    extras: set[str] = set(get_owner_emails())

    settings = load_user_settings()
    raw_list = settings.get("operator_search_allowlist") or []
    if isinstance(raw_list, list):
        for entry in raw_list:
            if isinstance(entry, str) and entry.strip():
                extras.add(entry.strip().lower())

    env_csv = os.environ.get("OPERATOR_SEARCH_ALLOWLIST", "").strip()
    if env_csv:
        for raw in env_csv.split(","):
            r = raw.strip()
            if r:
                extras.add(r.lower())

    return extras


def is_operator_search_principal(email: Optional[str]) -> bool:
    """True iff `email` is permitted to use V1.5-OperatorSearch-1.

    Strictly wider than `is_owner_email` — every owner is also an
    operator-search principal, but the operator-search allowlist may
    include additional trusted secondary accounts (test cohorts,
    dev-personal Google accounts). Other owner-gated surfaces continue
    to use `is_owner_email` directly; this helper is scoped to the
    operator-search feature.
    """
    if not email:
        return False
    return email.strip().lower() in get_operator_search_allowlist()


def get_or_create_jwt_secret() -> bytes:
    """Return the HMAC signing secret used for both the session JWT and
    the transient OAuth state cookie.

    `ZSPAN_SESSION_SECRET` is authoritative when set. Local development
    and self-hosted installs retain the existing fallback: lazily generate
    a 64-byte url-safe secret on first call and persist it to
    user_settings.json under `jwt_session_signing_secret`.
    """
    env_secret = os.environ.get("ZSPAN_SESSION_SECRET")
    if env_secret:
        logger.info("google_oauth: using env-backed ZSPAN_SESSION_SECRET")
        return env_secret.encode("utf-8")

    logger.warning(
        "google_oauth: ZSPAN_SESSION_SECRET is unset; falling back to "
        "user_settings.json jwt_session_signing_secret"
    )
    settings = load_user_settings()
    secret = settings.get("jwt_session_signing_secret")
    if not secret:
        secret = secrets.token_urlsafe(64)
        settings["jwt_session_signing_secret"] = secret
        save_user_settings(settings)
        logger.info(
            "google_oauth: generated jwt_session_signing_secret (first run)"
        )
    return secret.encode("utf-8")


# ── base64url helpers (no padding) ────────────────────────────────────


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s + pad)


# ── PKCE + state ──────────────────────────────────────────────────────


def generate_pkce() -> Tuple[str, str]:
    """Return (verifier, challenge_S256). Verifier is 64 bytes of
    url-safe random; challenge is SHA-256(verifier) base64url-encoded
    without padding — both per RFC 7636.
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = _b64url_encode(digest)
    return verifier, challenge


def random_state(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


# ── HMAC-signed envelope (used by both state cookie + JWT) ────────────


def _sign_envelope(payload: dict) -> str:
    """Return `<base64url(payload_json)>.<base64url(hmac_sig)>`.

    Used for the transient OAuth state cookie. The session JWT uses the
    proper three-segment HS256 JWT format below.
    """
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(
        get_or_create_jwt_secret(), body.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{body}.{_b64url_encode(sig)}"


def _verify_envelope(token: str) -> Optional[dict]:
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(
        get_or_create_jwt_secret(), body.encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(_b64url_decode(sig), expected):
        return None
    try:
        return json.loads(_b64url_decode(body))
    except (ValueError, json.JSONDecodeError):
        return None


# ── OAuth state cookie (carries state + code_verifier + next) ─────────


def build_oauth_state_cookie(state: str, code_verifier: str, next_url: str) -> str:
    """Encode the transient pre-redirect payload as a signed cookie value.

    The cookie should be set with HttpOnly + SameSite=Lax (so it returns
    on the callback redirect from accounts.google.com), Path=/, short
    Max-Age, and Secure in prod. The route helpers below set these.
    """
    payload = {
        "state": state,
        "code_verifier": code_verifier,
        "next": next_url,
        "exp": int(time.time()) + OAUTH_STATE_TTL_SECONDS,
    }
    return _sign_envelope(payload)


def verify_oauth_state_cookie(value: str, expected_state: str) -> Optional[dict]:
    """Return the cookie payload if the signature checks out, the
    embedded `state` matches the request's `state` query param, and the
    cookie hasn't expired. Returns None otherwise — the callback should
    reject the request when this returns None.
    """
    payload = _verify_envelope(value)
    if not payload:
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    if payload.get("state") != expected_state:
        return None
    return payload


# ── Session JWT (HS256, three-segment) ────────────────────────────────


def mint_session_token(user_id: int, role: str = "light") -> str:
    """Return a signed three-segment HS256 JWT. The token carries
    `{sub: user_id, role, iat, exp}` — minimal claims, no refresh
    machinery (per the SPEC's stateless-cookie posture).
    """
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": str(user_id),
        "role": role,
        "iat": now,
        "exp": now + SESSION_TTL_SECONDS,
    }
    header_b64 = _b64url_encode(
        json.dumps(header, separators=(",", ":")).encode("utf-8")
    )
    payload_b64 = _b64url_encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    sig = hmac.new(
        get_or_create_jwt_secret(), signing_input, hashlib.sha256
    ).digest()
    return f"{header_b64}.{payload_b64}.{_b64url_encode(sig)}"


def verify_session_token(token: str) -> Optional[dict]:
    """Verify the HS256 signature + expiry. Returns the payload dict on
    success or None on any failure (bad signature, malformed, expired).
    Never raises — callers can use a falsy check.
    """
    if not token or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    header_b64, payload_b64, sig_b64 = parts
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    expected = hmac.new(
        get_or_create_jwt_secret(), signing_input, hashlib.sha256
    ).digest()
    try:
        actual = _b64url_decode(sig_b64)
    except Exception:
        return None
    if not hmac.compare_digest(actual, expected):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < int(time.time()):
        return None
    return payload


# ── Google API calls ──────────────────────────────────────────────────


def build_consent_url(
    state: str,
    code_challenge: str,
    redirect_uri: str,
) -> str:
    """Return the Google consent URL the browser should be 302'd to."""
    client_id, _ = get_oauth_client_credentials()
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": OAUTH_SCOPES,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "access_type": "online",
        # `prompt=select_account` lets the user pick a different Google
        # account on every sign-in even if one is already authenticated
        # in the browser — useful for the operator who toggles between
        # personal + project Google accounts during development.
        "prompt": "select_account",
        "include_granted_scopes": "true",
    }
    return f"{GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(
    code: str,
    code_verifier: str,
    redirect_uri: str,
) -> dict:
    """POST to Google's token endpoint to redeem the authorization code
    for tokens. Returns the parsed JSON response. Raises
    requests.HTTPError on non-2xx responses (the callback handler should
    treat this as a 502).
    """
    client_id, client_secret = get_oauth_client_credentials()
    response = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        },
        timeout=15,
        headers={"Accept": "application/json"},
    )
    response.raise_for_status()
    return response.json()


def fetch_userinfo(access_token: str) -> dict:
    """GET Google's openid-userinfo endpoint. Returns
    `{sub, email, email_verified, name, picture, given_name, ...}`.
    """
    response = requests.get(
        GOOGLE_USERINFO_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


# ── Redirect-URI helpers ──────────────────────────────────────────────


def compute_redirect_uri(request_host_url: str) -> str:
    """Pick the right redirect_uri for the current request context.

    The OAuth Web client uses these redirect URIs:
      - http://localhost:3000/api/auth/google/callback (dev)
      - https://zspan.org/api/auth/google/callback   (prod)
      - https://operator.zspan.org/api/auth/google/callback (operator)

    Either URI must MATCH EXACTLY what the client app was configured
    with at consent time + at token-exchange time, so we cannot
    dynamically build one off the request host (a Cloudflare-fronted
    request would pass through as zspan.org which is correct; a local
    Flask :5001 request would pass through as 127.0.0.1:5001 which is
    NOT registered). We pick by host substring instead.
    """
    host = (request_host_url or "").lower()
    # Allow override (handy for testing); default by host inspection.
    override = os.environ.get("ZSPAN_OAUTH_REDIRECT_URI", "").strip()
    if override:
        return override
    # Exact-hostname match (urlparse extracts host out of scheme/port/path).
    # The earlier "<name> in host" substring match was loose — would have
    # matched malicious-lab.zspan.org.attacker.com if the Host header ever
    # arrived crafted. Not currently exploitable because Google validates
    # the redirect_uri against registered exact strings at OAuth-flow time,
    # but defense-in-depth says match exactly here too.
    parse_target = host if "//" in host else f"http://{host}"
    try:
        hostname = (urlparse(parse_target).hostname or "").lower()
    except Exception:
        hostname = ""
    # lab.zspan.org dev tunnel (cloudflared → localhost Vite). Checked
    # BEFORE the bare zspan.org case so the subdomain wins. The lab
    # redirect URI must also be registered on the Flask OAuth client
    # in Google Cloud Console — added 2026-06-30 in the same Phase 2.6
    # substrate commit (edd3729).
    if hostname == "lab.zspan.org":
        return "https://lab.zspan.org/api/auth/google/callback"
    if hostname == "operator.zspan.org":
        return "https://operator.zspan.org/api/auth/google/callback"
    if hostname == "zspan.org":
        return "https://zspan.org/api/auth/google/callback"
    # Dev fallback — both 127.0.0.1:3000 and localhost:3000 map to the
    # Express dev gateway. Google's redirect-URI matcher requires the
    # exact registered string, so always emit localhost:3000.
    return "http://localhost:3000/api/auth/google/callback"
