"""
Flagship civic synthesizer — complete local evidence + frontier generation.

The V1 text-output generation: replaces the retired bridge with the
architecture per [D-126](../../01_Project_Overview/DECISIONS.md#d-126):

  1. Load every indexed chunk in chronological order and verify the local
     index still matches the canonical ``transcript_words`` hash.

  2. Build a synthesis prompt that gives the generator the complete chunks
     (with their karaoke-timecode metadata) + the canonical prompt
     template for the target output type.

  3. Invoke Gemini 3.1 Pro High through the isolated Agy wrapper. A typed
     failure may spend the work order's single automatic Opus 4.6 backstop
     attempt. Gemini 3.6 Flash High remains the last model-specific Google
     rung when Pro itself is unavailable; provider-wide Google failures skip
     it because a second call on the same dead account/transport is not a
     fallback.

  4. Return the synthesized text. The canonical fetcher.py path, or the
     retained regenerate_via_qdrant maintenance one-shot, persists it to
     the notebook_outputs cache + bumps generated_at + prompt_filename +
     prompt_version, matching the existing cache schema so the frontend
     renders the new content without any UI change.

This module is import-only — it has no CLI of its own. ``fetcher.py`` is
the canonical production caller. ``regenerate_via_qdrant.py`` is retained
as a lineage maintenance CLI for one output type + one meeting.

Composes [V1-RAG-1](../../01_Project_Overview/V1_RAG1_SURFACE_PRO_HANDOFF.md) +
[V1-RAG-2](council_navigator/parsers/scripts/index_meeting_to_qdrant.py) +
[surfacepro_rag_node](../surfacepro_rag_node/server.py) +
[S-033](../../01_Project_Overview/FUTURE_THOUGHTS.md#s-033).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .prompt_loader import strip_explicit_model_boundaries

logger = logging.getLogger(__name__)


class ClaudePError(RuntimeError):
    """A failed Claude CLI call with its diagnostic streams preserved."""

    def __init__(
        self,
        message: str,
        *,
        returncode: int | None,
        stdout: str,
        stderr: str,
        duration_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.duration_seconds = duration_seconds


class GeminiPError(RuntimeError):
    """A typed failure from the isolated Gemini subscription rung."""

    def __init__(self, failure_class: str, message: str) -> None:
        super().__init__(message)
        self.failure_class = failure_class


@dataclass(frozen=True)
class GenerationAttempt:
    """One provider/model attempt and its terminal classification."""

    provider: str
    model_id: str
    failure_class: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "failure_class": self.failure_class,
        }


@dataclass(frozen=True)
class GenerationResult:
    """Successful content plus the exact model and attempt trail."""

    content: str
    model_id: str
    attempts: tuple[GenerationAttempt, ...]


class GenerationPausedError(RuntimeError):
    """Terminal fail-closed state; already-written meeting work is preserved."""

    def __init__(
        self,
        failure_class: str,
        message: str,
        *,
        attempts: Sequence[GenerationAttempt],
    ) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.attempts = tuple(attempts)


@dataclass
class WorkOrderGenerationState:
    """Mutable provider circuit shared by every generation in one work order."""

    work_order_id: int | str
    opus_attempted: bool = False
    provider_account_failures: dict[str, str] = field(default_factory=dict)


_WORK_ORDER_GENERATION_STATE: ContextVar[WorkOrderGenerationState | None] = (
    ContextVar("zspan_work_order_generation_state", default=None)
)


@contextmanager
def work_order_generation_scope(work_order_id: int | str):
    """Bind one quota circuit to a complete top-level worker invocation.

    ``asyncio.to_thread`` copies context variables into its worker thread, so
    the serial fetcher passes and the later sidecar pass see this same mutable
    object. Nested use for the same work order is harmless; crossing work-order
    identities in one context fails closed rather than sharing quota state.
    """
    existing = _WORK_ORDER_GENERATION_STATE.get()
    if existing is not None:
        if existing.work_order_id != work_order_id:
            raise RuntimeError(
                "cannot nest generation scopes for different work orders: "
                f"{existing.work_order_id!r} != {work_order_id!r}"
            )
        yield existing
        return

    state = WorkOrderGenerationState(work_order_id=work_order_id)
    token = _WORK_ORDER_GENERATION_STATE.set(state)
    try:
        yield state
    finally:
        _WORK_ORDER_GENERATION_STATE.reset(token)


# Retired compatibility defaults.  Public call signatures still accept these
# arguments, but flagship retrieval is in-process and does not resolve or use
# RAG-node environment/settings plumbing.
DEFAULT_RAG_NODE_HOST = "retired-in-process"
DEFAULT_RAG_NODE_PORT = 0

# Independent low-level helpers may still request Sonnet explicitly. It is not
# an active rung in the flagship generation chain.
SONNET_MODEL_ID = "claude-sonnet-4-6"

# Session-107 corrected reassignment: use the idle Google subscription first,
# then the tournament-winning Opus model before the lower-quality Flash rung.
# Fable remains outside the pipeline because it is the operator's direction
# model; Sonnet is not a flagship backstop.
GEMINI_PRIMARY_MODEL_ID = "gemini-3.1-pro-high"
GEMINI_BACKUP_MODEL_ID = "gemini-3.6-flash-high"
OPUS_BACKSTOP_MODEL_ID = "claude-opus-4-6"
FLAGSHIP_MODEL_ID = GEMINI_PRIMARY_MODEL_ID
GEMINI_EFFORT = "high"
OPUS_BACKSTOP_EFFORT = "max"
GEMINI_PROMPT_BYTE_LIMIT = 400_000

MODEL_UNAVAILABLE = "model_unavailable"
ACCOUNT_LIMIT = "account_limit"
AUTH_FAILURE = "auth_failure"
TRANSIENT_NETWORK = "transient_network"
TIMEOUT = "timeout"
UNKNOWN_FAILURE = "unknown"
PROMPT_TOO_LARGE_FOR_GEMINI = "prompt_too_large_for_gemini_transport"
GEMINI_CLI_UNAVAILABLE = "gemini_cli_unavailable"
OPUS_WORK_ORDER_BUDGET_EXHAUSTED = "opus_work_order_budget_exhausted"

# Historical producer identities stay accepted so valid Fable/Sonnet artifacts
# do not become stale merely because those models left the active chain.
CURRENT_GENERATION_MODEL_IDS = frozenset({
    GEMINI_PRIMARY_MODEL_ID,
    GEMINI_BACKUP_MODEL_ID,
    OPUS_BACKSTOP_MODEL_ID,
    SONNET_MODEL_ID,
    "claude-fable-5",
})

# Worker synthesis now carries the full canonical prompt (roughly 10x the
# former prompt-loader slice).  Match the decisions sidecar's production-
# proven ceiling while retaining an operator override for unusually slow or
# constrained hosts.
SYNTHESIS_TIMEOUT_ENV = "ZSPAN_SYNTHESIS_TIMEOUT_SECONDS"
DEFAULT_SYNTHESIS_TIMEOUT_SECONDS = 900.0

# Claude Code may split a long answer across assistant messages when its
# per-turn output budget is exhausted. In `--output-format text`, only the last
# continuation reaches stdout, which makes a complete JSON object look as if
# its front was truncated. Large structured-output callers can raise the limit.
CLAUDE_MAX_OUTPUT_TOKENS_ENV = "CLAUDE_CODE_MAX_OUTPUT_TOKENS"

# Sentinel meaning "no wall-clock cap at all" — distinct from None, which means
# "use the configured default". Pass this for calls whose input size makes the
# runtime legitimately unbounded (e.g. a whole-meeting identity pass).
NO_TIMEOUT = "__no_timeout__"

# Path to the prompts directory (relative to this module). Caller can
# override; this is the canonical Z-SPAN prompt location.
_THIS_DIR = Path(__file__).resolve().parent
DEFAULT_PROMPTS_DIR = _THIS_DIR.parent / "prompts"
PREVIEW_DIR = _THIS_DIR.parent.parent / ".preview"


# Whole-meeting artifacts use the complete chronological transcript. Query-
# shaped surfaces keep their own semantic retrieval calls outside this set.
WHOLE_MEETING_OUTPUT_TYPES = frozenset({
    "key_decisions",
    "community_calls_to_action",
    "synopsis",
    "newsletter",
    "tracked_claims",
    "council_sentiment",
    "whats_next",
    "episode_tagline",
})


@dataclass
class RetrievedChunk:
    """One chunk returned by the Surface Pro /query endpoint.

    Phase 2 D5 (2026-06-24): optional `speaker_turns` populated when the
    meeting was diarized at indexing time (D4 indexer + D7 worker integration).
    Each entry is `{speaker_label, start, end, text}` representing one
    contiguous same-speaker run inside this chunk's time window. Pre-
    diarization meetings have `speaker_turns=None` and the formatter falls
    back to the undiarized body rendering.
    """

    score: float
    body: str
    chunk_index: int
    start_seconds: float
    end_seconds: float
    meeting_id: int
    city: str
    county: str
    state: str
    speaker_turns: Optional[list[dict[str, Any]]] = None

    @classmethod
    def from_hit(cls, hit: dict[str, Any]) -> "RetrievedChunk":
        payload = hit.get("payload", {}) or {}
        raw_turns = payload.get("speaker_turns")
        speaker_turns: Optional[list[dict[str, Any]]] = None
        if isinstance(raw_turns, list) and raw_turns:
            speaker_turns = [
                {
                    "speaker_label": str(t.get("speaker_label", "UNKNOWN")),
                    "start": float(t.get("start", 0.0)),
                    "end": float(t.get("end", 0.0)),
                    "text": str(t.get("text", "")),
                }
                for t in raw_turns
                if isinstance(t, dict)
            ]
        return cls(
            score=float(hit.get("score", 0.0)),
            body=str(payload.get("body", "")),
            chunk_index=int(payload.get("chunk_index", 0)),
            start_seconds=float(payload.get("start_seconds", 0.0)),
            end_seconds=float(payload.get("end_seconds", 0.0)),
            meeting_id=int(payload.get("meeting_id", 0)),
            city=str(payload.get("city", "")),
            county=str(payload.get("county", "")),
            state=str(payload.get("state", "")),
            speaker_turns=speaker_turns,
        )


# ── Retrieval ─────────────────────────────────────────────────────────


def _shared_retrieval_core():
    try:
        from zspan_cli import local_retrieval
    except ImportError:
        from zspan_cli.zspan_cli import local_retrieval
    return local_retrieval


def retrieve_chunks(
    meeting_id: int,
    query: str,
    *,
    top_k: int = 12,
    host: str = DEFAULT_RAG_NODE_HOST,
    port: int = DEFAULT_RAG_NODE_PORT,
    token: Optional[str] = None,
    timeout_seconds: float = 30.0,
    db_path: Path | str | None = None,
) -> list[RetrievedChunk]:
    """Retrieve top-K chunks from the flagship's in-process SQLite index.

    ``host``, ``port``, ``token``, and ``timeout_seconds`` remain accepted for
    caller compatibility only; the retired Surface Pro endpoint is never read.
    ``db_path`` lets narrow offline workers bind retrieval to the same explicit
    database they have already validated.
    """
    _ = (host, port, token, timeout_seconds)
    from zspan_pipeline import local_vector_store

    core = _shared_retrieval_core()
    stored, matrix = local_vector_store.load_chunk_matrix(
        meeting_id,
        expected_model=core.EMBED_MODEL_NAME,
        expected_dim=core.VECTOR_DIM,
        expected_chunker_version=core.CHUNKER_VERSION,
        db_path=db_path,
    )
    if not stored:
        return []
    query_vector = core.embed_query(query)
    top = core.top_k_cosine(matrix, query_vector, k=top_k)
    city, county, state = local_vector_store.load_meeting_geography(
        meeting_id,
        db_path=db_path,
    )
    chunks = [
        RetrievedChunk(
            score=score,
            body=stored[index].text,
            chunk_index=stored[index].chunk_index,
            start_seconds=stored[index].start_seconds,
            end_seconds=stored[index].end_seconds,
            meeting_id=meeting_id,
            city=city,
            county=county,
            state=state,
            speaker_turns=stored[index].speaker_turns,
        )
        for index, score in top
    ]
    logger.info(
        "Locally retrieved %d chunks for meeting=%d (scores: %s)",
        len(chunks), meeting_id,
        ", ".join(f"{c.score:.3f}" for c in chunks),
    )
    return chunks


def load_complete_meeting_chunks(
    meeting_id: int,
    *,
    db_path: Path | str | None = None,
) -> list[RetrievedChunk]:
    """Load every indexed chunk after proving it matches ``transcript_words``.

    Transcript content, the index digest, and chunk rows are read from one
    SQLite snapshot. This prevents a regeneration from mixing a new canonical
    transcript with an older chunk set. The returned shape is deliberately the
    same ``RetrievedChunk`` shape used by citation and alignment code.
    """
    from zspan_pipeline import local_vector_store

    conn = local_vector_store.connect(db_path)
    try:
        local_vector_store.ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN")
        transcript_row = conn.execute(
            """
            SELECT content, error
            FROM notebook_outputs
            WHERE meeting_id = ? AND output_type = 'transcript_words'
            """,
            (meeting_id,),
        ).fetchone()
        if transcript_row is None:
            raise LookupError(
                f"meeting {meeting_id} has no transcript_words cache row"
            )
        if transcript_row["error"]:
            raise ValueError(
                f"meeting {meeting_id} transcript_words cache has error: "
                f"{transcript_row['error']}"
            )
        raw_transcript = transcript_row["content"]
        transcript = (
            json.loads(raw_transcript)
            if isinstance(raw_transcript, str)
            else raw_transcript
        )
        if (
            not isinstance(transcript, dict)
            or not isinstance(transcript.get("words"), list)
            or not transcript["words"]
        ):
            raise ValueError(
                f"meeting {meeting_id} transcript_words content has no words list"
            )

        index_row = conn.execute(
            """
            SELECT transcript_sha256
            FROM local_retrieval_indexes
            WHERE meeting_id = ?
            """,
            (meeting_id,),
        ).fetchone()
        if index_row is None:
            raise LookupError(f"meeting {meeting_id} has no local retrieval index")
        canonical_hash = local_vector_store.transcript_hash(transcript)
        if str(index_row["transcript_sha256"]) != canonical_hash:
            raise RuntimeError(
                f"meeting {meeting_id} local index is stale relative to "
                "canonical transcript_words"
            )

        rows = conn.execute(
            """
            SELECT chunk_index, text, start_seconds, end_seconds, speaker_turns
            FROM local_retrieval_chunks
            WHERE meeting_id = ?
            ORDER BY chunk_index
            """,
            (meeting_id,),
        ).fetchall()
        if not rows:
            raise RuntimeError(
                f"meeting {meeting_id} local index contains zero chunks"
            )

        geography = ("", "", "")
        meetings_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meetings'"
        ).fetchone()
        if meetings_table is not None:
            meeting_row = conn.execute(
                "SELECT city_name, county, state FROM meetings WHERE id = ?",
                (meeting_id,),
            ).fetchone()
            if meeting_row is not None:
                geography = (
                    str(meeting_row["city_name"] or ""),
                    str(meeting_row["county"] or ""),
                    str(meeting_row["state"] or ""),
                )
    finally:
        conn.close()

    city, county, state = geography
    chunks = [
        RetrievedChunk.from_hit({
            "score": 1.0,
            "payload": {
                "body": row["text"],
                "chunk_index": row["chunk_index"],
                "start_seconds": row["start_seconds"],
                "end_seconds": row["end_seconds"],
                "meeting_id": meeting_id,
                "city": city,
                "county": county,
                "state": state,
                "speaker_turns": (
                    json.loads(row["speaker_turns"])
                    if row["speaker_turns"]
                    else None
                ),
            },
        })
        for row in rows
    ]
    logger.info(
        "Loaded complete transcript evidence meeting=%d chunks=%d",
        meeting_id,
        len(chunks),
    )
    return chunks


# ── Prompt building ───────────────────────────────────────────────────


def _format_chunk_for_prompt(chunk: RetrievedChunk) -> str:
    """One chunk rendered as a labeled block for inclusion in the Sonnet prompt.

    Includes karaoke-timecode metadata so Sonnet can produce inline
    citations like `[at 12:34]` when the prompt template asks for them.
    The display layer can later strip + linkify these.

    Phase 2 D5: when the chunk carries speaker_turns (diarized meeting),
    the body renders as `SPEAKER_NN: "<text>"` blocks instead of
    undiarized prose so Sonnet can cite speakers directly from the
    cluster labels instead of inferring from proximity.
    """
    start_min = int(chunk.start_seconds // 60)
    start_sec = int(chunk.start_seconds % 60)
    header = (
        f"[chunk_index={chunk.chunk_index} "
        f"timecode={start_min:02d}:{start_sec:02d} "
        f"start_seconds={chunk.start_seconds:.1f}]"
    )
    if chunk.speaker_turns:
        body_lines = [
            f"  {t['speaker_label']}: \"{t['text']}\""
            for t in chunk.speaker_turns
        ]
        return header + "\n" + "\n".join(body_lines)
    return f"{header}\n{chunk.body}"


def build_synthesis_prompt(
    *,
    output_type: str,
    canonical_prompt: str,
    meeting_id: int,
    chunks: list[RetrievedChunk],
) -> str:
    """Compose the full generator input combining complete chunks + the
    canonical Z-SPAN prompt template for the target output type.

    The shape is deliberate:

      - Start with a brief framing sentence so Sonnet knows what it's
        seeing + what the task is.
      - Include every karaoke-tagged chunk in chronological order.
      - Embed the existing prompts/<output>.md verbatim so the output
        shape matches what the frontend already expects (numbered list,
        prose paragraph, JSON, whatever the canonical prompt asks for).
      - End with a tight final instruction reinforcing "use only the
        complete transcript" so the generator does not invent evidence.
    """
    chunks_block = "\n\n".join(_format_chunk_for_prompt(c) for c in chunks)

    # Inject the meeting's cluster→canonical mapping so Sonnet can
    # translate `SPEAKER_NN` labels in the chunk turns to named members.
    # Without this, outputs that REQUIRE named speakers (like
    # community_calls_to_action's strict prompt) returned `[]` even
    # when the roster mapping was confirmed — Sonnet had no way to
    # know SPEAKER_24 was the council member it could legally name.
    # Same data the sidecar_pipeline extractor has been injecting since
    # Phase 2; just newly extended to the V1-RAG-3 synthesis path.
    roster_block = ""
    try:
        from zspan_pipeline import cluster_roster_mapper as _crm
        roster_block = _crm.build_cluster_roster_block(meeting_id) or ""
    except Exception:
        logger.exception(
            "build_synthesis_prompt: cluster roster lookup failed; "
            "proceeding without CLUSTER_ROSTER block",
        )
        roster_block = ""

    roster_section = (
        f"{roster_block}\n\n" if roster_block.strip() else ""
    )

    return (
        f"You are extracting structured output from a U.S. municipal city "
        f"council meeting transcript. The output type is "
        f"`{output_type}`.\n\n"
        f"{roster_section}"
        f"COMPLETE CHRONOLOGICAL TRANSCRIPT — all {len(chunks)} indexed "
        f"chunks for meeting_id={meeting_id}, in chunk_index order. Each "
        f"chunk is tagged with karaoke-timecode metadata so you can reference "
        f"specific moments. Do NOT use information that is not in this "
        f"complete transcript.\n\n"
        f"---\n"
        f"{chunks_block}\n"
        f"---\n\n"
        f"TASK — generate the output following EXACTLY the canonical Z-SPAN "
        f"prompt template below. Match its output format, tone, and "
        f"constraints precisely. Do NOT include any preamble, scaffolding, "
        f"or commentary beyond what the template instructs.\n\n"
        f"CANONICAL PROMPT:\n"
        f"{canonical_prompt}\n\n"
        f"FINAL INSTRUCTION: synthesize the output now using ONLY the "
        f"complete chronological transcript above. If it does not contain enough "
        f"information for a confident answer, output a shorter answer "
        f"rather than fabricating content."
    )


def load_canonical_prompt(
    output_type: str, prompts_dir: Optional[Path] = None
) -> str:
    """Load the body of prompts/<output_type>.md, stripping the YAML
    frontmatter so only the actual prompt instructions remain.

    The canonical prompts are stored with frontmatter blocks like
    `--- ... ---` at the top for metadata (output_type, target, status,
    description, etc.); the actual prompt is everything after the second
    `---` line.
    """
    base = prompts_dir or DEFAULT_PROMPTS_DIR
    candidate = (base / f"{output_type}.md").resolve()
    # Path containment (defense-in-depth): a hostile output_type must not
    # traverse out of the prompts dir — "../../etc/passwd" or an absolute
    # path both escape the `base / ...` join. Callers currently constrain
    # output_type, but this closes the latent traversal regardless.
    try:
        candidate.relative_to(base.resolve())
    except ValueError:
        raise ValueError(
            f"Invalid output_type {output_type!r}: escapes the prompts directory"
        )
    if not candidate.exists():
        raise FileNotFoundError(
            f"Prompt template not found at {candidate}. "
            f"V1-RAG-3 expects prompts/{output_type}.md to exist."
        )
    text = candidate.read_text()
    # Strip YAML frontmatter (--- ... --- at the top)
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            text = parts[2]
    return strip_explicit_model_boundaries(text)


# ── Claude synthesis via claude -p ────────────────────────────────────


def _sanitized_synth_env(
    *, max_output_tokens: int | None = None
) -> dict[str, str]:
    """os.environ minus secret-bearing vars, for the `claude -p` subprocess.

    S-144 capability isolation: the synthesis prompt embeds untrusted civic
    transcript text. `--tools ""` already denies the model every tool, but
    stripping secret-shaped env vars additionally closes the
    "echo $OPENAI_API_KEY"-style environment-exfil channel deterministically.
    Claude Code authenticates via the Max subscription (keychain / ~/.claude),
    NOT via an env var, so removing API-key-shaped vars does not affect auth —
    verified 2026-07-12 (`claude -p` returns text with the secret vars unset).

    Shares the CLI's `_sanitized_codex_env()` posture (strip secrets before an
    untrusted-input subprocess); the logic is replicated, not imported, because
    the flagship pipeline and the sovereign CLI are deliberately separate
    packages (D-163). Deliberate deviation from the CLI list: the flagship
    subprocess IS `claude`, so we DON'T strip provider-name prefixes — that
    would remove claude's own routing config (e.g. ANTHROPIC_BASE_URL) on the
    risk-sensitive synthesis path. The credential-shaped rules below still catch
    every real key/token (ANTHROPIC_API_KEY matches both `_KEY` and `API_KEY`);
    a bare routing URL is not a credential and is not worth exfil-protecting.
    """

    def _sensitive(name: str) -> bool:
        n = name.upper()
        return (
            n.endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIALS"))
            or "API_KEY" in n
        )

    env = {k: v for k, v in os.environ.items() if not _sensitive(k)}
    if max_output_tokens is not None:
        env[CLAUDE_MAX_OUTPUT_TOKENS_ENV] = str(max_output_tokens)
    return env


def synthesize_via_claude_p(
    prompt: str,
    *,
    model: str = SONNET_MODEL_ID,
    timeout_seconds: Optional[float] = None,
    max_output_tokens: int | None = None,
    output_json_schema: Mapping[str, Any] | None = None,
    effort: str | None = None,
    system_prompt: str | None = None,
) -> str:
    """Invoke `claude -p` as a subprocess; return the synthesized text.

    ``system_prompt`` is optional so existing synthesis callers retain their
    exact command shape; supplied values are passed as one argv item through
    Claude CLI's canonical ``--system-prompt`` flag.

    Uses the operator's Claude Code installation (resolved from PATH,
    the ZSPAN_CLAUDE_BIN env override, or the nvm default under the
    home directory) and the Max subscription's headless-metered budget
    per D-119. No separate Anthropic API key required.

    The model id is passed explicitly per invocation per the
    [[explicit-at-invocation-not-config-default-for-supervised-tools]]
    discipline. The low-level default remains Sonnet 4.6 for independent
    cheap-task helpers; the flagship fallback runner passes Opus explicitly.
    """
    if max_output_tokens is not None and (
        isinstance(max_output_tokens, bool)
        or not isinstance(max_output_tokens, int)
        or max_output_tokens <= 0
    ):
        raise ValueError(
            "max_output_tokens must be a positive integer or None; "
            f"got {max_output_tokens!r}"
        )
    if system_prompt is not None and not isinstance(system_prompt, str):
        raise TypeError("system_prompt must be a string or None")
    serialized_schema: str | None = None
    if output_json_schema is not None:
        if not isinstance(output_json_schema, Mapping):
            raise ValueError("output_json_schema must be a mapping or None")
        try:
            serialized_schema = json.dumps(
                output_json_schema,
                ensure_ascii=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("output_json_schema must be JSON-serializable") from exc
    # `NO_TIMEOUT` disables the wall-clock cap entirely. A whole-meeting call on
    # a multi-hour transcript is legitimately slow, and killing it yields no
    # result at all — strictly worse than waiting. `None` still means "use the
    # configured default", so every existing caller is unaffected.
    if timeout_seconds is NO_TIMEOUT:
        timeout_seconds = None
    else:
        if timeout_seconds is None:
            raw_timeout = os.environ.get(SYNTHESIS_TIMEOUT_ENV)
            if raw_timeout is None or not raw_timeout.strip():
                timeout_seconds = DEFAULT_SYNTHESIS_TIMEOUT_SECONDS
            else:
                try:
                    timeout_seconds = float(raw_timeout)
                except ValueError as exc:
                    raise ValueError(
                        f"{SYNTHESIS_TIMEOUT_ENV} must be a positive number; "
                        f"got {raw_timeout!r}"
                    ) from exc
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError(
                f"synthesis timeout must be a positive finite number; "
                f"got {timeout_seconds!r}"
            )

    # Resolve claude binary — shutil.which first (honors PATH), then env
    # override, then known-good Mac default. Flask sometimes runs with a
    # stripped PATH that drops nvm; the fallback chain keeps things working
    # regardless of how the parent process was launched.
    claude_bin = (
        shutil.which("claude")
        or os.environ.get("ZSPAN_CLAUDE_BIN")
        or os.path.expanduser("~/.nvm/versions/node/v22.22.1/bin/claude")
    )
    if not Path(claude_bin).exists():
        raise RuntimeError(
            f"`claude` CLI not found (tried PATH, ZSPAN_CLAUDE_BIN, fallback {claude_bin!r}). "
            "Install Claude Code or set ZSPAN_CLAUDE_BIN to the binary path."
        )

    logger.info(
        "Invoking claude -p (model=%s, prompt_chars=%d, timeout=%s)",
        model,
        len(prompt),
        "none" if timeout_seconds is None else f"{timeout_seconds:.0f}s",
    )
    # S-144 capability isolation: `--tools ""` denies the model every tool for
    # this pure text-synthesis call (the prompt embeds untrusted transcript
    # text), and env= strips secret-shaped vars so an injection can't exfil a
    # credential via the environment. Both close injection channels without
    # changing the synthesized output; verified against a live pipeline run.
    command = [
        claude_bin, "-p",
        "--model", model,
        "--output-format", "text",
        "--tools", "",
    ]
    if system_prompt is not None:
        command.extend(["--system-prompt", system_prompt])
    if effort is not None:
        command.extend(["--effort", effort])
    if serialized_schema is not None:
        command.extend(["--json-schema", serialized_schema])
    started_at = time.monotonic()
    result = subprocess.run(
        command,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        env=_sanitized_synth_env(max_output_tokens=max_output_tokens),
    )
    duration_seconds = time.monotonic() - started_at
    if result.returncode != 0:
        raise ClaudePError(
            f"claude -p failed with returncode={result.returncode} after "
            f"{duration_seconds:.1f}s. "
            f"stderr: {result.stderr[:500]!r}",
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=duration_seconds,
        )
    output = result.stdout.strip()
    if not output:
        raise ClaudePError(
            f"claude -p returned empty stdout. stderr: {result.stderr[:500]!r}",
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=duration_seconds,
        )
    logger.info(
        "Synthesized %d chars from %d input chars in %.1fs",
        len(output),
        len(prompt),
        duration_seconds,
    )
    return output


def classify_claude_failure(error: BaseException | str) -> str:
    """Map one Claude transport failure to the session-107 taxonomy."""
    if isinstance(error, (subprocess.TimeoutExpired, TimeoutError)):
        return TIMEOUT

    parts = [str(error)]
    if isinstance(error, ClaudePError):
        parts.extend((error.stdout, error.stderr))
    elif isinstance(error, subprocess.TimeoutExpired):
        parts.extend((str(error.stdout or ""), str(error.stderr or "")))
    text = " ".join(parts).casefold()

    if "session limit" in text or "account limit" in text:
        return ACCOUNT_LIMIT
    if any(
        marker in text
        for marker in (
            "failed to authenticate",
            "invalid authentication credentials",
            "authentication failed",
            "authentication error",
            "not authenticated",
            "not logged in",
            "please log in",
            "please login",
            "please run /login",
            "oauth token",
            "unauthorized",
            "api error: 401",
        )
    ):
        return AUTH_FAILURE
    if any(
        marker in text
        for marker in (
            "model unavailable",
            "model is unavailable",
            "model not available",
            "model is not available",
            "unsupported model",
            "unknown model",
            "invalid model",
            "does not have access to model",
            "model not found",
        )
    ):
        return MODEL_UNAVAILABLE
    if "timed out" in text or "timeout" in text:
        return TIMEOUT
    if any(
        marker in text
        for marker in (
            "unable to connect to api",
            "network error",
            "connection reset",
            "connection refused",
            "temporary failure in name resolution",
            "service unavailable",
            "enotfound",
            "econnreset",
            "econnrefused",
            "etimedout",
        )
    ):
        return TRANSIENT_NETWORK
    return UNKNOWN_FAILURE


def _failure_from_cli_output(output: str) -> str | None:
    """Recognize short CLI diagnostics that were returned with rc=0."""
    normalized = " ".join(output.strip().casefold().split())
    if not normalized or len(normalized) > 1_000:
        return None
    if normalized.startswith(("you've hit your session limit", "you have hit your session limit")):
        return ACCOUNT_LIMIT
    if normalized.startswith(("failed to authenticate", "authentication failed")):
        return AUTH_FAILURE
    if normalized.startswith(("api error: unable to connect", "network error:")):
        return TRANSIENT_NETWORK
    if normalized.startswith(("error: model", "model unavailable", "unsupported model")):
        return MODEL_UNAVAILABLE
    return None


def _minimal_agy_env() -> dict[str, str]:
    """Pass only process/bootstrap variables needed by the installed Agy CLI."""
    allowed = ("HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "USER", "LOGNAME")
    return {name: os.environ[name] for name in allowed if name in os.environ}


def _build_gemini_command(
    prompt: str,
    *,
    agy_bin: str,
    model_id: str = GEMINI_PRIMARY_MODEL_ID,
) -> list[str]:
    """Build the proof-passed, tool-denied Gemini argv contract."""
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("Gemini prompt must be a nonempty string")
    if model_id not in {GEMINI_PRIMARY_MODEL_ID, GEMINI_BACKUP_MODEL_ID}:
        raise ValueError(f"Gemini model is not in the locked chain: {model_id!r}")
    prompt_bytes = len(prompt.encode("utf-8"))
    if prompt_bytes > GEMINI_PROMPT_BYTE_LIMIT:
        raise GeminiPError(
            PROMPT_TOO_LARGE_FOR_GEMINI,
            f"Gemini argv prompt is {prompt_bytes} bytes; proven ceiling is "
            f"{GEMINI_PROMPT_BYTE_LIMIT} bytes and truncation is forbidden",
        )
    command = [
        agy_bin,
        "-p",
        prompt,
        "--model",
        model_id,
        "--effort",
        GEMINI_EFFORT,
        "--sandbox",
        "--output-format",
        "text",
        "--print-timeout",
        "15m",
    ]
    if "--dangerously-skip-permissions" in command or "--permissions" in command:
        raise AssertionError("Gemini generation command widened tool permissions")
    return command


def _compose_gemini_prompt(
    prompt: str,
    *,
    system_prompt: str | None,
    output_json_schema: Mapping[str, Any] | None,
) -> str:
    """Flatten Claude-only control fields into Agy's proven argv transport."""
    parts: list[str] = []
    if system_prompt:
        parts.append(f"SYSTEM INSTRUCTIONS:\n{system_prompt}")
    parts.append(f"TASK INPUT:\n{prompt}")
    if output_json_schema is not None:
        parts.append(
            "REQUIRED JSON SCHEMA:\n"
            + json.dumps(output_json_schema, ensure_ascii=True, separators=(",", ":"))
        )
    return "\n\n".join(parts)


