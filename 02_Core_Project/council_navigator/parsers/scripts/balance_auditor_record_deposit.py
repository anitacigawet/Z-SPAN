#!/usr/bin/env python3.11
"""balance_auditor_record_deposit -- log a deposit into the balance_ledger.

OpenAI doesn't expose deposit history through any public endpoint, so
the auditor can't observe "money in" from the API. James records each
deposit manually via this CLI (or a future HQ surface that posts to
/api/balance/deposit -- not built yet).

The auditor's reconciler then uses these deposit rows to compute the
current balance: sum(deposits) - sum(spend_observed).

This is operator-facing -- not called by the agent itself. It lives
in scripts/ alongside the other auditor wrappers so the patterns stay
co-located.

Usage:
    python3.11 scripts/balance_auditor_record_deposit.py \\
        --amount-usd 5.00 \\
        --provider openai \\
        --notes "Test deposit to validate auditor"

    python3.11 scripts/balance_auditor_record_deposit.py \\
        --amount-usd 20.00 \\
        --notes "Top-up via dashboard 2026-05-30"

Exit codes:
    0: deposit recorded (new ledger row appended)
    3: validation failed (amount, currency, etc.)
    4: ledger write failed
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Make parsers/ importable when invoked from cwd=parsers/.
_PARSERS_DIR = Path(__file__).resolve().parent.parent
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

from database import append_ledger_event, get_current_balance  # noqa: E402

ALLOWED_PROVIDERS = {"openai", "anthropic", "other"}


def _format_money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:.2f}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Append a deposit event to balance_ledger. Use this when you "
            "top up an OpenAI / Anthropic / other provider account."
        ),
    )
    parser.add_argument(
        "--amount-usd", type=str, required=True,
        help=(
            "Deposit amount in USD (e.g., 5.00, 20.00). Positive only -- "
            "for refunds or corrections use manual_correction (TODO: separate "
            "wrapper) or negative amount with --allow-negative."
        ),
    )
    parser.add_argument(
        "--provider", default="openai", choices=sorted(ALLOWED_PROVIDERS),
        help="Provider the deposit went to. Defaults to openai (the dominant cost source).",
    )
    parser.add_argument(
        "--notes", default="",
        help="Free-form note (where / when / why). Recommended: include the date "
             "+ method (e.g., 'Top-up via dashboard 2026-05-30').",
    )
    parser.add_argument(
        "--external-ref", default=None,
        help="Optional cross-reference (e.g., OpenAI invoice ID, bank statement line).",
    )
    parser.add_argument(
        "--allow-negative", action="store_true",
        help="Permit negative amounts (e.g., for refunds recorded as a "
             "manual_correction). Default behavior rejects negatives.",
    )
    args = parser.parse_args()

    try:
        amount_usd = float(args.amount_usd)
    except ValueError:
        print(f"DENIED: --amount-usd must be a number, got {args.amount_usd!r}", file=sys.stderr)
        return 3
    amount_cents = int(round(amount_usd * 100))

    if amount_cents == 0:
        print("DENIED: --amount-usd of 0 is not meaningful (no ledger entry created).", file=sys.stderr)
        return 3
    if amount_cents < 0 and not args.allow_negative:
        print(
            f"DENIED: --amount-usd negative ({amount_usd}). For refunds or corrections, "
            f"pass --allow-negative explicitly. Doing so logs as a manual_correction, "
            f"NOT a deposit_observed.",
            file=sys.stderr,
        )
        return 3

    # Negative amounts go in as manual_correction (clearer audit trail).
    event_type = "manual_correction" if amount_cents < 0 else "deposit_observed"

    # Default the operator note with a UTC date stamp if blank.
    note_text = args.notes.strip()
    if not note_text:
        note_text = f"Operator-recorded {event_type} on {datetime.now(tz=timezone.utc).date().isoformat()}"

    row_id = append_ledger_event(
        provider=args.provider,
        event_type=event_type,
        amount_cents=amount_cents,
        source="operator_manual",
        notes=note_text,
        external_ref=args.external_ref,
    )

    if row_id is None:
        # Unique-constraint collision -- shouldn't happen for deposits (bucket
        # times are NULL), but defensive.
        print(
            "WARNING: ledger row was treated as a duplicate (unexpected for "
            "deposit / correction events). Inspect balance_ledger manually.",
            file=sys.stderr,
        )
        return 4

    new_balance = get_current_balance(args.provider)
    summary = {
        "ok": True,
        "row_id": row_id,
        "event_type": event_type,
        "provider": args.provider,
        "amount_cents": amount_cents,
        "amount_pretty": _format_money(amount_cents),
        "new_balance_cents": new_balance,
        "new_balance_pretty": _format_money(new_balance),
        "notes": note_text,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
