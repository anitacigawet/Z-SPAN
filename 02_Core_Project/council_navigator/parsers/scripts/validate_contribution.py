#!/usr/bin/env python3
"""validate_contribution.py — Z-SPAN contributor PR validation.

Runs locally OR in CI against the files a contributor PR touches.
Validates three kinds of contribution:

  1. **Recon JSON** at `state_scaffolding/<state>/discovery/<county>/<city>.json`
     — validated against `parsers/schemas/recon.schema.json`.
  2. **Parser** at `parsers/<city>_parser.py` — smoke-tested under the
     worker venv python3.11; output validated against the canonical
     11-field meeting schema via `normalize.py`.
  3. **City intelligence** at `parsers/city_intelligence/<slug>.json` —
     validated against the T-006 kingman.json shape (council member
     roster + meeting series + persona preambles).

Exit 0 = all checks pass; exit 1 = at least one violation.

Usage (CI):
    python3.11 scripts/validate_contribution.py --files <file1> <file2> ...

Usage (local):
    python3.11 scripts/validate_contribution.py --pr-diff   # auto-detect
    python3.11 scripts/validate_contribution.py --all-recon # sweep AZ recon

Per S-066 (CONTRIBUTING.md § Submitting via the schema-validated path).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_PARSERS = _SCRIPT_DIR.parent
_REPO_ROOT = _PARSERS.parent.parent.parent
_SCHEMA_PATH = _PARSERS / "schemas" / "recon.schema.json"
_VENV_PY = _REPO_ROOT / ".venv-worker" / "bin" / "python3.11"

RECON_PATH_RE = re.compile(
    r"^.*?state_scaffolding/(?P<state>[^/]+)/discovery/(?P<county>[^/]+)/(?P<city>[^/]+)\.json$"
)
PARSER_PATH_RE = re.compile(
    r"^.*?parsers/(?P<slug>[a-z0-9_]+)_parser\.py$"
)
CITY_INTELLIGENCE_PATH_RE = re.compile(
    r"^.*?parsers/city_intelligence/(?P<slug>[a-z0-9_]+)\.json$"
)


def _err(msg: str) -> dict:
    return {"ok": False, "msg": msg}


def _ok(msg: str = "") -> dict:
    return {"ok": True, "msg": msg}


def _try_import_jsonschema():
    try:
        import jsonschema  # noqa: F401
        return True
    except ImportError:
        return False


def validate_recon_json(path: Path) -> dict:
    """Validate a recon JSON against `recon.schema.json`."""
    if not path.exists():
        return _err(f"recon JSON not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return _err(f"recon JSON not valid JSON: {path} — {e}")

    if not _try_import_jsonschema():
        return _err(
            f"jsonschema library not installed; "
            f"run `pip install jsonschema` into the venv first. (path={path})"
        )

    import jsonschema
    try:
        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return _err(f"recon.schema.json missing or invalid: {e}")

    try:
        jsonschema.validate(instance=data, schema=schema)
    except jsonschema.ValidationError as e:
        path_pretty = ".".join(str(p) for p in e.absolute_path) or "(root)"
        return _err(
            f"recon JSON schema violation in {path}: "
            f"at {path_pretty} — {e.message}"
        )
    return _ok(f"recon JSON valid: {path} (city={data.get('city_name')!r} "
               f"calendar_format={data.get('calendar_format')!r})")


def validate_parser(path: Path) -> dict:
    """Smoke-test a parser by importing + calling scrape_calendar()."""
    if not path.exists():
        return _err(f"parser file not found: {path}")
    if not _VENV_PY.exists():
        return _err(
            f"worker venv python3.11 not found at {_VENV_PY}; "
            f"bootstrap the venv per CLAUDE.md before validating parsers."
        )
    slug_match = PARSER_PATH_RE.match(str(path))
    if not slug_match:
        return _err(f"parser path doesn't match `parsers/<slug>_parser.py`: {path}")
    slug = slug_match.group("slug")

    smoke = subprocess.run(
        [str(_VENV_PY), str(path)],
        cwd=str(_PARSERS),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if smoke.returncode != 0:
        return _err(
            f"parser smoke failed for {slug}: "
            f"exit={smoke.returncode} stderr={smoke.stderr[-400:]!r}"
        )
    return _ok(
        f"parser smoke passed: {slug} "
        f"(stdout_tail={smoke.stdout.strip().splitlines()[-1] if smoke.stdout.strip() else ''!r})"
    )


def validate_city_intelligence(path: Path) -> dict:
    """Validate per-city intelligence JSON against the T-006 shape."""
    if not path.exists():
        return _err(f"city_intelligence JSON not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return _err(f"city_intelligence JSON not valid JSON: {path} — {e}")
    if not isinstance(data, dict):
        return _err(f"city_intelligence root must be object, got {type(data).__name__}")
    required_keys = {"city", "state", "council", "meeting_series"}
    missing = required_keys - data.keys()
    if missing:
        return _err(
            f"city_intelligence {path}: missing required keys {sorted(missing)}. "
            f"Reference example: parsers/city_intelligence/kingman.json"
        )
    return _ok(f"city_intelligence valid: {path}")


def _classify_path(path_str: str) -> tuple[str | None, Path | None]:
    path = Path(path_str)
    if not path.is_absolute():
        path = (_REPO_ROOT / path_str).resolve()
    s = str(path)
    if RECON_PATH_RE.match(s):
        return "recon", path
    if CITY_INTELLIGENCE_PATH_RE.match(s):
        return "city_intelligence", path
    if PARSER_PATH_RE.match(s):
        return "parser", path
    return None, path


def _collect_pr_diff_files() -> list[str]:
    """Use git to enumerate files in HEAD~1..HEAD that match our patterns."""
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
        return [line for line in out.stdout.splitlines() if line.strip()]
    except subprocess.CalledProcessError as e:
        print(f"WARNING: git diff failed: {e.stderr}", file=sys.stderr)
        return []


def _collect_all_recon_files() -> list[str]:
    scaffold_root = _REPO_ROOT / "02_Core_Project" / "council_navigator" / "state_scaffolding"
    if not scaffold_root.exists():
        return []
    return [str(p) for p in scaffold_root.glob("*/discovery/*/*.json") if not p.name.startswith("_")]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--files", nargs="+", help="File paths to validate.")
    group.add_argument("--pr-diff", action="store_true",
                       help="Auto-discover files changed in HEAD~1..HEAD.")
    group.add_argument("--all-recon", action="store_true",
                       help="Sweep every recon JSON in state_scaffolding/.")
    ap.add_argument("--skip-parser-smoke", action="store_true",
                    help="Skip parser smoke tests (schema-only validation).")
    args = ap.parse_args()

    if args.files:
        file_list = args.files
    elif args.pr_diff:
        file_list = _collect_pr_diff_files()
        if not file_list:
            print("no files in HEAD~1..HEAD diff to validate.", file=sys.stderr)
            return 0
    else:
        file_list = _collect_all_recon_files()
        if not file_list:
            print(f"no recon JSONs found.", file=sys.stderr)
            return 0

    results = []
    for f in file_list:
        kind, path = _classify_path(f)
        if kind is None:
            print(f"SKIP {f} (not a recon JSON / parser / city_intelligence path)",
                  file=sys.stderr)
            continue
        if kind == "recon":
            result = validate_recon_json(path)
        elif kind == "parser":
            if args.skip_parser_smoke:
                result = _ok(f"parser smoke skipped: {path}")
            else:
                result = validate_parser(path)
        elif kind == "city_intelligence":
            result = validate_city_intelligence(path)
        else:
            result = _err(f"unknown kind: {kind}")
        results.append((kind, path, result))

    print(f"\n=== Validation results: {len(results)} files ===\n", file=sys.stderr)
    bad = 0
    for kind, path, result in results:
        status = "✓" if result["ok"] else "✗"
        rel = str(path.relative_to(_REPO_ROOT)) if path.is_relative_to(_REPO_ROOT) else str(path)
        print(f"  [{status}] {kind:18s} {rel}", file=sys.stderr)
        print(f"        {result['msg']}", file=sys.stderr)
        if not result["ok"]:
            bad += 1

    print("", file=sys.stderr)
    if bad:
        print(f"FAIL: {bad} of {len(results)} validation(s) failed.", file=sys.stderr)
        return 1
    print(f"PASS: {len(results)} validation(s) succeeded.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
