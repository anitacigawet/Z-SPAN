#!/usr/bin/env python3.11
"""pipeline_operator_escalate — allowlisted Slack escalation wrapper for the headless Pipeline Operator (S-004).

Mirrors the D-065 escalate wrapper pattern with role hardcoded to
'pipeline-operator'. The slack_notifier per-role registry rate-limit applies
automatically; no per-call override.

Pre-existing protections from slack_notifier (rate limit, local-fallback to
`pending_escalations`, role-header attribution) flow through unchanged.

Severity is one of: info | decision | blocked | error.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `parsers/` importable when invoked from cwd=parsers/.
_PARSERS_DIR = Path(__file__).resolve().parent.parent
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from slack_notifier import send_escalation, SEVERITY_LEVELS  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send a Slack escalation as the pipeline-operator.",
    )
    parser.add_argument(
        "--severity", required=True, choices=list(SEVERITY_LEVELS),
        help="info / decision / blocked / error (per slack_notifier).",
    )
    parser.add_argument(
        "--summary", required=True,
        help="One-sentence headline (D-054 human prose, no schema field names).",
    )
    parser.add_argument(
        "--see", action="append", default=[], metavar="LINE",
        help="Bullet line for 'What I see'. Repeatable.",
    )
    parser.add_argument(
        "--do", action="append", default=[], metavar="LINE",
        help="Bullet line for 'What I'd do'. Repeatable.",
    )
    parser.add_argument(
        "--deep-link", default=None,
        help="Optional operator-surface URL the operator can click into.",
    )
    parser.add_argument(
        "--audit-row", default=None,
        help="Optional 'table.id=N' reference (e.g., work_orders.id=147) for already-escalated-skipped lookups.",
    )
    args = parser.parse_args()

    result = send_escalation(
        role="pipeline-operator",
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
