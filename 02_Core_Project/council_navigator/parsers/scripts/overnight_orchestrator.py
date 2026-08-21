#!/usr/bin/env python3.11
"""overnight_orchestrator — Z-SPAN autonomous overnight processor.

Per D-138 + [[three-shift-autonomous-processing-pattern]] + operator
authorization 2026-06-25 (first unsupervised overnight run, Maricopa-only
scope for blast-radius caution; expands to remaining 7 AZ counties if
clean).

Six phases sequential (failures in one don't kill the next; orchestrator
reports per-phase verdict + final summary in audit log):

  A — Maricopa parser scrape sweep (populate meetings cache)
  B — POST /api/work-orders/scan (enqueue WOs from newly-cached meetings)
  C — haiku_match_videos.py for each YT-channel-registered Maricopa city
      (auto-promote awaiting_video → pending on high-confidence matches)
  D — Monitor worker daemon's V1-RAG-3 + sidecar drain (passive; the
      launchd-managed worker @ PID 517 autonomously processes pending
      WOs per its D-005 60s defrag interval)
  E — Cleanup mechanicals (check_alignment_health --fix; temp-test-batch
      revert SQL; older-Kingman sidecar backfill)
  F — Generate audit log + final summary

Insanity-check stops only (per operator 2026-06-25 — the run must not be
interrupted by ordinary hard caps, but obvious gone-wrong maximums stay
in force — subscription via MAX, Anthropic's 5-hour rolling cap
is the rate-limit; no per-token / per-meeting / per-spend behavior caps):

  - Per-subprocess 75-min wall-clock timeout (single hang signal)
  - 5 consecutive subprocess failures within a phase (cascading signal)
  - Cumulative error rate >50% across all subprocess calls (structural)
  - Whisper-intermediates dir >50GB accumulated (disk-fill signal)
  - Anthropic rate-limit error detected → log + exit cleanly

Audit log: ~/Z-SPAN_overnight_<YYYY-MM-DD>.md (structured Markdown,
per-action records, hard-cap utilization, things-for-operator-eye
summary at end).

Usage:
    python3.11 parsers/scripts/overnight_orchestrator.py

    # Or as a one-shot background process:
    nohup python3.11 parsers/scripts/overnight_orchestrator.py > /dev/null 2>&1 &
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ─────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────

# Repo root derived from this file's location (parsers/scripts/ sits four
# levels below the root); ZSPAN_ROOT env overrides for unusual layouts.
PROJECT_ROOT = Path(os.environ.get("ZSPAN_ROOT") or Path(__file__).resolve().parents[4])
PARSERS_DIR = PROJECT_ROOT / "02_Core_Project/council_navigator/parsers"
MEETINGS_DB = PARSERS_DIR / "meetings_cache.db"
FLASK_BASE = "http://127.0.0.1:5001"
AUDIT_PATH = Path.home() / f"Z-SPAN_overnight_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"

SCRAPE_TIMEOUT_SEC = 120          # per-city scrape (parser HTTP I/O)
SCAN_TIMEOUT_SEC = 60             # /api/work-orders/scan endpoint
HAIKU_MATCH_TIMEOUT_SEC = 600     # per-city haiku_match (channel video list + N matches)
CLEANUP_TIMEOUT_SEC = 1800        # 30 min ceiling on any cleanup step

# Insanity-check stops (no behavior-limiting caps)
PER_SUBPROCESS_TIMEOUT_SEC = 75 * 60    # 75 min — single-hang signal
CONSECUTIVE_FAILURE_HALT = 5            # cascading failure signal
CUMULATIVE_ERROR_RATE_HALT = 0.50       # structural pipeline issue
WHISPER_DISK_HALT_GB = 50               # disk-fill insanity signal

# Worker-daemon monitoring (Phase D)
WORKER_MONITOR_POLL_SEC = 60
WORKER_MONITOR_MAX_IDLE_MIN = 30        # if no queue movement for 30 min, exit Phase D (worker may be done or stuck)

# Anthropic rate-limit error patterns (graceful detection)
ANTHROPIC_RATE_LIMIT_PATTERNS = [
    "rate_limit_error",
    "rate limit",
    "Too Many Requests",
    "429",
    "usage_limit_exceeded",
]

# Target scope: Maricopa County only for first-night unsupervised run
TARGET_COUNTIES = ["Maricopa County"]

# Memory: temp-test-batch sentinel cohort (single-shot revert per session-6 carry-forward)
TEMP_TEST_BATCH_MEETING_IDS = (
    103223, 103224, 103225, 103323, 103324, 103753, 103983, 103993,
    103995, 104614, 104615, 104616, 104617, 104713, 101087, 101092,
    101099, 101115, 101118,
)
TEMP_TEST_BATCH_LABEL = "temp-test-batch-2026-06-24"

# Older Kingman sidecar backfill candidates (pre-sidecar_pipeline completions)
OLDER_KINGMAN_MEETING_IDS = (103324, 103323, 101091, 101092)

# ─────────────────────────────────────────────────────────────────
# Audit logging
# ─────────────────────────────────────────────────────────────────

class AuditLog:
    """Structured Markdown audit log. Append-only; flushes after every write
    so a kill mid-run still leaves a readable trail."""

    def __init__(self, path: Path):
        self.path = path
        self.fh = path.open("a", encoding="utf-8")
        self.subprocess_total = 0
        self.subprocess_errors = 0
        self.consecutive_failures = 0
        self.halt_reason: str | None = None

    def write(self, text: str) -> None:
        self.fh.write(text + "\n")
        self.fh.flush()

    def header(self, title: str, level: int = 2) -> None:
        self.write("")
        self.write("#" * level + " " + title)
        self.write("")

    def kv(self, key: str, value) -> None:
        self.write(f"- **{key}:** {value}")

    def record_subprocess(self, label: str, success: bool, detail: str = "") -> None:
        self.subprocess_total += 1
        if not success:
            self.subprocess_errors += 1
            self.consecutive_failures += 1
        else:
            self.consecutive_failures = 0
        status = "✅" if success else "❌"
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.write(f"  - {status} [{ts}] {label}" + (f" — {detail}" if detail else ""))

    def check_insanity_stops(self) -> str | None:
        """Returns halt reason if any insanity-check fires; None otherwise."""
        if self.consecutive_failures >= CONSECUTIVE_FAILURE_HALT:
            return f"consecutive_failures={self.consecutive_failures} >= {CONSECUTIVE_FAILURE_HALT}"
        if self.subprocess_total >= 20:  # don't compute rate on tiny samples
            rate = self.subprocess_errors / self.subprocess_total
            if rate > CUMULATIVE_ERROR_RATE_HALT:
                return f"cumulative_error_rate={rate:.0%} > {CUMULATIVE_ERROR_RATE_HALT:.0%} ({self.subprocess_errors}/{self.subprocess_total})"
        # Disk-fill check
        whisper_dir = PROJECT_ROOT / "02_Core_Project/zspan_pipeline/whisper_cache"
        if whisper_dir.exists():
            try:
                size_gb = sum(f.stat().st_size for f in whisper_dir.rglob('*') if f.is_file()) / (1024**3)
                if size_gb > WHISPER_DISK_HALT_GB:
                    return f"whisper_cache_disk={size_gb:.1f}GB > {WHISPER_DISK_HALT_GB}GB"
            except Exception:
                pass
        return None

    def close(self, final_summary: str = "") -> None:
        if final_summary:
            self.write("")
            self.write("---")
            self.write(final_summary)
        self.fh.close()


# ─────────────────────────────────────────────────────────────────
# Subprocess helper with timeout + rate-limit detection
# ─────────────────────────────────────────────────────────────────

def run_subprocess(cmd: list[str], audit: AuditLog, label: str, timeout: int = PER_SUBPROCESS_TIMEOUT_SEC, cwd: Path | None = None) -> tuple[bool, str]:
    """Run a subprocess with insanity-check timeout + Anthropic-cap detection.
    Returns (success, output_or_error)."""
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd or PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        combined = (result.stdout or "") + "\n" + (result.stderr or "")
        # Anthropic rate-limit detection
        for pattern in ANTHROPIC_RATE_LIMIT_PATTERNS:
            if pattern.lower() in combined.lower():
                audit.record_subprocess(label, success=False, detail=f"ANTHROPIC RATE LIMIT: {pattern}")
                audit.halt_reason = f"anthropic_rate_limit_detected ({pattern}) — exit cleanly so queue resumes naturally next shift"
                return False, combined
        if result.returncode == 0:
            audit.record_subprocess(label, success=True, detail=f"exit=0")
            return True, combined
        else:
            tail = combined.strip().split("\n")[-1][:200] if combined.strip() else "(no output)"
            audit.record_subprocess(label, success=False, detail=f"exit={result.returncode}: {tail}")
            return False, combined
    except subprocess.TimeoutExpired:
        audit.record_subprocess(label, success=False, detail=f"TIMEOUT after {timeout}s — single-hang insanity signal")
        return False, f"timeout after {timeout}s"
    except Exception as e:
        audit.record_subprocess(label, success=False, detail=f"exception: {type(e).__name__}: {e}")
        return False, str(e)


def flask_post(path: str, body: dict | None = None, timeout: int = SCAN_TIMEOUT_SEC) -> tuple[bool, dict | str]:
    """POST to Flask. Returns (success, parsed_json_or_error_text)."""
    try:
        data = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(
            FLASK_BASE + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return True, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def flask_get(path: str, timeout: int = SCAN_TIMEOUT_SEC) -> tuple[bool, dict | str]:
    try:
        req = urllib.request.Request(FLASK_BASE + path, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return True, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def query_db(sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(MEETINGS_DB)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def execute_db(sql: str, params: tuple = ()) -> int:
    conn = sqlite3.connect(MEETINGS_DB)
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────
# Phase A — Maricopa parser scrape sweep
# ─────────────────────────────────────────────────────────────────

def phase_a_scrape_maricopa(audit: AuditLog) -> dict:
    """Scrape every Maricopa parser via Flask /scrape/<city> endpoint.
    Returns metrics: cities_attempted, cities_succeeded, new_meetings, errors."""
    audit.header("Phase A — Maricopa parser scrape sweep")

    cities = query_db(
        "SELECT name FROM cities WHERE state='Arizona' AND county IN (" +
        ",".join("?" * len(TARGET_COUNTIES)) +
        ") AND parser_file IS NOT NULL AND parser_file != '' ORDER BY name",
        tuple(TARGET_COUNTIES),
    )
    audit.kv("Maricopa cities with registered parsers", len(cities))

    metrics = {"attempted": 0, "succeeded": 0, "empty_success": 0, "errors": 0, "new_meetings": 0}
    pre_count = query_db("SELECT COUNT(*) FROM meetings WHERE state='Arizona' AND county IN (" + ",".join("?" * len(TARGET_COUNTIES)) + ")", tuple(TARGET_COUNTIES))[0][0]

    for (city,) in cities:
        if audit.check_insanity_stops():
            return metrics
        metrics["attempted"] += 1
        # Force a refresh to pick up new meetings since last cache
        # URL-encode the city name
        from urllib.parse import quote
        city_encoded = quote(city)
        ok, resp = flask_get(f"/scrape/{city_encoded}?refresh=true", timeout=SCRAPE_TIMEOUT_SEC)
        if ok and isinstance(resp, dict):
            cnt = resp.get("count", 0)
            is_stale = resp.get("is_stale", False)
            err = resp.get("error")
            if resp.get("success") and not err:
                metrics["succeeded"] += 1
                if cnt == 0 or is_stale:
                    metrics["empty_success"] += 1
                    audit.record_subprocess(f"scrape {city}", success=True, detail=f"empty-success or stale (count={cnt}, is_stale={is_stale})")
                else:
                    audit.record_subprocess(f"scrape {city}", success=True, detail=f"count={cnt}")
            else:
                metrics["errors"] += 1
                audit.record_subprocess(f"scrape {city}", success=False, detail=f"success=False, count={cnt}, error={err}")
        else:
            metrics["errors"] += 1
            audit.record_subprocess(f"scrape {city}", success=False, detail=str(resp)[:200])

    post_count = query_db("SELECT COUNT(*) FROM meetings WHERE state='Arizona' AND county IN (" + ",".join("?" * len(TARGET_COUNTIES)) + ")", tuple(TARGET_COUNTIES))[0][0]
    metrics["new_meetings"] = post_count - pre_count

    audit.write("")
    audit.kv("Cities attempted", metrics["attempted"])
    audit.kv("Succeeded (incl honest-empty)", metrics["succeeded"])
    audit.kv("Empty-success (no meetings or stale-known)", metrics["empty_success"])
    audit.kv("Errors", metrics["errors"])
    audit.kv("Net new meetings cached", metrics["new_meetings"])
    return metrics


# ─────────────────────────────────────────────────────────────────
# Phase B — Enqueue WOs from newly-cached meetings
# ─────────────────────────────────────────────────────────────────

def phase_b_scan_work_orders(audit: AuditLog) -> dict:
    """POST /api/work-orders/scan to enqueue WOs from any new meetings.

    Must pass the cities parameter explicitly — the scanner's
    DEFAULT_TARGET_CITIES is hardcoded to Mohave only (Kingman, Bullhead
    City, Lake Havasu City, Colorado City). Without cities=[Maricopa
    list], the scan walks only Mohave and Maricopa meetings never
    enqueue (caught by 2026-06-25 orchestrator run-2 — Phase B was
    silently no-op on Maricopa data).
    """
    audit.header("Phase B — Work-order scan (enqueue from cache)")

    # Pull every city with a parser in our target counties; pass to scan
    target_cities = [
        r[0] for r in query_db(
            "SELECT name FROM cities WHERE state='Arizona' AND county IN (" +
            ",".join("?" * len(TARGET_COUNTIES)) +
            ") AND parser_file IS NOT NULL AND parser_file != '' ORDER BY name",
            tuple(TARGET_COUNTIES),
        )
    ]
    audit.kv("Cities passed to scanner", f"{len(target_cities)} ({', '.join(target_cities[:8])}{'...' if len(target_cities) > 8 else ''})")

    pre_count = query_db("SELECT COUNT(*) FROM work_orders")[0][0]
    ok, resp = flask_post("/api/work-orders/scan", body={"cities": target_cities, "age_limit_days": 30})
    post_count = query_db("SELECT COUNT(*) FROM work_orders")[0][0]
    new_wos = post_count - pre_count

    if ok and isinstance(resp, dict) and resp.get("success"):
        summary = resp.get("summary", {})
        detail = f"new WOs={new_wos}; scanned={summary.get('scanned',0)}; awaiting_video={summary.get('enqueued_awaiting_video',0)}; pending={summary.get('enqueued_pending',0)}; skipped_too_old={summary.get('skipped_too_old',0)}"
        audit.record_subprocess("POST /api/work-orders/scan", success=True, detail=detail)
    else:
        audit.record_subprocess("POST /api/work-orders/scan", success=False, detail=str(resp)[:200])

    audit.kv("New WOs enqueued", new_wos)
    audit.kv("Total WOs in queue", post_count)
    return {"new_wos": new_wos, "total_wos": post_count}


# ─────────────────────────────────────────────────────────────────
# Phase C — haiku_match for each YT-channel-registered Maricopa city
# ─────────────────────────────────────────────────────────────────

def phase_c_haiku_match(audit: AuditLog) -> dict:
    """Fire haiku_match_videos.py --apply for each Maricopa city with a
    registered YT channel. Pre/post promotion delta tracked."""
    audit.header("Phase C — haiku_match_videos.py autonomous URL matching")

    cities = query_db(
        """SELECT name FROM cities WHERE state='Arizona' AND county IN (""" +
        ",".join("?" * len(TARGET_COUNTIES)) +
        """) AND youtube_channel_url IS NOT NULL AND youtube_channel_url != '' ORDER BY name""",
        tuple(TARGET_COUNTIES),
    )
    audit.kv("Maricopa cities with registered YT channels", len(cities))

    pre_awaiting = query_db("""
        SELECT COUNT(*) FROM work_orders wo
        JOIN meetings m ON m.id = wo.meeting_id
        WHERE m.county IN (""" + ",".join("?" * len(TARGET_COUNTIES)) + """) AND wo.state = 'awaiting_video'
    """, tuple(TARGET_COUNTIES))[0][0]
    pre_pending = query_db("""
        SELECT COUNT(*) FROM work_orders wo
        JOIN meetings m ON m.id = wo.meeting_id
        WHERE m.county IN (""" + ",".join("?" * len(TARGET_COUNTIES)) + """) AND wo.state = 'pending'
    """, tuple(TARGET_COUNTIES))[0][0]

    metrics = {"cities_attempted": 0, "cities_succeeded": 0, "errors": 0}
    haiku_script = PARSERS_DIR / "scripts/haiku_match_videos.py"
    python_bin = str(PROJECT_ROOT / ".venv-worker/bin/python")  # match worker venv

    for (city,) in cities:
        if audit.check_insanity_stops():
            return metrics
        if audit.halt_reason:
            return metrics
        metrics["cities_attempted"] += 1
        ok, _ = run_subprocess(
            [python_bin, str(haiku_script), "--city", city, "--apply", "--within-days", "14", "--state", "Arizona"],
            audit,
            label=f"haiku_match {city}",
            timeout=HAIKU_MATCH_TIMEOUT_SEC,
            cwd=PARSERS_DIR,
        )
        if ok:
            metrics["cities_succeeded"] += 1
        else:
            metrics["errors"] += 1

    post_awaiting = query_db("""
        SELECT COUNT(*) FROM work_orders wo
        JOIN meetings m ON m.id = wo.meeting_id
        WHERE m.county IN (""" + ",".join("?" * len(TARGET_COUNTIES)) + """) AND wo.state = 'awaiting_video'
    """, tuple(TARGET_COUNTIES))[0][0]
    post_pending = query_db("""
        SELECT COUNT(*) FROM work_orders wo
        JOIN meetings m ON m.id = wo.meeting_id
        WHERE m.county IN (""" + ",".join("?" * len(TARGET_COUNTIES)) + """) AND wo.state = 'pending'
    """, tuple(TARGET_COUNTIES))[0][0]

    metrics["awaiting_delta"] = post_awaiting - pre_awaiting
    metrics["pending_delta"] = post_pending - pre_pending

    audit.write("")
    audit.kv("Cities attempted", metrics["cities_attempted"])
    audit.kv("Cities succeeded", metrics["cities_succeeded"])
    audit.kv("Errors", metrics["errors"])
    audit.kv("awaiting_video → pending delta", f"{metrics['awaiting_delta']:+d} awaiting / {metrics['pending_delta']:+d} pending (high-confidence haiku matches auto-promote)")
    return metrics


# ─────────────────────────────────────────────────────────────────
# Phase D — Monitor worker-daemon drain
# ─────────────────────────────────────────────────────────────────

def phase_d_monitor_worker(audit: AuditLog) -> dict:
    """Passive monitor: the launchd-managed worker daemon (PID 517) drains
    pending WOs at its D-005 60s defrag pace. Orchestrator polls queue
    state and reports progress until queue is exhausted OR no movement
    for WORKER_MONITOR_MAX_IDLE_MIN."""
    audit.header("Phase D — Worker-daemon drain monitor (passive)")

    start = time.time()
    last_movement = start
    metrics = {"polls": 0, "drained": 0, "started_pending": 0, "wall_clock_min": 0}

    # Initial snapshot
    pending_q = query_db("SELECT COUNT(*) FROM work_orders WHERE state='pending'")[0][0]
    processing_q = query_db("SELECT COUNT(*) FROM work_orders WHERE state='processing'")[0][0]
    completed_pre = query_db("SELECT COUNT(*) FROM work_orders WHERE state='completed'")[0][0]
    metrics["started_pending"] = pending_q
    audit.kv("Pending at Phase D start", pending_q)
    audit.kv("Processing at Phase D start", processing_q)
    audit.kv("Completed at Phase D start", completed_pre)
    audit.write("")
    audit.write("Polling worker progress every 60s. Drain naturally completes when pending+processing = 0.")
    audit.write("")

    last_pending = pending_q
    last_completed = completed_pre

    while True:
        time.sleep(WORKER_MONITOR_POLL_SEC)
        metrics["polls"] += 1
        pending_q = query_db("SELECT COUNT(*) FROM work_orders WHERE state='pending'")[0][0]
        processing_q = query_db("SELECT COUNT(*) FROM work_orders WHERE state='processing'")[0][0]
        completed_q = query_db("SELECT COUNT(*) FROM work_orders WHERE state='completed'")[0][0]
        failed_q = query_db("SELECT COUNT(*) FROM work_orders WHERE state='failed'")[0][0]

        if completed_q != last_completed or pending_q != last_pending:
            last_movement = time.time()
            audit.write(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')}] pending={pending_q} processing={processing_q} completed={completed_q} failed={failed_q}")
            last_pending = pending_q
            last_completed = completed_q

        # Drain complete?
        if pending_q == 0 and processing_q == 0:
            audit.write("")
            audit.write("✅ Queue drained — no pending or processing WOs remaining")
            break

        # No movement for too long?
        idle_min = (time.time() - last_movement) / 60
        if idle_min > WORKER_MONITOR_MAX_IDLE_MIN:
            audit.write("")
            audit.write(f"⏸ No queue movement for {idle_min:.0f} min — worker may be stuck or Anthropic-cap-limited. Exiting Phase D for cleanup phases.")
            break

        # Insanity halt check
        if audit.check_insanity_stops():
            audit.write("")
            audit.write(f"⛔ Insanity-check fired: {audit.check_insanity_stops()}")
            break

        # Wall-clock sanity (don't poll forever)
        elapsed_hr = (time.time() - start) / 3600
        if elapsed_hr > 10:  # 10-hour max for Phase D alone
            audit.write("")
            audit.write(f"⏰ Phase D wall-clock {elapsed_hr:.1f}h exceeded 10h ceiling — exiting for cleanup phases.")
            break

    completed_post = query_db("SELECT COUNT(*) FROM work_orders WHERE state='completed'")[0][0]
    metrics["drained"] = completed_post - completed_pre
    metrics["wall_clock_min"] = round((time.time() - start) / 60, 1)

    audit.write("")
    audit.kv("Meetings drained by worker", metrics["drained"])
    audit.kv("Phase D wall-clock", f"{metrics['wall_clock_min']} min")
    audit.kv("Total polls", metrics["polls"])
    return metrics


# ─────────────────────────────────────────────────────────────────
# Phase E — Cleanup mechanicals
# ─────────────────────────────────────────────────────────────────

def phase_e_cleanup(audit: AuditLog) -> dict:
    """check_alignment_health + temp-test-batch revert + older Kingman backfill."""
    audit.header("Phase E — Cleanup mechanicals")

    metrics = {"alignment_health_ok": False, "temp_test_batch_reverted_meetings": 0, "temp_test_batch_reverted_wos": 0, "older_kingman_backfilled": 0}
    python_bin = str(PROJECT_ROOT / ".venv-worker/bin/python")

    # E.1 — check_alignment_health --fix
    audit.write("")
    audit.write("**E.1 — check_alignment_health sweep**")
    check_script = PROJECT_ROOT / "02_Core_Project/zspan_pipeline/scripts/check_alignment_health.py"
    if check_script.exists():
        ok, _ = run_subprocess(
            [python_bin, str(check_script), "--fix"],
            audit, label="check_alignment_health --fix", timeout=CLEANUP_TIMEOUT_SEC,
            cwd=PROJECT_ROOT / "02_Core_Project",
        )
        metrics["alignment_health_ok"] = ok
    else:
        audit.write(f"  ⚠️ {check_script} not found — skipping")

    # E.2 — temp-test-batch revert SQL (single-shot per session-6 carry-forward).
    # SANCTIONED BYPASS of publish_meeting()/unpublish_meeting() — this is the
    # ONLY place the codebase should be writing is_published directly. Scope is
    # per-meeting-id from TEMP_TEST_BATCH_MEETING_IDS (never the whole table);
    # both counts (pre/post) are captured in the audit stream so the sentinel
    # revert has a paper trail. If a NEW ad-hoc revert script appears elsewhere
    # in the repo, route it through unpublish_meeting() instead — the m104714
    # rogue-publish incident (session-31) traced back to a since-deleted
    # _publish_m101091.py one-shot that skipped this discipline.
    audit.write("")
    audit.write("**E.2 — temp-test-batch sentinel revert**")
    placeholders = ",".join("?" * len(TEMP_TEST_BATCH_MEETING_IDS))
    try:
        # Count pre
        pre_published = query_db(
            f"SELECT COUNT(*) FROM meetings WHERE id IN ({placeholders}) AND is_published=1",
            TEMP_TEST_BATCH_MEETING_IDS,
        )[0][0]
        pre_approved = query_db(
            f"SELECT COUNT(*) FROM work_orders WHERE approved_by=?",
            (TEMP_TEST_BATCH_LABEL,),
        )[0][0]

        # Apply revert
        m_rows = execute_db(
            f"UPDATE meetings SET is_published=0 WHERE id IN ({placeholders})",
            TEMP_TEST_BATCH_MEETING_IDS,
        )
        w_rows = execute_db(
            f"UPDATE work_orders SET approved_at=NULL, approved_by=NULL WHERE approved_by=?",
            (TEMP_TEST_BATCH_LABEL,),
        )
        metrics["temp_test_batch_reverted_meetings"] = m_rows
        metrics["temp_test_batch_reverted_wos"] = w_rows
        audit.record_subprocess(
            "temp-test-batch revert SQL",
            success=True,
            detail=f"meetings flipped is_published=0: {m_rows} (pre-published count: {pre_published}); WO approval cleared: {w_rows} (pre-approved by sentinel: {pre_approved})",
        )
    except Exception as e:
        audit.record_subprocess("temp-test-batch revert SQL", success=False, detail=f"{type(e).__name__}: {e}")

    # E.3 — Older Kingman sidecar backfill
    audit.write("")
    audit.write("**E.3 — Older Kingman sidecar backfill**")
    sidecar_backfill_script = PROJECT_ROOT / "02_Core_Project/zspan_pipeline/scripts/backfill_sidecars.py"
    if sidecar_backfill_script.exists():
        for mid in OLDER_KINGMAN_MEETING_IDS:
            if audit.check_insanity_stops() or audit.halt_reason:
                break
            # Check if already has full sidecar set
            preview_dir = PROJECT_ROOT / ".preview"
            existing_files = list(preview_dir.glob(f"m{mid}*.json")) if preview_dir.exists() else []
            if len(existing_files) >= 4:
                audit.write(f"  ⏭ m{mid} already has {len(existing_files)} sidecar files — skipping")
                continue
            ok, _ = run_subprocess(
                [python_bin, str(sidecar_backfill_script), "--run", "--meeting-id", str(mid)],
                audit, label=f"sidecar backfill m{mid}", timeout=PER_SUBPROCESS_TIMEOUT_SEC,
                cwd=PROJECT_ROOT / "02_Core_Project",
            )
            if ok:
                metrics["older_kingman_backfilled"] += 1
    else:
        audit.write(f"  ⚠️ {sidecar_backfill_script} not found — skipping")

    audit.write("")
    return metrics


# ─────────────────────────────────────────────────────────────────
# Phase F — Final summary
# ─────────────────────────────────────────────────────────────────

def phase_f_summary(audit: AuditLog, phase_metrics: dict, started_at: datetime, ended_at: datetime) -> str:
    """Render the operator-facing morning summary at the bottom of the audit log."""
    audit.header("Phase F — Morning summary for operator")
    duration_min = (ended_at - started_at).total_seconds() / 60
    audit.kv("Started (UTC)", started_at.strftime("%Y-%m-%d %H:%M:%S"))
    audit.kv("Ended (UTC)", ended_at.strftime("%Y-%m-%d %H:%M:%S"))
    audit.kv("Total duration", f"{duration_min:.1f} min ({duration_min/60:.1f} hr)")
    audit.kv("Halt reason", audit.halt_reason or "natural completion (queue drained or all phases ran)")
    audit.kv("Subprocess calls", f"{audit.subprocess_total} total, {audit.subprocess_errors} errors ({audit.subprocess_errors/max(audit.subprocess_total,1)*100:.0f}%)")

    audit.write("")
    audit.write("### Per-phase deltas")
    audit.write("")
    a = phase_metrics.get("A", {})
    audit.write(f"- **Phase A (Maricopa scrape):** {a.get('succeeded',0)}/{a.get('attempted',0)} cities succeeded; {a.get('empty_success',0)} empty-success (likely stale parsers worth investigation); {a.get('errors',0)} errors; **+{a.get('new_meetings',0)} new meetings** cached")
    b = phase_metrics.get("B", {})
    audit.write(f"- **Phase B (WO scan):** +{b.get('new_wos',0)} new WOs enqueued (total queue depth now: {b.get('total_wos','?')})")
    c = phase_metrics.get("C", {})
    audit.write(f"- **Phase C (haiku_match):** {c.get('cities_succeeded',0)}/{c.get('cities_attempted',0)} cities succeeded; {c.get('errors',0)} errors; **awaiting_video {c.get('awaiting_delta','?'):+d} / pending {c.get('pending_delta','?'):+d}** (deltas show high-confidence auto-promotions)")
    d = phase_metrics.get("D", {})
    audit.write(f"- **Phase D (worker drain):** {d.get('drained',0)} meetings drained over {d.get('wall_clock_min',0)} min ({d.get('polls',0)} polls)")
    e = phase_metrics.get("E", {})
    audit.write(f"- **Phase E (cleanup):** alignment_health={'✅' if e.get('alignment_health_ok') else '❌'}; temp-test-batch reverted {e.get('temp_test_batch_reverted_meetings',0)} meetings + {e.get('temp_test_batch_reverted_wos',0)} WOs; {e.get('older_kingman_backfilled',0)} older Kingman sidecars backfilled")

    audit.write("")
    audit.write("### Things for operator eye in the morning")
    audit.write("")

    things = []
    if a.get("empty_success", 0) > 0:
        things.append(f"- ⚠️ **{a['empty_success']} Maricopa cities returned empty-success on scrape** — could be honest empty (no recent meetings) OR stale parsers (silent F8 failure). Worth quick sanity check against expected meeting cadence.")
    if a.get("errors", 0) > 0:
        things.append(f"- ❌ **{a['errors']} Maricopa parser scrape errors** — list above. Likely WAF blocks, vendor changes, or broken parsers. Triage candidates.")
    if c.get("errors", 0) > 0:
        things.append(f"- ❌ **{c['errors']} haiku_match errors** — could be YouTube Data API quota, channel-not-found, or Haiku/Anthropic rate. Check per-city detail above.")
    if c.get("pending_delta", 0) > 0:
        things.append(f"- ✅ **{c['pending_delta']} WOs auto-promoted to pending** by haiku high-confidence matches — these will need operator audit-review to confirm the matches are real (per [[never-frame-operator-paste-as-constraint]], wrong-YT-attached-to-meeting is the single overnight misfire class that the audit log exists to catch).")
    if d.get("drained", 0) > 0:
        things.append(f"- ✅ **{d['drained']} meetings drained** through V1-RAG-3 + sidecar by worker daemon. Spot-check 2-3 BroadcastPages to confirm the new content renders correctly.")
    if e.get("temp_test_batch_reverted_meetings", 0) > 0:
        things.append(f"- ✅ **temp-test-batch sentinel cleared** — {e['temp_test_batch_reverted_meetings']} meetings flipped to is_published=0, {e['temp_test_batch_reverted_wos']} WOs cleared. One of the 🟠 carry-forwards now closed.")
    if audit.halt_reason and "anthropic_rate_limit" in audit.halt_reason:
        things.append(f"- ⏸ **Anthropic rate-limit hit mid-run** — orchestrator exited cleanly. Queue resumes naturally on next shift (worker daemon picks up where it left off after cap window resets).")
    if not things:
        things.append("- 🟢 No anomalies surfaced. Clean overnight run.")
    for t in things:
        audit.write(t)

    summary_line = f"Overnight run complete: {audit.subprocess_total} subprocess calls, {audit.subprocess_errors} errors ({audit.subprocess_errors/max(audit.subprocess_total,1)*100:.0f}%). See {AUDIT_PATH} for full detail."
    return summary_line


# ─────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────

def main() -> int:
    started_at = datetime.now(timezone.utc)
    audit = AuditLog(AUDIT_PATH)

    # File header
    audit.write(f"# Z-SPAN Overnight Audit Log — {started_at.strftime('%Y-%m-%d')}")
    audit.write("")
    audit.write(f"**Started:** {started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}  ")
    audit.write(f"**Scope:** {', '.join(TARGET_COUNTIES)} (first-night unsupervised; expands to remaining 7 AZ counties on subsequent shifts if clean)  ")
    audit.write(f"**Per D-138:** autonomous ingestion is the floor — haiku_match_videos.py + parser-native + S-037 V0 transcribe-non-youtube.  ")
    audit.write(f"**Insanity-check stops only** (subscription via MAX, Anthropic 5-hour cap is the rate-limit):")
    audit.write(f"  - per-subprocess {PER_SUBPROCESS_TIMEOUT_SEC//60}min wall-clock timeout")
    audit.write(f"  - {CONSECUTIVE_FAILURE_HALT} consecutive subprocess failures → halt")
    audit.write(f"  - cumulative error rate >{int(CUMULATIVE_ERROR_RATE_HALT*100)}% → halt")
    audit.write(f"  - Whisper-intermediates disk >{WHISPER_DISK_HALT_GB}GB → halt")
    audit.write(f"  - Anthropic rate-limit detected → log + exit cleanly (queue resumes next shift)")
    audit.write("")

    phase_metrics = {}

    try:
        # Phase A
        phase_metrics["A"] = phase_a_scrape_maricopa(audit)
        if audit.halt_reason:
            raise SystemExit(0)
        halt = audit.check_insanity_stops()
        if halt:
            audit.halt_reason = halt
            raise SystemExit(0)

        # Phase B
        phase_metrics["B"] = phase_b_scan_work_orders(audit)
        if audit.halt_reason:
            raise SystemExit(0)
        halt = audit.check_insanity_stops()
        if halt:
            audit.halt_reason = halt
            raise SystemExit(0)

        # Phase C
        phase_metrics["C"] = phase_c_haiku_match(audit)
        if audit.halt_reason:
            raise SystemExit(0)
        halt = audit.check_insanity_stops()
        if halt:
            audit.halt_reason = halt
            raise SystemExit(0)

        # Phase D
        phase_metrics["D"] = phase_d_monitor_worker(audit)
        # Don't halt on D insanity — let cleanup still run

        # Phase E (always runs even after D halt — cleanups are cheap + valuable)
        phase_metrics["E"] = phase_e_cleanup(audit)

    except SystemExit:
        pass
    except Exception as e:
        audit.write("")
        audit.write(f"⛔ Orchestrator-level exception: {type(e).__name__}: {e}")
        audit.halt_reason = f"orchestrator_exception: {type(e).__name__}"

    ended_at = datetime.now(timezone.utc)
    summary = phase_f_summary(audit, phase_metrics, started_at, ended_at)
    audit.close(final_summary=summary)
    print(summary)
    return 0 if audit.subprocess_errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
