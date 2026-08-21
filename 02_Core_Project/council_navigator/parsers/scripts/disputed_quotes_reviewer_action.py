#!/usr/bin/env python3.11
"""disputed_quotes_reviewer_action — allowlisted mutating-action wrapper for the headless DQR (S-004).

Stage-B wrapper pattern (parallel to D-065's orchestrator board-read shape but
for mutating POST endpoints): typed sub-commands per action, hardcoded
endpoint paths + JSON body shapes built from validated CLI args. The agent
cannot construct arbitrary JSON bodies.

The DQR's two mutating endpoints (per agents/disputed-quotes-reviewer.md):
  - /api/disputed-quotes/<id>/resolve        → "verify" / "reject" sub-commands
  - /api/disputed-quotes/<id>/agent-propose  → "agent-propose" sub-command

`resolved_by` / `agent_role` are HARDCODED to 'disputed-quotes-reviewer' —
the agent cannot impersonate operator or another agent. Forge-resistant.

Usage:
    python3.11 scripts/disputed_quotes_reviewer_action.py verify \\
        --quote-id 42 [--quote-text "<polished form>"] \\
        [--resolver-notes "<one-to-three-sentence reasoning>"]

    python3.11 scripts/disputed_quotes_reviewer_action.py reject \\
        --quote-id 42 [--resolver-notes "<reasoning>"]

    python3.11 scripts/disputed_quotes_reviewer_action.py agent-propose \\
        --quote-id 42 --proposed-quote-text "<your candidate text>" \\
        [--reasoning "<why mine is better than cleaner+verifier>"]
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
ROLE = "disputed-quotes-reviewer"
TIMEOUT_SECONDS = 10

# HARDCODED here, never derived from CLI args.
RESOLVED_BY = ROLE

# S-008 V0 / surface S-7: free-text-field caps on agent-emitted strings.
MAX_QUOTE_TEXT_LEN = 8_000
MAX_RESOLVER_NOTES_LEN = 4_000
MAX_REASONING_LEN = 4_000


def _validate_agent_text(
    value, field_name: str, max_length: int
):  # -> str | None
    """Defer to parsers.agent_audit.validate_agent_text with consistent
    error handling — on validation failure, print DENIED + exit 3."""
    try:
        from parsers.agent_audit import validate_agent_text  # local import
        from parsers.input_security.primitives import UnicodeRejectionError
    except Exception as e:
        print(f"WARN: agent_audit unavailable; skipping validation ({e})", file=sys.stderr)
        return value

    try:
        return validate_agent_text(value, field_name=field_name, max_length=max_length)
    except (ValueError, UnicodeRejectionError) as e:
        print(f"DENIED: {field_name}: {e}", file=sys.stderr)
        sys.exit(3)


def _audit(action_name: str, quote_id: int, body: dict, reasoning: str | None = None) -> None:
    """Best-effort audit-log write. Never raises into caller."""
    try:
        from parsers.agent_audit import record_agent_action  # local import
    except Exception:
        return
    record_agent_action(
        agent_role=ROLE,
        action_name=action_name,
        action_argument_table="quotes",
        action_argument_id=quote_id,
        action_body=body,
        reasoning=reasoning,
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
    """POST the action through PC's agent relay (D-099 Phase 2.1e)."""
    payload: dict = {
        "cmd": args.cmd,
        "quote_id": args.quote_id,
    }
    if args.cmd == "verify":
        if getattr(args, "quote_text", None):
            payload["quote_text"] = args.quote_text
        if getattr(args, "resolver_notes", None):
            payload["resolver_notes"] = args.resolver_notes
    elif args.cmd == "reject":
        if getattr(args, "resolver_notes", None):
            payload["resolver_notes"] = args.resolver_notes
    elif args.cmd == "agent-propose":
        payload["proposed_quote_text"] = args.proposed_quote_text
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


def cmd_verify(args: argparse.Namespace) -> int:
    # S-008 V0 / S-7: validate + normalize agent-emitted text before POST.
    quote_text = _validate_agent_text(args.quote_text, "--quote-text", MAX_QUOTE_TEXT_LEN)
    resolver_notes = _validate_agent_text(args.resolver_notes, "--resolver-notes", MAX_RESOLVER_NOTES_LEN)
    body: dict = {
        "action": "verify",
        "resolved_by": RESOLVED_BY,
    }
    if quote_text:
        body["quote_text"] = quote_text
    if resolver_notes:
        body["resolver_notes"] = resolver_notes
    rc = _post(f"/api/disputed-quotes/{args.quote_id}/resolve", body)
    if rc == 0:
        _audit("verify", args.quote_id, body, reasoning=resolver_notes)
    return rc


def cmd_reject(args: argparse.Namespace) -> int:
    resolver_notes = _validate_agent_text(args.resolver_notes, "--resolver-notes", MAX_RESOLVER_NOTES_LEN)
    body: dict = {
        "action": "reject",
        "resolved_by": RESOLVED_BY,
    }
    if resolver_notes:
        body["resolver_notes"] = resolver_notes
    rc = _post(f"/api/disputed-quotes/{args.quote_id}/resolve", body)
    if rc == 0:
        _audit("reject", args.quote_id, body, reasoning=resolver_notes)
    return rc


def cmd_agent_propose(args: argparse.Namespace) -> int:
    if not args.proposed_quote_text or not args.proposed_quote_text.strip():
        print("DENIED: --proposed-quote-text is required and must be non-empty", file=sys.stderr)
        return 3
    proposed = _validate_agent_text(args.proposed_quote_text, "--proposed-quote-text", MAX_QUOTE_TEXT_LEN)
    reasoning = _validate_agent_text(args.reasoning, "--reasoning", MAX_REASONING_LEN)
    body: dict = {
        "proposed_quote_text": proposed,
        "agent_role": ROLE,
    }
    if reasoning:
        body["reasoning"] = reasoning
    rc = _post(f"/api/disputed-quotes/{args.quote_id}/agent-propose", body)
    if rc == 0:
        _audit("agent-propose", args.quote_id, body, reasoning=reasoning)
    return rc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Disputed Quotes Reviewer mutating actions (typed sub-commands; "
                    "endpoint paths + JSON bodies hardcoded per command).",
    )
    parser.add_argument(
        "--http-source", default=None,
        help="Optional. Base URL of PC's agent relay disputed-quotes-reviewer namespace. Mac path.",
    )
    parser.add_argument(
        "--http-bearer", default=None,
        help="Bearer token paired with --http-source.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_verify = sub.add_parser("verify", help="Mark a disputed quote as verified.")
    p_verify.add_argument("--quote-id", type=int, required=True)
    p_verify.add_argument(
        "--quote-text", default=None,
        help="Optional: polished form to promote as canonical (word-content-identical, just caps+punct).",
    )
    p_verify.add_argument("--resolver-notes", default=None)
    p_verify.set_defaults(func=cmd_verify)

    p_reject = sub.add_parser("reject", help="Mark a disputed quote as rejected.")
    p_reject.add_argument("--quote-id", type=int, required=True)
    p_reject.add_argument("--resolver-notes", default=None)
    p_reject.set_defaults(func=cmd_reject)

    p_propose = sub.add_parser(
        "agent-propose",
        help="Record a counter-proposal (D-057) — your better quote_text candidate.",
    )
    p_propose.add_argument("--quote-id", type=int, required=True)
    p_propose.add_argument("--proposed-quote-text", required=True)
    p_propose.add_argument("--reasoning", default=None)
    p_propose.set_defaults(func=cmd_agent_propose)

    args = parser.parse_args()
    if args.http_source:
        return _post_to_callback(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
