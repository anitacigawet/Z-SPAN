#!/usr/bin/env python3.11
"""claude_p_metered -- budget-gated, self-metering wrapper around `claude -p`.

D-119 cost model. Per [[claude-headless-metered-as-of-2026-06-15]] headless
`claude -p` is now metered per token (interactive Claude + Workflow-subagent
calls are NOT), and 13 of the 14 fleet heartbeats invoke `claude -p`. So every
fleet heartbeat's claude call is wrapped here in a D-078 risky-API control
envelope -- per-day spend ceiling + per-call cap + refuse-and-escalate
auto-shutdown -- and self-meters its spend into balance_ledger under
provider='anthropic'.

Why self-meter: Anthropic has no per-key usage API equivalent to OpenAI's
/v1/organization/costs (which the balance-auditor reads), so the self-metered
rows ARE the Anthropic spend ground-truth. `claude -p --output-format
stream-json` reports `total_cost_usd` in its final `result` event; we parse it.

Finance nuance (D-121, James 2026-06-17 — do NOT re-conflate): the Anthropic
"balance" is NOT a deposited balance like the OpenAI funds. It is an INCLUDED
non-interactive compute cap that rides WITH the paid MAX subscription (~$100 at
MAX 5x, ~$200 at MAX 20x), granted by Anthropic to bound headless `claude -p` /
Agent-SDK use. Exercising it is using an included allowance, not drawing down
money James deposited. The DAILY GATE below (today's spend vs the per-day
ceiling) is what protects the cap and works regardless of any balance readout.
The cap resets MONTHLY (confirmed James 2026-06-17), so the daily gate (which
resets daily) is the correct + sufficient protection -- $3/day x 30 ~= $90/mo,
just under the ~$100 cap. A get_current_balance('anthropic') "remaining cap"
readout would need monthly-reset handling, so do NOT seed a naive one-time
starting-balance row (it would mislead after month 1); the readout is deferred as
not-needed. Each call appends
a `spend_observed` row with a per-call bucket window [start_unix, end_unix]
(unique per call -> no INSERT-OR-IGNORE collision).

Thresholds (operator-set per D-119; James 2026-06-17 chose $3/day + $1/call for
rung-1 calibration, walkable-up): read from user_settings.json keys
`anthropic_daily_ceiling_usd` / `anthropic_per_call_cap_usd`, else the defaults
below.

Nothing here spends until the fleet is deployed on launchd (Stage A, operator-
gated). This wrapper just gates + records the spend when those scheduled calls
eventually fire.

Usage (prompt via stdin):
    echo "$PROMPT" | python3.11 claude_p_metered.py \\
        --role orchestrator --model claude-opus-4-7 \\
        --settings <abs_path> --cwd <parsers_dir> --log <abs_path> [--no-chrome]

Test/inspection without spending:
    --claude-output-fixture <path>   read a captured stream-json instead of
                                     invoking claude (meter the fixture's cost)
    --dry-run                        gate-check + parse only; write NO ledger row

Exit codes:
    (passes through claude's own exit code on a normal metered run)
    6: misuse (empty prompt on stdin, bad args)
    7: budget ceiling reached -- call REFUSED, not fired (D-078 auto-shutdown)
    8: call ran but its cost could not be parsed (meter gap -- recorded as a
       flag + zero-cost row so the gap is visible; operator should investigate)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Make parsers/ importable (cwd may be parsers/ or elsewhere); mirrors the
# balance_auditor_*.py path shim so `from database import ...` resolves.
_PARSERS_DIR = Path(__file__).resolve().parent.parent
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

PROVIDER = "anthropic"
USER_SETTINGS_PATH = _PARSERS_DIR / "user_settings.json"

# D-119 rung-1 defaults (operator-chosen 2026-06-17; override in user_settings.json).
DEFAULT_DAILY_CEILING_CENTS = 300   # $3.00/day
DEFAULT_PER_CALL_CAP_CENTS = 100    # $1.00/call
DAILY_CEILING_KEY = "anthropic_daily_ceiling_usd"
PER_CALL_CAP_KEY = "anthropic_per_call_cap_usd"

SOURCE_TAG = "claude_p_self_meter"


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested; no DB, no subprocess)
# --------------------------------------------------------------------------- #
def load_thresholds(settings_path: Path = USER_SETTINGS_PATH) -> tuple[int, int]:
    """Return (daily_ceiling_cents, per_call_cap_cents).

    Reads optional USD overrides from user_settings.json; falls back to the
    D-119 rung-1 defaults. Malformed / missing values fall back silently to
    the default for that single knob (fail-safe toward the conservative
    default, never toward "no ceiling").
    """
    daily = DEFAULT_DAILY_CEILING_CENTS
    per_call = DEFAULT_PER_CALL_CAP_CENTS
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return daily, per_call
    for key, default, setter in (
        (DAILY_CEILING_KEY, DEFAULT_DAILY_CEILING_CENTS, "daily"),
        (PER_CALL_CAP_KEY, DEFAULT_PER_CALL_CAP_CENTS, "per_call"),
    ):
        val = data.get(key)
        if isinstance(val, (int, float)) and val > 0:
            cents = int(float(val) * 100 + 0.5)
            if setter == "daily":
                daily = cents
            else:
                per_call = cents
    return daily, per_call


def utc_today_start_unix(now_unix: Optional[int] = None) -> int:
    """Unix epoch for 00:00:00 UTC of the current day (the per-day window).

    UTC (not local) for consistency with OpenAI's UTC date-granular buckets;
    the ceiling is a coarse guardrail, so the UTC vs local boundary is
    immaterial in practice.
    """
    now = datetime.fromtimestamp(now_unix, tz=timezone.utc) if now_unix is not None \
        else datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def parse_cost_cents(stream_text: str) -> tuple[Optional[int], Optional[dict]]:
    """Parse the final `result` event of a stream-json transcript.

    Returns (cost_cents, usage_dict). cost_cents is None when no result event
    with a numeric total_cost_usd is found (call crashed before completing, or
    a non-stream-json format). Tolerant of interleaved non-JSON lines.
    """
    cost_usd: Optional[float] = None
    usage: Optional[dict] = None
    for line in stream_text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except ValueError:
            continue
        if not isinstance(evt, dict):
            continue
        if evt.get("type") == "result":
            tc = evt.get("total_cost_usd")
            if isinstance(tc, (int, float)):
                cost_usd = float(tc)
            u = evt.get("usage")
            if isinstance(u, dict):
                usage = u
    if cost_usd is None:
        return None, usage
    # Round half-UP (not banker's round, which zeroes a 0.5-cent cost): a spend
    # meter should never under-count toward the budget. cost_usd is always >= 0.
    return int(cost_usd * 100 + 0.5), usage


def gate_decision(today_spent_cents: int, daily_ceiling_cents: int) -> bool:
    """True = allowed to fire; False = refuse (already at/over the day's ceiling)."""
    return today_spent_cents < daily_ceiling_cents


# --------------------------------------------------------------------------- #
# DB-touching layer (thin wrappers over the proven database.py ledger helpers)
# --------------------------------------------------------------------------- #
def _warm_database() -> None:
    """Import database once with its init_db() banner routed to stderr, so this
    wrapper's stdout stays clean JSON for the heartbeat runner that parses it.
    Later `from database import ...` calls hit the cached module (no reprint)."""
    import contextlib
    with contextlib.redirect_stdout(sys.stderr):
        import database  # noqa: F401


def _today_spent_cents() -> int:
    from database import get_spend_observed_since
    return get_spend_observed_since(PROVIDER, utc_today_start_unix())


def _record_spend(cost_cents: int, *, role: str, start_unix: int, end_unix: int,
                  usage: Optional[dict], over_cap: bool, meter_ok: bool) -> Optional[int]:
    """Append one spend_observed row for this call. Bumps bucket_end_time by 1s
    on the (near-impossible, sequential-fleet) chance of a same-second bucket
    collision, so no metered call is ever silently dropped by INSERT OR IGNORE.
    """
    from database import append_ledger_event
    note = (
        f"role={role}; tokens_in={(usage or {}).get('input_tokens', '?')}; "
        f"tokens_out={(usage or {}).get('output_tokens', '?')}; "
        f"over_call_cap={over_cap}; meter_ok={meter_ok}"
    )
    end = max(end_unix, start_unix + 1)
    for _ in range(5):
        row_id = append_ledger_event(
            provider=PROVIDER, event_type="spend_observed", amount_cents=cost_cents,
            bucket_start_time=start_unix, bucket_end_time=end, source=SOURCE_TAG,
            notes=note,
        )
        if row_id is not None:
            return row_id
        end += 1  # collision: shift the window and retry
    return None


def _record_flag(reason: str, *, role: str, now_unix: int, amount_cents: Optional[int]) -> None:
    """Audit-trail row for a refusal or per-call-cap breach. discrepancy_flagged
    is not part of the balance math (database.get_current_balance) -- pure signal
    the orchestrator/auditor can surface to the operator (D-078 escalate)."""
    from database import append_ledger_event
    append_ledger_event(
        provider=PROVIDER, event_type="discrepancy_flagged", amount_cents=amount_cents,
        bucket_start_time=now_unix, bucket_end_time=now_unix + 1, source=SOURCE_TAG,
        notes=f"role={role}; {reason}",
    )


def _current_balance_cents() -> int:
    from database import get_current_balance
    return get_current_balance(PROVIDER)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run(args: argparse.Namespace, prompt: str) -> int:
    _warm_database()  # route database's init banner off stdout -> clean JSON out
    daily_ceiling, per_call_cap = load_thresholds()
    today_spent = _today_spent_cents()

    # --- D-078 gate: refuse + escalate if today's spend is at/over the ceiling.
    if not gate_decision(today_spent, daily_ceiling):
        if not args.dry_run:
            _record_flag(
                f"budget_refused today_spent_cents={today_spent} ceiling_cents={daily_ceiling}",
                role=args.role, now_unix=int(time.time()), amount_cents=None,
            )
        print(json.dumps({
            "ok": False, "refused": True, "provider": PROVIDER, "role": args.role,
            "today_spent_cents": today_spent, "daily_ceiling_cents": daily_ceiling,
            "reason": "daily Anthropic ceiling reached -- call refused (D-078 auto-shutdown)",
            "balance_cents": _current_balance_cents(),
        }, indent=2))
        return 7

    # --- Run the call (or read a fixture for cost-free inspection/testing).
    start = time.time()
    if args.claude_output_fixture:
        transcript = Path(args.claude_output_fixture).read_text(encoding="utf-8")
        claude_exit = 0
    else:
        cmd = ["claude", "-p", "--model", args.model, "--settings", args.settings,
               "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
        if args.no_chrome:
            cmd.append("--no-chrome")
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            cwd=args.cwd or None,
        )
        transcript = proc.stdout
        claude_exit = proc.returncode
        if args.log:
            Path(args.log).write_text(transcript, encoding="utf-8")
    end = time.time()

    cost_cents, usage = parse_cost_cents(transcript)
    meter_ok = cost_cents is not None
    record_cents = cost_cents if cost_cents is not None else 0
    over_cap = cost_cents is not None and cost_cents > per_call_cap

    if not args.dry_run:
        _record_spend(record_cents, role=args.role, start_unix=int(start),
                      end_unix=int(end), usage=usage, over_cap=over_cap, meter_ok=meter_ok)
        if over_cap:
            _record_flag(
                f"per_call_cap_exceeded cost_cents={cost_cents} cap_cents={per_call_cap}",
                role=args.role, now_unix=int(end), amount_cents=cost_cents,
            )

    summary = {
        "ok": True, "refused": False, "provider": PROVIDER, "role": args.role,
        "meter_ok": meter_ok, "call_cost_cents": cost_cents,
        "over_call_cap": over_cap, "per_call_cap_cents": per_call_cap,
        "today_spent_cents_before": today_spent,
        "today_spent_cents_after": today_spent + record_cents,
        "daily_ceiling_cents": daily_ceiling,
        "balance_cents": _current_balance_cents() if not args.dry_run else None,
        "claude_exit": claude_exit, "dry_run": args.dry_run,
    }
    if not meter_ok:
        summary["meter_warning"] = (
            "claude ran but total_cost_usd was unparseable -- recorded a "
            "zero-cost flagged row; investigate the transcript"
        )
    print(json.dumps(summary, indent=2))

    if not meter_ok:
        return 8
    return claude_exit


def main() -> int:
    ap = argparse.ArgumentParser(description="Budget-gated self-metering claude -p wrapper (D-119).")
    ap.add_argument("--role", required=True, help="fleet role label (for ledger notes)")
    ap.add_argument("--model", default="claude-opus-4-7")
    ap.add_argument("--settings", default="", help="path to the role's scoped settings.json")
    ap.add_argument("--cwd", default="", help="working dir for the claude subprocess (usually parsers/)")
    ap.add_argument("--log", default="", help="write the raw stream-json transcript here")
    ap.add_argument("--no-chrome", action="store_true", help="disable Chrome MCP init (headless)")
    ap.add_argument("--dry-run", action="store_true", help="gate + parse only; write NO ledger row")
    ap.add_argument("--claude-output-fixture", default="",
                    help="read this stream-json instead of invoking claude (cost-free inspection/test)")
    args = ap.parse_args()

    prompt = sys.stdin.read()
    if not args.claude_output_fixture and not prompt.strip():
        print("ERROR: empty prompt on stdin (pipe the prompt in).", file=sys.stderr)
        return 6

    return run(args, prompt)


if __name__ == "__main__":
    sys.exit(main())