def classify_agy_failure(error: BaseException | str) -> str:
    """Map observed Agy diagnostics onto the shared generation taxonomy.

    Model-specific capacity/unavailability is intentionally distinct from a
    Google account wall: only the former is eligible for the Flash rung.
    Generic service/transport failures skip Flash because both Gemini models
    share the same provider path.
    """
    if isinstance(error, (subprocess.TimeoutExpired, TimeoutError)):
        return TIMEOUT

    text = str(error).casefold()
    if any(
        marker in text
        for marker in (
            "model unavailable",
            "model is unavailable",
            "model not available",
            "model is not available",
            "unsupported model",
            "unknown model",
            "invalid model",
            "model not found",
            "no capacity for model",
            "model capacity",
            "model is overloaded",
            "requested model is overloaded",
        )
    ):
        return MODEL_UNAVAILABLE
    if any(
        marker in text
        for marker in (
            "usage limit",
            "session limit",
            "account limit",
            "quota exceeded",
            "quota has been exceeded",
            "resource exhausted",
            "resource_exhausted",
            "rate limit exceeded",
            "rate_limit_exceeded",
            "too many requests",
            "http 429",
            "status 429",
            "status=429",
            "error 429",
        )
    ):
        return ACCOUNT_LIMIT
    if any(
        marker in text
        for marker in (
            "failed to authenticate",
            "authentication failed",
            "authentication error",
            "not authenticated",
            "not logged in",
            "please log in",
            "please login",
            "oauth token",
            "refresh token",
            "invalid credentials",
            "unauthorized",
            "http 401",
            "status 401",
            "status=401",
            "error 401",
            "http 403",
            "status 403",
            "status=403",
            "error 403",
        )
    ):
        return AUTH_FAILURE
    if "timed out" in text or "timeout" in text or "deadline exceeded" in text:
        return TIMEOUT
    if any(
        marker in text
        for marker in (
            "network error",
            "connection reset",
            "connection refused",
            "temporary failure in name resolution",
            "service unavailable",
            "provider unavailable",
            "bad gateway",
            "gateway timeout",
            "enotfound",
            "econnreset",
            "econnrefused",
            "etimedout",
            "http 502",
            "http 503",
            "http 504",
            "status 502",
            "status 503",
            "status 504",
        )
    ):
        return TRANSIENT_NETWORK
    return UNKNOWN_FAILURE


