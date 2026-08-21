"""
mac_claude_relay_client — invoke claude -p on James's Mac from the PC.
======================================================================

PC-side client for the Mac Claude Relay service (per
`02_Core_Project/mac_claude_relay/`). Lets PC-side Z-SPAN Claude
maintainer drive Mac-side work directly without James copy-pasting
between two chat windows.

Usage (from any Python module on the PC side, e.g. parsers/ or an ad-hoc
script run via `python3.11`):

    from mac_claude_relay_client import invoke_mac_claude, health

    # Health check
    print(health())

    # Run claude -p on the Mac
    result = invoke_mac_claude(
        "Read 02_Core_Project/mac_transcriber/STATUS.json and tell me the "
        "current base_url + whether up is true.",
        working_dir="~/Desktop/zspan",
        timeout_seconds=120,
    )
    print(result["text"])         # claude -p's stdout (the response)
    print(result["exit_code"])    # 0 on success
    print(result["duration_s"])   # how long the Mac side took

    # Lower-level shell escape hatch (requires opt-in on Mac side)
    out = shell_on_mac("uname -a && sw_vers")
    print(out["stdout"])

Config — same shape as the transcription node:
- Mac base URL: read from `02_Core_Project/mac_claude_relay/STATUS.json`
  (committed by Mac-side Claude when the relay bootstrapped). The
  dispatcher reads STATUS.json each call so flipping the Mac's URL
  takes effect without a Flask restart.
- Bearer token: read from `parsers/user_settings.json:zspan_mac_relay_token`
  (operator-copied from the Mac's launchd plist EnvironmentVariables;
  NEVER committed to git).

Security: this client invokes claude -p on James's Mac with whatever
scope Mac Claude has by default. The bearer token is the primary line
of defense. Treat it like a deploy key. See
`02_Core_Project/mac_claude_relay/README.md` "Security model" for the
full posture.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import requests

from env_config import load_user_settings

logger = logging.getLogger(__name__)


# ── Exceptions ────────────────────────────────────────────────────────


class MacRelayError(Exception):
    """Base class for Mac Claude Relay client failures."""


class MacRelayConfigError(MacRelayError):
    """Raised when STATUS.json is missing / up=false / token unset."""


class MacRelayHTTPError(MacRelayError):
    """Raised on a non-200 response from the Mac relay endpoints."""


# ── Config resolution ────────────────────────────────────────────────


# Repo-relative path to the Mac relay handshake file.
# From parsers/mac_claude_relay_client.py: .parent.parent.parent / "mac_claude_relay"
# parsers/ -> council_navigator/ -> 02_Core_Project/
_MAC_RELAY_STATUS_PATH = (
    Path(__file__).resolve().parent.parent.parent / "mac_claude_relay" / "STATUS.json"
)


def _resolve_mac_relay_config() -> tuple[str, str]:
    """Resolve the Mac relay base URL + bearer token.

    Returns:
      (base_url, bearer_token)

    Raises:
      MacRelayConfigError if STATUS.json missing / up=false / token unset.

    Post-D-111 substrate consolidation (solo Mac): STATUS.json's auto-detected
    host_lan_ip resolves to the VPN-tunnel egress (e.g. ProtonVPN 10.2.0.x utun)
    which is correct for cross-machine dispatch but the kernel routing layer
    does NOT route same-host packets to a utun-bound listener (Connection
    refused or timeout depending on routing state).

    As of the S-090 fix 2026-06-25, `mac_claude_relay/server.py` now
    DUAL-BINDS — always 127.0.0.1 (loopback) plus the auto-detected or
    HOST-specified interface. So `zspan_mac_relay_local: "true"` is the
    correct default for any same-host client; the loopback path is
    guaranteed to work regardless of VPN state.

    The override knob `zspan_mac_relay_local` accepts:
      - true/1/yes/on  → loopback http://127.0.0.1:8766 (RECOMMENDED for
        same-host clients; always works post-S-090 dual-bind)
      - "<host>:<port>" or "<host>" string  → use that base directly
        (useful for cross-machine clients OR for diagnosing relay-side
        bind issues by hitting a specific interface)
    Falls through to STATUS.json otherwise. Token still from user_settings.json.
    Mirrors the `zspan_whisper_local` pattern; differs because whisper-node
    binds to 0.0.0.0 by default and mac-relay binds to a specific interface.
    """
    settings = load_user_settings()
    token = (settings.get("zspan_mac_relay_token") or "").strip()
    if not token:
        raise MacRelayConfigError(
            "zspan_mac_relay_token not set in user_settings.json — copy from "
            "Mac's launchd plist EnvironmentVariables (ZSPAN_MAC_RELAY_TOKEN). "
            "On Mac: `launchctl print user/$(id -u)/com.zspan.mac-relay | grep "
            "ZSPAN_MAC_RELAY_TOKEN`"
        )

    settings_local_raw = settings.get("zspan_mac_relay_local")
    if settings_local_raw is not None:
        settings_local = str(settings_local_raw).strip()
        lower = settings_local.lower()
        if lower in {"true", "1", "yes", "on"}:
            return "http://127.0.0.1:8766", token
        if settings_local and lower not in {"false", "0", "no", "off", ""}:
            # Explicit host or host:port override (the LAN-bound-relay case).
            if "://" in settings_local:
                return settings_local.rstrip("/"), token
            host_part = settings_local
            if ":" not in host_part:
                host_part = f"{host_part}:8766"
            return f"http://{host_part}", token

    if not _MAC_RELAY_STATUS_PATH.exists():
        raise MacRelayConfigError(
            f"mac_claude_relay STATUS.json not found at {_MAC_RELAY_STATUS_PATH}; "
            "has Mac-side Claude bootstrapped the relay yet? See "
            "02_Core_Project/mac_claude_relay/SETUP.md"
        )
    try:
        status = json.loads(_MAC_RELAY_STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise MacRelayConfigError(
            f"mac_claude_relay STATUS.json unreadable: {e}"
        ) from e

    if not status.get("up"):
        raise MacRelayConfigError(
            "mac_claude_relay STATUS.json reports up=false; check the Mac relay service"
        )
    base_url = (status.get("base_url") or "").rstrip("/")
    if not base_url:
        raise MacRelayConfigError(
            "mac_claude_relay STATUS.json missing base_url"
        )

    return base_url, token


# ── Default timeouts ────────────────────────────────────────────────


# Most claude -p invocations on the Mac for the kinds of tasks the relay
# handles (setup, debugging, housekeeping) finish in seconds to a few
# minutes. 50 min default ceiling (raised from 30 min on 2026-05-31 per
# James — the first real production task ran 24 min wall-clock for a
# straightforward audit-log addition; bigger tasks can plausibly take
# 30-45 min, so 50 gives genuine headroom). Mac side's INVOKE_TIMEOUT_SECONDS
# default should match (set via the relay-bump task that ran alongside this).
DEFAULT_INVOKE_TIMEOUT_SECONDS = 50 * 60

# Shell commands should be short. 5 min ceiling matches the Mac side.
DEFAULT_SHELL_TIMEOUT_SECONDS = 5 * 60


# ── Public API ───────────────────────────────────────────────────────


def health() -> dict:
    """Hit the Mac relay's /health endpoint. No auth required.

    Returns the JSON body. Useful for confirming the relay is reachable
    + getting current config (which endpoints are enabled, what the
    audit log path is, etc.).

    Raises:
      MacRelayConfigError if STATUS.json missing / up=false.
      MacRelayHTTPError on non-200 response.
      requests.RequestException on network failure (connection refused,
        timeout, DNS, etc. — common when PC + Mac aren't on the same LAN).
    """
    base_url, _ = _resolve_mac_relay_config()
    url = f"{base_url}/health"
    resp = requests.get(url, timeout=10)
    if resp.status_code != 200:
        raise MacRelayHTTPError(
            f"Mac relay /health returned HTTP {resp.status_code}: {resp.text[:500]}"
        )
    try:
        return resp.json()
    except ValueError as e:
        raise MacRelayHTTPError(
            f"Mac relay /health returned non-JSON: {resp.text[:500]}"
        ) from e


def invoke_mac_claude(
    prompt: str,
    *,
    working_dir: Optional[str] = None,
    allowed_tools: Optional[list[str]] = None,
    model: Optional[str] = None,
    settings: Optional[str] = None,
    timeout_seconds: int = DEFAULT_INVOKE_TIMEOUT_SECONDS,
) -> dict:
    """POST a prompt to the Mac relay; Mac runs claude -p locally; return result.

    Args:
      prompt: the prompt text to send to claude -p on the Mac.
      working_dir: optional Mac-side cwd for the claude -p process (e.g.,
        "~/Desktop/zspan" to operate inside the cloned repo).
        Defaults to the Mac user's home dir if not set.
      allowed_tools: optional list of tool names passed to claude -p as
        `--allowed-tools <comma-joined>`. Use this to scope what the Mac
        Claude session is allowed to do per-call (e.g.,
        `allowed_tools=["Read", "Bash"]` for a read-only-ish session).
        Defaults to whatever Mac Claude has globally allowed.
      model: optional model ID passed to claude -p as `--model <id>`
        (D-099 Phase 1). Use for example `claude-haiku-4-5-20251001` to
        pin a specific cheap Haiku call for S-036-style scrapes. Defaults
        to whatever Mac Claude has set globally.
      settings: optional path to a scope-locked settings.json passed as
        `--settings <path>` (D-099 Phase 1). Relative paths resolve
        relative to working_dir on the Mac side. The settings file must
        exist on the Mac side — usually that means it's committed in the
        repo (e.g., agents/haiku-html-scraper.settings.json).
      timeout_seconds: HTTP timeout. Matches the Mac side's default of
        50 min; bump up for known-long tasks, down for tight loops.

    Returns a dict:
      {
        "text":       <claude -p's stdout — the response>,
        "stderr":     <claude -p's stderr — usually empty>,
        "exit_code":  <0 on success>,
        "duration_s": <how long the Mac side spent on the claude -p call>,
      }

    Raises:
      MacRelayConfigError if STATUS.json missing / up=false / token unset.
      MacRelayHTTPError on non-200 from the Mac.
      requests.RequestException on network failure.
    """
    base_url, token = _resolve_mac_relay_config()
    url = f"{base_url}/invoke"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body: dict = {"prompt": prompt}
    if working_dir is not None:
        body["working_dir"] = working_dir
    if allowed_tools is not None:
        body["allowed_tools"] = list(allowed_tools)
    if model is not None:
        body["model"] = model
    if settings is not None:
        body["settings"] = settings

    logger.info(
        "mac relay: invoke POST %s (prompt_len=%d, cwd=%s, tools=%s, model=%s, settings=%s, timeout=%ds)",
        url, len(prompt), working_dir, allowed_tools, model, settings, timeout_seconds,
    )
    resp = requests.post(url, json=body, headers=headers, timeout=timeout_seconds)
    if resp.status_code != 200:
        raise MacRelayHTTPError(
            f"Mac relay /invoke returned HTTP {resp.status_code}: {resp.text[:500]}"
        )
    try:
        result = resp.json()
    except ValueError as e:
        raise MacRelayHTTPError(
            f"Mac relay /invoke returned non-JSON: {resp.text[:500]}"
        ) from e
    logger.info(
        "mac relay: invoke done exit=%s duration=%ss text_len=%d",
        result.get("exit_code"), result.get("duration_s"), len(result.get("text", "")),
    )
    return result


def shell_on_mac(
    cmd: str,
    *,
    working_dir: Optional[str] = None,
    timeout_seconds: int = DEFAULT_SHELL_TIMEOUT_SECONDS,
) -> dict:
    """POST a shell command to the Mac relay; Mac runs it via subprocess; return result.

    Requires the Mac side to have explicitly enabled the shell endpoint
    (ZSPAN_MAC_RELAY_ALLOW_SHELL=true in launchd plist). Returns HTTP 403
    if not enabled.

    Use for quick "check this file exists / read this small output" type
    of things where spinning up claude -p is overkill. For anything
    requiring reasoning, use invoke_mac_claude() instead.

    Args:
      cmd: the shell command to run on the Mac. Runs via subprocess
        with shell=True (so pipes, env-var expansion, etc. all work).
      working_dir: optional Mac-side cwd. Defaults to user home.
      timeout_seconds: HTTP timeout (default 5 min — shell commands
        shouldn't be long-running).

    Returns a dict:
      {
        "stdout":     <command stdout>,
        "stderr":     <command stderr>,
        "exit_code":  <subprocess returncode>,
        "duration_s": <how long the Mac side spent>,
      }

    Raises same as invoke_mac_claude().
    """
    base_url, token = _resolve_mac_relay_config()
    url = f"{base_url}/shell"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body: dict = {"cmd": cmd}
    if working_dir is not None:
        body["working_dir"] = working_dir

    logger.info(
        "mac relay: shell POST %s (cmd_len=%d, cwd=%s, timeout=%ds)",
        url, len(cmd), working_dir, timeout_seconds,
    )
    resp = requests.post(url, json=body, headers=headers, timeout=timeout_seconds)
    if resp.status_code == 403:
        raise MacRelayHTTPError(
            "Mac relay /shell endpoint disabled. Set "
            "ZSPAN_MAC_RELAY_ALLOW_SHELL=true in the Mac's launchd plist + reload."
        )
    if resp.status_code != 200:
        raise MacRelayHTTPError(
            f"Mac relay /shell returned HTTP {resp.status_code}: {resp.text[:500]}"
        )
    try:
        result = resp.json()
    except ValueError as e:
        raise MacRelayHTTPError(
            f"Mac relay /shell returned non-JSON: {resp.text[:500]}"
        ) from e
    logger.info(
        "mac relay: shell done exit=%s duration=%ss stdout_len=%d",
        result.get("exit_code"), result.get("duration_s"), len(result.get("stdout", "")),
    )
    return result


if __name__ == "__main__":
    # CLI smoke test:
    #   python3.11 mac_claude_relay_client.py             # health check
    #   python3.11 mac_claude_relay_client.py "<prompt>"  # invoke + print response
    import sys

    # Force UTF-8 stdout so Mac Claude responses containing emoji (✅, ⚠️, etc.)
    # don't crash with cp1252 encode errors when this runs on Windows. The
    # 2026-05-31 first-production-relay-call had EXIT 0 / DURATION 1465s
    # (Mac side fully succeeded + committed af789b2) but the PC-side print
    # of the response crashed on '✅' under default cp1252 — misleading
    # exit code 1 to the caller. This reconfigure makes that failure mode
    # impossible.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        # Older Python or non-text stream — best effort, fall through.
        pass

    logging.basicConfig(level=logging.INFO)

    try:
        if len(sys.argv) < 2:
            print("=== Mac Relay Health ===")
            print(json.dumps(health(), indent=2))
        else:
            prompt = sys.argv[1]
            print(f"=== Invoking Mac Claude with prompt: {prompt[:120]} ===")
            result = invoke_mac_claude(prompt)
            print(f"exit_code: {result['exit_code']}")
            print(f"duration_s: {result['duration_s']}")
            print(f"--- text ---")
            print(result["text"])
            if result.get("stderr"):
                print(f"--- stderr ---")
                print(result["stderr"])
    except MacRelayError as e:
        print(f"FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
    except requests.RequestException as e:
        print(f"NETWORK ERROR: {e}", file=sys.stderr)
        print(
            "PC + Mac may not be on the same network. Check STATUS.json's "
            "host_lan_ip + confirm reachability.",
            file=sys.stderr,
        )
        sys.exit(2)
