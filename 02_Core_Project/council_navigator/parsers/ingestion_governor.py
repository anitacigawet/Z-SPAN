"""
S-010 step 2 — the metering governor (read-only rate/progress computation).

The low-hum ingestion machine (FUTURE_THOUGHTS.md § S-010) flows the civic
record at a deliberately-sustainable cadence, not a firehose and not a trickle.
Its effective daily rate is:

    effective_rate = min(compute_ceiling, review_ceiling)

  - compute_ceiling = videos/day the pipeline tools (synthesis stack +
    Gemini-Pro verification) tolerate. Empirical, unpublished — James widens it
    on the autonomy gate-board (D-061) as clean cycles prove the rate is safe.
  - review_ceiling = videos/day a human will light-final-pass (S-011). Starts at
    1 (James, sole reviewer) and is the SUM of all active reviewers' throughput,
    so the machine breathes wider automatically as reviewers are added.

This module answers, per city: how many meetings are processed, how many are
candidates still to flow (the rolling window forward — NOT the skipped-too-old
archive, per Option A), how much room is left under today's ceiling, what the
next meeting to process is, and how many days the current candidates take to
drain at the effective rate. That last figure is the per-city seed of the
"days-per-state estimate."

What this module deliberately does NOT do: spend money, trigger any agent, touch
the bridge, or write any state. It is pure read-only computation over DB facts +
the two ceilings — exactly the rung-1 "read the board" surface the orchestrator
(S-007) consumes. The ceilings come from the caller (api_server reads them from
the gate-board calibration); the governor stays free of file/IO coupling so it
is trivially testable.

CLI (dev):
    cd 02_Core_Project/council_navigator/parsers
    python3.11 ingestion_governor.py --city Kingman --compute 1 --review 1
"""
from __future__ import annotations

import argparse
import math
import sys
from typing import Optional

from database import get_connection

# Work-order state → what it means for ingestion flow. Kept explicit so the
# governor's bucketing is transparent and survives new states being added.
PROCESSED_STATES = {"completed"}
READY_STATES = {"pending"}            # has a video URL; processable now
NEEDS_URL_STATES = {"awaiting_video"}  # needs James to add a YouTube URL first
EXCLUDED_STATES = {"skipped_too_old"}  # Option A: NOT the backlog; never auto-flowed
# Anything else (processing, failed, awaiting_notebook, ...) lands in `other`.


def _state_counts(cur, city: str) -> dict[str, int]:
    rows = cur.execute(
        """
        SELECT w.state AS state, COUNT(*) AS c
        FROM work_orders w JOIN meetings m ON m.id = w.meeting_id
        WHERE m.city_name = ?
        GROUP BY w.state
        """,
        (city,),
    ).fetchall()
    return {r["state"]: r["c"] for r in rows}


def _processed_today(cur, city: str) -> int:
    """Completed work orders whose completed_at falls on the current (UTC) date.

    The daily ceiling is measured against the DB's date('now') (UTC) — a minor
    boundary detail at 1/day; refine to local time if it ever matters."""
    row = cur.execute(
        """
        SELECT COUNT(*) AS c
        FROM work_orders w JOIN meetings m ON m.id = w.meeting_id
        WHERE m.city_name = ? AND w.state = 'completed'
          AND w.completed_at IS NOT NULL
          AND date(w.completed_at) = date('now')
        """,
        (city,),
    ).fetchone()
    return row["c"] if row else 0


def _next_ready_meeting(cur, city: str) -> Optional[dict]:
    """The next meeting ready to process — freshness-first (newest meeting_date).

    Under Option A the machine keeps each jurisdiction fresh, so it processes the
    most recent ready meeting first rather than draining oldest-first."""
    row = cur.execute(
        """
        SELECT m.id AS meeting_id, m.meeting_date, m.meeting_title
        FROM work_orders w JOIN meetings m ON m.id = w.meeting_id
        WHERE m.city_name = ? AND w.state = 'pending'
        ORDER BY m.meeting_date DESC, m.id DESC
        LIMIT 1
        """,
        (city,),
    ).fetchone()
    if not row:
        return None
    return {
        "meeting_id": row["meeting_id"],
        "meeting_date": row["meeting_date"],
        "meeting_title": row["meeting_title"],
    }


def budget_ceiling_per_day(available_balance, cost_per_video, solvency_days) -> Optional[float]:
    """Videos/day the balance supports without draining faster than the solvency
    window: balance / (cost_per_video * solvency_days). Returns None when the
    budget isn't configured (no balance / no cost) — then it simply doesn't bind.

    Units check: $ / ($/video * days) = videos/day. By construction, when this is
    the binding ceiling the runway equals solvency_days exactly (see runway_days)."""
    if available_balance is None or not cost_per_video or cost_per_video <= 0 \
            or not solvency_days or solvency_days <= 0:
        return None
    return float(available_balance) / (float(cost_per_video) * float(solvency_days))


def runway_days(available_balance, cost_per_video, effective_rate) -> Optional[float]:
    """Days until the balance depletes at the effective daily rate:
    balance / (effective_rate * cost_per_video). None when not computable.

    When the budget ceiling binds, this is exactly solvency_days; when compute or
    review binds (a slower rate), the balance lasts longer, so runway > solvency."""
    if available_balance is None or not cost_per_video or cost_per_video <= 0 \
            or not effective_rate or effective_rate <= 0:
        return None
    return float(available_balance) / (float(effective_rate) * float(cost_per_video))


