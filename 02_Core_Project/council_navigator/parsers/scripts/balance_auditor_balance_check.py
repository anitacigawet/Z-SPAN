#!/usr/bin/env python3.11
"""balance_auditor_balance_check -- fetch OpenAI usage + reconcile the ledger.

V1 heartbeat task for the Balance Auditor (S-004-class watcher). Run daily
via ops/balance-auditor-heartbeat.ps1. Idempotent: re-running same day is
a no-op (the UNIQUE constraint on balance_ledger blocks duplicate spend
buckets).

Behavior:
  1. Read openai_usage_key from user_settings.json (NOT openai_api_key --
     that's the inference key, wrong scope). If missing, fail-fast with
     a clear "James needs to create the restricted key" message.
  2. Compute fetch window: from max(last_bucket_end, now - 30d) to now.
  3. Call /v1/organization/costs?bucket_width=1d.
  4. For each FINALIZED bucket (bucket_end_time < now): append a
     spend_observed row via INSERT OR IGNORE.
  5. For today's running bucket: compute the in-progress total + return
     it as today_estimate (not persisted -- avoids double-counting when
     tomorrow's heartbeat records the same day as finalized).
  6. Compute current balance from the ledger.
  7. Append an api_balance_snapshot row stamping current state.
  8. Print a JSON summary the heartbeat ps1 can parse.

The wrapper layer + role hardcoding follow D-066. Operator surfaces
read it via the JSON output; nothing inside this script edits the
operator-set inference key or makes spend-bearing API calls.

Usage:
    python3.11 scripts/balance_auditor_balance_check.py
    python3.11 scripts/balance_auditor_balance_check.py --dry-run
    python3.11 scripts/balance_auditor_balance_check.py --since-days 7

Exit codes:
    0: success (balance check ran, ledger updated as needed)
    2: openai_usage_key not configured (operator action required)
    3: OpenAI API call failed (transient or auth issue)
    4: ledger write failed
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Make parsers/ importable when invoked from cwd=parsers/.
_PARSERS_DIR = Path(__file__).resolve().parent.parent
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

from database import (  # noqa: E402
    append_ledger_event,
    get_current_balance,
    get_latest_spend_bucket_end,
    get_trailing_spend_observed,
)

PROVIDER = "openai"
USAGE_KEY_FIELD = "openai_usage_key"
# Optional list of project_ids in user_settings.json — when present + non-empty,
# the auditor filters /v1/organization/costs results client-side to only sum
# spend belonging to these projects. Without the filter, the auditor sums ALL
# org spend, which mixes other projects' spend into the Z-SPAN balance picture
# (a real bug surfaced in the 2026-05-30 brainstorm). Falls back to summing
# everything if unset (backward-compat for first-run cold start).
PROJECT_IDS_FIELD = "openai_project_ids"
USER_SETTINGS_PATH = _PARSERS_DIR / "user_settings.json"
OPENAI_COSTS_URL = "https://api.openai.com/v1/organization/costs"

# Per agents/balance-auditor.md hard rules.
PER_CALL_TIMEOUT_S = 15
PER_SESSION_API_CALL_CEILING = 5  # for V1, we only ever make 1-2; defensive


def _load_usage_key() -> str | None:
    """Read the restricted-scope openai_usage_key from user_settings.json.

    Returns None if missing OR present-but-empty. Never reads the main
    openai_api_key field -- that's the inference key, wrong scope, and
    the auditor's structural wall explicitly excludes it.
    """
    if not USER_SETTINGS_PATH.is_file():
        return None
    try:
        with USER_SETTINGS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    val = data.get(USAGE_KEY_FIELD)
    if not val or not isinstance(val, str):
        return None
    return val.strip() or None


def _load_project_ids_filter() -> set[str] | None:
    """Read the optional openai_project_ids list from user_settings.json.

    Returns:
      - set of project IDs (e.g., {"proj_yVI2LPRWMAc4vhLTNalMWEQU"}) when configured
      - None when not configured -> sum ALL org spend (legacy / cold-start behavior)

    The filter is applied CLIENT-SIDE in _process_buckets: each result has
    a project_id field; results not in the configured set are skipped.
    """
    if not USER_SETTINGS_PATH.is_file():
        return None
    try:
        with USER_SETTINGS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    val = data.get(PROJECT_IDS_FIELD)
    if not val:
        return None
    if isinstance(val, str):
        # Allow a single string for convenience.
        val = [val]
    if not isinstance(val, list):
        return None
    cleaned = {p.strip() for p in val if isinstance(p, str) and p.strip()}
    return cleaned or None


def _http_get(url: str, key: str) -> dict[str, Any]:
    """Minimal urllib-based GET with bearer auth. Returns parsed JSON.

    No third-party deps (requests / openai SDK) -- the auditor stays
    on the stdlib so it's lighter to audit + doesn't pull a wider
    dependency surface than necessary.
    """
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=PER_CALL_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_costs(key: str, start_time: int, end_time: int) -> dict[str, Any]:
    """Call /v1/organization/costs for the given window. Returns the
    raw API response dict (caller does the bucket processing).

    Errors propagate as urllib.error.HTTPError; caller distinguishes
    403 (scope issue) from 5xx (service issue) and emits appropriate
    operator messaging.
    """
    params = urllib.parse.urlencode({
        "start_time": str(start_time),
        "end_time": str(end_time),
        "bucket_width": "1d",
        "limit": "31",
    })
    url = f"{OPENAI_COSTS_URL}?{params}"
    return _http_get(url, key)


def _process_buckets(
    response: dict[str, Any],
    now_unix: int,
    dry_run: bool,
    project_ids_filter: set[str] | None = None,
) -> tuple[int, int, int, dict[str, Any]]:
    """Walk the API response's buckets. For each FINALIZED bucket
    (end_time < now), append a spend_observed row. For the running
    bucket, compute the in-progress estimate.

    When project_ids_filter is non-None, only sum results whose
    project_id is in the filter set -- other-project spend is excluded
    from the balance picture (typical case: the auditor tracks Z-SPAN's
    project balance, not the whole org's). When None, sum everything
    (cold-start / no-filter behavior, preserves legacy).

    Returns (finalized_appended, finalized_skipped_as_dup, todays_cents,
    detail_dict).
    """
    appended = 0
    skipped_dup = 0
    todays_cents = 0
    detail = {"finalized_buckets": [], "todays_bucket": None}

    for bucket in response.get("data", []):
        if bucket.get("object") != "bucket":
            continue
        bucket_start = bucket.get("start_time")
        bucket_end = bucket.get("end_time")
        if not isinstance(bucket_start, int) or not isinstance(bucket_end, int):
            continue

        # Sum line items. The API's result entries each have {amount: {value, currency}}.
        # Each result also has project_id (when org has multiple projects). When a
        # project filter is configured, skip results outside the allowed set.
        total_cents = 0
        line_items: list[str] = []
        currency = "usd"
        skipped_for_other_projects = 0
        for result in bucket.get("results", []):
            result_project = result.get("project_id")
            if project_ids_filter is not None and result_project not in project_ids_filter:
                skipped_for_other_projects += 1
                continue
            amt = result.get("amount") or {}
            value = amt.get("value")
            cur = amt.get("currency", "usd")
            if not isinstance(value, (int, float)):
                continue
            currency = cur
            total_cents += int(round(value * 100))
            line = result.get("line_item") or "<unspecified>"
            line_items.append(f"{line}: ${value:.4f}")

        bucket_info = {
            "start_time": bucket_start,
            "end_time": bucket_end,
            "total_cents": total_cents,
            "line_items": line_items,
            "skipped_for_other_projects": skipped_for_other_projects,
        }

        if bucket_end < now_unix:
            # Finalized -- safe to write.
            if dry_run:
                appended += 1
                detail["finalized_buckets"].append(bucket_info)
                continue
            # Bake project filter context into notes so a future audit can
            # tell which projects a row covers without re-querying the API.
            note_parts: list[str] = []
            if project_ids_filter is not None:
                note_parts.append(
                    f"projects={','.join(sorted(project_ids_filter))}"
                )
            elif skipped_for_other_projects == 0:
                note_parts.append("projects=ALL")
            if line_items:
                note_parts.extend(line_items)
            row_id = append_ledger_event(
                provider=PROVIDER,
                event_type="spend_observed",
                amount_cents=total_cents,
                currency=currency,
                bucket_start_time=bucket_start,
                bucket_end_time=bucket_end,
                source="openai_billing_api",
                notes="; ".join(note_parts) if note_parts else None,
            )
            if row_id is None:
                skipped_dup += 1
            else:
                appended += 1
                detail["finalized_buckets"].append(bucket_info)
        else:
            # Running bucket (today). Estimate only -- not persisted.
            todays_cents = total_cents
            detail["todays_bucket"] = bucket_info

    return appended, skipped_dup, todays_cents, detail


def _format_money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:.2f}"


# B-prime (James 2026-05-31): discrepancy reframe. The original "$0.50
# observed-vs-expected drift" check in agents/balance-auditor.md was a
# reconciliation concept that doesn't map cleanly to today's data flow
# (we don't have an independent "expected" balance to compare against —
# the auditor's computed balance IS the ground truth). James's reframe:
# detect today's in-progress spend SPIKES vs the trailing-7d average,
# with a $1 floor to suppress noise at near-zero baselines. Operates on
# today's in-progress bucket (NOT yesterday's finalized) so the
# orchestrator can alert same-day rather than next-morning.
SPEND_ANOMALY_TRAILING_DAYS = 7
SPEND_ANOMALY_MULTIPLIER = 2.0
SPEND_ANOMALY_FLOOR_CENTS = 100  # $1.00 — below this the absolute amount is too small to bother alerting


def _check_today_spend_anomaly(todays_cents: int) -> dict[str, Any]:
    """Compare today's in-progress spend to the trailing-7d daily average.

    Returns a dict suitable for inclusion in the balance_check JSON
    summary. The heartbeat .ps1 reads `anomaly_detected` + the
    humanized `reason` and decides whether to escalate.

    Logic per James 2026-05-31 spec:
      threshold = max($SPEND_ANOMALY_FLOOR_CENTS, multiplier * trailing_avg)
      anomaly  = todays_cents > threshold

    Cold-start safety: if no trailing data exists yet (auditor hasn't
    run long enough to accumulate ≥1 finalized spend bucket), we suppress
    the anomaly signal entirely — there's no baseline to compare against.
    """
    recent = get_trailing_spend_observed(
        provider=PROVIDER, days=SPEND_ANOMALY_TRAILING_DAYS,
    )
    if not recent:
        return {
            "anomaly_detected": False,
            "reason": "no trailing spend data yet (auditor cold-start); skipping anomaly check",
            "trailing_days_observed": 0,
            "trailing_avg_cents": 0,
            "threshold_cents": SPEND_ANOMALY_FLOOR_CENTS,
            "threshold_pretty": _format_money(SPEND_ANOMALY_FLOOR_CENTS),
            "todays_cents": todays_cents,
            "todays_pretty": _format_money(todays_cents),
            "multiplier": SPEND_ANOMALY_MULTIPLIER,
            "floor_cents": SPEND_ANOMALY_FLOOR_CENTS,
        }

    total = sum(int(r.get("amount_cents") or 0) for r in recent)
    avg = total // len(recent)
    threshold = max(SPEND_ANOMALY_FLOOR_CENTS, int(SPEND_ANOMALY_MULTIPLIER * avg))
    anomaly = todays_cents > threshold

    if anomaly:
        reason = (
            f"today's running spend {_format_money(todays_cents)} exceeds "
            f"threshold {_format_money(threshold)} "
            f"({SPEND_ANOMALY_MULTIPLIER}x trailing-{len(recent)}d-avg "
            f"{_format_money(avg)}, floor {_format_money(SPEND_ANOMALY_FLOOR_CENTS)})"
        )
    else:
        reason = (
            f"today's running spend {_format_money(todays_cents)} within "
            f"threshold {_format_money(threshold)} "
            f"({SPEND_ANOMALY_MULTIPLIER}x trailing-{len(recent)}d-avg "
            f"{_format_money(avg)}, floor {_format_money(SPEND_ANOMALY_FLOOR_CENTS)})"
        )

    return {
        "anomaly_detected": anomaly,
        "reason": reason,
        "trailing_days_observed": len(recent),
        "trailing_avg_cents": avg,
        "trailing_avg_pretty": _format_money(avg),
        "threshold_cents": threshold,
        "threshold_pretty": _format_money(threshold),
        "todays_cents": todays_cents,
        "todays_pretty": _format_money(todays_cents),
        "multiplier": SPEND_ANOMALY_MULTIPLIER,
        "floor_cents": SPEND_ANOMALY_FLOOR_CENTS,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch OpenAI usage + reconcile the balance_ledger "
            "(Balance Auditor heartbeat task)."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run the fetch but DON'T write to balance_ledger. Useful for "
             "the first-time smoke test before any deposits exist.",
    )
    parser.add_argument(
        "--since-days", type=int, default=None,
        help="Force the fetch window to look back N days instead of the "
             "default (from latest spend_observed bucket, or 30d for cold "
             "start). Useful for backfill or debugging.",
    )
    args = parser.parse_args()

    key = _load_usage_key()
    if not key:
        print(
            f"DENIED: '{USAGE_KEY_FIELD}' not configured in user_settings.json.\n"
            f"  The Balance Auditor needs a RESTRICTED OpenAI project key with "
            f"`api.usage.read` scope (separate from the inference key).\n"
            f"  Create one in the OpenAI dashboard -> API keys -> "
            f"Create new secret key -> Restricted -> add `api.usage.read` scope.\n"
            f"  Then add it to user_settings.json as: \"{USAGE_KEY_FIELD}\": \"sk-...\".",
            file=sys.stderr,
        )
        return 2

    now_unix = int(time.time())

    # Compute start_time: from latest bucket_end (resume), or N days ago.
    if args.since_days is not None:
        start_time = now_unix - (args.since_days * 86400)
        window_source = f"--since-days {args.since_days}"
    else:
        latest_end = get_latest_spend_bucket_end(PROVIDER)
        if latest_end is None:
            # Cold start: look back 30 days.
            start_time = now_unix - (30 * 86400)
            window_source = "cold start (30d lookback)"
        else:
            # Resume from where we left off.
            start_time = latest_end
            window_source = f"resume from {datetime.fromtimestamp(latest_end, tz=timezone.utc).date().isoformat()}"

    # Defensive: never fetch more than 31 days in one call (matches bucket limit).
    max_window = 31 * 86400
    if now_unix - start_time > max_window:
        start_time = now_unix - max_window
        window_source += " (clamped to 31d)"

    # OpenAI's /v1/organization/costs is date-granular (not timestamp). When
    # start and end fall on the same UTC date the API returns 400
    # "end_date must come after start_date". With bucket_width=1d we also
    # can't get any new buckets out of a sub-day window. Skip the call.
    window_seconds = now_unix - start_time
    if window_seconds < 86400:
        # Still compute current balance + (optionally) write a no-op snapshot
        # so the heartbeat trail shows the auditor ran today even on no-data days.
        project_ids_filter = _load_project_ids_filter()
        current_balance_cents = get_current_balance(PROVIDER)
        project_filter_str = (
            ",".join(sorted(project_ids_filter)) if project_ids_filter else "ALL"
        )
        snapshot_id = None
        if not args.dry_run:
            snapshot_id = append_ledger_event(
                provider=PROVIDER,
                event_type="api_balance_snapshot",
                amount_cents=None,
                running_balance_cents=current_balance_cents,
                source="auditor_reconciler",
                notes=(
                    f"projects={project_filter_str}, "
                    f"window_too_small (window={window_seconds}s, "
                    f"need >=86400s for new buckets) — skipped API call"
                ),
            )
        summary = {
            "ok": True,
            "provider": PROVIDER,
            "project_ids_filter": sorted(project_ids_filter) if project_ids_filter else None,
            "current_balance_cents": current_balance_cents,
            "current_balance_pretty": _format_money(current_balance_cents),
            "finalized_buckets_appended": 0,
            "finalized_buckets_skipped_dup": 0,
            "todays_running_cents": 0,
            "todays_running_pretty": "$0.00",
            "snapshot_row_id": snapshot_id,
            "spend_anomaly": {
                "anomaly_detected": False,
                "reason": "skipped (sub-day fetch window; no fresh today bucket data)",
                "trailing_days_observed": 0,
                "trailing_avg_cents": 0,
                "threshold_cents": SPEND_ANOMALY_FLOOR_CENTS,
                "threshold_pretty": _format_money(SPEND_ANOMALY_FLOOR_CENTS),
                "todays_cents": 0,
                "todays_pretty": "$0.00",
                "multiplier": SPEND_ANOMALY_MULTIPLIER,
                "floor_cents": SPEND_ANOMALY_FLOOR_CENTS,
            },
            "dry_run": args.dry_run,
            "window_source": window_source,
            "skipped_reason": (
                f"window_too_small ({window_seconds}s; need >=86400s "
                f"for /v1/organization/costs date-granular bucket fetch)"
            ),
            "buckets_detail": None,
        }
        print(json.dumps(summary, indent=2))
        return 0

    print(
        f"balance_check: fetching OpenAI costs "
        f"start={datetime.fromtimestamp(start_time, tz=timezone.utc).isoformat()} "
        f"end=now ({window_source})"
    )

    try:
        response = _fetch_costs(key, start_time, now_unix)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        if e.code == 403:
            print(
                f"ERROR: OpenAI returned 403 -- key lacks `api.usage.read` scope "
                f"or is the wrong type.\n"
                f"  Response: {body}\n"
                f"  Fix: regenerate the key with the restricted scope, then "
                f"re-run. See agents/balance-auditor.md.",
                file=sys.stderr,
            )
        else:
            print(
                f"ERROR: OpenAI HTTP {e.code} on /v1/organization/costs.\n"
                f"  Response: {body}",
                file=sys.stderr,
            )
        return 3
    except urllib.error.URLError as e:
        print(f"ERROR: network failure: {e.reason}", file=sys.stderr)
        return 3
    except Exception as e:
        print(f"ERROR: unexpected failure during /v1/organization/costs: {e}", file=sys.stderr)
        return 3

    project_ids_filter = _load_project_ids_filter()
    appended, skipped_dup, todays_cents, detail = _process_buckets(
        response, now_unix, args.dry_run, project_ids_filter=project_ids_filter
    )

    # B-prime: spend anomaly check (James 2026-05-31).
    # Compares today's in-progress spend to the trailing-7d average; writes
    # a discrepancy_flagged ledger row when threshold exceeded so the
    # heartbeat .ps1 can pick it up + escalate same-day.
    spend_anomaly = _check_today_spend_anomaly(todays_cents)
    discrepancy_row_id = None
    if spend_anomaly["anomaly_detected"] and not args.dry_run:
        discrepancy_row_id = append_ledger_event(
            provider=PROVIDER,
            event_type="discrepancy_flagged",
            amount_cents=todays_cents,
            source="auditor_reconciler",
            notes=spend_anomaly["reason"],
        )
        spend_anomaly["discrepancy_row_id"] = discrepancy_row_id

    # Compute current balance + append snapshot row.
    current_balance_cents = get_current_balance(PROVIDER)
    snapshot_id = None
    project_filter_str = (
        ",".join(sorted(project_ids_filter)) if project_ids_filter else "ALL"
    )
    if not args.dry_run:
        snapshot_id = append_ledger_event(
            provider=PROVIDER,
            event_type="api_balance_snapshot",
            amount_cents=None,
            running_balance_cents=current_balance_cents,
            source="auditor_reconciler",
            notes=(
                f"projects={project_filter_str}, "
                f"finalized_buckets_appended={appended}, "
                f"finalized_buckets_skipped_dup={skipped_dup}, "
                f"todays_running_cents={todays_cents}, "
                f"spend_anomaly={spend_anomaly['anomaly_detected']}"
            ),
        )

    summary = {
        "ok": True,
        "provider": PROVIDER,
        "project_ids_filter": sorted(project_ids_filter) if project_ids_filter else None,
        "current_balance_cents": current_balance_cents,
        "current_balance_pretty": _format_money(current_balance_cents),
        "finalized_buckets_appended": appended,
        "finalized_buckets_skipped_dup": skipped_dup,
        "todays_running_cents": todays_cents,
        "todays_running_pretty": _format_money(todays_cents),
        "snapshot_row_id": snapshot_id,
        "spend_anomaly": spend_anomaly,
        "dry_run": args.dry_run,
        "window_source": window_source,
        "buckets_detail": detail if args.dry_run else None,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
