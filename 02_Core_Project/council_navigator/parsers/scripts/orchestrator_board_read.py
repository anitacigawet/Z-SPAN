#!/usr/bin/env python3.11
"""orchestrator_board_read — read-only board query for the headless orchestrator (S-007 Stage A).

The orchestrator runs in headless mode (`claude -p`) and reads operation state via
this allowlisted wrapper instead of direct curl/HTTP, so its `settings.json` can
pin the URL allowlist *structurally* (curl is denied; only this wrapper is allowed).

Allowed paths are the rung-1 read surface from `agents/orchestrator.md § What I
have to work with`: badges, ingestion governor, work-orders LIST/DETAIL (never
the action subroutes), HQ status, scrape (cached fallback). Anything outside the
allowlist exits non-zero — fails closed, heading off prompt-injection nudging the
agent toward an action endpoint.

Usage:
    python3.11 scripts/orchestrator_board_read.py /api/operator/badges
    python3.11 scripts/orchestrator_board_read.py "/api/ingestion/governor?city=Kingman"
    python3.11 scripts/orchestrator_board_read.py "/api/work-orders?city=Kingman"
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
ROLE = "orchestrator"
TIMEOUT_SECONDS = 10

# Rung-1 read surface (prefix match against the path).
ALLOWED_PATH_PREFIXES = (
    "/api/operator/badges",
    "/api/operator/pending-escalations",
    "/api/ingestion/governor",
    "/api/work-orders",
    "/api/hq/status",
    "/scrape/",
)

# Action subroutes within /api/work-orders/<id>/... that must NEVER be reachable
# from this wrapper, even though the prefix matches. Belt-and-suspenders.
DENIED_PATH_SUBSTRINGS = (
    "/process",
    "/process-next",
    "/approve",
    "/retry",
    "/scan",
    "/confirm-match",
    "/register-notebook",
    "/set-video-url",
    "/build-review-queue",
    "/ingest-responses",
    "/push-to-flagship",
    "/clear-source-cache",
    "/match-videos",
)


def _is_allowed(path: str) -> tuple[bool, str]:
    if not path.startswith("/"):
        return False, f"path must start with /; got {path!r}"
    for denied in DENIED_PATH_SUBSTRINGS:
        if denied in path:
            return False, f"path contains denied action substring {denied!r}: {path}"
    if not any(path.startswith(p) for p in ALLOWED_PATH_PREFIXES):
        return False, f"path not in the rung-1 read allowlist: {path}"
    return True, ""


def _fetch_local(path: str) -> int:
    """PC path: GET Flask :5001 directly. Existing behavior."""
    # Defer requests import so Mac (which doesn't have parsers deps installed)
    # can still use the --http-source path without crashing.
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
    """Mac path: GET PC's agent relay /board?path=<encoded>."""
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
        description="Orchestrator board-read (rung-1 allowlisted Flask GET; relay proxy when --http-source set).",
    )
    parser.add_argument(
        "path",
        help="Flask path to GET. Validated against the rung-1 read allowlist.",
    )
    parser.add_argument(
        "--http-source", default=None,
        help=(
            "Optional. Base URL of PC's agent relay orchestrator namespace "
            "(e.g. http://<relay-host>:5002/api/agents/orchestrator). When set, "
            "proxy the read through the relay instead of hitting 127.0.0.1:5001 "
            "directly. Mac path."
        ),
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
