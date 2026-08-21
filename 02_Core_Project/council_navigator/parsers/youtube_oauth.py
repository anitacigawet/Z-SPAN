"""
youtube_oauth — OAuth 2.0 flow for the YouTube Data API.
==========================================================================

T-009 Phase 2 (Z-SPAN Proofs YouTube channel automation). The flow runs
ONCE — James clicks "Authorize" in his browser, we save the refresh
token, the worker uses it from then on to mint access tokens
automatically. No further user interaction needed unless the token is
revoked or James wants to re-authenticate.

Why we need OAuth in addition to the YOUTUBE_DATA_API_KEY that the
T-004 video matcher already uses: API keys authorize READ operations
on public YouTube data. UPLOAD operations require user-context auth
(the caller is acting on behalf of a specific Google account that owns
the target channel), which is OAuth's job.

Doctrinal note: this is the third write-credential surface in the
project (alongside the OpenAI API key). Both are
gitignored. The refresh token grants persistent upload access to
James's YouTube channel — handle with the same care as a long-lived
API key.

File locations
--------------

  * OAuth client secret (the file James downloads from Google Cloud
    Console → Credentials → OAuth 2.0 Client ID, Desktop application):
        env var ZSPAN_YOUTUBE_OAUTH_CLIENT (preferred), OR
        parsers/secrets/client_secret.json (canonical home since 2026-06-13), OR
        ZSPAN/client_secret*.json (legacy root location — auto-detected fallback)

  * Refresh token (created by the consent flow, used by the uploader):
        env var ZSPAN_YOUTUBE_REFRESH_TOKEN (preferred), OR
        parsers/secrets/youtube_refresh_token.json (canonical home since 2026-06-13), OR
        ZSPAN/youtube_refresh_token.json (legacy root — read-only fallback if present)

    The parsers/secrets/ directory is gitignored as a whole (directory rule,
    not filename patterns) — relocated from repo root per the D-104 audit's
    F-1 observation so credential safety doesn't depend on exact filenames.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# YouTube Data API v3 scopes. Upload is the heavy one; readonly lets us
# verify the channel exists + read upload status. We grant both so the
# uploader can confirm + retry intelligently.
YOUTUBE_OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


def _zspan_root() -> Path:
    """Walk up from this module to the ZSPAN repo root.

    parsers/youtube_oauth.py
      .parent           → parsers/
      .parent.parent    → council_navigator/
      .parent.parent.parent → 02_Core_Project/
      .parent.parent.parent.parent → ZSPAN/
    """
    return Path(__file__).resolve().parent.parent.parent.parent


def _secrets_dir() -> Path:
    """parsers/secrets/ — the canonical (gitignored-by-directory) credential home."""
    return Path(__file__).resolve().parent / "secrets"


def find_client_secret_path() -> Optional[Path]:
    """Locate the OAuth client_secret JSON.

    Resolution order:
      1. ZSPAN_YOUTUBE_OAUTH_CLIENT env var (full path)
      2. parsers/secrets/client_secret.json (canonical home)
      3. parsers/secrets/, glob `client_secret*.json` (re-downloads dropped in
         with the Google-issued filename still resolve)
      4. ZSPAN repo root, glob `client_secret*.googleusercontent.com.json`
         then `client_secret*.json` (legacy location — back-compat)

    Returns None if not found.
    """
    env_path = os.environ.get("ZSPAN_YOUTUBE_OAUTH_CLIENT")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p
        logger.warning(
            "ZSPAN_YOUTUBE_OAUTH_CLIENT set but file doesn't exist: %s",
            env_path,
        )

    canonical = _secrets_dir() / "client_secret.json"
    if canonical.exists():
        return canonical

    if _secrets_dir().is_dir():
        matches = list(_secrets_dir().glob("client_secret*.json"))
        if matches:
            matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return matches[0]

    root = _zspan_root()
    for pattern in (
        "client_secret*.googleusercontent.com.json",
        "client_secret*.json",
    ):
        matches = list(root.glob(pattern))
        if matches:
            # If multiple, take the most-recently-modified one.
            matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            logger.info(
                "OAuth client secret found at the legacy repo-root location; "
                "the canonical home is parsers/secrets/client_secret.json"
            )
            return matches[0]

    return None


def get_token_path() -> Path:
    """Path where the refresh token is persisted.

    Resolution order: env override → parsers/secrets/ (canonical) →
    legacy repo-root file IF it already exists (read back-compat).
    Fresh tokens always persist to the canonical secrets/ path.
    """
    env_path = os.environ.get("ZSPAN_YOUTUBE_REFRESH_TOKEN")
    if env_path:
        return Path(env_path)
    canonical = _secrets_dir() / "youtube_refresh_token.json"
    legacy = _zspan_root() / "youtube_refresh_token.json"
    if not canonical.exists() and legacy.exists():
        return legacy
    return canonical


def run_consent_flow():
    """Run the one-time browser OAuth consent flow. Returns the resulting
    `google.oauth2.credentials.Credentials` object (also persisted to
    `get_token_path()` for subsequent worker runs).

    Mechanics:
      - Starts an ephemeral local HTTP server on a free port.
      - Opens the user's browser to Google's consent page with a
        redirect_uri pointing at that local server.
      - User clicks "Authorize"; Google redirects back with an
        authorization code.
      - We exchange the code for an access token + refresh token.
      - Save credentials (including refresh token) to disk.

    Raises FileNotFoundError if the client_secret JSON can't be found,
    plus any propagated errors from the OAuth library.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_secret = find_client_secret_path()
    if client_secret is None:
        raise FileNotFoundError(
            "Could not find OAuth client_secret JSON. Download it from "
            "Google Cloud Console (Credentials → OAuth 2.0 Client ID → "
            "Desktop application) and save it to the ZSPAN repo root, "
            "OR set ZSPAN_YOUTUBE_OAUTH_CLIENT to the full path."
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(client_secret),
        YOUTUBE_OAUTH_SCOPES,
    )
    # access_type="offline" + prompt="consent" guarantees we get a
    # refresh token (Google omits it on consent screens that have
    # already been approved unless explicitly requested).
    credentials = flow.run_local_server(
        port=0,                # let the OS pick a free port
        access_type="offline",
        prompt="consent",
        open_browser=True,
    )

    save_credentials(credentials)
    return credentials


