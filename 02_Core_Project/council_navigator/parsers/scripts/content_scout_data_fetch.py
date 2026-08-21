#!/usr/bin/env python3.11
"""content_scout_data_fetch — return the canonical per-city snapshot to the agent.

The Content Scout reads per-city snapshots at session start:
    agents/_scout_state/<slug>.json

On PC, this wrapper reads the file directly and emits its contents.
On Mac (where the file does not exist), the wrapper GETs from PC's agent
relay via --http-source + --http-bearer. The agent's flow is identical
on either machine — call this wrapper via Bash, parse stdout JSON.

Missing snapshot is the first-run case; the wrapper returns an empty
object `{}` rather than failing, so the agent can treat first-run + present
state uniformly.

D-099 Phase 2.1b. Mirrors parser_custodian_data_fetch.py but adapted for
the per-city slug rather than parser-custodian's two singleton resources.

Usage:
    python3.11 scripts/content_scout_data_fetch.py --slug kingman
    python3.11 scripts/content_scout_data_fetch.py --slug bullhead-city

    python3.11 scripts/content_scout_data_fetch.py \\
        --slug kingman \\
        --http-source http://<relay-host>:5002/api/agents/content-scout \\
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
STATE_DIR = _REPO_ROOT / "agents" / "_scout_state"

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _validate_slug(slug: str) -> tuple[bool, str]:
    if not slug:
        return False, "slug is empty"
    if not SLUG_PATTERN.fullmatch(slug):
        return False, f"slug {slug!r} contains characters outside [a-z0-9-]"
    return True, ""


def _read_local(slug: str) -> dict:
    path = STATE_DIR / f"{slug}.json"
    if not path.exists():
        return {}  # first-run case; not an error.
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        print(f"ERROR: {path.name} is unreadable JSON: {exc}", file=sys.stderr)
        return {}
    except OSError as exc:
        print(f"ERROR: could not read {path}: {exc}", file=sys.stderr)
        return {}


def _fetch_remote(slug: str, http_source: str, bearer: str | None) -> dict:
    encoded = urllib.parse.quote(slug, safe="")
    url = f"{http_source.rstrip('/')}/data/snapshot?slug={encoded}"
    headers = {}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(body)
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
        description="Fetch Content Scout per-city snapshot (local FS on PC, HTTP relay on Mac).",
    )
    parser.add_argument(
        "--slug", required=True,
        help="City slug (e.g. 'kingman', 'bullhead-city'). Validated [a-z0-9-]+.",
    )
    parser.add_argument(
        "--http-source", default=None,
        help=(
            "Optional. Base URL of PC's agent relay content-scout namespace. "
            "When set, GET /data/snapshot?slug=<slug> instead of reading local FS."
        ),
    )
    parser.add_argument(
        "--http-bearer", default=None,
        help="Bearer token paired with --http-source.",
    )
    args = parser.parse_args()

    ok, reason = _validate_slug(args.slug)
    if not ok:
        print(f"DENIED: {reason}", file=sys.stderr)
        return 3

    if args.http_source:
        data = _fetch_remote(args.slug, args.http_source, args.http_bearer)
    else:
        data = _read_local(args.slug)

    json.dump(data, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
