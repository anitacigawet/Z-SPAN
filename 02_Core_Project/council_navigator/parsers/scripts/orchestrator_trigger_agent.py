#!/usr/bin/env python3.11
"""orchestrator_trigger_agent — autonomous sub-agent trigger for the orchestrator (D-074 / Stage B piece 2 chunk 3).

The orchestrator's ONE permitted way to fire a sub-agent autonomously.
Spawns the ONE parametrized Mac runner `ops/fleet_heartbeat.py --role <role>`
detached (the same runner launchd fires for scheduled runs; D-120 Mac port,
superseding the PC-era per-role `.ps1`). The sub-agent then runs in its own
fresh `claude -p` session — fired through the D-119 metering gate — with ITS
OWN settings.json, so the D-066 structural wall is preserved end-to-end. The
orchestrator never gains the sub-agent's tools.

Why this shape (and not Skills + context: fork):
  - Skills + fork inherits parent tools. Forking the orchestrator (which
    denies action endpoints per D-065) into a DQR Skill would give the
    fork orchestrator's READ-ONLY scope — the fork can't do DQR's work.
  - Relaxing orchestrator's settings to include sub-agent tools would
    break conduct-never-do (the orchestrator could call DQR actions
    directly, not just trigger them).
  - Subprocess-spawn is the project's existing pattern. Each sub-agent
    already has its own heartbeat script + settings.json. The
    autonomy gap was just "orchestrator can fire those scripts," which
    this wrapper closes with ~80 LOC.

Rung gating (the wall this wrapper enforces):
  - Rung 1 = autonomous triggering ONLY for read-only watchers
    (content-scout + parser-custodian). Worst case = an unnecessary
    escalation; fully reversible.
  - Rung 2+ = autonomous triggering of Opus judgment agents (DQR +
    Curator). PROMOTION REQUIRES editing this wrapper's
    RUNG_2_ADDITIONS set + the orchestrator role manual + a DECISIONS
    entry citing the audit-trail evidence (per D-061's ladder).
  - Rung 3+ = bounded autonomous Pipeline Operator triggering. Same
    promotion discipline.
  - INSTRUCTED execution (Mode B — James DM'd the orchestrator) is NOT
    gated by rung. Detected via the ZSPAN_ORCHESTRATOR_REPLY_THREAD_TS
    env var set by the instructed-spawn path (ops/fleet_heartbeat.py
    --role orchestrator-instructed-spawn). When instructed, any role in
    ALL_KNOWN_ROLES can be triggered.

Forge-resistance properties (the structural wall):
  - Role hardcoded against an allowlist; arbitrary role names rejected.
  - Runner path resolved from a constant (ops/fleet_heartbeat.py); the role
    is validated against the allowlist; neither can be redirected by CLI
    argument or env var.
  - The spawned runner invocation uses list-form args (no
    shell=True); no shell metachar interpretation.
  - Logs the trigger event to orchestrator-logs for audit trail.

Usage:
    python3.11 scripts/orchestrator_trigger_agent.py --role <role-name>

The orchestrator's settings.json allows ONLY this entry point + the
existing board_read / escalate / memory_write wrappers. The agent cannot
shell out arbitrarily.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

# Make `parsers/` importable.
_THIS = Path(__file__).resolve()
_PARSERS_DIR = _THIS.parent.parent
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# Hardcoded paths — the wrapper cannot be redirected to spawn arbitrary
# scripts even if the agent's reasoning suggested it. Repo root is 4
# levels up from this file: scripts/ -> parsers/ ->
# council_navigator/ -> 02_Core_Project/ -> ZSPAN/.
_REPO_ROOT = _THIS.parents[4]
_OPS_DIR = _REPO_ROOT / "ops"
_LOGS_DIR = _OPS_DIR / "orchestrator-logs"

# Rung-gating allowlists. Promotion = edit these sets + role manual +
# DECISIONS entry (per D-061's ladder discipline).
RUNG_1_AUTO_ROLES = {
    # Read-only watchers — worst-case is an unnecessary escalation;
    # safe to autonomously trigger.
    "content-scout",
    "parser-custodian",
}
RUNG_2_ADDITIONS = {
    # Opus judgment agents — mutate DB state (resolve/promote). Held
    # for rung 2+ until clean rung-1 cycles earn promotion.
    "disputed-quotes-reviewer",
    "vocabulary-curator",
}
RUNG_3_ADDITIONS = {
    # Pipeline Operator — spends money + touches the synthesis pipeline.
    # Held for rung 3+ within strict per-day ceilings (per D-064).
    "pipeline-operator",
}
ALL_KNOWN_ROLES = RUNG_1_AUTO_ROLES | RUNG_2_ADDITIONS | RUNG_3_ADDITIONS

# CURRENT_RUNG — the active autonomy rung. EDIT THIS AT PROMOTION
# (alongside the role manual + DECISIONS entry). Reading from a file
# was considered + rejected: the wrapper's structural-wall property
# depends on rung NOT being mutable by data the agent can write to.
CURRENT_RUNG = 1


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _audit_trigger(
    role: str,
    rung_outcome: str,
    *,
    reasoning: Optional[str] = None,
) -> None:
    """Record this trigger attempt to the agent_actions audit table.

    Per S-008 V0 / surface S-8: every orchestrator trigger attempt — allowed
    or denied — lands a row with rung_attempted + rung_outcome so post-hoc
    audit can confirm the rung gate fired correctly. Best-effort; never
    raises into the caller's hot path.
    """
    try:
        from parsers.agent_audit import record_agent_action  # noqa: PLC0415
    except Exception:
        return
    rung_label = "instructed" if is_instructed_mode() else f"rung-{CURRENT_RUNG}"
    record_agent_action(
        agent_role="orchestrator",
        action_name=f"trigger:{role}",
        action_argument_table="ops_heartbeats",
        action_argument_id=None,
        action_body={"role": role, "rung_outcome": rung_outcome},
        reasoning=reasoning,
        rung_attempted=rung_label,
        rung_outcome=rung_outcome,
    )


def _current_rung_allowed() -> set:
    """Return the set of roles allowed to be AUTONOMOUSLY triggered at
    the current rung. Instructed mode bypasses this — see is_instructed_mode."""
    if CURRENT_RUNG <= 0:
        return set()
    allowed = set(RUNG_1_AUTO_ROLES)
    if CURRENT_RUNG >= 2:
        allowed |= RUNG_2_ADDITIONS
    if CURRENT_RUNG >= 3:
        allowed |= RUNG_3_ADDITIONS
    return allowed


def is_instructed_mode() -> bool:
    """True iff the spawning context is the Mode B instructed-spawn
    script. Detection: the ZSPAN_ORCHESTRATOR_REPLY_THREAD_TS env var
    is set ONLY by ops/orchestrator-instructed-spawn.ps1 (per D-072 /
    D-071). Heartbeat-mode + cron-mode don't set it.

    Per D-061: instructed execution is NOT gated by rung; James's
    instruction authorizes the action regardless of where the
    autonomy ladder currently sits.
    """
    return bool((os.environ.get("ZSPAN_ORCHESTRATOR_REPLY_THREAD_TS") or "").strip())


def resolve_heartbeat_script(role: str) -> Path:
    """The hardcoded path computation. Cannot be redirected by the agent.

    Post-D-120 (Mac launchd port): resolves the ONE parametrized Mac runner
    (ops/fleet_heartbeat.py), which takes --role. The PC era resolved a per-role
    `{role}-heartbeat.ps1`; the role still gates which agent fires (validated
    against ALL_KNOWN_ROLES + the rung wall in main)."""
    return _OPS_DIR / "fleet_heartbeat.py"


def spawn_heartbeat(role: str, script_path: Path) -> tuple[Optional[int], str]:
    """Spawn the Mac fleet runner (ops/fleet_heartbeat.py --role <role>) detached.
    Returns (pid, log_path). Detached so this wrapper returns immediately + the
    orchestrator session continues without blocking on the sub-agent's run.

    Post-D-120: the runner fires `claude -p` through the D-119 metering gate; the
    PC era shelled to powershell.exe + a per-role `.ps1`. POSIX detachment
    (start_new_session) replaces the Windows DETACHED_PROCESS creationflags."""
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    log_path = _LOGS_DIR / f"{stamp}-trigger-{role}.log"

    cmd = [sys.executable, str(script_path), "--role", role]
    log(f"  spawning: {' '.join(cmd)}")
    log(f"  log:      {log_path}")
    # Open the log file ourselves + redirect the child's stdout/stderr into it;
    # a detached process otherwise loses its output. start_new_session puts the
    # child in its own session so it outlives this short-lived wrapper.
    try:
        log_fh = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            cwd=str(_REPO_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        return proc.pid, str(log_path)
    except Exception as exc:
        return None, f"spawn raised {type(exc).__name__}: {exc}"


def _post_to_callback(args: argparse.Namespace) -> int:
    """POST trigger args to PC's agent relay (D-099 Phase 2.1b).

    Mac orchestrator cannot spawn PC PowerShell heartbeats. Routing the
    trigger through PC's relay keeps the canonical heartbeat-spawn pattern
    on PC; the relay forwards to this same wrapper locally without
    --http-callback so the rung-gate + script-exists walls still fire.
    """
    payload = {
        "role": args.role,
        "dry_run": bool(args.dry_run),
    }
    headers = {"Content-Type": "application/json"}
    if args.http_bearer:
        headers["Authorization"] = f"Bearer {args.http_bearer}"
    req = urllib.request.Request(
        args.http_callback,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(body)
                stdout = parsed.get("stdout") or body
            except json.JSONDecodeError:
                stdout = body
            print(stdout.rstrip())
            return 0 if resp.status == 200 else 5
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        print(f"ERROR: HTTP {e.code} from {args.http_callback}: {err_body[:300]}", file=sys.stderr)
        return 5
    except urllib.error.URLError as e:
        print(f"ERROR: could not reach {args.http_callback}: {e}", file=sys.stderr)
        return 5


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Autonomous sub-agent trigger for the Z-SPAN orchestrator. "
            "Spawns ops/fleet_heartbeat.py --role <role> detached (D-120 Mac "
            "runner, via the D-119 metering gate). Rung-gated per D-061 "
            "(bypassed in Mode B instructed execution)."
        ),
    )
    parser.add_argument(
        "--role", required=True, choices=sorted(ALL_KNOWN_ROLES),
        help="Sub-agent role to trigger.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help=(
            "Walk all rung-gate + script-exists walls but do NOT actually "
            "spawn the heartbeat. Reports what would happen + exits 0 if "
            "the spawn would be authorized."
        ),
    )
    parser.add_argument(
        "--http-callback", default=None,
        help=(
            "Optional, VESTIGIAL post-D-111/D-120: POST args to the (retired) "
            "PC agent relay instead of spawning locally. The Mac now runs the "
            "fleet natively (local spawn), so nothing passes this. Kept as "
            "preserved-as-reference for the D-099 Phase 2.1b cross-machine path."
        ),
    )
    parser.add_argument(
        "--http-bearer", default=None,
        help="Bearer token paired with --http-callback.",
    )
    args = parser.parse_args()

    if args.http_callback:
        return _post_to_callback(args)

    log(f"=== orchestrator_trigger_agent --role {args.role} ===")
    log(f"  current rung: {CURRENT_RUNG}")
    log(f"  instructed mode: {is_instructed_mode()}")

    # Wall 1: role must be a known sub-agent role.
    if args.role not in ALL_KNOWN_ROLES:
        log(f"DENIED: role {args.role!r} not in ALL_KNOWN_ROLES")
        _audit_trigger(args.role, "rejected-unknown-role")
        return 2

    # Wall 2: rung gating (unless Mode B instructed — D-061).
    if not is_instructed_mode():
        allowed = _current_rung_allowed()
        if args.role not in allowed:
            log(
                f"DENIED: autonomous triggering of {args.role!r} requires "
                f"rung > {CURRENT_RUNG}. Allowed at current rung: "
                f"{sorted(allowed)}. "
                f"Promotion = edit CURRENT_RUNG/RUNG_*_ADDITIONS + role "
                f"manual + DECISIONS entry per D-061's ladder."
            )
            _audit_trigger(args.role, "rejected-out-of-rung")
            return 3
        log(f"  rung-1 allows autonomous trigger of {args.role!r}")
    else:
        log(
            f"  Mode B (instructed): rung gate bypassed per D-061; "
            f"firing {args.role!r}"
        )

    # Wall 3: heartbeat script must exist.
    script_path = resolve_heartbeat_script(args.role)
    if not script_path.is_file():
        log(f"DENIED: heartbeat script not found at {script_path}")
        _audit_trigger(args.role, "rejected-script-missing")
        return 4

    if args.dry_run:
        log(f"  DRY-RUN: would spawn {script_path}")
        print(
            f"DRY-RUN OK role={args.role} script={script_path.name} "
            f"mode={'instructed' if is_instructed_mode() else 'autonomous'} "
            f"rung={CURRENT_RUNG}"
        )
        _audit_trigger(args.role, "dry-run-allowed")
        return 0

    # Wall 4: spawn detached + return immediately.
    pid, log_path = spawn_heartbeat(args.role, script_path)
    if pid is None:
        log(f"SPAWN FAILED: {log_path}")
        _audit_trigger(args.role, "spawn-failed", reasoning=log_path)
        return 5

    log(f"OK spawned pid={pid} log={log_path}")
    print(
        f"triggered role={args.role} pid={pid} "
        f"mode={'instructed' if is_instructed_mode() else 'autonomous'} "
        f"rung={CURRENT_RUNG} log={Path(log_path).name}"
    )
    _audit_trigger(args.role, "spawned", reasoning=f"pid={pid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
