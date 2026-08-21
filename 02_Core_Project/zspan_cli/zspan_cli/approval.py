"""Per-output transparency and approval for interactive synthesis runs."""
from __future__ import annotations

import os
import re
import sys
from enum import Enum
from typing import Any, Callable, TextIO


class ApprovalDecision(Enum):
    PROCEED = "proceed"
    SKIP = "skip"
    ABORT_ALL = "abort_all"


YES_TO_ALL_ENV_VAR = "ZSPAN_SKIP_APPROVALS"

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def should_prompt(cli_flag_yes_to_all: bool) -> bool:
    """Whether an interactive approval should be shown for this run."""
    return not (
        cli_flag_yes_to_all or os.environ.get(YES_TO_ALL_ENV_VAR, "")
    )


def strip_display_ansi(text: str) -> str:
    """Remove terminal control sequences from display text only."""
    return _ANSI_ESCAPE_RE.sub("", text)


def _fmt_ts(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def render_chunk_review(
    *,
    output_type: str,
    chunk_index: int,
    chunk_total: int,
    retrieval_query: str,
    retrieved_chunks: list[Any],
    canonical_prompt: str,
    full_envelope: str,
    provider: str,
    model: str,
    key_fingerprint_str: str,
    out: TextIO | None = None,
) -> None:
    """Render the complete initial-call review without changing send bytes."""
    if out is None:
        out = sys.stdout

    header = "\n".join((
        "=" * 60,
        f" CHUNK {chunk_index} of {chunk_total} — {output_type}",
        "=" * 60,
        f"Provider:  {provider} · {key_fingerprint_str}",
        f"Model:     {model}",
    ))
    retrieval = "\n".join((
        "── Retrieval query ─────────────────────────────────────────",
        retrieval_query,
    ))

    chunk_blocks = [
        "\n".join((
            f"[{_fmt_ts(chunk.start_seconds)}]  "
            f"(chunk #{chunk.chunk_index}, score {chunk.score:.3f})",
            strip_display_ansi(chunk.text),
        ))
        for chunk in retrieved_chunks
    ]
    retrieved = (
        f"── Retrieved transcript chunks ({len(retrieved_chunks)} at cosine top-k) "
        "──────"
    )
    if chunk_blocks:
        retrieved += "\n" + "\n\n".join(chunk_blocks)

    canonical = "\n".join((
        f"── Canonical prompt (prompts/{output_type}.md) ─────────────",
        strip_display_ansi(canonical_prompt),
    ))
    envelope = "\n".join((
        "── Full envelope (verbatim — what ships to the provider) ───",
        strip_display_ansi(full_envelope),
    ))

    print(
        "\n\n".join((header, retrieval, retrieved, canonical, envelope, "═" * 60)),
        file=out,
    )


def prompt_decision(
    input_fn: Callable[[str], str] = input,
) -> ApprovalDecision:
    """Read an opt-in decision, defaulting safely on invalid or absent input."""
    prompts = [
        "Proceed with this chunk? [y/N/a=abort all]: ",
        *[
            "please answer y (yes) / n (skip) / a (abort all): "
            for _ in range(3)
        ],
    ]
    for prompt in prompts:
        try:
            answer = input_fn(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt, OSError):
            return ApprovalDecision.ABORT_ALL
        if answer in {"y", "yes"}:
            return ApprovalDecision.PROCEED
        if answer in {"", "n", "no"}:
            return ApprovalDecision.SKIP
        if answer in {"a", "abort", "q", "quit"}:
            return ApprovalDecision.ABORT_ALL
    return ApprovalDecision.SKIP


def approve_chunk(
    *,
    output_type: str,
    chunk_index: int,
    chunk_total: int,
    retrieval_query: str,
    retrieved_chunks: list[Any],
    canonical_prompt: str,
    full_envelope: str,
    provider: str,
    model: str,
    key_fingerprint_str: str,
    yes_to_all: bool = False,
    out: TextIO | None = None,
    input_fn: Callable[[str], str] = input,
) -> ApprovalDecision:
    """Render and request approval, or silently proceed when bypassed."""
    if not should_prompt(yes_to_all):
        return ApprovalDecision.PROCEED
    render_chunk_review(
        output_type=output_type,
        chunk_index=chunk_index,
        chunk_total=chunk_total,
        retrieval_query=retrieval_query,
        retrieved_chunks=retrieved_chunks,
        canonical_prompt=canonical_prompt,
        full_envelope=full_envelope,
        provider=provider,
        model=model,
        key_fingerprint_str=key_fingerprint_str,
        out=out,
    )
    return prompt_decision(input_fn)


def print_approval_intro(progress_fn: Callable[[str], None]) -> None:
    progress_fn(
        "Chunk-by-chunk review is ON. You will see 4 chunks, each with the "
        "transcript slice and the prompt. Press Enter (or n) to skip a chunk; "
        "press y to synthesize it; press a to abort all. Turn this off with "
        "--yes-to-all or ZSPAN_SKIP_APPROVALS=1."
    )
