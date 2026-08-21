"""Capture an Antigravity-Jules-Gemini-Pro hardening findings JSON file
and POST it to Z-SPAN's ingest endpoint.

Part of the D-100 scaffolding documented in
01_Project_Overview/HARDENING_FINDINGS_SCHEMA.md.

Workflow:
  1. Operator runs a defensive-only hardening pass on the Gemini-Pro
     side (see HARDENING_FINDINGS_SCHEMA.md for the audit prompt).
  2. Gemini-Pro returns findings JSON in the v1 schema.
  3. Operator saves it to a file + runs this script.
  4. Script POSTs to /api/operator/hardening-runs/ingest; persisted
     findings surface in the operator review queue.

Usage:
  python3.11 parsers/scripts/hardening_capture.py \\
      --findings <path-to-findings.json> \\
      --cookie zspan_session=<jwt-value> \\
      [--base-url http://localhost:3010]
  python3.11 parsers/scripts/hardening_capture.py \\
      --findings <path> \\
      --cookie-file <path>   # file has the raw "name=value" cookie string

Env-var fallback: ZSPAN_OPERATOR_COOKIE is used if --cookie / --cookie-file
are absent. Useful for shell-scripting the capture step.

This script does NOT call any LLM. It is pure plumbing between the
Gemini-Pro-side JSON deliverable and Z-SPAN's persistence layer.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

DEFAULT_BASE_URL = "http://localhost:3010"


def _resolve_cookie(args: argparse.Namespace) -> Optional[str]:
    if args.cookie:
        return args.cookie.strip()
    if args.cookie_file:
        try:
            return Path(args.cookie_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"ERROR: cookie-file unreadable: {exc}", file=sys.stderr)
            return None
    env_cookie = os.environ.get("ZSPAN_OPERATOR_COOKIE", "").strip()
    if env_cookie:
        return env_cookie
    return None


def _load_findings(path: Path) -> Optional[dict]:
    if not path.exists():
        print(f"ERROR: findings file not found: {path}", file=sys.stderr)
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"ERROR: findings file is not valid JSON: {exc}", file=sys.stderr)
        return None


def _client_side_sanity(payload: dict) -> Optional[str]:
    """Minimal local sanity check before the network round-trip. The
    server validates exhaustively; this just catches obvious shape errors
    + saves the operator a network call for the easy mistakes.
    """
    if not isinstance(payload, dict):
        return "payload is not a JSON object"
    if "schema_version" not in payload:
        return "missing top-level schema_version"
    if "run_metadata" not in payload:
        return "missing top-level run_metadata"
    if "findings" not in payload:
        return "missing top-level findings"
    rm = payload["run_metadata"]
    if not isinstance(rm, dict):
        return "run_metadata is not an object"
    for k in ("run_label", "run_date", "runner_identity", "scope_surfaces"):
        if k not in rm:
            return f"run_metadata.{k} missing"
    if not isinstance(payload["findings"], list):
        return "findings is not a list"
    return None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture a hardening findings JSON and POST to Z-SPAN.",
    )
    parser.add_argument(
        "--findings", required=True,
        help="path to the findings JSON file the Gemini-Pro pass produced",
    )
    parser.add_argument(
        "--cookie",
        help="raw 'name=value' session cookie for the operator session",
    )
    parser.add_argument(
        "--cookie-file",
        help="path to a file containing the raw 'name=value' cookie",
    )
    parser.add_argument(
        "--base-url", default=DEFAULT_BASE_URL,
        help=f"Z-SPAN base URL (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--timeout-seconds", type=int, default=30,
        help="HTTP timeout (default: 30s)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="local sanity check + print payload summary; do NOT POST",
    )
    args = parser.parse_args(argv)

    findings_path = Path(args.findings)
    payload = _load_findings(findings_path)
    if payload is None:
        return 2

    err = _client_side_sanity(payload)
    if err:
        print(f"ERROR: client-side sanity check failed: {err}", file=sys.stderr)
        return 3

    summary = {
        "schema_version": payload.get("schema_version"),
        "run_label": payload["run_metadata"].get("run_label"),
        "run_date": payload["run_metadata"].get("run_date"),
        "runner_identity": payload["run_metadata"].get("runner_identity"),
        "scope_surfaces": payload["run_metadata"].get("scope_surfaces"),
        "findings_count": len(payload.get("findings", [])),
    }
    print("PAYLOAD SUMMARY:")
    print(json.dumps(summary, indent=2))

    if args.dry_run:
        print("--dry-run set; skipping POST.", file=sys.stderr)
        return 0

    cookie = _resolve_cookie(args)
    if not cookie:
        print(
            "ERROR: no operator cookie provided. Pass --cookie / --cookie-file, "
            "or set $ZSPAN_OPERATOR_COOKIE. The cookie value is the entire "
            "'name=value' string from the operator's signed-in session.",
            file=sys.stderr,
        )
        return 4

    url = args.base_url.rstrip("/") + "/api/operator/hardening-runs/ingest"
    headers = {
        "Cookie": cookie,
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(
            url, json=payload, headers=headers,
            timeout=args.timeout_seconds,
        )
    except requests.RequestException as exc:
        print(f"ERROR: network failure: {exc}", file=sys.stderr)
        return 5

    try:
        body = resp.json()
    except ValueError:
        body = {"raw_text": resp.text}

    print(f"HTTP {resp.status_code}")
    print(json.dumps(body, indent=2))
    if not resp.ok:
        return 6
    return 0


if __name__ == "__main__":
    sys.exit(main())
