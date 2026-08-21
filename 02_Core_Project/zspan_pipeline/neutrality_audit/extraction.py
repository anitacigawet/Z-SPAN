"""Stage-1 extraction: the one cheap constrained LLM call, run twice.

Two independent model families read the identical prompt (prompts/votes.md
instructions + the raw transcript) and emit typed Vote frames. The model is
the only variable — that's what makes cross-family convergence a signal
(probe-2 methodology, S-133). Family A rides the production `claude -p`
invocation (MAX-cap absorbed, per D-119/D-126); family B is gpt-4o-mini at
temperature 0 with a fixed seed via the operator's existing OpenAI key.

Deliberate v0.1 deviation from the bridge-era votes.md contract: the city
persona preamble + vocabulary-corrections blocks are NOT prepended, so
per-member names may arrive non-canonical. The deterministic layer therefore
treats member names softly (enum checks on the vote value, no roster check).

Oversized transcripts (multi-hour Phoenix/LHC meetings) are extracted in
overlapping segments and merged: gpt-4o-mini's context window is the binding
constraint, and truncation would silently un-ground tail-of-meeting votes.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

import requests

from zspan_pipeline.prompt_loader import load_prompt_with_meta
from zspan_pipeline.qdrant_synthesizer import SONNET_MODEL_ID, synthesize_via_claude_p

from .deterministic import (_frame_search_text, _frame_topic_text, _jaccard,
                            content_tokens, extract_refs)

logger = logging.getLogger(__name__)

VOTES_PROMPT_FILE = "votes.md"
OPENAI_MODEL_ID = "gpt-4o-mini"
OPENAI_SEED = 42

SEGMENT_WORDS = 55_000      # ≈73k tokens of transcript; safe under 128k with instructions
SEGMENT_OVERLAP_WORDS = 1_500
DEDUPE_JACCARD = 0.6

# gpt-4o-mini demonstrably under-extracts on very long contexts (m103996:
# 2 frames against 16 vote moments at 35k words, JSON completed cleanly) —
# shorter reads per call fix it with the same segmentation machinery
FAMILY_SEGMENT_WORDS = {"claude": SEGMENT_WORDS, "openai": 15_000}

_THIS_DIR = Path(__file__).resolve().parent
CORE_PROJECT_DIR = _THIS_DIR.parents[1]
USER_SETTINGS_PATH = CORE_PROJECT_DIR / "council_navigator" / "parsers" / "user_settings.json"


def load_votes_instructions() -> str:
    """votes.md instruction body via the canonical loader. The loader's
    generic '## Instructions' marker leaves a '(sent to NotebookLM)' residue
    line (the S-128 cosmetic finding); strip it caller-side — the prompt
    file itself is James's and stays untouched."""
    _, body = load_prompt_with_meta(VOTES_PROMPT_FILE)
    body = re.sub(r"^\(sent[^)]*\)\s*", "", body.lstrip())
    return body.strip()


def build_prompt(instructions: str, transcript_text: str) -> str:
    return instructions + "\n\n===== MEETING TRANSCRIPT =====\n" + transcript_text


def _openai_key() -> str:
    key = json.loads(USER_SETTINGS_PATH.read_text()).get("openai_api_key")
    if not key:
        raise RuntimeError(f"openai_api_key missing from {USER_SETTINGS_PATH}")
    return key


def call_claude(prompt: str, *, timeout: float = 420.0) -> str:
    return synthesize_via_claude_p(prompt, model=SONNET_MODEL_ID, timeout_seconds=timeout)


def call_openai(prompt: str, *, timeout: float = 300.0) -> str:
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {_openai_key()}",
                 "Content-Type": "application/json"},
        json={"model": OPENAI_MODEL_ID, "temperature": 0, "seed": OPENAI_SEED,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=timeout,
    )
    payload = resp.json()
    if "choices" not in payload:
        raise RuntimeError(f"OpenAI error: {json.dumps(payload)[:300]}")
    return payload["choices"][0]["message"]["content"]


