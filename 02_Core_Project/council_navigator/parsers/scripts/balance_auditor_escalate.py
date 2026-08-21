#!/usr/bin/env python3.11
"""balance_auditor_escalate -- allowlisted Slack escalation wrapper for the Balance Auditor.

D-066 pattern. Role hardcoded to 'balance-auditor'. The slack_notifier
per-role registry handles rate-limiting + persona attribution + local
fallback queue. Severity is one of: info | decision | blocked | error.

Used by:
  - ops/balance-auditor-heartbeat.ps1 -- daily heartbeat surface
  - (future) the V2 LLM-driven heartbeat -- once balance reasoning needs
    judgment beyond the V1 mechanical rules
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make parsers/ importable when invoked from cwd=parsers/.
_PARSERS_DIR = Path(__file__).resolve().parent.parent
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from database import get_unacked_escalation_by_prefix  # noqa: E402
from slack_notifier import send_escalation, SEVERITY_LEVELS  # noqa: E402

ROLE = "balance-auditor"


def _tail_cents(audit_row: str | None) -> int | None:
    """Parse the trailing ':<cents>' magnitude from a dedupe-stamped audit_row."""
    if not audit_row or ":" not in audit_row:
        return None
    try:
        return int(audit_row.rsplit(":", 1)[1])
    except (ValueError, TypeError):
        return None


def should_skip_duplicate(
    dedupe_prefix: str, current_cents: int | None
) -> tuple[bool, str]:
    """already-escalated-skipped guard (agents/README.md; adopted per D-106).

    Skip re-posting when an UNACKED escalation of the same threshold class
    already exists — same fact, no new information, pure badge inflation
    (the failure mode that produced 13 duplicate 'Balance critical' posts
    between 2026-05-31 and 2026-06-12). Escalate fresh only when the
    situation materially WORSENED since the prior post:
      - balance dropped by >= $1.00 since the prior escalation, OR
      - balance crossed zero (any depletion-to-or-below-zero is always news).
    A threshold-CLASS change (decision -> blocked) never matches the prefix,
    so class transitions always escalate fresh by construction.
    """
    prior = get_unacked_escalation_by_prefix(ROLE, dedupe_prefix)
    if prior is None:
        return False, ""
    prior_cents = _tail_cents(prior.get("audit_row"))
    if current_cents is not None and prior_cents is not None:
        crossed_zero = current_cents <= 0 < prior_cents
        dropped_dollar = current_cents <= prior_cents - 100
        if crossed_zero or dropped_dollar:
            return False, ""
    reason = (
        f"already-escalated-skipped: unacked escalation id={prior['id']} "
        f"({prior.get('audit_row')}, created {prior.get('created_at')}) already "
        f"covers this state; not re-posting. The prior escalation stays as the "
        f"live signal (agents/README.md § already-escalated-skipped; D-106)."
    )
    return True, reason


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send a Slack escalation as the Balance Auditor.",
    )
    parser.add_argument(
        "--severity", required=True, choices=list(SEVERITY_LEVELS),
        help="info / decision / blocked / error.",
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
        help="Optional 'table.id=N' reference (e.g., 'balance_ledger.id=42').",
    )
    parser.add_argument(
        "--dedupe-prefix", default=None, metavar="CLASS",
        help=(
            "Escalation-class prefix for the already-escalated-skipped guard "
            "(e.g., 'balance_threshold:blocked'). When an UNACKED escalation "
            "with this audit_row prefix exists and the state hasn't materially "
            "worsened, the post is skipped (exit 0). D-106."
        ),
    )
    parser.add_argument(
        "--dedupe-cents", type=int, default=None,
        help=(
            "Current magnitude in cents for the worsening check; also stamped "
            "into audit_row as '<prefix>:<cents>' so future runs can compare."
        ),
    )
    args = parser.parse_args()

    if args.dedupe_prefix:
        skip, reason = should_skip_duplicate(args.dedupe_prefix, args.dedupe_cents)
        if skip:
            print(reason)
            return 0
        # Stamp the dedupe marker so future heartbeats can find + compare this row.
        if args.audit_row is None:
            args.audit_row = (
                f"{args.dedupe_prefix}:{args.dedupe_cents}"
                if args.dedupe_cents is not None
                else args.dedupe_prefix
            )

    result = send_escalation(
        role=ROLE,
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
