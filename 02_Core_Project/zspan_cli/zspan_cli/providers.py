"""Provider matrix — what each API key unlocks in the local pipeline.

Re-scoped from the hosted bring-your-own-key posture for a local CLI:
there is no relay and no CORS wall here, so every provider is called
directly and the key never touches Z-SPAN. The matrix is static local
data — provider guidance must not depend on the flagship being
reachable.

Transcription runs LOCALLY by default: `zspan process` ships with a
local Whisper model, free, so ANY single key — including a free Gemini
key — runs the pipeline end-to-end at zero cost. The
`cloud_transcription` flag
below marks providers whose key can OPTIONALLY speed transcription up
via their paid API (whisper-1); it is a speed opt-in, never the floor.
"""
from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

# Keyed by the canonical provider id used in config.json's api_keys map.
# default_model is the approved frontier fallback when a saved key has no
# model inventory. Config/CLI overrides still have to pass the civic-
# synthesis floor in processing.py; economy tiers are never a fallback.
PROVIDERS: Dict[str, Dict] = {
    "gemini": {
        "label": "Google Gemini (AI Studio)",
        "key_url": "https://aistudio.google.com/app/apikey",
        "key_prefix_hint": "AIza",
        "synthesis": True,
        "cloud_transcription": False,
        "default_model": "gemini-2.5-pro",
        "cost_note": "the key must reach a Gemini Pro tier for civic synthesis",
    },
    "openai": {
        "label": "OpenAI",
        "key_url": "https://platform.openai.com/api-keys",
        "key_prefix_hint": "sk-",
        "synthesis": True,
        "cloud_transcription": True,  # whisper-1 — optional speed upgrade over local
        "default_model": "gpt-4.1",
        "cost_note": (
            "pay-as-you-go: a GPT-4.x/GPT-5.x flagship tier handles synthesis; "
            "optional cloud transcription ~$0.36 per meeting-hour"
        ),
    },
    "anthropic": {
        "label": "Anthropic Claude",
        "key_url": "https://console.anthropic.com/settings/keys",
        "key_prefix_hint": "sk-ant-",
        "synthesis": True,
        "cloud_transcription": False,
        "default_model": "claude-sonnet-4-6",  # the flagship's own synthesis tier
        "cost_note": "pay-as-you-go; the premium-quality synthesis tier",
    },
}

# The provider `zspan init` suggests first. Gemini is the default because
# its free tier + local transcription means the whole pipeline runs at
# zero cost with one key. Local transcription removed the old
# whisper-1-requires-OpenAI floor that used to compromise this.
DEFAULT_PROVIDER = "gemini"

TRANSCRIPTION_NOTE = (
    "Transcription runs locally on your machine, free — no key needed for "
    "it, just patience (roughly real-time on an ordinary laptop). An "
    "OpenAI key can optionally speed it up through their whisper-1 cloud "
    "service (~$0.36 per hour of meeting audio). Any single key runs the "
    "pipeline end-to-end."
)


# ---------------------------------------------------------------- codex

# The installed Codex CLI as a keyless synthesis engine — the
# bring-your-own-AI rung. Not in PROVIDERS: it has no API key, no
# validation ping, and appears only where the binary actually exists.
CODEX_PROVIDER_ID = "codex"
CODEX_DEFAULT_MODEL = "gpt-5.6-sol"   # highest tier verified reachable on this CLI


def _codex_candidate_paths(config: Optional[Dict[str, Any]] = None) -> tuple[str, ...]:
    """Executable candidates in priority order.

    Finder-launched ``.command`` processes do not inherit a login shell's
    PATH, so a bare ``shutil.which("codex")`` loses npm/nvm installs. Keep
    PATH first for normal terminal runs, then probe standard user/system
    install locations. ``codex_binary`` is an optional explicit override.
    """
    if config is None:
        try:
            from zspan_cli.config import load_config
            config = load_config() or {}
        except Exception:
            config = {}
    home = Path.home()
    candidates: list[Path] = []
    override = str((config or {}).get("codex_binary") or "").strip()
    if override:
        override_path = Path(override).expanduser()
        if override_path.is_absolute() or override_path.parent != Path("."):
            candidates.append(override_path)
        else:
            override_hit = shutil.which(override)
            if override_hit:
                candidates.append(Path(override_hit))

    path_hit = shutil.which("codex")
    if path_hit:
        candidates.append(Path(path_hit))

    candidates.extend([
        home / ".npm-global" / "bin" / "codex",
        home / ".local" / "bin" / "codex",
        home / ".nvm" / "current" / "bin" / "codex",
        Path("/usr/local/bin/codex"),
        Path("/opt/homebrew/bin/codex"),
    ])
    candidates.extend(sorted(
        (home / ".nvm" / "versions" / "node").glob("*/bin/codex"),
        reverse=True,
    ))

    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        text = str(candidate)
        if text not in seen:
            seen.add(text)
            unique.append(text)
    return tuple(unique)


@lru_cache(maxsize=32)
def _resolve_codex_candidates(candidates: tuple[str, ...]) -> Optional[str]:
    for raw in candidates:
        path = Path(raw)
        if path.is_file() and os.access(path, os.X_OK):
            # Keep an nvm shim's own absolute path instead of resolving its
            # symlink to codex.js: sibling ``node`` must remain discoverable.
            return str(path.absolute())
    return None


