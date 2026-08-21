#!/usr/bin/env python3.11
"""content_scout_state_write — write the Content Scout's per-city snapshot file.

The scout's only mutation: writing `agents/_scout_state/<slug>.json` with the
current known-meeting-id list after diffing each session. The target directory
is hardcoded in this wrapper — there's no way to redirect the write elsewhere
even if a prompt-injection talks the agent into passing a tricky --city value
(slug is strict-validated to `[a-z0-9-]+`, which blocks path traversal).

The scout's settings.json denies the Write tool flatly; this wrapper is the
only path to disk for state. The choke point is the file location, the JSON
shape, and the freshly-stamped `last_scan_at` (the wrapper computes it server-
side at write time so the agent can't forge a stale timestamp).

Usage:
    python3.11 scripts/content_scout_state_write.py \\
        --city-slug kingman \\
        --city-name "Kingman" \\
        --meeting-ids 101087,101089,101091

Empty --meeting-ids is allowed (a city with a freshly-cleared cache, or a
truly-empty calendar; either way it's the diff source for the next session).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Hardcoded — never derived from an arg. The wrapper is the only thing that
# knows where state goes.
_REPO_ROOT = Path(__file__).resolve().parents[4]
STATE_DIR = _REPO_ROOT / "agents" / "_scout_state"

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _validate_slug(slug: str) -> tuple[bool, str]:
    if not slug:
        return False, "slug is empty"
    if not SLUG_PATTERN.fullmatch(slug):
        return False, (
            f"slug {slug!r} contains characters outside [a-z0-9-] or starts with '-'; "
            f"refusing to construct a state-file path that could traverse"
        )
    return True, ""


def _parse_ids(raw: str) -> tuple[list[int], str]:
    if not raw:
        return [], ""
    parts = [s.strip() for s in raw.split(",") if s.strip()]
    out: list[int] = []
    for p in parts:
        try:
            out.append(int(p))
        except ValueError:
            return [], f"meeting id {p!r} is not an integer"
    return out, ""


def _post_to_callback(args: argparse.Namespace) -> int:
    """POST validated args to PC's agent relay (D-099 Phase 2.1b)."""
    payload = {
        "city_slug": args.city_slug,
        "city_name": args.city_name,
        "meeting_ids": args.meeting_ids,
    }
    headers = {"Content-Type": "application/json"}
    if args.http_bearer:
        headers["Authorization"] = f"Bearer {args.http_bearer}"
    req = urllib.request.Request(
        args.http_callback,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(body)
                stdout = parsed.get("stdout") or body
            except json.JSONDecodeError:
                stdout = body
            print(stdout.rstrip())
            return 0 if resp.status == 200 else 5
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        print(f"ERROR: HTTP {e.code} from {args.http_callback}: {err_body[:300]}", file=sys.stderr)
        return 5
    except urllib.error.URLError as e:
        print(f"ERROR: could not reach {args.http_callback}: {e}", file=sys.stderr)
        return 5


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Write a Content Scout per-city snapshot file (S-004 watcher).",
    )
    parser.add_argument(
        "--city-slug", required=True,
        help="Lowercase-kebab-case slug (e.g., 'kingman', 'bullhead-city'). "
             "Validated [a-z0-9-]+ — blocks path traversal.",
    )
    parser.add_argument(
        "--city-name", required=True,
        help="Display name of the city (e.g., 'Kingman'). Written into the JSON.",
    )
    parser.add_argument(
        "--meeting-ids", default="",
        help="Comma-separated integer meeting IDs (the diff source for next session). "
             "May be empty.",
    )
    parser.add_argument(
        "--http-callback", default=None,
        help=(
            "Optional. POST args to this URL instead of writing locally. "
            "Used by Mac claude to route state writes to PC's canonical store "
            "via the PC Agent Relay (D-099 Phase 2.1b)."
        ),
    )
    parser.add_argument(
        "--http-bearer", default=None,
        help="Bearer token paired with --http-callback.",
    )
    args = parser.parse_args()

    ok, reason = _validate_slug(args.city_slug)
    if not ok:
        print(f"DENIED: {reason}", file=sys.stderr)
        return 3

    if args.http_callback:
        return _post_to_callback(args)

    meeting_ids, err = _parse_ids(args.meeting_ids)
    if err:
        print(f"DENIED: {err}", file=sys.stderr)
        return 3

    if not STATE_DIR.exists():
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            print(f"ERROR: could not create {STATE_DIR}: {exc}", file=sys.stderr)
            return 4

    target = STATE_DIR / f"{args.city_slug}.json"
    payload = {
        "city": args.city_name,
        "last_scan_at": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "known_meeting_ids": sorted(set(meeting_ids)),
    }
    try:
        with open(target, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
    except Exception as exc:
        print(f"ERROR: could not write {target}: {exc}", file=sys.stderr)
        return 4

    print(
        f"wrote {target.name} city={args.city_name!r} "
        f"known_count={len(payload['known_meeting_ids'])}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
