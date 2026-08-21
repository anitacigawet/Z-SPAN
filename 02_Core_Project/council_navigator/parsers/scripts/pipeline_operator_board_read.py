#!/usr/bin/env python3.11
"""pipeline_operator_board_read — read-only board query for the headless Pipeline Operator (S-004).

Mirrors the D-065/D-066 wrapper pattern: URL-allowlisted GET-only Flask reads
with `X-Zspan-Agent-Role: pipeline-operator`. The agent's settings.json denies
direct curl / WebFetch / HTTP — this wrapper is the only path to read state.

Allowed paths are the read surface the Pipeline Operator needs to sequence
work orders + check downstream signals (per agents/pipeline-operator.md §
Allowed Flask endpoints):
  - /api/work-orders               LIST / DETAIL (never action subroutes)
  - /api/operator/badges           disputed + vocab counts (downstream load)
  - /api/calendar/events           cached read for parser-side state checks
  - /api/quotes/meeting/<id>       hero-quote count before firing [BUILD]
  - /api/ingestion/governor        pacing visibility (rate, room under ceiling)

Action subroutes within /api/work-orders/<id>/... are NEVER reachable through
this wrapper (belt-and-suspenders even though the wrapper is GET-only):
mutations go through `pipeline_operator_action.py`.

Usage:
    python3.11 scripts/pipeline_operator_board_read.py /api/operator/badges
    python3.11 scripts/pipeline_operator_board_read.py "/api/work-orders?city=Kingman"
    python3.11 scripts/pipeline_operator_board_read.py /api/quotes/meeting/101091
    python3.11 scripts/pipeline_operator_board_read.py "/api/ingestion/governor?city=Kingman"
"""
from __future__ import annotations

import sys

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

FLASK_BASE = "http://127.0.0.1:5001"
ROLE = "pipeline-operator"
TIMEOUT_SECONDS = 10

ALLOWED_PATH_PREFIXES = (
    "/api/operator/badges",
    "/api/work-orders",
    "/api/calendar/events",
    "/api/quotes/meeting/",
    "/api/ingestion/governor",
)

# Action subroutes within /api/work-orders/<id>/... that must NEVER be reachable
# from this wrapper, even though the /api/work-orders prefix matches. The agent
# uses pipeline_operator_action.py for mutating POSTs. Belt-and-suspenders given
# this wrapper is already GET-only.
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
    "/burn",
)


def _is_allowed(path: str) -> tuple[bool, str]:
    if not path.startswith("/"):
        return False, f"path must start with /; got {path!r}"
    for denied in DENIED_PATH_SUBSTRINGS:
        if denied in path:
            return False, f"path contains denied action substring {denied!r}: {path}"
    if not any(path.startswith(p) for p in ALLOWED_PATH_PREFIXES):
        return False, f"path not in the pipeline-operator read allowlist: {path}"
    return True, ""


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: pipeline_operator_board_read.py <path>", file=sys.stderr)
        return 2

    path = sys.argv[1]
    ok, reason = _is_allowed(path)
    if not ok:
        print(f"DENIED: {reason}", file=sys.stderr)
        return 3

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


if __name__ == "__main__":
    sys.exit(main())
