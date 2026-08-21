"""~/.zspan/workspace.db — the private local workspace (one SQLite file).

Mirrors the flagship's shapes, simplified to meetings / outputs /
chunks. The database remains the user's local, removable copy and also keeps
durable retry state for the required private contribution intake.

Conventions inherited from the flagship codebase: idempotent DDL only
(IF NOT EXISTS everywhere), so re-running any command against an
existing workspace is always safe. `pull` populates meetings; `process`
fills outputs + chunks.

The meetings primary key is purely local. `public_id` is the external
meeting key, while `flagship_row_id` remains only as the legacy pull
contract's match column. The full source row is kept verbatim in
source_row_json so later chunks can read fields this schema didn't
promote to columns, without a migration.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Tuple

from zspan_cli.config import zspan_home

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY,          -- local workspace id
    city TEXT NOT NULL,
    county TEXT,
    state TEXT,
    title TEXT,
    meeting_date TEXT,               -- YYYY-MM-DD
    meeting_time TEXT,
    location TEXT,
    meeting_status TEXT,
    agenda_url TEXT,
    minutes_url TEXT,
    agenda_packet_url TEXT,
    video_url TEXT,
    source_row_json TEXT NOT NULL,   -- the flagship row, verbatim
    pulled_at TEXT NOT NULL,
    processed_at TEXT,  -- stamps on successful process
    transcript_path TEXT  -- local transcript artifact
);

CREATE TABLE IF NOT EXISTS outputs (
    meeting_id INTEGER NOT NULL,
    output_type TEXT NOT NULL,
    content TEXT,
    provider TEXT,
    model TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (meeting_id, output_type)
);

CREATE TABLE IF NOT EXISTS chunks (
    meeting_id INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    text TEXT,
    start_seconds REAL,
    end_seconds REAL,
    embedding BLOB,  -- float32 numpy bytes
    PRIMARY KEY (meeting_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS contribution_submissions (
    meeting_id INTEGER PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'submitted')),
    submission_public_id TEXT,
    updated_at TEXT NOT NULL,
    submitted_at TEXT,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_meetings_city_date
    ON meetings (city, meeting_date);
"""


def workspace_path() -> Path:
    return zspan_home() / "workspace.db"