FAMILIES: dict[str, tuple[str, Callable[..., str]]] = {
    "claude": (SONNET_MODEL_ID, call_claude),
    "openai": (OPENAI_MODEL_ID, call_openai),
}


def parse_frames(raw: str) -> tuple[list[dict[str, Any]], Optional[str], bool]:
    """Returns (frames, extraction_notes, parse_ok). Tolerates fenced JSON
    and leading/trailing prose; per the F8 discipline a parse failure is
    reported as failed-silent, never coerced to an empty success."""
    body = raw
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if fenced:
        body = fenced.group(1)
    else:
        brace = body.find("{")
        if brace > 0:
            body = body[brace:]
        end = body.rfind("}")
        if end != -1:
            body = body[: end + 1]
    try:
        data = json.loads(body)
    except Exception as exc:
        logger.warning("vote-frame JSON parse failed: %s", exc)
        return [], f"parse_failed: {exc}", False
    frames = data.get("votes", [])
    if not isinstance(frames, list):
        return [], "parse_failed: votes not a list", False
    frames = [f for f in frames if isinstance(f, dict)]
    for f in frames:
        tally = f.get("tally")
        if isinstance(tally, dict):
            for k, v in list(tally.items()):
                if isinstance(v, str) and v.isdigit():
                    tally[k] = int(v)
    return frames, data.get("extraction_notes"), True


def _segments(words: list[str], segment_words: int = SEGMENT_WORDS) -> list[list[str]]:
    if len(words) <= segment_words:
        return [words]
    segs, start = [], 0
    while start < len(words):
        segs.append(words[start: start + segment_words])
        if start + segment_words >= len(words):
            break
        start += segment_words - SEGMENT_OVERLAP_WORDS
    return segs


def _dedupe(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Overlap-merge: frames from adjacent segments describing the same vote
    collapse to the first occurrence (ref match or high token overlap)."""
    kept: list[dict[str, Any]] = []
    kept_refs: list[set[str]] = []
    kept_toks: list[set[str]] = []
    for f in frames:
        refs = extract_refs(_frame_search_text(f))
        toks = content_tokens(_frame_topic_text(f))
        dup = False
        for i in range(len(kept)):
            if refs and kept_refs[i] and (refs & kept_refs[i]) \
               and f.get("vote_result") == kept[i].get("vote_result"):
                dup = True
                break
            if _jaccard(toks, kept_toks[i]) >= DEDUPE_JACCARD:
                dup = True
                break
        if not dup:
            kept.append(f)
            kept_refs.append(refs)
            kept_toks.append(toks)
    return kept


def extract_frames(words: list[str], family: str, *, pace_seconds: float = 3.0,
                   timeout: float = 420.0) -> dict[str, Any]:
    """Run one family over a full transcript (segmenting if oversized).
    Returns {frames, raw, segments, parse_ok, notes, model, seconds}."""
    model_id, call = FAMILIES[family]
    instructions = load_votes_instructions()
    segs = _segments(words, FAMILY_SEGMENT_WORDS.get(family, SEGMENT_WORDS))
    all_frames: list[dict[str, Any]] = []
    raws: list[str] = []
    notes: list[str] = []
    ok = True
    t0 = time.time()
    for n, seg in enumerate(segs):
        if n > 0 and pace_seconds:
            time.sleep(pace_seconds)
        prompt = build_prompt(instructions, " ".join(seg))
        logger.info("[%s] extracting segment %d/%d (%d words)",
                    family, n + 1, len(segs), len(seg))
        raw = call(prompt, timeout=timeout)
        raws.append(raw)
        frames, note, parse_ok = parse_frames(raw)
        ok = ok and parse_ok
        if note:
            notes.append(note)
        all_frames.extend(frames)
    merged = _dedupe(all_frames) if len(segs) > 1 else all_frames
    return {
        "family": family,
        "model": model_id,
        "frames": merged,
        "frames_pre_dedupe": len(all_frames),
        "segments": len(segs),
        "parse_ok": ok,
        "notes": notes or None,
        "raw": raws,
        "seconds": round(time.time() - t0, 1),
    }
