#!/usr/bin/env python3.11
"""disputed_quotes_reviewer_board_read — read-only board query for the headless DQR (S-004).

Mirrors orchestrator board-read pattern (D-065): per-agent URL allowlist,
GET-only, role hardcoded.

The DQR's reads (per agents/disputed-quotes-reviewer.md):
  - /api/disputed-quotes[?city=...]        (the disputed queue)
  - /api/operator/pending-escalations      (already-escalated-skipped lookup —
                                            reading the agent's own prior outputs)

Usage:
    python3.11 scripts/disputed_quotes_reviewer_board_read.py /api/disputed-quotes
    python3.11 scripts/disputed_quotes_reviewer_board_read.py "/api/disputed-quotes?city=Kingman"
    python3.11 scripts/disputed_quotes_reviewer_board_read.py /api/operator/pending-escalations
"""
from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

FLASK_BASE = "http://127.0.0.1:5001"
ROLE = "disputed-quotes-reviewer"
TIMEOUT_SECONDS = 10

ALLOWED_PATH_PREFIXES = (
    "/api/disputed-quotes",
    "/api/operator/pending-escalations",
)

# Belt-and-suspenders: even within /api/disputed-quotes*, the reader cannot
# reach the mutating subroutes via this wrapper. The action wrapper is the
# only path to those endpoints.
DENIED_PATH_SUBSTRINGS = (
    "/resolve",
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
        return False, f"path not in the disputed-quotes-reviewer read allowlist: {path}"
    return True, ""


def _fetch_local(path: str) -> int:
    import requests  # noqa: PLC0415
    url = f"{FLASK_BASE}{path}"
    headers = {
        "X-Zspan-Agent-Role": ROLE,
        "Accept": "application/json",
    }
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
        description="Disputed Quotes Reviewer board-read (allowlisted Flask GET; relay proxy when --http-source set).",
    )
    parser.add_argument("path", help="Flask path; validated against DQR's read allowlist.")
    parser.add_argument(
        "--http-source", default=None,
        help="Optional. Base URL of PC's agent relay disputed-quotes-reviewer namespace. Mac path.",
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
