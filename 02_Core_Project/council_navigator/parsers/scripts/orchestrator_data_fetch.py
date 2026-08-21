#!/usr/bin/env python3.11
"""orchestrator_data_fetch — return cross-agent state files to the orchestrator.

The orchestrator's Tier 0 triage reads state files canonically owned by
other agents:

- parser-health  → agents/_custodian_state/parser-health.json
                   (Parser Custodian's snapshot of focus-list parser health)
- scout-snapshot → agents/_scout_state/<slug>.json
                   (Content Scout's per-city known-meeting-ids snapshot)

On PC, this wrapper reads the file directly. On Mac, it GETs the
appropriate cross-agent endpoint on PC's relay (parser-custodian's or
content-scout's namespace). The orchestrator never needs to know about
the agent-namespace boundary — it just asks for the resource by kind.

Why this wrapper instead of letting the orchestrator call other agents'
data_fetch wrappers directly: each agent's settings.json scopes Bash to
ITS OWN script names. The orchestrator's allowlist would have to include
parser_custodian_data_fetch.py + content_scout_data_fetch.py, which
violates D-066 per-agent walls. Instead, this orchestrator-namespaced
wrapper aggregates the cross-agent reads behind a single scoped entry
point.

D-099 Phase 2.1d. The relay's existing cross-agent endpoints are reused
verbatim — no new relay endpoints needed for the data path.

Usage:
    python3.11 scripts/orchestrator_data_fetch.py --resource parser-health
    python3.11 scripts/orchestrator_data_fetch.py --resource scout-snapshot --slug kingman

    python3.11 scripts/orchestrator_data_fetch.py \\
        --resource parser-health \\
        --http-base http://<relay-host>:5002 \\
        --http-bearer <TOKEN>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_REPO_ROOT = Path(__file__).resolve().parents[4]

PARSER_HEALTH_PATH = _REPO_ROOT / "agents" / "_custodian_state" / "parser-health.json"
SCOUT_STATE_DIR = _REPO_ROOT / "agents" / "_scout_state"

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
VALID_RESOURCES = ("parser-health", "scout-snapshot")


def _validate_slug(slug: str) -> tuple[bool, str]:
    if not slug:
        return False, "slug is empty"
    if not SLUG_PATTERN.fullmatch(slug):
        return False, f"slug {slug!r} contains characters outside [a-z0-9-]"
    return True, ""


def _read_local(resource: str, slug: str | None) -> dict:
    if resource == "parser-health":
        path = PARSER_HEALTH_PATH
    elif resource == "scout-snapshot":
        path = SCOUT_STATE_DIR / f"{slug}.json"
    else:
        return {}
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        print(f"ERROR: {path.name} is unreadable JSON: {exc}", file=sys.stderr)
        return {}
    except OSError as exc:
        print(f"ERROR: could not read {path}: {exc}", file=sys.stderr)
        return {}


def _fetch_remote(resource: str, slug: str | None, http_base: str, bearer: str | None) -> dict:
    base = http_base.rstrip("/")
    if resource == "parser-health":
        url = f"{base}/api/agents/parser-custodian/data/prior-health"
    elif resource == "scout-snapshot":
        encoded = urllib.parse.quote(slug or "", safe="")
        url = f"{base}/api/agents/content-scout/data/snapshot?slug={encoded}"
    else:
        return {}

    headers = {}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(body)
            # Both cross-agent endpoints return {"resource"|"slug": ..., "data": {...}, "found": bool}
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
        description="Orchestrator cross-agent state fetcher (local FS on PC, HTTP relay on Mac).",
    )
    parser.add_argument(
        "--resource", required=True, choices=list(VALID_RESOURCES),
        help="parser-health (Custodian's snapshot) or scout-snapshot (per-city Scout state).",
    )
    parser.add_argument(
        "--slug", default=None,
        help="Required when --resource=scout-snapshot. City slug, [a-z0-9-]+.",
    )
    parser.add_argument(
        "--http-base", default=None,
        help=(
            "Optional. Base URL of PC's agent relay (e.g. http://<relay-host>:5002). "
            "When set, GET the appropriate cross-agent endpoint instead of reading "
            "local FS. Mac path."
        ),
    )
    parser.add_argument(
        "--http-bearer", default=None,
        help="Bearer token paired with --http-base.",
    )
    args = parser.parse_args()

    if args.resource == "scout-snapshot":
        if not args.slug:
            print("ERROR: --slug required when --resource=scout-snapshot", file=sys.stderr)
            return 3
        ok, reason = _validate_slug(args.slug)
        if not ok:
            print(f"DENIED: {reason}", file=sys.stderr)
            return 3

    if args.http_base:
        data = _fetch_remote(args.resource, args.slug, args.http_base, args.http_bearer)
    else:
        data = _read_local(args.resource, args.slug)

    json.dump(data, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
