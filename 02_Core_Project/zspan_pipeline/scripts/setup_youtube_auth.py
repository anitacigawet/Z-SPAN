#!/usr/bin/env python3.11
"""
One-time YouTube OAuth consent flow (T-009 Phase 2 setup).

Usage:
    cd 02_Core_Project
    python3.11 -m zspan_pipeline.scripts.setup_youtube_auth

(Use `python3.11` explicitly — the project's other deps live under 3.11
ARM64. The default `python` on this machine is 3.12 x64 and won't have
the Google libs installed.)

What it does:
  1. Finds the OAuth client_secret JSON (downloaded from Google Cloud
     Console → Credentials → OAuth 2.0 Client ID → Desktop application).
  2. Opens your browser to Google's consent page.
  3. You click "Allow" to grant Z-SPAN permission to upload videos to
     your YouTube channel.
  4. We save the resulting refresh token locally (gitignored).

After this runs once, the worker uses the refresh token to mint access
tokens automatically. No further user interaction needed unless the
token is revoked or you want to re-authenticate against a different
Google account.

Re-running this script when a token is already saved is a no-op — it
prints the current status and exits without re-prompting. Delete the
token file to force re-authorization.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make parsers/ importable
_PARSERS_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "council_navigator"
    / "parsers"
)
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

from youtube_oauth import (  # noqa: E402
    find_client_secret_path,
    get_token_path,
    load_credentials,
    run_consent_flow,
)


def main() -> int:
    print("=" * 64)
    print("  YouTube OAuth setup (T-009 Phase 2 — Z-SPAN Proofs uploader)")
    print("=" * 64)
    print()

    # Step 1 — locate the OAuth client secret.
    client_secret = find_client_secret_path()
    if client_secret is None:
        print("ERROR: Could not find OAuth client_secret JSON.")
        print()
        print("To fix this:")
        print()
        print("  1. Open https://console.cloud.google.com/apis/credentials")
        print("     for the Google Cloud project where YOUTUBE_DATA_API_KEY")
        print("     lives (the same project the T-004 matcher uses).")
        print("  2. Click '+ Create Credentials' → 'OAuth client ID' →")
        print("     application type 'Desktop application'. Name it")
        print("     anything (e.g., 'Z-SPAN Proofs Uploader').")
        print("  3. Download the resulting JSON file.")
        print("  4. Save it to the ZSPAN repo root (the auto-discovery")
        print("     looks for client_secret*.json there), OR set the env")
        print("     var ZSPAN_YOUTUBE_OAUTH_CLIENT to its full path.")
        print("  5. Re-run this script.")
        return 1

    print(f"  Client secret : {client_secret.name}")
    print(f"  Token will save to: {get_token_path()}")
    print()

    # Step 2 — short-circuit if already authorized.
    existing = load_credentials()
    if existing is not None:
        print("Already authorized — refresh token loaded successfully.")
        print()
        print("To re-authorize (different Google account, scope change,")
        print("revoked token, etc.):")
        print()
        print(f"  delete  {get_token_path()}")
        print("  re-run  python -m zspan_pipeline.scripts.setup_youtube_auth")
        print()
        return 0

    # Step 3 — run the browser consent flow.
    print("Opening your browser for Google OAuth consent...")
    print()
    print("In the browser:")
    print("  • Sign in with the Google account that owns your Z-SPAN")
    print("    Proofs YouTube channel.")
    print("  • You'll see a 'Z-SPAN wants to access your Google Account'")
    print("    screen — review the requested permissions (upload + read")
    print("    YouTube data) and click 'Allow'.")
    print("  • The browser will show 'The authentication flow has")
    print("    completed' — you can close that tab afterward.")
    print()
    print("Waiting for browser consent...")

    try:
        run_consent_flow()
    except FileNotFoundError as e:
        print(f"\nERROR: {e}")
        return 1
    except Exception as e:
        print(f"\nERROR during consent flow: {e}")
        print()
        print("Common causes:")
        print("  • You declined the consent screen — re-run to retry.")
        print("  • The client_secret JSON is for a different application")
        print("    type (must be 'Desktop application').")
        print("  • Browser blocked the local callback port — try a")
        print("    different browser or restart the script.")
        return 2

    print()
    print(f"Saved refresh token to: {get_token_path()}")
    print()
    print("Done. The uploader can now publish to your channel without")
    print("further interaction.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
