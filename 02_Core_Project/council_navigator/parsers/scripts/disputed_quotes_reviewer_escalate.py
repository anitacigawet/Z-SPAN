#!/usr/bin/env python3.11
"""disputed_quotes_reviewer_escalate — allowlisted Slack escalation wrapper for the headless DQR (S-004).

Mirrors the orchestrator's escalate wrapper (D-065) with role hardcoded
to 'disputed-quotes-reviewer'. The slack_notifier per-role rate limit
(5/hour default for Opus-class judgment agents) applies automatically.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

_PARSERS_DIR = Path(__file__).resolve().parent.parent
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SEVERITY_LEVELS = ("info", "decision", "blocked", "error")


def _post_to_callback(args: argparse.Namespace) -> int:
    payload = {
        "severity": args.severity,
        "summary": args.summary,
        "see": list(args.see) if args.see else None,
        "do": list(args.do) if args.do else None,
        "deep_link": args.deep_link,
        "audit_row": args.audit_row,
    }
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
        description="Send a Slack escalation as the Disputed Quotes Reviewer (S-004).",
    )
    parser.add_argument(
        "--severity", required=True, choices=list(SEVERITY_LEVELS),
        help="info / decision / blocked / error.",
    )
    parser.add_argument("--summary", required=True)
    parser.add_argument("--see", action="append", default=[], metavar="LINE")
    parser.add_argument("--do", action="append", default=[], metavar="LINE")
    parser.add_argument("--deep-link", default=None)
    parser.add_argument("--audit-row", default=None)
    parser.add_argument(
        "--http-callback", default=None,
        help="Optional. POST args to PC's agent relay instead of invoking slack_notifier locally.",
    )
    parser.add_argument(
        "--http-bearer", default=None,
        help="Bearer token paired with --http-callback.",
    )
    args = parser.parse_args()

    if args.http_callback:
        return _post_to_callback(args)

    from slack_notifier import send_escalation  # noqa: E402

    result = send_escalation(
        role="disputed-quotes-reviewer",
        severity=args.severity,
        summary=args.summary,
        what_i_see=args.see or None,
        what_id_do=args.do or None,
        deep_link=args.deep_link,
        audit_row=args.audit_row,
    )

    print(
        f"escalated severity={args.severity} "
        f"slack={'yes' if result.delivered_to_slack else 'no'} "
        f"queued={'yes' if result.queued_locally else 'no'} "
        f"rate_limited={'yes' if result.rate_limited else 'no'} "
        f"pending_id={result.pending_id}"
    )
    if result.error:
        print(f"warning: {result.error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
