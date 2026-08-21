#!/usr/bin/env python3.11
"""balance_auditor_memory_write -- write/replace/delete entries in the Auditor's memory dir.

D-067 wrapper pattern. Hardcoded target dir (agents/_auditor_memory/) + role attribution;
slug-validated; type-enumed. See content_scout_memory_write.py for the full pattern docs.

Usage:
    set:    python3.11 scripts/balance_auditor_memory_write.py set --slug foo --type observation --description "..."
    delete: python3.11 scripts/balance_auditor_memory_write.py delete --slug foo
    index:  python3.11 scripts/balance_auditor_memory_write.py index  (MEMORY.md content via stdin)
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
except Exception:
    pass

_REPO_ROOT = Path(__file__).resolve().parents[4]
MEMORY_DIR = _REPO_ROOT / "agents" / "_auditor_memory"
ROLE = "balance-auditor"

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
        body = body[1:]  # strip UTF-8 BOM
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description=f"{ROLE} memory-write wrapper (target: agents/{MEMORY_DIR.name}/)",
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
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
