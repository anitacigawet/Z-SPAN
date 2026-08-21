"""Persistent flagship transcript-chunk and embedding storage.

The tables live in the canonical ``meetings_cache.db`` rather than the CLI
workspace database because flagship chunks carry diarization metadata and an
explicit index-version record.  Every meeting replacement is one SQLite
transaction, so retrieval never observes a half-rebuilt matrix.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

_PARSERS_DIR = Path(__file__).resolve().parent.parent / "council_navigator" / "parsers"
DEFAULT_DB_PATH = _PARSERS_DIR / "meetings_cache.db"


@dataclass(frozen=True)
class StoredChunk:
    meeting_id: int
    chunk_index: int
    text: str
    start_seconds: float
    end_seconds: float
    speaker_turns: Optional[list[dict[str, Any]]]


def resolve_db_path(db_path: Optional[Path | str] = None) -> Path:
    if db_path is not None:
        return Path(db_path)
    return Path(os.environ.get("ZSPAN_DB_PATH") or DEFAULT_DB_PATH)


def connect(db_path: Optional[Path | str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(resolve_db_path(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the local retrieval schema idempotently."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS local_retrieval_indexes (
            meeting_id INTEGER PRIMARY KEY,
            transcript_sha256 TEXT NOT NULL,
            embed_model TEXT NOT NULL,
            vector_dim INTEGER NOT NULL,
            chunk_token_target INTEGER NOT NULL,
            chunk_token_overlap INTEGER NOT NULL,
            chunker_version TEXT NOT NULL,
            indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS local_retrieval_chunks (
            meeting_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            text TEXT NOT NULL,
            start_seconds REAL NOT NULL,
            end_seconds REAL NOT NULL,
            speaker_turns TEXT,
            embedding BLOB NOT NULL,
            PRIMARY KEY (meeting_id, chunk_index),
            FOREIGN KEY (meeting_id)
                REFERENCES local_retrieval_indexes(meeting_id)
                ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_local_retrieval_chunks_meeting
        ON local_retrieval_chunks(meeting_id, chunk_index)
        """
    )


def load_transcript_words(
    meeting_id: int,
    *,
    db_path: Optional[Path | str] = None,
) -> dict[str, Any]:
    """Load and validate the canonical cached ``transcript_words`` object."""
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT content, error
            FROM notebook_outputs
            WHERE meeting_id = ? AND output_type = 'transcript_words'
            """,
            (meeting_id,),
        ).fetchone()
    if row is None:
        raise LookupError(f"meeting {meeting_id} has no transcript_words cache row")
    if row["error"]:
        raise ValueError(
            f"meeting {meeting_id} transcript_words cache has error: {row['error']}"
        )
    raw = row["content"]
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(parsed, dict) or not isinstance(parsed.get("words"), list):
        raise ValueError(
            f"meeting {meeting_id} transcript_words content has no words list"
        )
    if not parsed["words"]:
        raise ValueError(f"meeting {meeting_id} transcript_words list is empty")
    return parsed


def save_transcript_words(
    meeting_id: int,
    transcript: dict[str, Any],
    *,
    db_path: Optional[Path | str] = None,
) -> None:
    """Replace only the cached transcript content, preserving custody columns."""
    payload = json.dumps(transcript, ensure_ascii=False, separators=(",", ":"))
    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE notebook_outputs
            SET content = ?
            WHERE meeting_id = ? AND output_type = 'transcript_words'
            """,
            (payload, meeting_id),
        )
        if cursor.rowcount != 1:
            raise LookupError(
                f"meeting {meeting_id} has no unique transcript_words cache row"
            )


