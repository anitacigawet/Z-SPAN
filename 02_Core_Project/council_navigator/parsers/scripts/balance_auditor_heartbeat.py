#!/usr/bin/env python3.11
"""balance_auditor_heartbeat -- the daily Balance Auditor orchestration (pure Python).

Mac port of ops/balance-auditor-heartbeat.ps1 (D-120 launchd port). The auditor
(agents/balance-auditor.md) is rules-driven, not spirit-driven, and calls NO
`claude -p` -- so this job is NOT metered (it talks to OpenAI's billing API +
deterministic Python, never the Anthropic headless API). The PowerShell wrapper
held the orchestration logic; this moves it into Python so the `.sh` launcher
(ops/balance-auditor-heartbeat.sh) + com.zspan.balance-auditor launchd plist stay
thin. The `.ps1` stays on disk as preserved-as-reference.

Each daily fire:
  1. balance_auditor_balance_check.py -- fetch OpenAI usage, append spend_observed
     rows for finalized buckets, append an api_balance_snapshot, return a JSON
     summary on stdout.
  2. Evaluate thresholds from that summary (blocked < $1, decision < $5) + the
     trailing-7d spend anomaly (B-prime, James 2026-05-31).
  3. balance_auditor_escalate.py -- post Slack info/decision/blocked/error, with
     the D-106 already-escalated-skipped dedupe guard on the threshold rows.
  3b. balance_auditor_bmac_check.py -- the donations leg (added 2026-07-08):
     ingest Buy Me a Coffee supporter payments as API-sourced deposit_observed
     rows (provider='bmac'). GUARDED: inert (one log line, no escalation) until
     `bmac_api_token` exists in user_settings.json -- the operator pasting the
     token is the activation switch. New deposits surface as an 'info'
     escalation; transport/shape failures with a configured token escalate
     'error'.
  4. balance_auditor_refresh_infographic.py -- best-effort daily press-TV
     infographic refresh that piggy-backs on the same daily window.

Idempotent: the spend rows have a UNIQUE(provider, event_type, bucket_start,
bucket_end) constraint, so re-running on the same day is a DB no-op.

Usage:
    python3.11 scripts/balance_auditor_heartbeat.py [--dry-run]
    (run from the parsers/ dir, or via ops/balance-auditor-heartbeat.sh)

Exit codes:
    0  ok (healthy or escalated cleanly)
    2  openai_usage_key not configured (a one-time 'decision' escalation fired)
    3  OpenAI billing API call failed (an 'error' escalation fired)
    4  balance_check ran but its JSON summary couldn't be parsed
    <n> any other unexpected balance_check exit is passed through
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
_PARSERS_DIR = _SCRIPTS_DIR.parent
_REPO_ROOT = _PARSERS_DIR.parents[2]
_LOGS_DIR = _REPO_ROOT / "ops" / "balance-auditor-logs"

# Thresholds, matching agents/balance-auditor.md (V1 hardcoded constants).
BALANCE_DECISION_FLOOR_CENTS = 500   # $5.00
BALANCE_BLOCKED_FLOOR_CENTS = 100    # $1.00

_LOG_LINES: list[str] = []


def _log(msg: str) -> None:
    """Narrate to stderr (launchd captures it) and buffer for the per-run log."""
    line = f"[{_dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, file=sys.stderr, flush=True)
    _LOG_LINES.append(line)


def _run_wrapper(script: str, args: list[str], *, label: str) -> tuple[str, str, int]:
    """Run a sibling wrapper script under this same interpreter, cwd=parsers/.
    Returns (stdout, stderr, exit). Both streams are appended to the run log."""
    cmd = [sys.executable, str(_SCRIPTS_DIR / script), *args]
    proc = subprocess.run(cmd, cwd=str(_PARSERS_DIR), capture_output=True, text=True)
    if proc.stdout:
        _LOG_LINES.append(f"--- {label} stdout ---")
        _LOG_LINES.append(proc.stdout)
    if proc.stderr:
        _LOG_LINES.append(f"--- {label} stderr ---")
        _LOG_LINES.append(proc.stderr)
    return proc.stdout, proc.stderr, proc.returncode


def _escalate(severity: str, summary: str, *, see: Optional[list[str]] = None,
              do: Optional[list[str]] = None, dedupe_prefix: str = "",
              dedupe_cents: Optional[int] = None) -> None:
    args = ["--severity", severity, "--summary", summary]
    for s in (see or []):
        args += ["--see", s]
    for d in (do or []):
        args += ["--do", d]
    if dedupe_prefix:
        args += ["--dedupe-prefix", dedupe_prefix]
        if dedupe_cents is not None:
            args += ["--dedupe-cents", str(dedupe_cents)]
    _log(f"Escalating severity={severity} summary={summary}")
    _run_wrapper("balance_auditor_escalate.py", args, label=f"escalate-{severity}")


def run(dry_run: bool) -> int:
    _log("Balance Auditor heartbeat firing...")
    _log(f"  cwd: {_PARSERS_DIR}")
    _log(f"  dry run: {dry_run}")

    # 1. balance_check.
    check_args = ["--dry-run"] if dry_run else []
    _log(f"Running balance_check ({' '.join(['balance_auditor_balance_check.py', *check_args])})...")
    stdout, _stderr, exit_code = _run_wrapper(
        "balance_auditor_balance_check.py", check_args, label="balance_check")
    _log(f"balance_check exit code: {exit_code}")

    if exit_code == 2:
        _log("balance_check returned 2: openai_usage_key not configured.")
        if not dry_run:
            _escalate(
                "decision", "Balance Auditor needs an OpenAI usage key",
                see=["I can't reconcile spend without the restricted-scope key.",
                     "Looking for 'openai_usage_key' in user_settings.json -- not set."],
                do=["Create a restricted project key in the OpenAI dashboard with the api.usage.read scope.",
                    "Add it to user_settings.json as 'openai_usage_key' (separate from openai_api_key).",
                    "Re-run the heartbeat (or wait for tomorrow's fire)."],
            )
        return 2

    if exit_code == 3:
        _log("balance_check returned 3: OpenAI API call failed.")
        if not dry_run:
            _escalate(
                "error", "Balance Auditor couldn't reach OpenAI billing API",
                see=["Check the ops/balance-auditor-logs/ run log for the HTTP error."],
                do=["If this persists, the openai_usage_key may have been revoked or its scope removed."],
            )
        return 3

    if exit_code != 0:
        _log(f"balance_check returned unexpected exit code {exit_code}; aborting.")
        return exit_code

    # 2. Parse the JSON summary (the last JSON object in stdout).
    brace = stdout.find("{")
    if brace < 0:
        _log("ERROR: could not find JSON summary in balance_check output. See log.")
        return 4
    try:
        summary: dict[str, Any] = json.loads(stdout[brace:])
    except (ValueError, json.JSONDecodeError) as exc:
        _log(f"ERROR: failed to parse balance_check JSON output: {exc}")
        return 4

    _log(f"Current balance: {summary.get('current_balance_pretty')}")
    _log(f"Today's running (estimate): {summary.get('todays_running_pretty')}")
    _log(f"Finalized buckets appended: {summary.get('finalized_buckets_appended')}")

    balance_cents = int(summary.get("current_balance_cents") or 0)
    appended = int(summary.get("finalized_buckets_appended") or 0)

    # 3. Threshold eval (with the D-106 dedupe guard on the balance escalations).
    escalations: list[dict[str, Any]] = []
    if balance_cents < BALANCE_BLOCKED_FLOOR_CENTS:
        escalations.append(dict(
            severity="blocked",
            summary=f"Balance critical -- only {summary.get('current_balance_pretty')} left.",
            see=[f"Current balance: {summary.get('current_balance_pretty')}"],
            do=["Top up the OpenAI account, then record the deposit via balance_auditor_record_deposit.py."],
            dedupe_prefix="balance_threshold:blocked", dedupe_cents=balance_cents,
        ))
    elif balance_cents < BALANCE_DECISION_FLOOR_CENTS:
        escalations.append(dict(
            severity="decision",
            summary=f"Balance approaching zero -- {summary.get('current_balance_pretty')} left.",
            see=[f"Current balance: {summary.get('current_balance_pretty')}",
                 f"Today's running spend (estimate): {summary.get('todays_running_pretty')}"],
            do=["Consider a top-up before workflows stall."],
            dedupe_prefix="balance_threshold:decision", dedupe_cents=balance_cents,
        ))

    # B-prime spend anomaly (today vs trailing-7d-avg x2, $1 floor). balance_check
    # already wrote the discrepancy_flagged ledger row; surface it same-day.
    anomaly = summary.get("spend_anomaly")
    if isinstance(anomaly, dict) and anomaly.get("anomaly_detected"):
        escalations.append(dict(
            severity="decision",
            summary=f"Today's spend is anomalous: {anomaly.get('todays_pretty')} "
                    f"(threshold {anomaly.get('threshold_pretty')})",
            see=[str(anomaly.get("reason")),
                 f"Trailing-{anomaly.get('trailing_days_observed')}d daily avg: {anomaly.get('trailing_avg_pretty')}",
                 f"Discrepancy ledger row id: {anomaly.get('discrepancy_row_id')}"],
            do=["Check what's running today -- a 4-hour meeting / a re-processing bug / a legitimate spike?",
                "If legitimate, ack and move on (the ledger captures it).",
                "If a bug, halt spend-bearing workflows + investigate the cause."],
        ))

    if appended > 0 and not escalations:
        _log(f"Appended {appended} new spend bucket(s); balance healthy. No escalation.")
    elif appended == 0 and not escalations:
        _log("No new spend buckets, balance healthy. Quiet day.")

    if dry_run:
        if escalations:
            _log(f"DRY RUN: would have fired {len(escalations)} escalation(s).")
    else:
        for esc in escalations:
            _escalate(esc["severity"], esc["summary"], see=esc.get("see"), do=esc.get("do"),
                      dedupe_prefix=esc.get("dedupe_prefix", ""), dedupe_cents=esc.get("dedupe_cents"))

    # 3b. Donations leg -- Buy Me a Coffee deposits (guarded; inert until the
    # operator pastes bmac_api_token into user_settings.json).
    bmac_args = ["--dry-run"] if dry_run else []
    bmac_stdout, _bmac_stderr, bmac_exit = _run_wrapper(
        "balance_auditor_bmac_check.py", bmac_args, label="bmac_check")
    if bmac_exit == 2:
        _log("BMAC leg inert (no bmac_api_token configured -- activates when the "
             "operator wires the Buy Me a Coffee account).")
    elif bmac_exit in (3, 4):
        _log(f"BMAC leg failed with exit {bmac_exit} (token IS configured).")
        if not dry_run:
            _escalate(
                "error", "Balance Auditor couldn't read the Buy Me a Coffee API",
                see=["A bmac_api_token is configured but the supporters fetch failed.",
                     "Exit 3 = transport/auth failure; exit 4 = response shape drift.",
                     "Check the ops/balance-auditor-logs/ run log for the raw error."],
                do=["If the token was rotated/revoked, paste the fresh one into user_settings.json.",
                    "If the API shape drifted, re-run scripts/balance_auditor_bmac_check.py --dry-run --verbose and review."],
            )
    elif bmac_exit == 0:
        try:
            bmac_summary: dict[str, Any] = json.loads(bmac_stdout[bmac_stdout.find("{"):])
        except (ValueError, json.JSONDecodeError):
            bmac_summary = {}
        new_deposits = int(bmac_summary.get("deposits_appended") or 0)
        pool_pretty = bmac_summary.get("donation_pool_pretty", "$?")
        _log(f"BMAC leg: {new_deposits} new deposit(s); donation pool {pool_pretty}.")
        if new_deposits > 0 and not dry_run:
            _escalate(
                "info",
                f"{new_deposits} new supporter deposit(s) landed -- donation pool now {pool_pretty}.",
                see=[f"Gross new support this run: {bmac_summary.get('gross_new_pretty', '$?')}",
                     "Amounts are GROSS (what supporters put in); BMAC fees deduct at payout."],
                do=["Nothing needed -- the ledger recorded them from the BMAC API.",
                    "At your next BMAC payout, record the fee delta via balance_auditor_record_deposit.py "
                    "(negative amount = manual_correction) so net stays reconcilable."],
            )
    else:
        _log(f"BMAC leg returned unexpected exit {bmac_exit}; continuing (leg is non-fatal).")

    # (Step 4, the daily press-TV infographic refresh, was removed with
    # its retired generation workflow — the refresh wrapper it invoked
    # was deleted per D-143, so the step could only log a failure. The
    # PressScreen placeholder marks where the Z-SPAN-native infographic
    # lands per D-136 / V1.5-Infographic-1.)

    _log("Balance Auditor heartbeat complete.")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Daily Balance Auditor orchestration (D-120, not metered).")
    ap.add_argument("--dry-run", action="store_true",
                    help="run balance_check --dry-run; evaluate but fire NO escalations / no refresh.")
    args = ap.parse_args(argv)
    stamp = _dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    try:
        return run(args.dry_run)
    finally:
        try:
            _LOGS_DIR.mkdir(parents=True, exist_ok=True)
            (_LOGS_DIR / f"{stamp}.log").write_text("\n".join(_LOG_LINES) + "\n", encoding="utf-8")
        except OSError:
            pass  # logging is best-effort; never fail the job on a log-write error


if __name__ == "__main__":
    sys.exit(main())