def connect() -> sqlite3.Connection:
    """Open (creating if needed) the workspace with the schema applied.
    Idempotent — safe to call from every command."""
    path = workspace_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Own-user-only: the workspace holds the user's transcripts + meeting
    # data. sqlite3 would create the DB under the umask (0644 = world-
    # readable), so pre-create it 0600 BEFORE sqlite opens it (no
    # world-readable window) and keep the home dir 0700 so no sibling
    # file (transcripts/, media/) is reachable either. Best-effort;
    # Windows has no POSIX mode bits (the user-profile ACL is the wall).
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    if not path.exists():
        try:
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        except OSError:
            pass
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _apply_column_migrations(conn)
    return conn


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(
        row["name"] == column
        for row in conn.execute(f"PRAGMA table_info({table})")
    )


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Idempotent ALTER — the flagship's migration convention, so an
    older workspace upgrades in place on the next command."""
    if not _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _apply_column_migrations(conn: sqlite3.Connection) -> None:
    fresh_flagship_id = not _column_exists(conn, "meetings", "flagship_row_id")
    fresh_import_source = not _column_exists(conn, "meetings", "import_source")
    _ensure_column(conn, "meetings", "public_id", "TEXT")
    _ensure_column(conn, "meetings", "flagship_row_id", "INTEGER")
    _ensure_column(conn, "meetings", "import_source", "TEXT")
    if fresh_flagship_id:
        conn.execute(
            "UPDATE meetings SET flagship_row_id = id WHERE flagship_row_id IS NULL"
        )
    if fresh_import_source:
        conn.execute(
            "UPDATE meetings SET import_source = 'pull' WHERE import_source IS NULL"
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_meetings_public_id "
        "ON meetings(public_id) WHERE public_id IS NOT NULL"
    )
    # The post-synthesis gate's verdict travels with every cached output.
    _ensure_column(conn, "outputs", "gate_status", "TEXT")
    _ensure_column(conn, "outputs", "gate_log", "TEXT")
    # Registration metadata is nullable so pre-auth outputs remain
    # distinguishable and can be backfilled after sign-in.
    _ensure_column(conn, "outputs", "ribbon_token", "TEXT")
    _ensure_column(conn, "outputs", "generation_public_id", "TEXT")
    _ensure_column(conn, "outputs", "registration_state", "TEXT")
    _ensure_column(conn, "outputs", "registration_idempotency_key", "TEXT")
    _ensure_column(conn, "outputs", "registered_account", "TEXT")
    conn.commit()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def upsert_meeting(
    conn: sqlite3.Connection,
    row: Dict[str, Any],
    *,
    import_source: str = "pull",
) -> str:
    """Insert or refresh one flagship catalog row. Returns "new" or
    "updated" so `pull` can report honestly. Process-state columns
    (processed_at, transcript_path) are never touched by a re-pull."""
    public_id = (row.get("public_id") or "").strip() or None
    flagship_row_id = row.get("id")
    if flagship_row_id is None and public_id is None:
        raise ValueError("catalog row carries neither a flagship id nor a public_id")

    existing = None
    if public_id is not None:
        existing = conn.execute(
            "SELECT * FROM meetings WHERE public_id = ?", (public_id,)
        ).fetchone()
    if existing is None and flagship_row_id is not None:
        existing = conn.execute(
            "SELECT * FROM meetings WHERE flagship_row_id = ?", (flagship_row_id,)
        ).fetchone()

    values = {
        "public_id": public_id,
        "flagship_row_id": flagship_row_id,
        "import_source": import_source,
        "city": row.get("city_name") or row.get("city") or "",
        "county": row.get("county"),
        "state": row.get("state"),
        "title": row.get("meeting_title"),
        "meeting_date": row.get("meeting_date"),
        "meeting_time": row.get("meeting_time"),
        "location": row.get("meeting_location"),
        "meeting_status": row.get("meeting_status"),
        "agenda_url": row.get("agenda_url"),
        "minutes_url": row.get("minutes_url"),
        "agenda_packet_url": row.get("agenda_packet_url"),
        "video_url": row.get("video_url"),
        "source_row_json": json.dumps(row, ensure_ascii=False),
        "pulled_at": _utc_now_iso(),
    }
    if existing:
        values["id"] = int(existing["id"])
        values["public_id"] = public_id or existing["public_id"]
        values["flagship_row_id"] = (
            flagship_row_id
            if flagship_row_id is not None
            else existing["flagship_row_id"]
        )
        values["import_source"] = (
            "pull"
            if import_source == "pull" or existing["import_source"] == "pull"
            else import_source
        )
        assignments = ", ".join(f"{k} = :{k}" for k in values if k != "id")
        conn.execute(f"UPDATE meetings SET {assignments} WHERE id = :id", values)
        return "updated"
    columns = ", ".join(values)
    placeholders = ", ".join(f":{k}" for k in values)
    conn.execute(f"INSERT INTO meetings ({columns}) VALUES ({placeholders})", values)
    return "new"


def pull_stats(conn: sqlite3.Connection, city: str) -> Tuple[int, str]:
    """(row count, most recent meeting_date) for one city — the honest
    summary `pull` prints."""
    row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(meeting_date) AS latest FROM meetings WHERE city = ?",
        (city,),
    ).fetchone()
    return int(row["n"]), row["latest"] or "—"


# ---------------------------------------------------------------- reads


def get_meeting(conn: sqlite3.Connection, meeting_id: int):
    return conn.execute(
        "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
    ).fetchone()


def get_meeting_by_public_id(conn: sqlite3.Connection, public_id: str):
    return conn.execute(
        "SELECT * FROM meetings WHERE public_id = ?", (public_id,)
    ).fetchone()


def pick_processable(conn: sqlite3.Connection, city: str):
    """The default `zspan process` target: the most recent unprocessed
    meeting with a video source. None = nothing eligible (caller says
    which of the honest reasons applies)."""
    return conn.execute(
        """SELECT * FROM meetings
           WHERE city = ? AND video_url IS NOT NULL AND video_url != ''
             AND processed_at IS NULL
           ORDER BY meeting_date DESC, id DESC LIMIT 1""",
        (city,),
    ).fetchone()


def set_transcript_path(conn: sqlite3.Connection, meeting_id: int, path: str) -> None:
    conn.execute(
        "UPDATE meetings SET transcript_path = ? WHERE id = ?", (path, meeting_id)
    )
    conn.commit()


def mark_processed(conn: sqlite3.Connection, meeting_id: int) -> None:
    conn.execute(
        "UPDATE meetings SET processed_at = ? WHERE id = ?",
        (_utc_now_iso(), meeting_id),
    )
    conn.commit()


def prepare_contribution(
    conn: sqlite3.Connection,
    meeting_id: int,
    payload_sha256: str,
) -> str:
    """Return a stable retry key for these exact bytes and mark them pending.

    Regenerated bytes receive a new key and revoke local completion until the
    replacement package is accepted. Exact retries retain their prior key.
    """
    current = conn.execute(
        "SELECT * FROM contribution_submissions WHERE meeting_id = ?",
        (meeting_id,),
    ).fetchone()
    if current is not None and current["payload_sha256"] == payload_sha256:
        return str(current["idempotency_key"])
    idempotency_key = secrets.token_urlsafe(24)
    now = _utc_now_iso()
    conn.execute(
        """INSERT INTO contribution_submissions (
               meeting_id, idempotency_key, payload_sha256, state,
               submission_public_id, updated_at, submitted_at
           ) VALUES (?, ?, ?, 'pending', NULL, ?, NULL)
           ON CONFLICT(meeting_id) DO UPDATE SET
               idempotency_key = excluded.idempotency_key,
               payload_sha256 = excluded.payload_sha256,
               state = 'pending', submission_public_id = NULL,
               updated_at = excluded.updated_at, submitted_at = NULL""",
        (meeting_id, idempotency_key, payload_sha256, now),
    )
    conn.execute(
        "UPDATE meetings SET processed_at = NULL WHERE id = ?", (meeting_id,)
    )
    conn.commit()
    return idempotency_key


def mark_contribution_submitted(
    conn: sqlite3.Connection,
    meeting_id: int,
    *,
    payload_sha256: str,
    submission_public_id: str,
) -> None:
    now = _utc_now_iso()
    updated = conn.execute(
        """UPDATE contribution_submissions
           SET state = 'submitted', submission_public_id = ?,
               updated_at = ?, submitted_at = ?
           WHERE meeting_id = ? AND payload_sha256 = ? AND state = 'pending'""",
        (submission_public_id, now, now, meeting_id, payload_sha256),
    )
    if updated.rowcount != 1:
        conn.rollback()
        raise ValueError("contribution package changed before submission completed")
    conn.commit()


def contribution_submission(conn: sqlite3.Connection, meeting_id: int):
    return conn.execute(
        "SELECT * FROM contribution_submissions WHERE meeting_id = ?",
        (meeting_id,),
    ).fetchone()


def chunk_count(conn: sqlite3.Connection, meeting_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM chunks WHERE meeting_id = ?", (meeting_id,)
    ).fetchone()
    return int(row["n"])


def replace_chunks(conn: sqlite3.Connection, meeting_id: int, chunks, vectors) -> None:
    """Store a meeting's chunk set + embeddings atomically (replace, not
    append — a re-chunk always supersedes whole)."""
    conn.execute("DELETE FROM chunks WHERE meeting_id = ?", (meeting_id,))
    for chunk, vec in zip(chunks, vectors):
        conn.execute(
            """INSERT INTO chunks
               (meeting_id, chunk_index, text, start_seconds, end_seconds, embedding)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (meeting_id, chunk.chunk_index, chunk.text,
             chunk.start_seconds, chunk.end_seconds, vec.tobytes()),
        )
    conn.commit()


