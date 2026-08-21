#!/usr/bin/env python3.11
"""parser_custodian_data_fetch — return canonical PC-side JSON to the agent.

The Parser Custodian agent needs read-only access to two files that live
on PC's canonical filesystem:

- parser_test_results.json (the daily-cycle scrape's output)
- agents/_custodian_state/parser-health.json (the agent's own prior-state snapshot)

On PC, this wrapper reads the file directly and emits its contents.
On Mac (where the file does not exist), the wrapper GETs from PC's agent
relay via --http-source + --http-bearer. The agent's flow is identical
on either machine — call this wrapper via Bash, parse the stdout JSON.

D-099 Phase 2.1b. Replaces direct Read-tool calls in routine.md so the
agent's data delivery is honest about scale: no JSON gets embedded in
the prompt regardless of how many parsers / cities the focus list grows
to. The wrapper is the only path; HTTP is just the transport when the
canonical store lives on a different machine.

Usage:
    python3.11 scripts/parser_custodian_data_fetch.py --resource test-results
    python3.11 scripts/parser_custodian_data_fetch.py --resource prior-health

    python3.11 scripts/parser_custodian_data_fetch.py \\
        --resource test-results \\
        --http-source http://<relay-host>:5002/api/agents/parser-custodian \\
        --http-bearer <TOKEN>
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_REPO_ROOT = Path(__file__).resolve().parents[4]

RESOURCES = {
    "test-results": _REPO_ROOT / "02_Core_Project" / "council_navigator" / "parser_test_results.json",
    "prior-health": _REPO_ROOT / "agents" / "_custodian_state" / "parser-health.json",
}


def _read_local(resource: str) -> dict:
    path = RESOURCES[resource]
    if not path.exists():
        # Missing prior-health is the first-run case; emit empty dict, not error.
        return {} if resource == "prior-health" else {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        print(f"ERROR: {path.name} is unreadable JSON: {exc}", file=sys.stderr)
        return {}
    except OSError as exc:
        print(f"ERROR: could not read {path}: {exc}", file=sys.stderr)
        return {}


def _fetch_remote(resource: str, http_source: str, bearer: str | None) -> dict:
    url = f"{http_source.rstrip('/')}/data/{resource}"
    headers = {}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(body)
            # Relay wraps as {"resource": ..., "data": {...}, "found": bool}.
            if isinstance(parsed, dict) and "data" in parsed:
                return parsed.get("data") or {}
            return parsed
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        print(f"ERROR: HTTP {e.code} from {url}: {err_body[:300]}", file=sys.stderr)
        return {}
    except urllib.error.URLError as e:
        print(f"ERROR: could not reach {url}: {e}", file=sys.stderr)
        return {}
    except json.JSONDecodeError as e:
        print(f"ERROR: relay returned non-JSON: {e}", file=sys.stderr)
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch parser-custodian read-only data (local FS on PC, HTTP relay on Mac).",
    )
    parser.add_argument(
        "--resource", required=True, choices=list(RESOURCES),
        help="Which canonical file to return.",
    )
    parser.add_argument(
        "--http-source", default=None,
        help=(
            "Optional. Base URL of PC's agent relay (e.g. http://<IP>:5002/api/agents/parser-custodian). "
            "When set, GET /data/<resource> instead of reading local FS. Mac path."
        ),
    )
    parser.add_argument(
        "--http-bearer", default=None,
        help="Bearer token paired with --http-source.",
    )
    args = parser.parse_args()

    if args.http_source:
        data = _fetch_remote(args.resource, args.http_source, args.http_bearer)
    else:
        data = _read_local(args.resource)

    # Emit the resource content directly. The agent parses stdout as JSON.
    json.dump(data, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
