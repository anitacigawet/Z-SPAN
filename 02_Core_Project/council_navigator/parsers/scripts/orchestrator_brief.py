#!/usr/bin/env python3.11
"""orchestrator_brief — compile the daily DM brief (D-073 / Stage B piece 2 chunk 2).

Cron-fired once daily (typically morning local). Compiles past-24h state
across the fleet into a prioritized plain-prose brief and posts it to
James's operator DM via `slack_notifier.send_dm_prose`.

Not an agent. This is a plain Python program that:
  - Reads the operation board directly (database imports + file reads +
    a lightweight Flask probe).
  - Composes the prose digest in code (deterministic, no LLM inference).
  - Calls send_dm_prose to deliver.

Why no LLM: the brief's shape is fixed (top + also + pace) and the data
is structured. An LLM in the loop would burn Opus tokens daily for a
templated synthesis — overkill. The orchestrator's full reasoning still
fires per the heartbeat / instructed paths; the brief is the digest.

Design choices (per James, 2026-05-30):
  - Cadence: once daily, morning (cron-set; the spawn script chooses time).
  - Content: prioritized — top 3 things to know first, then the rest.
  - Format: plain prose paragraphs with light *mrkdwn* emphasis for phone scanning.

Usage:
    python3.11 scripts/orchestrator_brief.py [--dry-run] [--window-hours N]

Options:
    --dry-run         Print the brief to stdout, do not post to Slack.
    --window-hours N  Lookback window for "past 24h" digestion. Default 24.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Make `parsers/` importable when this script is invoked from cwd=parsers/
# OR from the repo root (the spawn script can do either).
_THIS = Path(__file__).resolve()
_PARSERS_DIR = _THIS.parent.parent
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

logger = logging.getLogger("orchestrator_brief")

# Repo root for agent state-file reads.
_REPO_ROOT = _PARSERS_DIR.parents[1]
_AGENTS_DIR = _REPO_ROOT / "agents"

# Heuristic thresholds for prioritization.
_STUCK_PROCESSING_HOURS = 6           # WO in 'processing' longer than this -> top
_DISPUTED_BACKLOG_SECONDARY = 5       # below this, mention only if changed
_MEMORY_RECENT_LIMIT = 4              # at most this many recent memory entries cited


def log(msg: str) -> None:
    """Stderr log so transcript captures the brief's reasoning trace."""
    print(msg, file=sys.stderr, flush=True)


