#!/usr/bin/env python3
"""Scan client/public/ for dev/operator artifacts (sol Finding #9).

The Z-SPAN publication rule (sol pen-test 2026-07-24, Finding #9):
`02_Core_Project/council_navigator/client/public/` is a strict
publication surface — every file in it ships to every visitor as a
directly-addressable static asset. It must contain ONLY:

  - Vite/CF Pages config files at the root (_headers, _redirects,
    robots.txt, .gitkeep)
  - Referenced brand + channel + episode + state image/video assets
  - Referenced hq/ scene renders (ganymede.png, zspan-hq*.{png,webp},
    zspan-dashboard-scene.webp, zspan-hq-transparent.png)

It must NOT contain:

  - Python / shell / ANY code files (dev scripts)
  - .meta.json (leaks NotebookLM or successor identifiers)
  - .smoke.prompt.txt (prompt drafts)
  - .bak / .DS_Store (accidental developer state)
  - hq/crops/ (comparison and audit crops)
  - hq/fog/ (experimental fog layers)
  - Any operator screenshot showing internal dashboards

This scanner walks the tree and fails on any offender. Companion to
ops/scan_built_artifacts_for_vendor_hosts.py which watches for D-153
sealed-registry leaks in the same publication surface (different
concern, same class of defense).

Exit codes:
  0 — no dev artifacts found
  1 — one or more offending files present
  2 — usage / configuration error
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Extension-based bans. If a file with any of these extensions appears
# anywhere under client/public/, fail. Extensions are the class-based
# defense; the specific-path bans below catch known offenders that
# happen to have allowed extensions (.png in a dev-crop directory, etc.).
BANNED_EXTENSIONS = frozenset({
    ".py",       # dev scripts
    ".sh",       # dev scripts
    ".bak",      # accidental backups
    ".meta.json",  # NotebookLM / successor metadata leaks
})

# Files ending in any of these tails also fail. `.meta.json` needs
# explicit tail-match because pathlib's `.suffix` returns just `.json`.
BANNED_TAILS = frozenset({
    ".meta.json",
    ".smoke.prompt.txt",
    ".DS_Store",
})

# Whole subtrees that must not exist under client/public/. Match by
# path suffix rather than absolute path so the scanner works regardless
# of the repo layout above client/public/.
BANNED_SUBTREES = (
    "client/public/hq/crops",
)

# Specific-file bans — known dev/operator artifacts that could plausibly
# reappear via a well-meaning commit.
BANNED_FILENAMES = frozenset({
    "artifact_fixup.py",
    "rembg_postprocess.py",
    "composite_for_critique.png",
    "press_infographic.png",
    "press_infographic.meta.json",
})


def is_offender(path: Path) -> str | None:
    """Return a human-readable violation reason, or None if clean."""
    path_str = str(path).replace("\\", "/")
    name = path.name

    # Whole-subtree bans.
    for subtree in BANNED_SUBTREES:
        if subtree in path_str:
            return f"file lives under banned subtree '{subtree}/'"

    # Specific-filename bans.
    if name in BANNED_FILENAMES:
        return f"specific filename '{name}' is banned (dev/operator artifact)"

    # Extension bans.
    ext = path.suffix.lower()
    if ext in BANNED_EXTENSIONS:
        return f"banned extension '{ext}'"

    # Tail bans (multi-dot suffixes pathlib doesn't handle).
    lower = name.lower()
    for tail in BANNED_TAILS:
        if lower.endswith(tail.lower()):
            return f"banned tail '{tail}'"

    return None


def scan(public_dir: Path) -> list[tuple[Path, str]]:
    if not public_dir.is_dir():
        print(
            f"error: public_dir does not exist or is not a directory: {public_dir}",
            file=sys.stderr,
        )
        sys.exit(2)

    offenders: list[tuple[Path, str]] = []
    for path in sorted(public_dir.rglob("*")):
        if not path.is_file():
            continue
        reason = is_offender(path)
        if reason:
            offenders.append((path, reason))
    return offenders


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan client/public/ for dev/operator artifacts (sol Finding #9).",
    )
    parser.add_argument(
        "--public-dir",
        default="02_Core_Project/council_navigator/client/public",
        help="path to client/public/ (default: repo-relative)",
    )
    args = parser.parse_args()

    public_dir = Path(args.public_dir).resolve()
    offenders = scan(public_dir)

    if not offenders:
        print(f"✅ client/public/ clean ({public_dir})")
        return 0

    print(f"❌ client/public/ has {len(offenders)} dev/operator artifacts:")
    for path, reason in offenders:
        rel = path.relative_to(public_dir)
        print(f"  {rel}  ← {reason}")
    print()
    print(
        "Remove these files. See sol Finding #9 in the pen-test report + the "
        ".gitignore rules under 'Sol Finding #9' for the publication contract.",
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