def load_chunk_matrix(conn: sqlite3.Connection, meeting_id: int):
    """(rows, matrix) for retrieval — rows ordered by chunk_index, matrix
    row i is rows[i]'s embedding."""
    import numpy as np

    rows = conn.execute(
        """SELECT chunk_index, text, start_seconds, end_seconds, embedding
           FROM chunks WHERE meeting_id = ? ORDER BY chunk_index""",
        (meeting_id,),
    ).fetchall()
    if not rows:
        return [], np.zeros((0, 0), dtype=np.float32)
    matrix = np.vstack([
        np.frombuffer(r["embedding"], dtype=np.float32) for r in rows
    ])
    return rows, matrix


def save_output(
    conn: sqlite3.Connection,
    meeting_id: int,
    output_type: str,
    *,
    content: str,
    provider: str,
    model: str,
    gate_status: str,
    gate_log: str,
    registration_idempotency_key: str | None = None,
    registered_account: str | None = None,
) -> None:
    """Cache fresh content in the retryable pending state.

    Registration identity is cleared on every insert/replace. This makes the
    content write the durable first half of the pending-first contract and
    prevents ``--force`` from leaving an old ribbon attached to new bytes.
    """
    idempotency_key = registration_idempotency_key or secrets.token_urlsafe(24)
    conn.execute(
        """INSERT INTO outputs
           (meeting_id, output_type, content, provider, model, created_at,
            gate_status, gate_log, ribbon_token, generation_public_id,
            registration_state, registration_idempotency_key, registered_account)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 'pending', ?, ?)
           ON CONFLICT (meeting_id, output_type) DO UPDATE SET
             content = excluded.content, provider = excluded.provider,
             model = excluded.model, created_at = excluded.created_at,
             gate_status = excluded.gate_status, gate_log = excluded.gate_log,
             ribbon_token = NULL, generation_public_id = NULL,
             registration_state = 'pending',
             registration_idempotency_key = excluded.registration_idempotency_key,
             registered_account = excluded.registered_account""",
        (meeting_id, output_type, content, provider, model,
         _utc_now_iso(), gate_status, gate_log, idempotency_key,
         registered_account),
    )
    conn.commit()