# ── Data collection ──────────────────────────────────────────────────


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _window_cutoff(hours: int) -> str:
    """ISO timestamp for `hours` ago (UTC). SQL CMP uses ISO lexicographic order."""
    return (_now_utc() - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")


def collect_escalations(window_hours: int) -> Dict[str, Any]:
    """Past-window escalations + the current unacked backlog."""
    out: Dict[str, Any] = {
        "unacked_count": 0,
        "unacked_by_severity": {},
        "recent_count": 0,
        "recent_examples": [],
        "error": None,
    }
    try:
        from database import (
            count_pending_escalations,
            list_pending_escalations,
            get_connection,
        )
    except Exception as e:
        out["error"] = f"escalation imports failed: {e}"
        return out

    try:
        out["unacked_count"] = count_pending_escalations(
            unacknowledged_only=True, undelivered_only=False,
        )
        unacked = list_pending_escalations(unacknowledged_only=True, limit=20)
        for row in unacked:
            sev = (row.get("severity") or "info").strip()
            out["unacked_by_severity"][sev] = out["unacked_by_severity"].get(sev, 0) + 1

        cutoff = _window_cutoff(window_hours)
        conn = get_connection()
        try:
            cur = conn.execute(
                """
                SELECT id, agent_role, severity, summary, created_at, acknowledged_at
                FROM pending_escalations
                WHERE created_at >= ?
                ORDER BY created_at DESC
                LIMIT 30
                """,
                (cutoff,),
            )
            rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
        out["recent_count"] = len(rows)
        out["recent_examples"] = rows[:5]
    except Exception as e:
        out["error"] = f"escalation reads failed: {e}"

    return out


def collect_badges() -> Dict[str, Any]:
    """Disputed quote count + Kingman vocab inbox count. Same source as
    /api/operator/badges but read directly (no Flask hop)."""
    out: Dict[str, Any] = {
        "disputed_count": 0,
        "vocab_pending_kingman": 0,
        "error": None,
    }
    try:
        from database import get_connection
    except Exception as e:
        out["error"] = f"badges imports failed: {e}"
        return out

    try:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT COUNT(*) AS n FROM quotes WHERE verified_status = 'disputed'"
            )
            out["disputed_count"] = int((cur.fetchone() or {"n": 0})["n"])
            cur = conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM city_vocabulary_corrections
                WHERE city_name = 'Kingman'
                  AND auto_apply = 1
                  AND promoted_at IS NULL
                """
            )
            out["vocab_pending_kingman"] = int((cur.fetchone() or {"n": 0})["n"])
        finally:
            conn.close()
    except Exception as e:
        out["error"] = f"badges reads failed: {e}"
    return out


def collect_work_orders(window_hours: int) -> Dict[str, Any]:
    """WO state counts + stuck-in-processing detection + recent failures.
    work_orders doesn't carry city_name/meeting_title columns — JOIN to
    meetings for the human-readable bits. Uses `started_at` (the actual
    column name) for the processing-start timestamp."""
    out: Dict[str, Any] = {
        "state_counts": {},
        "stuck_processing": [],
        "failed_recent": [],
        "error": None,
    }
    try:
        from database import get_connection
    except Exception as e:
        out["error"] = f"WO imports failed: {e}"
        return out

    try:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT state, COUNT(*) AS n FROM work_orders GROUP BY state"
            )
            for r in cur.fetchall():
                out["state_counts"][r["state"]] = r["n"]

            stuck_cutoff = (
                _now_utc() - dt.timedelta(hours=_STUCK_PROCESSING_HOURS)
            ).strftime("%Y-%m-%dT%H:%M:%S")
            cur = conn.execute(
                """
                SELECT wo.id, wo.meeting_id, m.city_name, m.meeting_title,
                       wo.started_at
                FROM work_orders wo
                LEFT JOIN meetings m ON m.id = wo.meeting_id
                WHERE wo.state = 'processing'
                  AND wo.started_at IS NOT NULL
                  AND wo.started_at < ?
                ORDER BY wo.started_at ASC
                LIMIT 5
                """,
                (stuck_cutoff,),
            )
            out["stuck_processing"] = [dict(r) for r in cur.fetchall()]

            recent_cutoff = _window_cutoff(window_hours)
            cur = conn.execute(
                """
                SELECT wo.id, wo.meeting_id, m.city_name, m.meeting_title,
                       wo.last_error, wo.completed_at, wo.started_at
                FROM work_orders wo
                LEFT JOIN meetings m ON m.id = wo.meeting_id
                WHERE wo.state IN ('failed', 'failed_truth_packet')
                  AND COALESCE(wo.completed_at, wo.started_at) >= ?
                ORDER BY COALESCE(wo.completed_at, wo.started_at) DESC
                LIMIT 5
                """,
                (recent_cutoff,),
            )
            out["failed_recent"] = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        out["error"] = f"WO reads failed: {e}"

    return out


def _load_calibration_from_disk() -> Dict[str, Any]:
    """Read the calibration block from orchestrator_autonomy.json with the
    same defaults Flask uses (videos_per_day, reviewers,
    reviews_per_reviewer_per_day, available_balance, cost_per_video,
    solvency_days). Inlined so the brief script doesn't need to import
    api_server (which would trigger Flask app init)."""
    defaults = {
        "videos_per_day": 1,
        "reviewers": 1,
        "reviews_per_reviewer_per_day": 1,
        "available_balance": None,
        "cost_per_video": None,
        "solvency_days": 30,
    }
    autonomy_path = _PARSERS_DIR / "orchestrator_autonomy.json"
    if not autonomy_path.is_file():
        return defaults
    try:
        data = json.loads(autonomy_path.read_text(encoding="utf-8"))
    except Exception:
        return defaults
    saved = (data.get("calibration") or {})
    merged = dict(defaults)
    merged.update({k: v for k, v in saved.items() if k in defaults})
    return merged


def collect_governor(city: str = "Kingman") -> Dict[str, Any]:
    """Ingestion governor state for the focus city.
    Reads the calibration block to compute compute_ceiling + review_ceiling,
    then calls ingestion_governor.compute_city_metering."""
    out: Dict[str, Any] = {"city": city, "snapshot": None, "error": None}
    try:
        from ingestion_governor import compute_city_metering
        cal = _load_calibration_from_disk()
        compute_ceiling = float(cal["videos_per_day"])
        review_ceiling = float(cal["reviewers"]) * float(cal["reviews_per_reviewer_per_day"])
        bal = cal.get("available_balance")
        cost = cal.get("cost_per_video")
        snapshot = compute_city_metering(
            city, compute_ceiling, review_ceiling,
            available_balance=(float(bal) if bal is not None else None),
            cost_per_video=(float(cost) if cost not in (None, 0, 0.0) else None),
            solvency_days=float(cal.get("solvency_days") or 30),
        )
        out["snapshot"] = snapshot
    except Exception as e:
        out["error"] = f"governor read failed: {e}"
    return out


def _read_json_file(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def collect_watchers() -> Dict[str, Any]:
    """Scout + Custodian state-file reads. Per orchestrator.md, an absent
    file means 'no signal yet', not an error."""
    out: Dict[str, Any] = {
        "scout_states": [],
        "parser_health": None,
        "error": None,
    }
    scout_dir = _AGENTS_DIR / "_scout_state"
    if scout_dir.is_dir():
        for entry in sorted(scout_dir.glob("*.json")):
            data = _read_json_file(entry)
            if data:
                data["_filename"] = entry.name
                out["scout_states"].append(data)

    custodian = _AGENTS_DIR / "_custodian_state" / "parser-health.json"
    out["parser_health"] = _read_json_file(custodian)
    return out


def collect_recent_memory(window_hours: int) -> Dict[str, Any]:
    """Per-agent memory entries with `created_at` in the past window.
    Reads each agent's memory dir + filters entries by frontmatter date."""
    out: Dict[str, Any] = {"entries": [], "error": None}
    cutoff = _window_cutoff(window_hours)
    agent_roles = [
        ("orchestrator", _AGENTS_DIR / "_orchestrator_memory"),
        ("disputed-quotes-reviewer", _AGENTS_DIR / "_dqr_memory"),
        ("vocabulary-curator", _AGENTS_DIR / "_curator_memory"),
        ("pipeline-operator", _AGENTS_DIR / "_pipeline_memory"),
        ("content-scout", _AGENTS_DIR / "_scout_memory"),
        ("parser-custodian", _AGENTS_DIR / "_custodian_memory"),
    ]
    for role, mdir in agent_roles:
        if not mdir.is_dir():
            continue
        for entry_path in mdir.glob("*.md"):
            if entry_path.name == "MEMORY.md":
                continue
            try:
                text = entry_path.read_text(encoding="utf-8")
            except Exception:
                continue
            # Naive frontmatter parse — find created_at line in the block.
            if not text.startswith("---"):
                continue
            try:
                fm_end = text.index("---", 3)
                fm = text[3:fm_end]
            except ValueError:
                continue
            created_at = ""
            kind = ""
            desc = ""
            for line in fm.splitlines():
                line = line.strip()
                if line.startswith("created_at:"):
                    created_at = line.split(":", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("type:"):
                    kind = line.split(":", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("description:"):
                    desc = line.split(":", 1)[1].strip().strip('"').strip("'")
            # Time-window filter — keep if created_at >= cutoff (string CMP is
            # safe for ISO timestamps).
            if created_at and created_at >= cutoff:
                out["entries"].append({
                    "role": role,
                    "slug": entry_path.stem,
                    "type": kind or "observation",
                    "description": desc,
                    "created_at": created_at,
                })
    out["entries"].sort(key=lambda e: e["created_at"], reverse=True)
    return out


def collect_brief_data(window_hours: int) -> Dict[str, Any]:
    """Top-level data collection — calls each source-specific collector."""
    return {
        "window_hours": window_hours,
        "generated_at": _now_utc().isoformat(),
        "escalations": collect_escalations(window_hours),
        "badges": collect_badges(),
        "work_orders": collect_work_orders(window_hours),
        "governor": collect_governor("Kingman"),
        "watchers": collect_watchers(),
        "memory": collect_recent_memory(window_hours),
    }


# ── Prioritization ───────────────────────────────────────────────────


def prioritize_items(data: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Return (top_items, secondary_items) as prose snippets ready for
    paragraph assembly. Top items go in the *Top:* line; secondary in
    the *Also:* line. Rules ordered from most-urgent."""
    top: List[str] = []
    secondary: List[str] = []

    esc = data.get("escalations", {})
    sev = esc.get("unacked_by_severity", {})
    blocked_n = sev.get("blocked", 0)
    decision_n = sev.get("decision", 0)
    error_n = sev.get("error", 0)

    # Rule 1: blocked-severity escalations -> always top.
    if blocked_n:
        s = "escalation" if blocked_n == 1 else "escalations"
        top.append(
            f"*{blocked_n} blocked {s}* still unacked — these are flagged "
            "as needing your hand before downstream lanes can move."
        )
    # Rule 2: decision-severity escalations -> top if 1+
    if decision_n:
        s = "decision" if decision_n == 1 else "decisions"
        top.append(
            f"*{decision_n} {s} waiting* — a sub-agent flagged something "
            "it can't resolve on its own; quickest path is the escalations "
            "inbox."
        )

    wo = data.get("work_orders", {})
    stuck = wo.get("stuck_processing") or []
    if stuck:
        first = stuck[0]
        title = first.get("meeting_title") or f"WO {first.get('id')}"
        top.append(
            f"*WO {first.get('id')}* ({first.get('city_name')} · {title}) has "
            f"been processing for >{_STUCK_PROCESSING_HOURS}h — likely stuck "
            "rather than truly failed; worker logs are the place to look."
        )

    failed = wo.get("failed_recent") or []
    if failed and len(failed) >= 2:
        top.append(
            f"*{len(failed)} work orders failed* in the past "
            f"{data['window_hours']}h. Look at last_error to triage; "
            "transient API hiccups vs. wedged notebooks split into "
            "different recovery paths."
        )
    elif failed:
        f0 = failed[0]
        secondary.append(
            f"One failure to note: WO {f0.get('id')} "
            f"({f0.get('city_name')}) — `{(f0.get('last_error') or '').strip()[:80]}`."
        )

    badges = data.get("badges", {})
    dq = badges.get("disputed_count", 0)
    vp = badges.get("vocab_pending_kingman", 0)
    if dq >= _DISPUTED_BACKLOG_SECONDARY:
        secondary.append(
            f"Disputed-quote backlog at *{dq}* — Reviewer has work queued."
        )
    elif dq:
        secondary.append(f"Disputed-quote backlog steady at {dq}.")
    if vp:
        secondary.append(f"Vocabulary inbox: {vp} Kingman correction(s) pending promotion.")

    watchers = data.get("watchers", {})
    parser_health = watchers.get("parser_health") or {}
    regressions = []
    if isinstance(parser_health, dict):
        for city, status in (parser_health.get("cities") or {}).items():
            if isinstance(status, dict) and status.get("regression_today"):
                regressions.append(city)
    if regressions:
        top.append(
            f"*Parser regression(s)* surfaced: {', '.join(regressions[:3])}. "
            "Custodian has the details — worth a maintainer pass."
        )

    new_meetings = []
    for ss in watchers.get("scout_states") or []:
        for m in (ss.get("new_meetings") or [])[:2]:
            title = m.get("meeting_title") or "meeting"
            date = m.get("meeting_date") or ""
            city = ss.get("city_name") or ss.get("city") or ""
            new_meetings.append(f"{city} {date}: {title}")
    if new_meetings:
        secondary.append(
            "New meetings detected: " + "; ".join(new_meetings[:3]) + "."
        )

    mem = data.get("memory", {})
    mem_entries = mem.get("entries") or []
    if mem_entries:
        # Lead with suggestions + insights; observations are noisier.
        priority = [e for e in mem_entries if e["type"] in ("suggestion", "insight")]
        rest = [e for e in mem_entries if e["type"] not in ("suggestion", "insight")]
        picked = (priority + rest)[:_MEMORY_RECENT_LIMIT]
        for e in picked:
            secondary.append(
                f"_{e['role']}_ ({e['type']}): {e.get('description') or e['slug']}."
            )

    # Error reporting — any source that failed gets a one-line secondary.
    for source_key in ("escalations", "badges", "work_orders", "governor"):
        src = data.get(source_key, {})
        if src.get("error"):
            secondary.append(
                f"_data source `{source_key}` failed to read_: "
                f"{src['error'][:120]}."
            )

    return top, secondary


# ── Rendering ────────────────────────────────────────────────────────


def _render_pace_line(data: Dict[str, Any]) -> str:
    """One sentence on ingestion governor + days-to-drain + rung.
    Snapshot shape comes from ingestion_governor.compute_city_metering:
      ceilings.{effective_per_day, bound_by}, today.{processed_today,
      room_today}, next_meeting, days_to_drain."""
    gov = data.get("governor", {})
    snap = gov.get("snapshot") or {}
    if not snap:
        if gov.get("error"):
            return f"_pace unknown — governor read failed: {gov['error'][:80]}_"
        return "_pace unknown — governor returned no snapshot._"

    ceilings = snap.get("ceilings") or {}
    today = snap.get("today") or {}
    eff = ceilings.get("effective_per_day")
    bound_by = ceilings.get("bound_by") or ""
    processed_today = today.get("processed_today")
    room_today = today.get("room_today")
    next_meeting = snap.get("next_meeting") or {}
    days_to_drain = snap.get("days_to_drain")

    pieces: List[str] = []
    if eff is not None:
        bound_clause = f" (bound by {bound_by})" if bound_by else ""
        pieces.append(f"effective rate *{eff}* meetings/day{bound_clause}")
    if processed_today is not None and room_today is not None:
        total = processed_today + room_today
        pieces.append(f"today: {processed_today} of {int(total)} processed")
    if next_meeting and next_meeting.get("meeting_id"):
        nm_title = (next_meeting.get("meeting_title") or "").strip()[:60]
        pieces.append(
            f"next ready: m{next_meeting['meeting_id']} ({nm_title})"
        )
    if days_to_drain is not None:
        pieces.append(f"days-to-drain: {days_to_drain}")
    pieces.append("orchestrator at *rung 1*")
    return ". ".join(pieces) + "."


def render_brief(data: Dict[str, Any]) -> str:
    """Compose the prose brief from the collected data + prioritization."""
    top, secondary = prioritize_items(data)

    now_local = _now_utc().astimezone()
    # Use Windows-safe strftime codes only (%-d / %-I are POSIX-only).
    # Cleanup: strip leading zero on day-of-month and hour for readability,
    # so "07" -> "7" without falling out of cross-platform support.
    try:
        raw_header = now_local.strftime("%a %b %d, %I%p %Z")
        # Tidy: " 0" -> " " collapses leading zeros after spaces.
        tidy = raw_header.replace(" 0", " ")
        header = f"*Daily brief* — {tidy}".rstrip()
    except Exception:
        header = f"*Daily brief* — {now_local.isoformat(timespec='minutes')}"

    paragraphs: List[str] = [header]

    if top:
        paragraphs.append("*Top:* " + " ".join(top[:3]))
    else:
        # No top-priority items -> the operation is quiet at the urgent layer.
        paragraphs.append(
            "*Top:* Operation is quiet overall — no blocked escalations, "
            "no stuck WOs, no parser regressions in the past "
            f"{data['window_hours']}h."
        )

    if secondary:
        paragraphs.append("*Also:* " + " ".join(secondary[:6]))

    paragraphs.append("*Pace:* " + _render_pace_line(data))

    # Soft footer — gives James a one-line anchor for "where did this come from"
    # without polluting the readable body.
    paragraphs.append(
        f"_brief covers past {data['window_hours']}h; "
        f"generated {data['generated_at'][:16]}Z._"
    )

    return "\n\n".join(paragraphs)


# ── Main ─────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compile + post the daily DM brief (D-073). "
            "Reads operation state directly (no Flask required) + posts "
            "via slack_notifier.send_dm_prose to James's operator DM."
        ),
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the brief to stdout; do not post.")
    parser.add_argument("--window-hours", type=int, default=24,
                        help="Lookback window for the digest (default 24).")
    args = parser.parse_args()

    log(f"=== orchestrator_brief --window-hours {args.window_hours} ===")
    log("collecting data...")
    try:
        data = collect_brief_data(args.window_hours)
    except Exception as e:
        log(f"FATAL: data collection raised: {type(e).__name__}: {e}")
        return 2

    log("rendering...")
    try:
        text = render_brief(data)
    except Exception as e:
        log(f"FATAL: render raised: {type(e).__name__}: {e}")
        return 3

    log(f"brief length: {len(text)} chars")

    if args.dry_run:
        print(text)
        log("dry-run: not posting to Slack.")
        return 0

    log("posting to operator DM...")
    try:
        from slack_notifier import send_dm_prose
    except Exception as e:
        log(f"FATAL: could not import slack_notifier: {e}")
        return 4

    result = send_dm_prose(text)
    if result.get("ok"):
        log(f"posted ok ts={result.get('ts')} channel={result.get('channel')}")
        return 0
    log(f"post FAILED: {result.get('error')}")
    return 5


if __name__ == "__main__":
    sys.exit(main())
