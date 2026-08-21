#!/usr/bin/env python3.11
"""vocabulary_curator_action — allowlisted mutating-action wrapper for the headless Vocabulary Curator (S-004).

This is the NEW Stage-B wrapper pattern (parallel to D-065's orchestrator
board-read shape but for mutating POST endpoints): typed sub-commands per
action, each with a hardcoded endpoint path + a hardcoded JSON body shape
built from validated CLI args. The agent cannot construct arbitrary JSON
bodies — every key in every POST is one this wrapper authored, eliminating
the "padded body" injection class.

The curator's three mutating endpoints (per agents/vocabulary-curator.md):
  - /api/vocabulary-inbox/promote               → "promote" sub-command
  - /api/vocabulary-inbox/reject                → "reject" sub-command
  - /api/vocabulary-inbox/<id>/agent-propose    → "agent-propose" sub-command

`promoted_by` / `rejected_by` / `agent_role` are HARDCODED to
'vocabulary-curator' — the agent cannot impersonate operator or another
agent via these endpoints. Role attribution stays forge-resistant.

Usage:
    python3.11 scripts/vocabulary_curator_action.py promote --correction-id 42 [--category street]
    python3.11 scripts/vocabulary_curator_action.py reject --correction-id 42
    python3.11 scripts/vocabulary_curator_action.py agent-propose \\
        --correction-id 42 --proposed-right "Councilmember Stehly" \\
        [--reasoning "title 'Counselor' implies legal advisor; canonical Kingman title is Councilmember"]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Make `parsers.*` imports resolve when invoked from scripts/.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARSERS_DIR = os.path.dirname(_THIS_DIR)
_PROJECT_ROOT = os.path.dirname(_PARSERS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
if _PARSERS_DIR not in sys.path:  # so `import agent_auth` (RR-8) resolves
    sys.path.insert(0, _PARSERS_DIR)

FLASK_BASE = "http://127.0.0.1:5001"
ROLE = "vocabulary-curator"
TIMEOUT_SECONDS = 10

# S-008 V0 / surface S-7: free-text-field caps on agent-emitted strings.
MAX_PROPOSED_RIGHT_LEN = 512
MAX_REASONING_LEN = 4_000


def _validate_agent_text(value, field_name: str, max_length: int):
    """Defer to parsers.agent_audit.validate_agent_text with consistent
    error handling — on validation failure, print DENIED + exit 3."""
    try:
        from parsers.agent_audit import validate_agent_text
        from parsers.input_security.primitives import UnicodeRejectionError
    except Exception as e:
        print(f"WARN: agent_audit unavailable; skipping validation ({e})", file=sys.stderr)
        return value

    try:
        return validate_agent_text(value, field_name=field_name, max_length=max_length)
    except (ValueError, UnicodeRejectionError) as e:
        print(f"DENIED: {field_name}: {e}", file=sys.stderr)
        sys.exit(3)


def _audit(action_name: str, correction_id: int, body: dict, reasoning: str | None = None) -> None:
    """Best-effort audit-log write. Never raises into caller."""
    try:
        from parsers.agent_audit import record_agent_action
    except Exception:
        return
    record_agent_action(
        agent_role=ROLE,
        action_name=action_name,
        action_argument_table="vocabulary_corrections",
        action_argument_id=correction_id,
        action_body=body,
        reasoning=reasoning,
    )

# Operator/agent self-identification — HARDCODED here, never derived from
# CLI args. The agent cannot pose as "operator" or another agent via these
# endpoints.
PROMOTED_BY = ROLE
REJECTED_BY = ROLE

# Valid `category` values for promote (operator metadata; agent picks one
# that fits the wrong/right pair, leaves null when none clearly applies).
# Per agents/vocabulary-curator.md § Per-correction action shapes.
VALID_CATEGORIES = (
    "person", "street", "place", "business", "civic_term", "event", "other",
)


def _agent_bearer() -> dict:
    """RR-8 SEC-AUTH: attach the fleet bearer so owner-or-token routes
    authenticate. Degrades to no header if agent_auth is unreachable — the
    ungated commands still run; the gated routes then fail closed on the
    server's own 401."""
    try:
        from agent_auth import bearer_header
        return bearer_header()
    except Exception:
        return {}


