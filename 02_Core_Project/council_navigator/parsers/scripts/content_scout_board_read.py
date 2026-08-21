#!/usr/bin/env python3.11
"""content_scout_board_read — read-only board query for the headless Content Scout (S-004).

The Content Scout is a Sonnet 4.6 watcher that polls a focus list of cities once
per session, diffs each city's cached calendar against its prior snapshot, and
escalates new meetings + anomalies via Slack. Its scope is narrow: it reads two
endpoints and never advances the queue.

This wrapper is the structural URL-allowlist for the scout's reads (Claude Code's
Bash permission syntax has no mid-string wildcard for HTTP URLs; the wrapper is
the only way to enforce "only these read paths"). It mirrors the orchestrator's
board-read pattern (D-065) — same shape, scout's narrower allowlist, role
hardcoded to 'content-scout' so prompt-injection cannot forge attribution.

Hard rule from agents/content-scout.md: NEVER pass ?refresh=true. The cache TTL
(D-038/D-039) is sacred — a refresh forces a live scrape, which violates the
once-per-session cadence and risks calendar-spam at the city's site. The
wrapper rejects ?refresh=true structurally.

Endpoint note: the canonical Flask GET that returns a city's cached calendar
is `/scrape/<city>` (per api_server.py line 163; D-039). Older agent docs
reference `/api/calendar/events` which is an *Express :3000* POST proxy — not
reachable from headless scope (we read Flask :5001 directly, no Express layer).

Usage:
    python3.11 scripts/content_scout_board_read.py /scrape/Kingman
    python3.11 scripts/content_scout_board_read.py /api/operator/badges
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
ROLE = "content-scout"
TIMEOUT_SECONDS = 30  # scrape can be slow on cold cache; cached returns are <1s

ALLOWED_PATH_PREFIXES = (
    "/scrape/",
    "/api/operator/badges",
)

DENIED_QUERY_SUBSTRINGS = (
    "refresh=true",
    "refresh=1",
)


def _is_allowed(path: str) -> tuple[bool, str]:
    if not path.startswith("/"):
        return False, f"path must start with /; got {path!r}"
    for denied in DENIED_QUERY_SUBSTRINGS:
        if denied in path:
            return False, (
                f"path contains denied query {denied!r}: ?refresh=true violates "
                f"D-038/D-039 cache-respect; scout reads cache only"
            )
    if not any(path.startswith(p) for p in ALLOWED_PATH_PREFIXES):
        return False, f"path not in the content-scout read allowlist: {path}"
    return True, ""


def _fetch_local(path: str) -> int:
    """PC path: GET Flask :5001 directly. Existing behavior."""
    # Defer the requests import so Mac (which doesn't have parsers deps
    # installed) can still use the --http-source path without crashing.
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
    """Mac path: GET PC's agent relay /board?path=<encoded>.

    The relay validates the same allowlist server-side via the same wrapper
    running locally on PC; the network call here is just transport.
    """
    encoded = urllib.parse.quote(path, safe="")
    url = f"{http_source.rstrip('/')}/board?path={encoded}"
    headers = {"Accept": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            # Relay returns the raw Flask response inside a wrapper envelope; if
            # the envelope has a "body" key, prefer that, otherwise pass through.
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
        description="Content Scout board-read (allowlisted Flask GET; relay proxy when --http-source set).",
    )
    parser.add_argument(
        "path",
        help="Flask path to GET (must start with /). Validated against the content-scout allowlist.",
    )
    parser.add_argument(
        "--http-source", default=None,
        help=(
            "Optional. Base URL of PC's agent relay content-scout namespace "
            "(e.g. http://<relay-host>:5002/api/agents/content-scout). "
            "When set, proxy the read through the relay instead of hitting "
            "127.0.0.1:5001 directly. Mac path."
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
