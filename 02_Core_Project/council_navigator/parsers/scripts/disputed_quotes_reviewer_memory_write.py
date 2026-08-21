#!/usr/bin/env python3.11
"""disputed_quotes_reviewer_memory_write — write/replace/delete entries in the Reviewer's memory dir.

D-067 wrapper pattern. Hardcoded target dir + role attribution; slug-validated; type-enumed.
See orchestrator_memory_write.py for the full pattern documentation.

Usage:
    set:    python3.11 scripts/disputed_quotes_reviewer_memory_write.py set --slug foo --type observation --description "..."
    delete: python3.11 scripts/disputed_quotes_reviewer_memory_write.py delete --slug foo
    index:  python3.11 scripts/disputed_quotes_reviewer_memory_write.py index  (MEMORY.md content via stdin)
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
    sys.stdin.reconfigure(encoding="utf-8")
except Exception:
    pass

_REPO_ROOT = Path(__file__).resolve().parents[4]
MEMORY_DIR = _REPO_ROOT / "agents" / "_reviewer_memory"
ROLE = "disputed-quotes-reviewer"

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
VALID_TYPES = ("observation", "suggestion", "insight")
INDEX_FILENAME = "MEMORY.md"
RESERVED_SLUGS = ("MEMORY", "memory", "index")


def _validate_slug(slug: str) -> tuple[bool, str]:
    if not slug:
        return False, "slug is empty"
    if slug in RESERVED_SLUGS:
        return False, f"slug {slug!r} is reserved; use the 'index' sub-command for MEMORY.md"
    if not SLUG_PATTERN.fullmatch(slug):
        return False, f"slug {slug!r} contains characters outside [a-z0-9-] or starts with '-'"
    return True, ""


def _ensure_dir() -> int:
    if not MEMORY_DIR.exists():
        try:
            MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            print(f"ERROR: could not create {MEMORY_DIR}: {exc}", file=sys.stderr)
            return 4
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    ok, reason = _validate_slug(args.slug)
    if not ok:
        print(f"DENIED: {reason}", file=sys.stderr)
        return 3
    if args.type not in VALID_TYPES:
        print(f"DENIED: type {args.type!r} not in {VALID_TYPES}", file=sys.stderr)
        return 3
    if not args.description or not args.description.strip():
        print("DENIED: --description is required and must be non-empty", file=sys.stderr)
        return 3

    rc = _ensure_dir()
    if rc:
        return rc

    body = sys.stdin.read()
    if body.startswith("﻿"):
        body = body[1:]  # strip UTF-8 BOM (PowerShell stdin pipes occasionally prepend one)
    target = MEMORY_DIR / f"{args.slug}.md"
    created_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")

    frontmatter = (
        f"---\n"
        f"name: {args.slug}\n"
        f"description: {args.description.strip()}\n"
        f"metadata:\n"
        f"  type: {args.type}\n"
        f"  created_at: {created_at}\n"
        f"  role: {ROLE}\n"
        f"---\n\n"
    )

    try:
        with open(target, "w", encoding="utf-8") as f:
            f.write(frontmatter)
            f.write(body)
            if not body.endswith("\n"):
                f.write("\n")
    except Exception as exc:
        print(f"ERROR: could not write {target}: {exc}", file=sys.stderr)
        return 4

    print(f"set {target.name} type={args.type} desc={args.description.strip()!r}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    ok, reason = _validate_slug(args.slug)
    if not ok:
        print(f"DENIED: {reason}", file=sys.stderr)
        return 3

    target = MEMORY_DIR / f"{args.slug}.md"
    if not target.exists():
        print(f"ERROR: {target.name} does not exist; nothing to delete", file=sys.stderr)
        return 5

    try:
        target.unlink()
    except Exception as exc:
        print(f"ERROR: could not delete {target}: {exc}", file=sys.stderr)
        return 4

    print(f"deleted {target.name}")
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    rc = _ensure_dir()
    if rc:
        return rc

    body = sys.stdin.read()
    if body.startswith("﻿"):
        body = body[1:]  # strip UTF-8 BOM (PowerShell stdin pipes occasionally prepend one)
    target = MEMORY_DIR / INDEX_FILENAME
    try:
        with open(target, "w", encoding="utf-8") as f:
            f.write(body)
            if not body.endswith("\n"):
                f.write("\n")
    except Exception as exc:
        print(f"ERROR: could not write {target}: {exc}", file=sys.stderr)
        return 4

    print(f"updated {target.name} ({len(body)} chars)")
    return 0


def _post_to_callback(args: argparse.Namespace) -> int:
    payload: dict = {"cmd": args.cmd}
    if args.cmd == "set":
        payload.update({"slug": args.slug, "type": args.type, "description": args.description})
        payload["body"] = sys.stdin.read()
    elif args.cmd == "delete":
        payload["slug"] = args.slug
    elif args.cmd == "index":
        payload["body"] = sys.stdin.read()

    headers = {"Content-Type": "application/json"}
    if args.http_bearer:
        headers["Authorization"] = f"Bearer {args.http_bearer}"
    req = urllib.request.Request(
        args.http_callback, data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST",
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
        description=f"{ROLE} memory-write wrapper (target: agents/{MEMORY_DIR.name}/)",
    )
    parser.add_argument(
        "--http-callback", default=None,
        help="Optional. POST args to PC's agent relay instead of writing locally.",
    )
    parser.add_argument(
        "--http-bearer", default=None,
        help="Bearer token paired with --http-callback.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_set = sub.add_parser("set", help="Create or overwrite a memory entry (idempotent).")
    p_set.add_argument("--slug", required=True)
    p_set.add_argument("--type", required=True, choices=list(VALID_TYPES))
    p_set.add_argument("--description", required=True)
    p_set.set_defaults(func=cmd_set)

    p_delete = sub.add_parser("delete", help="Remove a retired memory entry.")
    p_delete.add_argument("--slug", required=True)
    p_delete.set_defaults(func=cmd_delete)

    p_index = sub.add_parser("index", help="Write the MEMORY.md index from stdin (no frontmatter).")
    p_index.set_defaults(func=cmd_index)

    args = parser.parse_args()
    if args.http_callback:
        return _post_to_callback(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
