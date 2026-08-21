"""
slack_listener_status_tailer -- live-updating ack message for the DM bridge
============================================================================

Chunk 1c follow-up to D-062a (DM bridge inbound).

When the slack_listener spawns an orchestrator in response to James's DM,
this module's `start_status_watcher()` runs a background thread that edits
the initial ack message in-place as the orchestrator progresses. Two
signal sources:

  1. **Crashes CSV** (`ops/orchestrator-logs/crashes.csv`) — appended one
     row per `claude -p` attempt. While the spawn script is in retry mode
     (attempts crashing at startup), this gives us the live attempt count
     and lets the ack read e.g. "Spawning -- Claude crashed at startup,
     retrying attempt 3 of ~10..." instead of a silent wait.

  2. **Orchestrator JSONL log** (`ops/orchestrator-logs/<stamp>-instructed
     *.jsonl`) -- written by `claude -p --output-format stream-json` once
     it actually survives startup. PowerShell's Tee-Object writes UTF-16
     LE; the tailer reads with that encoding. Tool-use blocks map to
     friendly operator-facing statuses ("Reading the board...", "Checking
     escalations...", "Thinking...", "Posting reply...").

Design rules:

  * **Edit, never post.** The watcher only calls `chat.update` on the ack
    that the listener already posted. The orchestrator's actual reply
    lands as a separate threaded message via its own escalate path
    (slack_notifier DM routing per D-072 / chunk 1b).

  * **Debounced edits.** At most one edit every 2.5 seconds, and only
    when the status string actually changes. Avoids flicker + respects
    Slack's 1-edit-per-sec rate limit.

  * **Operator-first surfaces (D-054).** Every status string reads like
    a colleague would say it. No schema labels, no JSON keys. "Checking
    escalations..." not "tool_use=Bash desc=read_badges".

  * **Hard ceiling.** Watcher quits after 5 min wall-clock (3 min spawn
    retry budget + 2 min orchestrator runtime + buffer) regardless of
    subprocess state. The subprocess itself is detached and not awaited;
    if it lingers past the ceiling, James can still read the JSONL trace
    from the operator terminal.

  * **No new MCP / Slack scopes needed.** Uses `chat.update` which is
    already granted under the bot's existing `chat:write` scope.
"""
from __future__ import annotations

import csv
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# Repo paths -- this file lives at
# 02_Core_Project/council_navigator/parsers/slack_listener_status_tailer.py
# so the repo root is 3 levels up.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_LOGS_DIR = _REPO_ROOT / "ops" / "orchestrator-logs"
_CRASHES_CSV = _LOGS_DIR / "crashes.csv"


# Tuning knobs. Slack's chat.update rate limit is ~1/sec/channel; we
# add a safety margin so a single conversation doesn't get throttled.
POLL_INTERVAL_S = 1.0
MIN_EDIT_GAP_S = 2.5
MAX_WAIT_S = 300.0  # 5 minutes hard ceiling

# The spawn script uses this byte threshold to distinguish "claude crashed
# at startup" (0 bytes) from "claude ran and errored" (some output). Same
# constant as the PowerShell script.
CRASH_BYTE_THRESHOLD = 100


# ===== Status mapping =====================================================


def _humanize_bash_desc(desc: str) -> str:
    """Translate the orchestrator's Bash `description` field into a short
    operator-facing status string. The orchestrator writes specific
    descriptions per the routine.md prompt ("Tier 0: read operator badges"
    etc.); we don't enumerate every one -- we pattern-match the common
    cases and fall back to a short rendition of the description itself.
    """
    lower = desc.lower()
    # Order matters: action verbs first (most specific intent), then
    # specific nouns/contexts, then generic patterns. Otherwise a
    # description like "trigger content-scout watcher" would match
    # the "watcher" branch instead of "trigger".
    if "trigger" in lower:
        return "Triggering an agent"
    if "escalate" in lower:
        return "Posting reply"
    if "badge" in lower:
        return "Checking the badges"
    if "ingestion" in lower or "governor" in lower:
        return "Checking the ingestion pace"
    if "meeting" in lower or "work_order" in lower or "work order" in lower:
        return "Checking the work-order queue"
    if "watcher" in lower or "parser-health" in lower or "parser_health" in lower:
        return "Checking watcher state"
    if "memory" in lower:
        return "Reading agent memory"
    if "escalation" in lower or "pending" in lower:
        return "Checking escalations"
    # Generic fallback -- short and operator-facing.
    short = desc.strip().rstrip(".")
    if len(short) > 50:
        short = short[:47] + "..."
    return f"Running: {short}"


