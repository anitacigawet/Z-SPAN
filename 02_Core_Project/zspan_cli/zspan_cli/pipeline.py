"""Chunk + embed + retrieve for `zspan process` — the tiny local
RAG at its lightest honest tier.

Chunking mirrors the flagship indexer (index_meeting_to_qdrant.py):
token-bounded chunks, target 400 / overlap 50, counted with the embedding
model's own tokenizer, each chunk carrying start/end seconds from the
word timings (that's what makes `[at MM:SS]` citations possible). When
the exact tokenizer can't load, a words-per-token approximation keeps
the pipeline running with slightly coarser boundaries — said out loud,
never silently.

Embedding is the flagship's exact model — BAAI/bge-small-en-v1.5,
384-dim, normalized — served through fastembed's ONNX runtime (the same
onnxruntime faster-whisper already installs) instead of the torch stack.
Vectors never cross machines, so torch-vs-ONNX numeric drift is
irrelevant: the workspace embeds and queries with the same local model.

Retrieval is pure-numpy cosine over the workspace's chunk BLOBs. A
meeting is a few hundred chunks; a vector database would be pure
dependency weight. No query-instruction prefix — the flagship's own
query path embeds the bare query string, and this mirrors it.
"""
from __future__ import annotations

from dataclasses import dataclass

from .local_retrieval import (
    CHUNKER_VERSION,
    CHUNK_TOKEN_OVERLAP,
    CHUNK_TOKEN_TARGET,
    EMBEDDING_NORMALIZATION,
    EMBED_MODEL_NAME,
    SIMILARITY_METRIC,
    VECTOR_DIM,
    Chunk,
    PipelineError,
    chunk_transcript,
    embed_query,
    embed_texts,
    load_token_counter,
    top_k_cosine,
)


@dataclass
class RetrievedChunk:
    chunk_index: int
    text: str
    start_seconds: float
    end_seconds: float
    score: float