def save_credentials(creds) -> Path:
    """Persist the credentials' refresh token JSON to disk. Returns the
    path written. The file is gitignored by the patterns added to
    `.gitignore` 2026-05-16.
    """
    token_path = get_token_path()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    logger.info("youtube_oauth: saved refresh token to %s", token_path)
    return token_path


def load_credentials():
    """Load saved credentials from disk. Returns a
    `google.oauth2.credentials.Credentials` (with refresh-token attached)
    or None if not authorized / refresh failed.

    Automatically refreshes the access token if it's expired but the
    refresh token is still valid. Persists the refreshed credentials
    back to disk.
    """
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google.auth.exceptions import RefreshError

    token_path = get_token_path()
    if not token_path.exists():
        return None

    try:
        creds = Credentials.from_authorized_user_file(
            str(token_path), YOUTUBE_OAUTH_SCOPES,
        )
    except Exception as e:
        logger.warning(
            "youtube_oauth: failed to load credentials from %s: %s",
            token_path, e,
        )
        return None

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save_credentials(creds)
        except RefreshError as e:
            logger.warning(
                "youtube_oauth: refresh failed (token may be revoked): %s. "
                "Re-run setup_youtube_auth.py to re-authorize.", e,
            )
            return None

    if not creds.valid:
        logger.warning(
            "youtube_oauth: credentials loaded but not valid. "
            "Re-run setup_youtube_auth.py."
        )
        return None
    return creds


def is_authorized() -> bool:
    """True iff a refresh token is on disk and currently usable
    (refreshes the access token if needed; returns False on refresh
    failure)."""
    return load_credentials() is not None


def build_youtube_service():
    """Return an authenticated `googleapiclient.discovery.Resource` for
    youtube/v3. Raises if not authorized — callers should check
    `is_authorized()` first or handle the exception.
    """
    from googleapiclient.discovery import build

    creds = load_credentials()
    if creds is None:
        raise RuntimeError(
            "YouTube OAuth not configured. Run "
            "`python -m zspan_pipeline.scripts.setup_youtube_auth` "
            "to authorize."
        )
    return build("youtube", "v3", credentials=creds, cache_discovery=False)