def _event_to_status(event: dict) -> Optional[str]:
    """Map one JSONL event from claude -p --output-format stream-json to
    an operator-friendly status string, OR return None if the event
    shouldn't update the status. Called per new line.

    Priority order (highest signal first):
      - `assistant` with tool_use block -- the orchestrator is taking an
        action; the action name + description maps to a status.
      - `assistant` with text block -- the orchestrator is composing
        prose; surface as "Writing reply" (close to the end).
      - `system/thinking_tokens` -- extended thinking; "Thinking".
      - `system/init` -- spawn started; "Reading the board".
      - `result/success` -- orchestrator finished; sentinel for the
        final-state edit (caller decides what to write).

    The mapping deliberately ignores high-frequency events
    (`stream_event` deltas, `rate_limit_event`) -- they fire many
    times per second and would just thrash the edit cadence.
    """
    etype = event.get("type")
    subtype = event.get("subtype")

    if etype == "system" and subtype == "init":
        return "Reading the board"

    if etype == "system" and subtype == "thinking_tokens":
        return "Thinking"

    if etype == "result":
        # Caller handles result/success vs result/error vs result/<other>
        # via the watcher loop's terminal-state path.
        return None

    if etype != "assistant":
        return None

    msg = event.get("message", {})
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if not isinstance(content, list):
        return None

    # Walk content blocks in order; the LAST tool_use or text block in the
    # message is the most informative.
    latest_status: Optional[str] = None
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_use":
            name = block.get("name") or ""
            inp = block.get("input") or {}
            if not isinstance(inp, dict):
                inp = {}
            if name == "Bash":
                desc = (inp.get("description") or "").strip()
                if desc:
                    latest_status = _humanize_bash_desc(desc)
                else:
                    # Bash with no description -- short fallback.
                    cmd = (inp.get("command") or "").strip()
                    short = cmd[:40] + ("..." if len(cmd) > 40 else "")
                    latest_status = f"Running shell: {short}" if short else "Running a shell command"
            elif name == "Read":
                fp = (inp.get("file_path") or "").strip()
                tail = Path(fp).name if fp else ""
                latest_status = f"Reading {tail}" if tail else "Reading a file"
            elif name == "Glob":
                latest_status = "Looking up files"
            elif name == "Grep":
                latest_status = "Searching files"
            elif name == "TodoWrite":
                latest_status = "Planning steps"
            else:
                latest_status = f"Using {name}"
        elif btype == "text":
            t = (block.get("text") or "").strip()
            if t:
                # The orchestrator is composing prose -- close to the
                # final reply. Don't surface partial text content; just
                # the act of composing.
                latest_status = "Writing reply"
        # We don't break -- we want the LAST informative block, not the first.

    return latest_status


# ===== Crash CSV tailer ===================================================


def _count_run_attempts(run_started_at: float) -> tuple[int, int, bool]:
    """Read crashes.csv and return (total_attempts, crashed_count,
    any_success) for rows whose timestamp is newer than run_started_at.

    The CSV columns: timestamp,attempt,exit_code,exit_hex,transcript_bytes,
    duration_s,outcome,log_file. `outcome` is one of: success / crashed /
    error / success-instructed / crashed-instructed / error-instructed.

    The CSV's `timestamp` column is the run-stamp (yyyy-MM-ddTHH-mm-ss),
    SAME stamp for every retry attempt of one spawn. We can't directly
    parse it back to a unix epoch reliably across timezones, so we use
    file mtime semantics: if a row was appended AFTER our spawn started,
    it belongs to us (assumption: serialized spawns).
    """
    if not _CRASHES_CSV.is_file():
        return (0, 0, False)
    try:
        # Read mtime as our "row arrival time" proxy. Crude but works
        # because rows are appended; the file's mtime updates on each
        # append. We can't get per-row append time without a re-read,
        # so we scan from the end and stop when the row count stabilizes.
        # Simpler approach: scan all rows since the file's last seek
        # point. We track that per call via the file size.
        with _CRASHES_CSV.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            # We can't easily filter by "newer than run_started_at" without
            # a stamp comparison, so we use the latest stamp instead --
            # rows for OUR spawn share the same stamp as the latest one,
            # because spawns are serialized (single-operator pattern).
            # That assumption breaks if two spawns overlap, but the
            # listener guards against that (one DM at a time).
            rows = list(reader)
        if not rows:
            return (0, 0, False)
        latest_stamp = rows[-1].get("timestamp", "")
        run_rows = [r for r in rows if r.get("timestamp") == latest_stamp]
        total = len(run_rows)
        crashed = sum(1 for r in run_rows if "crashed" in (r.get("outcome") or ""))
        success = any("success" in (r.get("outcome") or "") for r in run_rows)
        return (total, crashed, success)
    except Exception as e:
        logger.warning("status_tailer: crashes.csv read failed: %s", e)
        return (0, 0, False)