def resolve_codex_binary(
    config: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Absolute Codex executable path, cached for this launch context."""
    return _resolve_codex_candidates(_codex_candidate_paths(config))


def codex_available(config: Optional[Dict[str, Any]] = None) -> bool:
    return resolve_codex_binary(config) is not None


def codex_unavailable_message(config: Optional[Dict[str, Any]] = None) -> str:
    checked = ["PATH", *_codex_candidate_paths(config)]
    nvm_pattern = str(Path.home() / ".nvm" / "versions" / "node" / "*/bin/codex")
    if not any("/.nvm/versions/node/" in item for item in checked):
        checked.append(nvm_pattern)
    return (
        "the Codex CLI isn't reachable from this launch context "
        f"(checked {', '.join(checked)}). Set `codex_binary` in config.json "
        "to its absolute path if it is installed somewhere else."
    )


# ---------------------------------------------------------------- strongest-reachable
#
# Default-strongest-reachable. An A/B across model tiers showed the
# quality cutline is a model-tier property: paying more at the cost floor
# pads the bill, while the frontier tier holds. The default synthesis model is the
# strongest the user's key ACTUALLY reaches — ranked empirically against
# the key's own list-models response (saved at init), never a hardcoded
# lineup trusted blind. Explicit --model / config synthesis_model stay
# the cost-opt-down.

# Per provider: explicitly tested civic-synthesis tiers, strongest first.
# Economy families do not appear here: they are rejected, not ranked last.
_PREFERENCE_TIERS = {
    "openai": (
        r"^gpt-5(?:\.\d+)?(?:-[a-z0-9.]+)*$",
        r"^gpt-4(?:\.\d+|o)?(?:-[a-z0-9.]+)*$",
    ),
    "anthropic": (
        r"^claude-sonnet-.*$|^claude-\d.*sonnet.*$",
    ),
    "gemini": (
        r"^gemini-\d+(\.\d+)?-pro.*$",
    ),
}

_ECONOMY_TIER_RE = r"(?:^|[-_.])(mini|nano|flash|haiku)(?:$|[-_.])"
_APPROVED_TIER_DESCRIPTION = (
    "Anthropic Claude Sonnet, OpenAI GPT-4.x/GPT-5.x flagship "
    "(not mini/nano), or Gemini Pro"
)

# Ids that look like chat models but aren't synthesis surfaces.
_EXCLUDE_MARKERS = ("embedding", "embed", "tts", "audio", "whisper",
                    "image", "vision-only", "moderation", "realtime",
                    "transcribe", "dall-e", "veo", "imagen", "-live",
                    "search", "computer-use", "instruct")


def _version_key(model_id: str):
    """Within-tier ordering: numeric version first (4.1 beats 4o's bare
    4), then the id string as the tiebreaker — 'gpt-4.1' > 'gpt-4o',
    'gpt-5.2' > everything 4.x, 'claude-sonnet-4-6' > '-4-5'."""
    import re
    nums = [int(n) for n in re.findall(r"\d+", model_id)[:3]]
    nums += [0] * (3 - len(nums))
    return (*nums, model_id)


def is_approved_synthesis_model(provider: str, model_id: str) -> bool:
    """Whether a model is inside the tested civic-synthesis floor."""
    import re

    provider = (provider or "").strip().lower()
    model = (model_id or "").strip().lower()
    if provider == CODEX_PROVIDER_ID:
        return model == CODEX_DEFAULT_MODEL
    if not model or re.search(_ECONOMY_TIER_RE, model):
        return False
    return any(re.match(tier, model) for tier in _PREFERENCE_TIERS.get(provider, ()))


def model_floor_message(provider: str, model_id: str = "") -> str:
    """Process-time guidance for a configured or reachable sub-floor tier."""
    named = f"model '{model_id}'" if model_id else f"the models reachable by {provider}"
    return (
        f"{named} does not meet Z-SPAN's civic-synthesis model floor. "
        f"Accepted key-based tiers are {_APPROVED_TIER_DESCRIPTION}. "
        f"Install the Codex CLI for the keyless {CODEX_DEFAULT_MODEL} path, "
        "or supply a key that reaches one of those flagship tiers. "
        "Economy tiers (mini, nano, flash, and haiku) are not accepted."
    )


def strongest_reachable(provider: str, model_ids) -> str:
    """Strongest approved model in the key's list.

    An absent inventory uses the provider's approved static fallback. A
    present inventory with no approved tier returns ``""`` so processing
    can stop with actionable model-floor guidance instead of opting down.
    """
    fallback = PROVIDERS.get(provider, {}).get("default_model", "")
    ids = [m for m in (model_ids or [])
           if m and not any(x in m.lower() for x in _EXCLUDE_MARKERS)]
    if not ids:
        return fallback
    import re
    for tier in _PREFERENCE_TIERS.get(provider, ()):  # strongest tier first
        matches = [m for m in ids
                   if re.match(tier, m)
                   and is_approved_synthesis_model(provider, m)]
        if matches:
            return max(matches, key=_version_key)
    return ""


def provider_ids() -> List[str]:
    return list(PROVIDERS.keys())


def cloud_transcription_providers() -> List[str]:
    """Providers whose key offers the optional cloud-speed transcription
    path — consulted when resolving the opt-in flag."""
    return [pid for pid, p in PROVIDERS.items() if p["cloud_transcription"]]


def matrix_lines() -> List[str]:
    """The matrix rendered as plain sentences — words a person reads, not
    a schema dump. One block per provider + the transcription note."""
    lines: List[str] = []
    for pid, p in PROVIDERS.items():
        covers = "synthesis"
        if p["cloud_transcription"]:
            covers += " + optional cloud transcription (speed)"
        lines.append(f"{p['label']}  ({pid})")
        lines.append(f"  covers: {covers}")
        lines.append(f"  cost:   {p['cost_note']}")
        lines.append(f"  get a key: {p['key_url']}")
        lines.append("")
    lines.append(TRANSCRIPTION_NOTE)
    return lines