def update_registration(
    conn: sqlite3.Connection,
    meeting_id: int,
    output_type: str,
    *,
    ribbon_token: str | None,
    generation_public_id: str | None,
    state: str,
) -> None:
    if state not in {"registered", "pending"}:
        raise ValueError(f"invalid registration state: {state}")
    conn.execute(
        """UPDATE outputs
           SET ribbon_token = ?, generation_public_id = ?, registration_state = ?
           WHERE meeting_id = ? AND output_type = ?""",
        (ribbon_token, generation_public_id, state, meeting_id, output_type),
    )
    conn.commit()


def prepare_legacy_registration(
    conn: sqlite3.Connection,
    meeting_id: int,
    output_type: str,
    *,
    idempotency_key: str,
    registered_account: str,
) -> None:
    """Bind a pre-auth output to the account attempting its first backfill."""
    conn.execute(
        """UPDATE outputs
           SET registration_state = 'pending',
               registration_idempotency_key = ?, registered_account = ?,
               ribbon_token = NULL, generation_public_id = NULL
           WHERE meeting_id = ? AND output_type = ?
             AND registration_state IS NULL""",
        (idempotency_key, registered_account, meeting_id, output_type),
    )
    conn.commit()


def rows_needing_registration(conn: sqlite3.Connection, meeting_id: int):
    """Pending and legacy-with-content outputs eligible for a retry pass."""
    return conn.execute(
        """SELECT * FROM outputs
           WHERE meeting_id = ? AND content IS NOT NULL
             AND (registration_state = 'pending' OR registration_state IS NULL)
           ORDER BY output_type""",
        (meeting_id,),
    ).fetchall()


def existing_outputs(conn: sqlite3.Connection, meeting_id: int) -> dict:
    """output_type → gate_status for what's already synthesized (the
    resume check — re-runs skip these unless --force)."""
    return {
        r["output_type"]: r["gate_status"]
        for r in conn.execute(
            "SELECT output_type, gate_status FROM outputs WHERE meeting_id = ?",
            (meeting_id,),
        )
    }


def processed_meetings(conn: sqlite3.Connection):
    """Meetings with at least one synthesized output, newest first — the
    `zspan open` index. Fully-processed and partial both list (the render
    shows what exists; honest absence for the rest)."""
    return conn.execute(
        """SELECT m.*, COUNT(o.output_type) AS output_count
           FROM meetings m JOIN outputs o ON o.meeting_id = m.id
           GROUP BY m.id
           ORDER BY m.meeting_date DESC, m.id DESC""",
    ).fetchall()


def all_meetings(conn: sqlite3.Connection):
    """Every meeting with its local output count, newest first."""
    return conn.execute(
        """SELECT m.*, COUNT(o.output_type) AS output_count
           FROM meetings m LEFT JOIN outputs o ON o.meeting_id = m.id
           GROUP BY m.id
           ORDER BY m.meeting_date DESC, m.id DESC""",
    ).fetchall()


def load_outputs(conn: sqlite3.Connection, meeting_id: int) -> dict:
    """output_type → {content, provider, model, created_at, gate_status,
    gate_log} for one meeting — the render's data source.

    Legacy key_decisions rows may contain the prompt's private trailing audit
    block.  Normalize those rows in memory so an existing generation heals on
    its next read without rewriting registered bytes (and therefore without
    invalidating its registration hash).
    """
    from zspan_cli.gate import strip_key_decisions_audit

    outputs = {}
    for r in conn.execute(
        "SELECT * FROM outputs WHERE meeting_id = ?", (meeting_id,)
    ):
        output_type = r["output_type"]
        content = r["content"]
        gate_status = r["gate_status"]
        if output_type == "key_decisions":
            content = strip_key_decisions_audit(content or "")
            if not content:
                gate_status = "empty"
        outputs[output_type] = {
            "content": content,
            "provider": r["provider"],
            "model": r["model"],
            "created_at": r["created_at"],
            "gate_status": gate_status,
            "gate_log": r["gate_log"],
            "ribbon_token": r["ribbon_token"],
            "generation_public_id": r["generation_public_id"],
            "registration_state": r["registration_state"],
            "registration_idempotency_key": r["registration_idempotency_key"],
            "registered_account": r["registered_account"],
        }
    return outputs