def transcript_hash(transcript: dict[str, Any]) -> str:
    """Stable digest covering words, timings, and diarization labels."""
    encoded = json.dumps(
        transcript,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def meeting_index_matches_transcript(
    meeting_id: int,
    transcript: dict[str, Any],
    *,
    db_path: Optional[Path | str] = None,
) -> bool:
    """Return whether the durable local index covers this exact transcript."""
    with connect(db_path) as conn:
        ensure_schema(conn)
        row = conn.execute(
            """
            SELECT transcript_sha256
            FROM local_retrieval_indexes
            WHERE meeting_id = ?
            """,
            (meeting_id,),
        ).fetchone()
    return row is not None and row["transcript_sha256"] == transcript_hash(transcript)


def replace_meeting_index(
    meeting_id: int,
    chunks: Sequence[Any],
    vectors: Any,
    speaker_turns: Sequence[Optional[list[dict[str, Any]]]],
    *,
    transcript_sha256: str,
    embed_model: str,
    vector_dim: int,
    chunk_token_target: int,
    chunk_token_overlap: int,
    chunker_version: str,
    db_path: Optional[Path | str] = None,
) -> None:
    """Atomically replace one meeting's index record and all chunk rows."""
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.shape != (len(chunks), vector_dim):
        raise ValueError(
            f"vector matrix shape {matrix.shape} != ({len(chunks)}, {vector_dim})"
        )
    if len(speaker_turns) != len(chunks):
        raise ValueError("speaker_turns length must match chunks length")

    conn = connect(db_path)
    try:
        ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM local_retrieval_indexes WHERE meeting_id = ?",
            (meeting_id,),
        )
        conn.execute(
            """
            INSERT INTO local_retrieval_indexes (
                meeting_id, transcript_sha256, embed_model, vector_dim,
                chunk_token_target, chunk_token_overlap, chunker_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                meeting_id,
                transcript_sha256,
                embed_model,
                vector_dim,
                chunk_token_target,
                chunk_token_overlap,
                chunker_version,
            ),
        )
        conn.executemany(
            """
            INSERT INTO local_retrieval_chunks (
                meeting_id, chunk_index, text, start_seconds, end_seconds,
                speaker_turns, embedding
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    meeting_id,
                    int(chunk.chunk_index),
                    str(chunk.text),
                    float(chunk.start_seconds),
                    float(chunk.end_seconds),
                    (
                        json.dumps(turns, ensure_ascii=False, separators=(",", ":"))
                        if turns
                        else None
                    ),
                    sqlite3.Binary(matrix[index].tobytes(order="C")),
                )
                for index, (chunk, turns) in enumerate(zip(chunks, speaker_turns))
            ],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def load_chunk_matrix(
    meeting_id: int,
    *,
    expected_model: str,
    expected_dim: int,
    expected_chunker_version: str,
    db_path: Optional[Path | str] = None,
) -> tuple[list[StoredChunk], np.ndarray]:
    """Load one validated meeting matrix in deterministic chunk order."""
    with connect(db_path) as conn:
        ensure_schema(conn)
        index_row = conn.execute(
            "SELECT * FROM local_retrieval_indexes WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchone()
        if index_row is None:
            return [], np.zeros((0, expected_dim), dtype=np.float32)
        if (
            index_row["embed_model"] != expected_model
            or int(index_row["vector_dim"]) != expected_dim
            or index_row["chunker_version"] != expected_chunker_version
        ):
            raise RuntimeError(
                f"meeting {meeting_id} local index is stale: "
                f"model={index_row['embed_model']!r}, dim={index_row['vector_dim']}, "
                f"chunker={index_row['chunker_version']!r}"
            )
        rows = conn.execute(
            """
            SELECT meeting_id, chunk_index, text, start_seconds, end_seconds,
                   speaker_turns, embedding
            FROM local_retrieval_chunks
            WHERE meeting_id = ?
            ORDER BY chunk_index
            """,
            (meeting_id,),
        ).fetchall()

    chunks: list[StoredChunk] = []
    vectors: list[np.ndarray] = []
    for row in rows:
        raw_turns = json.loads(row["speaker_turns"]) if row["speaker_turns"] else None
        chunks.append(StoredChunk(
            meeting_id=int(row["meeting_id"]),
            chunk_index=int(row["chunk_index"]),
            text=str(row["text"]),
            start_seconds=float(row["start_seconds"]),
            end_seconds=float(row["end_seconds"]),
            speaker_turns=raw_turns,
        ))
        vector = np.frombuffer(row["embedding"], dtype=np.float32)
        if vector.shape != (expected_dim,):
            raise ValueError(
                f"meeting {meeting_id} chunk {row['chunk_index']} has "
                f"vector shape {vector.shape}, expected ({expected_dim},)"
            )
        vectors.append(vector.copy())
    matrix = (
        np.stack(vectors).astype(np.float32, copy=False)
        if vectors
        else np.zeros((0, expected_dim), dtype=np.float32)
    )
    return chunks, matrix


def load_meeting_geography(
    meeting_id: int,
    *,
    db_path: Optional[Path | str] = None,
) -> tuple[str, str, str]:
    """Return ``(city, county, state)`` without requiring the table in tests."""
    with connect(db_path) as conn:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meetings'"
        ).fetchone()
        if table is None:
            return "", "", ""
        row = conn.execute(
            "SELECT city_name, county, state FROM meetings WHERE id = ?",
            (meeting_id,),
        ).fetchone()
    if row is None:
        return "", "", ""
    return str(row["city_name"] or ""), str(row["county"] or ""), str(row["state"] or "")