def _post(path: str, body: dict) -> int:
    import requests  # noqa: PLC0415
    url = f"{FLASK_BASE}{path}"
    headers = {
        "X-Zspan-Agent-Role": ROLE,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    headers.update(_agent_bearer())
    try:
        r = requests.post(url, json=body, headers=headers, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        print(f"ERROR: POST {url} failed: {exc}", file=sys.stderr)
        return 4

    print(r.text)
    if not r.ok:
        print(f"ERROR: HTTP {r.status_code}", file=sys.stderr)
        return 5
    return 0


def _post_to_callback(args: argparse.Namespace) -> int:
    """POST the action through PC's agent relay (D-099 Phase 2.1e).

    Body is {cmd, correction_id, category, proposed_right, reasoning} — the
    relay reconstructs the wrapper CLI args + runs the wrapper locally
    (without --http-source) so the same body-construction logic + role
    hardcoding applies.
    """
    payload: dict = {
        "cmd": args.cmd,
        "correction_id": args.correction_id,
    }
    if args.cmd == "promote":
        if getattr(args, "category", None):
            payload["category"] = args.category
    elif args.cmd == "agent-propose":
        payload["proposed_right"] = args.proposed_right
        if getattr(args, "reasoning", None):
            payload["reasoning"] = args.reasoning

    headers = {"Content-Type": "application/json"}
    if args.http_bearer:
        headers["Authorization"] = f"Bearer {args.http_bearer}"
    url = f"{args.http_source.rstrip('/')}/action"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST",
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
        print(f"ERROR: HTTP {e.code} from {url}: {err_body[:300]}", file=sys.stderr)
        return 5
    except urllib.error.URLError as e:
        print(f"ERROR: could not reach {url}: {e}", file=sys.stderr)
        return 5


def cmd_promote(args: argparse.Namespace) -> int:
    body: dict = {
        "correction_id": args.correction_id,
        "promoted_by": PROMOTED_BY,
    }
    if args.category:
        if args.category not in VALID_CATEGORIES:
            print(
                f"DENIED: category {args.category!r} not in {VALID_CATEGORIES}",
                file=sys.stderr,
            )
            return 3
        body["category"] = args.category
    rc = _post("/api/vocabulary-inbox/promote", body)
    if rc == 0:
        _audit("promote", args.correction_id, body)
    return rc


def cmd_reject(args: argparse.Namespace) -> int:
    body = {
        "correction_id": args.correction_id,
        "rejected_by": REJECTED_BY,
    }
    rc = _post("/api/vocabulary-inbox/reject", body)
    if rc == 0:
        _audit("reject", args.correction_id, body)
    return rc


def cmd_agent_propose(args: argparse.Namespace) -> int:
    if not args.proposed_right or not args.proposed_right.strip():
        print("DENIED: --proposed-right is required and must be non-empty", file=sys.stderr)
        return 3
    proposed_right = _validate_agent_text(args.proposed_right, "--proposed-right", MAX_PROPOSED_RIGHT_LEN)
    reasoning = _validate_agent_text(args.reasoning, "--reasoning", MAX_REASONING_LEN)
    body: dict = {
        "proposed_right": proposed_right,
        "agent_role": ROLE,
    }
    if reasoning:
        body["reasoning"] = reasoning
    rc = _post(f"/api/vocabulary-inbox/{args.correction_id}/agent-propose", body)
    if rc == 0:
        _audit("agent-propose", args.correction_id, body, reasoning=reasoning)
    return rc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Vocabulary Curator mutating actions (typed sub-commands; "
                    "endpoint paths + JSON bodies hardcoded per command).",
    )
    parser.add_argument(
        "--http-source", default=None,
        help="Optional. Base URL of PC's agent relay vocabulary-curator namespace. Mac path.",
    )
    parser.add_argument(
        "--http-bearer", default=None,
        help="Bearer token paired with --http-source.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_promote = sub.add_parser("promote", help="Accept the verifier's `right` value.")
    p_promote.add_argument("--correction-id", type=int, required=True)
    p_promote.add_argument(
        "--category", default=None,
        help=f"Optional category: one of {VALID_CATEGORIES}.",
    )
    p_promote.set_defaults(func=cmd_promote)

    p_reject = sub.add_parser("reject", help="Drop the correction from auto-application.")
    p_reject.add_argument("--correction-id", type=int, required=True)
    p_reject.set_defaults(func=cmd_reject)

    p_propose = sub.add_parser(
        "agent-propose",
        help="Record a counter-proposal (D-057) — your better `right` value.",
    )
    p_propose.add_argument("--correction-id", type=int, required=True)
    p_propose.add_argument("--proposed-right", required=True)
    p_propose.add_argument("--reasoning", default=None)
    p_propose.set_defaults(func=cmd_agent_propose)

    args = parser.parse_args()
    if args.http_source:
        return _post_to_callback(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
