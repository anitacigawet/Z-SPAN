#!/usr/bin/env python3.11
"""pipeline_operator_action — allowlisted mutating-action wrapper for the headless Pipeline Operator (S-004).

D-066 action-wrapper pattern (typed sub-commands per endpoint, hardcoded
endpoint paths + empty/minimal JSON bodies). The agent cannot construct
arbitrary JSON or hit endpoints outside its lane:

  - process              → POST /api/work-orders/<id>/process
  - retry                → POST /api/work-orders/<id>/retry
  - build-review-queue   → POST /api/work-orders/<id>/build-review-queue
  - ingest-responses     → POST /api/work-orders/<id>/ingest-responses

Each endpoint takes only the work_order_id from the URL (no body fields the
agent could forge). Role attribution: `X-Zspan-Agent-Role: pipeline-operator`
on every request (hardcoded; not derived from CLI args).

Explicitly NOT exposed here (the structural wall against D-005 / D-006 / D-049
violations the manual prohibits):
  - /push-to-flagship    (owner-only flagship sync; D-049)
  - /approve             (publication gate; D-006)
  - /burn                (destructive; always escalate first per manual)
  - /set-video-url       (REMOVED per D-138 — returns HTTP 410. autonomous
                          replacement: haiku_match_videos.py)
  - /confirm-match       (operator decision — confirms autonomous match,
                          not manual paste)

If the agent's reasoning ever wants to call those, that IS the escalation event
— the wall blocks structurally, and the absence here makes it explicit.

Usage:
    python3.11 scripts/pipeline_operator_action.py process --work-order-id 147
    python3.11 scripts/pipeline_operator_action.py retry --work-order-id 147
    python3.11 scripts/pipeline_operator_action.py build-review-queue --work-order-id 145
    python3.11 scripts/pipeline_operator_action.py ingest-responses --work-order-id 145
"""
from __future__ import annotations

import argparse
import os
import sys

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Make parsers/ importable (for agent_auth) when invoked from scripts/.
_PARSERS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARSERS_DIR not in sys.path:
    sys.path.insert(0, _PARSERS_DIR)

FLASK_BASE = "http://127.0.0.1:5001"
ROLE = "pipeline-operator"

# [BUILD] is synchronous on the server side (yt-dlp source download on cold
# cache); the wrapper waits up to 13 minutes (server-side cap is 12 min,
# plus a small buffer). [INGEST] caps at 60s server-side. The default 10s is
# fine for [PROCESS] and [RETRY], which spawn the worker and return promptly.
DEFAULT_TIMEOUT_SECONDS = 10
BUILD_TIMEOUT_SECONDS = 13 * 60
INGEST_TIMEOUT_SECONDS = 70


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


def _post(path: str, *, timeout: int) -> int:
    url = f"{FLASK_BASE}{path}"
    headers = {
        "X-Zspan-Agent-Role": ROLE,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    headers.update(_agent_bearer())
    try:
        r = requests.post(url, json={}, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        print(f"ERROR: POST {url} failed: {exc}", file=sys.stderr)
        return 4

    print(r.text)
    if not r.ok:
        print(f"ERROR: HTTP {r.status_code}", file=sys.stderr)
        return 5
    return 0


def cmd_process(args: argparse.Namespace) -> int:
    return _post(
        f"/api/work-orders/{args.work_order_id}/process",
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )


def cmd_retry(args: argparse.Namespace) -> int:
    return _post(
        f"/api/work-orders/{args.work_order_id}/retry",
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )


def cmd_build_review_queue(args: argparse.Namespace) -> int:
    return _post(
        f"/api/work-orders/{args.work_order_id}/build-review-queue",
        timeout=BUILD_TIMEOUT_SECONDS,
    )


def cmd_ingest_responses(args: argparse.Namespace) -> int:
    return _post(
        f"/api/work-orders/{args.work_order_id}/ingest-responses",
        timeout=INGEST_TIMEOUT_SECONDS,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pipeline Operator mutating actions (typed sub-commands; "
                    "endpoint paths hardcoded per command; no body fields).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_process = sub.add_parser(
        "process",
        help="[PROCESS] — kick the bridge worker to generate outputs for this WO.",
    )
    p_process.add_argument("--work-order-id", type=int, required=True)
    p_process.set_defaults(func=cmd_process)

    p_retry = sub.add_parser(
        "retry",
        help="[RETRY] — reset a failed/awaiting_notebook WO to pending (D-033 Case A).",
    )
    p_retry.add_argument("--work-order-id", type=int, required=True)
    p_retry.set_defaults(func=cmd_retry)

    p_build = sub.add_parser(
        "build-review-queue",
        help="[BUILD] — extract clips for T-013 V4 review (synchronous, up to ~12 min).",
    )
    p_build.add_argument("--work-order-id", type=int, required=True)
    p_build.set_defaults(func=cmd_build_review_queue)

    p_ingest = sub.add_parser(
        "ingest-responses",
        help="[INGEST] — parse RESPONSE.md files for the meeting into the quotes table.",
    )
    p_ingest.add_argument("--work-order-id", type=int, required=True)
    p_ingest.set_defaults(func=cmd_ingest_responses)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