def compute_city_metering(
    city: str,
    compute_ceiling: float,
    review_ceiling: float,
    conn=None,
    *,
    available_balance: Optional[float] = None,
    cost_per_video: Optional[float] = None,
    solvency_days: float = 30.0,
) -> dict:
    """Compute the ingestion-metering state for one city. Read-only.

    Returns the rate/progress board the orchestrator reads to decide whether the
    operation is under today's ceiling and what (if anything) is next to flow.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        cur = conn.cursor()
        counts = _state_counts(cur, city)

        processed = sum(counts.get(s, 0) for s in PROCESSED_STATES)
        ready = sum(counts.get(s, 0) for s in READY_STATES)
        needs_url = sum(counts.get(s, 0) for s in NEEDS_URL_STATES)
        excluded = sum(counts.get(s, 0) for s in EXCLUDED_STATES)
        known = PROCESSED_STATES | READY_STATES | NEEDS_URL_STATES | EXCLUDED_STATES
        other = {s: c for s, c in counts.items() if s not in known}

        # Candidates = the rolling-window-forward work that should flow. Excludes
        # the skipped-too-old archive (Option A) and the already-done.
        candidate_unprocessed = ready + needs_url

        budget_per_day = budget_ceiling_per_day(
            available_balance, cost_per_video, solvency_days
        )
        ceilings = {"compute": compute_ceiling, "review": review_ceiling}
        if budget_per_day is not None:
            ceilings["budget"] = budget_per_day
        effective_rate = min(ceilings.values())
        bound_by = "+".join(sorted(n for n, c in ceilings.items() if c == effective_rate))
        rway = runway_days(available_balance, cost_per_video, effective_rate)
        processed_today = _processed_today(cur, city)
        room_today = max(0.0, effective_rate - processed_today)
        under_ceiling = room_today > 0

        days_to_drain = (
            math.ceil(candidate_unprocessed / effective_rate)
            if effective_rate > 0 and candidate_unprocessed > 0
            else (0 if candidate_unprocessed == 0 else None)
        )

        next_meeting = _next_ready_meeting(cur, city)

        return {
            "city": city,
            "ceilings": {
                "compute_per_day": compute_ceiling,
                "review_per_day": review_ceiling,
                "budget_per_day": budget_per_day,
                "effective_per_day": effective_rate,
                "bound_by": bound_by,
            },
            "budget": {
                "configured": budget_per_day is not None,
                "available_balance": available_balance,
                "cost_per_video": cost_per_video,
                "solvency_days": solvency_days,
                "budget_per_day": budget_per_day,
                "runway_days": rway,
            },
            "progress": {
                "processed": processed,
                "ready_to_process": ready,
                "needs_video_url": needs_url,
                "candidate_unprocessed": candidate_unprocessed,
                "excluded_too_old": excluded,
                "other": other,
            },
            "today": {
                "processed_today": processed_today,
                "room_today": room_today,
                "under_ceiling": under_ceiling,
            },
            "next_meeting": next_meeting,
            "days_to_drain": days_to_drain,
        }
    finally:
        if own_conn:
            conn.close()


def _format(state: dict) -> str:
    c = state["ceilings"]
    p = state["progress"]
    t = state["today"]
    nm = state["next_meeting"]
    lines = [
        f"S-010 governor — {state['city']}",
        f"  ceilings: compute {c['compute_per_day']}/day · review {c['review_per_day']}/day "
        f"→ effective {c['effective_per_day']}/day (bound by {c['bound_by']})",
        f"  progress: {p['processed']} processed · {p['ready_to_process']} ready · "
        f"{p['needs_video_url']} need a video URL · {p['candidate_unprocessed']} candidates "
        f"({p['excluded_too_old']} too-old excluded)",
        f"  today:    {t['processed_today']} processed · room for {t['room_today']:g} more "
        f"· {'UNDER ceiling' if t['under_ceiling'] else 'AT ceiling'}",
        f"  drain:    {state['days_to_drain']} day(s) to clear current candidates"
        if state["days_to_drain"] is not None
        else "  drain:    — (rate is 0/day)",
    ]
    b = state["budget"]
    if b["configured"]:
        rw = b["runway_days"]
        lines.append(
            f"  budget:   ${b['available_balance']:g} balance · ${b['cost_per_video']:g}/video "
            f"· {b['solvency_days']:g}-day solvency → ceiling {b['budget_per_day']:.2f}/day"
            + (f" · ~{rw:.0f} days runway at this rate" if rw is not None else "")
        )
    if nm:
        lines.append(f"  next:     m{nm['meeting_id']} {nm['meeting_date']} — {nm['meeting_title']}")
    else:
        lines.append("  next:     (nothing ready to process)")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--city", required=True, help="City to meter (e.g., 'Kingman').")
    parser.add_argument("--compute", type=float, default=1.0, help="Compute ceiling (videos/day). Default 1.")
    parser.add_argument("--review", type=float, default=1.0, help="Review ceiling (videos/day). Default 1.")
    parser.add_argument("--balance", type=float, default=None, help="Available balance ($) for the budget ceiling. Omit to leave budget unconfigured.")
    parser.add_argument("--cost-per-video", type=float, default=None, help="Conservative $/video (Whisper + cents). Omit to leave budget unconfigured.")
    parser.add_argument("--solvency-days", type=float, default=30.0, help="Solvency window in days (default 30).")
    args = parser.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    state = compute_city_metering(
        args.city, args.compute, args.review,
        available_balance=args.balance, cost_per_video=args.cost_per_video,
        solvency_days=args.solvency_days,
    )
    print(_format(state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
