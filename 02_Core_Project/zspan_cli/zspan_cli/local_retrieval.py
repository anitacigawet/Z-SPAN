"""Shared local chunking, embedding, and cosine retrieval primitives.

This module is the single source of truth for both the distributable CLI and
the flagship pipeline.  It intentionally has no workspace or flagship-store
dependencies: callers own persistence, while this module owns the model and
chunking contract.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Optional

EMBED_MODEL_NAME = "BAAI/bge-small-en-v1.5"
VECTOR_DIM = 384
CHUNK_TOKEN_TARGET = 400
CHUNK_TOKEN_OVERLAP = 50
EMBEDDING_NORMALIZATION = "l2"
SIMILARITY_METRIC = "cosine"
CHUNKER_VERSION = "word-token-v1"

_APPROX_TOKENS_PER_WORD = 1.3


class PipelineError(Exception):
    """A chunk/embed/retrieve step failed in a way the user should read."""


@dataclass
class Chunk:
    chunk_index: int
    text: str
    start_seconds: float
    end_seconds: float
    # Half-open word bounds let flagship indexing attach diarization turns
    # from exactly the words that formed this chunk.  Defaults preserve the
    # CLI's existing public construction shape.
    word_start_index: int = 0
    word_end_index: int = 0


def load_token_counter() -> tuple[Callable[[str], int], bool]:
    """Return the exact model tokenizer counter, or a safe approximation."""
    try:
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        tok_path = hf_hub_download(EMBED_MODEL_NAME, "tokenizer.json")
        tokenizer = Tokenizer.from_file(tok_path)

        def _count(word: str) -> int:
            return max(1, len(tokenizer.encode(word, add_special_tokens=False).ids))

        return _count, True
    except Exception:
        def _approx(word: str) -> int:  # noqa: ARG001 - uniform estimate
            return 1

        return _approx, False


def chunk_transcript(
    words: list[dict],
    *,
    token_counter: Optional[Callable[[str], int]] = None,
    exact: Optional[bool] = None,
    target_tokens: int = CHUNK_TOKEN_TARGET,
    overlap_tokens: int = CHUNK_TOKEN_OVERLAP,
) -> list[Chunk]:
    """Split transcript words into overlapping, token-bounded chunks."""
    if token_counter is None:
        token_counter, exact = load_token_counter()
    if exact is False:
        target_tokens = int(target_tokens / _APPROX_TOKENS_PER_WORD)
        overlap_tokens = int(overlap_tokens / _APPROX_TOKENS_PER_WORD)

    tokens_per_word = [token_counter(w.get("word") or "") for w in words]

    chunks: list[Chunk] = []
    start = 0
    n = len(words)
    while start < n:
        count = 0
        end = start
        while end < n and count < target_tokens:
            count += tokens_per_word[end]
            end += 1

        text = " ".join((words[i].get("word") or "") for i in range(start, end)).strip()
        if text:
            chunks.append(Chunk(
                chunk_index=len(chunks),
                text=text,
                start_seconds=float(words[start].get("start", 0.0)),
                end_seconds=float(words[end - 1].get("end", 0.0)),
                word_start_index=start,
                word_end_index=end,
            ))

        if end >= n:
            break

        overlap_count = 0
        next_start = end
        while next_start > start and overlap_count < overlap_tokens:
            next_start -= 1
            overlap_count += tokens_per_word[next_start]
        start = max(next_start, start + 1)

    return chunks


_EMBEDDER = None
_EMBED_LOCK = threading.Lock()


def _embedder():
    global _EMBEDDER
    if _EMBEDDER is None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise PipelineError(
                "fastembed isn't installed — `pip install -r requirements.txt` "
                "inside the zspan_cli folder adds it."
            ) from exc
        _EMBEDDER = TextEmbedding(model_name=EMBED_MODEL_NAME)
    return _EMBEDDER


def embed_texts(texts: list[str], *, progress: Callable[[str], None] = print):
    """Embed text as an L2-normalized ``(n, 384)`` float32 matrix."""
    import numpy as np

    if not texts:
        return np.zeros((0, VECTOR_DIM), dtype=np.float32)
    progress(f"  embedding {len(texts)} chunks with {EMBED_MODEL_NAME} (local, free)...")
    # Flagship operator-search fans out across meetings.  fastembed's shared
    # tokenizer/model session is serialized just as the retired node was.
    with _EMBED_LOCK:
        vectors = np.array(list(_embedder().embed(texts)), dtype=np.float32)
    if vectors.shape != (len(texts), VECTOR_DIM):
        raise PipelineError(
            f"embedding shape {vectors.shape} != ({len(texts)}, {VECTOR_DIM}) - "
            f"the model at {EMBED_MODEL_NAME} isn't what this build expects."
        )
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return vectors / norms


def embed_query(query: str):
    """Embed one bare query with the same model and normalization."""
    return embed_texts([query], progress=lambda _msg: None)[0]


def top_k_cosine(matrix, query_vec, k: int = 12) -> list[tuple[int, float]]:
    """Return matrix row indices and cosine scores in descending order."""
    import numpy as np

    if matrix.shape[0] == 0:
        return []
    scores = matrix @ query_vec
    k = min(k, matrix.shape[0])
    idx = np.argpartition(-scores, k - 1)[:k]
    idx = idx[np.argsort(-scores[idx])]
    return [(int(i), float(scores[i])) for i in idx]
