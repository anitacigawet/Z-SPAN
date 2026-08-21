"""codex_cli_client — invoke OpenAI's Codex CLI as a programmatic LLM call site.

Companion to `mac_claude_relay_client.py` (which routes Claude calls to
the Mac via the relay daemon). This module wraps `codex exec` so the
project's gpt-4o-mini call sites (quote_cleaner / verdict_emphasis /
polish_for_display) can swap from per-call paid-API billing to the
operator's ChatGPT-Plus subscription that backs Codex CLI.

Per memory [[explicit-at-invocation-not-config-default-for-supervised-tools]]:
always pass `--model` and `model_reasoning_effort` explicitly. Never
rely on Codex's config.toml defaults as a backstop against silent
routing under rate-limits / cohort A/B / version drift.

S-144 capability isolation: this wrapper feeds UNTRUSTED transcript text
(dispute-flagged quotes, meeting text) into `codex exec`. Every invocation
runs with `--sandbox read-only` (denies write/exec/network — mirrors the
flagship Claude-path's `--tools ""`) and a sanitized env that strips
credential-shaped vars (mirrors `qdrant_synthesizer._sanitized_synth_env`).
Together they close the two most-common prompt-injection exfil channels
(tool calls + env-var readback). The read-only file-read residual is
accepted-by-doctrine per S-144.

The subprocess writes ONLY the final agent message to a temp file via
`--output-last-message`, so we never have to parse Codex CLI's
human-readable section output (`user` / `codex` / `tokens used` blocks).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# Per [[explicit-at-invocation-not-config-default-for-supervised-tools]]:
# always-pass these explicitly. Codex CLI on ChatGPT-Plus auth tops out
# at gpt-5.5; do not silently fall through to whatever config.toml says.
DEFAULT_MODEL = "gpt-5.5"
DEFAULT_REASONING_EFFORT = "high"

# Per-call ceiling. Quote-cleaner-shaped tasks (~100-1000 input chars,
# expected ~150ms-3s of model thinking) usually return well under 60s.
# 180s gives headroom for the occasional cold-start + reasoning burst.
DEFAULT_TIMEOUT_SECONDS = 180


def _sanitized_codex_env() -> dict[str, str]:
    """os.environ minus credential-shaped vars, for the codex subprocess.

    S-144 capability isolation: `--sandbox read-only` already denies the codex
    agent every write/exec/network tool, but a prompt-injected model still
    runs in the calling process's environment — stripping credential-shaped
    vars closes the "echo $OPENAI_API_KEY"-style env-exfil channel
    deterministically. Codex authenticates from its own on-disk config
    (~/.codex, backing the operator's ChatGPT-Plus subscription), NOT from
    env vars, so removing API-key-shaped vars does not affect auth.

    Mirrors `qdrant_synthesizer._sanitized_synth_env` (the flagship
    Claude-path treatment) — same credential-shaped ruleset (5 suffixes +
    API_KEY-contains). The sibling `zspan_cli/synthesize._sanitized_codex_env`
    additionally strips provider-name prefixes (OPENAI_*, ANTHROPIC_*, …);
    kept parallel to the flagship shape here so the two subprocesses use the
    same conservative env-strip rule and residual routing-shaped env vars
    still pass through if a caller depends on them.

    Residual (honest, per S-144): read-only still permits file reads, so a
    determined injection could read a file-based secret. Fully closing that
    needs OS-level sandboxing beyond a stdlib wrapper — accepted by doctrine.
    """
    def _sensitive(name: str) -> bool:
        n = name.upper()
        return (
            n.endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIALS"))
            or "API_KEY" in n
        )
    return {k: v for k, v in os.environ.items() if not _sensitive(k)}


@dataclass
class CodexResult:
    """Outcome of a single codex exec invocation."""

    text: str
    model: str
    reasoning_effort: str
    duration_seconds: float
    error: Optional[str] = None  # populated when the invocation failed

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.text)


class CodexCLIError(RuntimeError):
    """Raised when codex exec fails in a way the caller should handle."""


def _resolve_codex_binary() -> str:
    """Find the codex binary. Caches the path on the function attribute
    so we only do the lookup once per process."""
    cached = getattr(_resolve_codex_binary, "_cached", None)
    if cached:
        return cached
    path = shutil.which("codex")
    if not path:
        # On the Mac with nvm-installed node, codex lives at
        # ~/.nvm/versions/node/<version>/bin/codex but the shell PATH
        # at non-interactive subprocess invocation time may not include
        # it. Fall back to the known nvm install path if present.
        home = os.path.expanduser("~")
        nvm_root = os.path.join(home, ".nvm", "versions", "node")
        if os.path.isdir(nvm_root):
            for v in sorted(os.listdir(nvm_root), reverse=True):
                candidate = os.path.join(nvm_root, v, "bin", "codex")
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    path = candidate
                    break
    if not path:
        raise CodexCLIError(
            "codex binary not found on PATH or in ~/.nvm/versions/node/*/bin. "
            "Install via `npm i -g @openai/codex` (Codex CLI) and ensure operator "
            "is signed into ChatGPT via `codex login`."
        )
    _resolve_codex_binary._cached = path  # type: ignore[attr-defined]
    return path


def invoke_codex(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    require_json_object: bool = False,
) -> CodexResult:
    """Invoke `codex exec` with the given system + user prompts.

    Codex CLI doesn't have a separate `system` parameter — both prompts
    are concatenated into one input. We use explicit section markers so
    the model can distinguish the two roles.

    When `require_json_object=True` we append an explicit JSON-only
    instruction. Codex doesn't support OpenAI's `response_format` knob,
    so this is the equivalent enforcement at the prompt layer.
    """
    binary = _resolve_codex_binary()

    parts = [
        "[SYSTEM]",
        system_prompt.strip(),
        "",
        "[USER]",
        user_prompt.strip(),
    ]
    if require_json_object:
        parts.extend([
            "",
            "[OUTPUT FORMAT]",
            "Respond with ONLY a single JSON object. No preamble, no markdown "
            "fences, no commentary before or after. Just the raw JSON.",
        ])
    combined = "\n".join(parts)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", prefix="codex_last_", delete=False
    ) as tf:
        out_path = tf.name

    cmd = [
        binary,
        "exec",
        "--model", model,
        "-c", f"model_reasoning_effort={reasoning_effort}",
        # S-144: deny write/exec/network on untrusted transcript input.
        # read-only still permits file reads (documented residual) and
        # --output-last-message writes to its own designated path.
        "--sandbox", "read-only",
        "--skip-git-repo-check",
        "--output-last-message", out_path,
        combined,
    ]

    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            # S-144: strip credential-shaped env vars before an
            # untrusted-input subprocess. See _sanitized_codex_env docstring.
            env=_sanitized_codex_env(),
        )
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        try:
            os.unlink(out_path)
        except OSError:
            pass
        return CodexResult(
            text="",
            model=model,
            reasoning_effort=reasoning_effort,
            duration_seconds=duration,
            error=f"codex exec timed out after {timeout_seconds}s",
        )

    duration = time.monotonic() - start

    if proc.returncode != 0:
        try:
            os.unlink(out_path)
        except OSError:
            pass
        tail = (proc.stderr or proc.stdout or "")[-400:]
        return CodexResult(
            text="",
            model=model,
            reasoning_effort=reasoning_effort,
            duration_seconds=duration,
            error=f"codex exec returned {proc.returncode}: {tail}",
        )

    try:
        text = ""
        if os.path.isfile(out_path):
            with open(out_path, "r", encoding="utf-8") as f:
                text = f.read().strip()
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass

    if not text:
        return CodexResult(
            text="",
            model=model,
            reasoning_effort=reasoning_effort,
            duration_seconds=duration,
            error="codex exec produced no output-last-message content",
        )

    return CodexResult(
        text=text,
        model=model,
        reasoning_effort=reasoning_effort,
        duration_seconds=duration,
    )


def invoke_codex_json(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[Optional[dict], CodexResult]:
    """Convenience: invoke + parse the result as a JSON object.

    Returns (parsed_dict_or_none, raw_result). The dict is None when the
    invocation failed OR when the response wasn't valid JSON; in either
    case `raw_result.error` is populated to explain why.

    Strips common LLM JSON-output corruptions (markdown fences) defensively
    even though we request raw JSON.
    """
    result = invoke_codex(
        system_prompt,
        user_prompt,
        model=model,
        reasoning_effort=reasoning_effort,
        timeout_seconds=timeout_seconds,
        require_json_object=True,
    )
    if not result.ok:
        return None, result

    body = result.text.strip()
    if body.startswith("```"):
        # Strip code fence: ``` or ```json
        lines = body.split("\n")
        if len(lines) >= 2:
            body = "\n".join(lines[1:])
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3].rstrip()

    try:
        parsed = json.loads(body)
        if not isinstance(parsed, dict):
            return None, CodexResult(
                text=result.text,
                model=result.model,
                reasoning_effort=result.reasoning_effort,
                duration_seconds=result.duration_seconds,
                error=f"response was JSON {type(parsed).__name__}, not object",
            )
        return parsed, result
    except json.JSONDecodeError as exc:
        return None, CodexResult(
            text=result.text,
            model=result.model,
            reasoning_effort=result.reasoning_effort,
            duration_seconds=result.duration_seconds,
            error=f"response was not valid JSON: {exc}",
        )


def health() -> dict:
    """Probe the wrapper: confirm binary is locatable + try a tiny
    invocation. Useful for setup checks + ops dashboards.
    """
    try:
        binary = _resolve_codex_binary()
    except CodexCLIError as exc:
        return {
            "ok": False,
            "binary": None,
            "error": str(exc),
        }

    result = invoke_codex(
        "You are a smoke test responder.",
        'Respond with the JSON {"ok": true} and nothing else.',
        timeout_seconds=60,
        require_json_object=True,
    )
    return {
        "ok": result.ok,
        "binary": binary,
        "model": result.model,
        "duration_seconds": round(result.duration_seconds, 2),
        "error": result.error,
        "response_preview": result.text[:120],
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(json.dumps(health(), indent=2))
