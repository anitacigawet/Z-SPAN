#!/usr/bin/env python3.11
"""vocabulary_curator_board_read — read-only board query for the headless Vocabulary Curator (S-004).

Mirrors the orchestrator's board-read pattern (D-065): per-agent URL allowlist,
GET-only, role hardcoded so prompt-injection cannot forge attribution.

The curator's reads are narrow per agents/vocabulary-curator.md:
  - /api/vocabulary-inbox[?city=...&threshold=...]  (the inbox itself)
  - /api/city-intelligence/<slug>                   (optional: existing dictionary
                                                     check before promoting)

Usage:
    python3.11 scripts/vocabulary_curator_board_read.py "/api/vocabulary-inbox?city=Kingman&threshold=2"
    python3.11 scripts/vocabulary_curator_board_read.py /api/city-intelligence/kingman
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Make parsers/ importable (for agent_auth) when invoked from scripts/.
_PARSERS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARSERS_DIR not in sys.path:
    sys.path.insert(0, _PARSERS_DIR)

FLASK_BASE = "http://127.0.0.1:5001"
ROLE = "vocabulary-curator"
TIMEOUT_SECONDS = 10

ALLOWED_PATH_PREFIXES = (
    "/api/vocabulary-inbox",
    "/api/city-intelligence/",
)

# Belt-and-suspenders: even within /api/vocabulary-inbox*, the curator should
# never reach the mutating action subpaths via this wrapper. The action wrapper
# is the only path to those endpoints.
DENIED_PATH_SUBSTRINGS = (
    "/promote",
    "/reject",
    "/agent-propose",
)


def _is_allowed(path: str) -> tuple[bool, str]:
    if not path.startswith("/"):
        return False, f"path must start with /; got {path!r}"
    for denied in DENIED_PATH_SUBSTRINGS:
        if denied in path:
            return False, (
                f"path contains denied action substring {denied!r}: "
                f"reads cannot cross into the action wrapper's territory"
            )
    if not any(path.startswith(p) for p in ALLOWED_PATH_PREFIXES):
        return False, f"path not in the vocabulary-curator read allowlist: {path}"
    return True, ""


def _agent_bearer() -> dict:
    """RR-8 SEC-AUTH: attach the fleet bearer so the owner-or-token read
    (/api/city-intelligence/<slug>) authenticates. Degrades to no header if
    agent_auth is unreachable — the ungated /api/vocabulary-inbox read still
    runs; the gated read then fails closed on the server's own 401."""
    try:
        from agent_auth import bearer_header
        return bearer_header()
    except Exception:
        return {}


def _fetch_local(path: str) -> int:
    import requests  # noqa: PLC0415
    url = f"{FLASK_BASE}{path}"
    headers = {
        "X-Zspan-Agent-Role": ROLE,
        "Accept": "application/json",
    }
    headers.update(_agent_bearer())
    try:
        r = requests.get(url, headers=headers, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        print(f"ERROR: GET {url} failed: {exc}", file=sys.stderr)
        return 4
    print(r.text)
    if not r.ok:
        print(f"ERROR: HTTP {r.status_code}", file=sys.stderr)
        return 5
    return 0


def _fetch_via_relay(path: str, http_source: str, bearer: str | None) -> int:
    encoded = urllib.parse.quote(path, safe="")
    url = f"{http_source.rstrip('/')}/board?path={encoded}"
    headers = {"Accept": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                import json  # noqa: PLC0415
                parsed = json.loads(body)
                if isinstance(parsed, dict) and "body" in parsed:
                    print(parsed["body"])
                else:
                    print(body)
            except Exception:
                print(body)
            return 0 if resp.status == 200 else 5
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        print(f"ERROR: HTTP {e.code} from relay: {err_body[:300]}", file=sys.stderr)
        return 5
    except urllib.error.URLError as e:
        print(f"ERROR: could not reach relay at {http_source}: {e}", file=sys.stderr)
        return 5


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Vocabulary Curator board-read (allowlisted Flask GET; relay proxy when --http-source set).",
    )
    parser.add_argument("path", help="Flask path; validated against the curator's read allowlist.")
    parser.add_argument(
        "--http-source", default=None,
        help="Optional. Base URL of PC's agent relay vocabulary-curator namespace. Mac path.",
    )
    parser.add_argument(
        "--http-bearer", default=None,
        help="Bearer token paired with --http-source.",
    )
    args = parser.parse_args()

    ok, reason = _is_allowed(args.path)
    if not ok:
        print(f"DENIED: {reason}", file=sys.stderr)
        return 3

    if args.http_source:
        return _fetch_via_relay(args.path, args.http_source, args.http_bearer)
    return _fetch_local(args.path)


if __name__ == "__main__":
    sys.exit(main())