def _failure_from_agy_output(output: str) -> str | None:
    """Recognize short Agy diagnostics that may arrive with return code zero."""
    normalized = " ".join(output.strip().casefold().split())
    if not normalized or len(normalized) > 1_000:
        return None
    if not normalized.startswith(
        (
            "error:",
            "agy error:",
            "gemini error:",
            "rpc error:",
            "failed to authenticate",
            "authentication failed",
            "not logged in",
            "you've hit",
            "you have hit",
            "usage limit",
            "account limit",
            "quota exceeded",
            "resource_exhausted",
            "resource exhausted",
            "model unavailable",
            "model is unavailable",
            "unsupported model",
        )
    ):
        return None
    failure_class = classify_agy_failure(normalized)
    return failure_class if failure_class != UNKNOWN_FAILURE else None


def _synthesize_via_gemini(prompt: str, *, model_id: str) -> str:
    """Invoke Gemini from an empty scratch directory with a minimal env."""
    agy_bin = shutil.which("agy") or os.environ.get("ZSPAN_AGY_BIN")
    if not agy_bin or not Path(agy_bin).exists():
        raise GeminiPError(
            GEMINI_CLI_UNAVAILABLE,
            "`agy` CLI not found; install it or set ZSPAN_AGY_BIN",
        )
    command = _build_gemini_command(prompt, agy_bin=agy_bin, model_id=model_id)
    try:
        with tempfile.TemporaryDirectory(prefix="zspan-gemini-") as scratch_dir:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=16 * 60,
                cwd=scratch_dir,
                env=_minimal_agy_env(),
            )
    except subprocess.TimeoutExpired as exc:
        raise GeminiPError(
            TIMEOUT,
            f"{model_id} exceeded its 16-minute process wall",
        ) from exc
    except OSError as exc:
        raise GeminiPError(
            GEMINI_CLI_UNAVAILABLE,
            f"Gemini invocation could not start: {exc}",
        ) from exc
    if result.returncode != 0:
        diagnostic = (
            f"returncode={result.returncode} stdout={result.stdout[:500]!r} "
            f"stderr={result.stderr[:500]!r}"
        )
        raise GeminiPError(
            classify_agy_failure(diagnostic),
            f"{model_id} failed: {diagnostic}",
        )
    output = result.stdout.strip()
    if not output:
        diagnostic = f"empty stdout; stderr={result.stderr[:500]!r}"
        raise GeminiPError(
            classify_agy_failure(diagnostic),
            f"{model_id} returned {diagnostic}",
        )
    output_failure = _failure_from_agy_output(output)
    if output_failure is not None:
        raise GeminiPError(
            output_failure,
            f"{model_id} returned a diagnostic on stdout: {output[:500]!r}",
        )
    return output


