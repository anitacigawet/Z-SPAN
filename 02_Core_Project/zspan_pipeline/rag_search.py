"""V1.5-RAG-Search-1 — retrieval-only endpoint helpers.

Per [BYOK_ARCHITECTURE_SPEC § 2.1 + § 3](../../01_Project_Overview/BYOK_ARCHITECTURE_SPEC.md):
builds the deterministic provenance packet (run_id, prompt_template_hash,
vector_ids, query_hash, timestamp) and loads the canonical system prompt
template that ships to user-side BYOK LLMs alongside the retrieved chunks.

No LLM call happens here. This is pure metadata construction + template
packaging. The /api/rag-search/{meeting_id} Flask endpoint composes:

  1. zspan_pipeline.qdrant_synthesizer.retrieve_chunks(...)  ← Surface Pro
  2. rag_search.load_prompt_template()                          ← here
  3. rag_search.make_provenance_packet(...)                     ← here
  4. response: {chunks, provenance, recommended_system_prompt}  ← shipped

Composes with:
  - zspan_pipeline.qdrant_synthesizer  (retrieve_chunks call upstream)
  - 02_Core_Project/prompts/rag_search_v1.md  (the canonical template)
  - V1.5-Verify-1 (forthcoming) — adds byok_audit_runs persistence so the
    run_ids returned here become durable + verifiable via the public
    /api/verify-run/{run_id} endpoint.

Why vector_ids are reconstructed here (not returned by Surface Pro): the
Surface Pro /query endpoint returns hits as {score, payload} only — no
point_id. Index time uses uuid5(ZSPAN_QDRANT_NAMESPACE, f"{meeting_id}:
{chunk_index}") per index_meeting_to_qdrant.py:154, so we can reconstruct
the canonical UUID5 here without a Qdrant round-trip. Stays cheap.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .prompt_loader import strip_explicit_model_boundaries

logger = logging.getLogger(__name__)


# Pinned at parsers/scripts/index_meeting_to_qdrant.py:99 — same UUID5
# namespace used at index time. If that constant ever changes, this one
# MUST change in lockstep or vector_id reconstruction silently diverges.
ZSPAN_QDRANT_NAMESPACE = uuid.UUID("3a76e8c5-2c40-4f1d-b3ad-c1b9e9d8b2a4")

# Bump this whenever the body of prompts/rag_search_v1.md changes. The
# frontmatter `version` field in that file MUST match this string. Used
# as the prompt_template_version field of the provenance packet so the
# verification endpoint can confirm "this template body was the one
# shipped at run time" by version label alone (the hash is the cryptographic
# backstop, version is the human-readable handle).
PROMPT_TEMPLATE_VERSION = "v1.5-rag-search-2026-07-04-paragraph-structure"

_THIS_DIR = Path(__file__).resolve().parent
DEFAULT_PROMPT_TEMPLATE_PATH = _THIS_DIR.parent / "prompts" / "rag_search_v1.md"


def chunk_to_vector_id(meeting_id: int, chunk_index: int) -> str:
    """Reconstruct the Qdrant point_id for a chunk via the same UUID5
    formula index_meeting_to_qdrant.py uses at index time. Deterministic
    by construction — same (meeting_id, chunk_index) always returns the
    same UUID, no Qdrant round-trip needed."""
    return str(uuid.uuid5(ZSPAN_QDRANT_NAMESPACE, f"{meeting_id}:{chunk_index}"))


def load_prompt_template(path: Optional[Path] = None) -> str:
    """Load the canonical RAG-search system prompt template body.

    Strips YAML frontmatter (the `--- ... ---` block at the top) so only
    the actual prompt instructions remain — same shape as
    qdrant_synthesizer.load_canonical_prompt. The returned string is what
    gets hashed for prompt_template_hash and what ships to the client as
    recommended_system_prompt.
    """
    p = path or DEFAULT_PROMPT_TEMPLATE_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"RAG-search prompt template missing at {p} — V1.5-RAG-Search-1 "
            f"expects prompts/rag_search_v1.md to exist."
        )
    text = p.read_text(encoding="utf-8")
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            text = parts[2]
    return strip_explicit_model_boundaries(text)


def _normalize_query(query: str) -> str:
    """Hash-stable normalization: trim + collapse internal whitespace +
    lowercase. So the same intent in different casings/spacings produces
    the same hash. Users post the query_hash from their own screenshot and
    we confirm — that confirmation is meaningful only if cosmetic variation
    doesn't break the match."""
    return " ".join(query.strip().lower().split())


def query_hash(query: str) -> str:
    """SHA-256 hex of the normalized query (length 64, no `sha256:` prefix)."""
    h = hashlib.sha256()
    h.update(_normalize_query(query).encode("utf-8"))
    return h.hexdigest()


def prompt_template_hash(template_body: str) -> str:
    """SHA-256 hex of the template body (post-frontmatter-strip). Constant
    per template version; the hash + the version label give two independent
    ways to verify shipped-template fidelity (the version is human-readable
    + git-grep-able; the hash is the cryptographic backstop against
    deliberate or accidental drift)."""
    h = hashlib.sha256()
    h.update(template_body.encode("utf-8"))
    return h.hexdigest()


def make_run_id(meeting_id: int, query_hash_hex: str, ts: datetime) -> str:
    """Composite run_id per BYOK_ARCHITECTURE_SPEC § 3.2.

    Format: `zspan-rag-{ISO-8601-millisecond-Z}-{short-hash}-{nonce}` where
    short_hash = first 6 hex chars of sha256(iso|meeting_id|query_hash) and
    nonce is 128 random bits. The readable prefix preserves determinism of
    the identifying inputs while the nonce adds uniqueness of issuance,
    including identical inputs issued within the same millisecond.

    The composite identity proves "Z-SPAN orchestrated retrieval for
    THIS meeting against THIS query at THIS instant" — verifiable via
    /api/verify-run/{run_id} once V1.5-Verify-1 ships the byok_audit_runs
    table.
    """
    iso = ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"
    h = hashlib.sha256()
    h.update(f"{iso}|{meeting_id}|{query_hash_hex}".encode("utf-8"))
    short = h.hexdigest()[:6]
    return f"zspan-rag-{iso}-{short}-{uuid.uuid4().hex}"


def make_provenance_packet(
    *,
    meeting_id: int,
    query: str,
    chunks: list[Any],  # list[qdrant_synthesizer.RetrievedChunk]
    template_body: str,
    ts: Optional[datetime] = None,
    run_id: Optional[str] = None,
) -> dict[str, Any]:
    """Build the provenance packet per BYOK_ARCHITECTURE_SPEC § 3.

    The caller passes `template_body` (already-loaded via
    load_prompt_template) so we don't re-read the file on every request.
    A caller that must reserve the identifier before retrieval can also
    pass its once-issued ``run_id`` so rebuilding provenance with returned
    chunks does not mint a second nonce-bearing identity.

    The returned dict shape matches the spec's § 2.1 response example —
    callers can ship it verbatim under the "provenance" key in the
    /api/rag-search response.
    """
    if ts is None:
        ts = datetime.now(timezone.utc)
    q_hash = query_hash(query)
    return {
        "run_id": (
            run_id
            if run_id is not None
            else make_run_id(meeting_id, q_hash, ts)
        ),
        "vector_ids": [
            chunk_to_vector_id(meeting_id, c.chunk_index) for c in chunks
        ],
        "prompt_template_hash": "sha256:" + prompt_template_hash(template_body),
        "prompt_template_version": PROMPT_TEMPLATE_VERSION,
        "query_hash": "sha256:" + q_hash,
        "timestamp_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
