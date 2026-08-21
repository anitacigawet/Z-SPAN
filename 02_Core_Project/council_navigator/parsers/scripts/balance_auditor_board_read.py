#!/usr/bin/env python3.11
"""balance_auditor_board_read -- read-only board query for the Balance Auditor.

D-066 wrapper pattern. URL-allowlist + GET-only + role-hardcoded header.

The Balance Auditor's V1 heartbeat is plain Python and doesn't actually
call this wrapper -- it reads the ledger directly via database.py helpers.
This wrapper exists as scaffolding for V2 (LLM-driven heartbeat) and for
on-demand checks via the orchestrator's Mode B instruction lane.

Allowed paths (V1 narrow):
  /api/operator/badges        general state awareness
  /api/balance/summary        (V2: not yet exposed; will summarize ledger)

Usage:
    python3.11 scripts/balance_auditor_board_read.py /api/operator/badges
"""
from __future__ import annotations

import sys

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

FLASK_BASE = "http://127.0.0.1:5001"
ROLE = "balance-auditor"
TIMEOUT_SECONDS = 10

ALLOWED_PATH_PREFIXES = (
    "/api/operator/badges",
    "/api/balance/",
)


def _is_allowed(path: str) -> tuple[bool, str]:
    if not path.startswith("/"):
        return False, f"path must start with /; got {path!r}"
    if not any(path.startswith(p) for p in ALLOWED_PATH_PREFIXES):
        return False, f"path not in the balance-auditor read allowlist: {path}"
    return True, ""


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: balance_auditor_board_read.py <path>", file=sys.stderr)
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