def _attempt_claude_generation(
    prompt: str,
    *,
    model_id: str,
    timeout_seconds: Optional[float],
    max_output_tokens: int | None,
    output_json_schema: Mapping[str, Any] | None,
    system_prompt: str | None,
) -> tuple[str | None, str | None, str]:
    try:
        output = synthesize_via_claude_p(
            prompt,
            model=model_id,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
            output_json_schema=output_json_schema,
            effort=OPUS_BACKSTOP_EFFORT,
            system_prompt=system_prompt,
        )
    except Exception as exc:
        return None, classify_claude_failure(exc), str(exc)[:500]
    diagnostic = _failure_from_cli_output(output)
    if diagnostic is not None:
        return None, diagnostic, output[:500]
    return output, None, ""


def generate_with_fallback(
    prompt: str,
    *,
    timeout_seconds: Optional[float] = None,
    max_output_tokens: int | None = None,
    output_json_schema: Mapping[str, Any] | None = None,
    system_prompt: str | None = None,
) -> GenerationResult:
    """Run Gemini Pro→one-WO Opus→Flash and return exact provenance.

    There are no blind same-prompt retries. Callers may issue a new generation
    only after a deterministic validator supplies a repair note (the PR #232
    pattern); every such call still shares the work-order provider circuits and
    single Opus ceiling. Flash is eligible only after a model-specific Pro
    failure; shared Google account/transport failures skip it.
    """
    attempts: list[GenerationAttempt] = []

    state = _WORK_ORDER_GENERATION_STATE.get()
    if state is None:
        state = WorkOrderGenerationState(work_order_id="standalone-call")

    gemini_prompt = _compose_gemini_prompt(
        prompt,
        system_prompt=system_prompt,
        output_json_schema=output_json_schema,
    )

    def gemini_attempt(model_id: str) -> tuple[str | None, str | None, str]:
        try:
            content = _synthesize_via_gemini(gemini_prompt, model_id=model_id)
        except GeminiPError as exc:
            attempts.append(GenerationAttempt("google", model_id, exc.failure_class))
            logger.warning(
                "Generation attempt failed model=%s failure_class=%s",
                model_id,
                exc.failure_class,
            )
            return None, exc.failure_class, str(exc)[:500]
        attempts.append(GenerationAttempt("google", model_id, None))
        logger.info("Generation attempt succeeded model=%s", model_id)
        return content, None, ""

    def opus_backstop(
        trigger_class: str,
        trigger_detail: str,
        *,
        allow_flash_after_failure: bool = False,
    ) -> GenerationResult | None:
        cached_anthropic_failure = state.provider_account_failures.get("anthropic")
        if cached_anthropic_failure is not None:
            if allow_flash_after_failure:
                logger.warning(
                    "Skipping Opus rung for work_order=%s cached_failure=%s",
                    state.work_order_id,
                    cached_anthropic_failure,
                )
                return None
            pause(
                cached_anthropic_failure,
                "Anthropic subscription is already open-circuited for work order "
                f"{state.work_order_id}: {cached_anthropic_failure}",
            )
        if state.opus_attempted:
            if allow_flash_after_failure:
                logger.warning(
                    "Skipping Opus rung for work_order=%s because its one "
                    "automatic attempt was already spent",
                    state.work_order_id,
                )
                return None
            pause(
                OPUS_WORK_ORDER_BUDGET_EXHAUSTED,
                "the one automatic Opus attempt was already spent for work "
                f"order {state.work_order_id}; trigger={trigger_class}: "
                f"{trigger_detail}",
            )
        state.opus_attempted = True
        content, failure_class, detail = _attempt_claude_generation(
            prompt,
            model_id=OPUS_BACKSTOP_MODEL_ID,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
            output_json_schema=output_json_schema,
            system_prompt=system_prompt,
        )
        attempts.append(
            GenerationAttempt(
                "anthropic",
                OPUS_BACKSTOP_MODEL_ID,
                failure_class,
            )
        )
        if failure_class is None:
            logger.info(
                "Generation backstop succeeded model=%s work_order=%s",
                OPUS_BACKSTOP_MODEL_ID,
                state.work_order_id,
            )
            return GenerationResult(
                content or "",
                OPUS_BACKSTOP_MODEL_ID,
                tuple(attempts),
            )
        if failure_class in {ACCOUNT_LIMIT, AUTH_FAILURE}:
            state.provider_account_failures["anthropic"] = failure_class
        if allow_flash_after_failure and failure_class in {
            MODEL_UNAVAILABLE,
            ACCOUNT_LIMIT,
            AUTH_FAILURE,
            TRANSIENT_NETWORK,
            TIMEOUT,
        }:
            return None
        pause(failure_class or UNKNOWN_FAILURE, detail)
        raise AssertionError("unreachable Opus backstop state")

    def pause(failure_class: str, detail: str) -> None:
        models = ", ".join(attempt.model_id for attempt in attempts)
        raise GenerationPausedError(
            failure_class,
            f"generation paused after {failure_class}; attempted models: {models}; "
            f"last detail: {detail}",
            attempts=attempts,
        )

    cached_google_failure = state.provider_account_failures.get("google")
    if cached_google_failure is not None:
        logger.warning(
            "Skipping Gemini rungs for work_order=%s cached_failure=%s",
            state.work_order_id,
            cached_google_failure,
        )
        result = opus_backstop(
            cached_google_failure,
            "Google subscription was open-circuited by an earlier artifact",
        )
        if result is None:
            raise AssertionError("Opus terminal backstop returned no result")
        return result

    content, failure_class, detail = gemini_attempt(GEMINI_PRIMARY_MODEL_ID)
    if failure_class is None:
        return GenerationResult(
            content or "",
            GEMINI_PRIMARY_MODEL_ID,
            tuple(attempts),
        )
    if failure_class in {ACCOUNT_LIMIT, AUTH_FAILURE}:
        state.provider_account_failures["google"] = failure_class
        result = opus_backstop(failure_class, detail)
        if result is None:
            raise AssertionError("Opus terminal backstop returned no result")
        return result
    if failure_class == MODEL_UNAVAILABLE:
        opus_result = opus_backstop(
            failure_class,
            detail,
            allow_flash_after_failure=True,
        )
        if opus_result is not None:
            return opus_result
        content, backup_failure, backup_detail = gemini_attempt(
            GEMINI_BACKUP_MODEL_ID
        )
        if backup_failure is None:
            return GenerationResult(
                content or "",
                GEMINI_BACKUP_MODEL_ID,
                tuple(attempts),
            )
        if backup_failure in {ACCOUNT_LIMIT, AUTH_FAILURE}:
            state.provider_account_failures["google"] = backup_failure
        pause(backup_failure or UNKNOWN_FAILURE, backup_detail)
    if failure_class in {
        TRANSIENT_NETWORK,
        TIMEOUT,
        GEMINI_CLI_UNAVAILABLE,
        PROMPT_TOO_LARGE_FOR_GEMINI,
    }:
        result = opus_backstop(failure_class, detail)
        if result is None:
            raise AssertionError("Opus terminal backstop returned no result")
        return result
    pause(failure_class or UNKNOWN_FAILURE, detail)
    raise AssertionError("unreachable generation fallback state")


