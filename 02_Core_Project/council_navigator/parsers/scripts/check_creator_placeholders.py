#!/usr/bin/env python3.11
"""check_creator_placeholders — V0 launch-day pre-flight gate.

Per `01_Project_Overview/ACCOUNT_SYSTEM_SPEC.md` chunk 8 redline decision 3:
the Creator Network signup flow ships in dev with literal placeholder
text strings ("placeholder terms of service", "placeholder disclaimer
narration script"). Before flagship-public access narrows, ALL placeholder
strings must be replaced with real operator-authored text.

This script greps the codebase for the placeholder substrings + exits
non-zero if any are still present. Run from the Navigator project root::

    python3.11 parsers/scripts/check_creator_placeholders.py

Exit codes:
    0 — no placeholder strings found; safe to enable flagship-public access
    1 — placeholder strings still present; NOT safe to enable flagship-
        public access. Diff list printed to stdout.

The script is intentionally simple — a literal substring scan over the
repo, excluding `.git/` and a handful of known-noise directories. It is
deterministic, has no LLM dependency, and runs in seconds.

Per [D-100](../../../01_Project_Overview/DECISIONS.md#d-100): defensive
launch-day pre-flight.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Resolve the repo root: scripts/ -> parsers/ -> Navigator/ -> 02_Core_Project/ -> repo
_THIS = Path(__file__).resolve()
_REPO_ROOT = _THIS.parents[4]


# Literal substrings the signup surface ships with in V0. When James swaps
# them for real legal text, this script returns clean.
PLACEHOLDER_STRINGS: tuple[str, ...] = (
    "placeholder terms of service",
    "placeholder disclaimer text",
    "placeholder disclaimer narration",
    "placeholder disclaimer audio",
    "placeholder karaoke",
)


# Directories to exclude from the scan. We intentionally DO include
# 01_Project_Overview/ so the SPEC's quoted placeholder strings (which
# document the placeholder mechanism) are surfaced — but only on the
# launch-day check, the operator can grep -v / silence as needed.
# The .git/, node_modules/, .venv/, __pycache__/, dist/ directories
# never contain user-facing content and are noise.
EXCLUDED_DIR_NAMES: frozenset[str] = frozenset({
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    ".pytest_cache",
    ".mypy_cache",
    ".idea",
    ".vscode",
})


# File suffixes to scan. We include user-facing content carriers (md, html,
# tsx, jsx, json, yaml, ts, js, py) and skip binaries.
INCLUDED_SUFFIXES: frozenset[str] = frozenset({
    ".md", ".mdx", ".txt",
    ".html", ".htm", ".xml",
    ".tsx", ".jsx", ".ts", ".js",
    ".py",
    ".json", ".yaml", ".yml",
})


# Files containing PLACEHOLDER strings that are LEGITIMATELY there as
# documentation/spec references. These get excluded from the failure
# count but ARE reported in --verbose mode so the operator can confirm
# they shouldn't be cleaned up.
DOC_REFERENCE_PATHS: frozenset[str] = frozenset({
    str(Path("01_Project_Overview") / "ACCOUNT_SYSTEM_SPEC.md"),
    str(Path("01_Project_Overview") / "S008_S012_SCOPE_PROPOSAL_2026-06-10.md"),
    str(Path("01_Project_Overview") / "S008_INPUT_SECURITY_SPEC.md"),
    str(Path("01_Project_Overview") / "THREAT_MODEL_INPUT_SECURITY.md"),
    str(Path("02_Core_Project") / "council_navigator" /
        "parsers" / "scripts" / "check_creator_placeholders.py"),
    str(Path("02_Core_Project") / "council_navigator" /
        "parsers" / "input_security" / "test_check_creator_placeholders.py"),
})


def _walk(root: Path):
    """Yield every scan-eligible file under `root`, skipping excluded dirs."""
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in INCLUDED_SUFFIXES:
            continue
        if any(part in EXCLUDED_DIR_NAMES for part in path.parts):
            continue
        yield path


def _scan_file(path: Path) -> list[tuple[int, str, str]]:
    """Return a list of (line_number, placeholder_string, line_text) tuples."""
    findings: list[tuple[int, str, str]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line_no, line in enumerate(f, start=1):
                lowered = line.lower()
                for placeholder in PLACEHOLDER_STRINGS:
                    if placeholder in lowered:
                        findings.append((line_no, placeholder, line.rstrip()))
    except OSError:
        pass
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Launch-day pre-flight scan for Creator Network "
                    "placeholder text.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print doc-reference matches too (normally suppressed)."
    )
    parser.add_argument(
        "--root", default=str(_REPO_ROOT),
        help="Override the repo root (default: auto-detected).",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"ERROR: root {root} is not a directory", file=sys.stderr)
        return 2

    fail_findings: list[tuple[Path, int, str, str]] = []
    doc_findings: list[tuple[Path, int, str, str]] = []

    for path in _walk(root):
        rel = path.relative_to(root)
        rel_str = str(rel).replace("\\", "/")
        is_doc_ref = any(
            rel_str == doc.replace("\\", "/")
            for doc in DOC_REFERENCE_PATHS
        )
        findings = _scan_file(path)
        for line_no, placeholder, line in findings:
            tup = (rel, line_no, placeholder, line)
            (doc_findings if is_doc_ref else fail_findings).append(tup)

    if args.verbose and doc_findings:
        print("DOC-REFERENCE matches (excluded from failure count):")
        for rel, line_no, placeholder, line in doc_findings:
            print(f"  {rel}:{line_no} — {placeholder!r}")
        print()

    if fail_findings:
        print("FAIL — Creator Network placeholder strings still present:")
        for rel, line_no, placeholder, line in fail_findings:
            print(f"  {rel}:{line_no} — {placeholder!r}")
            print(f"    {line[:160]}")
        print()
        print(
            f"\n{len(fail_findings)} placeholder occurrence(s) found. "
            "Replace with operator-authored real text before enabling "
            "flagship-public access."
        )
        return 1

    print(
        "OK — no Creator Network placeholder strings present in "
        "user-facing content."
    )
    print(
        f"(scanned {len(list(_walk(root)))} files; "
        f"{len(doc_findings)} doc-reference matches excluded)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
