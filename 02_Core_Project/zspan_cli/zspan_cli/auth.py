"""Flagship-brokered Google sign-in for the local CLI."""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlencode, urlparse

from zspan_cli.config import flagship_url, redact_key, save_config
from zspan_cli.flagship import (
    FlagshipError,
    exchange_cli_code,
    fetch_cli_me,
    revoke_cli_token,
)

_CALLBACK_TIMEOUT_SECONDS = 300


def current_auth(config: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    auth = (config or {}).get("auth")
    if not isinstance(auth, dict) or not isinstance(auth.get("token"), str):
        return None
    if not auth["token"]:
        return None
    return auth


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server callback name
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_error(404)
            return
        values = {
            key: vals[0]
            for key, vals in parse_qs(parsed.query).items()
            if vals
        }
        self.server.callback_values = values  # type: ignore[attr-defined]
        expected = self.server.expected_state  # type: ignore[attr-defined]
        success = values.get("state") == expected and bool(values.get("code"))
        if values.get("error") == "cancelled":
            message = "Sign-in cancelled. You can return to your terminal."
        elif success:
            message = "You’re signed in — back to your terminal."
        else:
            message = "Sign-in could not be completed. Return to your terminal."
        body = (
            "<!doctype html><html><head><meta name=\"robots\" content=\"noindex\">"
            f"<title>Z-SPAN CLI</title></head><body><p>{message}</p></body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _wait_for_callback(server: HTTPServer, timeout: int) -> Optional[Dict[str, str]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        server.timeout = min(1.0, max(0.0, deadline - time.monotonic()))
        server.handle_request()
        values = getattr(server, "callback_values", None)
        if isinstance(values, dict):
            return values
    return None


def _redacted_error(error: BaseException, *secret_values: str) -> str:
    text = str(error)
    for secret_value in secret_values:
        text = redact_key(text, secret_value)
    return text


def login(config: Optional[Dict[str, Any]]) -> bool:
    """Run the loopback OAuth flow and persist the returned opaque token."""
    try:
        server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    except OSError as e:
        print(
            "sign-in could not start its local callback server "
            f"({type(e).__name__})"
        )
        return False
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    server.expected_state = state  # type: ignore[attr-defined]
    port = int(server.server_address[1])
    base_url = flagship_url(config)
    start_url = base_url.rstrip("/") + "/api/auth/cli/start?" + urlencode(
        {
            "port": str(port),
            "state": state,
            "challenge": challenge,
        }
    )
    print(f"Open this URL on this computer to sign in:\n{start_url}")
    try:
        webbrowser.open(start_url)
    except Exception:
        pass
    print("waiting up to 5 minutes — Ctrl-C to abort")
    try:
        callback = _wait_for_callback(server, _CALLBACK_TIMEOUT_SECONDS)
    except KeyboardInterrupt:
        print("sign-in aborted; nothing was changed")
        return False
    finally:
        server.server_close()

    if callback is None:
        print("sign-in timed out — run `zspan login` to try again")
        return False
    if callback.get("error") == "cancelled":
        print("sign-in cancelled from the browser")
        return False
    if callback.get("state") != state:
        print("sign-in refused because the callback state did not match")
        return False
    code = callback.get("code", "")
    if not code:
        print("sign-in failed because the callback carried no authorization code")
        return False
    try:
        result = exchange_cli_code(base_url, code, verifier)
    except FlagshipError as e:
        print(f"sign-in failed: {_redacted_error(e, code, verifier)}")
        return False
    token = result.get("token")
    account = result.get("account")
    expires_at = result.get("expires_at")
    if not isinstance(token, str) or not token or not isinstance(account, dict):
        print("sign-in failed because the endpoint returned an unexpected response")
        return False
    updated = dict(config or {})
    updated["auth"] = {
        "token": token,
        "email": account.get("email") or "",
        "display_name": account.get("display_name") or "",
        "expires_at": expires_at or "",
    }
    save_config(updated)
    print(f"signed in as {updated['auth']['email']}")
    return True


def logout(config: Optional[Dict[str, Any]]) -> bool:
    auth = current_auth(config)
    updated = dict(config or {})
    if auth is None:
        updated.pop("auth", None)
        if config is not None:
            save_config(updated)
        print("already signed out")
        return True
    token = auth["token"]
    try:
        revoke_cli_token(flagship_url(config), token)
        message = "signed out; this CLI token was revoked"
    except FlagshipError as e:
        if e.status == 401:
            message = "signed out; this CLI token was already inactive"
        else:
            message = "signed out locally; the endpoint could not confirm token revocation"
    updated.pop("auth", None)
    save_config(updated)
    print(message)
    return True


def whoami(config: Optional[Dict[str, Any]], *, verify: bool = False) -> bool:
    auth = current_auth(config)
    if auth is None:
        print("you’re signed out — run `zspan login`")
        return False
    email = auth.get("email") or "unknown account"
    expires_at = auth.get("expires_at") or "unknown"
    print(f"signed in as {email}; token expires {expires_at}")
    if not verify:
        return True
    try:
        live = fetch_cli_me(flagship_url(config), auth["token"])
    except FlagshipError as e:
        if e.status == 401:
            print("live check: expired or revoked — run `zspan login` again")
        elif e.status is None:
            print("live check: unreachable — your local sign-in could not be verified")
        else:
            print("live check: unavailable — the endpoint could not verify your sign-in")
        return False
    account = live.get("account") if isinstance(live.get("account"), dict) else {}
    print(f"live check: valid for {account.get('email') or email}")
    return True