# ── Top-level orchestrator ────────────────────────────────────────────


def record_synthesis_provenance(
    *,
    meeting_id: int,
    output_type: str,
    prompt: str,
    model_id: str,
    retrieved_chunk_ids: list[int],
    evidence_mode: str = "complete_transcript",
    attempts: Sequence[GenerationAttempt] = (),
) -> Path:
    """Atomically record the latest synthesis inputs for one output type."""
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    artifact_path = PREVIEW_DIR / f"m{meeting_id}_synthesis_provenance.json"

    try:
        existing = json.loads(artifact_path.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            raise ValueError("artifact root is not a JSON object")
    except FileNotFoundError:
        existing = {}
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "Could not read synthesis provenance artifact %s; "
            "starting a fresh record: %s",
            artifact_path,
            exc,
        )
        existing = {}

    existing[output_type] = {
        "output_type": output_type,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "prompt_char_count": len(prompt),
        "model_id": model_id,
        "retrieved_chunk_ids": list(retrieved_chunk_ids),
        "evidence_mode": evidence_mode,
        "attempts": [attempt.as_dict() for attempt in attempts],
        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=PREVIEW_DIR,
            prefix=f".{artifact_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            json.dump(existing, temp_file, indent=2)
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, artifact_path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()

    return artifact_path


@dataclass
class SynthesisResult:
    """The full output of one synthesis run, ready for the caller to
    persist to the notebook_outputs cache."""

    content: str
    output_type: str
    meeting_id: int
    chunks: list[RetrievedChunk]
    model_id: str
    prompt_filename: str
    attempts: tuple[GenerationAttempt, ...] = ()


def synthesize_output(
    *,
    meeting_id: int,
    output_type: str,
    prompts_dir: Optional[Path] = None,
    timeout_seconds: Optional[float] = None,
    db_path: Path | str | None = None,
) -> SynthesisResult:
    """End-to-end: load complete chunks → generate → return provenance.

    The caller persists the result to the notebook_outputs cache.
    ``fetcher.py`` is the canonical production caller; the regeneration
    script is a retained one-output maintenance caller.
    """
    canonical_prompt = load_canonical_prompt(output_type, prompts_dir=prompts_dir)
    chunks = load_complete_meeting_chunks(meeting_id, db_path=db_path)

    full_prompt = build_synthesis_prompt(
        output_type=output_type,
        canonical_prompt=canonical_prompt,
        meeting_id=meeting_id,
        chunks=chunks,
    )
    generation = generate_with_fallback(
        full_prompt,
        timeout_seconds=timeout_seconds,
    )
    try:
        record_synthesis_provenance(
            meeting_id=meeting_id,
            output_type=output_type,
            prompt=full_prompt,
            model_id=generation.model_id,
            retrieved_chunk_ids=[chunk.chunk_index for chunk in chunks],
            evidence_mode="complete_transcript",
            attempts=generation.attempts,
        )
    except Exception as exc:
        logger.warning(
            "Could not record synthesis provenance for meeting=%d "
            "output_type=%s: %s",
            meeting_id,
            output_type,
            exc,
            exc_info=True,
        )

    return SynthesisResult(
        content=generation.content,
        output_type=output_type,
        meeting_id=meeting_id,
        chunks=chunks,
        model_id=generation.model_id,
        prompt_filename=f"{output_type}.md",
        attempts=generation.attempts,
    )