# ===== Orchestrator JSONL tailer ==========================================


def _find_latest_log(run_started_at: float) -> Optional[Path]:
    """Find the most recently-modified *-instructed*.jsonl file in
    ops/orchestrator-logs/. Returns None if no matching file exists or
    if the newest one is older than run_started_at (a stale leftover).
    """
    if not _LOGS_DIR.is_dir():
        return None
    candidates = list(_LOGS_DIR.glob("*-instructed*.jsonl"))
    if not candidates:
        return None
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    # 2-second slack -- the spawn script may stamp the filename slightly
    # before the file's first mtime update.
    if latest.stat().st_mtime < run_started_at - 2:
        return None
    return latest


@dataclass
class _TailState:
    """Per-log-file tailer position. The encoding gotcha: PowerShell
    Tee-Object writes UTF-16 LE with a 2-byte BOM. Each character takes
    2 bytes; line endings are CR-LF (4 bytes). We track the byte offset
    we've read up to and decode incrementally.
    """
    path: Path
    byte_offset: int = 0
    pending_partial: bytes = b""


def _read_new_events(state: _TailState) -> list[dict]:
    """Read any new lines appended to the JSONL log since the last call.
    Returns parsed event dicts; lines that fail to parse are skipped.

    Updates state.byte_offset + state.pending_partial in place so the
    next call resumes from where this one left off.
    """
    try:
        size = state.path.stat().st_size
    except OSError:
        return []
    if size <= state.byte_offset:
        return []
    try:
        with state.path.open("rb") as f:
            f.seek(state.byte_offset)
            new_bytes = f.read(size - state.byte_offset)
    except OSError:
        return []
    state.byte_offset = size
    buf = state.pending_partial + new_bytes
    # UTF-16 LE: each char is 2 bytes. If we read an odd number of bytes,
    # save the last byte for next time.
    if len(buf) % 2 != 0:
        state.pending_partial = buf[-1:]
        buf = buf[:-1]
    else:
        state.pending_partial = b""
    try:
        text = buf.decode("utf-16-le", errors="replace")
    except Exception:
        return []
    # Strip leading BOM if present (only on first read).
    if text and text[0] == "﻿":
        text = text[1:]
    events: list[dict] = []
    for raw_line in text.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            obj = json.loads(raw_line)
            if isinstance(obj, dict):
                events.append(obj)
        except Exception:
            # Partial line at end of file -- save it for next round.
            # We approximate this by checking if the last char isn't \n.
            # Cheap heuristic: if json.loads failed AND this is the last
            # line, push it back into pending_partial.
            continue
    return events


# ===== Slack edit (debounced) =============================================


def _try_chat_update(web_client, channel: str, ack_ts: str, text: str) -> bool:
    """Call chat.update on the ack message. Returns True if the API call
    succeeded (used to track "last edit time" for debouncing).
    """
    try:
        web_client.chat_update(channel=channel, ts=ack_ts, text=text)
        return True
    except Exception as e:
        # Common failure: rate-limited. We catch broadly so a transient
        # error doesn't kill the watcher; next poll will retry.
        logger.debug(
            "status_tailer: chat.update failed for ack_ts=%s: %s",
            ack_ts, e,
        )
        return False


# ===== Watch loop =========================================================


@dataclass
class _WatchState:
    """Per-watch mutable state."""
    last_status_text: str = ""
    last_edit_at: float = 0.0
    last_attempt_count: int = 0
    tail_state: Optional[_TailState] = field(default=None)
    saw_orchestrator_running: bool = False


def _format_retry_status(total_attempts: int, crashed_count: int) -> str:
    """Operator-facing status for the "spawn script still retrying" phase.

    Examples:
      "Spawning the orchestrator"                  (no rows yet)
      "Spawning -- attempt 2 (Claude crashed at startup, retrying)"
      "Spawning -- attempt 5 (Claude crashed at startup, retrying)"
    """
    if total_attempts <= 1:
        return "Spawning the orchestrator"
    return (
        f"Spawning -- attempt {total_attempts} "
        f"(Claude crashed at startup, retrying)"
    )


def _final_status_after_subprocess_exit(
    state: _WatchState,
    elapsed: float,
) -> str:
    """Compose the terminal-state ack edit when the subprocess has exited.

    Cases:
      - Orchestrator ran and posted a reply (saw_orchestrator_running +
        at least one tool_use seen): "Orchestrator done -- reply above"
        (we can't tell from here whether the reply actually posted, but
        the orchestrator's escalate wrapper handles that on its side).
      - Spawn script retried but all attempts crashed at startup:
        "Spawn failed -- Claude CLI crashed N times in {elapsed:.0f}s"
      - Mixed / unknown: surface a neutral state with a pointer.
    """
    total, crashed, success = _count_run_attempts(0.0)
    if success or state.saw_orchestrator_running:
        return "Orchestrator done -- reply above this message"
    if crashed > 0 and total == crashed:
        return (
            f"Spawn failed -- Claude CLI crashed {crashed} times at startup "
            f"in {elapsed:.0f}s. See ops/orchestrator-logs/crashes.csv."
        )
    return (
        f"Orchestrator exited after {elapsed:.0f}s without posting a reply. "
        f"See ops/orchestrator-logs/ for the transcript."
    )


def _watch_loop(
    web_client,
    channel: str,
    ack_ts: str,
    proc,
    started_at: float,
) -> None:
    """Background-thread body. Polls the crash CSV + the orchestrator
    JSONL log, debounces edits to the ack message, stops when the
    subprocess exits OR the 5-minute ceiling fires.
    """
    state = _WatchState(last_status_text="Spawning the orchestrator")
    try:
        while True:
            elapsed = time.time() - started_at
            if elapsed > MAX_WAIT_S:
                _try_chat_update(
                    web_client, channel, ack_ts,
                    f"Watcher timed out after {MAX_WAIT_S:.0f}s -- "
                    "check ops/orchestrator-logs/",
                )
                return

            # Subprocess alive check. proc.poll() returns None while
            # running, exit code once exited. Works even with
            # DETACHED_PROCESS on Windows.
            try:
                rc = proc.poll()
            except Exception:
                rc = None
            subprocess_alive = (rc is None)

            # Find / read the orchestrator log.
            if state.tail_state is None:
                log_path = _find_latest_log(started_at)
                if log_path:
                    state.tail_state = _TailState(path=log_path)
            events: list[dict] = []
            if state.tail_state is not None:
                events = _read_new_events(state.tail_state)
                if events:
                    state.saw_orchestrator_running = True

            # Pick the new status string.
            new_status: Optional[str] = None
            for ev in events:
                mapped = _event_to_status(ev)
                if mapped:
                    new_status = mapped  # take the latest mapping

            if new_status is None and not state.saw_orchestrator_running:
                # Still in spawn-script phase. Check retries.
                total, crashed, success = _count_run_attempts(started_at)
                if total > 0 and total != state.last_attempt_count:
                    state.last_attempt_count = total
                    new_status = _format_retry_status(total, crashed)

            # Debounced edit.
            if (
                new_status
                and new_status != state.last_status_text
                and (time.time() - state.last_edit_at) >= MIN_EDIT_GAP_S
            ):
                if _try_chat_update(web_client, channel, ack_ts, new_status):
                    state.last_status_text = new_status
                    state.last_edit_at = time.time()

            if not subprocess_alive:
                # Subprocess exited. One last status edit + return.
                final = _final_status_after_subprocess_exit(state, elapsed)
                if final != state.last_status_text:
                    _try_chat_update(web_client, channel, ack_ts, final)
                return

            time.sleep(POLL_INTERVAL_S)
    except Exception as e:
        # Never crash the listener -- log + try to surface a fallback edit.
        logger.exception("status_tailer: watch loop crashed: %s", e)
        try:
            _try_chat_update(
                web_client, channel, ack_ts,
                "Watcher crashed -- see Flask log. Spawn continues in background.",
            )
        except Exception:
            pass


def start_status_watcher(
    web_client,
    channel: str,
    ack_ts: str,
    proc,
) -> threading.Thread:
    """Spawn a daemon thread that edits the ack message in place as the
    orchestrator progresses. Returns the thread (caller can ignore).

    Args:
        web_client: The slack_sdk WebClient already initialized with the
            bot token (the listener's existing instance).
        channel: The Slack channel ID where the ack was posted (the DM).
        ack_ts: The `ts` of the ack message (returned by chat.postMessage).
        proc: The subprocess.Popen handle for the spawned orchestrator.
            Used for poll() to detect subprocess exit.

    The thread is a daemon so it dies with Flask. Caller need not join.
    """
    t = threading.Thread(
        target=_watch_loop,
        args=(web_client, channel, ack_ts, proc, time.time()),
        name=f"orchestrator-status-watcher-{ack_ts}",
        daemon=True,
    )
    t.start()
    return t
