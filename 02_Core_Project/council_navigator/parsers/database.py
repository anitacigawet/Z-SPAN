#!/usr/bin/env python3.11
"""
Database caching layer for city council meeting data.
Uses SQLite for fast local storage with automatic refresh.
"""
import errno
import hashlib
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import stat
import string
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Any, Literal, Optional, Protocol, TypeAlias

# Flask starts from parsers/, unlike the worker, so make the sibling pipeline
# package importable before consuming its executable output-contract registry.
_CORE_PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(_CORE_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_PROJECT_DIR))

from zspan_pipeline.output_contracts import PUBLICATION_CONTRACT
from zspan_pipeline import citation_validator as _citation_validator
from council_navigator.parsers import quote_align as _quote_align

try:
    from parsers import operator_identity
except ImportError:  # Direct imports from parsers/ at runtime.
    import operator_identity

# TOPIC_TAGS lives in parsers/topic_tags.py — canonical Python home of
# the controlled 5-tag vocabulary. Import under an alias so downstream
# derivations (e.g. _TRUTH_BOOK_FEATURED_LANES) stay one-line.
try:
    from parsers.topic_tags import TOPIC_TAGS as _TOPIC_TAGS
except ImportError:
    from topic_tags import TOPIC_TAGS as _TOPIC_TAGS

logger = logging.getLogger(__name__)

# Default: alongside this file (parsers/meetings_cache.db) for local self-hosters.
# Override via ZSPAN_DB_PATH for deployments where the DB lives on a persistent
# volume separate from the code (e.g., the flagship Railway container mounts
# /data and points ZSPAN_DB_PATH=/data/meetings_cache.db so the SQLite file
# survives container redeploys).
DB_PATH = os.environ.get('ZSPAN_DB_PATH') or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'meetings_cache.db'
)


def _correct_mode_if_wider(
    path: str | Path,
    required_mode: int,
    label: str,
) -> None:
    """Best-effort tightening for an existing database-related path."""
    # Windows exposes compatibility bits rather than the file's NTFS ACL;
    # POSIX chmod values therefore cannot express or verify the real policy.
    if os.name != "posix":
        return
    try:
        current_mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        return
    if (current_mode & ~required_mode) == 0:
        return
    logger.warning(
        "%s %s has mode %04o; tightening to %04o",
        label,
        path,
        current_mode,
        required_mode,
    )
    try:
        os.chmod(path, required_mode)
    except OSError as exc:
        # Railway's /data mount may reject chmod; keep this batch fail-open
        # until the production mount behavior has been verified.
        logger.warning(
            "Could not tighten %s %s to %04o; continuing: %s",
            label,
            path,
            required_mode,
            exc,
        )


def _ensure_private_db_parent(parent: Path) -> None:
    """Create the DB parent at 0700 or best-effort tighten its mode."""
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if os.name != "posix":
        return
    current_mode = stat.S_IMODE(parent.stat().st_mode)
    if current_mode == 0o700:
        return
    logger.warning(
        "Database directory %s has mode %04o; correcting to 0700",
        parent,
        current_mode,
    )
    try:
        parent.chmod(0o700)
    except OSError as exc:
        logger.warning(
            "Could not correct database directory %s to 0700; continuing: %s",
            parent,
            exc,
        )


def _correct_db_sidecar_permissions(db_path: str) -> None:
    """Best-effort tighten SQLite WAL/SHM sidecars when present."""
    for suffix in ("-wal", "-shm"):
        _correct_mode_if_wider(f"{db_path}{suffix}", 0o600, "SQLite sidecar")


def _ensure_secure_db_perms(db_path: str) -> None:
    """Secure the DB parent/file before SQLite opens the database."""
    db_file = Path(db_path)
    _ensure_private_db_parent(db_file.parent)

    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        fd = os.open(db_file, flags, 0o600)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise
    else:
        try:
            if os.name != "posix":
                pass
            elif hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            else:
                os.chmod(db_file, 0o600)
        except OSError as exc:
            logger.warning(
                "Could not set new SQLite database %s to 0600; continuing: %s",
                db_file,
                exc,
            )
        finally:
            os.close(fd)

    _correct_mode_if_wider(db_file, 0o600, "SQLite database")
    _correct_db_sidecar_permissions(db_path)


# Cache TTL in seconds (6 hours default)
CACHE_TTL = 6 * 60 * 60

PUBLIC_ID_RE = re.compile(r"^m_[0-9A-Za-z]{22}$")
_PUBLIC_ID_ALPHABET = string.digits + string.ascii_uppercase + string.ascii_lowercase
_PUBLIC_ID_COLLISION_RETRIES = 100
WATERMARK_BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
_RIBBON_COLLISION_RETRIES = 8

# These are the generated-output surfaces that currently render a
# WatermarkRibbon in the flagship client. Keep the registration boundary
# aligned with those call sites: internal custody rows such as
# transcript_words must not acquire a public provenance ribbon accidentally.
FLAGSHIP_RIBBON_OUTPUT_TYPES = frozenset({
    "community_calls_to_action",
    "key_decisions",
    "synopsis",
})


def generate_public_id() -> str:
    """Return a CSPRNG-backed opaque external meeting identifier."""
    suffix = "".join(secrets.choice(_PUBLIC_ID_ALPHABET) for _ in range(22))
    return f"m_{suffix}"


def generate_generation_public_id() -> str:
    """Return a CSPRNG-backed opaque external generation identifier."""
    suffix = "".join(secrets.choice(_PUBLIC_ID_ALPHABET) for _ in range(22))
    return f"g_{suffix}"


def derive_watermark_token(meeting_id: int, output_type: str) -> str:
    """Reproduce the retired public derivation for legacy classification."""
    digest = hashlib.sha256(
        f"zspan-output:{meeting_id}:{output_type}".encode("utf-8")
    ).digest()[:5]
    result = []
    for bit_offset in range(0, 40, 5):
        value = 0
        for bit_number in range(5):
            bit_index = bit_offset + bit_number
            byte_index = bit_index // 8
            bit_in_byte = bit_index % 8
            bit = (digest[byte_index] >> (7 - bit_in_byte)) & 1
            value = (value << 1) | bit
        result.append(WATERMARK_BASE32_ALPHABET[value])
    return "".join(result)


def find_flagship_watermark_row(
    cursor: sqlite3.Cursor, token: str
) -> Optional[sqlite3.Row]:
    """Return the notebook output whose legacy derived token matches ``token``.

    This is retained only to classify publicly reproducible pre-registry
    identifiers. A hit is not authenticated provenance.
    """
    rows = cursor.execute(
        """
        SELECT no.id, no.meeting_id, no.output_type, no.prompt_version,
               no.generated_at, m.meeting_title, m.city_name
        FROM notebook_outputs AS no
        LEFT JOIN meetings AS m ON m.id = no.meeting_id
        """
    ).fetchall()
    for row in rows:
        if derive_watermark_token(row["meeting_id"], row["output_type"]) == token:
            return row
    return None


def find_flagship_generation_by_token(
    cursor: sqlite3.Cursor, token: str
) -> Optional[sqlite3.Row]:
    """Return the account-bound flagship registration for ``token``."""
    return cursor.execute(
        """
        SELECT fg.id AS generation_id, fg.ribbon_token,
               fg.notebook_output_id, fg.meeting_id, fg.output_type,
               fg.user_id, fg.status, fg.minted_at, fg.created_at,
               no.prompt_version, no.generated_at AS output_generated_at,
               m.meeting_title, m.city_name
        FROM flagship_generations AS fg
        INNER JOIN notebook_outputs AS no ON no.id = fg.notebook_output_id
        LEFT JOIN meetings AS m ON m.id = fg.meeting_id
        WHERE fg.ribbon_token = ?
        """,
        (token,),
    ).fetchone()


def _ribbon_token_in_use(cursor: sqlite3.Cursor, candidate: str) -> bool:
    """Check every live and legacy ribbon namespace for ``candidate``."""
    cli_match = cursor.execute(
        "SELECT 1 FROM cli_generations WHERE ribbon_token = ?",
        (candidate,),
    ).fetchone()
    if cli_match is not None:
        return True
    flagship_match = cursor.execute(
        "SELECT 1 FROM flagship_generations WHERE ribbon_token = ?",
        (candidate,),
    ).fetchone()
    if flagship_match is not None:
        return True
    return find_flagship_watermark_row(cursor, candidate) is not None


def _mint_ribbon_token(cursor: sqlite3.Cursor, namespace: str) -> str:
    """Mint one token unused by CLI, flagship, and legacy namespaces."""
    for _ in range(_RIBBON_COLLISION_RETRIES):
        candidate = "".join(
            secrets.choice(WATERMARK_BASE32_ALPHABET) for _ in range(8)
        )
        if _ribbon_token_in_use(cursor, candidate):
            continue
        return candidate
    raise RuntimeError(
        f"Unable to mint a collision-free {namespace} ribbon token"
    )


def mint_cli_ribbon_token(cursor: sqlite3.Cursor) -> str:
    """Mint a registry token unused by every ribbon namespace."""
    return _mint_ribbon_token(cursor, "CLI")


def mint_flagship_ribbon_token(cursor: sqlite3.Cursor) -> str:
    """Mint a flagship token unused by every ribbon namespace."""
    return _mint_ribbon_token(cursor, "flagship")


def _generate_available_public_id(cursor: sqlite3.Cursor) -> str:
    """Generate an ID unused by both canonical meeting rows and aliases."""
    for _ in range(_PUBLIC_ID_COLLISION_RETRIES):
        candidate = generate_public_id()
        exists = cursor.execute(
            """
            SELECT 1 FROM meetings WHERE public_id = ?
            UNION ALL
            SELECT 1 FROM meeting_public_id_aliases WHERE alias_public_id = ?
            LIMIT 1
            """,
            (candidate, candidate),
        ).fetchone()
        if exists is None:
            return candidate
    raise RuntimeError("Unable to generate an unused meeting public_id")


def ensure_meeting_public_ids(conn: sqlite3.Connection) -> int:
    """Fill NULL meeting public IDs atomically without changing assigned IDs."""
    rows = conn.execute(
        "SELECT id FROM meetings WHERE public_id IS NULL ORDER BY id"
    ).fetchall()
    if not rows:
        return 0

    filled = 0
    conn.execute("SAVEPOINT ensure_meeting_public_ids")
    try:
        for row in rows:
            meeting_id = row[0]
            for _ in range(_PUBLIC_ID_COLLISION_RETRIES):
                candidate = _generate_available_public_id(conn.cursor())
                try:
                    cursor = conn.execute(
                        "UPDATE meetings SET public_id = ? "
                        "WHERE id = ? AND public_id IS NULL",
                        (candidate, meeting_id),
                    )
                except sqlite3.IntegrityError:
                    # Another row won the astronomically unlikely UNIQUE race.
                    continue
                filled += cursor.rowcount
                break
            else:
                raise RuntimeError(
                    f"Unable to assign a unique public_id to meeting {meeting_id}"
                )
        conn.execute("RELEASE SAVEPOINT ensure_meeting_public_ids")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT ensure_meeting_public_ids")
        conn.execute("RELEASE SAVEPOINT ensure_meeting_public_ids")
        raise
    return filled


# Child tables swept when twin meeting rows merge. CONTENT tables hold
# regenerable per-meeting artifacts: when the kept row already has rows
# there, the dropped twin's rows are duplicates of the same meeting's
# content and are deleted; when the kept row has none, the twin's rows
# repoint. AUDIT tables are history and always repoint (doubled log rows
# beat lost log rows).
_TWIN_MERGE_CONTENT_TABLES = (
    "work_orders", "notebook_outputs", "quotes", "member_quotes",
    "member_attendance", "meeting_speaker_roster", "transcript_nodes",
    "tracked_claims", "live_streams", "episode_sim_queries",
)
_TWIN_MERGE_AUDIT_TABLES = (
    "byok_audit_runs", "librarian_gate_events", "flagship_sync_log",
    "extraction_anomalies", "quote_verifications", "corrections",
    "correction_pending_review", "suggestions", "meeting_media_archive",
    "operator_review_events",
)


def _merge_title_suffix_twin_meetings(conn) -> int:
    """Merge 'Title - Mon DD, YYYY' scraper-era rows onto their clean twins.

    An older parser format baked the meeting date into the title, so the
    exact natural-key dedupe can never merge those rows with the current
    parser's clean-titled row for the same real-world meeting. Guard: the
    suffix must parse as '%b %d, %Y' AND equal the row's own meeting_date —
    'City Council - Special Session' or the Dewey-Humboldt 'X - Zoom Video'
    class never match. Keep-selection is state-driven: published wins, else
    more notebook_outputs, else the lower id. The kept row ends up carrying
    the CLEAN title (today's natural key, so future scrapes upsert onto it
    instead of re-creating the twin) and the dropped row's public_id becomes
    a permanent alias (the PI-2 machinery). Idempotent: merged pairs no
    longer match the join.
    """
    cursor = conn.cursor()
    # Fresh-DB bootstrap guard: is_published + public_id are added by later
    # idempotent migrations in the same startup pass. A DB that lacks them is
    # mid-bootstrap and cannot hold legacy twins yet — skip; the next init
    # pass (columns present) merges. On the live DB both exist and the merge
    # runs immediately.
    if not _column_exists(cursor, "meetings", "is_published") or not _column_exists(
        cursor, "meetings", "public_id"
    ):
        return 0
    pairs = cursor.execute(
        """
        SELECT a.id AS suffixed_id, a.meeting_title AS suffixed_title,
               a.is_published AS suffixed_pub, a.meeting_date,
               a.city_name, a.state,
               b.id AS clean_id, b.meeting_title AS clean_title,
               b.is_published AS clean_pub
        FROM meetings a
        JOIN meetings b
          ON a.city_name = b.city_name AND a.state = b.state
         AND a.meeting_date = b.meeting_date AND a.id != b.id
        WHERE a.meeting_title LIKE b.meeting_title || ' - %'
        ORDER BY a.id
        """
    ).fetchall()
    if not pairs:
        return 0

    from collections import Counter
    clean_counts = Counter(p["clean_id"] for p in pairs)
    suffixed_counts = Counter(p["suffixed_id"] for p in pairs)
    existing_tables = {
        r[0]
        for r in cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    merged = 0
    conn.execute("SAVEPOINT merge_title_suffix_twins")
    try:
        consumed: set = set()
        for pair in pairs:
            s_id, c_id = pair["suffixed_id"], pair["clean_id"]
            if s_id in consumed or c_id in consumed:
                continue
            if clean_counts[c_id] > 1 or suffixed_counts[s_id] > 1:
                logger.warning(
                    "twin-merge: ambiguous multi-match around meetings %s/%s "
                    "(%s %s) — skipped, needs manual review",
                    s_id, c_id, pair["city_name"], pair["meeting_date"],
                )
                continue
            suffix = pair["suffixed_title"][len(pair["clean_title"]) + 3:]
            try:
                parsed = datetime.strptime(suffix, "%b %d, %Y").strftime("%Y-%m-%d")
            except ValueError:
                continue  # venue/topic suffix ('Zoom Video', 'Special Session')
            if parsed != pair["meeting_date"]:
                continue

            # State-driven keep selection: published wins, else outputs, else age.
            if bool(pair["suffixed_pub"]) != bool(pair["clean_pub"]):
                keep_id, drop_id = (
                    (s_id, c_id) if pair["suffixed_pub"] else (c_id, s_id)
                )
            else:
                s_outputs = cursor.execute(
                    "SELECT COUNT(*) FROM notebook_outputs WHERE meeting_id = ?",
                    (s_id,),
                ).fetchone()[0]
                c_outputs = cursor.execute(
                    "SELECT COUNT(*) FROM notebook_outputs WHERE meeting_id = ?",
                    (c_id,),
                ).fetchone()[0]
                if s_outputs != c_outputs:
                    keep_id, drop_id = (
                        (s_id, c_id) if s_outputs > c_outputs else (c_id, s_id)
                    )
                else:
                    keep_id, drop_id = (min(s_id, c_id), max(s_id, c_id))

            for table in _TWIN_MERGE_CONTENT_TABLES:
                if table not in existing_tables:
                    continue
                keep_has = cursor.execute(
                    f"SELECT 1 FROM {table} WHERE meeting_id = ? LIMIT 1",
                    (keep_id,),
                ).fetchone()
                if keep_has:
                    cursor.execute(
                        f"DELETE FROM {table} WHERE meeting_id = ?", (drop_id,)
                    )
                else:
                    cursor.execute(
                        f"UPDATE {table} SET meeting_id = ? WHERE meeting_id = ?",
                        (keep_id, drop_id),
                    )
            for table in _TWIN_MERGE_AUDIT_TABLES:
                if table not in existing_tables:
                    continue
                cursor.execute(
                    f"UPDATE {table} SET meeting_id = ? WHERE meeting_id = ?",
                    (keep_id, drop_id),
                )

            # Alias discipline (PI-2): the dropped identity stays resolvable.
            drop_public_id = cursor.execute(
                "SELECT public_id FROM meetings WHERE id = ?", (drop_id,)
            ).fetchone()
            cursor.execute(
                "UPDATE meeting_public_id_aliases SET canonical_meeting_id = ? "
                "WHERE canonical_meeting_id = ?",
                (keep_id, drop_id),
            )
            if drop_public_id and drop_public_id[0]:
                cursor.execute(
                    "INSERT OR IGNORE INTO meeting_public_id_aliases "
                    "(alias_public_id, canonical_meeting_id) VALUES (?, ?)",
                    (drop_public_id[0], keep_id),
                )

            # Delete the twin FIRST (it may hold the clean natural key), then
            # retitle the kept row onto today's key.
            cursor.execute("DELETE FROM meetings WHERE id = ?", (drop_id,))
            cursor.execute(
                "UPDATE meetings SET meeting_title = ?, "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND meeting_title != ?",
                (pair["clean_title"], keep_id, pair["clean_title"]),
            )
            consumed.add(s_id)
            consumed.add(c_id)
            merged += 1
        conn.execute("RELEASE SAVEPOINT merge_title_suffix_twins")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT merge_title_suffix_twins")
        conn.execute("RELEASE SAVEPOINT merge_title_suffix_twins")
        raise
    if merged:
        logger.info(
            "twin-merge: merged %d title-suffix duplicate meeting pairs", merged
        )
    return merged


def get_connection():
    """Get a database connection with WAL mode for better concurrency."""
    _ensure_secure_db_perms(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    _correct_db_sidecar_permissions(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    _correct_db_sidecar_permissions(DB_PATH)
    # busy_timeout: a writer that finds the DB locked waits up to 5s for the
    # lock instead of raising "database is locked" immediately. Matters now that
    # the metered fleet (D-120) writes spend_observed rows concurrently with
    # Flask + the worker; WAL allows concurrent readers but writes still
    # serialize, so the timeout turns a rare collision into a brief wait.
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def publication_text_violation(value: object) -> Optional[str]:
    """Check publish/unpublish prose against owner and legacy identities."""
    try:
        from parsers.google_oauth import get_owner_emails
    except ImportError:  # Direct imports from parsers/ at runtime.
        from google_oauth import get_owner_emails

    conn = get_connection()
    try:
        return operator_identity.publication_text_violation(
            value,
            conn=conn,
            database_key=str(Path(DB_PATH).resolve()),
            owner_emails=get_owner_emails(),
        )
    finally:
        conn.close()


def init_episode_sim_queries_schema(cursor: sqlite3.Cursor) -> None:
    """Create the atomic three-slot signed-out Librarian answer cache."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS episode_sim_queries (
            meeting_id INTEGER NOT NULL,
            query_slot INTEGER NOT NULL,
            question_text TEXT NOT NULL,
            answer_text TEXT NOT NULL,
            prompt_name TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            prompt_hash TEXT NOT NULL,
            vocab_version TEXT NOT NULL,
            query_hash TEXT NOT NULL,
            answer_digest TEXT NOT NULL,
            model_id TEXT NOT NULL,
            retrieved_chunk_ids TEXT NOT NULL,
            run_id TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            PRIMARY KEY (meeting_id, query_slot),
            FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE,
            CHECK (query_slot BETWEEN 0 AND 2)
        )
    """)


def init_db():
    """Initialize the database schema."""
    conn = get_connection()
    cursor = conn.cursor()
    
    # Cities table - stores city metadata
    # Note: UNIQUE is on (name, county, state) — same name can exist across states
    # (e.g., Maricopa AZ vs Maricopa CA). See DECISIONS.md § D-027.
    # Existing databases with the old `name UNIQUE` constraint are migrated by
    # _migrate_cities_unique_to_composite() below.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            county TEXT NOT NULL,
            state TEXT NOT NULL,  -- resolved via resolve_city_state; no Arizona default (s53 NV bug class)
            calendar_url TEXT,
            parser_file TEXT,
            calendar_format TEXT,
            status TEXT DEFAULT 'active',
            last_scraped TIMESTAMP,
            scrape_success INTEGER DEFAULT 0,
            total_meetings INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(name, county, state)
        )
    """)
    _migrate_cities_unique_to_composite(cursor)
    
    # Meetings table - stores all meeting data
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS meetings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city_id INTEGER NOT NULL,
            city_name TEXT NOT NULL,
            county TEXT NOT NULL,
            state TEXT NOT NULL,  -- resolved via resolve_city_state; no Arizona default (s53 NV bug class)
            meeting_title TEXT,
            meeting_date TEXT,
            meeting_time TEXT,
            meeting_location TEXT,
            meeting_status TEXT,
            agenda_url TEXT,
            minutes_url TEXT,
            video_url TEXT,
            agenda_packet_url TEXT,
            ecomment_url TEXT,
            meeting_id TEXT,
            summary TEXT,
            raw_data TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (city_id) REFERENCES cities(id)
        )
    """)

    # Three pre-computed, cited factual answers for the signed-out public
    # Librarian. This belongs to central bootstrap so local and cloud databases
    # receive identical storage before generation, sync, or serving begins.
    init_episode_sim_queries_schema(cursor)
    
    # Scrape log table - tracks scraping history
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scrape_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city_name TEXT NOT NULL,
            scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            success INTEGER NOT NULL,
            meetings_found INTEGER DEFAULT 0,
            error_message TEXT,
            duration_seconds REAL
        )
    """)
    
    # Council members table - for civic engagement features
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS council_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city_name TEXT NOT NULL,
            name TEXT NOT NULL,
            title TEXT,
            email TEXT,
            phone TEXT,
            photo_url TEXT,
            ward TEXT,
            term_start TEXT,
            term_end TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Create indexes for fast lookups
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_meetings_city ON meetings(city_name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_meetings_county ON meetings(county)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_meetings_state ON meetings(state)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_meetings_date ON meetings(meeting_date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_meetings_title ON meetings(meeting_title)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_cities_county ON cities(county)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_cities_state ON cities(state)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_scrape_log_city ON scrape_log(city_name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_council_city ON council_members(city_name)")

    # D-164: opaque, immutable identifiers for every external meeting surface.
    # SQLite cannot add a UNIQUE column in-place, so the additive migration is
    # column -> unique index -> NULL-only backfill. Existing non-NULL values are
    # never rewritten.
    public_id_column_created = not _column_exists(cursor, "meetings", "public_id")
    if public_id_column_created:
        cursor.execute("ALTER TABLE meetings ADD COLUMN public_id TEXT")
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_meetings_public_id "
        "ON meetings(public_id)"
    )
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS meeting_public_id_aliases (
            alias_public_id TEXT PRIMARY KEY,
            canonical_meeting_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_meeting_public_id_aliases_canonical "
        "ON meeting_public_id_aliases(canonical_meeting_id)"
    )
    public_ids_filled = ensure_meeting_public_ids(conn)
    if public_ids_filled and not public_id_column_created:
        # The spec claims NOT NULL but SQLite's additive migration can't
        # enforce it; both insert sites generate the id, so a late NULL means
        # a NEW insert path skipped generation. Heal it (done above) but say
        # so LOUDLY — silent healing hides the defective caller forever.
        logger.warning(
            "public_id backfill healed %d meeting rows AFTER the initial "
            "migration — some insert path is writing meetings without "
            "public_id generation (every insert site must set it; see "
            "PUBLIC_INTERFACE_SPEC § 1).",
            public_ids_filled,
        )

    # D-038 (2026-05-13): UNIQUE natural-key index on meetings supports the
    # UPSERT pattern in cache_meetings(). Before D-038 the cache refresh did
    # a wholesale DELETE + INSERT, which (combined with ON DELETE CASCADE on
    # work_orders + notebook_outputs + quote_verifications + member_*)
    # destroyed every dependent row on every cache miss. UPSERT preserves
    # meeting IDs across re-scrapes; downstream rows stay intact.
    #
    # Dedup pass before creating the index — older versions of the table may
    # contain duplicate (city, state, date, title) rows from prior buggy
    # scrapes. We keep the lowest id (the original) and CASCADE-drop the
    # rest. Idempotent: no-op once the table is clean.
    cursor.execute("""
        SELECT city_name, state, meeting_date, meeting_title, MIN(id) AS keep_id, COUNT(*) AS n
        FROM meetings
        GROUP BY city_name, state, meeting_date, meeting_title
        HAVING COUNT(*) > 1
    """)
    dupe_groups = cursor.fetchall()
    for g in dupe_groups:
        cursor.execute(
            """
            SELECT id, public_id
            FROM meetings
            WHERE city_name = ? AND state = ?
              AND meeting_date = ? AND meeting_title = ?
              AND id != ?
            ORDER BY id
            """,
            (g['city_name'], g['state'], g['meeting_date'], g['meeting_title'], g['keep_id'])
        )
        duplicate_rows = cursor.fetchall()
        for duplicate in duplicate_rows:
            # Preserve aliases that already resolved through a row which is
            # itself now losing a later duplicate merge.
            cursor.execute(
                """
                UPDATE meeting_public_id_aliases
                SET canonical_meeting_id = ?
                WHERE canonical_meeting_id = ?
                """,
                (g['keep_id'], duplicate['id']),
            )
            if duplicate['public_id']:
                cursor.execute(
                    """
                    INSERT INTO meeting_public_id_aliases
                        (alias_public_id, canonical_meeting_id)
                    VALUES (?, ?)
                    """,
                    (duplicate['public_id'], g['keep_id']),
                )
        cursor.execute(
            """
            DELETE FROM meetings
            WHERE city_name = ? AND state = ?
              AND meeting_date = ? AND meeting_title = ?
              AND id != ?
            """,
            (g['city_name'], g['state'], g['meeting_date'], g['meeting_title'], g['keep_id'])
        )
    # NOTE: cascade-deletes here only affect rows that were already
    # orphan-duplicates; the canonical row (lowest id) is preserved.

    # D-164/PI-5 data-hygiene: merge scraper-era title-suffix twins. An older
    # parser format wrote 'Title - Mon DD, YYYY' while the current parser
    # writes 'Title', so the natural-key dedupe above can never merge the two
    # rows of the same real-world meeting. The /v1 metadata tier made the
    # class publicly misleading (a published episode's twin rendered as
    # "Episode coming" beside it) — 94 Kingman pairs at 2026-07-13 discovery,
    # including the long-carried 103753/104617 duplicate. Runs after the
    # exact-key dedupe + the public_id backfill (aliases need public_ids).
    _merge_title_suffix_twin_meetings(conn)

    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_meetings_natural_key
        ON meetings(city_name, state, meeting_date, meeting_title)
    """)
    
    # Full-text search index for meetings
    cursor.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS meetings_fts USING fts5(
            meeting_title, city_name, county, meeting_location,
            content='meetings',
            content_rowid='id'
        )
    """)
    
    # Triggers to keep FTS in sync
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS meetings_ai AFTER INSERT ON meetings BEGIN
            INSERT INTO meetings_fts(rowid, meeting_title, city_name, county, meeting_location)
            VALUES (new.id, new.meeting_title, new.city_name, new.county, new.meeting_location);
        END
    """)
    
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS meetings_ad AFTER DELETE ON meetings BEGIN
            INSERT INTO meetings_fts(meetings_fts, rowid, meeting_title, city_name, county, meeting_location)
            VALUES ('delete', old.id, old.meeting_title, old.city_name, old.county, old.meeting_location);
        END
    """)
    
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS meetings_au AFTER UPDATE ON meetings BEGIN
            INSERT INTO meetings_fts(meetings_fts, rowid, meeting_title, city_name, county, meeting_location)
            VALUES ('delete', old.id, old.meeting_title, old.city_name, old.county, old.meeting_location);
            INSERT INTO meetings_fts(rowid, meeting_title, city_name, county, meeting_location)
            VALUES (new.id, new.meeting_title, new.city_name, new.county, new.meeting_location);
        END
    """)
    
    conn.commit()
    conn.close()

    # Z-SPAN extensions: notebook bridge schema
    init_notebook_schema()
    # Account-scoped reject control belongs to the same bootstrap contract:
    # callers that initialize a fresh test/deployment database must not need
    # a second, undocumented schema call before Librarian routes are usable.
    init_librarian_abuse_state_schema()
    init_librarian_policy_schema()

    print(f"Database initialized at {DB_PATH}")


# ─────────────────────────────────────────────────────────────────
# Z-SPAN NotebookLM Bridge Schema
# Adds notebook_id column to meetings and a notebook_outputs table
# for storing NotebookLM Studio outputs (newsletter, audio, video, etc.)
# ─────────────────────────────────────────────────────────────────

def _column_exists(cursor, table: str, column: str) -> bool:
    """Check if a column exists in a table."""
    cursor.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cursor.fetchall())


def _create_users_table(cursor: sqlite3.Cursor, table_name: str = "users") -> None:
    """Create the provider-neutral account row used by every sign-in method."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name):
        raise ValueError("unsafe users table name")
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            google_sub TEXT UNIQUE,
            email TEXT UNIQUE NOT NULL,
            display_name TEXT,
            avatar_url TEXT,
            role TEXT NOT NULL DEFAULT 'light'
                CHECK (role IN ('light','creator','verified-creator')),
            librarian_access TEXT NOT NULL DEFAULT 'none',
            librarian_enforcement_epoch INTEGER NOT NULL DEFAULT 0
                CHECK (librarian_enforcement_epoch >= 0),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)


def _ensure_users_support_local_auth(conn: sqlite3.Connection) -> None:
    """Make ``google_sub`` optional without changing any account identifiers.

    The original account table assumed every user came from Google. Email and
    password authentication is another identity attached to the same user id,
    so a local account must not carry a fabricated Google subject. SQLite
    cannot relax a NOT NULL constraint in place; rebuild the table atomically
    with foreign-key checks disabled only for the duration of the copy.
    """
    cursor = conn.cursor()
    existing = cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'users'"
    ).fetchone()
    if existing is None:
        _create_users_table(cursor)
        return

    # Older account databases may predate these additive fields. Bring them to
    # the current shape before the constraint-only rebuild.
    if not _column_exists(cursor, "users", "role"):
        cursor.execute(
            "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'light'"
        )
    if not _column_exists(cursor, "users", "librarian_access"):
        cursor.execute(
            "ALTER TABLE users ADD COLUMN librarian_access "
            "TEXT NOT NULL DEFAULT 'none'"
        )
    if not _column_exists(cursor, "users", "librarian_enforcement_epoch"):
        cursor.execute(
            "ALTER TABLE users ADD COLUMN librarian_enforcement_epoch "
            "INTEGER NOT NULL DEFAULT 0 "
            "CHECK (librarian_enforcement_epoch >= 0)"
        )

    google_sub_info = next(
        (
            row
            for row in cursor.execute("PRAGMA table_info(users)").fetchall()
            if row[1] == "google_sub"
        ),
        None,
    )
    if google_sub_info is None:
        raise RuntimeError("users table is missing google_sub")
    if int(google_sub_info[3]) == 0:
        return

    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        _create_users_table(cursor, "users_local_auth_migration")
        cursor.execute("""
            INSERT INTO users_local_auth_migration (
                id, google_sub, email, display_name, avatar_url, role,
                librarian_access, librarian_enforcement_epoch,
                created_at, last_seen_at
            )
            SELECT
                id, google_sub, email, display_name, avatar_url, role,
                librarian_access, librarian_enforcement_epoch,
                created_at, last_seen_at
            FROM users
        """)
        cursor.execute("DROP TABLE users")
        cursor.execute("ALTER TABLE users_local_auth_migration RENAME TO users")
        violations = cursor.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                "users local-auth migration would break foreign keys"
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def init_cli_auth_schema(cursor: sqlite3.Cursor) -> None:
    """Create the D-172 CLI auth and generation registry tables."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cli_auth_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code_hash TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            loopback_port INTEGER NOT NULL,
            cli_state TEXT NOT NULL,
            code_challenge TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cli_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT NOT NULL,
            last_used_at TEXT,
            revoked_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_cli_tokens_user ON cli_tokens(user_id)"
    )
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cli_generations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            generation_public_id TEXT UNIQUE NOT NULL,
            ribbon_token TEXT UNIQUE NOT NULL,
            user_id INTEGER,
            meeting_public_id TEXT NOT NULL,
            output_type TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'registered'
                CHECK (status IN ('registered','superseded')),
            superseded_by TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
        )
    """)
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_cli_generations_idem "
        "ON cli_generations(user_id, idempotency_key)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_cli_generations_ribbon "
        "ON cli_generations(ribbon_token)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_cli_generations_meeting "
        "ON cli_generations(meeting_public_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_cli_generations_user "
        "ON cli_generations(user_id)"
    )
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cli_contributions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            submission_public_id TEXT UNIQUE NOT NULL,
            user_id INTEGER,
            meeting_id INTEGER NOT NULL,
            meeting_public_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            transcript_sha256 TEXT NOT NULL,
            transcript_json TEXT NOT NULL,
            source_url TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'received_unverified'
                CHECK (status IN ('received_unverified','accepted','rejected')),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TEXT,
            UNIQUE(user_id, idempotency_key),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
            FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE RESTRICT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cli_contribution_outputs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contribution_id INTEGER NOT NULL,
            output_type TEXT NOT NULL,
            content TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            gate_status TEXT NOT NULL,
            gate_log TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(contribution_id, output_type),
            FOREIGN KEY (contribution_id)
                REFERENCES cli_contributions(id) ON DELETE CASCADE
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_cli_contributions_user "
        "ON cli_contributions(user_id, created_at)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_cli_contributions_meeting "
        "ON cli_contributions(meeting_id, created_at)"
    )


def init_flagship_generation_schema(cursor: sqlite3.Cursor) -> None:
    """Create the append-only, account-bound flagship ribbon registry."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS flagship_generations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ribbon_token TEXT UNIQUE NOT NULL,
            notebook_output_id INTEGER UNIQUE NOT NULL,
            meeting_id INTEGER NOT NULL,
            output_type TEXT NOT NULL,
            user_id INTEGER,
            status TEXT NOT NULL DEFAULT 'registered'
                CHECK (status IN ('registered','superseded')),
            minted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(meeting_id, output_type),
            FOREIGN KEY (notebook_output_id)
                REFERENCES notebook_outputs(id) ON DELETE RESTRICT,
            FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE RESTRICT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_flagship_generations_ribbon "
        "ON flagship_generations(ribbon_token)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_flagship_generations_output "
        "ON flagship_generations(notebook_output_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_flagship_generations_meeting "
        "ON flagship_generations(meeting_id, output_type)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_flagship_generations_user "
        "ON flagship_generations(user_id)"
    )


def _migrate_cities_unique_to_composite(cursor):
    """One-shot, idempotent migration: cities.name UNIQUE → UNIQUE(name, county, state).

    Required for multi-state operation — same name can exist in different states
    (Maricopa AZ vs Maricopa CA). See DECISIONS.md § D-027 and the playbook at
    01_Project_Overview/CHANNEL_DISCOVERY_PLAYBOOK.md.

    Strategy: SQLite can't drop UNIQUE constraints in-place, so we recreate the
    cities table with the new schema, copy rows preserving id (so meetings.city_id
    foreign keys remain valid), drop the old table, and rename. Idempotent —
    no-op on databases that already have the new schema.

    Includes the youtube_channel_url / youtube_channel_id columns (added by
    init_notebook_schema's column-add migration) so the swap doesn't lose them
    if init_notebook_schema has already run on this DB.
    """
    row = cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='cities'"
    ).fetchone()
    if row is None:
        return  # table doesn't exist yet (init_db will create with new schema)
    schema = row[0] or ""

    # Already migrated → no-op
    if "UNIQUE(name, county, state)" in schema or "UNIQUE (name, county, state)" in schema:
        return

    # Schema doesn't have the OLD constraint either (hand-edited?) — bail safely
    if "name TEXT UNIQUE" not in schema:
        return

    conn = cursor.connection
    has_yt_url = _column_exists(cursor, "cities", "youtube_channel_url")
    has_yt_id = _column_exists(cursor, "cities", "youtube_channel_id")

    # Build the new-table column list dynamically so we don't lose YouTube columns
    # if they've already been added by init_notebook_schema on this DB.
    yt_url_col = ", youtube_channel_url TEXT" if has_yt_url else ""
    yt_id_col = ", youtube_channel_id TEXT" if has_yt_id else ""
    yt_url_select = ", youtube_channel_url" if has_yt_url else ""
    yt_id_select = ", youtube_channel_id" if has_yt_id else ""

    conn.execute("PRAGMA foreign_keys=OFF")
    migrated_ok = False
    try:
        cursor.execute("BEGIN")
        try:
            cursor.execute(f"""
                CREATE TABLE cities_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    county TEXT NOT NULL,
                    state TEXT NOT NULL,  -- resolved via resolve_city_state; no Arizona default (s53 NV bug class)
                    calendar_url TEXT,
                    parser_file TEXT,
                    calendar_format TEXT,
                    status TEXT DEFAULT 'active',
                    last_scraped TIMESTAMP,
                    scrape_success INTEGER DEFAULT 0,
                    total_meetings INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP{yt_url_col}{yt_id_col},
                    UNIQUE(name, county, state)
                )
            """)
            cursor.execute(f"""
                INSERT INTO cities_new
                    (id, name, county, state, calendar_url, parser_file, calendar_format,
                     status, last_scraped, scrape_success, total_meetings,
                     created_at, updated_at{yt_url_select}{yt_id_select})
                SELECT id, name, county, state, calendar_url, parser_file, calendar_format,
                       status, last_scraped, scrape_success, total_meetings,
                       created_at, updated_at{yt_url_select}{yt_id_select}
                FROM cities
            """)
            cursor.execute("DROP TABLE cities")
            cursor.execute("ALTER TABLE cities_new RENAME TO cities")
            cursor.execute("COMMIT")
            migrated_ok = True
        except Exception:
            cursor.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")

    if migrated_ok:
        # ASCII-only print to avoid Windows cp1252 encoding errors. Wrapped in
        # its own try so a print failure can never roll back a committed migration.
        try:
            print("[init_db] Migrated cities table: name UNIQUE -> UNIQUE(name, county, state)")
        except Exception:
            pass


def init_notebook_schema():
    """
    Idempotent migration that adds Z-SPAN bridge schema:
      - cities.youtube_channel_url, cities.youtube_channel_id  (per-city YouTube channel for council uploads)
      - meetings.notebook_id   (the NotebookLM notebook bound to this meeting)
      - notebook_outputs table — caches the Studio outputs per (meeting, output_type)
      - work_orders table      — defrag-style processing queue (rate-limit friendly)
    """
    conn = get_connection()
    cursor = conn.cursor()

    # The canonical account row is shared by Google and email/password.
    # Establish that shape before any account-owned tables are created.
    _ensure_users_support_local_auth(conn)

    # Per-city YouTube channel tracking
    if not _column_exists(cursor, "cities", "youtube_channel_url"):
        cursor.execute("ALTER TABLE cities ADD COLUMN youtube_channel_url TEXT")
    if not _column_exists(cursor, "cities", "youtube_channel_id"):
        cursor.execute("ALTER TABLE cities ADD COLUMN youtube_channel_id TEXT")

    # Add notebook_id column to meetings if missing
    if not _column_exists(cursor, "meetings", "notebook_id"):
        cursor.execute("ALTER TABLE meetings ADD COLUMN notebook_id TEXT")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_meetings_notebook ON meetings(notebook_id)")

    # T-004: video_url match metadata. Set by match_videos.py when it auto-
    # populates meetings.video_url; informs the operator-terminal UI which
    # rows came from auto-matching (and at what confidence) vs manual paste.
    if not _column_exists(cursor, "meetings", "video_url_match_confidence"):
        cursor.execute("ALTER TABLE meetings ADD COLUMN video_url_match_confidence TEXT")
    if not _column_exists(cursor, "meetings", "video_url_match_method"):
        cursor.execute("ALTER TABLE meetings ADD COLUMN video_url_match_method TEXT")

    # (T-004 work_orders.video_url_match_* ALTERs moved below — they must run
    # AFTER CREATE TABLE work_orders, otherwise a fresh DB inits fail with
    # "no such table: work_orders" before work_orders is created.)

    # Broadcast outputs — one row per (meeting_id, output_type), written by
    # the V1-RAG-3 pipeline (Qdrant retrieval + claude -p Sonnet synthesis).
    # The table NAME `notebook_outputs` is a historical artifact kept
    # deliberately: DEEP_CLEAN Phase 1a (2026-07-04 session-32 downgrade)
    # found ~30 internal references at zero operator-facing benefit for the
    # rename — the API layer already returns this data as `outputs`.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notebook_outputs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL,
            notebook_id TEXT NOT NULL,
            output_type TEXT NOT NULL,
            content TEXT,
            content_url TEXT,
            prompt_filename TEXT,
            prompt_version TEXT,
            generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            error TEXT,
            voided_at TIMESTAMP,
            voided_by TEXT,
            UNIQUE(meeting_id, output_type),
            FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
        )
    """)
    if not _column_exists(cursor, "notebook_outputs", "voided_at"):
        cursor.execute(
            "ALTER TABLE notebook_outputs ADD COLUMN voided_at TIMESTAMP"
        )
    if not _column_exists(cursor, "notebook_outputs", "voided_by"):
        cursor.execute("ALTER TABLE notebook_outputs ADD COLUMN voided_by TEXT")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notebook_outputs_meeting ON notebook_outputs(meeting_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notebook_outputs_type ON notebook_outputs(output_type)")

    # Work orders — the defrag-style processing queue.
    # One row per meeting we want to process. The worker daemon polls this
    # table and processes pending items one at a time, respecting rate limits.
    #
    # State machine:
    #   pending                          → ready to process
    #   processing                       → worker is currently handling it
    #   awaiting_video                   → no video URL yet (skip, scanner will retry)
    #   awaiting_notebook                → no notebook_id yet (manual step or auto-create failed)
    #   completed                        → all outputs generated successfully
    #   failed                           → all retries exhausted
    #   skipped_too_old                  → meeting older than MEETING_AGE_LIMIT_DAYS (won't process)
    #   no_video_source                  → S-037 V0: no usable video source (terminal, won't retry)
    #   failed_truth_packet              → S-009 ch3 (2026-06-19): truth-packet gate returned 'halt'
    #                                      (decisively wrong source — operator re-pastes URL or abandons)
    #   awaiting_truth_packet_review     → S-009 ch3 (2026-06-19): truth-packet gate returned 'ambiguous'
    #                                      (operator inspects observations, decides to proceed/re-paste/abandon)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS work_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL UNIQUE,
            state TEXT NOT NULL DEFAULT 'pending',
            priority INTEGER NOT NULL DEFAULT 0,
            youtube_video_url TEXT,
            notebook_id TEXT,
            requested_outputs TEXT DEFAULT 'episode_tagline,synopsis,newsletter,key_decisions,community_calls_to_action,whats_next,council_sentiment,suggested_questions,transcript_words,tracked_claims,quote_extraction',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            next_attempt_at TIMESTAMP,
            retry_count INTEGER NOT NULL DEFAULT 0,
            max_retries INTEGER NOT NULL DEFAULT 3,
            last_error TEXT,
            diarization_status TEXT,
            diarization_detail TEXT,
            diarization_updated_at TIMESTAMP,
            FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_work_orders_state ON work_orders(state)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_work_orders_priority ON work_orders(priority DESC, created_at ASC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_work_orders_next_attempt ON work_orders(next_attempt_at)")

    # T-004: mirror video_url match metadata onto work_orders so the operator
    # terminal can show a confidence pill on each WO row without joining the
    # meetings table. `--apply` (and the eventual operator-confirm flow) writes
    # both rows. Must run AFTER the CREATE TABLE work_orders above — moved here
    # from earlier in the function to fix init-order on fresh databases (the
    # ALTER would fail with "no such table: work_orders" before the CREATE).
    if not _column_exists(cursor, "work_orders", "video_url_match_confidence"):
        cursor.execute("ALTER TABLE work_orders ADD COLUMN video_url_match_confidence TEXT")
    if not _column_exists(cursor, "work_orders", "video_url_match_method"):
        cursor.execute("ALTER TABLE work_orders ADD COLUMN video_url_match_method TEXT")

    # Diarization is an optional post-processing concern, independent of the
    # work order's publication-output state.  Keep its status separate so a
    # deliberately deferred run is never represented as a WO failure (and so
    # deferred meetings can be selected for a later backfill).
    if not _column_exists(cursor, "work_orders", "diarization_status"):
        cursor.execute("ALTER TABLE work_orders ADD COLUMN diarization_status TEXT")
    if not _column_exists(cursor, "work_orders", "diarization_detail"):
        cursor.execute("ALTER TABLE work_orders ADD COLUMN diarization_detail TEXT")
    if not _column_exists(cursor, "work_orders", "diarization_updated_at"):
        cursor.execute("ALTER TABLE work_orders ADD COLUMN diarization_updated_at TIMESTAMP")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_work_orders_diarization_status "
        "ON work_orders(diarization_status)"
    )

    # ─── S-015: the Guide — live civic-feed detection cache ──────────
    # One row per detected live broadcast. guide_detector.py polls (calendar-
    # gated) the YouTube Data API for each registered city channel; the Guide
    # view reads is_live=1 rows. Soft state: a stream that ends is marked
    # is_live=0 (not deleted), so a brief blip doesn't lose the record and the
    # row can flip back live on the next pass. Pure mirror of public streams —
    # touches no NotebookLM/generation/publish machinery.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS live_streams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city_name TEXT NOT NULL,
            state TEXT,
            county TEXT,
            channel_id TEXT,
            video_id TEXT NOT NULL,
            video_url TEXT NOT NULL,
            title TEXT,
            started_at TEXT,
            detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_live INTEGER NOT NULL DEFAULT 1,
            meeting_id INTEGER,
            UNIQUE(city_name, state, video_id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_live_streams_is_live ON live_streams(is_live)")

    # ─── Phase H: Guide data engine — meeting-pattern health log ──────
    # One row per (city, pattern_id) per weekly refresh. The refresh job
    # (`scripts/refresh_city_calendars.py`, H-3) scrapes each city's calendar,
    # projects the same window from the city's curated `meeting_patterns[]`
    # (in `city_intelligence/<city>.json`), and writes a reconciliation row
    # here. Drift detection (H-4) reads recent rows to flag patterns whose
    # projection has missed N consecutive scrapes.
    #
    # `expected_next` + `actually_scraped` are JSON arrays of dates so the
    # operator's calendar-health surface (H-7) can render diffs without
    # re-running projection. `match_status` is the bucketed verdict:
    #   match     — projection and scrape agree on the window
    #   drift     — material disagreement (one or more expected dates missing)
    #   partial   — some matches, some misses (e.g. extra scraped instance)
    #   no_data   — scrape failed or returned nothing; can't reconcile
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pattern_health (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city_name TEXT NOT NULL,
            state TEXT,
            pattern_id TEXT NOT NULL,
            refreshed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            window_start TEXT NOT NULL,
            window_end TEXT NOT NULL,
            expected_next TEXT,
            actually_scraped TEXT,
            match_status TEXT NOT NULL,
            drift_notes TEXT
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_pattern_health_pattern "
        "ON pattern_health(city_name, state, pattern_id, refreshed_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_pattern_health_refreshed "
        "ON pattern_health(refreshed_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_pattern_health_status "
        "ON pattern_health(match_status, refreshed_at DESC)"
    )

    # NOTE: `protected_notebook_ids` + `notebook_deletion_log` tables +
    # their indexes were removed per D-143 (NotebookLM subsystem removal
    # 2026-07-01). The GC tooling (notebook_gc.py + /api/notebooks/*
    # routes) went with them — see S-109.

    # ─── D-051 flagship sync log ─────────────────────────────────────
    # One row per push-to-flagship attempt. Tracks status (success / failed
    # / in_progress), bytes transferred, the cloud response (JSON), and
    # which operator triggered the push. The latest row per meeting_id is
    # what the operator-terminal [PUSH] button consults for its status
    # indicator.
    #
    # Local-only table — the cloud Railway instance has its own (empty)
    # version that doesn't get written. Only the sender side records
    # attempts.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS flagship_sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL,
            attempted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            pushed_by TEXT,
            status TEXT NOT NULL,
            error TEXT,
            payload_bytes INTEGER,
            media_bytes INTEGER,
            flagship_response TEXT,
            FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_flagship_sync_meeting ON flagship_sync_log(meeting_id, attempted_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_flagship_sync_status ON flagship_sync_log(status, attempted_at DESC)")

    # ─── Review-gate approval columns + per-quote verification audit (D-032) ───
    # work_orders.approved_at + approved_by track when (and by whom) a broadcast
    # passed both gates of the operator-terminal review. NULL = not yet approved
    # = the [REVIEW] button is visible. After approval, the broadcast is eligible
    # to render on public surfaces (or in our case, becomes part of the published
    # set; the actual gating of the BroadcastPage on this flag is a follow-up).
    if not _column_exists(cursor, "work_orders", "approved_at"):
        cursor.execute("ALTER TABLE work_orders ADD COLUMN approved_at TIMESTAMP")
    if not _column_exists(cursor, "work_orders", "approved_by"):
        cursor.execute("ALTER TABLE work_orders ADD COLUMN approved_by TEXT")

    # ─── Phase 3 — Publish state on meetings ───
    # is_published is the public-visibility switch separable from D-032
    # quality approval. The D-032 approval (work_orders.approved_at) means
    # "operator vouches for the outputs"; is_published means "this broadcast
    # is visible on the public channel browser." Decoupled so an approved
    # broadcast can be temporarily unpublished without un-approving the
    # operator's quality vouch. For V1 the [APPROVE FOR PUBLICATION] flow
    # sets both in one click; future operator UX can split them.
    if not _column_exists(cursor, "meetings", "is_published"):
        cursor.execute("ALTER TABLE meetings ADD COLUMN is_published INTEGER NOT NULL DEFAULT 0")
    if not _column_exists(cursor, "meetings", "published_at"):
        cursor.execute("ALTER TABLE meetings ADD COLUMN published_at TIMESTAMP")
    if not _column_exists(cursor, "meetings", "published_by"):
        cursor.execute("ALTER TABLE meetings ADD COLUMN published_by TEXT")
    if not _column_exists(cursor, "meetings", "publish_notes"):
        cursor.execute("ALTER TABLE meetings ADD COLUMN publish_notes TEXT")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_meetings_is_published "
        "ON meetings(is_published, meeting_date DESC)"
    )

    # Per-quote verification audit. Each row = one verbatim quote that the
    # human reviewer ticked through during a Gate-2 spot-check (D-028). Stored
    # in its own table for audit-trail integrity and so future T-001 (Gemini
    # auditor) can query "which quotes did the human personally verify vs.
    # which slipped through?"
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS quote_verifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            work_order_id INTEGER NOT NULL,
            meeting_id INTEGER NOT NULL,
            quote_id TEXT NOT NULL,
            verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            verified_by TEXT,
            UNIQUE(work_order_id, quote_id),
            FOREIGN KEY (work_order_id) REFERENCES work_orders(id) ON DELETE CASCADE,
            FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_quote_verifications_wo ON quote_verifications(work_order_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_quote_verifications_meeting ON quote_verifications(meeting_id)")

    # ─── Cast page V1 (T-007 / 2026-05-11) ───
    # Augments the legacy `council_members` table with the canonical fields
    # used by city_intelligence/*.json (`seat_id`, `role`, `source_url`,
    # `term_started`, `term_ends`). The legacy `title` and `ward` columns are
    # preserved for backward compatibility but new code reads/writes the
    # explicit columns. A partial unique index on (city_name, seat_id)
    # enables idempotent ON CONFLICT upserts from the JSON seeder.
    if not _column_exists(cursor, "council_members", "seat_id"):
        cursor.execute("ALTER TABLE council_members ADD COLUMN seat_id TEXT")
    if not _column_exists(cursor, "council_members", "role"):
        cursor.execute("ALTER TABLE council_members ADD COLUMN role TEXT")
    if not _column_exists(cursor, "council_members", "source_url"):
        cursor.execute("ALTER TABLE council_members ADD COLUMN source_url TEXT")
    if not _column_exists(cursor, "council_members", "term_started"):
        cursor.execute("ALTER TABLE council_members ADD COLUMN term_started TEXT")
    if not _column_exists(cursor, "council_members", "term_ends"):
        cursor.execute("ALTER TABLE council_members ADD COLUMN term_ends TEXT")

    # SQLite's ON CONFLICT(...) upsert targets do NOT match partial unique
    # indexes (those with a WHERE clause) — they must match a non-partial
    # unique constraint or unique index. We use a full unique index here;
    # SQLite treats NULLs as distinct in unique indexes by default, so any
    # legacy rows with NULL seat_id won't conflict, and the seeder never
    # inserts NULL seat_id anyway (rows lacking seat_id are skipped). Drop
    # the earlier partial-index name in case it exists from a prior schema
    # migration before re-creating as a non-partial unique index.
    cursor.execute("DROP INDEX IF EXISTS idx_council_member_seat")
    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_council_member_seat
        ON council_members(city_name, seat_id)
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_council_member_city ON council_members(city_name)")

    # member_attendance — one row per (member, meeting). Populated by the
    # NotebookLM roll-call extraction prompt (V2 work, not yet wired).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS member_attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL,
            meeting_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            notes TEXT,
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(member_id, meeting_id),
            FOREIGN KEY (member_id) REFERENCES council_members(id) ON DELETE CASCADE,
            FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_attendance_member ON member_attendance(member_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_attendance_meeting ON member_attendance(meeting_id)")

    # member_quotes — one row per verified quote attributed to a member.
    # topic_tags is a JSON array of strings drawn from a controlled
    # vocabulary (see FUTURE_THOUGHTS.md § T-008). proof_clip_url is the
    # eventual Z-SPAN Proofs YouTube link (T-009); NULL in V1.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS member_quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL,
            meeting_id INTEGER NOT NULL,
            quote_text TEXT NOT NULL,
            topic_tags TEXT,
            minutes_page_ref TEXT,
            video_timestamp_seconds INTEGER,
            proof_clip_url TEXT,
            verified_status TEXT DEFAULT 'pending',
            extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (member_id) REFERENCES council_members(id) ON DELETE CASCADE,
            FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_quotes_member ON member_quotes(member_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_quotes_meeting ON member_quotes(meeting_id)")

    # T-009 Phase 0b — `word_timings` is a JSON array of
    # `{word, start_ms, end_ms}` rows produced by `parsers/quote_align.py`
    # aligning the quote text against the meeting's Whisper transcript
    # (notebook_outputs row of type `transcript_words`). Powers the
    # synced-transcript karaoke UI on the Cast page (Phase 0c). NULL on
    # rows where alignment hasn't run yet OR where the alignment failed
    # (no matching blocks of size >=2 found between the quote and the
    # transcript — likely paraphrased or misattributed quote).
    if not _column_exists(cursor, "member_quotes", "word_timings"):
        cursor.execute("ALTER TABLE member_quotes ADD COLUMN word_timings TEXT")

    # T-013 V3 — Gemini Pro review round-trip ingestion.
    # The ingest script (`zspan_pipeline/scripts/ingest_review_response.py`)
    # writes verdicts + mechanical corrections into these columns. Per D-043,
    # the original NotebookLM extraction is preserved in `quote_text_original`
    # so a future audit / verbosity-slider (T-015) can show what changed.
    # `gemini_correction_notes` is a JSON blob: source RESPONSE.md path,
    # response_received timestamp, raw Gemini verdict, applied substitutions,
    # unapplied differences, decision, ingestion timestamp. Everything an
    # auditor needs to roll back or explain a change.
    if not _column_exists(cursor, "member_quotes", "quote_text_original"):
        cursor.execute("ALTER TABLE member_quotes ADD COLUMN quote_text_original TEXT")
    if not _column_exists(cursor, "member_quotes", "gemini_correction_notes"):
        cursor.execute("ALTER TABLE member_quotes ADD COLUMN gemini_correction_notes TEXT")
    if not _column_exists(cursor, "member_quotes", "verified_by"):
        cursor.execute("ALTER TABLE member_quotes ADD COLUMN verified_by TEXT")
    if not _column_exists(cursor, "member_quotes", "verified_at"):
        cursor.execute("ALTER TABLE member_quotes ADD COLUMN verified_at TIMESTAMP")

    # T-012 — Tracked Claims Ledger. Sibling to `member_quotes`. Holds
    # forward-looking statements made by officials (assurances, commitments,
    # predictions, promises) that someone could later check for fulfillment
    # or contradiction. Each row carries its own word_timings JSON so the
    # marker-styled karaoke renders without going through member_quotes.
    # Status is a small enum operator-flipped via review surface (NOT
    # auto-verified — see T-012 § "V1 outcome tracking — manual").
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tracked_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL,
            meeting_id INTEGER NOT NULL,
            claim_type TEXT,
            claim_text TEXT NOT NULL,
            expected_outcome TEXT,
            time_horizon_months INTEGER,
            topic_tags TEXT,
            confidence TEXT,
            context TEXT,
            word_timings TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            status_updated_at TIMESTAMP,
            status_updated_by TEXT,
            status_evidence TEXT,
            extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (member_id) REFERENCES council_members(id) ON DELETE CASCADE,
            FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_tracked_claims_member "
        "ON tracked_claims(member_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_tracked_claims_meeting "
        "ON tracked_claims(meeting_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_tracked_claims_status "
        "ON tracked_claims(status)"
    )
    # Composite index for the "aged past horizon, still active" query
    # on the City Ledger — the actionable-feed default sort.
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_tracked_claims_aging "
        "ON tracked_claims(status, extracted_at, time_horizon_months)"
    )

    # ─────────────────────────────────────────────────────────────────
    # Conversational Compiler IR (S-023 / CONVERSATIONAL_COMPILER_SPEC §
    # IR schema V0) — Chunk B-0 (2026-06-05). Two tables that hold the
    # typed-IR rendering of a meeting transcript: one row per parsed
    # utterance/event, one row per logical connection between them.
    #
    # Populated by Track B's parser pipeline. Per Decision #8a, the
    # parser is NotebookLM via sibling prompts to `prompts/tracked_claims.md`
    # (motions.md, votes.md, agenda_transitions.md, etc.) — NOT a
    # third-party LLM. `parser_model` carries an identifier like
    # 'notebooklm:motions.md@v1' so we can re-extract / reconcile when
    # prompts change.
    #
    # `Commit_P` nodes ALSO project into `tracked_claims` (see the new
    # nullable `tracked_claims.source_node_id` FK added below). The
    # existing UI keeps reading `tracked_claims` unchanged; the compiler
    # surfaces the broader graph by reading `transcript_nodes` directly.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transcript_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,                  -- monotonic within meeting
            audio_offset_seconds REAL,
            audio_duration_seconds REAL,
            speaker_id INTEGER,                        -- FK council_members; NULL for public-comment / unknown
            speaker_name TEXT,                         -- denormalized for unknown / external speakers
            transcript_span_text TEXT NOT NULL,
            word_timings TEXT,                         -- JSON, same shape as quotes.word_timings
            node_type TEXT NOT NULL,                   -- 'Utterance' | 'Motion' | 'Second' | 'Vote' | 'Commit_P' | 'AgendaTransition' | 'Contradiction'
            typed_fields TEXT,                         -- JSON, node-type-specific (per SPEC § Node types)
            parser_model TEXT NOT NULL,                -- e.g. 'notebooklm:motions.md@v1' (Decision #8a)
            parser_confidence REAL,
            parser_ran_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            parent_node_id INTEGER,                    -- self-FK for layered abstraction (Decision #2)
            FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE,
            FOREIGN KEY (speaker_id) REFERENCES council_members(id) ON DELETE SET NULL,
            FOREIGN KEY (parent_node_id) REFERENCES transcript_nodes(id) ON DELETE CASCADE
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_transcript_nodes_meeting "
        "ON transcript_nodes(meeting_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_transcript_nodes_meeting_ordinal "
        "ON transcript_nodes(meeting_id, ordinal)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_transcript_nodes_type "
        "ON transcript_nodes(meeting_id, node_type)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_transcript_nodes_speaker "
        "ON transcript_nodes(speaker_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_transcript_nodes_parent "
        "ON transcript_nodes(parent_node_id)"
    )

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transcript_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_node_id INTEGER NOT NULL,
            target_node_id INTEGER NOT NULL,
            edge_type TEXT NOT NULL,                   -- 'references' | 'entails' | 'contradicts' | 'satisfies' | 'responds_to'
            parser_confidence REAL,
            parser_ran_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (source_node_id) REFERENCES transcript_nodes(id) ON DELETE CASCADE,
            FOREIGN KEY (target_node_id) REFERENCES transcript_nodes(id) ON DELETE CASCADE
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_transcript_edges_source "
        "ON transcript_edges(source_node_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_transcript_edges_target "
        "ON transcript_edges(target_node_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_transcript_edges_type "
        "ON transcript_edges(edge_type)"
    )

    # Backfill the nullable FK on tracked_claims pointing back to the
    # originating transcript_nodes row. NULL for hand-seeded / legacy
    # rows (the 3 m101091 sandbox claims fall here); populated when the
    # compiler projects a parsed Commit_P node into tracked_claims (per
    # SPEC § Relationship to existing tracked_claims projection rule).
    if not _column_exists(cursor, "tracked_claims", "source_node_id"):
        cursor.execute(
            "ALTER TABLE tracked_claims ADD COLUMN source_node_id INTEGER "
            "REFERENCES transcript_nodes(id) ON DELETE SET NULL"
        )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_tracked_claims_source_node "
        "ON tracked_claims(source_node_id)"
    )

    # ─────────────────────────────────────────────────────────────────
    # `quotes` — Unified quotes table (Quotes Unification Refactor, 2026-05-26)
    #
    # Supersedes the two siloed streams that existed before:
    #   • `council_quotes` — JSON blob in `notebook_outputs.content` (no
    #     verification infrastructure at all)
    #   • `member_quotes` — structured rows (council-member-only; the only
    #     stream T-013 V2/V3 verified)
    #
    # The new table holds ALL official-capacity quotes (council members +
    # staff + outside experts) with polymorphic speaker representation:
    # `member_id` is nullable; staff/external speakers use `speaker_name` +
    # `speaker_role` directly. `speaker_class` discriminates the three.
    # `is_broadcast_hero` flags the 5-8 curated quotes the unified extraction
    # prompt picked for the BroadcastPage hero section.
    #
    # The V3-verdict-wipe bug from `save_member_quotes_batch` is structurally
    # prevented here: `UNIQUE(meeting_id, content_hash)` lets the save helper
    # use INSERT...ON CONFLICT...DO UPDATE to preserve verification state
    # (verified_status, verified_by, verified_at, gemini_correction_notes,
    # quote_text_original, proof_clip_url, proof_clip_sha256) across
    # re-extractions. No DELETE step → no wipe.
    #
    # `content_hash` is SHA256-hex of `lower(speaker_name) || '|' || lower(quote_text)`,
    # maintained by `_compute_content_hash()` at all write sites. When T-013 V3
    # corrects quote_text, the hash is recomputed so subsequent re-extractions
    # (which now produce the corrected form thanks to T-017 prompt priming)
    # match the row and UPDATE rather than orphaning it.
    #
    # During the refactor's transition (Chunks 1-5), this table coexists with
    # `member_quotes` + the `council_quotes` JSON blob. Cut-over happens in
    # Chunks 6-7 (BroadcastPage + Cast page rewrites). Old structures retire
    # in Chunk 9.
    #
    # See `01_Project_Overview/REFACTOR_QUOTES_UNIFICATION.md` for the full
    # architectural rationale + chunk plan.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL,

            -- Polymorphic speaker (council_member / staff / external)
            member_id INTEGER,
            speaker_name TEXT NOT NULL,
            speaker_role TEXT,
            speaker_class TEXT NOT NULL DEFAULT 'council_member',

            -- Quote content
            quote_text TEXT NOT NULL,
            quote_text_original TEXT,
            topic_tags TEXT,
            minutes_page_ref TEXT,
            context TEXT,

            -- Surface flag
            is_broadcast_hero INTEGER NOT NULL DEFAULT 0,

            -- Audio anchoring (T-009 Phase 0b alignment)
            video_timestamp_seconds INTEGER,
            word_timings TEXT,

            -- Verification chain (T-013 V2/V3; D-043)
            verified_status TEXT NOT NULL DEFAULT 'pending',
            verified_by TEXT,
            verified_at TIMESTAMP,
            gemini_correction_notes TEXT,

            -- Permanence (T-009 Phase 2 Proofs)
            proof_clip_url TEXT,
            proof_clip_sha256 TEXT,

            -- Stable identity for re-extraction preservation
            content_hash TEXT NOT NULL,

            -- Provenance
            extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            UNIQUE(meeting_id, content_hash),
            FOREIGN KEY (member_id) REFERENCES council_members(id) ON DELETE SET NULL,
            FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_quotes_meeting_member "
        "ON quotes(meeting_id, member_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_quotes_meeting_hero "
        "ON quotes(meeting_id, is_broadcast_hero)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_quotes_verified "
        "ON quotes(verified_status)"
    )

    # D-054 review-surface helpers (added 2026-05-26 for DisputedQuotesPage).
    # `quote_text_display` caches the readability-polished form (caps +
    # light punctuation) lazy-computed by `parsers/quote_cleaner.py
    # § polish_for_display`. The polished form pre-fills the disputed-quote
    # textarea so the operator scans a readable sentence rather than the
    # verbatim transcript; on Verify it propagates to `quote_text` and the
    # verbatim is preserved in `quote_text_original` (already on table).
    # `verdict_emphasis_tokens` caches the JSON-array of short substrings
    # to red-highlight in the verdict heads-up note, computed by
    # `parsers/verdict_emphasis.py § extract_verdict_emphasis`. Both are
    # nullable — endpoints fall back gracefully if OPENAI_API_KEY isn't
    # configured.
    if not _column_exists(cursor, "quotes", "quote_text_display"):
        cursor.execute("ALTER TABLE quotes ADD COLUMN quote_text_display TEXT")
    if not _column_exists(cursor, "quotes", "verdict_emphasis_tokens"):
        cursor.execute("ALTER TABLE quotes ADD COLUMN verdict_emphasis_tokens TEXT")

    # D-057 extension — agent counter-proposals on disputed quotes.
    # Mirrors the city_vocabulary_corrections columns. Populated by the
    # Disputed Quotes Reviewer (and any future Opus judgment agent on
    # `quotes`) when the cleaner+verifier output is wrong but the agent
    # has a defensible better alternative — e.g. quote 46 ("the more the
    # broader we get…") should preserve Walsh's "but I would just caution"
    # framing. The DisputedQuotesPage textarea pre-fills with the agent's
    # value when present; the Slack `:sparkles:` fast-path applies it
    # directly via `resolve_disputed_quote(action='verify',
    # quote_text=agent_proposed_quote_text)`.
    if not _column_exists(cursor, "quotes", "agent_proposed_quote_text"):
        cursor.execute("ALTER TABLE quotes ADD COLUMN agent_proposed_quote_text TEXT")
    if not _column_exists(cursor, "quotes", "agent_reasoning"):
        cursor.execute("ALTER TABLE quotes ADD COLUMN agent_reasoning TEXT")
    if not _column_exists(cursor, "quotes", "agent_proposed_by"):
        cursor.execute("ALTER TABLE quotes ADD COLUMN agent_proposed_by TEXT")
    if not _column_exists(cursor, "quotes", "agent_proposed_at"):
        cursor.execute("ALTER TABLE quotes ADD COLUMN agent_proposed_at TIMESTAMP")

    # S-005 cleanup — enforce the invariant that a rejected quote is never a
    # broadcast hero. `resolve_disputed_quote(action='reject')` now clears the
    # flag going forward; this idempotent pass scrubs any pre-existing
    # rejected-but-hero rows so they can't silently block the publish gate.
    cursor.execute(
        "UPDATE quotes SET is_broadcast_hero = 0 "
        "WHERE verified_status = 'rejected' AND is_broadcast_hero = 1"
    )

    # T-017 Layer 2 — per-city vocabulary corrections dictionary.
    # Populated by `ingest_review_response.py` whenever Gemini surfaces a
    # `"X" should be "Y"` substitution. Applied (a) post-extraction to
    # text Studio outputs (newsletter, synopsis, etc.) via the cleaner
    # pass + (b) as prompt-level correction directives prepended to ALL
    # prompts so even media regenerations (audio_overview, video,
    # infographic) honor the corrections. Self-improving across meetings;
    # operator can flip `auto_apply` off per-entry without losing the
    # historical record.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS city_vocabulary_corrections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city_name TEXT NOT NULL,
            wrong TEXT NOT NULL,
            right TEXT NOT NULL,
            applied_count INTEGER NOT NULL DEFAULT 0,
            auto_apply INTEGER NOT NULL DEFAULT 1,
            first_observed_response_file TEXT,
            last_applied_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(city_name, wrong)
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_city_vocab_corr_city "
        "ON city_vocabulary_corrections(city_name)"
    )

    # T-018 — promotion tracking. When a correction crosses the threshold
    # (or operator manually promotes a one-off via the Vocabulary Inbox),
    # we stamp `promoted_at` + `promoted_by` so the Inbox can filter it
    # out of the pending queue. NULL = not yet promoted. Soft-revert
    # toggles `auto_apply` to 0 AND clears these fields so the Inbox
    # surfaces the correction again as actionable.
    if not _column_exists(cursor, "city_vocabulary_corrections", "promoted_at"):
        cursor.execute(
            "ALTER TABLE city_vocabulary_corrections ADD COLUMN promoted_at TIMESTAMP"
        )
    if not _column_exists(cursor, "city_vocabulary_corrections", "promoted_by"):
        cursor.execute(
            "ALTER TABLE city_vocabulary_corrections ADD COLUMN promoted_by TEXT"
        )

    # D-057 — agent counter-proposals. When an agent (Vocab Curator etc.)
    # determines that the verifier-proposed `right` value is wrong but has
    # a better alternative (e.g. "Councilmember Stehly" instead of
    # "Counselor Stehly" — title-correct), the agent records the
    # counter-proposal here. The Vocabulary Inbox UI surfaces both
    # values; operator can promote with the agent's value, the
    # verifier's value, or a hand-edited override. Slack fast-path (✨
    # reaction) applies the agent's counter-proposal directly without
    # operator UI visit.
    if not _column_exists(cursor, "city_vocabulary_corrections", "agent_proposed_right"):
        cursor.execute(
            "ALTER TABLE city_vocabulary_corrections ADD COLUMN agent_proposed_right TEXT"
        )
    if not _column_exists(cursor, "city_vocabulary_corrections", "agent_reasoning"):
        cursor.execute(
            "ALTER TABLE city_vocabulary_corrections ADD COLUMN agent_reasoning TEXT"
        )
    if not _column_exists(cursor, "city_vocabulary_corrections", "agent_proposed_by"):
        cursor.execute(
            "ALTER TABLE city_vocabulary_corrections ADD COLUMN agent_proposed_by TEXT"
        )
    if not _column_exists(cursor, "city_vocabulary_corrections", "agent_proposed_at"):
        cursor.execute(
            "ALTER TABLE city_vocabulary_corrections ADD COLUMN agent_proposed_at TIMESTAMP"
        )

    # V1-Consensus-1 C3 — WHISPER_PHONETIC_VARIANT signal flag.
    # Set when a correction was detected as a phonetic variant of an
    # existing canonical `right` form for the same city (e.g., adding
    # Dikins→Dykens when Dykins→Dykens already exists). Triggers stricter
    # Prong 2 specificity check + surfaces in operator review queue as
    # "PHONETIC VARIANT — additional rows surfaced for the same canonical
    # source." Detection runs at correction-insert time via
    # `detect_phonetic_variant_for_right`; the column persists the signal
    # so the consensus pipeline + review queue read it uniformly.
    if not _column_exists(
        cursor, "city_vocabulary_corrections", "is_phonetic_variant"
    ):
        cursor.execute(
            "ALTER TABLE city_vocabulary_corrections ADD COLUMN "
            "is_phonetic_variant INTEGER NOT NULL DEFAULT 0"
        )

    # V1-Consensus-1 C4 — pending consensus review queue.
    # Polish-rejection events from quote_cleaner.polish_for_display land
    # here as pending candidates. The vocabulary-curator agent's heartbeat
    # (per S-061 async-batch shape) reads pending rows, runs Codex query
    # + Prong 1 + Prong 2 + its own judgment, and resolves each row to
    # one of {consensus_match_promoted, consensus_disagreement_review,
    # prong_fail_review, operator_reviewed}. The schema is OPEN to other
    # source types in addition to polish_rejection (e.g. post-extraction
    # audit candidates) — `source_type` column distinguishes them.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS correction_pending_review (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city_name TEXT NOT NULL,
            meeting_id INTEGER,
            quote_id INTEGER,
            source_type TEXT NOT NULL DEFAULT 'polish_rejection',
            original_text TEXT NOT NULL,
            polished_proposal TEXT NOT NULL,
            wrong_token TEXT,
            right_token TEXT,
            is_phonetic_variant INTEGER NOT NULL DEFAULT 0,
            sibling_wrongs_json TEXT,
            codex_proposed_right TEXT,
            codex_confidence TEXT,
            codex_reasoning TEXT,
            curator_proposed_right TEXT,
            curator_reasoning TEXT,
            prong_1_passed INTEGER,
            prong_1_reasoning TEXT,
            prong_1_evidence_json TEXT,
            prong_2_passed INTEGER,
            prong_2_reasoning TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            resolution_action TEXT,
            resolved_at TIMESTAMP,
            resolved_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_review_status "
        "ON correction_pending_review(status, city_name)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_review_meeting "
        "ON correction_pending_review(meeting_id)"
    )

    # Phase 2 D2 (2026-06-24) — speaker-cluster to canonical-roster mapping.
    # pyannote produces anonymous SPEAKER_00 / SPEAKER_01 labels per meeting;
    # cluster_roster_mapper (D6) infers the canonical council_members name
    # for each cluster via Sonnet over the meeting's opening minutes + a
    # two-prong safety gate (anchor evidence + last-name specificity, mirrors
    # the V1-Consensus-1 pattern at correction_pending_review). Rows lifecycle:
    #   'pending_review'        — Sonnet proposed; one or both prongs failed
    #   'auto_promoted'         — both prongs passed; confirmed_canonical
    #                             auto-set to proposed_canonical at insert
    #   'operator_confirmed'    — operator agreed with proposed via D-Build-B UI
    #   'operator_overridden'   — operator picked a different canonical
    #   'left_anonymous'        — operator decided no canonical match; render
    #                             as "Speaker N" in the broadcast surface
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS meeting_speaker_roster (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL,
            cluster_label TEXT NOT NULL,
            proposed_canonical TEXT,
            confirmed_canonical TEXT,
            evidence_chunk_indices TEXT,
            evidence_text TEXT,
            prong_1_passed INTEGER,
            prong_1_reasoning TEXT,
            prong_2_passed INTEGER,
            prong_2_reasoning TEXT,
            status TEXT NOT NULL DEFAULT 'pending_review',
            model_id TEXT,
            resolution_action TEXT,
            resolved_at TIMESTAMP,
            resolved_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(meeting_id, cluster_label),
            FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_speaker_roster_meeting "
        "ON meeting_speaker_roster(meeting_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_speaker_roster_status "
        "ON meeting_speaker_roster(status)"
    )

    # D-039 follow-up — cross-session conflict detection.
    # Every active client (operator terminal in a browser tab, a
    # manually-run Python script, etc.) heartbeats into this table
    # every few seconds. The status banner reads the count of rows
    # whose last_seen is within the last 30s and shows a warning when
    # N > 1, so the operator never accidentally collides with another
    # session of themselves running somewhere else.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS active_sessions (
            session_id TEXT PRIMARY KEY,
            client_kind TEXT NOT NULL,
            current_action TEXT,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_active_sessions_last_seen "
        "ON active_sessions(last_seen)"
    )

    # ── S-004 agent-employee escalations ─────────────────────────────────
    # The canonical record of every agent escalation. Slack is the
    # notification layer (best-effort, can fail); this table is the
    # durable record James can always recover from. Each row carries
    # the full message shape (summary, what_i_see, what_id_do, deep_link,
    # audit_row) + the delivery flag so the operator-terminal badge can
    # surface "N escalations not yet delivered to Slack" without
    # round-tripping the webhook.
    #
    # `delivered_to_slack` flips true once a successful POST returns; the
    # table row stays as audit history regardless. Rows persist
    # indefinitely — pruning happens via a future operator-driven sweep.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_escalations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_role TEXT NOT NULL,
            severity TEXT NOT NULL,
            summary TEXT NOT NULL,
            what_i_see TEXT,
            what_id_do TEXT,
            deep_link TEXT,
            audit_row TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            delivered_to_slack INTEGER NOT NULL DEFAULT 0,
            delivered_at TIMESTAMP,
            acknowledged_at TIMESTAMP,
            acknowledged_by TEXT,
            slack_message_ts TEXT
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_escalations_undelivered "
        "ON pending_escalations(delivered_to_slack, created_at)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_escalations_unacked "
        "ON pending_escalations(acknowledged_at, created_at)"
    )
    # S-004 Phase 2 (D-055): slack_message_ts maps a Slack message timestamp
    # back to its escalation row so the reaction listener can resolve a
    # reaction_added event to the escalation it belongs to. Populated on
    # successful chat.postMessage; left NULL on webhook-fallback paths
    # (webhook responses don't expose a ts, so reactions on those messages
    # can't be dispatched — the operator uses EscalationsInboxPage instead).
    if not _column_exists(cursor, "pending_escalations", "slack_message_ts"):
        cursor.execute(
            "ALTER TABLE pending_escalations ADD COLUMN slack_message_ts TEXT"
        )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_escalations_slack_ts "
        "ON pending_escalations(slack_message_ts)"
    )

    # ── Balance Auditor (2026-05-30): per-project financial ledger ───────
    # Tracks deposits (operator-recorded) + spend (auto-fetched from
    # provider billing APIs). Current balance = sum(deposits) - sum(spend).
    # Append-only audit trail; rows are never updated or deleted.
    #
    # Event types (see agents/balance-auditor.md § Schema for full enum):
    #   deposit_observed       operator-recorded money-in
    #   spend_observed         finalized-bucket cost from provider API
    #   api_balance_snapshot   periodic snapshot of computed-balance
    #   discrepancy_flagged    observed-vs-expected drift > threshold
    #   manual_correction      operator override (refund/adjustment)
    #
    # The UNIQUE constraint enforces idempotency for spend_observed rows
    # (re-fetching the same bucket = INSERT OR IGNORE no-op). Other event
    # types leave bucket_*_time NULL; the UNIQUE check tolerates multiple
    # NULL-bucket rows of the same event_type since SQLite treats NULLs
    # as distinct under UNIQUE constraints by default.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS balance_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            provider TEXT NOT NULL,
            event_type TEXT NOT NULL,
            amount_cents INTEGER,
            currency TEXT NOT NULL DEFAULT 'usd',
            bucket_start_time INTEGER,
            bucket_end_time INTEGER,
            running_balance_cents INTEGER,
            source TEXT NOT NULL,
            notes TEXT,
            external_ref TEXT,
            UNIQUE(provider, event_type, bucket_start_time, bucket_end_time)
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_balance_ledger_observed_at "
        "ON balance_ledger(observed_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_balance_ledger_provider_event "
        "ON balance_ledger(provider, event_type)"
    )

    # ── D-054 follow-up: content_hash normalization upgrade ─────────────
    # Pre-2026-05-26 content_hashes used `lower(strip(...))` normalization,
    # which produced different hashes for "the budget" vs "The budget." vs
    # "the budget!" The new `_compute_content_hash` (above) strips all
    # non-alphanumeric chars so those three forms hash identically. That
    # closes a subtle re-extraction-duplicate gap on the polished-becomes-
    # canonical flow (DisputedQuotesPage Verify path).
    #
    # PRAGMA user_version gates the migration — it runs once per DB on
    # first startup after this code lands, then is skipped thereafter.
    # The cloud DB (Railway) picks it up automatically on next deploy.
    user_version = cursor.execute("PRAGMA user_version").fetchone()[0]
    if user_version < 1:
        # Re-compute every existing quotes.content_hash with the new
        # normalization. Row-by-row with try/except so a UNIQUE
        # violation (two existing rows that previously hashed
        # differently but now collide) doesn't abort the whole migration
        # — we keep the offending row's old hash and surface a warning.
        rows = cursor.execute(
            "SELECT id, speaker_name, quote_text FROM quotes"
        ).fetchall()
        migrated = 0
        skipped_collisions = 0
        for row in rows:
            new_hash = _compute_content_hash(row['speaker_name'], row['quote_text'])
            try:
                cursor.execute(
                    "UPDATE quotes SET content_hash = ? WHERE id = ?",
                    (new_hash, row['id']),
                )
                migrated += 1
            except sqlite3.IntegrityError as e:
                # UNIQUE (meeting_id, content_hash) violation — another
                # row in the same meeting already has the new hash.
                # Leaving this row's old hash means re-extraction may
                # still create a duplicate for it specifically; the
                # operator can resolve manually if it ever bites.
                logger.warning(
                    "content_hash migration: row %s would collide with "
                    "existing row; keeping old hash. error=%s",
                    row['id'], e,
                )
                skipped_collisions += 1
        cursor.execute("PRAGMA user_version = 1")
        logger.info(
            "content_hash migration: %d rows migrated, %d skipped (collisions)",
            migrated, skipped_collisions,
        )

    # ── S-008 V0 input-security migrations ────────────────────────────────
    # See `01_Project_Overview/S008_INPUT_SECURITY_SPEC.md` chunk 2 + the
    # threat-model surfaces S-2 (extraction anomalies), S-4 (Haiku-fallback
    # provenance), S-7 (agent action audit), S-8 (orchestrator rung audit).
    #
    # All idempotent.

    # S-2 — extraction anomalies surfacing for owner review.
    # Populated by `parsers/extraction_postcheck.py` (chunk 2.5) when a
    # post-extraction deterministic rule pass flags a NotebookLM-emitted
    # payload as anomalous. The row references the source notebook_outputs
    # row + carries the structured anomaly verdict the operator reviews.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS extraction_anomalies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL,
            notebook_output_id INTEGER,
            output_type TEXT NOT NULL,
            anomaly_kind TEXT NOT NULL,
            anomaly_detail TEXT,
            raw_excerpt TEXT,
            flagged_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TIMESTAMP,
            reviewed_by TEXT,
            verdict TEXT,
            FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE,
            FOREIGN KEY (notebook_output_id) REFERENCES notebook_outputs(id) ON DELETE SET NULL
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_extraction_anomalies_unreviewed "
        "ON extraction_anomalies(reviewed_at, flagged_at)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_extraction_anomalies_meeting "
        "ON extraction_anomalies(meeting_id)"
    )

    # S-4 — meetings.scraper_source provenance.
    # Marks rows that came from the Haiku-class fallback scraper (S-036)
    # vs the deterministic per-city parsers, so the parser-custodian's
    # spot-check surface can filter on it. Existing rows default to
    # 'deterministic_parser' (the historical source); new Haiku-fallback
    # writes set 'haiku_fallback'.
    if not _column_exists(cursor, "meetings", "scraper_source"):
        cursor.execute(
            "ALTER TABLE meetings ADD COLUMN scraper_source TEXT "
            "DEFAULT 'deterministic_parser'"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_meetings_scraper_source "
            "ON meetings(scraper_source)"
        )

    # S-7 + S-8 — centralized agent action audit.
    # Every agent action (verify / reject / counter / escalate / promote /
    # trigger-sub-agent) writes one row here. Captures `action_argument_origin`
    # as a SHA-256 hash of the row content the agent saw at decision time —
    # the audit can later confirm the agent acted on the row it claims, even
    # if the underlying row was later updated. For the orchestrator,
    # `rung_attempted` / `rung_outcome` record graduated-autonomy enforcement
    # (S-007 ladder).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_role TEXT NOT NULL,
            action_name TEXT NOT NULL,
            action_argument_table TEXT,
            action_argument_id INTEGER,
            action_argument_origin TEXT,
            reasoning TEXT,
            rung_attempted TEXT,
            rung_outcome TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_actions_role_created "
        "ON agent_actions(agent_role, created_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_actions_argument "
        "ON agent_actions(action_argument_table, action_argument_id)"
    )

    # S-008 V0 chunk 3 — user_input_attempts (S-11 + S-12 rate-limit storage).
    # Backs `parsers/input_moderation.py` per-user per-surface per-day cap.
    # Append-only; never updated. Pruning is operator-discretion later.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_input_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            surface TEXT NOT NULL,
            accept INTEGER NOT NULL,
            reason TEXT,
            submitted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_input_attempts_per_day "
        "ON user_input_attempts(user_id, surface, submitted_at)"
    )

    # V1-UI-3 — `suggestions` content storage.
    # Per V1_PUBLIC_RELEASE_SPEC.md: login-gated user-supplied query
    # against a processed episode. Free-text passes through
    # input_moderation(surface="suggestion_query") before persistence;
    # this table stores the resulting content + the moderation verdict
    # so an operator review queue can later replay them.
    #
    # `accepted = 0` rows ARE kept (audit-trail). The operator queue
    # surfaces only rows where `operator_review_needed = 1` (the
    # moderation classifier flagged them for human review without
    # outright rejection).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            meeting_id INTEGER NOT NULL,
            query_text TEXT NOT NULL,
            normalized_text TEXT,
            accepted INTEGER NOT NULL DEFAULT 0,
            reason TEXT,
            operator_review_needed INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_suggestions_user "
        "ON suggestions(user_id, created_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_suggestions_meeting "
        "ON suggestions(meeting_id, created_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_suggestions_review_queue "
        "ON suggestions(operator_review_needed, created_at DESC) "
        "WHERE operator_review_needed = 1"
    )

    # ── S-012 + D-095 — Account system foundation + Creator Network ───────
    # Per `01_Project_Overview/ACCOUNT_SYSTEM_SPEC.md` chunks 1 + 7-9.
    # Idempotent. The auth flow (chunks 2-3) ships separately once James
    # provides Google Cloud Web OAuth client credentials.

    # users — the foundation table. role added 2026-06-10 per D-095.
    _create_users_table(cursor)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_normalized "
        "ON users(lower(email))"
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)")

    # Idempotent ALTER for existing dev DBs that have users without role.
    if not _column_exists(cursor, "users", "role"):
        cursor.execute(
            "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'light'"
        )
    if not _column_exists(cursor, "users", "librarian_access"):
        cursor.execute(
            "ALTER TABLE users ADD COLUMN librarian_access "
            "TEXT NOT NULL DEFAULT 'none'"
        )
    if not _column_exists(
        cursor,
        "users",
        "librarian_enforcement_epoch",
    ):
        cursor.execute(
            "ALTER TABLE users ADD COLUMN librarian_enforcement_epoch "
            "INTEGER NOT NULL DEFAULT 0 "
            "CHECK (librarian_enforcement_epoch >= 0)"
        )

    # Passwords are a credential attached to the same provider-neutral user
    # row as Google. The verifier is scrypt; plaintext passwords and reset
    # bearer tokens are never stored.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS password_credentials (
            user_id INTEGER PRIMARY KEY,
            password_hash TEXT NOT NULL,
            password_salt TEXT NOT NULL,
            scrypt_n INTEGER NOT NULL,
            scrypt_r INTEGER NOT NULL,
            scrypt_p INTEGER NOT NULL,
            scrypt_dklen INTEGER NOT NULL,
            failed_attempts INTEGER NOT NULL DEFAULT 0
                CHECK (failed_attempts >= 0),
            locked_until TEXT,
            last_failed_at TEXT,
            password_changed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token_hash TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_user "
        "ON password_reset_tokens(user_id, created_at DESC)"
    )

    # Founder-distributed invitation cards. The bearer token is never stored
    # in the database: only its SHA-256 digest crosses the activation seam.
    # One card can activate one account, has no silent expiry, and can be
    # revoked by the owner before redemption.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS invitation_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_name TEXT NOT NULL,
            serial_number INTEGER NOT NULL CHECK (serial_number > 0),
            token_hash TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64),
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'redeemed', 'revoked')),
            redeemed_by_user_id INTEGER,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            redeemed_at TIMESTAMP,
            revoked_at TIMESTAMP,
            UNIQUE (batch_name, serial_number),
            FOREIGN KEY (redeemed_by_user_id) REFERENCES users(id)
                ON DELETE SET NULL
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_invitation_codes_batch "
        "ON invitation_codes(batch_name, serial_number)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_invitation_codes_status "
        "ON invitation_codes(status, created_at DESC)"
    )
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS invitation_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invitation_code_id INTEGER NOT NULL,
            event_type TEXT NOT NULL
                CHECK (event_type IN ('imported', 'redeemed', 'revoked')),
            actor_user_id INTEGER,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (invitation_code_id) REFERENCES invitation_codes(id)
                ON DELETE CASCADE,
            FOREIGN KEY (actor_user_id) REFERENCES users(id)
                ON DELETE SET NULL
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_invitation_events_code "
        "ON invitation_events(invitation_code_id, id)"
    )

    # Private owner-side attribution. Public role columns carry only the
    # institutional identity; the authenticated user link lives here.
    operator_identity.ensure_operator_review_events_schema(cursor)

    # D-172 — flagship-brokered CLI authentication + generation provenance.
    # Kept as a focused idempotent initializer beside the users-table setup
    # because every row is anchored to the canonical account namespace.
    init_cli_auth_schema(cursor)
    init_flagship_generation_schema(cursor)

    # follows — generic subscribe primitive (city/county/topic/meeting).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS follows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            target_type TEXT NOT NULL CHECK (target_type IN ('city','county','topic','meeting')),
            target_key TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, target_type, target_key),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_follows_user ON follows(user_id)")
    # Session-103 (product-slice3a) — the notification fan-out query
    # joins follows(target_type, target_key) against
    # meeting_topic_tags(tag_id) + meetings(city_name). The existing
    # index leads with user_id, which is the wrong shape for that
    # query. Adding a target-first index without touching the unique
    # key so cross-user idempotence stays intact.
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_follows_target "
        "ON follows(target_type, target_key, user_id)"
    )

    # Session-105 — optional per-city topic decoration for city-follow
    # notifications. These rows do not create a second notification path;
    # they only record which matched meeting tags should be shown to each
    # city follower.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS follow_city_topics (
            user_id INTEGER NOT NULL,
            city_key TEXT NOT NULL,
            tag_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, city_key, tag_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_follow_city_topics_lookup "
        "ON follow_city_topics(user_id, city_key)"
    )

    # Session-103 (product-slice3a) — deterministic per-meeting topic
    # classification. Sol Round-2: normalized table, not a JSON column
    # on meetings; five rows max per meeting; evidence lives on the
    # relationship so the notification email can truthfully say WHY
    # ("matched 'hyperscaler' in the meeting title"). matcher_version
    # is a stamped lineage label so a future trigger-vocab retune is
    # distinguishable in place without a data migration.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS meeting_topic_tags (
            meeting_id      INTEGER NOT NULL,
            tag_id          TEXT NOT NULL,
            evidence_field  TEXT NOT NULL,
            trigger_phrase  TEXT NOT NULL,
            matcher_version TEXT NOT NULL,
            created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (meeting_id, tag_id),
            FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_meeting_topic_tags_tag "
        "ON meeting_topic_tags(tag_id, meeting_id)"
    )

    # channel_revival_requests — CGC heartbeat storage.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS channel_revival_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            target_type TEXT NOT NULL CHECK (target_type IN ('city','county')),
            target_key TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, target_type, target_key),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # notification_prefs — per-user digest cadence + channel selection.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notification_prefs (
            user_id INTEGER PRIMARY KEY,
            digest_cadence TEXT NOT NULL DEFAULT 'weekly' CHECK (digest_cadence IN ('off','daily','weekly','monthly')),
            email_enabled INTEGER NOT NULL DEFAULT 1,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # Session-103 (product-slice3b) — one durable fan-out event per
    # publicly-visible meeting. The meeting_id UNIQUE constraint is the
    # concurrency/idempotency gate: only the connection that inserts this row
    # is allowed to enumerate followers and populate the outbox.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notification_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL UNIQUE,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            recipient_count INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
        )
    """)

    # Durable per-recipient delivery queue. UNIQUE(user_id, meeting_id) is a
    # second duplicate-delivery guard beneath notification_events, while the
    # stable row id becomes Resend's idempotency-key suffix.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notification_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            meeting_id INTEGER NOT NULL,
            reasons_json TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            sent_at TIMESTAMP,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_error TEXT,
            provider_message_id TEXT,
            UNIQUE(user_id, meeting_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_notification_outbox_pending "
        "ON notification_outbox(next_attempt_at, sent_at)"
    )

    # Opaque token ids are stored separately from their HMAC. Tokens are
    # single-use and expire 30 days after minting.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS unsubscribe_tokens (
            token_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            used_at TIMESTAMP,
            expires_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+30 days')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    if not _column_exists(cursor, "unsubscribe_tokens", "expires_at"):
        # SQLite does not permit a non-constant datetime expression in an
        # ALTER TABLE default. Backfill the migration explicitly; token minting
        # also writes the 30-day value so upgraded databases retain the same
        # contract as fresh databases.
        cursor.execute(
            "ALTER TABLE unsubscribe_tokens ADD COLUMN expires_at TIMESTAMP"
        )
        cursor.execute(
            """
            UPDATE unsubscribe_tokens
            SET expires_at = datetime(created_at, '+30 days')
            WHERE expires_at IS NULL
            """
        )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_unsubscribe_tokens_user "
        "ON unsubscribe_tokens(user_id)"
    )

    # creator_agreements — TOS + disclaimer acceptance per D-095.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS creator_agreements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            tos_version TEXT NOT NULL,
            disclaimer_version TEXT NOT NULL,
            disclaimer_acknowledged_at TIMESTAMP NOT NULL,
            signed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            revoked_at TIMESTAMP,
            revoked_reason TEXT,
            signup_ip_hash TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_creator_agreements_user_active "
        "ON creator_agreements(user_id, revoked_at)"
    )

    # creator_downloads — per-download audit trail per D-095 faucet observation.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS creator_downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            asset_id TEXT NOT NULL,
            asset_type TEXT NOT NULL CHECK (asset_type IN ('clip','summary','infographic','audio','video','other')),
            tos_version_at_download TEXT NOT NULL,
            download_source_ip_hash TEXT,
            downloaded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_creator_downloads_user_time "
        "ON creator_downloads(user_id, downloaded_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_creator_downloads_asset_time "
        "ON creator_downloads(asset_id, downloaded_at DESC)"
    )

    # creator_feedback — moderated free-text feedback per S-11 surface.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS creator_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            asset_id TEXT,
            feedback_text TEXT NOT NULL,
            submitted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            operator_review_needed INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_creator_feedback_review "
        "ON creator_feedback(operator_review_needed, submitted_at)"
    )

    # ── Operator review queue audit columns ─────────────────────────────
    # Per the V1-Sec-2 follow-up that closes the moderation loop V1-UI-3 +
    # chunk 8 open: the operator surface in OperatorTerminal needs to clear
    # the per-row review flag + record who acted on it, when, what they did,
    # and (optionally) a short operator note. We also add review-flag
    # columns to creator_agreements so chunk 8's moderation verdict can
    # ride on the agreement row itself (suggestions + creator_feedback
    # already have operator_review_needed shipped).
    #
    # Idempotent ALTERs for existing dev DBs.

    for col, ddl in [
        # creator_agreements — review flag + moderation evidence + audit.
        ("operator_review_needed",
            "ALTER TABLE creator_agreements ADD COLUMN operator_review_needed INTEGER NOT NULL DEFAULT 0"),
        ("moderation_reason",
            "ALTER TABLE creator_agreements ADD COLUMN moderation_reason TEXT"),
        ("moderation_normalized_text",
            "ALTER TABLE creator_agreements ADD COLUMN moderation_normalized_text TEXT"),
        ("operator_resolved_at",
            "ALTER TABLE creator_agreements ADD COLUMN operator_resolved_at TIMESTAMP"),
        ("operator_resolved_by",
            "ALTER TABLE creator_agreements ADD COLUMN operator_resolved_by TEXT"),
        ("operator_action",
            "ALTER TABLE creator_agreements ADD COLUMN operator_action TEXT"),
        ("operator_note",
            "ALTER TABLE creator_agreements ADD COLUMN operator_note TEXT"),
    ]:
        if not _column_exists(cursor, "creator_agreements", col):
            cursor.execute(ddl)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_creator_agreements_review_queue "
        "ON creator_agreements(operator_review_needed, signed_at DESC) "
        "WHERE operator_review_needed = 1"
    )

    for col, ddl in [
        ("operator_resolved_at",
            "ALTER TABLE suggestions ADD COLUMN operator_resolved_at TIMESTAMP"),
        ("operator_resolved_by",
            "ALTER TABLE suggestions ADD COLUMN operator_resolved_by TEXT"),
        ("operator_action",
            "ALTER TABLE suggestions ADD COLUMN operator_action TEXT"),
        ("operator_note",
            "ALTER TABLE suggestions ADD COLUMN operator_note TEXT"),
    ]:
        if not _column_exists(cursor, "suggestions", col):
            cursor.execute(ddl)

    for col, ddl in [
        ("operator_resolved_at",
            "ALTER TABLE creator_feedback ADD COLUMN operator_resolved_at TIMESTAMP"),
        ("operator_resolved_by",
            "ALTER TABLE creator_feedback ADD COLUMN operator_resolved_by TEXT"),
        ("operator_action",
            "ALTER TABLE creator_feedback ADD COLUMN operator_action TEXT"),
        ("operator_note",
            "ALTER TABLE creator_feedback ADD COLUMN operator_note TEXT"),
    ]:
        if not _column_exists(cursor, "creator_feedback", col):
            cursor.execute(ddl)

    # ── D-100 independent-verification scaffolding ───────────────────────
    # Persistence for the round-trip contract documented at
    # 01_Project_Overview/HARDENING_FINDINGS_SCHEMA.md.
    #
    # Per D-100, the independent verification of Z-SPAN's defensive
    # surfaces runs on the Antigravity-Jules-Gemini-Pro side. This
    # scaffolding is the flow-back layer: findings the Gemini-Pro
    # analysis surfaces (defensive observations + suggested mitigations)
    # ingest here, persist as an audit trail, and surface in the operator
    # review queue alongside V1-UI-3 suggestions + V1-Sec-2 creator
    # signups.

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS adversarial_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            schema_version TEXT NOT NULL DEFAULT '1',
            run_label TEXT NOT NULL,
            run_date TEXT NOT NULL,
            runner_identity TEXT NOT NULL,
            scope_surfaces TEXT NOT NULL,
            runner_notes TEXT,
            findings_count INTEGER NOT NULL DEFAULT 0,
            ingested_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            ingested_by TEXT
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_adversarial_runs_date "
        "ON adversarial_runs(run_date DESC)"
    )

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS adversarial_findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            surface_id TEXT NOT NULL,
            severity TEXT NOT NULL CHECK (severity IN ('low','medium','high')),
            defensive_observation TEXT NOT NULL,
            suggested_mitigation TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','triaged','resolved')),
            operator_resolved_at TIMESTAMP,
            operator_resolved_by TEXT,
            operator_action TEXT,
            operator_note TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (run_id) REFERENCES adversarial_runs(id) ON DELETE CASCADE
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_adversarial_findings_open "
        "ON adversarial_findings(status, created_at DESC) "
        "WHERE status = 'open'"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_adversarial_findings_run "
        "ON adversarial_findings(run_id)"
    )

    # ── D-095 / D-006 repository deposit gate ───────────────────────────
    # Per D-095 Decentralized Creator Network, every asset destined for
    # the static-asset repository carries a repository_status value
    # (draft / pending_owner_review / approved / withdrawn). Approval is
    # owner-only per D-006 extending to repository deposits — only
    # approved assets become available to creators.
    #
    # The repository_assets table is a polymorphic registry: each row
    # references its source via (source_type, source_id) so a single
    # operator queue + a single faucet-decision log span Studio outputs
    # (notebook_outputs), member quotes (member_quotes), and future
    # asset classes (watermarked clips, remixed assets) without per-class
    # status columns. Population happens via parsers/scripts/
    # enqueue_repository_candidates.py for the V0 seed and via the
    # worker.py auto-deposit path in a follow-up chunk.
    #
    # Per CREATOR_NETWORK_PLAYBOOK.md § Faucet criteria the rejection /
    # withdrawal log is publicly readable when creators arrive. The
    # repository_filter_log table persists the decision trail so the
    # public endpoint that surfaces it is a thin read over an existing
    # data shape rather than a new write surface bolted on at launch.

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS repository_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL CHECK (source_type IN ('notebook_output','member_quote','clip_file','other')),
            source_id INTEGER NOT NULL,
            source_meeting_id INTEGER NOT NULL,
            asset_type TEXT NOT NULL CHECK (asset_type IN ('clip','summary','infographic','audio','video','other')),
            asset_metadata TEXT,
            repository_status TEXT NOT NULL DEFAULT 'pending_owner_review' CHECK (repository_status IN ('draft','pending_owner_review','approved','withdrawn')),
            queued_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            approved_at TIMESTAMP,
            approved_by TEXT,
            withdrawn_at TIMESTAMP,
            withdrawn_reason TEXT,
            filter_reason TEXT,
            UNIQUE(source_type, source_id, asset_type),
            FOREIGN KEY (source_meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_repository_assets_pending "
        "ON repository_assets(repository_status, queued_at DESC) "
        "WHERE repository_status = 'pending_owner_review'"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_repository_assets_approved "
        "ON repository_assets(repository_status, approved_at DESC) "
        "WHERE repository_status = 'approved'"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_repository_assets_meeting "
        "ON repository_assets(source_meeting_id)"
    )

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS repository_filter_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL,
            filter_action TEXT NOT NULL CHECK (filter_action IN ('reject','withdraw')),
            filter_reason TEXT NOT NULL,
            filtered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            filtered_by TEXT,
            FOREIGN KEY (asset_id) REFERENCES repository_assets(id) ON DELETE CASCADE
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_repository_filter_log_time "
        "ON repository_filter_log(filtered_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_repository_filter_log_asset "
        "ON repository_filter_log(asset_id)"
    )

    # ─── S-062 V0 parser-health logic — DORMANT SCHEMA (2026-06-19) ───
    # Per James 2026-06-19 (post speed-audit): build the parser-health
    # LOGIC but activate and test nothing. A forgotten background system
    # randomly pinging thousands of city endpoints is exactly what he
    # doesn't want running unattended — the logic gets built now, and
    # any activation waits until well after the build ships.
    #
    # This migration creates the schema + helper-callable surface. NO
    # scheduler hook wires it up. Activation is a deliberate operator-side
    # decision once the canonical endpoint catalog is large enough to need
    # continuous tracking (per S-062 spec). The audit's claim — that
    # continuous tracking "unlocks future N=5-10 rebuild fan-out because you
    # finally know what's broken without an operator triggering the
    # discovery" — only pays off when activated; until then, the table sits
    # empty and the helper module (parsers/parser_health.py) sits unused.
    #
    # Activation wire-in point (dormant since D-169 retired the auto_scraper
    # daemon): the future per-city-scrape callsite (whichever surface replaces
    # ScrapeStatusPanel — the BitTorrent parser-view redesign, when it lands)
    # should call, after a successful scrape:
    #   from parser_health import classify_and_record
    #   classify_and_record(city_name, current_count=count, scrape_log_id=...)
    # Default baseline_window_days=14.
    if not _column_exists(cursor, "scrape_log", "auto_suppressed"):
        cursor.execute("ALTER TABLE scrape_log ADD COLUMN auto_suppressed INTEGER NOT NULL DEFAULT 0")
    if not _column_exists(cursor, "scrape_log", "suppression_reason"):
        cursor.execute("ALTER TABLE scrape_log ADD COLUMN suppression_reason TEXT")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS parser_health_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city_name TEXT NOT NULL,
            detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL,
            current_count INTEGER NOT NULL DEFAULT 0,
            baseline_count INTEGER,
            baseline_window_days INTEGER,
            delta_pct REAL,
            reason TEXT,
            raw_scrape_log_id INTEGER,
            acknowledged_at TIMESTAMP,
            acknowledged_by TEXT
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_parser_health_alerts_city "
        "ON parser_health_alerts(city_name, detected_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_parser_health_alerts_status "
        "ON parser_health_alerts(status, detected_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_parser_health_alerts_unack "
        "ON parser_health_alerts(detected_at DESC) WHERE acknowledged_at IS NULL"
    )

    # ─── D-155 — Archival video mirror (PLAYER-2) ───
    # One row per archived meeting recording. The archive itself lives
    # OUTSIDE every repo (operator-side dir, `zspan_video_archive_dir`
    # setting; default ~/zspan-video-archive/) — this table is the
    # provenance ledger only. Two source classes carry two different
    # verification claims (D-146): a YT-sourced copy can NOT byte-hash-
    # verify against what the city published (YouTube re-encodes), so its
    # sha256 attests OUR copy's chain of custody; a vendor-direct MP4 is
    # the byte-exact published file, so its sha256 verifies externally.
    # Rescue fields stay NULL until the operator activates a rescue
    # (embed_disabled | source_deleted) — serving is deliberately NOT
    # built at V1 (D-155 § 5: offline-first, zero cloud billing).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS meeting_media_archive (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL,
            source_url TEXT NOT NULL,
            source_kind TEXT NOT NULL,          -- youtube | direct_mp4 | vendor_page
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            sha256 TEXT,
            bytes INTEGER,
            resolution TEXT,                    -- e.g. "720p"
            format TEXT,                        -- container/codec summary from yt-dlp
            archive_path_rel TEXT,              -- relative to zspan_video_archive_dir
            status TEXT NOT NULL DEFAULT 'ok',  -- ok | empty | error  (F8 discipline)
            error TEXT,
            rescue_reason TEXT,                 -- embed_disabled | source_deleted (NULL until activated)
            rescue_activated_at TIMESTAMP,
            serving_url TEXT,                   -- NULL until an operator serving decision (D-155 § 5)
            UNIQUE(meeting_id),
            FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_media_archive_status "
        "ON meeting_media_archive(status, fetched_at DESC)"
    )
    # Opt-in per-meeting keep-file flag (D-155 § 3). Set via the archive
    # script's --set-flag / cleared via --clear-flag; the fetch honors it
    # in --flagged mode. Default 0 — archival is deliberate, never ambient.
    if not _column_exists(cursor, "meetings", "archive_video"):
        cursor.execute("ALTER TABLE meetings ADD COLUMN archive_video INTEGER NOT NULL DEFAULT 0")

    # ─── RR-4 — Public corrections log (S-043 B-4) ───
    # The institutional doorbell. One row per correction request that the
    # operator chose to log; the RUNNING LOG IS PUBLIC by design ("visibly,
    # not silently" — CORRECTIONS_POLICY_DRAFT). Privacy floor: NO reporter
    # fields exist in this table — who asked stays in the corrections@
    # mailbox, never the database; the log shows only what was checked and
    # what changed. detail_internal is owner-side working notes and is
    # never served to anonymous callers.
    # Status vocabulary mirrors the policy's three honest outcomes plus
    # the intake state: under_review | corrected | record_stands |
    # disputed_ambiguous.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS corrections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER,
            corrected_surface TEXT,
            status TEXT NOT NULL DEFAULT 'under_review',
            summary_public TEXT,
            detail_internal TEXT,
            reported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMP,
            FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE SET NULL
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_corrections_reported "
        "ON corrections(reported_at DESC)"
    )

    conn.commit()
    conn.close()


def register_notebook(meeting_id: int, notebook_id: str) -> bool:
    """Bind a NotebookLM notebook ID to a meeting. Returns True if updated."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE meetings SET notebook_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (notebook_id, meeting_id)
    )
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def get_meeting_with_notebook(meeting_id: int) -> Optional[Dict]:
    """Get a meeting row including its notebook_id and any cached outputs.

    Also LEFT-joins work_orders so callers can see whether the meeting's
    broadcast has been approved through the D-032 review gate. The
    BroadcastPage uses these to gate public render (D-001 + D-031).
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT m.*,
               w.approved_at AS wo_approved_at,
               w.approved_by AS wo_approved_by,
               w.youtube_video_url AS wo_video_url
        FROM meetings m
        LEFT JOIN work_orders w ON w.meeting_id = m.id
        WHERE m.id = ?
        """,
        (meeting_id,),
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None

    meeting = dict(row)

    # Pull all cached outputs
    cursor.execute(
        """
        SELECT no.*, fg.ribbon_token, fg.status AS registration_state
        FROM notebook_outputs AS no
        LEFT JOIN flagship_generations AS fg
            ON fg.notebook_output_id = no.id
        WHERE no.meeting_id = ?
        ORDER BY no.output_type
        """,
        (meeting_id,),
    )
    outputs = {}
    for output_row in cursor.fetchall():
        out = dict(output_row)
        outputs[out['output_type']] = {
            'content': out['content'],
            'content_url': out['content_url'],
            'prompt_filename': out['prompt_filename'],
            'prompt_version': out['prompt_version'],
            'generated_at': out['generated_at'],
            'error': out['error'],
            'voided_at': out['voided_at'],
            'voided_by': out['voided_by'],
            'ribbon_token': out['ribbon_token'],
            'registration_state': (
                out['registration_state']
                if out['registration_state']
                else (
                    'pending'
                    if out['output_type'] in FLAGSHIP_RIBBON_OUTPUT_TYPES
                    else None
                )
            ),
        }

    meeting['notebook_outputs'] = outputs
    conn.close()
    return meeting


def get_meeting_public_record(public_id: str) -> Optional[Dict]:
    """Resolve a canonical or aliased public ID to the canonical meeting row."""
    if not isinstance(public_id, str) or PUBLIC_ID_RE.fullmatch(public_id) is None:
        return None

    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT m.*, m.public_id AS canonical_public_id
            FROM meetings AS m
            WHERE m.public_id = ?
            """,
            (public_id,),
        ).fetchone()
        if row is None:
            row = conn.execute(
                """
                SELECT m.*, m.public_id AS canonical_public_id
                FROM meeting_public_id_aliases AS a
                JOIN meetings AS m ON m.id = a.canonical_meeting_id
                WHERE a.alias_public_id = ?
                """,
                (public_id,),
            ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def is_output_already_present(meeting_id: int, output_type: str) -> Optional[Dict]:
    """Return the existing notebook_outputs row iff it represents a SUCCESSFUL
    prior run for this (meeting, output_type), else None.

    "Successful" = error column is null/empty AND at least one of content /
    content_url is non-empty. We deliberately don't trust a row that has
    `error` set or that's all-nulls — those are mid-flight or failed states
    that the next retry should overwrite.

    Used by `fetcher.fetch_one_output` to make retries idempotent: if an
    earlier worker run completed an output before being killed, the retry
    skips that output and only re-runs the genuinely missing ones. This
    is the kill-survivability primitive — see DECISIONS.md (added 2026-05-12).
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT meeting_id, notebook_id, output_type, content, content_url,
               prompt_filename, prompt_version, generated_at, error
        FROM notebook_outputs
        WHERE meeting_id = ? AND output_type = ?
        """,
        (meeting_id, output_type),
    )
    row = cursor.fetchone()
    conn.close()
    if row is None:
        return None
    d = dict(row)
    err = (d.get("error") or "").strip()
    if err:
        return None
    has_content = bool((d.get("content") or "").strip())
    has_url = bool((d.get("content_url") or "").strip())
    if not (has_content or has_url):
        return None
    return d


def set_notebook_output_void_state(
    meeting_id: int,
    output_type: str,
    *,
    voided: bool,
    actor_email: str,
    actor_user_id: int,
    event_key: str,
) -> Optional[Dict[str, Any]]:
    """Void or restore one stored output without changing its content.

    Repeating the same requested state is a successful no-op. Every request
    still receives a private operator-review event so the click trail remains
    complete; ``changed`` distinguishes the no-op in the response.
    """
    if not event_key:
        raise ValueError("event_key is required")
    if actor_user_id <= 0:
        raise ValueError("actor_user_id must identify an authenticated user")
    normalized_email = str(actor_email or "").strip()
    if voided and not normalized_email:
        raise ValueError("actor_email is required when voiding an output")

    conn = get_connection()
    cursor = conn.cursor()
    try:
        row = cursor.execute(
            """
            SELECT id, meeting_id, output_type, voided_at, voided_by
            FROM notebook_outputs
            WHERE meeting_id = ? AND output_type = ?
            """,
            (meeting_id, output_type),
        ).fetchone()
        if row is None:
            return None

        currently_voided = row["voided_at"] is not None
        changed = currently_voided != voided
        if changed and voided:
            cursor.execute(
                """
                UPDATE notebook_outputs
                SET voided_at = CURRENT_TIMESTAMP, voided_by = ?
                WHERE id = ?
                """,
                (normalized_email, row["id"]),
            )
        elif changed:
            cursor.execute(
                """
                UPDATE notebook_outputs
                SET voided_at = NULL, voided_by = NULL
                WHERE id = ?
                """,
                (row["id"],),
            )

        occurred_at = cursor.execute(
            "SELECT CURRENT_TIMESTAMP AS occurred_at"
        ).fetchone()["occurred_at"]
        cursor.execute(
            """
            INSERT INTO operator_review_events (
                event_key, action, meeting_id, output_type,
                actor_user_id, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_key,
                "void" if voided else "restore",
                meeting_id,
                output_type,
                actor_user_id,
                occurred_at,
            ),
        )
        updated = cursor.execute(
            """
            SELECT meeting_id, output_type, voided_at, voided_by
            FROM notebook_outputs WHERE id = ?
            """,
            (row["id"],),
        ).fetchone()
        conn.commit()
        result = dict(updated)
        result["changed"] = changed
        return result
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────
# D-039 follow-up: cross-session conflict detection (active_sessions)
# ─────────────────────────────────────────────────────────────────

# Sessions older than this many seconds are considered stale and pruned
# from the active_sessions table. Slightly longer than the frontend's
# poll interval (5s) so a brief network blip doesn't flap a session
# in-and-out of "active."
_SESSION_STALE_SECONDS = 30


def heartbeat_session(
    session_id: str,
    client_kind: str,
    current_action: Optional[str] = None,
) -> Dict:
    """Upsert this session's heartbeat. Returns a small summary dict
    describing OTHER currently-active sessions.

    Should be called every ~5-10 seconds by each long-lived client (the
    operator terminal, manually-run Python scripts that take more than a
    few seconds, etc.). Stale rows are pruned on every call so the table
    stays small.

    Returns:
        {
          "other_active": int,                    # count of NON-self sessions
          "self_session_id": str,
          "sessions": [{client_kind, age_seconds, current_action}, ...]
        }
    """
    if not session_id or not client_kind:
        return {
            "other_active": 0,
            "self_session_id": session_id or "",
            "sessions": [],
        }

    conn = get_connection()
    cursor = conn.cursor()

    # Prune stale heartbeats. Using SQLite's local-time CURRENT_TIMESTAMP
    # for consistency with how the row was written.
    cursor.execute(
        "DELETE FROM active_sessions WHERE "
        "last_seen < datetime('now', ?)",
        (f"-{_SESSION_STALE_SECONDS} seconds",),
    )

    # Upsert self.
    cursor.execute(
        """
        INSERT INTO active_sessions (session_id, client_kind, current_action,
                                     first_seen, last_seen)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(session_id) DO UPDATE SET
            client_kind = excluded.client_kind,
            current_action = excluded.current_action,
            last_seen = CURRENT_TIMESTAMP
        """,
        (session_id, client_kind, current_action),
    )

    # Read OTHER active sessions for the summary.
    cursor.execute(
        """
        SELECT session_id, client_kind, current_action,
               CAST((julianday('now') - julianday(last_seen)) * 86400 AS INTEGER) AS age_seconds
        FROM active_sessions
        WHERE session_id != ?
        ORDER BY last_seen DESC
        """,
        (session_id,),
    )
    others = [dict(r) for r in cursor.fetchall()]

    conn.commit()
    conn.close()

    return {
        "other_active": len(others),
        "self_session_id": session_id,
        "sessions": others,
    }


def list_active_sessions() -> list[Dict]:
    """Read-only list of all currently-active sessions (caller's own
    session is included if it heartbeated recently). For diagnostic
    surfaces — the heartbeat endpoint should be used for routine UI.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT session_id, client_kind, current_action,
               first_seen, last_seen,
               CAST((julianday('now') - julianday(last_seen)) * 86400 AS INTEGER) AS age_seconds
        FROM active_sessions
        WHERE last_seen >= datetime('now', ?)
        ORDER BY last_seen DESC
        """,
        (f"-{_SESSION_STALE_SECONDS} seconds",),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def save_notebook_output(
    meeting_id: int,
    notebook_id: str,
    output_type: str,
    content: str = None,
    content_url: str = None,
    prompt_filename: str = None,
    prompt_version: str = None,
    error: str = None,
) -> None:
    """
    Upsert a notebook output for a given meeting + output_type.
    output_type is one of: 'newsletter', 'audio_overview', 'video_explainer', 'infographic'.

    Side effect (V1-Repo-1 follow-up): on a successful save (error is
    None), best-effort auto-enqueue the row into the D-095 repository
    deposit gate at pending_owner_review. Failures are logged and
    swallowed; the seed script
    (parsers/scripts/enqueue_repository_candidates.py) is the fallback
    for any output that slipped past this hook.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO notebook_outputs (
            meeting_id, notebook_id, output_type,
            content, content_url, prompt_filename, prompt_version, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(meeting_id, output_type) DO UPDATE SET
            notebook_id = excluded.notebook_id,
            content = excluded.content,
            content_url = excluded.content_url,
            prompt_filename = excluded.prompt_filename,
            prompt_version = excluded.prompt_version,
            error = excluded.error,
            generated_at = CURRENT_TIMESTAMP
    """, (
        meeting_id, notebook_id, output_type,
        content, content_url, prompt_filename, prompt_version, error
    ))
    # Re-read the row to get its id for the auto-enqueue hook. The
    # upsert may have inserted OR updated; lastrowid is unreliable on
    # the UPDATE path. The (meeting_id, output_type) pair is unique so
    # the SELECT always resolves to one row.
    cursor.execute(
        "SELECT id FROM notebook_outputs WHERE meeting_id = ? AND output_type = ?",
        (meeting_id, output_type),
    )
    row = cursor.fetchone()
    conn.commit()
    conn.close()

    if row is not None and error is None:
        notebook_output_id = row["id"]
        # Lazy import to avoid the cycle (repository_gate imports
        # database at module load). Best-effort — the write above
        # already committed; a failed enqueue must not roll it back.
        try:
            try:
                from parsers import repository_gate as _repo_gate
            except ImportError:  # pragma: no cover — Flask sibling-import
                import repository_gate as _repo_gate  # type: ignore[no-redef]
            _repo_gate.auto_enqueue_from_notebook_output(notebook_output_id)
        except Exception as exc:  # pragma: no cover — defensive
            import logging
            logging.getLogger(__name__).warning(
                "auto_enqueue_from_notebook_output(%s) failed: %s",
                notebook_output_id, exc,
            )


# ─────────────────────────────────────────────────────────────────
# V1-RAG-3 indexed-meeting proxy check (F8 honest-empty discipline)
# ─────────────────────────────────────────────────────────────────

def is_meeting_rag_indexed(meeting_id: int) -> bool:
    """True iff the flagship local store has chunks for this meeting."""
    conn = get_connection()
    try:
        table_exists = conn.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type = 'table' AND name = 'local_retrieval_chunks'"""
        ).fetchone()
        if table_exists is None:
            return False
        row = conn.execute(
            """SELECT 1 FROM local_retrieval_chunks
               WHERE meeting_id = ? LIMIT 1""",
            (meeting_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────
# YouTube channel registry (per city)
# ─────────────────────────────────────────────────────────────────

def set_city_youtube_channel(city_name: str, channel_url: str = None,
                             channel_id: str = None,
                             state: str = None,
                             county: str = None) -> bool:
    """Set or update the YouTube channel for a city. Returns True if updated.

    For multi-state operation, pass `state` (and optionally `county`) to
    disambiguate cities that share a name across states (Maricopa AZ vs
    Maricopa CA, etc.). See DECISIONS.md § D-027.

    Match precedence:
      - state AND county provided → WHERE name=? AND county=? AND state=?
      - state only             → WHERE name=? AND state=?
      - neither provided       → WHERE name=?, but raises ValueError if the
                                 name matches more than one row across the DB
                                 (forcing the caller to disambiguate).
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        if state and county:
            cursor.execute(
                """
                UPDATE cities
                SET youtube_channel_url = ?,
                    youtube_channel_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE name = ? AND county = ? AND state = ?
                """,
                (channel_url, channel_id, city_name, county, state)
            )
        elif state:
            cursor.execute(
                """
                UPDATE cities
                SET youtube_channel_url = ?,
                    youtube_channel_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE name = ? AND state = ?
                """,
                (channel_url, channel_id, city_name, state)
            )
        else:
            n = cursor.execute(
                "SELECT COUNT(*) FROM cities WHERE name = ?", (city_name,)
            ).fetchone()[0]
            if n > 1:
                raise ValueError(
                    f"City name '{city_name}' is ambiguous (matches {n} rows "
                    f"across states/counties). Pass state= and county= to disambiguate."
                )
            cursor.execute(
                """
                UPDATE cities
                SET youtube_channel_url = ?,
                    youtube_channel_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE name = ?
                """,
                (channel_url, channel_id, city_name)
            )
        updated = cursor.rowcount > 0
        conn.commit()
        return updated
    finally:
        conn.close()


def get_city_youtube_channel(city_name: str,
                             state: str = None,
                             county: str = None) -> Optional[Dict]:
    """Get a city's YouTube channel info. Returns None if city not found.

    For multi-state operation, pass `state` (and optionally `county`) to read
    the correct row when the same name exists across states. If state/county
    are omitted and the name matches multiple rows, raises ValueError.
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        if state and county:
            cursor.execute(
                "SELECT name, youtube_channel_url, youtube_channel_id "
                "FROM cities WHERE name = ? AND county = ? AND state = ?",
                (city_name, county, state)
            )
        elif state:
            cursor.execute(
                "SELECT name, youtube_channel_url, youtube_channel_id "
                "FROM cities WHERE name = ? AND state = ?",
                (city_name, state)
            )
        else:
            n = cursor.execute(
                "SELECT COUNT(*) FROM cities WHERE name = ?", (city_name,)
            ).fetchone()[0]
            if n > 1:
                raise ValueError(
                    f"City name '{city_name}' is ambiguous (matches {n} rows "
                    f"across states/counties). Pass state= and county= to disambiguate."
                )
            cursor.execute(
                "SELECT name, youtube_channel_url, youtube_channel_id "
                "FROM cities WHERE name = ?",
                (city_name,)
            )
        row = cursor.fetchone()
        if not row:
            return None
        return {
            'city': row['name'],
            'channel_url': row['youtube_channel_url'],
            'channel_id': row['youtube_channel_id'],
        }
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────
# Live stream helpers (S-015 — the Guide)
# ─────────────────────────────────────────────────────────────────

def get_cities_with_youtube_channel() -> List[Dict]:
    """Every city that has a YouTube channel registered (for live detection).

    Returns [{city, state, county, channel_url, channel_id}, ...].
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT name, state, county, youtube_channel_url, youtube_channel_id "
            "FROM cities "
            "WHERE youtube_channel_url IS NOT NULL AND youtube_channel_url != '' "
            "ORDER BY state, name"
        )
        return [
            {
                'city': r['name'],
                'state': r['state'],
                'county': r['county'],
                'channel_url': r['youtube_channel_url'],
                'channel_id': r['youtube_channel_id'],
            }
            for r in cursor.fetchall()
        ]
    finally:
        conn.close()


def get_cities_with_meeting_on(date_str: str) -> set:
    """Set of (city_name, state) with a meeting scheduled on YYYY-MM-DD.

    The Guide's coarse calendar gate (S-015): only poll the YouTube API for
    channels whose city is actually meeting that day, so live-detection quota
    is spent on the handful meeting tonight rather than every registered city.
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT DISTINCT city_name, state FROM meetings WHERE meeting_date = ?",
            (date_str,),
        )
        return {(r['city_name'], r['state']) for r in cursor.fetchall()}
    finally:
        conn.close()


def get_scheduled_meetings_on(date_str: str) -> List[Dict]:
    """Meetings scheduled on YYYY-MM-DD with their start time — feeds the Guide's
    time-window gate (only poll a channel near its scheduled meeting time).

    Returns [{city_name, state, meeting_time}, ...]. meeting_time format varies
    per parser ('5:00 PM', '17:00', …); the detector parses it best-effort.
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT city_name, state, meeting_time FROM meetings WHERE meeting_date = ?",
            (date_str,),
        )
        return [
            {'city_name': r['city_name'], 'state': r['state'],
             'meeting_time': r['meeting_time']}
            for r in cursor.fetchall()
        ]
    finally:
        conn.close()


def upsert_live_stream(city_name: str, state: Optional[str], county: Optional[str],
                       channel_id: Optional[str], video_id: str, video_url: str,
                       title: Optional[str] = None, started_at: Optional[str] = None,
                       meeting_id: Optional[int] = None) -> None:
    """Record or refresh a detected live broadcast (is_live=1, bump last_seen_at)."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO live_streams
                (city_name, state, county, channel_id, video_id, video_url,
                 title, started_at, detected_at, last_seen_at, is_live, meeting_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1, ?)
            ON CONFLICT(city_name, state, video_id) DO UPDATE SET
                last_seen_at = CURRENT_TIMESTAMP,
                is_live = 1,
                title = COALESCE(excluded.title, live_streams.title),
                channel_id = COALESCE(excluded.channel_id, live_streams.channel_id),
                meeting_id = COALESCE(excluded.meeting_id, live_streams.meeting_id)
            """,
            (city_name, state, county, channel_id, video_id, video_url,
             title, started_at, meeting_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_city_live_streams_ended(city_name: str, state: Optional[str],
                                 keep_video_ids: List[str]) -> int:
    """Mark is_live=0 for a city's live rows whose video_id isn't in keep_video_ids.

    Called after each detection pass: any previously-live stream we no longer
    see has ended. Soft (no delete). Returns the count marked ended.
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        if keep_video_ids:
            placeholders = ",".join("?" for _ in keep_video_ids)
            cursor.execute(
                f"UPDATE live_streams SET is_live = 0 "
                f"WHERE city_name = ? AND state IS ? AND is_live = 1 "
                f"AND video_id NOT IN ({placeholders})",
                [city_name, state, *keep_video_ids],
            )
        else:
            cursor.execute(
                "UPDATE live_streams SET is_live = 0 "
                "WHERE city_name = ? AND state IS ? AND is_live = 1",
                [city_name, state],
            )
        n = cursor.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def get_live_streams() -> List[Dict]:
    """All currently-live streams (is_live=1), newest detection first."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT city_name, state, county, channel_id, video_id, video_url, "
            "title, started_at, detected_at, last_seen_at, meeting_id "
            "FROM live_streams WHERE is_live = 1 "
            "ORDER BY detected_at DESC"
        )
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────
# Phase H — Meeting-pattern health helpers
#
# Each row in pattern_health is one weekly reconciliation between a
# city's curated meeting_patterns[] projection and what the city's
# calendar scrape actually returned. Drift detection (H-4) walks the
# recent rows; the operator's calendar-health surface (H-7) renders
# them. The H-3 refresh job is the only writer in normal operation.
# ─────────────────────────────────────────────────────────────────

# Valid match_status values for pattern_health. Keep in sync with the
# table-creation comment in init_notebook_schema().
PATTERN_HEALTH_STATUSES = {
    'match',    # projection and scrape agree on the window
    'drift',    # material disagreement (expected dates missing)
    'partial',  # some matches, some misses
    'no_data',  # scrape failed or returned nothing
}


def record_pattern_health(
    city_name: str,
    state: Optional[str],
    pattern_id: str,
    window_start: str,
    window_end: str,
    expected_next: Optional[str],
    actually_scraped: Optional[str],
    match_status: str,
    drift_notes: Optional[str] = None,
) -> int:
    """INSERT a pattern_health reconciliation row. Returns the new row id.

    `expected_next` / `actually_scraped` are caller-serialized JSON strings
    (typically `json.dumps([...])`) — the table stores them as TEXT so the
    H-7 surface can render diffs without re-running projection.

    Raises ValueError on unknown match_status — defensive guard so a typo
    in H-3 doesn't silently poison the drift detector's bucket counts.
    """
    if match_status not in PATTERN_HEALTH_STATUSES:
        raise ValueError(
            f"unknown match_status {match_status!r}; "
            f"must be one of {sorted(PATTERN_HEALTH_STATUSES)}"
        )
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO pattern_health
                (city_name, state, pattern_id, window_start, window_end,
                 expected_next, actually_scraped, match_status, drift_notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (city_name, state, pattern_id, window_start, window_end,
             expected_next, actually_scraped, match_status, drift_notes),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def get_newly_drifted_patterns() -> List[Dict]:
    """Return pattern_health rows where the MOST RECENT row for a given
    (city_name, state, pattern_id) is `match_status='drift'` AND either
    (a) it's the first row ever recorded for that pattern, OR (b) the
    immediately-prior row had a non-drift status.

    The "newly" matters: H-4 fires Slack escalations for drift events
    via slack_notifier, but a pattern that's been drifting for weeks
    would otherwise escalate every weekly refresh. This helper returns
    only the transition-into-drift event, so the operator gets one
    escalation per real drift onset, not weekly duplicates.

    A pattern that recovers (drift → match) and re-drifts (match →
    drift) WILL escalate again, which is the correct behavior (the
    second drift is a fresh event the operator should see).
    """
    conn = get_connection()
    try:
        # Per-pattern most-recent row + the row before it (if any).
        # Uses two correlated subqueries — fine at our scale (handful of
        # cities × ~10 patterns each = ~30-100 rows considered).
        rows = conn.execute(
            """
            WITH ranked AS (
                SELECT
                    id,
                    city_name,
                    state,
                    pattern_id,
                    refreshed_at,
                    window_start,
                    window_end,
                    expected_next,
                    actually_scraped,
                    match_status,
                    drift_notes,
                    ROW_NUMBER() OVER (
                        PARTITION BY city_name, state, pattern_id
                        ORDER BY refreshed_at DESC, id DESC
                    ) AS rn
                FROM pattern_health
            ),
            current_drift AS (
                SELECT * FROM ranked WHERE rn = 1 AND match_status = 'drift'
            ),
            prior_status AS (
                SELECT
                    city_name,
                    state,
                    pattern_id,
                    match_status AS prior_status
                FROM ranked
                WHERE rn = 2
            )
            SELECT
                cd.id,
                cd.city_name,
                cd.state,
                cd.pattern_id,
                cd.refreshed_at,
                cd.window_start,
                cd.window_end,
                cd.expected_next,
                cd.actually_scraped,
                cd.match_status,
                cd.drift_notes,
                ps.prior_status
            FROM current_drift cd
            LEFT JOIN prior_status ps
              ON ps.city_name = cd.city_name
             AND (ps.state IS cd.state OR (ps.state IS NULL AND cd.state IS NULL))
             AND ps.pattern_id = cd.pattern_id
            WHERE ps.prior_status IS NULL OR ps.prior_status != 'drift'
            ORDER BY cd.refreshed_at DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_recent_pattern_health(
    city_name: Optional[str] = None,
    pattern_id: Optional[str] = None,
    limit: int = 20,
) -> List[Dict]:
    """Most-recent pattern_health rows, newest first.

    Filters optional: pass `city_name` to scope to one city, `pattern_id`
    to scope to one pattern (typically combined with city_name). Without
    filters, returns the global most-recent rows — the H-7 operator surface
    uses this to render the calendar-health overview.
    """
    conn = get_connection()
    try:
        clauses = []
        params: List = []
        if city_name is not None:
            clauses.append("city_name = ?")
            params.append(city_name)
        if pattern_id is not None:
            clauses.append("pattern_id = ?")
            params.append(pattern_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = conn.execute(
            f"SELECT id, city_name, state, pattern_id, refreshed_at, "
            f"       window_start, window_end, expected_next, "
            f"       actually_scraped, match_status, drift_notes "
            f"FROM pattern_health{where} "
            f"ORDER BY refreshed_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────
# Work order helpers — the defrag-style processing queue
# ─────────────────────────────────────────────────────────────────

# Valid work order states
WORK_ORDER_STATES = {
    'pending',           # ready to process
    'processing',        # worker has it now
    'awaiting_video',    # no video URL yet (scanner will retry)
    'awaiting_notebook', # no notebook_id yet
    'completed',         # all outputs generated
    'failed',            # retries exhausted
    'skipped_too_old',   # meeting older than the age limit
    'no_video_source',   # S-037 V0: city/meeting has no usable video source
                         # (e.g., Colorado City meetings not on YouTube and
                         # tocc.us publishes only minutes). Terminal — won't
                         # be re-picked by next_pending_work_order.
}

DIARIZATION_STATUSES = frozenset({
    'deferred',
    'running',
    'succeeded',
    'failed',
})


def update_meeting_diarization_status(
    meeting_id: int,
    status: str,
    detail: Optional[str] = None,
) -> bool:
    """Persist a meeting's diarization substatus on its work order.

    This state is deliberately orthogonal to ``work_orders.state``: sidecars
    and the publish-readiness gate may complete while diarization remains
    deferred for the owner-only roster-review workflow.  Returns whether the
    meeting has a work order to update.
    """
    if status not in DIARIZATION_STATUSES:
        raise ValueError(f"Invalid diarization status: {status}")

    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            UPDATE work_orders
            SET diarization_status = ?,
                diarization_detail = ?,
                diarization_updated_at = CURRENT_TIMESTAMP
            WHERE meeting_id = ?
            """,
            (status, detail, int(meeting_id)),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def enqueue_work_order(
    meeting_id: int,
    youtube_video_url: str = None,
    priority: int = 0,
    requested_outputs: str = None,
) -> int:
    """
    Create or refresh a work order for a meeting.
    If one exists in a terminal state (completed/failed/skipped), the existing
    row is left alone — we don't reprocess unless explicitly retried.
    Returns the work_order id.
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT id, state FROM work_orders WHERE meeting_id = ?", (meeting_id,))
    existing = cursor.fetchone()

    if existing:
        wo_id = existing['id']
        # Don't disturb terminal states; do refresh if pending/awaiting and we have new info
        if existing['state'] in ('completed', 'failed', 'skipped_too_old', 'no_video_source'):
            conn.close()
            return wo_id
        # Update fields that may have changed
        sets = []
        params = []
        if youtube_video_url:
            sets.append("youtube_video_url = ?")
            params.append(youtube_video_url)
        if requested_outputs:
            sets.append("requested_outputs = ?")
            params.append(requested_outputs)
        sets.append("priority = ?")
        params.append(priority)
        sets.append("updated_at = CURRENT_TIMESTAMP")
        params.append(wo_id)
        cursor.execute(f"UPDATE work_orders SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()
        conn.close()
        return wo_id

    # T-004: if the caller didn't pass a URL but the meeting already has one
    # from --apply (high-confidence auto-match), inherit it onto the new WO
    # plus the match metadata. High auto-promotes to 'pending'; medium /
    # needs_review create the WO in 'awaiting_video' with metadata so the
    # operator-terminal shows a [CONFIRM URL] button right away.
    inherited_url = youtube_video_url
    inherited_state = 'pending'
    inherited_confidence = None
    inherited_method = None
    if not youtube_video_url:
        m_row = cursor.execute(
            "SELECT video_url, video_url_match_confidence, video_url_match_method "
            "FROM meetings WHERE id = ?",
            (meeting_id,)
        ).fetchone()
        if m_row:
            inherited_confidence = m_row['video_url_match_confidence']
            inherited_method = m_row['video_url_match_method']
            if m_row['video_url'] and inherited_confidence == 'high':
                inherited_url = m_row['video_url']
                inherited_state = 'pending'
            elif m_row['video_url']:
                # Medium / needs_review — preserve match info but keep
                # awaiting_video so the operator confirms via UI.
                inherited_state = 'awaiting_video'

    # NB: this COALESCE fallback is the production-defining set of output
    # types for any WO enqueued without an explicit requested_outputs. It
    # MUST stay in sync with the schema column DEFAULT above (the schema
    # DEFAULT is dead code because every INSERT in this function sets the
    # column explicitly via COALESCE — but they're aligned for clarity).
    # tracked_claims added 2026-05-16 per T-012 production unblock; the
    # prompt is `claude_authored · awaits_james_review` per
    # `prompts/PROMPT_REVIEW_LEDGER.md`.
    # `quotes` added 2026-05-26 per Quotes Unification Refactor (see
    # 01_Project_Overview/REFACTOR_QUOTES_UNIFICATION.md).
    # Chunk 9 (2026-05-26): `council_quotes` + `member_quotes_topic`
    # retired from defaults. Their prompts stay on disk (prompts/council_quotes.md
    # + prompts/member_quotes_topic.md) for historical reference and
    # manual per-WO use; new WOs only call the unified `quotes` prompt.
    # Existing WOs keep their old requested_outputs string intact.
    #
    # Session-20 Phase 1 (2026-07-01) — REMOVED from default: `audio_overview`,
    # `video_explainer`, `infographic` (studio media, per D-126 "V1 ships
    # without AI-generated studio media"; the D-126 intent was never enforced
    # at this layer until now — 2650 of 2869 audio-including WOs were created
    # post-D-126 because this default kept including them); `member_attendance`,
    # `quotes`, `episode_tags` (`text` strategy → NotebookLM; pending
    # V1-RAG-4 migration per S-109). All 6 removals are because the strategy
    # routes through NotebookLM, which is the incomplete-migration surface
    # session-20's ultracode audit surfaced. See
    # `01_Project_Overview/NOTEBOOKLM_REMOVAL_AUDIT_2026-07-01.md` +
    # `01_Project_Overview/V1_RAG_4_MIGRATION_PLAN_2026-07-01.md`.
    # Existing WOs keep their pre-Phase-1 requested_outputs string intact —
    # this change only affects NEW WOs enqueued without an explicit list.
    #
    # Session-32 (2026-07-04) — ADDED `quote_extraction` to the default.
    # The Phase 1 removal above dropped `quotes` (legacy NotebookLM-era
    # name) but never added its V1-RAG-3 replacement `quote_extraction`.
    # Result: every WO enqueued between 2026-07-01 and 2026-07-04 skipped
    # quote extraction entirely at the WO layer — the sidecar_pipeline.py
    # separately ran extraction into `.preview/m<id>.json` but nothing
    # imported those to the `quotes` table until this session's
    # `import_sidecar_quotes.py` backfill. `quote_extraction` in the WO
    # requested_outputs list means `_fetch_qdrant_extract_quotes`
    # (fetcher.py:274) fires natively during worker processing and
    # populates the DB directly — no sidecar import step needed for
    # future WOs.
    cursor.execute(
        """
        INSERT INTO work_orders
            (meeting_id, state, priority, youtube_video_url, requested_outputs,
             video_url_match_confidence, video_url_match_method)
        VALUES (?, ?, ?, ?, COALESCE(?, 'episode_tagline,synopsis,newsletter,key_decisions,community_calls_to_action,whats_next,council_sentiment,suggested_questions,transcript_words,tracked_claims,quote_extraction'), ?, ?)
        """,
        (meeting_id, inherited_state, priority, inherited_url, requested_outputs,
         inherited_confidence, inherited_method)
    )
    wo_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return wo_id


_CLEAR_ERROR = object()  # Sentinel meaning "clear the last_error field"


def update_work_order_requested_outputs(
    work_order_id: int,
    requested_outputs: str,
) -> bool:
    """Overwrite a WO's requested_outputs list. Session-32 (2026-07-04)
    added so the worker can self-heal a stale D-143-era requested_outputs
    on retry (e.g., inject `quote_extraction` when the WO's list was
    frozen against the pre-D-143 name `quotes`).

    Returns True if a row was updated. Doesn't touch state / retry count
    / any other WO field — callers pair this with update_work_order_state
    for the state transition.
    """
    conn = get_connection()
    try:
        cur = conn.execute(
            """
            UPDATE work_orders
            SET requested_outputs = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (requested_outputs, work_order_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def update_work_order_state(
    work_order_id: int,
    state: str,
    error=None,
    notebook_id: str = None,
    youtube_video_url: str = None,
    increment_retry: bool = False,
) -> None:
    """
    Update a work order's state, optionally setting error/notebook info.

    `error` semantics:
        - None (default): leave existing last_error as-is
        - str:            set last_error to this value
        - _CLEAR_ERROR or "": clear last_error to NULL

    On transitions to terminal success states (completed), last_error is
    automatically cleared even if no explicit error= argument was passed.
    """
    if state not in WORK_ORDER_STATES:
        raise ValueError(f"Invalid state: {state}")

    conn = get_connection()
    cursor = conn.cursor()

    sets = ["state = ?", "updated_at = CURRENT_TIMESTAMP"]
    params: list = [state]

    if state == 'processing':
        sets.append("started_at = CURRENT_TIMESTAMP")
    if state in ('completed', 'failed', 'skipped_too_old', 'no_video_source'):
        sets.append("completed_at = CURRENT_TIMESTAMP")

    # Auto-clear stale error on success transitions
    auto_clear_error = state == 'completed' and error is None

    if error is _CLEAR_ERROR or error == "" or auto_clear_error:
        sets.append("last_error = NULL")
    elif error is not None:
        sets.append("last_error = ?")
        params.append(error)

    if notebook_id is not None:
        sets.append("notebook_id = ?")
        params.append(notebook_id)
    if youtube_video_url is not None:
        sets.append("youtube_video_url = ?")
        params.append(youtube_video_url)
    if increment_retry:
        sets.append("retry_count = retry_count + 1")

    params.append(work_order_id)
    cursor.execute(f"UPDATE work_orders SET {', '.join(sets)} WHERE id = ?", params)
    conn.commit()
    conn.close()


def get_work_order(work_order_id: int) -> Optional[Dict]:
    """Get a single work order joined with its meeting metadata."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT
            w.*,
            m.meeting_title, m.meeting_date, m.city_name, m.county,
            m.meeting_id AS meeting_external_id,
            m.video_url AS meeting_video_url, m.agenda_url
        FROM work_orders w
        JOIN meetings m ON m.id = w.meeting_id
        WHERE w.id = ?
        """,
        (work_order_id,)
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def bump_eligible_failed_to_pending(base_backoff_minutes: int = 5) -> List[Dict]:
    """
    S-009-sibling auto-retry bumper (added 2026-06-19 per brainstorm-audit F1).

    Find work orders in state='failed' that still have retry_count<max_retries
    AND whose last failure happened at least an exponential-backoff window ago,
    and flip them back to state='pending' so next_pending_work_order picks them
    up on the daemon's next iteration. Without this bumper, failed WOs sit
    forever — the daemon's main selector only sees state='pending', and there
    was no automatic failed→pending path (only the manual /retry endpoint).

    Backoff progression:
      retry_count=0 → base_backoff_minutes after updated_at  (5 min default)
      retry_count=1 → base_backoff_minutes * 2                (10 min)
      retry_count=2 → base_backoff_minutes * 4                (20 min)
    The doubling slows retries on persistently-failing WOs without giving up.

    Returns the list of rows that were bumped (for the worker's log lines).

    DOES NOT touch failed WOs at retry_count>=max_retries — those are
    terminal-failed and require operator action (manual /retry resets
    retry_count too).
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, meeting_id, retry_count, max_retries, updated_at
            FROM work_orders
            WHERE state = 'failed'
              AND retry_count < max_retries
              AND (
                (retry_count = 0 AND updated_at < datetime('now', ?))
                OR (retry_count = 1 AND updated_at < datetime('now', ?))
                OR (retry_count = 2 AND updated_at < datetime('now', ?))
              )
            """,
            (
                f"-{int(base_backoff_minutes)} minutes",
                f"-{int(base_backoff_minutes * 2)} minutes",
                f"-{int(base_backoff_minutes * 4)} minutes",
            ),
        )
        eligible = [dict(r) for r in cursor.fetchall()]
        if not eligible:
            return []
        ids = [e["id"] for e in eligible]
        placeholders = ",".join(["?"] * len(ids))
        cursor.execute(
            f"""
            UPDATE work_orders
            SET state = 'pending',
                updated_at = CURRENT_TIMESTAMP,
                next_attempt_at = NULL
            WHERE id IN ({placeholders})
            """,
            ids,
        )
        conn.commit()
        return eligible
    finally:
        conn.close()


def next_pending_work_order() -> Optional[Dict]:
    """
    Pull the next work order to process.
    Order: pending state, highest priority first, oldest first within same priority.
    Skips items whose next_attempt_at is in the future (backoff).
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT
            w.*,
            m.meeting_title, m.meeting_date, m.city_name, m.county,
            m.meeting_id AS meeting_external_id,
            m.video_url AS meeting_video_url, m.agenda_url
        FROM work_orders w
        JOIN meetings m ON m.id = w.meeting_id
        WHERE w.state = 'pending'
          AND (w.next_attempt_at IS NULL OR w.next_attempt_at <= CURRENT_TIMESTAMP)
        ORDER BY w.priority DESC, w.created_at ASC
        LIMIT 1
        """
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def list_work_orders(state: str = None, city: str = None, limit: int = 200) -> List[Dict]:
    """List work orders with optional state/city filters."""
    conn = get_connection()
    cursor = conn.cursor()

    where = []
    params: list = []
    if state:
        where.append("w.state = ?")
        params.append(state)
    if city:
        where.append("m.city_name = ?")
        params.append(city)

    where_clause = "WHERE " + " AND ".join(where) if where else ""
    params.append(limit)

    cursor.execute(
        f"""
        SELECT
            w.*,
            m.meeting_title, m.meeting_date, m.city_name, m.county,
            m.video_url AS meeting_video_url, m.is_published
        FROM work_orders w
        JOIN meetings m ON m.id = w.meeting_id
        {where_clause}
        ORDER BY w.priority DESC, w.created_at DESC
        LIMIT ?
        """,
        params,
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def work_order_stats() -> Dict[str, int]:
    """Counts of work orders grouped by state."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT state, COUNT(*) AS n FROM work_orders GROUP BY state")
    counts = {state: 0 for state in WORK_ORDER_STATES}
    for row in cursor.fetchall():
        counts[row['state']] = row['n']
    counts['total'] = sum(counts.values())
    conn.close()
    return counts


# ── D-099 Phase 2 C4a refactor (2026-06-12) ──────────────────────────
# Three small wrappers moved out of bridge callers (worker.py +
# fetcher.py) so the Mac-side database_http_client (C4b) can mirror
# them via HTTP without forcing callers to know which backend is live.


def get_meeting_city(meeting_id: int) -> Optional[str]:
    """Return a meeting's city_name, or None. Moved out of
    zspan_pipeline/fetcher.py's _meeting_city for HTTP-shim parity."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT city_name FROM meetings WHERE id = ?", (meeting_id,)
        )
        row = cursor.fetchone()
    finally:
        conn.close()
    return row["city_name"] if row else None


def get_resolved_video_url(meeting_id: int) -> Optional[str]:
    """Return COALESCE(work_orders.youtube_video_url, meetings.video_url)
    for a meeting, or None. Moved out of zspan_pipeline/fetcher.py's
    _resolve_video_url for HTTP-shim parity."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COALESCE(wo.youtube_video_url, m.video_url) AS url
            FROM meetings m
            LEFT JOIN work_orders wo ON wo.meeting_id = m.id
            WHERE m.id = ?
            """,
            (meeting_id,),
        )
        row = cursor.fetchone()
    finally:
        conn.close()
    return row["url"] if row else None


def recover_stale_work_orders(hours: float = 2.0) -> List[Dict]:
    """Reset work orders stuck in 'processing' for >hours back to 'pending'.

    Returns a list of {id, meeting_id, hours_stale} for each recovered
    WO so the caller can log. Moved out of zspan_pipeline/worker.py's
    _recover_stale_work_orders for HTTP-shim parity. Likely victims of
    auth-expired silent failures or worker crashes.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, started_at, meeting_id,
                   (julianday('now') - julianday(started_at)) * 24.0 AS hours_stale
            FROM work_orders
            WHERE state = 'processing'
              AND started_at IS NOT NULL
              AND (julianday('now') - julianday(started_at)) * 24.0 > ?
            """,
            (hours,),
        )
        stale = [dict(row) for row in cursor.fetchall()]
        if not stale:
            return []
        recovery_msg = f"recovered from stuck 'processing' state (>{hours:.1f}h since started_at)"
        for row in stale:
            cursor.execute(
                """
                UPDATE work_orders
                SET state = 'pending',
                    last_error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (recovery_msg, row["id"]),
            )
        conn.commit()
    finally:
        conn.close()
    return stale


# ── State resolution — the fix for the "everything defaults to Arizona"
#    bug class (NV cities surfacing under the Arizona tab, 2026-07-10).
#
# parser_index.json + the channel-tree roster + the scrape/receiver write
# paths all used to hardcode 'Arizona' for any roster city, on the (now
# false) premise that the registry is Arizona-only. The Nevada fan-out
# added NV cities, so those leaked under Arizona everywhere a roster is
# grouped by state. This resolver is the single authority every write/
# read path routes through instead of hardcoding a state.
#
# Precedence: an explicit `state` on the entry wins (self-describing data);
# else the county→state gazetteer below (county names are unique across the
# states we cover — no AZ county collides with an NV county — so county
# resolves state deterministically); else the default with a LOUD warning,
# so a new state's counties can never silently land under Arizona again.
# When a new state joins the registry: add its counties here (and/or set an
# explicit `state` in the parser_index entries).
_COUNTY_TO_STATE = {
    # Arizona — all 15 counties
    'Apache': 'Arizona', 'Cochise': 'Arizona', 'Coconino': 'Arizona',
    'Gila': 'Arizona', 'Graham': 'Arizona', 'Greenlee': 'Arizona',
    'La Paz': 'Arizona', 'Maricopa': 'Arizona', 'Mohave': 'Arizona',
    'Navajo': 'Arizona', 'Pima': 'Arizona', 'Pinal': 'Arizona',
    'Santa Cruz': 'Arizona', 'Yavapai': 'Arizona', 'Yuma': 'Arizona',
    # Nevada — all 16 counties + the independent Carson City
    'Carson City': 'Nevada', 'Churchill': 'Nevada', 'Clark': 'Nevada',
    'Douglas': 'Nevada', 'Elko': 'Nevada', 'Esmeralda': 'Nevada',
    'Eureka': 'Nevada', 'Humboldt': 'Nevada', 'Lander': 'Nevada',
    'Lincoln': 'Nevada', 'Lyon': 'Nevada', 'Mineral': 'Nevada',
    'Nye': 'Nevada', 'Pershing': 'Nevada', 'Storey': 'Nevada',
    'Washoe': 'Nevada', 'White Pine': 'Nevada',
}


def resolve_city_state(
    entry: Optional[Dict[str, Any]],
    county: Optional[str],
    *,
    default: str = 'Arizona',
) -> str:
    """The single authority for a roster/scrape city's state.

    explicit entry['state'] → county gazetteer → default (with a warning).
    Tolerates a trailing ' County' on the county name. `entry` may be None
    when the caller only has a county (the scrape/receiver paths)."""
    if isinstance(entry, dict) and (entry.get('state') or '').strip():
        return entry['state'].strip()
    c = (county or '').strip()
    if c.endswith(' County'):
        c = c[:-len(' County')]
    if c in _COUNTY_TO_STATE:
        return _COUNTY_TO_STATE[c]
    if c:
        print(
            f"⚠️  resolve_city_state: county {county!r} is not in the "
            f"county→state gazetteer and the entry carries no explicit "
            f"'state' — defaulting to {default!r}. Add the county to "
            f"_COUNTY_TO_STATE (database.py) or set 'state' in the entry "
            f"so this city lands under the right state."
        )
    return default


def populate_cities_from_index():
    """Populate the cities table from parser_index.json.

    On a registry-sealed instance (public clone — recipes and
    parser_index.json distribute sealed per the registry policy), fall
    back to the public coverage_index.json so the city roster still
    seeds; recipe-grade fields stay empty there by design (D-153 § 8 /
    F8 honesty — same family as the /scrape routing-unavailable status).
    """
    index_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'parser_index.json')
    if not os.path.exists(index_path):
        return _populate_cities_from_coverage_index()
    with open(index_path, 'r') as f:
        index = json.load(f)

    conn = get_connection()
    cursor = conn.cursor()

    for city_name, info in index.items():
        # ON CONFLICT target lists all three columns because the unique
        # constraint is (name, county, state) (D-027 / D-029 multi-state
        # schema). State is resolved per-city — NOT hardcoded 'Arizona' —
        # so the Nevada roster cities land under Nevada (the 2026-07-10
        # NV-under-Arizona fix; see resolve_city_state).
        state = resolve_city_state(info, info.get('county'))
        cursor.execute("""
            INSERT INTO cities (name, county, state, calendar_url, parser_file, calendar_format, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name, county, state) DO UPDATE SET
                calendar_url=excluded.calendar_url,
                parser_file=excluded.parser_file,
                calendar_format=excluded.calendar_format,
                status=excluded.status,
                updated_at=CURRENT_TIMESTAMP
        """, (
            city_name,
            info.get('county', 'Unknown'),
            state,
            info.get('calendar_url', ''),
            info.get('parser_file', ''),
            info.get('calendar_format', ''),
            'active' if info.get('status') == 'success' else 'inactive'
        ))
    
    conn.commit()
    count = cursor.execute("SELECT COUNT(*) FROM cities").fetchone()[0]
    conn.close()
    print(f"Populated {count} cities from parser index")
    return count


# Public coverage_index.json uses lowercase state codes; the cities
# table stores full names (the flagship seeder writes 'Arizona' etc.).
_COVERAGE_STATE_NAMES = {
    'az': 'Arizona',
    'nv': 'Nevada',
    'ut': 'Utah',
    'va': 'Virginia',
}


def _populate_cities_from_coverage_index():
    """Seed the city roster from the public coverage_index.json on a
    registry-sealed instance. Recipe-grade fields (calendar_url,
    parser_file, calendar_format) stay empty — they are sealed; the
    channel browser gets its city list and everything else renders as
    honest scaffold."""
    coverage_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'coverage_index.json'
    )
    if not os.path.exists(coverage_path):
        print("No parser_index.json or coverage_index.json; skipping city seed.")
        return 0
    with open(coverage_path, 'r') as f:
        coverage = json.load(f)

    conn = get_connection()
    cursor = conn.cursor()
    skipped = 0
    for row in coverage.get('cities', []):
        state_code = row.get('state')
        state = _COVERAGE_STATE_NAMES.get(state_code or '', None)
        if not state:
            skipped += 1  # rows without a resolvable state can't join the
            continue      # (name, county, state) uniqueness scheme honestly
        cursor.execute("""
            INSERT INTO cities (name, county, state, calendar_url, parser_file, calendar_format, status)
            VALUES (?, ?, ?, '', '', '', ?)
            ON CONFLICT(name, county, state) DO UPDATE SET
                status=excluded.status,
                updated_at=CURRENT_TIMESTAMP
            WHERE cities.status IS NULL OR TRIM(cities.status) = ''
        """, (
            row.get('city'),
            row.get('county', 'Unknown'),
            state,
            'active' if row.get('coverage') == 'live' else 'inactive',
        ))
    conn.commit()
    count = cursor.execute("SELECT COUNT(*) FROM cities").fetchone()[0]
    conn.close()
    print(
        f"Registry sealed — seeded {count} cities from coverage_index.json"
        + (f" ({skipped} rows without a resolvable state skipped)" if skipped else "")
    )
    return count


def city_intelligence_path(city_name: str) -> Optional[str]:
    """Resolve the canonical filesystem path for a city's
    `city_intelligence/<slug>.json` file, or None if no matching file
    exists. Handles both naming conventions historically used:
      - hyphen-separated  (`lake-havasu-city.json`)
      - underscore-separated  (`lake_havasu_city.json`)
    Tries hyphen first since the original slugifier emits hyphens; falls
    back to underscore form so cities saved under the older convention
    still resolve. Returns the actual path that exists; callers can use
    it for both read and write.
    """
    if not city_name:
        return None
    base = (
        city_name.lower()
        .replace("&", "and")
        .replace("  ", " ")
        .strip()
    )
    intel_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', 'city_intelligence'
    )
    candidates = [
        base.replace(" ", "-"),  # hyphen form (canonical going forward)
        base.replace(" ", "_"),  # underscore form (legacy)
    ]
    for slug in candidates:
        path = os.path.join(intel_dir, f"{slug}.json")
        if os.path.isfile(path):
            return path
    return None


def load_city_intelligence(city_name: str) -> Optional[Dict]:
    """Load `city_intelligence/<slug>.json` for a city. Returns None if
    the file is missing or malformed (caller falls back gracefully).
    Slug resolution handled by `city_intelligence_path`."""
    path = city_intelligence_path(city_name)
    if not path:
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def append_whisper_vocabulary_hint(
    city_name: str,
    term: str,
    category: Optional[str] = None,
    first_seen: Optional[str] = None,
    source: Optional[str] = None,
    promoted_by: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """T-018: append a vocabulary entry to a city's `whisper_vocabulary_hints`
    array in `city_intelligence/<slug>.json`.

    Idempotent: if `term` (case-insensitive) is already present in the
    array — either as a bare string OR as an object with matching
    `term` field — the file is NOT rewritten and the existing entry's
    metadata stays as-is. Returns the entry that was added OR the
    existing matching entry, or None if the file doesn't exist.

    Atomic write: temp file + `os.replace` so an interrupted operation
    can't leave the canonical JSON corrupt.
    """
    path = city_intelligence_path(city_name)
    if not path:
        return None
    term = (term or "").strip()
    if not term:
        raise ValueError("term must be non-empty")

    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"could not load {path}: {e}")

    hints = data.get("whisper_vocabulary_hints")
    if not isinstance(hints, list):
        hints = []

    # Idempotency check — exact case-insensitive match against either
    # the bare string entry or the object's `term` field.
    term_lc = term.lower()
    for existing in hints:
        if isinstance(existing, str) and existing.strip().lower() == term_lc:
            return {"term": existing, "_already_present": True}
        if isinstance(existing, dict):
            t = (existing.get("term") or "").strip().lower()
            if t == term_lc:
                return {**existing, "_already_present": True}

    from datetime import datetime, timezone
    new_entry: Dict[str, Any] = {
        "term": term,
        "promoted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if category:
        new_entry["category"] = category
    if first_seen:
        new_entry["first_seen"] = first_seen
    if source:
        new_entry["source"] = source
    if promoted_by:
        new_entry["promoted_by"] = promoted_by

    hints.append(new_entry)
    data["whisper_vocabulary_hints"] = hints

    # Atomic write: temp file in same dir, then replace.
    import tempfile
    dir_name = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(
        suffix=".json", prefix=".tmp_vocab_", dir=dir_name
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")  # trailing newline (POSIX convention)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return {**new_entry, "_already_present": False}


def remove_whisper_vocabulary_hint(
    city_name: str,
    term: str,
) -> Optional[Dict[str, Any]]:
    """T-018 soft-revert: remove a vocabulary entry from the city JSON.
    Case-insensitive match against `term` (string entry) or `.term` (object).
    Returns the removed entry, or None if not found / file missing.
    Used when the operator decides a promoted correction was actually wrong.
    Same atomic-write semantics as `append_whisper_vocabulary_hint`.
    """
    path = city_intelligence_path(city_name)
    if not path:
        return None
    term_lc = (term or "").strip().lower()
    if not term_lc:
        raise ValueError("term must be non-empty")

    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"could not load {path}: {e}")

    hints = data.get("whisper_vocabulary_hints")
    if not isinstance(hints, list):
        return None

    removed = None
    new_hints = []
    for existing in hints:
        if isinstance(existing, str) and existing.strip().lower() == term_lc:
            removed = {"term": existing}
            continue
        if isinstance(existing, dict):
            t = (existing.get("term") or "").strip().lower()
            if t == term_lc:
                removed = dict(existing)
                continue
        new_hints.append(existing)

    if removed is None:
        return None

    data["whisper_vocabulary_hints"] = new_hints

    import tempfile
    dir_name = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(
        suffix=".json", prefix=".tmp_vocab_", dir=dir_name
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return removed


# Title prefixes NotebookLM sometimes prepends when echoing council
# members' names (especially in roll-call contexts). We try the name
# as-given first; if that misses, we strip these prefixes one at a time
# and retry. List in order from longest to shortest so "Vice Mayor"
# matches before "Mayor".
_NAME_TITLE_PREFIXES = (
    "vice mayor ",
    "councilmember ",
    "council member ",
    "councilwoman ",
    "councilman ",
    "councilor ",
    "counselor ",  # NotebookLM variant of "councilor" (caught 2026-06-05 in motions extraction)
    "honorable ",
    "mayor ",
    "hon. ",
    "hon ",
    "dr. ",
    "dr ",
    "mr. ",
    "mrs. ",
    "ms. ",
)


def _normalize_member_name_candidates(name: str) -> List[str]:
    """Generate name variants to try against the canonical roster.

    Order: the name as-given (lowercased), then title-stripped variants.
    The caller iterates until a candidate matches a `council_members` row.
    """
    base = (name or "").strip()
    if not base:
        return []
    base_lower = base.lower()
    candidates = [base_lower]
    for prefix in _NAME_TITLE_PREFIXES:
        if base_lower.startswith(prefix):
            stripped = base_lower[len(prefix):].strip()
            if stripped and stripped not in candidates:
                candidates.append(stripped)
    return candidates


def find_council_member_id(city_name: str, name: str) -> Optional[int]:
    """Resolve a canonical member name to its council_members.id.

    Multi-step match (per real-world NotebookLM output drift):
      1. Exact case-insensitive match.
      2. If miss, strip common title prefixes ("Mayor ", "Vice Mayor ",
         "Council Member ", "Councilmember ", "Councilor ", honorifics,
         etc.) and retry. Per 2026-05-16: NotebookLM sometimes prepends
         titles to mayoral names in roll-call attendance output even
         though the persona preamble lists the bare canonical name.

    Returns None when no candidate matches — caller should log + skip.
    """
    if not city_name or not name:
        return None
    conn = get_connection()
    cursor = conn.cursor()
    try:
        return _lookup_member_id_via_cursor(cursor, city_name, name)
    finally:
        conn.close()


def _lookup_member_id_via_cursor(cursor, city_name: str, name: str) -> Optional[int]:
    """Cursor-bound version of find_council_member_id (so the batch helpers
    don't open a fresh connection per row).

    Matching order:
      1. Exact case-insensitive match against the as-given name + each
         title-stripped variant.
      2. **Last-name fallback (2026-06-05):** if step 1 finds nothing,
         try matching the stripped form as a last-name suffix against
         the city's roster (`LOWER(name) LIKE '% <stripped>'`). Only
         matches when EXACTLY ONE roster member matches the suffix —
         ambiguous matches return None to preserve the precision-over-
         recall stance other batch helpers depend on. Added when motions
         extraction surfaced NotebookLM returning role-prefixed +
         last-name-only forms like 'Mayor Watkins' that prefix-stripping
         alone couldn't resolve (canonical is 'Ken Watkins').
    """
    candidates = _normalize_member_name_candidates(name)
    # Pass 1 — exact match against any candidate
    for cand in candidates:
        cursor.execute(
            """SELECT id FROM council_members
               WHERE city_name = ? AND LOWER(name) = ?
               LIMIT 1""",
            (city_name, cand),
        )
        row = cursor.fetchone()
        if row:
            return row['id']

    # Pass 2 — last-name suffix fallback. Only fires when pass-1 misses
    # entirely. Matches when EXACTLY ONE roster member's full name ends
    # with the candidate (preceded by a space). Ambiguous matches return
    # None to preserve precision-over-recall. Added 2026-06-05 after
    # motions extraction surfaced 'Mayor Watkins' (canonical 'Ken Watkins')
    # being dropped — prefix-stripping reduces to 'watkins', then this
    # pass resolves the single roster suffix match.
    for cand in candidates:
        # Skip candidates that are clearly full names (contain a space) —
        # those already had their exact-match shot in pass 1, no need
        # to also try suffix matching multi-word strings (which would
        # match accidentally on partial overlaps).
        if " " in cand:
            continue
        if len(cand) < 3:
            # Avoid runaway matches on tiny strings (e.g., "Jr").
            continue
        cursor.execute(
            """SELECT id FROM council_members
               WHERE city_name = ? AND LOWER(name) LIKE ?
               LIMIT 2""",
            (city_name, f"% {cand}"),
        )
        rows = cursor.fetchall()
        if len(rows) == 1:
            return rows[0]['id']
        # If 0 or 2+, fall through to the next candidate / give up.

    return None


def save_member_attendance_batch(
    meeting_id: int, city_name: str, items: List[Dict]
) -> Dict[str, int]:
    """UPSERT each attendance row into member_attendance.

    `items` is the JSON `attendance` array from the NotebookLM output:
        [{name, status, notes?}, ...]

    Members not in the canonical roster are SKIPPED with a warning (the
    extraction is constrained to canonical names in the prompt; a name
    not in the roster signals a NotebookLM drift the operator should
    review on the broadcast). Returns counts so the caller can log them.
    """
    if not items:
        return {"saved": 0, "skipped_unknown_member": 0}

    conn = get_connection()
    cursor = conn.cursor()
    saved = 0
    skipped = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        name = (it.get('name') or '').strip()
        status = (it.get('status') or '').strip()
        notes = (it.get('notes') or None)
        if not name or not status:
            continue
        member_id = _lookup_member_id_via_cursor(cursor, city_name, name)
        if member_id is None:
            skipped += 1
            continue
        cursor.execute(
            """INSERT INTO member_attendance (member_id, meeting_id, status, notes, recorded_at)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(member_id, meeting_id) DO UPDATE SET
                   status = excluded.status,
                   notes = excluded.notes,
                   recorded_at = CURRENT_TIMESTAMP""",
            (member_id, meeting_id, status, notes),
        )
        saved += 1
    conn.commit()
    conn.close()
    return {"saved": saved, "skipped_unknown_member": skipped}


def save_member_quotes_batch(
    meeting_id: int, city_name: str, items: List[Dict]
) -> Dict[str, int]:
    """Replace this meeting's member_quotes rows with the fresh extraction.

    Unlike attendance (UPSERT per member), quotes are bulk-replaced per
    meeting — re-running the extraction can yield a different number of
    quotes per member, so we wipe the meeting's existing rows first and
    re-INSERT. Members not in the canonical roster get skipped.

    `items` is the JSON `quotes` array:
        [{speaker, quote_text, topic_tags[], minutes_page_ref?,
          approximate_timestamp_seconds?, context?}, ...]
    """
    if not items:
        return {"saved": 0, "skipped_unknown_member": 0}

    conn = get_connection()
    cursor = conn.cursor()

    # Clear existing rows for this meeting (so re-runs don't accumulate).
    cursor.execute("DELETE FROM member_quotes WHERE meeting_id = ?", (meeting_id,))

    saved = 0
    skipped = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        speaker = (it.get('speaker') or '').strip()
        quote_text = (it.get('quote_text') or '').strip()
        if not speaker or not quote_text:
            continue
        member_id = _lookup_member_id_via_cursor(cursor, city_name, speaker)
        if member_id is None:
            skipped += 1
            continue
        topic_tags = it.get('topic_tags')
        if isinstance(topic_tags, list):
            topic_tags_str = json.dumps(topic_tags)
        elif isinstance(topic_tags, str):
            topic_tags_str = topic_tags
        else:
            topic_tags_str = None
        cursor.execute(
            """INSERT INTO member_quotes (
                   member_id, meeting_id, quote_text, topic_tags,
                   minutes_page_ref, video_timestamp_seconds, verified_status,
                   extracted_at
               ) VALUES (?, ?, ?, ?, ?, ?, 'pending', CURRENT_TIMESTAMP)""",
            (
                member_id, meeting_id, quote_text, topic_tags_str,
                it.get('minutes_page_ref'),
                it.get('approximate_timestamp_seconds'),
            ),
        )
        saved += 1
    conn.commit()
    conn.close()
    return {"saved": saved, "skipped_unknown_member": skipped}


# ─────────────────────────────────────────────────────────────────
# Unified `quotes` table helpers (Quotes Unification Refactor, 2026-05-26)
#
# These supersede save_member_quotes_batch above. The old helper stays in
# place during the refactor's transition (Chunks 1-5 still wire to it);
# Chunk 9 retires it.
#
# Architectural rationale: 01_Project_Overview/REFACTOR_QUOTES_UNIFICATION.md
# ─────────────────────────────────────────────────────────────────


def _compute_content_hash(speaker_name: str, quote_text: str) -> str:
    """Stable identity for a (speaker, quote) pair within a meeting.

    Computed as SHA256-hex of `normalize(speaker_name) || '|' || normalize(quote_text)`
    where `normalize` lowercases the input, replaces every non-alphanumeric
    character with a single space, and collapses runs of whitespace. This
    means capitalization + punctuation differences hash IDENTICALLY: "the
    budget" and "The budget." produce the same hash.

    Used as the conflict key in `save_quotes_batch`'s INSERT...ON CONFLICT...
    DO UPDATE pattern — preserves verification state across re-extractions when
    NotebookLM produces the same quote again.

    Re-computed by `update_quote_verification` whenever quote_text is corrected
    by the T-013 V3 ingest pass, so subsequent re-extractions match the row's
    hash and UPDATE rather than orphaning it. The harder normalization here
    (D-054 follow-up 2026-05-26) ALSO covers the polished-becomes-canonical
    flow on DisputedQuotesPage: a polished form ("The Capitol Police has...")
    and its pre-polish verbatim ("the Capitol Police has...") hash identically,
    so re-extraction by NotebookLM (which produces verbatim) UPSERTs the
    polished row instead of creating a duplicate.

    Historic rows (pre-2026-05-26) used a weaker normalization (lowercase +
    strip only). `init_db()` runs a one-shot migration (gated by
    `PRAGMA user_version < 1`) that recomputes every existing hash on first
    startup after this code lands. The migration is idempotent — re-running
    is a no-op once user_version is bumped.
    """
    def _normalize(s: str) -> str:
        keep = "".join(c.lower() if c.isalnum() else " " for c in s)
        return " ".join(keep.split())
    normalized = f"{_normalize(speaker_name)}|{_normalize(quote_text)}"
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def save_quotes_batch(
    meeting_id: int,
    items: List[Dict[str, Any]],
    broadcast_hero_ordinals: Optional[List[str]] = None,
    city_name: Optional[str] = None,
) -> Dict[str, int]:
    """Idempotent UPSERT for the unified `quotes` table.

    Replaces `save_member_quotes_batch` + the `council_quotes` JSON-blob save
    pattern. Uses content_hash + UNIQUE(meeting_id, content_hash) as the stable
    identity for preservation across re-extractions:

    - On INSERT (new content_hash): row created with `verified_status='pending'`.
    - On CONFLICT (matching content_hash): UPDATE preserves verification state
      (`verified_status`, `verified_by`, `verified_at`, `gemini_correction_notes`,
      `quote_text_original`, `proof_clip_url`, `proof_clip_sha256`, `word_timings`)
      and overwrites extraction fields (speaker_name, speaker_role, speaker_class,
      quote_text, topic_tags, minutes_page_ref, context, video_timestamp_seconds,
      is_broadcast_hero).

    The V3-verdict-wipe bug from `save_member_quotes_batch` (DELETE then INSERT
    with `verified_status='pending'` for everything) is structurally prevented
    here — there's no DELETE step. Verification work done since the last
    extraction is preserved.

    Args:
        meeting_id: the meeting these quotes belong to.
        items: list of quote dicts from the unified extraction prompt. Each
            should have at minimum: `speaker_name`, `quote_text`. Optional:
            `speaker_role`, `speaker_class` (defaults to 'council_member'),
            `topic_tags` (list or string), `minutes_page_ref`, `context`,
            `approximate_timestamp_seconds`, `quote_ordinal_id` (matched
            against broadcast_hero_ordinals to set is_broadcast_hero).
        broadcast_hero_ordinals: ordinal IDs the prompt flagged as the 5-8
            broadcast-hero subset (e.g., ["Quote one", "Quote two", ...]).
            Rows whose quote_ordinal_id is in this set get is_broadcast_hero=1;
            others get 0. Hero status IS overwritten on every extraction so
            the prompt's latest curation wins.
        city_name: used for member_id lookup when speaker_class='council_member'.
            If lookup fails, the row is still saved with member_id=NULL so the
            operator can correct attribution later via a future UI affordance.

    Returns:
        Dict with counts: {saved, updated, skipped_invalid, member_lookup_misses}.
        saved = new INSERTs; updated = ON CONFLICT UPDATEs (verification preserved).
    """
    if not items:
        return {"saved": 0, "updated": 0, "skipped_invalid": 0, "member_lookup_misses": 0}

    hero_set = set(broadcast_hero_ordinals or [])

    conn = get_connection()
    cursor = conn.cursor()

    saved = 0
    updated = 0
    skipped = 0
    member_misses = 0

    try:
        for it in items:
            if not isinstance(it, dict):
                skipped += 1
                continue

            speaker_name = (it.get('speaker_name') or it.get('speaker') or '').strip()
            quote_text = (it.get('quote_text') or it.get('text') or '').strip()
            if not speaker_name or not quote_text:
                skipped += 1
                continue

            speaker_class = (it.get('speaker_class') or 'council_member').strip().lower()
            if speaker_class not in ('council_member', 'staff', 'external'):
                speaker_class = 'council_member'  # defensive fallback
            speaker_role = (it.get('speaker_role') or '').strip() or None

            member_id = None
            if speaker_class == 'council_member' and city_name:
                member_id = _lookup_member_id_via_cursor(cursor, city_name, speaker_name)
                if member_id is None:
                    member_misses += 1
                    # Still save the row with member_id=NULL; operator can fix later

            topic_tags = it.get('topic_tags')
            if isinstance(topic_tags, list):
                topic_tags_str = json.dumps(topic_tags)
            elif isinstance(topic_tags, str):
                topic_tags_str = topic_tags
            else:
                topic_tags_str = None

            content_hash = _compute_content_hash(speaker_name, quote_text)
            is_hero = 1 if it.get('quote_ordinal_id') in hero_set else 0

            existing = cursor.execute(
                "SELECT id FROM quotes WHERE meeting_id = ? AND content_hash = ?",
                (meeting_id, content_hash),
            ).fetchone()

            if existing:
                # UPDATE — overwrite extraction fields, preserve verification fields
                cursor.execute(
                    """
                    UPDATE quotes
                    SET member_id = COALESCE(?, member_id),
                        speaker_name = ?,
                        speaker_role = ?,
                        speaker_class = ?,
                        quote_text = ?,
                        topic_tags = ?,
                        minutes_page_ref = ?,
                        context = ?,
                        video_timestamp_seconds = COALESCE(?, video_timestamp_seconds),
                        is_broadcast_hero = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        member_id, speaker_name, speaker_role, speaker_class,
                        quote_text, topic_tags_str,
                        it.get('minutes_page_ref'), it.get('context'),
                        it.get('approximate_timestamp_seconds'),
                        is_hero,
                        existing['id'],
                    ),
                )
                updated += 1
            else:
                # INSERT — fresh row, verification fields default to 'pending'
                cursor.execute(
                    """
                    INSERT INTO quotes (
                        meeting_id, member_id, speaker_name, speaker_role, speaker_class,
                        quote_text, topic_tags, minutes_page_ref, context,
                        video_timestamp_seconds, is_broadcast_hero,
                        content_hash, extracted_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (
                        meeting_id, member_id, speaker_name, speaker_role, speaker_class,
                        quote_text, topic_tags_str,
                        it.get('minutes_page_ref'), it.get('context'),
                        it.get('approximate_timestamp_seconds'),
                        is_hero,
                        content_hash,
                    ),
                )
                saved += 1

        conn.commit()
    finally:
        conn.close()

    return {
        "saved": saved,
        "updated": updated,
        "skipped_invalid": skipped,
        "member_lookup_misses": member_misses,
    }


_VERIFIED_STATUS_VALUES = ('pending', 'verified', 'disputed', 'rejected')


def update_quote_verification(
    quote_id: int,
    verified_status: str,
    verified_by: Optional[str] = None,
    gemini_correction_notes: Optional[Any] = None,
    corrected_quote_text: Optional[str] = None,
) -> bool:
    """V3 ingest hook for the unified `quotes` table.

    Updates verification fields and (optionally) the quote text. When the text
    is corrected (Gemini surfaced a `"X" should be "Y"` substitution applied
    via the per-city vocabulary corrections dictionary), this also:
        1. Stamps the pre-correction value into `quote_text_original` for audit
        2. Recomputes `content_hash` so subsequent re-extractions match the
           corrected form (T-017 prompt priming makes NotebookLM produce the
           corrected form next time; the hash needs to match)
        3. NULLs `word_timings` so the alignment pipeline re-runs against the
           corrected text on its next pass

    `verified_status` must be one of: 'pending', 'verified', 'disputed', 'rejected'.

    `gemini_correction_notes` accepts a dict / list (serialized to JSON) or a
    pre-serialized string.

    Returns True if the row existed and was updated; False if no such quote_id.
    """
    if verified_status not in _VERIFIED_STATUS_VALUES:
        raise ValueError(
            f"verified_status must be one of {_VERIFIED_STATUS_VALUES}; got {verified_status!r}"
        )

    conn = get_connection()
    cursor = conn.cursor()

    try:
        row = cursor.execute(
            "SELECT id, speaker_name, quote_text FROM quotes WHERE id = ?",
            (quote_id,),
        ).fetchone()
        if not row:
            return False

        if isinstance(gemini_correction_notes, (dict, list)):
            notes_str = json.dumps(gemini_correction_notes)
        else:
            notes_str = gemini_correction_notes

        if corrected_quote_text and corrected_quote_text.strip() != row['quote_text']:
            # Text correction — preserve original, recompute hash, NULL word_timings,
            # and NULL both D-054 display caches (the polished form was computed
            # against the pre-correction text + the verdict-emphasis tokens
            # match the pre-correction rendered note; both need re-computing
            # on next read).
            new_text = corrected_quote_text.strip()
            new_hash = _compute_content_hash(row['speaker_name'], new_text)
            cursor.execute(
                """
                UPDATE quotes
                SET verified_status = ?,
                    verified_by = ?,
                    verified_at = CURRENT_TIMESTAMP,
                    gemini_correction_notes = ?,
                    quote_text = ?,
                    quote_text_original = COALESCE(quote_text_original, ?),
                    content_hash = ?,
                    word_timings = NULL,
                    quote_text_display = NULL,
                    verdict_emphasis_tokens = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    verified_status, verified_by, notes_str,
                    new_text, row['quote_text'], new_hash,
                    quote_id,
                ),
            )
        else:
            # No text change — just update verification fields
            cursor.execute(
                """
                UPDATE quotes
                SET verified_status = ?,
                    verified_by = ?,
                    verified_at = CURRENT_TIMESTAMP,
                    gemini_correction_notes = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (verified_status, verified_by, notes_str, quote_id),
            )

        conn.commit()
        return True
    finally:
        conn.close()


def get_quotes_for_meeting(
    meeting_id: int,
    broadcast_hero_only: bool = False,
    exclude_rejected: bool = True,
) -> List[Dict[str, Any]]:
    """Read helper for the unified `quotes` table.

    Powers BroadcastPage queries (with broadcast_hero_only=True) and operator-
    side full-meeting views (default broadcast_hero_only=False).

    `exclude_rejected` defaults to True so public-facing surfaces don't
    accidentally render quotes the verification chain flagged as wrong. Set
    False for operator surfaces (DisputedQuotesPage, audit views) that need to
    see rejected rows.

    Disputed quotes are returned by default; the existing T-013 V4 Cast API
    filter (`verified_status NOT IN ('rejected', 'disputed')`) is applied at
    the API layer for public surfaces. See `get_quotes_for_member` below for
    the Cast-page-shaped helper that excludes disputed too.

    Returns: list of dicts with JSON columns (topic_tags, word_timings,
    gemini_correction_notes) pre-parsed for caller convenience.
    """
    where = ["meeting_id = ?"]
    params: List[Any] = [meeting_id]
    if broadcast_hero_only:
        where.append("is_broadcast_hero = 1")
    if exclude_rejected:
        where.append("verified_status != 'rejected'")

    conn = get_connection()
    rows = conn.execute(
        f"""
        SELECT id, meeting_id, member_id, speaker_name, speaker_role, speaker_class,
               quote_text, quote_text_original, topic_tags, minutes_page_ref, context,
               is_broadcast_hero, video_timestamp_seconds, word_timings,
               verified_status, verified_by, verified_at, gemini_correction_notes,
               proof_clip_url, proof_clip_sha256, content_hash,
               extracted_at, updated_at
        FROM quotes
        WHERE {" AND ".join(where)}
        ORDER BY COALESCE(video_timestamp_seconds, 999999), id
        """,
        params,
    ).fetchall()
    conn.close()

    result = []
    for r in rows:
        d = dict(r)
        for json_field in ('topic_tags', 'word_timings', 'gemini_correction_notes'):
            if d.get(json_field):
                try:
                    d[json_field] = json.loads(d[json_field])
                except (json.JSONDecodeError, TypeError):
                    pass  # leave as string if not parseable
        # Derive video_timestamp_seconds from word_timings[0].start_ms when
        # the column is null — matches the existing Cast API pattern so the
        # `[▶ Watch at <m:ss>]` deep-link button renders for any aligned
        # quote, even if NotebookLM didn't return an approximate timestamp.
        if d.get('video_timestamp_seconds') is None and isinstance(d.get('word_timings'), list):
            wt = d['word_timings']
            if wt and isinstance(wt[0], dict) and 'start_ms' in wt[0]:
                try:
                    d['video_timestamp_seconds'] = int(wt[0]['start_ms']) // 1000
                except (TypeError, ValueError):
                    pass
        result.append(d)
    return result


def get_quotes_for_member(
    meeting_id: int,
    member_id: int,
    exclude_rejected: bool = True,
    exclude_disputed: bool = True,
) -> List[Dict[str, Any]]:
    """Read helper for Cast page per-member panels.

    Returns this member's quotes for a meeting, filtered to speaker_class
    'council_member' (a member's quotes shouldn't show up under a staff or
    external classification by mistake). Public-facing Cast page filters out
    both rejected and disputed quotes by default (matching the existing T-013 V4
    `verified_status NOT IN ('rejected', 'disputed')` rule for the Cast API).

    Returns: list of dicts with JSON columns (topic_tags, word_timings)
    pre-parsed for caller convenience.
    """
    where = [
        "meeting_id = ?",
        "member_id = ?",
        "speaker_class = 'council_member'",
    ]
    params: List[Any] = [meeting_id, member_id]
    if exclude_rejected:
        where.append("verified_status != 'rejected'")
    if exclude_disputed:
        where.append("verified_status != 'disputed'")

    conn = get_connection()
    rows = conn.execute(
        f"""
        SELECT id, meeting_id, member_id, speaker_name, speaker_role, speaker_class,
               quote_text, quote_text_original, topic_tags, minutes_page_ref, context,
               is_broadcast_hero, video_timestamp_seconds, word_timings,
               verified_status, verified_by, verified_at,
               proof_clip_url, proof_clip_sha256,
               extracted_at, updated_at
        FROM quotes
        WHERE {" AND ".join(where)}
        ORDER BY COALESCE(video_timestamp_seconds, 999999), id
        """,
        params,
    ).fetchall()
    conn.close()

    result = []
    for r in rows:
        d = dict(r)
        for json_field in ('topic_tags', 'word_timings'):
            if d.get(json_field):
                try:
                    d[json_field] = json.loads(d[json_field])
                except (json.JSONDecodeError, TypeError):
                    pass
        # Derive video_timestamp_seconds from word_timings[0] when null —
        # matches the existing Cast API derivation pattern.
        if d.get('video_timestamp_seconds') is None and isinstance(d.get('word_timings'), list):
            wt = d['word_timings']
            if wt and isinstance(wt[0], dict) and 'start_ms' in wt[0]:
                try:
                    d['video_timestamp_seconds'] = int(wt[0]['start_ms']) // 1000
                except (TypeError, ValueError):
                    pass
        result.append(d)
    return result


def seed_council_members_from_intelligence():
    """Seed council_members from city_intelligence/*.json files.

    Idempotent: uses ON CONFLICT(city_name, seat_id) DO UPDATE SET so re-runs
    overwrite name/role/term changes that were edited in the JSON. The seed
    is called from api_server.py startup so an operator can hand-edit a JSON
    file, restart Flask, and see the changes reflected in the Cast page.
    """
    intel_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', 'city_intelligence'
    )
    if not os.path.isdir(intel_dir):
        print(f"No city_intelligence directory at {intel_dir}; skipping seed.")
        return 0

    conn = get_connection()
    cursor = conn.cursor()
    count = 0
    file_count = 0

    for fname in sorted(os.listdir(intel_dir)):
        if not fname.endswith('.json'):
            continue
        path = os.path.join(intel_dir, fname)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"  WARNING: could not load {fname}: {e}")
            continue

        city_name = data.get('canonical_name')
        if not city_name:
            print(f"  WARNING: {fname} missing canonical_name; skipping.")
            continue

        for m in data.get('current_members', []):
            seat_id = m.get('seat_id')
            if not seat_id:
                continue
            cursor.execute("""
                INSERT INTO council_members
                    (city_name, name, role, seat_id, term_started, term_ends, source_url, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(city_name, seat_id) DO UPDATE SET
                    name=excluded.name,
                    role=excluded.role,
                    term_started=excluded.term_started,
                    term_ends=excluded.term_ends,
                    source_url=excluded.source_url,
                    updated_at=CURRENT_TIMESTAMP
            """, (
                city_name,
                m.get('name'),
                m.get('role'),
                seat_id,
                m.get('term_started'),
                m.get('term_ends'),
                m.get('source_url'),
            ))
            count += 1
        file_count += 1

    conn.commit()
    conn.close()
    print(f"Seeded {count} council members from {file_count} city_intelligence file(s)")
    return count


def cache_meetings(city_name: str, county: str, meetings: List[Dict], state: Optional[str] = None):
    """Cache meeting data for a city using UPSERT (D-038 · 2026-05-13).

    `state` defaults to None → resolved from the county via
    resolve_city_state (was a blind 'Arizona' default, which mislabeled
    every Nevada scrape; the /scrape endpoint calls this without a state,
    so the default is the actual write path — 2026-07-10 NV fix).

    Before D-038 this function did a wholesale `DELETE FROM meetings WHERE
    city_name=? AND state=?` then INSERTed the fresh scrape. Combined with
    `ON DELETE CASCADE` on work_orders + notebook_outputs + member_*
    foreign keys, that destroyed every dependent row on every cache miss
    — a single page-load past the 6h TTL wiped a full night of processing.

    The UPSERT pattern preserves meeting IDs across re-scrapes:
      - INSERT a new row if no matching (city_name, state, meeting_date,
        meeting_title) exists.
      - UPDATE in place if a match exists. Keeps the id stable; downstream
        FKs (work_orders.meeting_id, notebook_outputs.meeting_id, etc.)
        remain valid.
      - Meetings that *disappear* from the source between scrapes are NOT
        deleted — they linger as orphans. A separate prune step can clean
        them up when an operator wants to, but the default is "keep what
        you have" because partial-processing state is more valuable than
        strict cache freshness.

    Requires the UNIQUE natural-key index `idx_meetings_natural_key`
    created in `init_db()`.
    """
    state = state or resolve_city_state(None, county)

    # ── Front-door ingest gate ────────────────────────────────────────────
    # Validate the raw scrape BEFORE it becomes cache: drop fabricated / wall
    # rows (stub-parser "Sample Meeting" placeholders, JS-challenge shells) so
    # they never reach the pipeline that trusts this cache. Deterministic
    # Tier 1 always runs; a fully-rejected listing caches nothing — an honest
    # empty. See ingest_validator. A validator CRASH fails CLOSED (RR-8 /
    # SEC-INPUT-1): abort the write, preserve the previous cache — never
    # promote unvalidated rows into the trusted cache.
    try:
        from ingest_validator import validate_listing  # noqa: PLC0415
        _verdict = validate_listing(meetings, city_name)
        if _verdict.rejected_count:
            logger.warning(
                "ingest gate [%s/%s]: dropped %d of %d row(s) for %s — %s",
                _verdict.tier, _verdict.status, _verdict.rejected_count,
                len(meetings), city_name, _verdict.rejection_summary(),
            )
        elif _verdict.status == "uncertain":
            logger.warning(
                "ingest gate: uncertain for %s — %s (accepted-and-flagged; set "
                "ZSPAN_INGEST_LLM_CHECK=1 for the Tier-2 ruling)",
                city_name, _verdict.reason,
            )
        meetings = _verdict.accepted
    except Exception as _gate_err:  # RR-8 / SEC-INPUT-1: FAIL CLOSED
        # A validator CRASH (not a rejection) must not promote unvalidated
        # rows into the trusted cache. Abort the write, preserve whatever was
        # cached before, record the failure loudly. (Previously this fell
        # through and cached the unvalidated listing — a fail-open hole.)
        logger.error(
            "ingest gate ERRORED for %s (%s) — aborting cache write; "
            "previous cache preserved (fail-closed)", city_name, _gate_err,
            exc_info=True,
        )
        return

    conn = get_connection()
    cursor = conn.cursor()

    # Get city_id
    cursor.execute("SELECT id FROM cities WHERE name = ?", (city_name,))
    row = cursor.fetchone()
    if not row:
        # Auto-create city entry
        cursor.execute(
            "INSERT INTO cities (name, county, state) VALUES (?, ?, ?)",
            (city_name, county, state)
        )
        city_id = cursor.lastrowid
    else:
        city_id = row['id']

    # UPSERT each meeting. excluded.X refers to the values that would have
    # been inserted — i.e., the fresh scrape's payload — so the UPDATE
    # branch refreshes mutable fields without touching id/created_at/public_id.
    for meeting in meetings:
        for _ in range(_PUBLIC_ID_COLLISION_RETRIES):
            public_id = _generate_available_public_id(cursor)
            try:
                cursor.execute("""
                    INSERT INTO meetings (
                        public_id, city_id, city_name, county, state,
                        meeting_title, meeting_date, meeting_time, meeting_location,
                        meeting_status, agenda_url, minutes_url, video_url,
                        agenda_packet_url, ecomment_url, meeting_id, raw_data
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(city_name, state, meeting_date, meeting_title) DO UPDATE SET
                        city_id          = excluded.city_id,
                        county           = excluded.county,
                        meeting_time     = excluded.meeting_time,
                        meeting_location = excluded.meeting_location,
                        meeting_status   = excluded.meeting_status,
                        agenda_url       = excluded.agenda_url,
                        minutes_url      = excluded.minutes_url,
                        video_url        = CASE
                            WHEN excluded.video_url IS NOT NULL AND excluded.video_url != ''
                                THEN excluded.video_url
                            ELSE meetings.video_url
                        END,
                        agenda_packet_url = excluded.agenda_packet_url,
                        ecomment_url     = excluded.ecomment_url,
                        meeting_id       = excluded.meeting_id,
                        raw_data         = excluded.raw_data,
                        updated_at       = CURRENT_TIMESTAMP
                """, (
                    public_id, city_id, city_name, county, state,
                    meeting.get('meeting_title', ''),
                    meeting.get('meeting_date', ''),
                    meeting.get('meeting_time', ''),
                    meeting.get('meeting_location', meeting.get('location', '')),
                    meeting.get('meeting_status', ''),
                    meeting.get('agenda_url', ''),
                    meeting.get('minutes_url', ''),
                    meeting.get('video_url', ''),
                    meeting.get('agenda_packet_url', ''),
                    meeting.get('ecomment_url', ''),
                    meeting.get('meeting_id', ''),
                    json.dumps(meeting)
                ))
            except sqlite3.IntegrityError as exc:
                if "meetings.public_id" in str(exc):
                    continue
                raise
            break
        else:
            raise RuntimeError("Unable to insert meeting with a unique public_id")

    # Update city metadata
    cursor.execute("""
        UPDATE cities SET
            last_scraped = CURRENT_TIMESTAMP,
            scrape_success = 1,
            total_meetings = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE name = ? AND state = ?
    """, (len(meetings), city_name, state))

    conn.commit()
    conn.close()
    return len(meetings)


# ── Public-serving visibility gate (RR-8 / SEC-PERIMETER-1) ──────────────
# A meeting reaches the public ONLY when BOTH publish fields hold:
#   1. meetings.is_published = 1        (operator flipped it live), AND
#   2. its work order has approved_at   (human review actually landed).
# Serving on is_published alone under-gates — the July-4 force-publish
# (developer.md § 5e) proved is_published=1 can exist without approval.
# Every public serving door composes this fragment; owner / include_drafts
# callers skip it.

def public_serving_sql(alias: str = "m") -> str:
    """The ` AND ...` fragment constraining a meetings query to
    publicly-visible rows. `alias` is the meetings-table alias in the
    caller's query (a literal — no user input reaches this)."""
    return (
        f" AND {alias}.is_published = 1"
        f" AND EXISTS (SELECT 1 FROM work_orders w"
        f" WHERE w.meeting_id = {alias}.id AND w.approved_at IS NOT NULL)"
    )


def is_meeting_publicly_visible(meeting_id: int, conn=None) -> bool:
    """True iff a single meeting passes the two-field public-serving gate
    (is_published=1 AND an approved work order). The ID-route counterpart
    of public_serving_sql() — /api/notebook, /api/quotes, and the citation
    route call this before serving meeting-derived content to a non-owner.
    RR-8 / SEC-PERIMETER."""
    own = conn is None
    if own:
        conn = get_connection()
    try:
        row = conn.execute(
            """SELECT 1 FROM meetings m
                WHERE m.id = ? AND m.is_published = 1
                  AND EXISTS (SELECT 1 FROM work_orders w
                              WHERE w.meeting_id = m.id
                                AND w.approved_at IS NOT NULL)
                LIMIT 1""",
            (meeting_id,),
        ).fetchone()
        return row is not None
    finally:
        if own:
            conn.close()


def get_cached_meetings_with_meta(
    city_name: str,
    state: str = 'Arizona',
    include_drafts: bool = True,
) -> Optional[Dict]:
    """Get cached meetings for a city, regardless of freshness, plus metadata.

    `include_drafts` controls the Phase 3 public-visibility filter:
      - True (default — operator-facing, this fn is admin-style): all
        meetings, with is_published exposed on each.
      - False (public-facing): only `is_published=1` meetings.

    Note the default differs from `get_cached_meetings` because this fn
    is typically called by the operator-terminal / draft-aware surfaces.
    The public ChannelsPage path uses `get_cached_meetings` whose
    default is the safer "drafts hidden" mode.

    Returns None ONLY when there's no cached row at all (no `last_scraped`
    on `cities` row, or city doesn't exist). When cache exists, returns:

        {
            'meetings': [...],
            'last_scraped': '2026-05-13T01:23:45',
            'cache_age_seconds': 7245,
            'is_stale': True,    # cache_age_seconds > CACHE_TTL
        }

    Callers decide whether to use stale data or force a live re-scrape.
    Replaces the old `get_cached_meetings` behavior where stale → None
    forced an automatic re-scrape — that was the trigger for the
    D-039 "background activity invisible to operator" failure mode.
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT last_scraped FROM cities WHERE name = ? AND state = ?",
        (city_name, state),
    )
    row = cursor.fetchone()
    if not row or not row['last_scraped']:
        conn.close()
        return None

    # SQLite's CURRENT_TIMESTAMP is UTC; compare against utcnow() so the
    # delta is positive even when the local clock is offset from UTC.
    last_scraped_dt = datetime.fromisoformat(row['last_scraped'])
    age_seconds = max(0, (datetime.utcnow() - last_scraped_dt).total_seconds())

    # Pull all meetings for this city/state, JOIN-ing in the two short
    # per-episode outputs the frontend renders on the calendar cards:
    # `episode_tagline` (one-line hook) and `episode_tags` (TAG/CATEGORY
    # pairs). Each is pulled with a scalar subquery so a meeting without a
    # processed notebook just gets NULL — the frontend renders a placeholder
    # for those. Heavier outputs (synopsis, council_quotes, etc.) stay on
    # the BroadcastPage detail call to keep this query cheap at list scale.
    publish_filter = "" if include_drafts else public_serving_sql("m")
    visible_output_filter = "" if include_drafts else " AND no.voided_at IS NULL"
    cursor.execute(
        f"""SELECT
               m.*,
               (SELECT no.content
                 FROM notebook_outputs no
                 WHERE no.meeting_id = m.id
                   AND no.output_type = 'episode_tagline'
                   {visible_output_filter}
                 LIMIT 1) AS episode_tagline,
               (SELECT no.content
                  FROM notebook_outputs no
                 WHERE no.meeting_id = m.id
                   AND no.output_type = 'episode_tags'
                   {visible_output_filter}
                 LIMIT 1) AS episode_tags
           FROM meetings m
           WHERE m.city_name = ? AND m.state = ?{publish_filter}
           ORDER BY m.meeting_date DESC""",
        (city_name, state),
    )
    meetings = [dict(r) for r in cursor.fetchall()]
    conn.close()

    # Operator-identity fields never leave on catalog rows (operator
    # direction 2026-07-09 session-49: personal identity off every public
    # surface; `published_by` is parked as a future contributor-credit
    # placeholder — "contributed from" — not a functional display field).
    # The DB columns stay (internal audit trail per F-6.2); the owner-gated
    # citation endpoint remains the one sanctioned attribution view.
    for m in meetings:
        m.pop('published_by', None)
        m.pop('publish_notes', None)

    if not meetings and include_drafts:
        # No rows + include_drafts=True (no filter applied) → treat as
        # no-cache rather than stale-empty so the UI doesn't show
        # "stale 0 meetings" forever. With include_drafts=False, an
        # empty result is a legitimate state ("data exists but nothing
        # is published yet") and we return the empty list directly.
        return None

    return {
        'meetings': meetings,
        'last_scraped': row['last_scraped'],
        'cache_age_seconds': int(age_seconds),
        'is_stale': age_seconds > CACHE_TTL,
    }


def get_cached_meetings(
    city_name: str,
    state: str = 'Arizona',
    include_drafts: bool = False,
) -> Optional[List[Dict]]:
    """Get cached meetings for a city if cache is fresh.

    `include_drafts` controls the Phase 3 public-visibility filter:
      - False (default, public-facing): only `is_published=1` meetings.
      - True (operator-mode): all meetings, with is_published exposed.

    The publish columns are ALWAYS included in the output dict so the
    frontend can distinguish drafts from published when in operator mode
    + render the "Reviewed by X on [date]" badge on published rows.
    """
    conn = get_connection()
    cursor = conn.cursor()

    # Check if cache is fresh
    cursor.execute("""
        SELECT last_scraped FROM cities
        WHERE name = ? AND state = ?
    """, (city_name, state))
    row = cursor.fetchone()

    if not row or not row['last_scraped']:
        conn.close()
        return None

    # Check TTL (SQLite CURRENT_TIMESTAMP is UTC; compare against utcnow)
    last_scraped = datetime.fromisoformat(row['last_scraped'])
    if (datetime.utcnow() - last_scraped).total_seconds() > CACHE_TTL:
        conn.close()
        return None

    # Get cached meetings, with the per-episode tagline from notebook_outputs
    # joined in. The tagline drives the streaming-network sidebar one-liner —
    # falls back to NULL when the meeting hasn't been processed yet, and the
    # frontend renders a placeholder for those rows.
    publish_filter = "" if include_drafts else public_serving_sql("m")
    visible_output_filter = "" if include_drafts else " AND no.voided_at IS NULL"
    cursor.execute(f"""
        SELECT
            m.*,
            (SELECT no.content
              FROM notebook_outputs no
              WHERE no.meeting_id = m.id
                AND no.output_type = 'episode_tagline'
                {visible_output_filter}
              LIMIT 1) AS episode_tagline,
            (SELECT no.content
              FROM notebook_outputs no
              WHERE no.meeting_id = m.id
                AND no.output_type = 'episode_tags'
                {visible_output_filter}
              LIMIT 1) AS episode_tags
        FROM meetings m
        WHERE m.city_name = ? AND m.state = ?{publish_filter}
        ORDER BY m.meeting_date DESC
    """, (city_name, state))

    meetings = []
    for row in cursor.fetchall():
        meetings.append({
            'id': row['id'],  # exposes meeting_id to the frontend for /broadcast navigation
            'meeting_title': row['meeting_title'],
            'meeting_date': row['meeting_date'],
            'meeting_time': row['meeting_time'],
            'meeting_location': row['meeting_location'],
            'meeting_status': row['meeting_status'],
            'agenda_url': row['agenda_url'],
            'minutes_url': row['minutes_url'],
            'video_url': row['video_url'],
            'agenda_packet_url': row['agenda_packet_url'],
            'ecomment_url': row['ecomment_url'],
            'summary': row['summary'],
            'notebook_id': row['notebook_id'] if 'notebook_id' in row.keys() else None,
            'episode_tagline': row['episode_tagline'] if 'episode_tagline' in row.keys() else None,
            'episode_tags': row['episode_tags'] if 'episode_tags' in row.keys() else None,
            'is_published': bool(row['is_published']) if 'is_published' in row.keys() else False,
            'published_at': row['published_at'] if 'published_at' in row.keys() else None,
            # published_by deliberately not emitted (2026-07-09
            # identity-strip: operator identity stays off public catalog
            # rows; the timestamp + flag carry the publish state).
        })

    conn.close()
    return meetings


def search_meetings(query: str, county: str = None, state: str = None,
                    date_from: str = None, date_to: str = None,
                    limit: int = 100, offset: int = 0) -> Dict:
    """
    Full-text search across all cached meetings.
    Returns results with pagination info.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    conditions = []
    params = []
    
    if query:
        # Use FTS5 for text search
        conditions.append("m.id IN (SELECT rowid FROM meetings_fts WHERE meetings_fts MATCH ?)")
        # Escape special FTS characters and add prefix matching
        safe_query = query.replace('"', '""')
        params.append(f'"{safe_query}"*')
    
    if county:
        conditions.append("m.county = ?")
        params.append(county)
    
    if state:
        conditions.append("m.state = ?")
        params.append(state)
    
    if date_from:
        conditions.append("m.meeting_date >= ?")
        params.append(date_from)
    
    if date_to:
        conditions.append("m.meeting_date <= ?")
        params.append(date_to)
    
    where_clause = " AND ".join(conditions) if conditions else "1=1"
    # Public search never returns drafts or unapproved meetings (RR-8 /
    # SEC-PERIMETER-4). Both count and results compose the same predicate,
    # so totals and rows stay consistent.
    where_clause = where_clause + public_serving_sql("m")

    # Get total count
    count_sql = f"SELECT COUNT(*) FROM meetings m WHERE {where_clause}"
    total = cursor.execute(count_sql, params).fetchone()[0]
    
    # Get results
    # Recipe fields (cities.calendar_url) are sealed and never leave on a
    # public search result (RR-8 / SEC-SEAL-3) — the join is dropped.
    results_sql = f"""
        SELECT m.*
        FROM meetings m
        WHERE {where_clause}
        ORDER BY m.meeting_date DESC
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])
    
    results = []
    for row in cursor.execute(results_sql, params):
        results.append({
            'id': row['id'],
            'city': row['city_name'],
            'county': row['county'],
            'state': row['state'],
            'meeting_title': row['meeting_title'],
            'meeting_date': row['meeting_date'],
            'meeting_time': row['meeting_time'],
            'meeting_location': row['meeting_location'],
            'meeting_status': row['meeting_status'],
            'agenda_url': row['agenda_url'],
            'minutes_url': row['minutes_url'],
            'video_url': row['video_url'],
            'summary': row['summary'],
        })
    
    conn.close()
    return {
        'results': results,
        'total': total,
        'limit': limit,
        'offset': offset,
        'has_more': (offset + limit) < total
    }


def count_users() -> int:
    """Return the total number of registered Z-SPAN accounts.

    Powers the V1-Odometer-1 "travelers" counter on the persistent footer.
    Public + unauthenticated; the value is non-sensitive (it's the size of
    the audience, not anyone's identity). Sub-ms on the indexed users
    table — no caching needed at V0.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        row = cursor.execute("SELECT COUNT(*) FROM users").fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


_LIBRARIAN_ACCESS_STATUSES = frozenset({
    "none",
    "requested",
    "granted",
    "banned",
})
_SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807
_INVITATION_BATCH_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_INVITATION_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{24,64}$")
_INVITATION_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
INVITATION_BATCH_LIMIT = 100


def invitation_token_hash(token: str) -> Optional[str]:
    """Return the normalized invitation digest, or None for bad input."""
    if not isinstance(token, str) or not _INVITATION_TOKEN_RE.fullmatch(token):
        return None
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def import_invitation_batch(
    batch_name: str,
    invitations: List[Dict[str, Any]],
    *,
    actor_user_id: Optional[int] = None,
) -> Dict[str, int]:
    """Import a bounded batch of pre-hashed card tokens idempotently."""
    if not isinstance(batch_name, str) or not _INVITATION_BATCH_RE.fullmatch(
        batch_name
    ):
        raise ValueError(
            "batch_name must be 1-64 lowercase letters, numbers, or hyphens"
        )
    if (
        not isinstance(invitations, list)
        or not invitations
        or len(invitations) > INVITATION_BATCH_LIMIT
    ):
        raise ValueError(
            f"invitations must contain 1-{INVITATION_BATCH_LIMIT} rows"
        )

    normalized: List[tuple[int, str]] = []
    seen_serials: set[int] = set()
    seen_hashes: set[str] = set()
    for item in invitations:
        if not isinstance(item, dict):
            raise ValueError("each invitation must be an object")
        serial = item.get("serial_number")
        token_hash = item.get("token_hash")
        if (
            isinstance(serial, bool)
            or not isinstance(serial, int)
            or serial <= 0
        ):
            raise ValueError("serial_number must be a positive integer")
        if (
            not isinstance(token_hash, str)
            or not _INVITATION_HASH_RE.fullmatch(token_hash)
        ):
            raise ValueError("token_hash must be a lowercase SHA-256 digest")
        if serial in seen_serials or token_hash in seen_hashes:
            raise ValueError("invitation batch contains a duplicate")
        seen_serials.add(serial)
        seen_hashes.add(token_hash)
        normalized.append((serial, token_hash))

    conn = get_connection()
    inserted = 0
    unchanged = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        for serial, token_hash in normalized:
            existing = conn.execute(
                """
                SELECT id, token_hash
                FROM invitation_codes
                WHERE batch_name = ? AND serial_number = ?
                """,
                (batch_name, serial),
            ).fetchone()
            if existing is not None:
                if existing["token_hash"] != token_hash:
                    raise ValueError(
                        "an invitation serial already belongs to another token"
                    )
                unchanged += 1
                continue
            collision = conn.execute(
                "SELECT id FROM invitation_codes WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if collision is not None:
                raise ValueError("an invitation token already belongs to another card")
            cursor = conn.execute(
                """
                INSERT INTO invitation_codes (
                    batch_name, serial_number, token_hash
                ) VALUES (?, ?, ?)
                """,
                (batch_name, serial, token_hash),
            )
            conn.execute(
                """
                INSERT INTO invitation_events (
                    invitation_code_id, event_type, actor_user_id
                ) VALUES (?, 'imported', ?)
                """,
                (cursor.lastrowid, actor_user_id),
            )
            inserted += 1
        conn.commit()
        return {"inserted": inserted, "unchanged": unchanged}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_invitation_status(token: str) -> str:
    """Return active/redeemed/revoked/invalid without exposing card data."""
    token_hash = invitation_token_hash(token)
    if token_hash is None:
        return "invalid"
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT status FROM invitation_codes WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        return str(row["status"]) if row is not None else "invalid"
    finally:
        conn.close()


def redeem_invitation_token_with_connection(
    conn: sqlite3.Connection,
    user_id: int,
    token: str,
) -> str:
    """Consume an invitation inside the caller's active transaction."""
    token_hash = invitation_token_hash(token)
    if token_hash is None:
        return "invalid"

    invitation = conn.execute(
        """
        SELECT id, status, redeemed_by_user_id
        FROM invitation_codes
        WHERE token_hash = ?
        """,
        (token_hash,),
    ).fetchone()
    if invitation is None:
        return "invalid"

    user = conn.execute(
        "SELECT librarian_access FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if user is None:
        return "user_not_found"
    if user["librarian_access"] == "banned":
        return "banned"

    if invitation["status"] == "revoked":
        return "revoked"
    if invitation["status"] == "redeemed":
        return (
            "redeemed"
            if invitation["redeemed_by_user_id"] == user_id
            else "unavailable"
        )
    if user["librarian_access"] == "granted":
        return "already_granted"

    claimed = conn.execute(
        """
        UPDATE invitation_codes
        SET status = 'redeemed',
            redeemed_by_user_id = ?,
            redeemed_at = CURRENT_TIMESTAMP
        WHERE id = ? AND status = 'active'
        """,
        (user_id, invitation["id"]),
    )
    if claimed.rowcount != 1:
        raise RuntimeError("invitation changed during redemption")
    granted = conn.execute(
        """
        UPDATE users
        SET librarian_access = 'granted',
            librarian_enforcement_epoch =
                librarian_enforcement_epoch + 1
        WHERE id = ? AND librarian_enforcement_epoch < ?
        """,
        (user_id, _SQLITE_MAX_INTEGER),
    )
    if granted.rowcount != 1:
        raise RuntimeError("user access changed during invitation redemption")
    conn.execute(
        """
        UPDATE librarian_abuse_state
        SET recent_rejects_json = '[]',
            recent_cooldowns_json = '[]',
            cooldown_until = NULL,
            cooldown_blocked_count = 0,
            duplicate_suppressed_count = 0,
            active_auto_ban = 0,
            last_restored_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE user_id = ?
        """,
        (user_id,),
    )
    conn.execute(
        """
        INSERT INTO invitation_events (
            invitation_code_id, event_type, actor_user_id
        ) VALUES (?, 'redeemed', ?)
        """,
        (invitation["id"], user_id),
    )
    return "redeemed"


def redeem_invitation_token(user_id: int, token: str) -> str:
    """Atomically consume one active invitation and grant Librarian access."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = redeem_invitation_token_with_connection(conn, user_id, token)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_invitation_codes() -> List[Dict[str, Any]]:
    """Return owner-safe invitation state without bearer hashes."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT
                invitation_codes.id,
                invitation_codes.batch_name,
                invitation_codes.serial_number,
                invitation_codes.status,
                invitation_codes.created_at,
                invitation_codes.redeemed_at,
                invitation_codes.revoked_at,
                users.email AS redeemed_by_email,
                users.display_name AS redeemed_by_name
            FROM invitation_codes
            LEFT JOIN users
                ON users.id = invitation_codes.redeemed_by_user_id
            ORDER BY invitation_codes.batch_name, invitation_codes.serial_number
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def revoke_invitation_code(
    invitation_id: int,
    *,
    actor_user_id: int,
) -> str:
    """Revoke an unused card; redeemed access remains separately governed."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status FROM invitation_codes WHERE id = ?",
            (invitation_id,),
        ).fetchone()
        if row is None:
            conn.commit()
            return "not_found"
        if row["status"] == "redeemed":
            conn.commit()
            return "already_redeemed"
        if row["status"] == "revoked":
            conn.commit()
            return "revoked"
        conn.execute(
            """
            UPDATE invitation_codes
            SET status = 'revoked', revoked_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'active'
            """,
            (invitation_id,),
        )
        conn.execute(
            """
            INSERT INTO invitation_events (
                invitation_code_id, event_type, actor_user_id
            ) VALUES (?, 'revoked', ?)
            """,
            (invitation_id, actor_user_id),
        )
        conn.commit()
        return "revoked"
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _increment_librarian_enforcement_epoch(
    conn: sqlite3.Connection,
    *,
    user_id: int,
) -> int:
    row = conn.execute(
        """
        UPDATE users
        SET librarian_enforcement_epoch =
                librarian_enforcement_epoch + 1
        WHERE id = ?
          AND librarian_enforcement_epoch < ?
        RETURNING librarian_enforcement_epoch
        """,
        (user_id, _SQLITE_MAX_INTEGER),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "Librarian enforcement epoch could not advance: "
            f"user_id={user_id}"
        )
    return int(row["librarian_enforcement_epoch"])


def get_user_librarian_access(user_id: int) -> Optional[str]:
    """Return a user's current Librarian access state, or None if absent."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT librarian_access FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return str(row["librarian_access"]) if row else None
    finally:
        conn.close()


def _librarian_access_allows_query(status: object) -> bool:
    """Keep ordinary signed-in accounts enabled while preserving bans.

    ``none`` and ``requested`` are legacy onboarding states. They no longer
    gate BYOK access, but remain stored so older account-management surfaces
    and audit history keep their meaning. Unknown values fail closed.
    """
    return status in {"none", "requested", "granted"}


def set_librarian_access(user_id: int, status: str) -> bool:
    """Set a validated Librarian access state; return whether the user exists."""
    if status not in _LIBRARIAN_ACCESS_STATUSES:
        raise ValueError(f"invalid librarian access status: {status!r}")

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT librarian_access
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        if row is None:
            conn.commit()
            return False
        if row["librarian_access"] != status:
            cursor = conn.execute(
                """
                UPDATE users
                SET librarian_access = ?,
                    librarian_enforcement_epoch =
                        librarian_enforcement_epoch + 1
                WHERE id = ?
                  AND librarian_enforcement_epoch < ?
                """,
                (status, user_id, _SQLITE_MAX_INTEGER),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "Librarian access mutation could not advance epoch"
                )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def decide_librarian_access(user_id: int, status: str) -> bool:
    """Apply an operator decision and clear auto-ban enforcement atomically."""
    if status not in _LIBRARIAN_ACCESS_STATUSES:
        raise ValueError(f"invalid librarian access status: {status!r}")

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute(
            """
            SELECT id
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        if user is None:
            conn.commit()
            return False
        conn.execute(
            "UPDATE users SET librarian_access = ? WHERE id = ?",
            (status, user_id),
        )

        if status == "granted":
            conn.execute(
                """
                UPDATE librarian_abuse_state
                SET recent_rejects_json = '[]',
                    recent_cooldowns_json = '[]',
                    cooldown_until = NULL,
                    cooldown_blocked_count = 0,
                    duplicate_suppressed_count = 0,
                    active_auto_ban = 0,
                    last_restored_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
                """,
                (user_id,),
            )
        else:
            # A manual ban (or any other operator decision) must never inherit
            # the automatic-ban label from an earlier enforcement episode.
            conn.execute(
                """
                UPDATE librarian_abuse_state
                SET active_auto_ban = 0,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
                """,
                (user_id,),
            )
        _increment_librarian_enforcement_epoch(conn, user_id=user_id)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _bounded_librarian_evidence(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    """Return only operator-safe auto-ban evidence fields."""
    if not raw:
        return None
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("librarian abuse evidence must be a JSON object")

    samples = []
    raw_samples = parsed.get("samples", [])
    if isinstance(raw_samples, list):
        for sample in raw_samples[:5]:
            if not isinstance(sample, dict):
                continue
            samples.append({
                "reason_code": str(sample.get("reason_code") or ""),
                "matched_rule_id": str(sample.get("matched_rule_id") or ""),
            })

    thresholds = parsed.get("thresholds")
    safe_thresholds = None
    if isinstance(thresholds, dict):
        allowed_threshold_keys = (
            "burst_threshold",
            "burst_window_seconds",
            "cooldown_seconds",
            "strike_threshold",
            "autoban_window_seconds",
        )
        safe_thresholds = {
            key: int(thresholds[key])
            for key in allowed_threshold_keys
            if (
                key in thresholds
                and isinstance(thresholds[key], int)
                and not isinstance(thresholds[key], bool)
            )
        }

    return {
        "refused_count": int(parsed.get("refused_count", 0)),
        "duplicate_suppressed_count": int(
            parsed.get("duplicate_suppressed_count", 0)
        ),
        "burst_count": int(parsed.get("burst_count", 0)),
        "window_started_at": str(parsed.get("window_started_at") or ""),
        "window_ended_at": str(parsed.get("window_ended_at") or ""),
        "samples": samples,
        "thresholds": safe_thresholds,
    }


def list_librarian_access_requests() -> List[Dict[str, Any]]:
    """Return non-default access rows plus bounded, query-free ban evidence."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT
                users.id,
                users.email,
                users.display_name,
                users.avatar_url,
                users.role,
                users.librarian_access,
                users.created_at,
                users.last_seen_at,
                COALESCE(abuse.active_auto_ban, 0) AS active_auto_ban,
                abuse.evidence_json
            FROM users
            LEFT JOIN librarian_abuse_state AS abuse
                ON abuse.user_id = users.id
            WHERE users.librarian_access != 'none'
            ORDER BY users.created_at ASC, users.id ASC
            """
        ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            evidence_json = item.pop("evidence_json", None)
            item["active_auto_ban"] = bool(item["active_auto_ban"])
            item["abuse_evidence"] = _bounded_librarian_evidence(
                evidence_json
            )
            results.append(item)
        return results
    finally:
        conn.close()


def get_stats() -> Dict:
    """Get database statistics."""
    conn = get_connection()
    cursor = conn.cursor()

    stats = {}
    stats['total_cities'] = cursor.execute("SELECT COUNT(*) FROM cities").fetchone()[0]
    stats['active_cities'] = cursor.execute("SELECT COUNT(*) FROM cities WHERE scrape_success = 1").fetchone()[0]
    stats['total_meetings'] = cursor.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
    stats['states'] = [r[0] for r in cursor.execute("SELECT DISTINCT state FROM cities ORDER BY state").fetchall()]
    stats['counties'] = [r[0] for r in cursor.execute("SELECT DISTINCT county FROM cities ORDER BY county").fetchall()]
    
    # Meetings by county
    stats['meetings_by_county'] = {}
    for row in cursor.execute("""
        SELECT county, COUNT(*) as count FROM meetings 
        GROUP BY county ORDER BY count DESC
    """):
        stats['meetings_by_county'][row['county']] = row['count']
    
    # Meetings by city (top 20)
    stats['top_cities'] = []
    for row in cursor.execute("""
        SELECT city_name, county, COUNT(*) as count FROM meetings 
        GROUP BY city_name ORDER BY count DESC LIMIT 20
    """):
        stats['top_cities'].append({
            'city': row['city_name'],
            'county': row['county'],
            'meetings': row['count']
        })
    
    # Recent scrapes
    stats['recent_scrapes'] = []
    for row in cursor.execute("""
        SELECT city_name, scraped_at, success, meetings_found 
        FROM scrape_log ORDER BY scraped_at DESC LIMIT 20
    """):
        stats['recent_scrapes'].append({
            'city': row['city_name'],
            'scraped_at': row['scraped_at'],
            'success': bool(row['success']),
            'meetings_found': row['meetings_found']
        })
    
    conn.close()
    return stats


def get_all_meetings_for_county(county: str, state: str = 'Arizona') -> List[Dict]:
    """Get all cached meetings for a county."""
    conn = get_connection()
    cursor = conn.cursor()
    
    # Legacy county route — gated to publicly-visible rows (RR-8 /
    # SEC-PERIMETER-4); it previously returned every cached meeting.
    cursor.execute(f"""
        SELECT * FROM meetings m
        WHERE m.county = ? AND m.state = ?{public_serving_sql("m")}
        ORDER BY m.city_name, m.meeting_date DESC
    """, (county, state))
    
    meetings = []
    for row in cursor.fetchall():
        meetings.append({
            'city': row['city_name'],
            'county': row['county'],
            'meeting_title': row['meeting_title'],
            'meeting_date': row['meeting_date'],
            'meeting_time': row['meeting_time'],
            'meeting_location': row['meeting_location'],
            'meeting_status': row['meeting_status'],
            'agenda_url': row['agenda_url'],
            'minutes_url': row['minutes_url'],
            'video_url': row['video_url'],
            'summary': row['summary'],
        })
    
    conn.close()
    return meetings


def save_council_member(city_name: str, name: str, title: str = None,
                        email: str = None, phone: str = None,
                        photo_url: str = None, ward: str = None):
    """Save a council member's contact info."""
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        INSERT OR REPLACE INTO council_members 
        (city_name, name, title, email, phone, photo_url, ward, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, (city_name, name, title, email, phone, photo_url, ward))
    
    conn.commit()
    conn.close()


def get_council_members(city_name: str) -> List[Dict]:
    """Get council members for a city.

    `role` (Mayor / Vice Mayor / Council Member) added 2026-06-05 — the
    earlier dict omission silently dropped role data downstream callers
    needed (notably the NotebookLM symbols-block builder, which derived
    role-prefixed aliases entirely as "Council Member" because every
    member came back without a role).
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT * FROM council_members WHERE city_name = ?
        ORDER BY title, name
    """, (city_name,))

    members = []
    for row in cursor.fetchall():
        members.append({
            'name': row['name'],
            'title': row['title'],
            'role': row['role'],
            'email': row['email'],
            'phone': row['phone'],
            'photo_url': row['photo_url'],
            'ward': row['ward'],
        })

    conn.close()
    return members


# NOTE: Notebook GC helpers (list_tracked_notebooks, is_notebook_tracked,
# is_notebook_protected, get_protection_reason, add_protected_notebook,
# remove_protected_notebook, list_protected_notebooks, log_deletion_attempt,
# list_deletion_log) were removed per D-143 (NotebookLM subsystem removal
# 2026-07-01) along with their backing tables (protected_notebook_ids,
# notebook_deletion_log), notebook_gc.py, and the /api/notebooks/* routes.
# See S-109 in FUTURE_THOUGHTS.md for the removal arc.


def _join_prose(items: list[str]) -> str:
    """Join a list of labels into a prose "A, B, and C" string. Used by
    check_publish_readiness's plain-language reason strings (D-054)."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


class PublishNotReadyError(Exception):
    """Raised by publish_meeting when the readiness gate refuses.

    The exception carries the structured verdict so the API layer can
    return a 422 with the specific reasons ("Missing required outputs:
    quote_extraction") instead of a generic 500.
    """

    def __init__(self, meeting_id: int, verdict: Dict[str, Any]):
        self.meeting_id = meeting_id
        self.verdict = verdict
        message_reasons = list(verdict.get("reasons") or ["not ready"])
        message_reasons.extend(verdict.get("publish_blockers") or [])
        reasons = "; ".join(message_reasons)
        super().__init__(
            f"Meeting {meeting_id} is not ready to publish: {reasons}"
        )


# Universal minimum output set for a publishable meeting. If any output is
# missing from notebook_outputs, publish_meeting refuses to flip
# is_published=1. Session-32 (2026-07-04) added the gate after operator fatigue
# over broken WOs reaching the publish surface.
#
# D-157 follow-through (2026-07-12): `suggested_questions` + `quote_extraction`
# were removed after worker generation retired them. Requiring outputs the
# worker never produces was an unsatisfiable readiness deadlock.
#
# D-164 (2026-07-13, operator-ratified) additionally drops
# `council_sentiment` + `tracked_claims`: the floor is universal, so D-157
# display-cut outputs must not block publication for any producer.
# `community_calls_to_action` remains honest-empty and therefore never floor-
# required. The executable floor now lives in output_contracts.py.
_PUBLISHABLE_REQUIRED_OUTPUTS = PUBLICATION_CONTRACT


def _preview_root_for_citation() -> Path:
    env = os.environ.get("ZSPAN_PREVIEW_ROOT")
    if env:
        return Path(env)
    # parsers/ -> council_navigator -> 02_Core_Project -> repo root -> .preview
    # canonical twin: flagship_sync._preview_root — keep in lockstep
    return Path(__file__).resolve().parents[3] / ".preview"


def _citation_membership_observation_reasons(
    decisions_json: Dict[str, Any],
    decisions_total: int,
) -> Dict[int, str]:
    """Recover independently anchored decisions from persisted alignment.

    Sidecars predating ``citation_alignment`` (or its ``source`` field) are
    grandfathered for chunk-membership misses so an evidence-schema rollout
    cannot silently make an already generated meeting unpublishable.  Missing
    citations still fail normally.  Known fallback rows and unknown future
    source values remain fail-closed.
    """
    if "citation_alignment" not in decisions_json:
        logger.warning(
            "decisions sidecar uses legacy citation policy: "
            "citation_alignment absent; chunk misses are observations"
        )
        return {
            index: "legacy_sidecar_without_citation_alignment"
            for index in range(1, decisions_total + 1)
        }

    alignment = decisions_json.get("citation_alignment")
    if not isinstance(alignment, list):
        logger.warning(
            "decisions sidecar citation_alignment is malformed; "
            "chunk membership remains fail-closed"
        )
        return {}

    reasons: Dict[int, str] = {}
    for entry in alignment:
        if not isinstance(entry, dict):
            logger.warning(
                "ignoring malformed decisions citation_alignment entry=%r",
                entry,
            )
            continue
        output_index = entry.get("output_index", entry.get("index"))
        if (
            not isinstance(output_index, int)
            or isinstance(output_index, bool)
            or not 1 <= output_index <= decisions_total
        ):
            logger.warning(
                "ignoring citation_alignment entry with invalid output index=%r",
                output_index,
            )
            continue

        source = entry.get("source")
        if source == "two_part_quote":
            reasons[output_index] = "quote_anchored_outside_retrieved_chunks"
        elif source == "outcome_signature_fallback":
            continue
        elif source is None or source == "":
            logger.warning(
                "decisions sidecar uses legacy citation policy for decision=%d: "
                "citation_alignment source absent; chunk miss is observational",
                output_index,
            )
            reasons[output_index] = "legacy_alignment_source_absent"
        else:
            logger.warning(
                "unknown citation_alignment source=%r for decision=%d; "
                "chunk membership remains fail-closed",
                source,
                output_index,
            )
    return reasons


def _citation_coverage(meeting_id: int) -> Dict[str, Any]:
    """Validate decision evidence under the sidecar's declared modality.

    ``transcript_excerpt_v1`` requires persisted, exactly reconstructible
    transcript spans for every decision. Legacy sidecars first attempt the
    same derivation in memory from their two-part alignment anchors; when that
    is impossible, the former inline-locator gate remains grandfathered.
    """
    coverage: Dict[str, Any] = {
        "ok": False,
        "decisions_total": None,
        "covered_indices": [],
        "uncited_decisions": [],
        "unknown_citations": [],
        "citation_observations": [],
        "no_decisions_extracted": False,
        "index_missing": False,
        "decisions_missing": False,
        "malformed": False,
        "citation_modality": None,
        "persisted_citation_modality": None,
        "legacy_materialized": False,
        "transcript_missing": False,
        "span_validation_errors": [],
    }
    try:
        preview_root = _preview_root_for_citation()
        decisions_path = preview_root / f"m{meeting_id}_decisions.json"

        try:
            decisions_json = json.loads(decisions_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            coverage["decisions_missing"] = True
            return coverage

        if not isinstance(decisions_json, dict):
            coverage["malformed"] = True
            return coverage
        prose = decisions_json.get("prose_output")
        if not isinstance(prose, str) or not prose.strip():
            coverage["malformed"] = True
            return coverage

        conn = get_connection()
        try:
            transcript_row = conn.execute(
                """
                SELECT content FROM notebook_outputs
                WHERE meeting_id = ? AND output_type = 'transcript_words'
                  AND content IS NOT NULL AND content != ''
                ORDER BY rowid DESC LIMIT 1
                """,
                (meeting_id,),
            ).fetchone()
            transcript_words: list[dict[str, Any]] = []
            if transcript_row:
                try:
                    transcript_payload = json.loads(transcript_row["content"])
                    raw_words = transcript_payload.get("words")
                    if isinstance(raw_words, list):
                        transcript_words = raw_words
                except (json.JSONDecodeError, TypeError, AttributeError):
                    transcript_words = []

            declared_modality = decisions_json.get("citation_modality")
            coverage["persisted_citation_modality"] = declared_modality
            validation_sidecar = decisions_json
            if declared_modality == _quote_align.TRANSCRIPT_EXCERPT_MODALITY:
                coverage["citation_modality"] = declared_modality
                if not transcript_words:
                    coverage["transcript_missing"] = True
                    return coverage
            elif declared_modality in (None, ""):
                if transcript_words:
                    derived = _quote_align.materialize_legacy_decision_excerpts(
                        decisions_json, transcript_words,
                    )
                    if (
                        derived.get("citation_modality")
                        == _quote_align.TRANSCRIPT_EXCERPT_MODALITY
                    ):
                        validation_sidecar = derived
                        coverage["citation_modality"] = (
                            _quote_align.TRANSCRIPT_EXCERPT_MODALITY
                        )
                        coverage["legacy_materialized"] = True
            else:
                coverage["citation_modality"] = declared_modality
                coverage["span_validation_errors"] = [
                    f"unsupported_citation_modality:{declared_modality}"
                ]
                return coverage

            if (
                validation_sidecar.get("citation_modality")
                == _quote_align.TRANSCRIPT_EXCERPT_MODALITY
            ):
                decisions_total = len(
                    _citation_validator.split_numbered_items(prose)
                )
                coverage["decisions_total"] = decisions_total
                if decisions_total == 0:
                    coverage["no_decisions_extracted"] = True
                    return coverage

                decisions = validation_sidecar.get("decisions")
                alignment = validation_sidecar.get("citation_alignment")
                if not isinstance(decisions, list) or not isinstance(alignment, list):
                    coverage["span_validation_errors"] = [
                        "decisions_or_citation_alignment_missing"
                    ]
                    return coverage
                decision_by_index: dict[int, dict[str, Any]] = {}
                alignment_by_index: dict[int, dict[str, Any]] = {}
                structural_errors: list[str] = []
                for decision in decisions:
                    if not isinstance(decision, dict):
                        structural_errors.append("malformed_decision_entry")
                        continue
                    index = decision.get("index")
                    if (
                        not isinstance(index, int)
                        or isinstance(index, bool)
                        or index in decision_by_index
                    ):
                        structural_errors.append(f"invalid_or_duplicate_decision_index:{index}")
                        continue
                    decision_by_index[index] = decision
                for entry in alignment:
                    if not isinstance(entry, dict):
                        structural_errors.append("malformed_alignment_entry")
                        continue
                    index = entry.get("output_index", entry.get("index"))
                    if (
                        not isinstance(index, int)
                        or isinstance(index, bool)
                        or index in alignment_by_index
                    ):
                        structural_errors.append(f"invalid_or_duplicate_alignment_index:{index}")
                        continue
                    alignment_by_index[index] = entry

                errors = list(structural_errors)
                covered: list[int] = []
                for index in range(1, decisions_total + 1):
                    decision = decision_by_index.get(index)
                    entry = alignment_by_index.get(index)
                    if decision is None or entry is None:
                        errors.append(f"decision_{index}_span_or_alignment_missing")
                        continue
                    item = entry.get("item_evidence")
                    action = entry.get("action_evidence")
                    if (
                        entry.get("source") != "two_part_quote"
                        or not isinstance(item, dict)
                        or not isinstance(action, dict)
                    ):
                        errors.append(f"decision_{index}_two_part_anchor_missing")
                        continue
                    span_errors = _quote_align.validate_transcript_excerpt_spans(
                        transcript_words,
                        decision.get("verbatim_spans"),
                        item,
                        action,
                    )
                    if span_errors:
                        errors.extend(
                            f"decision_{index}_{error}" for error in span_errors
                        )
                    else:
                        covered.append(index)

                if set(decision_by_index) != set(range(1, decisions_total + 1)):
                    errors.append("decision_index_set_mismatch")
                if set(alignment_by_index) != set(range(1, decisions_total + 1)):
                    errors.append("alignment_index_set_mismatch")
                coverage["covered_indices"] = covered
                coverage["uncited_decisions"] = [
                    index for index in range(1, decisions_total + 1)
                    if index not in covered
                ]
                coverage["span_validation_errors"] = errors
                coverage["ok"] = not errors and len(covered) == decisions_total
                return coverage

            rows = conn.execute(
                """
                SELECT start_seconds, end_seconds
                FROM local_retrieval_chunks
                WHERE meeting_id = ?
                """,
                (meeting_id,),
            ).fetchall()
            chunk_ranges = [
                (float(row["start_seconds"]), float(row["end_seconds"]))
                for row in rows
            ]
        except sqlite3.OperationalError:
            chunk_ranges = []
        finally:
            conn.close()

        if not chunk_ranges:
            coverage["index_missing"] = True
            return coverage

        decisions_total = len(_citation_validator.split_numbered_items(prose))
        observation_reasons = _citation_membership_observation_reasons(
            decisions_json,
            decisions_total,
        )
        report = _citation_validator.validate_inline_citations(
            prose,
            chunk_ranges,
            membership_observation_reasons=observation_reasons,
        )
        coverage["decisions_total"] = report.decisions_total
        coverage["covered_indices"] = list(report.covered_indices)
        coverage["uncited_decisions"] = list(report.uncovered_indices)
        coverage["unknown_citations"] = list(report.unknown_citations)
        coverage["citation_observations"] = list(
            report.nonmember_observations
        )
        if report.state == "no_decisions_extracted":
            coverage["no_decisions_extracted"] = True
            return coverage
        coverage["ok"] = report.state == "valid"
        return coverage
    except Exception:
        logger.exception("_citation_coverage failed for meeting %s", meeting_id)
        coverage["malformed"] = True
        return coverage


def _citation_publish_blockers(coverage: Dict[str, Any]) -> list[str]:
    """Translate citation failures into operator-facing publish blockers."""
    blockers = []
    uncited = coverage.get("uncited_decisions") or []
    if uncited:
        blockers.append(
            f"{len(uncited)} of {coverage['decisions_total']} key decisions still "
            "have no citation — the record can't back them yet."
        )
    unknown = coverage.get("unknown_citations") or []
    if unknown:
        blockers.append(
            f"{len(unknown)} citation(s) point outside the decision evidence "
            "retrieved from this meeting — regeneration needed."
        )
    span_errors = coverage.get("span_validation_errors") or []
    if span_errors:
        blockers.append(
            f"Transcript excerpts failed {len(span_errors)} exact source "
            "validation check(s) — regeneration needed."
        )
    if coverage.get("transcript_missing"):
        blockers.append(
            "The canonical word-timed transcript is missing — transcript "
            "excerpts can't be confirmed."
        )
    if coverage.get("no_decisions_extracted"):
        blockers.append(
            "No key decisions were extracted — this needs review before publishing "
            "(the meeting genuinely made none, or synthesis failed)."
        )
    if coverage.get("index_missing"):
        blockers.append(
            "The meeting hasn't been indexed for citation checks yet — run the "
            "pipeline first."
        )
    if coverage.get("malformed") or coverage.get("decisions_missing"):
        blockers.append(
            "The decision record is missing or unreadable — citations can't be "
            "confirmed."
        )
    return blockers


def check_publish_readiness(meeting_id: int) -> Dict[str, Any]:
    """Return a structured verdict on whether a meeting can be published.

    Session-32 (2026-07-04) — the publish gate. Runs the same checks
    publish_meeting() enforces internally, but as a pure read so the
    OperatorTerminal / caller can surface "Not ready — missing X"
    before the click.

    Verdict shape:
        {
          "ready": bool,
          "missing_outputs": list[str],  # V1-RAG-3 outputs not in notebook_outputs
          "output_errors": list[dict],   # any notebook_outputs rows with an error
          "no_quotes": bool,             # quote_extraction ran but produced 0 rows
          "no_video_url": bool,          # meeting has no video_url — karaoke broken
          "reasons": list[str],          # human-readable failure reasons
        }
    """
    conn = get_connection()
    try:
        meeting_row = conn.execute(
            "SELECT id, video_url FROM meetings WHERE id = ?",
            (meeting_id,),
        ).fetchone()
        if not meeting_row:
            citation_coverage = {
                "ok": False,
                "decisions_total": None,
                "covered_indices": [],
                "uncited_decisions": [],
                "unknown_citations": [],
                "citation_observations": [],
                "no_decisions_extracted": False,
                "index_missing": False,
                "decisions_missing": False,
                "malformed": False,
            }
            verdict = {
                "ready": False,
                "missing_outputs": [],
                "output_errors": [],
                "no_quotes": False,
                "no_video_url": False,
                "reasons": [f"Meeting {meeting_id} not found"],
                "required_ok": 0,
                "required_total": len(_PUBLISHABLE_REQUIRED_OUTPUTS),
            }
            verdict.update({
                "citation_coverage": citation_coverage,
                "publishable": verdict["ready"] and citation_coverage["ok"],
                "publish_blockers": _citation_publish_blockers(citation_coverage),
            })
            return verdict

        citation_coverage = _citation_coverage(meeting_id)

        outputs = conn.execute(
            """
            SELECT output_type, LENGTH(content) AS content_len, error
            FROM notebook_outputs
            WHERE meeting_id = ?
            """,
            (meeting_id,),
        ).fetchall()

        present_types = {
            r["output_type"] for r in outputs
            if r["content_len"] and r["content_len"] > 0 and not r["error"]
        }
        # Only surface errors on outputs the current V1-RAG-3 pipeline
        # still cares about. Pre-D-143 NotebookLM-era outputs (`episode_tags`,
        # `member_attendance`, `quotes`, `council_quotes`, `member_quotes_topic`,
        # `audio_overview`, `video_explainer`, `infographic`) commonly show
        # historical error rows from when the retired subsystem was
        # deprecating; those are noise, not current-meeting health signals.
        output_errors = [
            {"output_type": r["output_type"], "error": r["error"]}
            for r in outputs
            if r["error"] and r["output_type"] in _PUBLISHABLE_REQUIRED_OUTPUTS
        ]

        quote_count = conn.execute(
            "SELECT COUNT(*) AS n FROM quotes WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchone()["n"]

        # `quote_extraction` in the required-outputs list normally means
        # "there's a notebook_outputs row for it." But session-32's
        # sidecar-backfill wrote quotes to the canonical `quotes` table
        # without landing a notebook_outputs stub — so accept
        # "at least one row in `quotes`" as equivalent evidence that the
        # extraction ran and populated the display surface.
        satisfied_types = set(present_types)
        if quote_count > 0:
            satisfied_types.add("quote_extraction")

        missing = [
            t for t in _PUBLISHABLE_REQUIRED_OUTPUTS if t not in satisfied_types
        ]

        no_video_url = not (meeting_row["video_url"] and meeting_row["video_url"].strip())
        no_quotes = "quote_extraction" in present_types and quote_count == 0

        # Reason strings are plain-language per D-054 (schema-as-label
        # anti-pattern). Operator terminal + any future citizen-adjacent
        # surface reads these directly; internal output_type names stay
        # in the machine-readable fields (missing_outputs list, verdict
        # object) for programmatic consumers.
        _HUMAN_OUTPUT_LABELS = {
            "synopsis": "the synopsis",
            "newsletter": "the newsletter summary",
            "key_decisions": "the key-decisions list",
            "whats_next": "the what's-next section",
            "council_sentiment": "the council-sentiment read",
            "suggested_questions": "the suggested-questions block",
            "tracked_claims": "the tracked-claims ledger",
            "episode_tagline": "the episode tagline",
            "transcript_words": "the meeting transcript",
            "quote_extraction": "the key quotes",
        }
        reasons = []
        if missing:
            labels = [_HUMAN_OUTPUT_LABELS.get(t, t) for t in missing]
            reasons.append(
                f"This meeting is still missing {_join_prose(labels)}."
            )
        if output_errors:
            reasons.append(
                f"{len(output_errors)} piece(s) errored while being generated — "
                "the work order needs a retry before this can go public."
            )
        if no_quotes:
            reasons.append(
                "Quote extraction ran but returned nothing — karaoke would be empty."
            )
        if no_video_url:
            reasons.append(
                "No video URL is attached — karaoke citations would have nowhere to seek."
            )

        verdict = {
            "ready": len(reasons) == 0,
            "missing_outputs": missing,
            "output_errors": output_errors,
            "no_quotes": no_quotes,
            "no_video_url": no_video_url,
            "reasons": reasons,
            # F-7.1 (2026-07-06) — machine-readable completeness summary so
            # payload consumers get "N of M" without importing the floor
            # tuple. This function stays the single source of truth for
            # publish-readiness; consumers read the verdict, never
            # reimplement it.
            "required_ok": len(_PUBLISHABLE_REQUIRED_OUTPUTS) - len(missing),
            "required_total": len(_PUBLISHABLE_REQUIRED_OUTPUTS),
        }
        verdict.update({
            "citation_coverage": citation_coverage,
            "publishable": verdict["ready"] and citation_coverage["ok"],
            "publish_blockers": _citation_publish_blockers(citation_coverage),
        })
        return verdict
    finally:
        conn.close()


def _register_flagship_generations(
    cursor: sqlite3.Cursor,
    meeting_id: int,
    publisher_user_id: int,
) -> int:
    """Register each ribbon-bearing output once inside the publish transaction."""
    owner = cursor.execute(
        "SELECT 1 FROM users WHERE id = ?",
        (publisher_user_id,),
    ).fetchone()
    if owner is None:
        raise ValueError(
            f"Cannot register flagship outputs for unknown user {publisher_user_id}"
        )

    placeholders = ",".join("?" for _ in FLAGSHIP_RIBBON_OUTPUT_TYPES)
    rows = cursor.execute(
        f"""
        SELECT id, meeting_id, output_type
        FROM notebook_outputs
        WHERE meeting_id = ? AND output_type IN ({placeholders})
        ORDER BY id
        """,
        (meeting_id, *sorted(FLAGSHIP_RIBBON_OUTPUT_TYPES)),
    ).fetchall()
    inserted = 0
    for row in rows:
        existing = cursor.execute(
            "SELECT 1 FROM flagship_generations WHERE notebook_output_id = ?",
            (row["id"],),
        ).fetchone()
        if existing is not None:
            continue
        ribbon_token = mint_flagship_ribbon_token(cursor)
        cursor.execute(
            """
            INSERT INTO flagship_generations (
                ribbon_token, notebook_output_id, meeting_id, output_type,
                user_id, status
            ) VALUES (?, ?, ?, ?, ?, 'registered')
            ON CONFLICT(notebook_output_id) DO NOTHING
            """,
            (
                ribbon_token,
                row["id"],
                row["meeting_id"],
                row["output_type"],
                publisher_user_id,
            ),
        )
        inserted += cursor.rowcount
    return inserted


def publish_meeting(
    meeting_id: int,
    published_by: str,
    publisher_user_id: int,
    publish_notes: Optional[str] = None,
    force: bool = False,
    actor_user_id: Optional[int] = None,
    event_key: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Phase 3: flip a meeting from draft → published. Idempotent
    (re-publishing updates the timestamp + records the latest approver).
    Returns the updated meeting row, or None if no such meeting.

    Per ROADMAP Phase 3: this is the visibility-gate. The D-032 quality
    approval on the work order is a separate concern (operator vouches
    for the artifacts); is_published controls whether the public
    ChannelsPage shows this broadcast.

    CANONICAL PUBLISH PATH — any code flipping is_published=1 should go
    through this function so published_by / published_at / publish_notes
    stay in lockstep with the visibility bit. Direct `SET is_published=1`
    writes are only sanctioned in ad-hoc revert flows (see
    overnight_orchestrator.py TEMP_TEST_BATCH block) and leave a rogue
    row if they land without the metadata triad — the m104714 session-31
    incident traced back to exactly that shape.

    PUBLISH-READINESS GATE (session-32 2026-07-04) — refuses to flip
    is_published=1 when the meeting is missing V1-RAG-3 required outputs,
    has errored outputs, has zero extracted quotes, or has no video URL.
    Raises PublishNotReadyError with the structured verdict so the caller
    can surface "Not ready — missing X" to the operator. Pass force=True
    to override — the override is logged into publish_notes verbatim so
    the audit trail names which reasons the operator bypassed.
    """
    if not force:
        verdict = check_publish_readiness(meeting_id)
        if not verdict["publishable"]:
            raise PublishNotReadyError(meeting_id, verdict)

    published_by = operator_identity.coerce_role_identity(published_by)
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            UPDATE meetings
            SET is_published = 1,
                published_at = CURRENT_TIMESTAMP,
                published_by = ?,
                publish_notes = COALESCE(?, publish_notes),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (published_by, publish_notes, meeting_id),
        )
        if cursor.rowcount == 0:
            return None
        _register_flagship_generations(
            cursor,
            meeting_id=meeting_id,
            publisher_user_id=publisher_user_id,
        )
        if actor_user_id is not None:
            if not event_key:
                raise ValueError("event_key is required when actor_user_id is supplied")
            occurred_at = cursor.execute(
                "SELECT published_at FROM meetings WHERE id = ?", (meeting_id,)
            ).fetchone()["published_at"]
            cursor.execute(
                """
                INSERT INTO operator_review_events (
                    event_key, action, meeting_id, actor_user_id, occurred_at
                ) VALUES (?, 'publish', ?, ?, ?)
                """,
                (event_key, meeting_id, actor_user_id, occurred_at),
            )
        conn.commit()
        row = conn.execute(
            """
            SELECT id, city_name, meeting_title, meeting_date,
                   is_published, published_at, published_by, publish_notes
            FROM meetings WHERE id = ?
            """,
            (meeting_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def unpublish_meeting(
    meeting_id: int,
    unpublished_by: Optional[str] = None,
    reason: Optional[str] = None,
    actor_user_id: Optional[int] = None,
    event_key: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Phase 3: hide a previously-published broadcast from the public
    ChannelsPage. Doesn't touch the D-032 work-order approval — that's
    the operator's quality vouch and stays. Preserves the original
    `published_at` + `published_by` for the audit trail; appends an
    unpublish note to `publish_notes`.

    CANONICAL UNPUBLISH PATH — same discipline as publish_meeting(): any
    code flipping is_published=0 on a live broadcast should route through
    here so the audit note lands in publish_notes. Bulk-revert scripts
    that legitimately bypass this (TEMP_TEST_BATCH sentinel clears) must
    be per-meeting-id scoped and audit-logged.
    """
    unpublished_by = operator_identity.coerce_role_identity(unpublished_by)
    conn = get_connection()
    cursor = conn.cursor()
    try:
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        note = f"[unpublished at {ts} by {unpublished_by}"
        if reason:
            note += f": {reason}"
        note += "]"
        cursor.execute(
            """
            UPDATE meetings
            SET is_published = 0,
                publish_notes = TRIM(COALESCE(publish_notes, '') || ' ' || ?),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (note, meeting_id),
        )
        if cursor.rowcount == 0:
            return None
        if actor_user_id is not None:
            if not event_key:
                raise ValueError("event_key is required when actor_user_id is supplied")
            occurred_at = cursor.execute(
                "SELECT updated_at FROM meetings WHERE id = ?", (meeting_id,)
            ).fetchone()["updated_at"]
            cursor.execute(
                """
                INSERT INTO operator_review_events (
                    event_key, action, meeting_id, actor_user_id, occurred_at
                ) VALUES (?, 'unpublish', ?, ?, ?)
                """,
                (event_key, meeting_id, actor_user_id, occurred_at),
            )
        conn.commit()
        row = conn.execute(
            """
            SELECT id, city_name, meeting_title, meeting_date,
                   is_published, published_at, published_by, publish_notes
            FROM meetings WHERE id = ?
            """,
            (meeting_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_publish_status(meeting_id: int) -> Optional[Dict[str, Any]]:
    """Return a meeting's publish-state snapshot. Used by the
    BroadcastPage 'Published · [date]' badge + the operator terminal's
    per-WO publish indicator.

    Identity fields (published_by / publish_notes / approver) are
    deliberately NOT selected — operator direction 2026-07-09: personal
    identity stays off served surfaces; timestamps carry the state. The
    DB columns keep the internal audit trail."""
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT m.id, m.city_name, m.meeting_title, m.meeting_date,
                   m.is_published, m.published_at,
                   wo.approved_at AS wo_approved_at
            FROM meetings m
            LEFT JOIN work_orders wo ON wo.meeting_id = m.id
            WHERE m.id = ?
            """,
            (meeting_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────
# D-051 Flagship sync log helpers (push-to-flagship)
# ─────────────────────────────────────────────────────────────────

def record_flagship_sync_attempt(
    meeting_id: int,
    status: str,
    pushed_by: Optional[str] = None,
    error: Optional[str] = None,
    payload_bytes: Optional[int] = None,
    media_bytes: Optional[int] = None,
    flagship_response: Optional[str] = None,
) -> int:
    """Insert a flagship_sync_log row. Returns the new row's id.

    `status` is one of 'in_progress', 'success', 'failed'. Callers typically
    insert an 'in_progress' row at the start of a push attempt then UPDATE
    it to 'success' or 'failed' on completion — but this helper is
    INSERT-only; use `update_flagship_sync_attempt` for the second step.
    Append-only history preserves the audit trail.
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO flagship_sync_log
                (meeting_id, pushed_by, status, error,
                 payload_bytes, media_bytes, flagship_response)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (meeting_id, pushed_by, status, error,
             payload_bytes, media_bytes, flagship_response),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def update_flagship_sync_attempt(
    attempt_id: int,
    status: str,
    error: Optional[str] = None,
    payload_bytes: Optional[int] = None,
    media_bytes: Optional[int] = None,
    flagship_response: Optional[str] = None,
) -> bool:
    """Update an in-progress sync attempt with its terminal status + stats.
    Returns True if a row was updated.
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            UPDATE flagship_sync_log
            SET status = ?,
                error = COALESCE(?, error),
                payload_bytes = COALESCE(?, payload_bytes),
                media_bytes = COALESCE(?, media_bytes),
                flagship_response = COALESCE(?, flagship_response)
            WHERE id = ?
            """,
            (status, error, payload_bytes, media_bytes,
             flagship_response, attempt_id),
        )
        updated = cursor.rowcount > 0
        conn.commit()
        return updated
    finally:
        conn.close()


def get_latest_flagship_sync(meeting_id: int) -> Optional[Dict[str, Any]]:
    """Return the most-recent flagship_sync_log row for a meeting (or None).
    Used by the operator-terminal [PUSH] button's status indicator."""
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT id, meeting_id, attempted_at, pushed_by, status, error,
                   payload_bytes, media_bytes, flagship_response
            FROM flagship_sync_log
            WHERE meeting_id = ?
            ORDER BY attempted_at DESC
            LIMIT 1
            """,
            (meeting_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_flagship_sync_status_for_meetings(
    meeting_ids: List[int],
) -> Dict[int, Dict[str, Any]]:
    """Bulk-fetch the latest sync status per meeting_id. Used by the
    operator-terminal WO list to render per-row status indicators without
    N+1 queries.

    Returns {meeting_id: latest_log_row_dict}. Meetings with no sync
    attempts are absent from the result.
    """
    if not meeting_ids:
        return {}
    placeholders = ",".join("?" for _ in meeting_ids)
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT fsl.meeting_id, fsl.id, fsl.attempted_at, fsl.pushed_by,
                   fsl.status, fsl.error, fsl.payload_bytes, fsl.media_bytes,
                   fsl.flagship_response
            FROM flagship_sync_log fsl
            INNER JOIN (
                SELECT meeting_id, MAX(attempted_at) AS max_at
                FROM flagship_sync_log
                WHERE meeting_id IN ({placeholders})
                GROUP BY meeting_id
            ) latest
              ON latest.meeting_id = fsl.meeting_id
             AND latest.max_at = fsl.attempted_at
            """,
            meeting_ids,
        ).fetchall()
        return {row['meeting_id']: dict(row) for row in rows}
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────
# D-051 Receiver-side meeting + outputs UPSERT
# ─────────────────────────────────────────────────────────────────

def upsert_meeting_from_flagship_payload(
    meeting_payload: Dict[str, Any],
) -> int:
    """Idempotent UPSERT for the cloud receiver side. Accepts a meeting
    dict gathered by the sender's `gather_meeting_payload()` and either
    inserts a new row or updates an existing row keyed on `id`.

    Preserves local IDs across sender and receiver — the cloud's meeting
    row has the same `id` as the sender's, so subsequent /api/notebook/<id>
    URLs work identically. This assumes ID collisions don't happen across
    a single-flagship + single-sender topology, which holds for V1
    (one James, one cloud).

    City row is auto-created/found by (city_name, county, state) so the
    foreign key constraint on meetings.city_id is satisfied.

    Returns the (preserved) meeting id.
    """
    if 'id' not in meeting_payload:
        raise ValueError("meeting_payload requires 'id'")
    violation = publication_text_violation(meeting_payload.get('publish_notes'))
    if violation:
        raise ValueError(f"publish_notes {violation}")
    meeting_payload = dict(meeting_payload)
    meeting_payload['published_by'] = operator_identity.coerce_role_identity(
        meeting_payload.get('published_by')
    )
    meeting_id = int(meeting_payload['id'])

    conn = get_connection()
    cursor = conn.cursor()
    try:
        # Ensure city row exists for FK. NB: the `cities` table column is
        # `name`, not `city_name` (the `meetings` table denormalizes the
        # city's name into its own `city_name` column).
        city_name = meeting_payload.get('city_name')
        county = meeting_payload.get('county') or ''
        state = meeting_payload.get('state') or resolve_city_state(None, county)
        cursor.execute(
            """
            SELECT id FROM cities
            WHERE name = ? AND county = ? AND state = ?
            """,
            (city_name, county, state),
        )
        city_row = cursor.fetchone()
        if city_row is None:
            cursor.execute(
                """
                INSERT INTO cities (name, county, state)
                VALUES (?, ?, ?)
                """,
                (city_name, county, state),
            )
            city_id = cursor.lastrowid
        else:
            city_id = city_row['id']

        # UPSERT the meeting. The meetings table has TWO uniqueness
        # constraints: id (primary key) AND the composite natural key
        # (city_name, state, meeting_date, meeting_title) from D-038.
        # A single ON CONFLICT clause can only target one of them. To
        # handle both: pre-check whether a row already exists matching
        # either, then UPDATE if found / INSERT if not.
        #
        # If a composite-key match is found, we use ITS id (not the
        # payload's). The caller (and notebook_outputs UPSERT) then sees
        # the cloud's id, which may differ from the sender's local id.
        # This is acceptable because the BroadcastPage URL is built from
        # the cloud's id; the sender's id is just a payload field.
        meeting_title = meeting_payload.get('meeting_title')
        meeting_date = meeting_payload.get('meeting_date')
        cursor.execute(
            """
            SELECT id, is_published, published_at, published_by, publish_notes
            FROM meetings
            WHERE id = ?
               OR (city_name = ? AND state = ?
                   AND meeting_date = ? AND meeting_title = ?)
            LIMIT 1
            """,
            (meeting_id, city_name, state, meeting_date, meeting_title),
        )
        existing = cursor.fetchone()

        cols = [
            'city_id', 'city_name', 'county', 'state',
            'meeting_title', 'meeting_date', 'meeting_time',
            'meeting_location', 'meeting_status',
            'agenda_url', 'minutes_url', 'video_url',
            'agenda_packet_url', 'ecomment_url', 'meeting_id',
            'summary', 'notebook_id',
            'is_published', 'published_at', 'published_by', 'publish_notes',
        ]
        values_no_id = [
            city_id, city_name, county, state,
        ] + [meeting_payload.get(c) for c in cols[4:]]

        if existing:
            effective_id = existing['id']
            # Publish state is prod-owned; a sync payload must never downgrade an
            # operator-published row (2026-07-27 incident: meeting 127696).
            if (
                existing['is_published']
                and not meeting_payload.get('is_published')
            ):
                for publish_col in (
                    'is_published',
                    'published_at',
                    'published_by',
                    'publish_notes',
                ):
                    values_no_id[cols.index(publish_col)] = existing[publish_col]
            set_clause = ", ".join(f"{c} = ?" for c in cols)
            cursor.execute(
                f"""
                UPDATE meetings
                SET {set_clause}, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                values_no_id + [effective_id],
            )
        else:
            effective_id = meeting_id
            for _ in range(_PUBLIC_ID_COLLISION_RETRIES):
                public_id = _generate_available_public_id(cursor)
                try:
                    cursor.execute(
                        f"""
                        INSERT INTO meetings (id, public_id, {",".join(cols)})
                        VALUES (?, ?, {",".join("?" for _ in cols)})
                        """,
                        [effective_id, public_id] + values_no_id,
                    )
                except sqlite3.IntegrityError as exc:
                    if "meetings.public_id" in str(exc):
                        continue
                    raise
                break
            else:
                raise RuntimeError(
                    "Unable to insert flagship meeting with a unique public_id"
                )
        conn.commit()
        return effective_id
    finally:
        conn.close()


def upsert_notebook_outputs_from_flagship_payload(
    meeting_id: int,
    outputs: List[Dict[str, Any]],
) -> int:
    """Idempotent UPSERT for notebook_outputs rows. Receiver side of the
    sync push. Returns the number of rows upserted.
    """
    if not outputs:
        return 0
    conn = get_connection()
    cursor = conn.cursor()
    try:
        for output in outputs:
            cursor.execute(
                """
                INSERT INTO notebook_outputs
                    (meeting_id, notebook_id, output_type, content,
                     content_url, prompt_filename, prompt_version,
                     generated_at, error, voided_at, voided_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(meeting_id, output_type) DO UPDATE SET
                    content = excluded.content,
                    content_url = excluded.content_url,
                    prompt_filename = excluded.prompt_filename,
                    prompt_version = excluded.prompt_version,
                    generated_at = excluded.generated_at,
                    error = excluded.error,
                    voided_at = CASE
                        WHEN ? THEN excluded.voided_at
                        ELSE notebook_outputs.voided_at
                    END,
                    voided_by = CASE
                        WHEN ? THEN excluded.voided_by
                        ELSE notebook_outputs.voided_by
                    END
                """,
                (
                    meeting_id,
                    output.get('notebook_id') or '',
                    output['output_type'],
                    output.get('content'),
                    output.get('content_url'),
                    output.get('prompt_filename'),
                    output.get('prompt_version'),
                    output.get('generated_at'),
                    output.get('error'),
                    output.get('voided_at'),
                    output.get('voided_by'),
                    'voided_at' in output,
                    'voided_at' in output,
                ),
            )
        conn.commit()
        return len(outputs)
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────
# V1.5 flagship sync — receiver-side UPSERTs for structured tables
# (Quotes Unification Refactor Chunk 5, 2026-05-26)
# ─────────────────────────────────────────────────────────────────
#
# Each helper UPSERTs rows into one structured table on the cloud side
# from the JSON payload the local sender posted. Three patterns are in use,
# matched to each table's natural-key shape:
#
#   1. UPSERT-by-natural-key + id remap (council_members, member_attendance,
#      quotes): the table has a UNIQUE constraint that's stable across
#      sender + receiver, so we look up by that key and either UPDATE the
#      cloud's existing row (keeping its id, returning a remap so callers
#      can translate FK references) or INSERT preserving the sender's id
#      (which becomes the cloud's id when the table is fresh).
#
#   2. DELETE-then-INSERT keyed on meeting_id (member_quotes legacy,
#      tracked_claims): tables without a stable natural key. Receiver is
#      authoritative for the meeting; pre-existing rows on cloud for that
#      meeting get wiped before fresh INSERT. The sender is single-writer
#      for these tables (cloud doesn't independently mutate them), so the
#      wipe-and-replace pattern is safe — same pattern these tables already
#      use locally (see save_member_quotes_batch + save_tracked_claims_batch).


def upsert_council_members_from_flagship_payload(
    items: List[Dict[str, Any]],
) -> Dict[int, int]:
    """UPSERT council_members rows from the sync payload. Returns a
    {sender_id: receiver_id} mapping so callers can translate member_id
    FK references in the quotes / member_attendance / etc. payloads.

    For each incoming row:
      - If (city_name, seat_id) already exists on the receiver, UPDATE
        in place keeping the receiver's id. Map sender_id → receiver_id.
      - If not, INSERT preserving the sender's id. Map sender_id → sender_id.

    This handles both: (a) fresh cloud installs (ids match end-to-end),
    and (b) clouds that already seeded council_members via
    seed_council_members_from_intelligence on first boot (ids may differ;
    remap protects FK integrity).
    """
    mapping: Dict[int, int] = {}
    if not items:
        return mapping
    conn = get_connection()
    cursor = conn.cursor()
    try:
        for it in items:
            sender_id = it.get('id')
            city_name = it.get('city_name')
            seat_id = it.get('seat_id')
            if not city_name or not seat_id:
                # Can't safely upsert without natural key. Skip.
                continue
            existing = cursor.execute(
                "SELECT id FROM council_members WHERE city_name = ? AND seat_id = ?",
                (city_name, seat_id),
            ).fetchone()
            if existing:
                receiver_id = existing['id']
                cursor.execute(
                    """
                    UPDATE council_members
                    SET name = ?, title = ?, email = ?, phone = ?, photo_url = ?,
                        ward = ?, term_start = ?, term_end = ?, role = ?,
                        source_url = ?, term_started = ?, term_ends = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        it.get('name'), it.get('title'), it.get('email'), it.get('phone'),
                        it.get('photo_url'), it.get('ward'), it.get('term_start'),
                        it.get('term_end'), it.get('role'), it.get('source_url'),
                        it.get('term_started'), it.get('term_ends'),
                        receiver_id,
                    ),
                )
            else:
                receiver_id = sender_id
                cursor.execute(
                    """
                    INSERT INTO council_members (
                        id, city_name, name, title, email, phone, photo_url, ward,
                        term_start, term_end, seat_id, role, source_url,
                        term_started, term_ends
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sender_id, city_name, it.get('name'), it.get('title'),
                        it.get('email'), it.get('phone'), it.get('photo_url'),
                        it.get('ward'), it.get('term_start'), it.get('term_end'),
                        seat_id, it.get('role'), it.get('source_url'),
                        it.get('term_started'), it.get('term_ends'),
                    ),
                )
            if sender_id is not None:
                mapping[sender_id] = receiver_id
        conn.commit()
    finally:
        conn.close()
    return mapping


def _remap_member_id(value: Any, remap: Dict[int, int]) -> Any:
    """Translate a sender-side member_id through the remap. Returns the
    original value if not in the remap (e.g., None for non-council-member
    rows in the quotes table) or if remap is empty."""
    if value is None or not remap:
        return value
    return remap.get(value, value)


def upsert_quotes_from_flagship_payload(
    meeting_id: int,
    items: List[Dict[str, Any]],
    member_id_remap: Dict[int, int],
) -> int:
    """UPSERT rows into the unified `quotes` table via UNIQUE(meeting_id,
    content_hash). Preserves the receiver's verification state on conflict —
    if a row already exists for this (meeting_id, content_hash), the cloud's
    `verified_status`/`verified_by`/`verified_at`/`gemini_correction_notes`
    are kept; only extraction + alignment + hero fields are updated from
    the sender.

    Mirrors the save_quotes_batch preservation semantics so re-pushes don't
    wipe verification work the cloud has accumulated independently. (V1
    architecture: cloud doesn't independently verify, so this is moot for
    V1.5; the pattern is in place for future multi-operator scenarios.)
    """
    if not items:
        return 0
    conn = get_connection()
    cursor = conn.cursor()
    upserted = 0
    try:
        for it in items:
            content_hash = it.get('content_hash')
            if not content_hash:
                continue
            member_id = _remap_member_id(it.get('member_id'), member_id_remap)
            existing = cursor.execute(
                "SELECT id FROM quotes WHERE meeting_id = ? AND content_hash = ?",
                (meeting_id, content_hash),
            ).fetchone()
            if existing:
                cursor.execute(
                    """
                    UPDATE quotes
                    SET member_id = ?, speaker_name = ?, speaker_role = ?,
                        speaker_class = ?, quote_text = ?, quote_text_original = ?,
                        topic_tags = ?, minutes_page_ref = ?, context = ?,
                        is_broadcast_hero = ?, video_timestamp_seconds = ?,
                        word_timings = ?,
                        proof_clip_url = COALESCE(?, proof_clip_url),
                        proof_clip_sha256 = COALESCE(?, proof_clip_sha256),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        member_id, it.get('speaker_name'), it.get('speaker_role'),
                        it.get('speaker_class') or 'council_member',
                        it.get('quote_text'), it.get('quote_text_original'),
                        it.get('topic_tags'), it.get('minutes_page_ref'),
                        it.get('context'),
                        1 if it.get('is_broadcast_hero') else 0,
                        it.get('video_timestamp_seconds'),
                        it.get('word_timings'),
                        it.get('proof_clip_url'),
                        it.get('proof_clip_sha256'),
                        existing['id'],
                    ),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO quotes (
                        meeting_id, member_id, speaker_name, speaker_role, speaker_class,
                        quote_text, quote_text_original, topic_tags, minutes_page_ref, context,
                        is_broadcast_hero, video_timestamp_seconds, word_timings,
                        verified_status, verified_by, verified_at, gemini_correction_notes,
                        proof_clip_url, proof_clip_sha256, content_hash,
                        extracted_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?,
                        COALESCE(?, CURRENT_TIMESTAMP), CURRENT_TIMESTAMP
                    )
                    """,
                    (
                        meeting_id, member_id,
                        it.get('speaker_name'), it.get('speaker_role'),
                        it.get('speaker_class') or 'council_member',
                        it.get('quote_text'), it.get('quote_text_original'),
                        it.get('topic_tags'), it.get('minutes_page_ref'),
                        it.get('context'),
                        1 if it.get('is_broadcast_hero') else 0,
                        it.get('video_timestamp_seconds'),
                        it.get('word_timings'),
                        it.get('verified_status') or 'pending',
                        operator_identity.coerce_optional_role_identity(
                            it.get('verified_by')
                        ),
                        it.get('verified_at'),
                        it.get('gemini_correction_notes'),
                        it.get('proof_clip_url'), it.get('proof_clip_sha256'),
                        content_hash,
                        it.get('extracted_at'),
                    ),
                )
            upserted += 1
        conn.commit()
    finally:
        conn.close()
    return upserted


def upsert_member_attendance_from_flagship_payload(
    meeting_id: int,
    items: List[Dict[str, Any]],
    member_id_remap: Dict[int, int],
) -> int:
    """UPSERT member_attendance rows via UNIQUE(member_id, meeting_id)."""
    if not items:
        return 0
    conn = get_connection()
    cursor = conn.cursor()
    upserted = 0
    try:
        for it in items:
            member_id = _remap_member_id(it.get('member_id'), member_id_remap)
            if member_id is None:
                continue
            cursor.execute(
                """
                INSERT INTO member_attendance
                    (member_id, meeting_id, status, notes, recorded_at)
                VALUES (?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
                ON CONFLICT(member_id, meeting_id) DO UPDATE SET
                    status = excluded.status,
                    notes = excluded.notes,
                    recorded_at = excluded.recorded_at
                """,
                (
                    member_id, meeting_id,
                    it.get('status'), it.get('notes'),
                    it.get('recorded_at'),
                ),
            )
            upserted += 1
        conn.commit()
    finally:
        conn.close()
    return upserted


def upsert_member_quotes_legacy_from_flagship_payload(
    meeting_id: int,
    items: List[Dict[str, Any]],
    member_id_remap: Dict[int, int],
) -> int:
    """DELETE-then-INSERT for the legacy `member_quotes` table. The cloud is
    not an independent writer here (it only receives syncs), so wiping
    pre-existing rows for this meeting is safe and matches the local-side
    `save_member_quotes_batch` pattern. Retire this once Chunk 9 removes the
    legacy table entirely.
    """
    if not items:
        return 0
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM member_quotes WHERE meeting_id = ?", (meeting_id,))
        for it in items:
            member_id = _remap_member_id(it.get('member_id'), member_id_remap)
            if member_id is None:
                continue
            cursor.execute(
                """
                INSERT INTO member_quotes (
                    member_id, meeting_id, quote_text, topic_tags,
                    minutes_page_ref, video_timestamp_seconds, proof_clip_url,
                    verified_status, extracted_at, word_timings,
                    quote_text_original, gemini_correction_notes,
                    verified_by, verified_at
                ) VALUES (
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, COALESCE(?, CURRENT_TIMESTAMP), ?,
                    ?, ?,
                    ?, ?
                )
                """,
                (
                    member_id, meeting_id, it.get('quote_text'), it.get('topic_tags'),
                    it.get('minutes_page_ref'), it.get('video_timestamp_seconds'),
                    it.get('proof_clip_url'),
                    it.get('verified_status') or 'pending', it.get('extracted_at'),
                    it.get('word_timings'),
                    it.get('quote_text_original'),
                    it.get('gemini_correction_notes'),
                    operator_identity.coerce_optional_role_identity(
                        it.get('verified_by')
                    ),
                    it.get('verified_at'),
                ),
            )
        conn.commit()
        return len(items)
    finally:
        conn.close()


def upsert_tracked_claims_from_flagship_payload(
    meeting_id: int,
    items: List[Dict[str, Any]],
    member_id_remap: Dict[int, int],
) -> int:
    """DELETE-then-INSERT for `tracked_claims`. Same pattern as
    `save_tracked_claims_batch` locally — the table has no stable natural
    key, and the sender is single-writer."""
    if not items:
        return 0
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM tracked_claims WHERE meeting_id = ?", (meeting_id,))
        for it in items:
            member_id = _remap_member_id(it.get('member_id'), member_id_remap)
            if member_id is None:
                continue
            cursor.execute(
                """
                INSERT INTO tracked_claims (
                    member_id, meeting_id, claim_type, claim_text,
                    expected_outcome, time_horizon_months, topic_tags,
                    confidence, context, word_timings,
                    status, status_updated_at, status_updated_by, status_evidence,
                    extracted_at
                ) VALUES (
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    COALESCE(?, CURRENT_TIMESTAMP)
                )
                """,
                (
                    member_id, meeting_id, it.get('claim_type'), it.get('claim_text'),
                    it.get('expected_outcome'), it.get('time_horizon_months'),
                    it.get('topic_tags'), it.get('confidence'), it.get('context'),
                    it.get('word_timings'),
                    it.get('status') or 'active',
                    it.get('status_updated_at'), it.get('status_updated_by'),
                    it.get('status_evidence'), it.get('extracted_at'),
                ),
            )
        conn.commit()
        return len(items)
    finally:
        conn.close()


# NOTE: `clear_meeting_notebook_id` (NULLed meeting.notebook_id after a
# notebook was GC'd) was removed per D-143 (NotebookLM subsystem
# removal 2026-07-01). The notebook_id column on meetings is preserved
# for historical provenance but never written to going forward.


# ─────────────────────────────────────────────────────────────────
# Review-gate approval helpers (DECISIONS.md § D-032)
# ─────────────────────────────────────────────────────────────────

def approve_work_order(
    work_order_id: int,
    approved_by: str,
    verified_quote_ids: Optional[List[str]] = None,
    actor_user_id: Optional[int] = None,
    event_key: Optional[str] = None,
) -> Optional[Dict]:
    """Mark a work order as approved (Gate 1 + Gate 2 both passed) and record
    each per-quote verification.

    Idempotent on the work_order side (re-approving sets approved_at to the
    new time + approved_by to the new approver). Per-quote verifications are
    UNIQUE on (work_order_id, quote_id) so duplicates are silently ignored —
    we only record the first verification of each quote per WO.

    Returns the updated work order row as a dict, or None if no such WO.
    """
    approved_by = operator_identity.coerce_role_identity(approved_by)
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, meeting_id FROM work_orders WHERE id = ?", (work_order_id,))
        row = cursor.fetchone()
        if not row:
            return None
        meeting_id = row['meeting_id']

        cursor.execute(
            """
            UPDATE work_orders
            SET approved_at = CURRENT_TIMESTAMP,
                approved_by = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (approved_by, work_order_id),
        )

        for quote_id in (verified_quote_ids or []):
            try:
                cursor.execute(
                    """
                    INSERT INTO quote_verifications
                        (work_order_id, meeting_id, quote_id, verified_by)
                    VALUES (?, ?, ?, ?)
                    """,
                    (work_order_id, meeting_id, quote_id, approved_by),
                )
            except sqlite3.IntegrityError:
                # Already verified for this WO. Audit-only insert — silent ignore.
                pass

        if actor_user_id is not None:
            if not event_key:
                raise ValueError("event_key is required when actor_user_id is supplied")
            approved_at = cursor.execute(
                "SELECT approved_at FROM work_orders WHERE id = ?", (work_order_id,)
            ).fetchone()["approved_at"]
            cursor.execute(
                """
                INSERT INTO operator_review_events (
                    event_key, action, meeting_id, work_order_id,
                    actor_user_id, occurred_at
                ) VALUES (?, 'approve', ?, ?, ?, ?)
                """,
                (
                    event_key, meeting_id, work_order_id,
                    actor_user_id, approved_at,
                ),
            )

        conn.commit()

        cursor.execute("SELECT * FROM work_orders WHERE id = ?", (work_order_id,))
        updated = cursor.fetchone()
        return dict(updated) if updated else None
    finally:
        conn.close()


def list_quote_verifications(work_order_id: int) -> List[Dict]:
    """List per-quote verification rows for a WO, in the order they were verified."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT id, work_order_id, meeting_id, quote_id, verified_at, verified_by
        FROM quote_verifications
        WHERE work_order_id = ?
        ORDER BY verified_at ASC
        """,
        (work_order_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── T-013 V4 — Disputed-quotes resolution (operator review surface) ──


_DISPUTED_RESOLVE_ACTIONS = {"verify", "reject"}


def list_disputed_quotes(city_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return every `quotes` row with `verified_status='disputed'`,
    joined to meeting + Gemini-verdict audit. Powers the operator's
    DisputedQuotesPage.

    Reads from the unified `quotes` table (post-D-052 refactor 2026-05-26).
    Speaker fields (speaker_name, speaker_role, speaker_class) are
    denormalized on the table — no JOIN with council_members needed for
    the basic shape, but a LEFT JOIN is included so council_member quotes
    can surface seat_id (used by Cast-page cross-link in the UI).
    Staff + external speakers (member_id IS NULL) get seat_id=NULL via
    the LEFT JOIN — correct shape; the UI hides Cast-link controls when
    member_id is null.

    Disputed quotes are hidden from public Cast page + BroadcastPage
    (the existing T-013 V4 filter `verified_status NOT IN ('rejected',
    'disputed')` excludes them) until a human operator either accepts
    them as verified (optionally after editing the text) or rejects.
    """
    where = "WHERE q.verified_status = 'disputed'"
    params: List[Any] = []
    if city_name:
        where += " AND m.city_name = ?"
        params.append(city_name)

    conn = get_connection()
    rows = conn.execute(
        f"""
        SELECT q.id, q.quote_text, q.quote_text_original, q.quote_text_display,
               q.verdict_emphasis_tokens,
               q.topic_tags,
               q.word_timings, q.gemini_correction_notes,
               q.video_timestamp_seconds, q.verified_status,
               q.verified_by, q.verified_at,
               q.member_id, q.speaker_name, q.speaker_role, q.speaker_class,
               q.is_broadcast_hero, q.context,
               q.agent_proposed_quote_text, q.agent_reasoning,
               q.agent_proposed_by, q.agent_proposed_at,
               cm.seat_id,
               m.id AS meeting_id, m.meeting_title, m.meeting_date, m.city_name,
               COALESCE(wo.youtube_video_url, m.video_url) AS meeting_video_url
        FROM quotes q
        LEFT JOIN council_members cm ON cm.id = q.member_id
        JOIN meetings m ON m.id = q.meeting_id
        LEFT JOIN work_orders wo ON wo.meeting_id = m.id
        {where}
        ORDER BY m.meeting_date DESC, q.id ASC
        """,
        params,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_quote_display_cache(
    quote_id: int,
    quote_text_display: Optional[str] = None,
    verdict_emphasis_tokens: Optional[List[str]] = None,
) -> None:
    """Persist lazy-computed display fields for a quote.

    Called by the `/api/disputed-quotes` endpoint after a polish / emphasis
    pass so subsequent reads are instant. Either argument may be passed
    independently — None means "leave column unchanged."

    `verdict_emphasis_tokens` is serialized to a JSON array string before
    storage so the column matches the convention of other JSON-bearing
    columns on the `quotes` table (topic_tags, word_timings, gemini_correction_notes).
    """
    sets: List[str] = []
    params: List[Any] = []
    if quote_text_display is not None:
        sets.append("quote_text_display = ?")
        params.append(quote_text_display)
    if verdict_emphasis_tokens is not None:
        sets.append("verdict_emphasis_tokens = ?")
        params.append(json.dumps(verdict_emphasis_tokens, ensure_ascii=False))
    if not sets:
        return
    params.append(quote_id)
    conn = get_connection()
    try:
        conn.execute(
            f"UPDATE quotes SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        conn.commit()
    finally:
        conn.close()


def resolve_disputed_quote(
    quote_id: int,
    action: str,
    quote_text: Optional[str] = None,
    resolver_notes: Optional[str] = None,
    resolved_by: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Operator-triggered resolution of a disputed `quotes` row.

    Reads/writes the unified `quotes` table (post-D-052). The actual UPDATE
    routes through `update_quote_verification` so content_hash recomputation
    + word_timings NULLing on text correction land in one place.

    `action='verify'` → verified_status='verified'. If `quote_text` is
    provided AND differs from the current text, it replaces the row's
    text AND nulls word_timings (the caller is expected to re-run
    `align_quotes_for_meeting` so karaoke matches the corrected display
    tokens — same pattern as the T-013 V3 stale-alignment fix).

    `action='reject'` → verified_status='rejected'. `quote_text` is
    ignored — rejected quotes don't surface publicly so editing the
    text is moot.

    The resolution is recorded inline in `gemini_correction_notes` as
    an `operator_resolution` sub-field so the original Gemini audit
    stays intact. `verified_by` and `verified_at` are overwritten with
    the operator's info.

    Returns the updated row dict, or None if `quote_id` doesn't exist
    or isn't currently disputed (refuses to resolve a non-disputed
    quote — that path is for a different surface).
    """
    action = (action or "").strip().lower()
    if action not in _DISPUTED_RESOLVE_ACTIONS:
        raise ValueError(
            f"action must be one of {sorted(_DISPUTED_RESOLVE_ACTIONS)}, got {action!r}"
        )

    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT id, quote_text, gemini_correction_notes, verified_status
            FROM quotes WHERE id = ?
            """,
            (quote_id,),
        ).fetchone()
        if not row:
            return None
        if row["verified_status"] != "disputed":
            raise ValueError(
                f"quote {quote_id} is in state {row['verified_status']!r}, not 'disputed'"
            )

        new_status = "verified" if action == "verify" else "rejected"
        text_changed = False
        new_text: Optional[str] = None  # None means "no text change" for the helper
        if action == "verify" and quote_text is not None:
            cleaned = quote_text.strip()
            if cleaned and cleaned != (row["quote_text"] or ""):
                new_text = cleaned
                text_changed = True

        # Append the operator resolution to the Gemini audit JSON so the
        # original verdict + the human decision are both preserved.
        try:
            audit = json.loads(row["gemini_correction_notes"]) if row["gemini_correction_notes"] else {}
        except (json.JSONDecodeError, TypeError):
            audit = {}
        from datetime import datetime, timezone
        audit["operator_resolution"] = {
            "action": action,
            "resolved_to_status": new_status,
            "quote_text_edited": text_changed,
            "resolver_notes": (resolver_notes or "").strip() or None,
            "resolved_by": (resolved_by or "").strip() or None,
            "resolved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    finally:
        conn.close()

    # Single write path via the helper. Handles content_hash recomputation
    # + word_timings NULLing automatically when corrected_quote_text is set.
    update_quote_verification(
        quote_id=quote_id,
        verified_status=new_status,
        verified_by=resolved_by or "operator",
        gemini_correction_notes=audit,
        corrected_quote_text=new_text,
    )

    # Fetch + return the updated row
    conn = get_connection()
    try:
        # S-005: a rejected quote can no longer be a broadcast hero. Clear the
        # flag here so rejected-but-hero rows don't silently block the publish
        # gate (drift surfaced 2026-05-28 by m101091's quotes 42/43/45).
        if action == "reject":
            conn.execute(
                "UPDATE quotes SET is_broadcast_hero = 0 WHERE id = ?",
                (quote_id,),
            )
            conn.commit()
        out = conn.execute(
            """
            SELECT id, member_id, meeting_id, quote_text, quote_text_original,
                   topic_tags, word_timings, video_timestamp_seconds,
                   verified_status, verified_by, verified_at,
                   gemini_correction_notes
            FROM quotes WHERE id = ?
            """,
            (quote_id,),
        ).fetchone()
    finally:
        conn.close()
    result = dict(out) if out else None
    if result is not None:
        result["_text_changed"] = text_changed
        result["_word_timings_invalidated"] = text_changed
        # D-062 backlog hygiene: resolving the quote clears its escalation so
        # resolved work doesn't orphan in the backlog / inflate the badge. This
        # is the single choke point — both the website and the Slack ✨ apply
        # path route through here, so both surfaces are covered.
        acknowledge_escalations_for(
            f"quotes.id={quote_id}", acknowledged_by=f"auto:quote-{new_status}"
        )
    return result


def record_agent_quote_counter_proposal(
    quote_id: int,
    proposed_quote_text: str,
    reasoning: Optional[str] = None,
    agent_role: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """D-057 extension — record an agent's counter-proposal on a disputed
    quote. Mirrors `record_agent_counter_proposal` (vocab corrections).

    Used by the Disputed Quotes Reviewer (and any future Opus judgment
    agent on `quotes`) when the cleaner+verifier output is wrong but the
    agent has a defensible better alternative — e.g. preserving a
    cautionary preamble the cleaner stripped (the m101091 quote 46 / D-056
    case). The agent calls this BEFORE escalating; the DisputedQuotesPage
    UI surfaces both proposals; the Slack `:sparkles:` reaction applies
    the agent's value directly via `resolve_disputed_quote(action='verify',
    quote_text=<agent value>)`.

    Repeated calls UPDATE the row (most-recent agent proposal wins). Pass
    an empty `proposed_quote_text` to clear a prior proposal.

    Refuses to record against a non-disputed quote (the action shape only
    makes sense for the disputed lane; verified/rejected/pending quotes
    aren't in the agent's queue). Returns the updated row dict, or None
    if `quote_id` doesn't exist.
    """
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT id, verified_status FROM quotes WHERE id = ?",
            (quote_id,),
        ).fetchone()
        if existing is None:
            return None
        if existing["verified_status"] != "disputed":
            raise ValueError(
                f"quote {quote_id} is in state {existing['verified_status']!r}, "
                f"not 'disputed' — counter-proposals only apply to the "
                f"disputed lane"
            )

        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE quotes
            SET agent_proposed_quote_text = ?,
                agent_reasoning = ?,
                agent_proposed_by = ?,
                agent_proposed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (proposed_quote_text, reasoning, agent_role, quote_id),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT id, meeting_id, member_id, speaker_name, speaker_role,
                   speaker_class, quote_text, quote_text_original,
                   quote_text_display, verified_status, verified_by, verified_at,
                   agent_proposed_quote_text, agent_reasoning,
                   agent_proposed_by, agent_proposed_at
            FROM quotes
            WHERE id = ?
            """,
            (quote_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ── T-012 — Tracked Claims Ledger ────────────────────────────────────


_TRACKED_CLAIM_STATUSES = {
    "active", "fulfilled", "broken", "withdrawn", "unclear"
}
_TRACKED_CLAIM_TYPES = {
    "assurance", "commitment", "prediction", "promise"
}


def save_tracked_claims_batch(
    meeting_id: int, city_name: str, items: List[Dict]
) -> Dict[str, int]:
    """Replace this meeting's `tracked_claims` rows with a fresh extraction.

    Mirrors `save_member_quotes_batch`'s shape: bulk-replace per meeting
    (re-runs can yield different counts), resolve speaker → member_id via
    the canonical roster, skip rows whose speaker isn't in the roster.

    `items` is the JSON `tracked_claims` array from the prompt:
        [{speaker, claim_type, claim_text, expected_outcome,
          time_horizon_months, topic_tags[], confidence, context?}, ...]
    """
    if items is None:
        return {"saved": 0, "skipped_unknown_member": 0, "skipped_invalid": 0}

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM tracked_claims WHERE meeting_id = ?", (meeting_id,))

    saved = 0
    skipped_unknown = 0
    skipped_invalid = 0
    for it in items:
        if not isinstance(it, dict):
            skipped_invalid += 1
            continue
        speaker = (it.get("speaker") or "").strip()
        claim_text = (it.get("claim_text") or "").strip()
        if not speaker or not claim_text:
            skipped_invalid += 1
            continue
        member_id = _lookup_member_id_via_cursor(cursor, city_name, speaker)
        if member_id is None:
            skipped_unknown += 1
            continue

        claim_type = (it.get("claim_type") or "").strip().lower() or None
        if claim_type and claim_type not in _TRACKED_CLAIM_TYPES:
            # Don't drop the row — store the raw value so the operator can
            # see what the extractor produced. Frontend can render it as-is.
            pass

        topic_tags = it.get("topic_tags")
        if isinstance(topic_tags, list):
            topic_tags_str = json.dumps(topic_tags)
        elif isinstance(topic_tags, str):
            topic_tags_str = topic_tags
        else:
            topic_tags_str = None

        confidence = (it.get("confidence") or "").strip().lower() or None
        context = (it.get("context") or "").strip() or None
        expected_outcome = (it.get("expected_outcome") or "").strip() or None

        horizon = it.get("time_horizon_months")
        if horizon is not None:
            try:
                horizon = int(horizon)
            except (TypeError, ValueError):
                horizon = None

        cursor.execute(
            """
            INSERT INTO tracked_claims (
                member_id, meeting_id, claim_type, claim_text,
                expected_outcome, time_horizon_months, topic_tags,
                confidence, context, status, extracted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', CURRENT_TIMESTAMP)
            """,
            (
                member_id, meeting_id, claim_type, claim_text,
                expected_outcome, horizon, topic_tags_str,
                confidence, context,
            ),
        )
        saved += 1
    conn.commit()
    conn.close()
    return {
        "saved": saved,
        "skipped_unknown_member": skipped_unknown,
        "skipped_invalid": skipped_invalid,
    }


# ── Conversational Compiler IR (S-023 / CONVERSATIONAL_COMPILER_SPEC) ──

_MOTION_TYPES = {"procedural", "substantive"}
_COMPILER_PARSER_MODEL_MOTIONS = "notebooklm:motions.md@v1"


def save_motions_batch(
    meeting_id: int, city_name: str, items: List[Dict]
) -> Dict[str, int]:
    """Persist a NotebookLM motions extraction into transcript_nodes.

    Per CONVERSATIONAL_COMPILER_SPEC § Node types: each motion becomes
    a `transcript_nodes` row with `node_type='Motion'` and `typed_fields`
    JSON carrying `{summary_sentence, motion_text, motion_type}`. Mirrors
    the `save_tracked_claims_batch` pattern (Decision #8a — same shape,
    different table + node_type).

    Idempotent per-meeting: replaces existing Motion-typed rows for this
    meeting on each call (matches save_tracked_claims_batch's
    bulk-replace semantics). Vote / AgendaTransition / other-typed nodes
    for the same meeting are preserved (only `node_type='Motion'` rows
    get wiped).

    `items` is the JSON `motions` array from prompts/motions.md:
        [{speaker, motion_text, motion_type, summary_sentence,
          agenda_item?, context?}, ...]

    Returns: {saved, skipped_unknown_member, skipped_invalid}

    Caveats / V0 simplifications:
      - `ordinal` is assigned sequentially within the motions extraction
        (1..N). NOT chronologically correct relative to other node types
        — compute proper interleaving in a later chunk when multiple
        node types are populated. For Track B Chunk B-1 (motions only)
        this is fine; the compiler page doesn't need cross-type ordering
        until SPEC build seq item 5 (scroll-sync).
      - `audio_offset_seconds` / `audio_duration_seconds` are NULL.
        NotebookLM doesn't expose absolute timestamps in its citations;
        cross-referencing motion_text with the persisted transcript_words
        for timing is a separate enhancement (deferred).
      - `parent_node_id` is NULL (no logical-block parent yet — that's
        Decision #2's layered abstraction, which requires AgendaTransition
        nodes to anchor the parents).
    """
    if items is None:
        return {"saved": 0, "skipped_unknown_member": 0, "skipped_invalid": 0}

    conn = get_connection()
    cursor = conn.cursor()

    # Bulk-replace ONLY Motion-typed rows for this meeting (preserve
    # other node types so the parser pipeline can populate them
    # independently without each new prompt wiping the others).
    cursor.execute(
        "DELETE FROM transcript_nodes "
        "WHERE meeting_id = ? AND node_type = 'Motion'",
        (meeting_id,),
    )

    saved = 0
    skipped_unknown = 0
    skipped_invalid = 0
    for index, it in enumerate(items):
        if not isinstance(it, dict):
            skipped_invalid += 1
            continue
        speaker = (it.get("speaker") or "").strip()
        motion_text = (it.get("motion_text") or "").strip()
        if not speaker or not motion_text:
            skipped_invalid += 1
            continue

        member_id = _lookup_member_id_via_cursor(cursor, city_name, speaker)
        if member_id is None:
            # Per the motions.md prompt's strictness rule: NotebookLM
            # is supposed to drop non-canonical speakers itself, but
            # belt-and-suspenders here — skip rather than insert a
            # misattributed motion.
            skipped_unknown += 1
            continue

        motion_type = (it.get("motion_type") or "").strip().lower()
        if motion_type not in _MOTION_TYPES:
            # Don't drop — store whatever the extractor returned. Operator
            # can correct via the Edit menu (future SPEC § Future-option
            # note). Same forgiveness as save_tracked_claims_batch.
            pass

        summary_sentence = (it.get("summary_sentence") or "").strip() or None
        agenda_item = (it.get("agenda_item") or "").strip() or None
        context = (it.get("context") or "").strip() or None

        typed_fields = json.dumps({
            "summary_sentence": summary_sentence,
            "motion_text": motion_text,
            "motion_type": motion_type or None,
            "agenda_item": agenda_item,
            "context": context,
        }, ensure_ascii=False)

        cursor.execute(
            """
            INSERT INTO transcript_nodes (
                meeting_id, ordinal,
                audio_offset_seconds, audio_duration_seconds,
                speaker_id, speaker_name,
                transcript_span_text, word_timings,
                node_type, typed_fields,
                parser_model, parser_confidence,
                parser_ran_at, parent_node_id
            ) VALUES (
                ?, ?,
                NULL, NULL,
                ?, ?,
                ?, NULL,
                'Motion', ?,
                ?, NULL,
                CURRENT_TIMESTAMP, NULL
            )
            """,
            (
                meeting_id, index + 1,
                member_id, speaker,
                motion_text,
                typed_fields,
                _COMPILER_PARSER_MODEL_MOTIONS,
            ),
        )
        saved += 1
    conn.commit()
    conn.close()
    return {
        "saved": saved,
        "skipped_unknown_member": skipped_unknown,
        "skipped_invalid": skipped_invalid,
    }


_VOTE_RESULTS = {"passed", "failed", "tabled", "withdrawn", "tied"}
_VOTE_METHODS = {"voice", "roll_call", "unanimous_consent", "none"}
_PER_MEMBER_VOTES = {"aye", "nay", "abstain", "absent", "recused"}
_COMPILER_PARSER_MODEL_VOTES = "notebooklm:votes.md@v1"


def save_votes_batch(
    meeting_id: int, city_name: str, items: List[Dict]
) -> Dict[str, int]:
    """Persist a NotebookLM votes extraction into transcript_nodes.

    Per CONVERSATIONAL_COMPILER_SPEC § Node types: each vote becomes
    a `transcript_nodes` row with `node_type='Vote'` and `typed_fields`
    JSON carrying {summary_sentence, motion_reference, vote_result,
    vote_method, per_member_votes, tally, agenda_item, context}.
    Sibling to save_motions_batch.

    Idempotent per-meeting: replaces existing Vote-typed rows for this
    meeting on each call (preserves other node types for the same
    meeting — Motion / AgendaTransition / etc.).

    `items` is the JSON `votes` array from prompts/votes.md:
        [{motion_reference, summary_sentence, vote_result, vote_method,
          per_member_votes: [{member, vote}, ...], tally, agenda_item?,
          context?}, ...]

    Returns: {saved, skipped_invalid}. (No skipped_unknown_member —
    Vote nodes don't have a single speaker; per-member name resolution
    happens lazily when the frontend reads. Unresolvable member names
    in per_member_votes are kept verbatim in the JSON.)

    Caveats (same as save_motions_batch):
      - ordinal sequential within the votes extraction (1..N); not
        chronologically interleaved with Motion / AgendaTransition.
      - audio_offset / duration NULL (no NotebookLM timestamps).
      - speaker_id / speaker_name NULL (body action, not individual
        utterance; per_member_votes carries the per-member info).
      - parent_node_id NULL (no AgendaTransition parent yet).
      - The link to the originating Motion (responds_to edge) is NOT
        created here — that's a constraint-checker pass that reads
        typed_fields.motion_reference and matches against Motion
        nodes in the same meeting. Deferred to a later chunk.
    """
    if items is None:
        return {"saved": 0, "skipped_invalid": 0}

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "DELETE FROM transcript_nodes "
        "WHERE meeting_id = ? AND node_type = 'Vote'",
        (meeting_id,),
    )

    saved = 0
    skipped_invalid = 0
    for index, it in enumerate(items):
        if not isinstance(it, dict):
            skipped_invalid += 1
            continue
        vote_result = (it.get("vote_result") or "").strip().lower()
        motion_reference = (it.get("motion_reference") or "").strip()
        if not vote_result or not motion_reference:
            # Need at minimum a result + a motion reference; otherwise
            # the row is uninterpretable.
            skipped_invalid += 1
            continue
        if vote_result not in _VOTE_RESULTS:
            # Don't drop — keep the raw value so the operator sees the
            # extractor's output and can correct via Edit menu.
            pass

        vote_method = (it.get("vote_method") or "").strip().lower() or None
        if vote_method and vote_method not in _VOTE_METHODS:
            pass  # same forgiveness as vote_result

        # Per-member votes: keep verbatim. Member-name → member_id
        # resolution is lazy (frontend does it on read so re-canonicalizing
        # the roster doesn't require re-extracting). We DO normalize the
        # vote field to lowercase for consistency.
        per_member_raw = it.get("per_member_votes")
        per_member = []
        if isinstance(per_member_raw, list):
            for pm in per_member_raw:
                if not isinstance(pm, dict):
                    continue
                member = (pm.get("member") or "").strip()
                vote = (pm.get("vote") or "").strip().lower()
                if not member or not vote:
                    continue
                if vote not in _PER_MEMBER_VOTES:
                    pass  # keep raw — operator sees + can correct
                per_member.append({"member": member, "vote": vote})

        tally = it.get("tally") if isinstance(it.get("tally"), dict) else None
        summary_sentence = (it.get("summary_sentence") or "").strip() or None
        agenda_item = (it.get("agenda_item") or "").strip() or None
        context = (it.get("context") or "").strip() or None

        typed_fields = json.dumps({
            "summary_sentence": summary_sentence,
            "motion_reference": motion_reference,
            "vote_result": vote_result,
            "vote_method": vote_method,
            "per_member_votes": per_member,
            "tally": tally,
            "agenda_item": agenda_item,
            "context": context,
        }, ensure_ascii=False)

        # transcript_span_text: use the summary_sentence as the
        # human-readable span (since the vote isn't a single utterance).
        # Falls back to motion_reference if no summary.
        span_text = summary_sentence or motion_reference

        cursor.execute(
            """
            INSERT INTO transcript_nodes (
                meeting_id, ordinal,
                audio_offset_seconds, audio_duration_seconds,
                speaker_id, speaker_name,
                transcript_span_text, word_timings,
                node_type, typed_fields,
                parser_model, parser_confidence,
                parser_ran_at, parent_node_id
            ) VALUES (
                ?, ?,
                NULL, NULL,
                NULL, NULL,
                ?, NULL,
                'Vote', ?,
                ?, NULL,
                CURRENT_TIMESTAMP, NULL
            )
            """,
            (
                meeting_id, index + 1,
                span_text,
                typed_fields,
                _COMPILER_PARSER_MODEL_VOTES,
            ),
        )
        saved += 1
    conn.commit()
    conn.close()
    return {
        "saved": saved,
        "skipped_invalid": skipped_invalid,
    }


_COMPILER_PARSER_MODEL_AGENDA_TRANSITIONS = "notebooklm:agenda_transitions.md@v1"


def save_agenda_transitions_batch(
    meeting_id: int, city_name: str, items: List[Dict]
) -> Dict[str, int]:
    """Persist a NotebookLM agenda_transitions extraction into transcript_nodes.

    Per CONVERSATIONAL_COMPILER_SPEC § Node types: each transition becomes
    a `transcript_nodes` row with `node_type='AgendaTransition'` and
    `typed_fields` JSON carrying `{summary_sentence, agenda_item_number,
    agenda_item_title, transition_text, context}`. Sibling to
    save_motions_batch / save_votes_batch.

    Per Decision #2 (SPEC): AgendaTransition nodes serve as logical-block
    parents for Motion / Vote / Commit_P nodes via parent_node_id. A
    separate post-processing pass (not in V0) backfills parent_node_id
    on Motion/Vote/Commit_P rows based on agenda-item match — this batch
    helper just creates the transition rows themselves.

    Idempotent per-meeting: replaces existing AgendaTransition-typed
    rows for this meeting on each call. Other node types preserved.

    `items` is the JSON `agenda_transitions` array from
    prompts/agenda_transitions.md:
        [{agenda_item_number, agenda_item_title, summary_sentence,
          chair_speaker, transition_text, context?}, ...]

    Returns: {saved, skipped_invalid}
      - No skipped_unknown_chair: the AgendaTransition is structurally
        present even when the chair_speaker can't be resolved (the
        transition is a body-level event, not a per-member action).
        Unresolvable chair names land as speaker_id=NULL with the
        denormalized name preserved on the row.
    """
    if items is None:
        return {"saved": 0, "skipped_invalid": 0}

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "DELETE FROM transcript_nodes "
        "WHERE meeting_id = ? AND node_type = 'AgendaTransition'",
        (meeting_id,),
    )

    saved = 0
    skipped_invalid = 0
    for index, it in enumerate(items):
        if not isinstance(it, dict):
            skipped_invalid += 1
            continue
        title = (it.get("agenda_item_title") or "").strip()
        transition_text = (it.get("transition_text") or "").strip()
        if not title and not transition_text:
            # Need at least a title or the chair's words; otherwise the
            # row is uninterpretable.
            skipped_invalid += 1
            continue

        chair_speaker = (it.get("chair_speaker") or "").strip()
        # Best-effort matcher resolution; not required for the row to land.
        chair_member_id = None
        if chair_speaker:
            chair_member_id = _lookup_member_id_via_cursor(
                cursor, city_name, chair_speaker
            )

        agenda_number = (it.get("agenda_item_number") or "").strip() or None
        summary_sentence = (it.get("summary_sentence") or "").strip() or None
        context = (it.get("context") or "").strip() or None

        typed_fields = json.dumps({
            "summary_sentence": summary_sentence,
            "agenda_item_number": agenda_number,
            "agenda_item_title": title or None,
            "transition_text": transition_text or None,
            "context": context,
            # Echo agenda_item in the same shape Motion/Vote use so the
            # constraint-checker's agenda-key extractor finds it via the
            # same path. "Item 4A - CDBG project" — matches the format
            # Motion/Vote typed_fields use.
            "agenda_item": (
                f"Item {agenda_number} - {title}"
                if agenda_number and title
                else (f"Item {agenda_number}" if agenda_number else title or None)
            ),
        }, ensure_ascii=False)

        # transcript_span_text: the chair's transition phrase is the
        # textual extent of the node (the transition IS the chair's words).
        span_text = transition_text or title or summary_sentence or "(unstated)"

        cursor.execute(
            """
            INSERT INTO transcript_nodes (
                meeting_id, ordinal,
                audio_offset_seconds, audio_duration_seconds,
                speaker_id, speaker_name,
                transcript_span_text, word_timings,
                node_type, typed_fields,
                parser_model, parser_confidence,
                parser_ran_at, parent_node_id
            ) VALUES (
                ?, ?,
                NULL, NULL,
                ?, ?,
                ?, NULL,
                'AgendaTransition', ?,
                ?, NULL,
                CURRENT_TIMESTAMP, NULL
            )
            """,
            (
                meeting_id, index + 1,
                chair_member_id, chair_speaker or None,
                span_text,
                typed_fields,
                _COMPILER_PARSER_MODEL_AGENDA_TRANSITIONS,
            ),
        )
        saved += 1
    conn.commit()
    conn.close()
    return {
        "saved": saved,
        "skipped_invalid": skipped_invalid,
    }


_COMPILER_PARSER_MODEL_SECONDS = "notebooklm:seconds.md@v1"


def save_seconds_batch(
    meeting_id: int, city_name: str, items: List[Dict]
) -> Dict[str, int]:
    """Persist a NotebookLM seconds extraction into transcript_nodes.

    Per CONVERSATIONAL_COMPILER_SPEC § Node types: each second becomes
    a `transcript_nodes` row with `node_type='Second'` and `typed_fields`
    JSON carrying `{summary_sentence, motion_reference, second_text,
    agenda_item, context}`. Completes the Motion → Second → Vote
    procedural triad.

    Idempotent per-meeting: replaces existing Second-typed rows for
    this meeting on each call. Other node types preserved.

    `items` is the JSON `seconds` array from prompts/seconds.md:
        [{speaker, motion_reference, summary_sentence, second_text,
          agenda_item?, context?}, ...]

    Returns: {saved, skipped_unknown_member, skipped_invalid}

    The link to the originating Motion (responds_to edge) is NOT
    created here — that's a constraint-checker pass that reads
    typed_fields.motion_reference and matches against Motion nodes in
    the same meeting (parallel to the existing Vote→Motion inference
    in edge_inference.py).
    """
    if items is None:
        return {"saved": 0, "skipped_unknown_member": 0, "skipped_invalid": 0}

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "DELETE FROM transcript_nodes "
        "WHERE meeting_id = ? AND node_type = 'Second'",
        (meeting_id,),
    )

    saved = 0
    skipped_unknown = 0
    skipped_invalid = 0
    for index, it in enumerate(items):
        if not isinstance(it, dict):
            skipped_invalid += 1
            continue
        speaker = (it.get("speaker") or "").strip()
        motion_reference = (it.get("motion_reference") or "").strip()
        if not speaker or not motion_reference:
            skipped_invalid += 1
            continue

        member_id = _lookup_member_id_via_cursor(cursor, city_name, speaker)
        if member_id is None:
            # Same precision-over-recall stance as save_motions_batch —
            # NotebookLM is supposed to drop non-canonical speakers per
            # seconds.md, but belt-and-suspenders here.
            skipped_unknown += 1
            continue

        second_text = (it.get("second_text") or "").strip()
        summary_sentence = (it.get("summary_sentence") or "").strip() or None
        agenda_item = (it.get("agenda_item") or "").strip() or None
        context = (it.get("context") or "").strip() or None

        typed_fields = json.dumps({
            "summary_sentence": summary_sentence,
            "motion_reference": motion_reference,
            "second_text": second_text or None,
            "agenda_item": agenda_item,
            "context": context,
        }, ensure_ascii=False)

        span_text = second_text or summary_sentence or motion_reference

        cursor.execute(
            """
            INSERT INTO transcript_nodes (
                meeting_id, ordinal,
                audio_offset_seconds, audio_duration_seconds,
                speaker_id, speaker_name,
                transcript_span_text, word_timings,
                node_type, typed_fields,
                parser_model, parser_confidence,
                parser_ran_at, parent_node_id
            ) VALUES (
                ?, ?,
                NULL, NULL,
                ?, ?,
                ?, NULL,
                'Second', ?,
                ?, NULL,
                CURRENT_TIMESTAMP, NULL
            )
            """,
            (
                meeting_id, index + 1,
                member_id, speaker,
                span_text,
                typed_fields,
                _COMPILER_PARSER_MODEL_SECONDS,
            ),
        )
        saved += 1
    conn.commit()
    conn.close()
    return {
        "saved": saved,
        "skipped_unknown_member": skipped_unknown,
        "skipped_invalid": skipped_invalid,
    }


def get_tracked_claims_for_member(
    member_id: int, status_filter: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """Return all tracked claims for a member, newest meeting first.

    `status_filter` (optional) is a list of statuses to include
    (e.g. `["active", "unclear"]`). Default: return all statuses so the
    Cast page surface can show the full history with status pills.
    """
    where = "WHERE tc.member_id = ?"
    params: List[Any] = [member_id]
    if status_filter:
        placeholders = ",".join("?" * len(status_filter))
        where += f" AND tc.status IN ({placeholders})"
        params.extend(status_filter)

    conn = get_connection()
    rows = conn.execute(
        f"""
        SELECT tc.id, tc.member_id, tc.meeting_id,
               tc.claim_type, tc.claim_text, tc.expected_outcome,
               tc.time_horizon_months, tc.topic_tags, tc.confidence,
               tc.context, tc.word_timings, tc.status,
               tc.status_updated_at, tc.status_updated_by, tc.status_evidence,
               tc.extracted_at,
               m.meeting_date, m.meeting_title, m.video_url,
               wo.youtube_video_url
        FROM tracked_claims tc
        JOIN meetings m ON m.id = tc.meeting_id
        LEFT JOIN work_orders wo ON wo.meeting_id = m.id
        {where}
        ORDER BY m.meeting_date DESC, tc.extracted_at DESC
        """,
        params,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_tracked_claims_for_city(
    city_name: str,
    status_filter: Optional[List[str]] = None,
    aged_past_horizon_only: bool = False,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """Return all tracked claims for a city for the City Ledger view.

    Joins to council_members (speaker name + seat_id) and meetings
    (meeting date + video URL) so the frontend can render the full card
    without N+1 lookups.

    `aged_past_horizon_only=True` filters to claims that are still
    `active` AND whose `extracted_at + time_horizon_months` is in the
    past — the actionable next-review feed.
    """
    where = "WHERE m.city_name = ?"
    params: List[Any] = [city_name]
    if status_filter:
        placeholders = ",".join("?" * len(status_filter))
        where += f" AND tc.status IN ({placeholders})"
        params.extend(status_filter)
    if aged_past_horizon_only:
        # extracted_at is local-time TIMESTAMP; add time_horizon_months
        # using SQLite's date math. NULL horizons never age.
        where += (
            " AND tc.status = 'active'"
            " AND tc.time_horizon_months IS NOT NULL"
            " AND datetime(tc.extracted_at, '+' || tc.time_horizon_months || ' months')"
            " < datetime('now')"
        )

    conn = get_connection()
    rows = conn.execute(
        f"""
        SELECT tc.id, tc.member_id, tc.meeting_id,
               tc.claim_type, tc.claim_text, tc.expected_outcome,
               tc.time_horizon_months, tc.topic_tags, tc.confidence,
               tc.context, tc.word_timings, tc.status,
               tc.status_updated_at, tc.status_updated_by, tc.status_evidence,
               tc.extracted_at,
               cm.name AS speaker_name, cm.seat_id, cm.role AS speaker_role,
               m.meeting_date, m.meeting_title, m.video_url,
               wo.youtube_video_url
        FROM tracked_claims tc
        JOIN council_members cm ON cm.id = tc.member_id
        JOIN meetings m ON m.id = tc.meeting_id
        LEFT JOIN work_orders wo ON wo.meeting_id = m.id
        {where}
        ORDER BY m.meeting_date DESC, tc.extracted_at DESC
        LIMIT ?
        """,
        params + [limit],
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Truth Book Lite (D-059 Layer 1) ────────────────────────────────────
#
# get_truth_book_for_member assembles one Cast member's full record for the
# per-person research surface spec'd in TRUTH_BOOK_LITE_SPEC.md: every
# publicly-visible quote grouped into per-topic swimlanes on a shared time
# axis, plus the member's tracked claims (the accountability layer). It
# renders ONLY existing data — no new extraction, no federation (that's the
# FORK_VISION_INTELLIGENCE fork, Layer 2). The visibility filter matches the
# Cast endpoint exactly (verified_status NOT IN ('rejected','disputed')).
#
# The featured-lane vocabulary derives from `parsers/topic_tags.py` — the
# canonical Python home of the controlled 5-tag vocabulary the Cast page
# and (Session-103 forward) the meeting topic-matcher both consume.
# Previously this was a third literal copy of the tag list plus an
# obsolete comment claiming `parsers/topic_tags.py` did not exist —
# caught by sol Round-2 audit 2026-07-30. Deriving from TOPIC_TAGS keeps
# the vocabulary single-source; a lane lands in every featured slot it's
# tagged with; quotes with no featured tag fall to the "other" lane.
# All featured lanes are emitted even when empty so the absence of
# activity on a topic is itself legible.

_TRUTH_BOOK_FEATURED_LANES = tuple(
    (tag_id, label) for tag_id, label, _hint in _TOPIC_TAGS
)
_TRUTH_BOOK_OTHER_LANE = ("other", "Other")


def get_truth_book_for_member(
    city_name: str, seat_id: str
) -> Optional[Dict[str, Any]]:
    """Assemble the Truth Book Lite record for one Cast member.

    Returns None when the member doesn't exist (the endpoint maps that to a
    404). Otherwise returns:

        {
          "member": {id, name, role, seat_id, term_started, term_ends, source_url},
          "time_range": {"earliest": <YYYY-MM-DD|None>, "latest": <...|None>},
          "lanes": [{"topic", "label", "entries": [quote-entry, ...]}, ...],
          "claims": [claim-entry, ...],
        }

    Quotes are filtered to this member's council_member-class rows that are
    publicly visible (not rejected/disputed) — identical to the Cast page.
    Tracked claims carry every status (active/fulfilled/broken/...) so the
    accountability arcs can show resolved outcomes.
    """

    def _json_list(v):
        if not v:
            return []
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    def _json_or_none(v):
        if not v:
            return None
        try:
            return json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return None

    def _ts_from_timings(word_timings):
        """First-word start time in whole seconds, or None — matches the
        Cast endpoint's derivation when the schema column is null."""
        if isinstance(word_timings, list) and word_timings:
            first = word_timings[0]
            if isinstance(first, dict) and "start_ms" in first:
                try:
                    return int(first["start_ms"]) // 1000
                except (TypeError, ValueError):
                    return None
        return None

    conn = get_connection()
    try:
        member_row = conn.execute(
            """
            SELECT id, name, role, seat_id, term_started, term_ends, source_url
            FROM council_members
            WHERE city_name = ? AND seat_id = ?
            """,
            (city_name, seat_id),
        ).fetchone()
        if member_row is None:
            return None
        member = dict(member_row)
        member_id = member["id"]

        quote_rows = conn.execute(
            """
            SELECT
                q.id, q.quote_text, q.topic_tags, q.context,
                q.is_broadcast_hero, q.video_timestamp_seconds, q.word_timings,
                q.verified_status, q.verified_by, q.verified_at,
                q.proof_clip_sha256, q.speaker_role,
                m.id AS meeting_id, m.meeting_title, m.meeting_date,
                COALESCE(wo.youtube_video_url, m.video_url) AS meeting_video_url
            FROM quotes q
            JOIN meetings m ON m.id = q.meeting_id
            LEFT JOIN work_orders wo ON wo.meeting_id = m.id
            WHERE q.member_id = ?
              AND q.speaker_class = 'council_member'
              AND q.verified_status NOT IN ('rejected', 'disputed')
            ORDER BY m.meeting_date, q.id
            """,
            (member_id,),
        ).fetchall()

        claim_rows = conn.execute(
            """
            SELECT
                tc.id, tc.claim_type, tc.claim_text, tc.expected_outcome,
                tc.time_horizon_months, tc.topic_tags, tc.confidence,
                tc.context, tc.word_timings, tc.status,
                tc.status_updated_at, tc.extracted_at,
                m.id AS meeting_id, m.meeting_title, m.meeting_date,
                COALESCE(wo.youtube_video_url, m.video_url) AS meeting_video_url
            FROM tracked_claims tc
            JOIN meetings m ON m.id = tc.meeting_id
            LEFT JOIN work_orders wo ON wo.meeting_id = m.id
            WHERE tc.member_id = ?
            ORDER BY m.meeting_date, tc.extracted_at
            """,
            (member_id,),
        ).fetchall()
    finally:
        conn.close()

    dates: List[str] = []

    # Group quotes into lanes by featured topic. A quote with multiple
    # featured tags appears on each matching lane; one with none lands in
    # "other". Lanes start empty and stay in the response even if unused.
    lane_entries: Dict[str, List[Dict[str, Any]]] = {
        lane_id: [] for lane_id, _ in _TRUTH_BOOK_FEATURED_LANES
    }
    lane_entries[_TRUTH_BOOK_OTHER_LANE[0]] = []
    featured_ids = {lane_id for lane_id, _ in _TRUTH_BOOK_FEATURED_LANES}

    for r in quote_rows:
        d = dict(r)
        tags = _json_list(d.get("topic_tags"))
        word_timings = _json_or_none(d.get("word_timings"))
        ts = d.get("video_timestamp_seconds")
        if ts is None:
            ts = _ts_from_timings(word_timings)
        if d.get("meeting_date"):
            dates.append(d["meeting_date"])
        entry = {
            "type": "quote",
            "quote_id": d["id"],
            "meeting_id": d["meeting_id"],
            "meeting_date": d.get("meeting_date"),
            "meeting_title": d.get("meeting_title"),
            "meeting_video_url": d.get("meeting_video_url"),
            "text": d.get("quote_text"),
            "context": d.get("context"),
            "topic_tags": tags,
            "word_timings": word_timings,
            "video_timestamp_seconds": ts,
            "verified_status": d.get("verified_status"),
            "verified_by": d.get("verified_by"),
            "verified_at": d.get("verified_at"),
            "proof_clip_sha256": d.get("proof_clip_sha256"),
            "is_broadcast_hero": d.get("is_broadcast_hero"),
            "speaker_role": d.get("speaker_role"),
        }
        featured_hits = [t for t in tags if t in featured_ids]
        if featured_hits:
            for t in featured_hits:
                lane_entries[t].append(entry)
        else:
            lane_entries[_TRUTH_BOOK_OTHER_LANE[0]].append(entry)

    lanes = [
        {"topic": lane_id, "label": label, "entries": lane_entries[lane_id]}
        for lane_id, label in (*_TRUTH_BOOK_FEATURED_LANES, _TRUTH_BOOK_OTHER_LANE)
    ]

    # The accountability layer. resolved_meeting_id is part of the documented
    # contract (TRUTH_BOOK_LITE_SPEC chunk 5 draws the claim→resolution
    # connector from it) but tracked_claims has no such column yet — emit
    # null until that schema lands.
    claims = []
    for r in claim_rows:
        d = dict(r)
        word_timings = _json_or_none(d.get("word_timings"))
        if d.get("meeting_date"):
            dates.append(d["meeting_date"])
        claims.append({
            "type": "claim",
            "claim_id": d["id"],
            "claim_type": d.get("claim_type"),
            "status": d.get("status"),
            "topic_tags": _json_list(d.get("topic_tags")),
            "meeting_id": d["meeting_id"],
            "meeting_date": d.get("meeting_date"),
            "meeting_title": d.get("meeting_title"),
            "meeting_video_url": d.get("meeting_video_url"),
            "claim_text": d.get("claim_text"),
            "expected_outcome": d.get("expected_outcome"),
            "time_horizon_months": d.get("time_horizon_months"),
            "confidence": d.get("confidence"),
            "context": d.get("context"),
            "word_timings": word_timings,
            "video_timestamp_seconds": _ts_from_timings(word_timings),
            "status_updated_at": d.get("status_updated_at"),
            "extracted_at": d.get("extracted_at"),
            "resolved_meeting_id": None,
        })

    return {
        "member": member,
        "time_range": {
            "earliest": min(dates) if dates else None,
            "latest": max(dates) if dates else None,
        },
        "lanes": lanes,
        "claims": claims,
    }


def update_tracked_claim_status(
    claim_id: int,
    new_status: str,
    status_evidence: Optional[str] = None,
    updated_by: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Operator-triggered status flip. Returns the updated row, or None
    if the id doesn't exist. Validates `new_status` against the enum
    (per T-012 V1 design — auto-verify is Phase 2). The status_evidence
    note is required-by-convention but not enforced at the DB level so
    the operator can transition a status while typing the evidence.
    """
    new_status = (new_status or "").strip().lower()
    if new_status not in _TRACKED_CLAIM_STATUSES:
        raise ValueError(
            f"new_status must be one of {sorted(_TRACKED_CLAIM_STATUSES)}, got {new_status!r}"
        )

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE tracked_claims SET
                status = ?,
                status_updated_at = CURRENT_TIMESTAMP,
                status_updated_by = ?,
                status_evidence = ?
            WHERE id = ?
            """,
            (new_status, updated_by, status_evidence, claim_id),
        )
        if cursor.rowcount == 0:
            return None
        conn.commit()
        row = conn.execute(
            """
            SELECT id, member_id, meeting_id, claim_type, claim_text,
                   expected_outcome, time_horizon_months, topic_tags,
                   confidence, context, word_timings, status,
                   status_updated_at, status_updated_by, status_evidence,
                   extracted_at
            FROM tracked_claims WHERE id = ?
            """,
            (claim_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ── T-017 Layer 2 — city vocabulary corrections ─────────────────────


def upsert_vocabulary_correction(
    city_name: str,
    wrong: str,
    right: str,
    source_response_file: Optional[str] = None,
) -> Dict[str, Any]:
    """Record one `wrong → right` proper-noun correction for a city.

    Idempotent: if `(city_name, wrong)` already exists, the row's
    `applied_count` is incremented and `last_applied_at` is bumped to
    now. If `right` differs from what was previously stored, the new
    `right` wins (most recent Gemini surface is the authoritative value).
    `first_observed_response_file` is preserved on conflict — it records
    the FIRST occurrence, not the most recent.

    Returns `{'id', 'was_new', 'applied_count', 'right'}`.
    """
    wrong = wrong.strip()
    right = right.strip()
    if not city_name or not wrong or not right:
        raise ValueError("city_name, wrong, and right must all be non-empty")

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO city_vocabulary_corrections
                (city_name, wrong, right, applied_count,
                 first_observed_response_file, last_applied_at)
            VALUES (?, ?, ?, 1, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(city_name, wrong) DO UPDATE SET
                applied_count = applied_count + 1,
                right = excluded.right,
                last_applied_at = CURRENT_TIMESTAMP
            RETURNING id, applied_count, right
            """,
            (city_name, wrong, right, source_response_file),
        )
        row = cursor.fetchone()
        conn.commit()
        return {
            "id": row["id"],
            "was_new": row["applied_count"] == 1,
            "applied_count": row["applied_count"],
            "right": row["right"],
        }
    finally:
        conn.close()


def enqueue_polish_rejection_candidate(
    city_name: str,
    original_text: str,
    polished_proposal: str,
    *,
    meeting_id: Optional[int] = None,
    quote_id: Optional[int] = None,
    wrong_token: Optional[str] = None,
    right_token: Optional[str] = None,
    is_phonetic_variant: bool = False,
    sibling_wrongs: Optional[List[str]] = None,
    source_type: str = "polish_rejection",
) -> Dict[str, Any]:
    """V1-Consensus-1 C4 — write a polish-rejection candidate to the consensus queue.

    Called when `quote_cleaner.polish_for_display` rejects a polishing
    proposal because the polisher reworded (safety check at line 357).
    The rejected polished_proposal becomes a candidate for the consensus
    pipeline; the vocabulary-curator's next heartbeat picks it up.

    `wrong_token` / `right_token` are the extracted single-token diff
    when available (from a lightweight tokenizer in consensus_vocab);
    null when the diff was non-trivial and the curator should compute
    it at processing time.

    Returns dict with `id` (new row), `created` (bool — True for new
    INSERT, False if a near-duplicate already pending), `status`.
    """
    if not city_name or not original_text or not polished_proposal:
        raise ValueError(
            "city_name, original_text, polished_proposal all required"
        )

    conn = get_connection()
    try:
        # Idempotency check: if a near-identical pending row already exists
        # for the same (meeting_id, quote_id, original_text), don't double-enqueue.
        existing = conn.execute(
            """
            SELECT id, status FROM correction_pending_review
            WHERE city_name = ?
              AND COALESCE(meeting_id, -1) = COALESCE(?, -1)
              AND COALESCE(quote_id, -1) = COALESCE(?, -1)
              AND original_text = ?
              AND polished_proposal = ?
              AND status = 'pending'
            LIMIT 1
            """,
            (city_name, meeting_id, quote_id, original_text, polished_proposal),
        ).fetchone()
        if existing:
            return {
                "id": existing["id"],
                "created": False,
                "status": existing["status"],
            }

        sibling_json = (
            json.dumps(sibling_wrongs) if sibling_wrongs else None
        )
        cursor = conn.execute(
            """
            INSERT INTO correction_pending_review
                (city_name, meeting_id, quote_id, source_type,
                 original_text, polished_proposal,
                 wrong_token, right_token,
                 is_phonetic_variant, sibling_wrongs_json, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                city_name, meeting_id, quote_id, source_type,
                original_text, polished_proposal,
                wrong_token, right_token,
                1 if is_phonetic_variant else 0, sibling_json,
            ),
        )
        conn.commit()
        return {
            "id": cursor.lastrowid,
            "created": True,
            "status": "pending",
        }
    finally:
        conn.close()


def get_pending_review_row(row_id: int) -> Optional[Dict[str, Any]]:
    """V1-Consensus-1 C5 — fetch a single pending-review row by id."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM correction_pending_review WHERE id = ?",
            (row_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def resolve_pending_review(
    row_id: int,
    status: str,
    *,
    codex_proposed_right: Optional[str] = None,
    codex_confidence: Optional[str] = None,
    codex_reasoning: Optional[str] = None,
    curator_proposed_right: Optional[str] = None,
    curator_reasoning: Optional[str] = None,
    prong_1_passed: Optional[bool] = None,
    prong_1_reasoning: Optional[str] = None,
    prong_1_evidence: Optional[List[str]] = None,
    prong_2_passed: Optional[bool] = None,
    prong_2_reasoning: Optional[str] = None,
    resolution_action: Optional[str] = None,
    resolved_by: str = "codex-opus-consensus",
) -> Optional[Dict[str, Any]]:
    """V1-Consensus-1 C5 — stamp resolution onto a pending-review row.

    `status` is one of:
      - consensus_match_promoted: Codex + curator agreed AND both prongs passed
      - consensus_disagreement_review: Codex + curator proposed different right forms
      - prong_fail_review: consensus matched but Prong 1 or Prong 2 failed
      - operator_reviewed: operator hand-resolved via review queue UI

    Returns the updated row, or None if `row_id` doesn't exist.
    """
    valid_statuses = (
        "pending",
        "consensus_match_promoted",
        "consensus_disagreement_review",
        "prong_fail_review",
        "operator_reviewed",
    )
    if status not in valid_statuses:
        raise ValueError(f"status {status!r} not in {valid_statuses}")

    prong_1_evidence_json = (
        json.dumps(prong_1_evidence) if prong_1_evidence else None
    )

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE correction_pending_review
            SET status = ?,
                codex_proposed_right = COALESCE(?, codex_proposed_right),
                codex_confidence = COALESCE(?, codex_confidence),
                codex_reasoning = COALESCE(?, codex_reasoning),
                curator_proposed_right = COALESCE(?, curator_proposed_right),
                curator_reasoning = COALESCE(?, curator_reasoning),
                prong_1_passed = COALESCE(?, prong_1_passed),
                prong_1_reasoning = COALESCE(?, prong_1_reasoning),
                prong_1_evidence_json = COALESCE(?, prong_1_evidence_json),
                prong_2_passed = COALESCE(?, prong_2_passed),
                prong_2_reasoning = COALESCE(?, prong_2_reasoning),
                resolution_action = COALESCE(?, resolution_action),
                resolved_at = CURRENT_TIMESTAMP,
                resolved_by = ?
            WHERE id = ?
            """,
            (
                status,
                codex_proposed_right, codex_confidence, codex_reasoning,
                curator_proposed_right, curator_reasoning,
                (None if prong_1_passed is None else (1 if prong_1_passed else 0)),
                prong_1_reasoning, prong_1_evidence_json,
                (None if prong_2_passed is None else (1 if prong_2_passed else 0)),
                prong_2_reasoning,
                resolution_action,
                resolved_by,
                row_id,
            ),
        )
        if cursor.rowcount == 0:
            return None
        conn.commit()
        row = conn.execute(
            "SELECT * FROM correction_pending_review WHERE id = ?",
            (row_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_pending_review_rows(
    city_name: Optional[str] = None,
    status: str = "pending",
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """V1-Consensus-1 C4 — read pending consensus-review rows.

    Curator heartbeat reads this; review-queue UI reads this. `status`
    can be 'pending' (unresolved), 'consensus_match_promoted',
    'consensus_disagreement_review', 'prong_fail_review',
    'operator_reviewed'. `city_name=None` returns rows across all cities.
    """
    where_parts = ["status = ?"]
    params: List[Any] = [status]
    if city_name:
        where_parts.append("city_name = ?")
        params.append(city_name)
    params.append(limit)

    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT * FROM correction_pending_review
            WHERE {" AND ".join(where_parts)}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def backfill_phonetic_variant_flags(
    city_name: Optional[str] = None,
) -> Dict[str, Any]:
    """V1-Consensus-1 C3 — retroactively flag phonetic-variant clusters.

    Scans `city_vocabulary_corrections` for rows where two or more
    entries point at the same `right` form within the same city. All
    rows in such a cluster get `is_phonetic_variant=1`; singleton-right
    rows get `is_phonetic_variant=0` (kept correct so a previously
    flagged row whose siblings have been deleted isn't left stale).

    When `city_name` is provided, scopes the backfill to that city;
    otherwise sweeps every city in the table. Idempotent — running
    twice produces the same end state.

    Returns a dict with:
      - cities_swept (list[str])
      - rows_flagged_variant (int): rows newly set to is_phonetic_variant=1
      - rows_cleared_variant (int): rows newly set to is_phonetic_variant=0
      - clusters (dict[str, dict]): per-city cluster_count + variant_rows
    """
    conn = get_connection()
    try:
        if city_name:
            cities = [city_name.strip()]
        else:
            rows = conn.execute(
                "SELECT DISTINCT city_name FROM city_vocabulary_corrections"
            ).fetchall()
            cities = [r["city_name"] for r in rows]

        rows_flagged = 0
        rows_cleared = 0
        clusters_per_city: Dict[str, Dict[str, Any]] = {}

        for city in cities:
            cluster_rows = conn.execute(
                """
                SELECT TRIM(right) AS right_form, COUNT(*) AS n
                FROM city_vocabulary_corrections
                WHERE city_name = ?
                GROUP BY TRIM(right)
                """,
                (city,),
            ).fetchall()

            cluster_rights = [r["right_form"] for r in cluster_rows if r["n"] > 1]
            singleton_rights = [r["right_form"] for r in cluster_rows if r["n"] == 1]

            # Flag cluster members
            if cluster_rights:
                placeholders = ",".join("?" for _ in cluster_rights)
                result = conn.execute(
                    f"""
                    UPDATE city_vocabulary_corrections
                    SET is_phonetic_variant = 1
                    WHERE city_name = ?
                      AND TRIM(right) IN ({placeholders})
                      AND is_phonetic_variant != 1
                    """,
                    (city, *cluster_rights),
                )
                rows_flagged += result.rowcount

            # Clear singleton rows (in case they were previously in a cluster
            # but siblings have since been deleted)
            if singleton_rights:
                placeholders = ",".join("?" for _ in singleton_rights)
                result = conn.execute(
                    f"""
                    UPDATE city_vocabulary_corrections
                    SET is_phonetic_variant = 0
                    WHERE city_name = ?
                      AND TRIM(right) IN ({placeholders})
                      AND is_phonetic_variant != 0
                    """,
                    (city, *singleton_rights),
                )
                rows_cleared += result.rowcount

            clusters_per_city[city] = {
                "cluster_count": len(cluster_rights),
                "variant_rights": cluster_rights,
                "singleton_rights_count": len(singleton_rights),
            }

        conn.commit()
        return {
            "cities_swept": cities,
            "rows_flagged_variant": rows_flagged,
            "rows_cleared_variant": rows_cleared,
            "clusters": clusters_per_city,
        }
    finally:
        conn.close()


def detect_phonetic_variant_for_right(
    city_name: str,
    wrong: str,
    right: str,
) -> Dict[str, Any]:
    """V1-Consensus-1 C3 — detect phonetic-variant pattern at correction-insert time.

    A correction is a "phonetic variant" when the proposed `right` form
    is ALREADY present in `city_vocabulary_corrections` for the same
    city, under a DIFFERENT `wrong` key. Worked example: when V1-Repair-1
    added Dykins→Dykens, Daikins→Dykens, Dikins→Dykens to Kingman, each
    successive insert was a phonetic variant of the same canonical
    `Dykens` source — three rows pointing at the same right.

    Returns a dict with:
      - is_phonetic_variant (bool): True iff a sibling row exists.
      - sibling_wrongs (list[str]): the `wrong` forms of any existing
        rows that point at the same `right` (excluding the current
        `wrong` itself).
      - sibling_count (int): len(sibling_wrongs).

    The caller (typically the consensus pipeline at correction-insert
    time, or the operator review queue at display time) uses the flag
    to trigger stricter Prong 2 specificity checks + to surface the
    variant-multiplication pattern in the review surface.
    """
    if not city_name or not wrong or not right:
        return {"is_phonetic_variant": False, "sibling_wrongs": [], "sibling_count": 0}

    city_name = city_name.strip()
    wrong = wrong.strip()
    right = right.strip()

    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT wrong FROM city_vocabulary_corrections
            WHERE city_name = ? AND TRIM(right) = ? AND TRIM(wrong) != ?
            """,
            (city_name, right, wrong),
        ).fetchall()
        sibling_wrongs = [r["wrong"] for r in rows if r["wrong"]]
        return {
            "is_phonetic_variant": len(sibling_wrongs) > 0,
            "sibling_wrongs": sibling_wrongs,
            "sibling_count": len(sibling_wrongs),
        }
    finally:
        conn.close()


def load_vocabulary_corrections(
    city_name: str,
    auto_apply_only: bool = True,
) -> List[Dict[str, Any]]:
    """Return this city's known proper-noun corrections.

    Ordered by descending `LENGTH(wrong)` so callers that apply the
    substitutions in list order match longer phrases before shorter
    ones (e.g., "Andy Devine Avenue" before "Andy Devine") and don't
    chop partial matches. Secondary order is `applied_count DESC` —
    more-frequently-observed corrections are likelier to be canonical.
    """
    where = "WHERE city_name = ?"
    params: List[Any] = [city_name]
    if auto_apply_only:
        where += " AND auto_apply = 1"

    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT id, city_name, wrong, right, applied_count, auto_apply,
                   first_observed_response_file, last_applied_at, created_at
            FROM city_vocabulary_corrections
            {where}
            ORDER BY LENGTH(wrong) DESC, applied_count DESC, id ASC
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def toggle_vocabulary_correction_auto_apply(
    correction_id: int,
    auto_apply: bool,
) -> Optional[Dict[str, Any]]:
    """Flip `auto_apply` for one correction row. Returns the updated row
    (or None if the id doesn't exist). The historical record (applied_count,
    timestamps, provenance) is preserved either way — only the gate flips.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE city_vocabulary_corrections
            SET auto_apply = ?
            WHERE id = ?
            """,
            (1 if auto_apply else 0, correction_id),
        )
        if cursor.rowcount == 0:
            return None
        conn.commit()
        row = conn.execute(
            """
            SELECT id, city_name, wrong, right, applied_count, auto_apply,
                   first_observed_response_file, last_applied_at, created_at
            FROM city_vocabulary_corrections
            WHERE id = ?
            """,
            (correction_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_pending_promotions(
    city_name: str,
    threshold: int = 2,
) -> List[Dict[str, Any]]:
    """T-018: list `city_vocabulary_corrections` rows for `city_name`
    that are candidates for promotion into the city's canonical
    `whisper_vocabulary_hints` JSON.

    Returned set:
      - `auto_apply = 1` (operator hasn't already rejected this term)
      - `promoted_at IS NULL` (not yet promoted to the canonical JSON)
      - `applied_count >= threshold` (default 2) — auto-promotion candidates

    Plus, irrespective of `threshold`, all `auto_apply=1, promoted_at=NULL`
    rows are returned so the Inbox can also surface below-threshold
    corrections for manual one-off promotion. The caller (Inbox page)
    can sort/filter on `applied_count >= threshold` to render the
    "auto-promote candidates" group vs. the "manual-only" group.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, city_name, wrong, right, applied_count, auto_apply,
                   first_observed_response_file, last_applied_at, created_at,
                   promoted_at, promoted_by,
                   agent_proposed_right, agent_reasoning,
                   agent_proposed_by, agent_proposed_at
            FROM city_vocabulary_corrections
            WHERE city_name = ?
              AND auto_apply = 1
              AND promoted_at IS NULL
            ORDER BY (applied_count >= ?) DESC,
                     applied_count DESC,
                     LENGTH(wrong) DESC,
                     id ASC
            """,
            (city_name, threshold),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["meets_threshold"] = d["applied_count"] >= threshold
        out.append(d)
    return out


def record_agent_counter_proposal(
    correction_id: int,
    proposed_right: str,
    reasoning: Optional[str] = None,
    agent_role: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """D-057 — record an agent's counter-proposal for a vocabulary correction.

    Used when an agent (e.g. Vocab Curator) determines the verifier's
    `right` value is wrong but has a better alternative. The agent calls
    this before escalating; the operator UI surfaces both proposals; the
    Slack fast-path emoji applies the agent's value directly.

    Repeated calls UPDATE the existing counter-proposal (most-recent agent
    proposal wins). Use NULL `proposed_right` to clear a prior proposal.

    Returns the updated row dict, or None if `correction_id` doesn't exist.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE city_vocabulary_corrections
            SET agent_proposed_right = ?,
                agent_reasoning = ?,
                agent_proposed_by = ?,
                agent_proposed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (proposed_right, reasoning, agent_role, correction_id),
        )
        if cursor.rowcount == 0:
            return None
        conn.commit()
        row = conn.execute(
            """
            SELECT id, city_name, wrong, right, applied_count, auto_apply,
                   first_observed_response_file, last_applied_at, created_at,
                   promoted_at, promoted_by,
                   agent_proposed_right, agent_reasoning,
                   agent_proposed_by, agent_proposed_at
            FROM city_vocabulary_corrections
            WHERE id = ?
            """,
            (correction_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def mark_correction_promoted(
    correction_id: int,
    promoted_by: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """T-018: stamp a correction as promoted to the canonical JSON.
    Returns the updated row, or None if `correction_id` doesn't exist.
    The caller is responsible for writing the entry into the city
    JSON — this helper only updates DB state.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE city_vocabulary_corrections
            SET promoted_at = CURRENT_TIMESTAMP,
                promoted_by = ?
            WHERE id = ?
            """,
            (promoted_by, correction_id),
        )
        if cursor.rowcount == 0:
            return None
        conn.commit()
        row = conn.execute(
            """
            SELECT id, city_name, wrong, right, applied_count, auto_apply,
                   first_observed_response_file, last_applied_at, created_at,
                   promoted_at, promoted_by
            FROM city_vocabulary_corrections
            WHERE id = ?
            """,
            (correction_id,),
        ).fetchone()
        result = dict(row) if row else None
    finally:
        conn.close()
    # D-062 backlog hygiene: promoting a correction clears its escalation. Both
    # the promote endpoint AND the Slack ✨ apply path route through here.
    if result is not None:
        acknowledge_escalations_for(
            f"city_vocabulary_corrections.id={correction_id}",
            acknowledged_by="auto:vocab-promoted",
        )
    return result


def reject_promotion(
    correction_id: int,
    rejected_by: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """T-018: operator rejected a correction at the Inbox stage.
    Sets `auto_apply = 0` so it stops applying AND drops from the Inbox.
    Doesn't delete the row — the historical accumulation stays for audit.
    Records the rejection via `promoted_by` (reusing the column to mean
    "decided-by") and a non-null `promoted_at`. The Inbox query filters
    on `promoted_at IS NULL` so this row won't resurface unless the
    operator manually re-enables it via SQL or a future un-reject UI.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE city_vocabulary_corrections
            SET auto_apply = 0,
                promoted_at = CURRENT_TIMESTAMP,
                promoted_by = ?
            WHERE id = ?
            """,
            (f"rejected:{rejected_by or 'operator'}", correction_id),
        )
        if cursor.rowcount == 0:
            return None
        conn.commit()
        row = conn.execute(
            """
            SELECT id, city_name, wrong, right, applied_count, auto_apply,
                   promoted_at, promoted_by
            FROM city_vocabulary_corrections
            WHERE id = ?
            """,
            (correction_id,),
        ).fetchone()
        result = dict(row) if row else None
    finally:
        conn.close()
    # D-062 backlog hygiene: rejecting a correction clears its escalation too.
    if result is not None:
        acknowledge_escalations_for(
            f"city_vocabulary_corrections.id={correction_id}",
            acknowledged_by="auto:vocab-rejected",
        )
    return result


def apply_city_corrections(
    city_name: str,
    text: str,
) -> tuple[str, List[Dict[str, Any]]]:
    """Apply this city's known corrections (auto_apply=1 only) to `text`.

    Mechanical find-and-replace, in `load_vocabulary_corrections` order
    (longest-wrong-first), composed from `review_response_parser.apply_substitutions`
    so the audit-log shape matches what V3 already records on
    `member_quotes.gemini_correction_notes`.

    Returns `(corrected_text, applied_log)`. `applied_log` is the same
    `{from, to, count}` dict-list shape — entries with `count=0` mean
    the correction is on the books for this city but didn't fire on
    this particular text. Callers can filter on `count > 0` when
    persisting to keep the audit small.
    """
    if not text or not city_name:
        return text, []
    corrections = load_vocabulary_corrections(city_name, auto_apply_only=True)
    if not corrections:
        return text, []
    from review_response_parser import apply_substitutions  # local import — avoid cycles at module load
    pairs = [(c["wrong"], c["right"]) for c in corrections]
    return apply_substitutions(text, pairs)


# ── S-004 agent-employee escalation helpers ─────────────────────────────


def insert_pending_escalation(
    role: str,
    severity: str,
    summary: str,
    what_i_see: Optional[List[str]] = None,
    what_id_do: Optional[List[str]] = None,
    deep_link: Optional[str] = None,
    audit_row: Optional[str] = None,
) -> int:
    """Write an escalation to the pending table. Returns the new row id.

    Called by `parsers/slack_notifier.send_escalation` BEFORE attempting
    the Slack POST — the table is the canonical record; Slack is the
    notification layer. If the POST succeeds, the row's
    `delivered_to_slack` flips via `mark_pending_escalation_delivered`.
    If it fails, the row stays pending and James sees it in the operator
    terminal badge.

    `what_i_see` and `what_id_do` are arrays of bullet strings; they
    serialize to JSON for the DB column.
    """
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO pending_escalations (
                agent_role, severity, summary,
                what_i_see, what_id_do, deep_link, audit_row
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                role,
                severity,
                summary,
                json.dumps(what_i_see or []),
                json.dumps(what_id_do or []),
                deep_link,
                audit_row,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def mark_pending_escalation_delivered(
    escalation_id: int, slack_message_ts: Optional[str] = None
) -> bool:
    """Flip `delivered_to_slack=1` + stamp delivered_at after a successful
    Slack POST. When `slack_message_ts` is provided (chat.postMessage path),
    also persists the ts so the reaction listener can resolve future
    reaction_added events back to this row. Webhook-fallback paths pass
    None — those messages can't be reacted to programmatically. Returns
    True if the row existed.
    """
    conn = get_connection()
    try:
        if slack_message_ts is None:
            cur = conn.execute(
                """
                UPDATE pending_escalations
                SET delivered_to_slack = 1, delivered_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (escalation_id,),
            )
        else:
            cur = conn.execute(
                """
                UPDATE pending_escalations
                SET delivered_to_slack = 1,
                    delivered_at = CURRENT_TIMESTAMP,
                    slack_message_ts = ?
                WHERE id = ?
                """,
                (slack_message_ts, escalation_id),
            )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def find_pending_escalation_by_message_ts(
    slack_message_ts: str,
) -> Optional[Dict[str, Any]]:
    """Resolve a Slack message ts back to its pending_escalations row.

    Used by the Socket Mode listener (slack_listener.py) when handling
    reaction_added events — the event payload carries the message ts and
    we need the escalation id to dispatch the right action.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM pending_escalations WHERE slack_message_ts = ?",
            (slack_message_ts,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    out = dict(row)
    # Decode the JSON-encoded array fields for callers that need them.
    for key in ("what_i_see", "what_id_do"):
        if out.get(key):
            try:
                out[key] = json.loads(out[key])
            except Exception:
                pass
    return out


def acknowledge_pending_escalation(
    escalation_id: int, acknowledged_by: str = "operator"
) -> bool:
    """Mark an escalation as acknowledged (James saw it, decided what to do).

    Doesn't delete the row — keeps it for audit. The unacked badge count
    on the operator terminal drops once this fires. Returns True if the
    row existed and wasn't already acknowledged.
    """
    conn = get_connection()
    try:
        cur = conn.execute(
            """
            UPDATE pending_escalations
            SET acknowledged_at = CURRENT_TIMESTAMP,
                acknowledged_by = ?
            WHERE id = ? AND acknowledged_at IS NULL
            """,
            (acknowledged_by, escalation_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def acknowledge_escalations_for(
    audit_row: str, acknowledged_by: str = "auto-resolved"
) -> int:
    """D-062 backlog hygiene — auto-ack every unacked escalation for a resolved item.

    When an item is resolved (a disputed quote, a vocabulary correction), its
    escalation has served its purpose; acking it here keeps resolved work from
    orphaning in the backlog or inflating the badge (the `badge=0 vs N unacked`
    divergence the first orchestrator heartbeat flagged). Matched by `audit_row`
    (e.g. resolving `quotes.id=46` acks the escalation whose
    `audit_row='quotes.id=46'`). Rows are acked, never deleted — the audit trail
    stays in the DB; any Slack-message cleanup is a separate best-effort sweep.

    Returns the number of rows acked (0 if none were open for that audit_row).
    """
    if not audit_row:
        return 0
    conn = get_connection()
    try:
        cur = conn.execute(
            """
            UPDATE pending_escalations
            SET acknowledged_at = CURRENT_TIMESTAMP,
                acknowledged_by = ?
            WHERE audit_row = ? AND acknowledged_at IS NULL
            """,
            (acknowledged_by, audit_row),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def get_unacked_escalation_by_prefix(
    agent_role: str, audit_row_prefix: str
) -> Optional[Dict[str, Any]]:
    """Newest unacknowledged escalation for `agent_role` whose audit_row starts
    with `audit_row_prefix`.

    The lookup half of the already-escalated-skipped guard
    (agents/README.md § already-escalated-skipped). First deterministic
    adopter: the balance-auditor's threshold dedup (D-106) — its V1 heartbeat
    is plain Python, so the guard lives in code, not in a manual's prose.
    Convention: dedupe-able escalations stamp audit_row as
    '<class-prefix>:<cents>' (e.g. 'balance_threshold:blocked:98') so the
    prior row's magnitude is parseable from its own marker.
    """
    if not agent_role or not audit_row_prefix:
        return None
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT id, agent_role, severity, summary, audit_row, created_at
            FROM pending_escalations
            WHERE agent_role = ?
              AND acknowledged_at IS NULL
              AND audit_row LIKE ? || '%'
            ORDER BY id DESC
            LIMIT 1
            """,
            (agent_role, audit_row_prefix),
        ).fetchone()
        if row is None:
            return None
        keys = ["id", "agent_role", "severity", "summary", "audit_row", "created_at"]
        return dict(zip(keys, row))
    finally:
        conn.close()


def list_pending_escalations(
    unacknowledged_only: bool = True,
    role_filter: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Read helper for the operator-terminal badge + the escalations view.

    `what_i_see` and `what_id_do` are decoded from JSON back to arrays
    for the response.
    """
    sql = "SELECT * FROM pending_escalations"
    where: List[str] = []
    params: List[Any] = []
    if unacknowledged_only:
        where.append("acknowledged_at IS NULL")
    if role_filter:
        where.append("agent_role = ?")
        params.append(role_filter)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    conn = get_connection()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        for key in ("what_i_see", "what_id_do"):
            raw = d.get(key)
            if raw:
                try:
                    d[key] = json.loads(raw)
                    if not isinstance(d[key], list):
                        d[key] = []
                except (json.JSONDecodeError, TypeError):
                    d[key] = []
            else:
                d[key] = []
        out.append(d)
    return out


def count_pending_escalations(
    unacknowledged_only: bool = True,
    undelivered_only: bool = False,
) -> int:
    """Cheap COUNT(*) for the operator-terminal badge.

    `unacknowledged_only` is the default — the badge surfaces how much
    James-attention is owed regardless of whether Slack got the
    notification. `undelivered_only` is the count of escalations that
    NEVER reached Slack (useful for diagnosing webhook health).
    """
    sql = "SELECT COUNT(*) FROM pending_escalations"
    where: List[str] = []
    if unacknowledged_only:
        where.append("acknowledged_at IS NULL")
    if undelivered_only:
        where.append("delivered_to_slack = 0")
    if where:
        sql += " WHERE " + " AND ".join(where)

    conn = get_connection()
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────
# Balance Auditor helpers (2026-05-30) — append-only ledger access.
# ─────────────────────────────────────────────────────────────────────


def append_ledger_event(
    *,
    provider: str,
    event_type: str,
    amount_cents: Optional[int] = None,
    currency: str = "usd",
    bucket_start_time: Optional[int] = None,
    bucket_end_time: Optional[int] = None,
    running_balance_cents: Optional[int] = None,
    source: str,
    notes: Optional[str] = None,
    external_ref: Optional[str] = None,
) -> Optional[int]:
    """Append a row to balance_ledger using INSERT OR IGNORE.

    The UNIQUE(provider, event_type, bucket_start_time, bucket_end_time)
    constraint makes spend_observed rows idempotent — re-fetching the
    same bucket is a no-op (returns None, not a duplicate row).

    Returns the new row's id, or None if the row was a duplicate of an
    existing bucket. Callers can distinguish "I appended a new spend
    bucket" from "I already had this bucket" by checking the return.

    See agents/balance-auditor.md § Schema for the event_type enum +
    column semantics.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR IGNORE INTO balance_ledger (
                provider, event_type, amount_cents, currency,
                bucket_start_time, bucket_end_time,
                running_balance_cents, source, notes, external_ref
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                provider, event_type, amount_cents, currency,
                bucket_start_time, bucket_end_time,
                running_balance_cents, source, notes, external_ref,
            ),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
        return cursor.lastrowid
    finally:
        conn.close()


def get_current_balance(provider: str = "openai") -> int:
    """Compute current balance in cents for a provider, as the difference
    of recorded deposits and observed spend.

    Returns 0 if no rows exist (clean cold-start).

    The discrepancy_flagged and api_balance_snapshot rows are not part
    of the math — they're audit artifacts. manual_correction rows ARE
    counted (sign of amount_cents determines direction).
    """
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN event_type = 'deposit_observed' THEN amount_cents ELSE 0 END), 0) AS deposits,
                COALESCE(SUM(CASE WHEN event_type = 'spend_observed' THEN amount_cents ELSE 0 END), 0) AS spend,
                COALESCE(SUM(CASE WHEN event_type = 'manual_correction' THEN amount_cents ELSE 0 END), 0) AS corrections
            FROM balance_ledger
            WHERE provider = ?
            """,
            (provider,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return 0
    # deposits add; spend subtracts; corrections add (positive = credit, negative = debit)
    return int(row["deposits"]) - int(row["spend"]) + int(row["corrections"])


def get_latest_snapshot(provider: str = "openai") -> Optional[Dict]:
    """Return the most-recent api_balance_snapshot row for the provider,
    or None if no snapshot has been recorded yet (cold start)."""
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT * FROM balance_ledger
            WHERE provider = ? AND event_type = 'api_balance_snapshot'
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            (provider,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def get_latest_spend_bucket_end(provider: str = "openai") -> Optional[int]:
    """Return the bucket_end_time (Unix epoch seconds) of the most-recent
    spend_observed row, or None if no spend has been recorded.

    The balance-check heartbeat uses this to compute the next fetch's
    start_time — only fetch buckets AFTER what we've already recorded.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT MAX(bucket_end_time) AS latest_end
            FROM balance_ledger
            WHERE provider = ? AND event_type = 'spend_observed'
            """,
            (provider,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or row["latest_end"] is None:
        return None
    return int(row["latest_end"])


def get_recent_ledger_events(
    provider: str = "openai",
    limit: int = 20,
) -> List[Dict]:
    """Return the most-recent N ledger events for a provider, ordered by
    observed_at DESC. Used by the agent's memory + the operator's daily
    brief to summarize recent financial activity."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT * FROM balance_ledger
            WHERE provider = ?
            ORDER BY observed_at DESC
            LIMIT ?
            """,
            (provider, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_trailing_spend_observed(
    provider: str = "openai",
    days: int = 7,
) -> List[Dict]:
    """Return spend_observed rows for the last `days` finalized buckets,
    most recent first. Used by the Balance Auditor's discrepancy
    detection (B-prime, James 2026-05-31): compute the trailing-Nd
    daily-spend average to detect today's in-progress spend spikes.

    In normal operation there's exactly 1 spend_observed row per
    calendar day (bucket_width=1d on OpenAI's /v1/organization/costs),
    so `limit=7` ~= "the last week of finalized spend." If the auditor
    has been running fewer than 7 days, returns whatever exists.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT * FROM balance_ledger
            WHERE provider = ?
              AND event_type = 'spend_observed'
            ORDER BY bucket_end_time DESC
            LIMIT ?
            """,
            (provider, days),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_spend_observed_since(provider: str, since_unix: int) -> int:
    """Sum amount_cents of spend_observed rows for `provider` whose
    bucket_start_time is at or after `since_unix` (Unix epoch seconds).

    The D-119 Anthropic self-meter (claude_p_metered.py) writes one
    spend_observed row per `claude -p` call with a per-call bucket window,
    so this returns the accumulated metered spend since `since_unix` — pass
    today's UTC-midnight epoch to get today's running Anthropic spend, which
    the budget gate compares against the per-day ceiling. Returns 0 when no
    matching rows exist. (The OpenAI auditor's 1-row-per-day model also works
    with this query, but it has its own trailing-window helper.)
    """
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS total
            FROM balance_ledger
            WHERE provider = ?
              AND event_type = 'spend_observed'
              AND bucket_start_time >= ?
            """,
            (provider, since_unix),
        ).fetchone()
    finally:
        conn.close()
    return int(row["total"]) if row and row["total"] is not None else 0


# ── Phase 2 D2 — meeting_speaker_roster helpers ──────────────────────


def upsert_speaker_roster_row(
    meeting_id: int,
    cluster_label: str,
    *,
    proposed_canonical: Optional[str],
    evidence_chunk_indices: Optional[List[int]] = None,
    evidence_text: Optional[str] = None,
    prong_1_passed: Optional[bool] = None,
    prong_1_reasoning: Optional[str] = None,
    prong_2_passed: Optional[bool] = None,
    prong_2_reasoning: Optional[str] = None,
    status: str = "pending_review",
    model_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Upsert a Phase 2 D6 cluster→roster mapping proposal for a meeting.

    Idempotent on (meeting_id, cluster_label). On INSERT, returns the
    new row's id with `was_new=True`. On UPDATE (re-running the mapper
    against the same meeting), updates the proposal + prong fields but
    preserves any operator-touched `confirmed_canonical` /
    `resolved_*` fields — see `confirm_speaker_roster_row` for the
    operator path. If status is 'auto_promoted' and confirmed_canonical
    is empty, it's auto-set to proposed_canonical at insert time.
    """
    if not cluster_label:
        raise ValueError("cluster_label required")

    auto_confirmed = (
        proposed_canonical if status == "auto_promoted" else None
    )
    evidence_json = (
        json.dumps(evidence_chunk_indices) if evidence_chunk_indices else None
    )

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO meeting_speaker_roster
                (meeting_id, cluster_label, proposed_canonical,
                 confirmed_canonical, evidence_chunk_indices, evidence_text,
                 prong_1_passed, prong_1_reasoning,
                 prong_2_passed, prong_2_reasoning,
                 status, model_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(meeting_id, cluster_label) DO UPDATE SET
                proposed_canonical = excluded.proposed_canonical,
                evidence_chunk_indices = excluded.evidence_chunk_indices,
                evidence_text = excluded.evidence_text,
                prong_1_passed = excluded.prong_1_passed,
                prong_1_reasoning = excluded.prong_1_reasoning,
                prong_2_passed = excluded.prong_2_passed,
                prong_2_reasoning = excluded.prong_2_reasoning,
                status = CASE
                    WHEN meeting_speaker_roster.confirmed_canonical IS NOT NULL
                         AND meeting_speaker_roster.status IN ('operator_confirmed', 'operator_overridden', 'left_anonymous')
                    THEN meeting_speaker_roster.status
                    ELSE excluded.status
                END,
                model_id = excluded.model_id,
                updated_at = CURRENT_TIMESTAMP
            RETURNING id
            """,
            (
                meeting_id, cluster_label, proposed_canonical,
                auto_confirmed, evidence_json, evidence_text,
                int(prong_1_passed) if prong_1_passed is not None else None,
                prong_1_reasoning,
                int(prong_2_passed) if prong_2_passed is not None else None,
                prong_2_reasoning,
                status, model_id,
            ),
        )
        row = cursor.fetchone()
        conn.commit()
        return {"id": row["id"], "meeting_id": meeting_id, "cluster_label": cluster_label}
    finally:
        conn.close()


def get_speaker_roster_for_meeting(meeting_id: int) -> List[Dict[str, Any]]:
    """Return all roster rows for a meeting, ordered by cluster_label."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, meeting_id, cluster_label, proposed_canonical,
                   confirmed_canonical, evidence_chunk_indices, evidence_text,
                   prong_1_passed, prong_1_reasoning,
                   prong_2_passed, prong_2_reasoning,
                   status, model_id, resolution_action,
                   resolved_at, resolved_by, created_at, updated_at
            FROM meeting_speaker_roster
            WHERE meeting_id = ?
            ORDER BY cluster_label
            """,
            (meeting_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_canonical_for_cluster(
    meeting_id: int, cluster_label: str,
) -> Optional[str]:
    """Resolve a (meeting_id, cluster_label) to its display canonical.

    Returns confirmed_canonical if set (operator-confirmed / auto-promoted),
    else proposed_canonical, else None (rendering should fall back to the
    raw cluster_label like "Speaker 1").
    """
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT confirmed_canonical, proposed_canonical, status
            FROM meeting_speaker_roster
            WHERE meeting_id = ? AND cluster_label = ?
            """,
            (meeting_id, cluster_label),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    if row["status"] == "left_anonymous":
        return None
    return row["confirmed_canonical"] or row["proposed_canonical"] or None


def confirm_speaker_roster_row(
    row_id: int,
    *,
    confirmed_canonical: str,
    resolved_by: str,
    resolution_action: str = "operator_confirmed",
) -> Dict[str, Any]:
    """Operator-confirms a roster row (D-Build-B UI calls this).

    `resolution_action` distinguishes:
      - 'operator_confirmed'  — operator agreed with proposed_canonical
      - 'operator_overridden' — operator picked a different canonical
    """
    if resolution_action not in ("operator_confirmed", "operator_overridden"):
        raise ValueError(f"unknown resolution_action: {resolution_action}")

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE meeting_speaker_roster
            SET confirmed_canonical = ?,
                status = ?,
                resolution_action = ?,
                resolved_at = CURRENT_TIMESTAMP,
                resolved_by = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            RETURNING id, meeting_id, cluster_label, confirmed_canonical, status
            """,
            (confirmed_canonical, resolution_action, resolution_action,
             resolved_by, row_id),
        )
        row = cursor.fetchone()
        conn.commit()
        if not row:
            raise ValueError(f"no meeting_speaker_roster row with id={row_id}")
        return dict(row)
    finally:
        conn.close()


def mark_speaker_roster_anonymous(
    row_id: int, *, resolved_by: str,
) -> Dict[str, Any]:
    """Operator opts out — leave this cluster anonymous, rendered as
    'Speaker N' in the broadcast surface."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE meeting_speaker_roster
            SET confirmed_canonical = NULL,
                status = 'left_anonymous',
                resolution_action = 'left_anonymous',
                resolved_at = CURRENT_TIMESTAMP,
                resolved_by = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            RETURNING id, meeting_id, cluster_label, status
            """,
            (resolved_by, row_id),
        )
        row = cursor.fetchone()
        conn.commit()
        if not row:
            raise ValueError(f"no meeting_speaker_roster row with id={row_id}")
        return dict(row)
    finally:
        conn.close()


def list_pending_speaker_roster_reviews(
    *, limit: int = 100,
) -> List[Dict[str, Any]]:
    """For the D-Build-B Speaker Roster Review queue UI — non-auto-promoted
    rows the operator hasn't touched yet."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT r.id, r.meeting_id, r.cluster_label, r.proposed_canonical,
                   r.evidence_text, r.prong_1_passed, r.prong_1_reasoning,
                   r.prong_2_passed, r.prong_2_reasoning, r.status, r.model_id,
                   r.created_at,
                   m.meeting_title, m.meeting_date, m.city_name
            FROM meeting_speaker_roster r
            JOIN meetings m ON m.id = r.meeting_id
            WHERE r.status = 'pending_review'
            ORDER BY r.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────
# V1.5-Verify-1 — BYOK audit-runs schema (per BYOK_ARCHITECTURE_SPEC § 2.3)
#
# Append-only audit log for every Z-SPAN-orchestrated retrieval (and
# forthcoming notebook aggregates). The public /api/verify-run/{run_id}
# endpoint queries this table to confirm "yes Z-SPAN ran retrieval X at
# time Y against meeting Z" — the load-bearing civic-trust mechanism for
# distinguishing real Z-SPAN screenshots from fabrications.
#
# `kind` is forward-compatible: 'retrieval' for V1.5-RAG-Search-1,
# 'notebook' for V1.5-Notebook-RunID-1 (the per-meeting aggregate that
# indexes sub-run_ids), and any future kind a downstream chunk adds.
# ─────────────────────────────────────────────────────────────────

def init_byok_audit_runs_schema():
    """Create the byok_audit_runs table + indexes if absent. Idempotent."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS byok_audit_runs (
            run_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL DEFAULT 'retrieval',
            meeting_id INTEGER,
            timestamp_utc TEXT NOT NULL,
            prompt_template_version TEXT NOT NULL,
            prompt_template_hash TEXT NOT NULL,
            query_hash TEXT,
            vector_ids_json TEXT NOT NULL,
            provider TEXT,
            model TEXT,
            supersedes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_byok_audit_runs_meeting_id ON byok_audit_runs(meeting_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_byok_audit_runs_timestamp ON byok_audit_runs(timestamp_utc)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_byok_audit_runs_kind ON byok_audit_runs(kind)")
    # V1.5-OperatorSearch-1 Phase 3 — parent rows of kind="operator_search"
    # reference their per-meeting retrieval children via this column.
    # Idempotent — only adds the column if absent so existing DBs upgrade
    # cleanly.
    if not _column_exists(cursor, "byok_audit_runs", "child_run_ids_json"):
        cursor.execute(
            "ALTER TABLE byok_audit_runs ADD COLUMN child_run_ids_json TEXT"
        )
    conn.commit()
    conn.close()


def save_byok_audit_run(
    *,
    run_id: str,
    kind: str,
    meeting_id: Optional[int],
    timestamp_utc: str,
    prompt_template_version: str,
    prompt_template_hash: str,
    vector_ids: List[str],
    query_hash: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    supersedes: Optional[str] = None,
    child_run_ids: Optional[List[str]] = None,
) -> None:
    """Insert a BYOK audit-run row. Idempotent on run_id (INSERT OR IGNORE
    so re-firing the same run_id is a no-op rather than an error).

    child_run_ids — for kind="operator_search" parent rows, the list of
    per-meeting retrieval run_ids the fan-out produced. None for leaf
    retrieval rows.
    """
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO byok_audit_runs (
                run_id, kind, meeting_id, timestamp_utc,
                prompt_template_version, prompt_template_hash,
                query_hash, vector_ids_json,
                provider, model, supersedes, child_run_ids_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, kind, meeting_id, timestamp_utc,
                prompt_template_version, prompt_template_hash,
                query_hash, json.dumps(vector_ids),
                provider, model, supersedes,
                json.dumps(child_run_ids) if child_run_ids is not None else None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def init_librarian_gate_events_schema() -> None:
    """Create the hash-only Librarian stencil audit log idempotently.

    Accepted rows inside the active quota window are enforcement-critical
    and must never be purged by a future retention job.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS librarian_gate_events (
                event_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                meeting_id INTEGER,
                query_hash TEXT NOT NULL,
                gate_version TEXT NOT NULL,
                stencil_result TEXT NOT NULL,
                reason_code TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # These fields were added when the static stencil replaced the
        # proposed runtime classifier. Keep each addition safe for databases
        # created from an earlier development shape.
        later_columns = (
            ("matched_rule_id", "TEXT"),
            ("evaluation_ms", "REAL"),
            ("retrieval_run_id", "TEXT"),
            ("error_class", "TEXT"),
            ("synthesis_envelope_hash", "TEXT"),
            ("envelope_version", "TEXT"),
            ("envelope_expires_at", "TEXT"),
            (
                "relay_attempt_count",
                "INTEGER NOT NULL DEFAULT 0",
            ),
            ("relay_started_at", "TEXT"),
            ("relay_provider", "TEXT"),
            (
                "enforcement_epoch_at_decision",
                "INTEGER CHECK ("
                "enforcement_epoch_at_decision IS NULL OR "
                "enforcement_epoch_at_decision >= 0)",
            ),
            (
                "policy_revision",
                "INTEGER CHECK ("
                "policy_revision IS NULL OR policy_revision >= 1)",
            ),
            ("terminal_failure_reason", "TEXT"),
            ("terminal_failed_at", "TEXT"),
        )
        for column_name, column_type in later_columns:
            if not _column_exists(
                cursor,
                "librarian_gate_events",
                column_name,
            ):
                cursor.execute(
                    "ALTER TABLE librarian_gate_events "
                    f"ADD COLUMN {column_name} {column_type}"
                )

        cursor.execute(
            "CREATE INDEX IF NOT EXISTS "
            "idx_librarian_gate_events_user_id "
            "ON librarian_gate_events(user_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS "
            "idx_librarian_gate_events_created_at "
            "ON librarian_gate_events(created_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS "
            "idx_librarian_gate_events_user_result_created "
            "ON librarian_gate_events("
            "user_id, stencil_result, created_at)"
        )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_lge_accepted_run "
            "ON librarian_gate_events(user_id, retrieval_run_id) "
            "WHERE stencil_result = 'accepted' "
            "AND retrieval_run_id IS NOT NULL"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_lge_terminal_failure "
            "ON librarian_gate_events(terminal_failed_at) "
            "WHERE terminal_failure_reason IS NOT NULL"
        )
        conn.commit()
    finally:
        conn.close()


_LIBRARIAN_POLICY_DEFAULTS = {
    "daily_query_cap": 3,
    "quota_window_seconds": 86400,
    "reject_burst_threshold": 8,
    "reject_burst_window_seconds": 600,
    "reject_cooldown_seconds": 1800,
    "reject_autoban_strike_threshold": 3,
    "reject_autoban_window_seconds": 86400,
}
_LIBRARIAN_POLICY_SETTING_KEYS = {
    "daily_query_cap": "librarian_daily_query_cap",
    "reject_burst_threshold": "librarian_reject_burst_threshold",
    "reject_burst_window_seconds": (
        "librarian_reject_burst_window_seconds"
    ),
    "reject_cooldown_seconds": "librarian_reject_cooldown_seconds",
    "reject_autoban_strike_threshold": (
        "librarian_reject_autoban_strike_threshold"
    ),
    "reject_autoban_window_seconds": (
        "librarian_reject_autoban_window_seconds"
    ),
}


@dataclass(frozen=True, slots=True)
class AbusePolicySnapshot:
    """One complete, validated SQLite-owned Librarian policy revision."""

    revision: int
    daily_query_cap: int
    quota_window_seconds: int
    reject_burst_threshold: int
    reject_burst_window_seconds: int
    reject_cooldown_seconds: int
    reject_autoban_strike_threshold: int
    reject_autoban_window_seconds: int

    def __post_init__(self) -> None:
        values = {
            "revision": self.revision,
            "daily_query_cap": self.daily_query_cap,
            "quota_window_seconds": self.quota_window_seconds,
            "reject_burst_threshold": self.reject_burst_threshold,
            "reject_burst_window_seconds": (
                self.reject_burst_window_seconds
            ),
            "reject_cooldown_seconds": self.reject_cooldown_seconds,
            "reject_autoban_strike_threshold": (
                self.reject_autoban_strike_threshold
            ),
            "reject_autoban_window_seconds": (
                self.reject_autoban_window_seconds
            ),
        }
        for name, value in values.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
            ):
                raise ValueError(
                    f"{name} must be a positive non-boolean integer"
                )
        if self.quota_window_seconds != 86400:
            raise ValueError("quota_window_seconds must equal 86400")
        if not 4 <= self.reject_burst_threshold <= 64:
            raise ValueError(
                "reject_burst_threshold must be between 4 and 64"
            )
        if self.reject_burst_window_seconds < 60:
            raise ValueError(
                "reject_burst_window_seconds must be at least 60"
            )
        if self.reject_cooldown_seconds < 300:
            raise ValueError(
                "reject_cooldown_seconds must be at least 300"
            )
        if not 2 <= self.reject_autoban_strike_threshold <= 32:
            raise ValueError(
                "reject_autoban_strike_threshold must be between 2 and 32"
            )
        if self.reject_autoban_window_seconds < 3600:
            raise ValueError(
                "reject_autoban_window_seconds must be at least 3600"
            )
        if (
            self.reject_cooldown_seconds
            < self.reject_burst_window_seconds
        ):
            raise ValueError(
                "reject_cooldown_seconds must be at least "
                "reject_burst_window_seconds"
            )
        if (
            self.reject_cooldown_seconds
            > self.reject_autoban_window_seconds
        ):
            raise ValueError(
                "reject_cooldown_seconds exceeds "
                "reject_autoban_window_seconds"
            )
        if (
            (self.reject_autoban_strike_threshold - 1)
            * self.reject_cooldown_seconds
            >= self.reject_autoban_window_seconds
        ):
            raise ValueError(
                "auto-ban threshold is unreachable inside its window"
            )

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "AbusePolicySnapshot":
        return cls(
            revision=int(row["revision"]),
            daily_query_cap=int(row["daily_query_cap"]),
            quota_window_seconds=int(row["quota_window_seconds"]),
            reject_burst_threshold=int(row["reject_burst_threshold"]),
            reject_burst_window_seconds=int(
                row["reject_burst_window_seconds"]
            ),
            reject_cooldown_seconds=int(row["reject_cooldown_seconds"]),
            reject_autoban_strike_threshold=int(
                row["reject_autoban_strike_threshold"]
            ),
            reject_autoban_window_seconds=int(
                row["reject_autoban_window_seconds"]
            ),
        )


def _initial_librarian_policy_values() -> dict[str, int]:
    """Migrate one valid legacy file-backed policy, else use safe defaults."""
    values = dict(_LIBRARIAN_POLICY_DEFAULTS)
    try:
        try:
            from parsers.env_config import load_user_settings
        except ImportError:
            from env_config import load_user_settings
        settings = load_user_settings()
        if not isinstance(settings, dict):
            return values

        cap = settings.get("librarian_daily_query_cap")
        if (
            not isinstance(cap, bool)
            and isinstance(cap, int)
            and cap > 0
        ):
            values["daily_query_cap"] = cap

        abuse_values = {
            field: settings.get(setting_key)
            for field, setting_key in _LIBRARIAN_POLICY_SETTING_KEYS.items()
            if field != "daily_query_cap"
        }
        if all(value is None for value in abuse_values.values()):
            return values
        candidate = AbusePolicySnapshot(
            revision=1,
            daily_query_cap=values["daily_query_cap"],
            quota_window_seconds=86400,
            **abuse_values,
        )
        values.update({
            "reject_burst_threshold": candidate.reject_burst_threshold,
            "reject_burst_window_seconds": (
                candidate.reject_burst_window_seconds
            ),
            "reject_cooldown_seconds": candidate.reject_cooldown_seconds,
            "reject_autoban_strike_threshold": (
                candidate.reject_autoban_strike_threshold
            ),
            "reject_autoban_window_seconds": (
                candidate.reject_autoban_window_seconds
            ),
        })
    except Exception as exc:
        logger.warning(
            "legacy Librarian policy is invalid; migrating safe defaults: %s",
            exc,
        )
    return values


def init_librarian_policy_schema() -> None:
    """Create and one-time seed the SQLite Librarian policy singleton."""
    conn = get_connection()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS librarian_policy (
                singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                revision INTEGER NOT NULL CHECK (revision >= 1),
                daily_query_cap INTEGER NOT NULL
                    CHECK (daily_query_cap >= 1),
                quota_window_seconds INTEGER NOT NULL
                    CHECK (quota_window_seconds = 86400),
                reject_burst_threshold INTEGER NOT NULL
                    CHECK (reject_burst_threshold BETWEEN 4 AND 64),
                reject_burst_window_seconds INTEGER NOT NULL
                    CHECK (reject_burst_window_seconds >= 60),
                reject_cooldown_seconds INTEGER NOT NULL
                    CHECK (reject_cooldown_seconds >= 300),
                reject_autoban_strike_threshold INTEGER NOT NULL
                    CHECK (
                        reject_autoban_strike_threshold BETWEEN 2 AND 32
                    ),
                reject_autoban_window_seconds INTEGER NOT NULL
                    CHECK (reject_autoban_window_seconds >= 3600),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (
                    reject_cooldown_seconds
                    >= reject_burst_window_seconds
                ),
                CHECK (
                    reject_cooldown_seconds
                    <= reject_autoban_window_seconds
                ),
                CHECK (
                    (reject_autoban_strike_threshold - 1)
                    * reject_cooldown_seconds
                    < reject_autoban_window_seconds
                )
            )
            """
        )
        existing = conn.execute(
            "SELECT 1 FROM librarian_policy WHERE singleton_id = 1"
        ).fetchone()
        if existing is None:
            values = _initial_librarian_policy_values()
            conn.execute(
                """
                INSERT INTO librarian_policy (
                singleton_id,
                revision,
                daily_query_cap,
                quota_window_seconds,
                reject_burst_threshold,
                reject_burst_window_seconds,
                reject_cooldown_seconds,
                reject_autoban_strike_threshold,
                reject_autoban_window_seconds
                ) VALUES (1, 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["daily_query_cap"],
                    values["quota_window_seconds"],
                    values["reject_burst_threshold"],
                    values["reject_burst_window_seconds"],
                    values["reject_cooldown_seconds"],
                    values["reject_autoban_strike_threshold"],
                    values["reject_autoban_window_seconds"],
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _read_librarian_policy_snapshot(
    conn: sqlite3.Connection,
) -> AbusePolicySnapshot:
    row = conn.execute(
        "SELECT * FROM librarian_policy WHERE singleton_id = 1"
    ).fetchone()
    if row is None:
        raise RuntimeError("Librarian policy singleton is missing")
    return AbusePolicySnapshot.from_row(row)


def get_librarian_policy_snapshot() -> AbusePolicySnapshot:
    conn = get_connection()
    try:
        return _read_librarian_policy_snapshot(conn)
    finally:
        conn.close()


def update_librarian_policy(
    **changes: int,
) -> AbusePolicySnapshot:
    """Atomically replace a partial policy and increment its revision."""
    allowed = {
        "daily_query_cap",
        "reject_burst_threshold",
        "reject_burst_window_seconds",
        "reject_cooldown_seconds",
        "reject_autoban_strike_threshold",
        "reject_autoban_window_seconds",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(
            f"unknown Librarian policy field: {sorted(unknown)[0]}"
        )
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = _read_librarian_policy_snapshot(conn)
        candidate_values = {
            "revision": current.revision + 1,
            "daily_query_cap": current.daily_query_cap,
            "quota_window_seconds": current.quota_window_seconds,
            "reject_burst_threshold": current.reject_burst_threshold,
            "reject_burst_window_seconds": (
                current.reject_burst_window_seconds
            ),
            "reject_cooldown_seconds": current.reject_cooldown_seconds,
            "reject_autoban_strike_threshold": (
                current.reject_autoban_strike_threshold
            ),
            "reject_autoban_window_seconds": (
                current.reject_autoban_window_seconds
            ),
        }
        candidate_values.update(changes)
        candidate = AbusePolicySnapshot(**candidate_values)
        conn.execute(
            """
            UPDATE librarian_policy
            SET revision = ?,
                daily_query_cap = ?,
                quota_window_seconds = ?,
                reject_burst_threshold = ?,
                reject_burst_window_seconds = ?,
                reject_cooldown_seconds = ?,
                reject_autoban_strike_threshold = ?,
                reject_autoban_window_seconds = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE singleton_id = 1
            """,
            (
                candidate.revision,
                candidate.daily_query_cap,
                candidate.quota_window_seconds,
                candidate.reject_burst_threshold,
                candidate.reject_burst_window_seconds,
                candidate.reject_cooldown_seconds,
                candidate.reject_autoban_strike_threshold,
                candidate.reject_autoban_window_seconds,
            ),
        )
        conn.commit()
        return candidate
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_librarian_abuse_state_schema() -> None:
    """Create the unfloodable Librarian abuse accumulator idempotently.

    The table has at most one UPDATE-in-place row per account. Both JSON
    arrays are capped by validated settings, counters saturate, and evidence
    has a fixed schema, so request volume cannot cause unbounded row growth.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS librarian_abuse_state (
                user_id INTEGER PRIMARY KEY,
                recent_rejects_json TEXT NOT NULL DEFAULT '[]',
                recent_cooldowns_json TEXT NOT NULL DEFAULT '[]',
                cooldown_until TEXT,
                cooldown_blocked_count INTEGER NOT NULL DEFAULT 0,
                duplicate_suppressed_count INTEGER NOT NULL DEFAULT 0,
                active_auto_ban INTEGER NOT NULL DEFAULT 0,
                auto_banned_at TEXT,
                evidence_json TEXT,
                last_restored_at TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        later_columns = (
            ("recent_rejects_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("recent_cooldowns_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("cooldown_until", "TEXT"),
            (
                "cooldown_blocked_count",
                "INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "duplicate_suppressed_count",
                "INTEGER NOT NULL DEFAULT 0",
            ),
            ("active_auto_ban", "INTEGER NOT NULL DEFAULT 0"),
            ("auto_banned_at", "TEXT"),
            ("evidence_json", "TEXT"),
            ("last_restored_at", "TEXT"),
            ("updated_at", "TEXT"),
        )
        for column_name, column_type in later_columns:
            if not _column_exists(
                cursor,
                "librarian_abuse_state",
                column_name,
            ):
                cursor.execute(
                    "ALTER TABLE librarian_abuse_state "
                    f"ADD COLUMN {column_name} {column_type}"
                )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS "
            "idx_librarian_abuse_state_cooldown_until "
            "ON librarian_abuse_state(cooldown_until)"
        )
        conn.commit()
    finally:
        conn.close()


_LIBRARIAN_ABUSE_COUNTER_CAP = 1_000_000
_LIBRARIAN_EVIDENCE_SAMPLE_CAP = 5
_LIBRARIAN_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def _parse_librarian_ring(raw: str, field_name: str) -> list:
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError(f"{field_name} must be a JSON array")
    return parsed


def _parse_librarian_timestamp(value: str) -> datetime:
    return datetime.strptime(
        value,
        _LIBRARIAN_TIMESTAMP_FORMAT,
    ).replace(tzinfo=timezone.utc)


def _materialize_librarian_cooldown_expiry(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    now_text: str,
) -> bool:
    """Persist one observed natural expiry and mint its new epoch."""
    cursor = conn.execute(
        """
        UPDATE librarian_abuse_state
        SET cooldown_until = NULL,
            recent_rejects_json = '[]',
            updated_at = ?
        WHERE user_id = ?
          AND cooldown_until IS NOT NULL
          AND cooldown_until <= ?
        """,
        (now_text, user_id, now_text),
    )
    if cursor.rowcount == 0:
        return False
    _increment_librarian_enforcement_epoch(conn, user_id=user_id)
    return True


def preflight_librarian_abuse_state(user_id: int) -> Dict[str, Any]:
    """Capture the epoch lease used by the later atomic decision."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now_text = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        now_utc = _parse_librarian_timestamp(now_text)
        _materialize_librarian_cooldown_expiry(
            conn,
            user_id=user_id,
            now_text=now_text,
        )
        row = conn.execute(
            """
            SELECT u.librarian_access,
                   u.librarian_enforcement_epoch,
                   COALESCE(las.active_auto_ban, 0) AS active_auto_ban,
                   las.cooldown_until
            FROM users AS u
            LEFT JOIN librarian_abuse_state AS las
                   ON las.user_id = u.id
            WHERE u.id = ?
            """,
            (user_id,),
        ).fetchone()
        if row is None:
            conn.commit()
            return {"status": "not_granted"}
        epoch = int(row["librarian_enforcement_epoch"])
        if not _librarian_access_allows_query(row["librarian_access"]):
            conn.commit()
            return {"status": "not_granted", "expected_epoch": epoch}
        if row["active_auto_ban"]:
            conn.commit()
            return {"status": "auto_banned", "expected_epoch": epoch}

        cooldown_until = row["cooldown_until"]
        if cooldown_until:
            cooldown_utc = _parse_librarian_timestamp(cooldown_until)
            conn.execute(
                """
                UPDATE librarian_abuse_state
                SET cooldown_blocked_count = MIN(
                        cooldown_blocked_count + 1,
                        ?
                    ),
                    updated_at = ?
                WHERE user_id = ?
                """,
                (_LIBRARIAN_ABUSE_COUNTER_CAP, now_text, user_id),
            )
            conn.commit()
            return {
                "status": "cooldown_active",
                "expected_epoch": epoch,
                "retry_after_seconds": max(
                    1,
                    math.ceil((cooldown_utc - now_utc).total_seconds()),
                ),
            }
        conn.commit()
        return {"status": "clear", "expected_epoch": epoch}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_librarian_rejection_and_update_abuse(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    meeting_id: Optional[int],
    query_hash: str,
    gate_version: str,
    reason_code: str,
    matched_rule_id: Optional[str],
    evaluation_ms: Optional[float],
    policy: AbusePolicySnapshot,
    enforcement_epoch_at_decision: int,
) -> Dict[str, Any]:
    """Record one rejection inside the caller's admission transaction."""
    burst_threshold = policy.reject_burst_threshold
    burst_window_seconds = policy.reject_burst_window_seconds
    cooldown_seconds = policy.reject_cooldown_seconds
    strike_threshold = policy.reject_autoban_strike_threshold
    autoban_window_seconds = policy.reject_autoban_window_seconds
    fingerprint = hashlib.sha256(
        (
            f"{query_hash}|{reason_code}|"
            f"{matched_rule_id or ''}"
        ).encode("utf-8")
    ).hexdigest()
    event_id = str(uuid.uuid4())
    try:
        now_text = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        now_utc = _parse_librarian_timestamp(now_text)
        conn.execute(
            """
            INSERT OR IGNORE INTO librarian_abuse_state (user_id)
            VALUES (?)
            """,
            (user_id,),
        )
        state = conn.execute(
            """
            SELECT *
            FROM librarian_abuse_state
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
        if state is None:
            raise RuntimeError("Librarian abuse state row disappeared")
        if state["active_auto_ban"]:
            return {
                "status": "auto_banned",
                "event_id": None,
                "enforcement_epoch": enforcement_epoch_at_decision,
            }

        cooldown_until = state["cooldown_until"]
        if cooldown_until:
            cooldown_utc = _parse_librarian_timestamp(cooldown_until)
            if cooldown_utc > now_utc:
                conn.execute(
                    """
                    UPDATE librarian_abuse_state
                    SET cooldown_blocked_count = MIN(
                            cooldown_blocked_count + 1,
                            ?
                        ),
                        updated_at = ?
                    WHERE user_id = ?
                    """,
                    (
                        _LIBRARIAN_ABUSE_COUNTER_CAP,
                        now_text,
                        user_id,
                    ),
                )
                return {
                    "status": "cooldown_active",
                    "event_id": None,
                    "enforcement_epoch": enforcement_epoch_at_decision,
                    "retry_after_seconds": max(
                        1,
                        math.ceil(
                            (cooldown_utc - now_utc).total_seconds()
                        ),
                    ),
                }

        reject_cutoff = now_utc - timedelta(
            seconds=burst_window_seconds
        )
        recent_rejects = [
            item
            for item in _parse_librarian_ring(
                state["recent_rejects_json"],
                "recent_rejects_json",
            )
            if (
                isinstance(item, dict)
                and isinstance(item.get("ts"), str)
                and isinstance(item.get("fp"), str)
                and _parse_librarian_timestamp(item["ts"]) > reject_cutoff
            )
        ]
        prior_count = len(recent_rejects)
        duplicate = any(
            item["fp"] == fingerprint for item in recent_rejects
        )

        if duplicate:
            conn.execute(
                """
                UPDATE librarian_abuse_state
                SET duplicate_suppressed_count = MIN(
                        duplicate_suppressed_count + 1,
                        ?
                    )
                WHERE user_id = ?
                """,
                (_LIBRARIAN_ABUSE_COUNTER_CAP, user_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO librarian_gate_events (
                    event_id,
                    user_id,
                    meeting_id,
                    query_hash,
                    gate_version,
                    stencil_result,
                    reason_code,
                    matched_rule_id,
                    evaluation_ms,
                    enforcement_epoch_at_decision,
                    policy_revision,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, 'rejected', ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    user_id,
                    meeting_id,
                    query_hash,
                    gate_version,
                    reason_code,
                    matched_rule_id,
                    evaluation_ms,
                    enforcement_epoch_at_decision,
                    policy.revision,
                    now_text,
                ),
            )

        recent_rejects.append({"ts": now_text, "fp": fingerprint})
        recent_rejects = recent_rejects[-burst_threshold:]
        burst_triggered = (
            prior_count < burst_threshold
            and len(recent_rejects) >= burst_threshold
        )
        duplicate_only_burst = (
            burst_triggered
            and len({item["fp"] for item in recent_rejects}) == 1
        )

        cooldown_cutoff = now_utc - timedelta(
            seconds=autoban_window_seconds
        )
        recent_cooldowns = [
            ts
            for ts in _parse_librarian_ring(
                state["recent_cooldowns_json"],
                "recent_cooldowns_json",
            )
            if (
                isinstance(ts, str)
                and _parse_librarian_timestamp(ts) > cooldown_cutoff
            )
        ]
        if burst_triggered and not duplicate_only_burst:
            recent_cooldowns.append(now_text)
            recent_cooldowns = recent_cooldowns[-strike_threshold:]

        auto_banned = (
            burst_triggered
            and not duplicate_only_burst
            and len(recent_cooldowns) >= strike_threshold
        )
        next_cooldown_until = None
        if burst_triggered and not auto_banned:
            next_cooldown_until = (
                now_utc + timedelta(seconds=cooldown_seconds)
            ).strftime(_LIBRARIAN_TIMESTAMP_FORMAT)

        evidence_json = state["evidence_json"]
        auto_banned_at = state["auto_banned_at"]
        if auto_banned:
            evidence_cutoff = (
                now_utc - timedelta(seconds=autoban_window_seconds)
            ).strftime(_LIBRARIAN_TIMESTAMP_FORMAT)
            event_counts = conn.execute(
                """
                SELECT COUNT(*) AS refused_count,
                       MIN(created_at) AS window_started_at
                FROM librarian_gate_events
                WHERE user_id = ?
                  AND stencil_result = 'rejected'
                  AND reason_code != 'evaluation_error'
                  AND created_at > ?
                """,
                (user_id, evidence_cutoff),
            ).fetchone()
            samples = conn.execute(
                """
                SELECT reason_code, matched_rule_id
                FROM librarian_gate_events
                WHERE user_id = ?
                  AND stencil_result = 'rejected'
                  AND reason_code != 'evaluation_error'
                  AND created_at > ?
                GROUP BY reason_code, matched_rule_id
                ORDER BY MIN(created_at) ASC
                LIMIT ?
                """,
                (
                    user_id,
                    evidence_cutoff,
                    _LIBRARIAN_EVIDENCE_SAMPLE_CAP,
                ),
            ).fetchall()
            duplicate_count = conn.execute(
                """
                SELECT duplicate_suppressed_count
                FROM librarian_abuse_state
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()[0]
            evidence_json = json.dumps(
                {
                    "refused_count": int(event_counts["refused_count"]),
                    "duplicate_suppressed_count": int(duplicate_count),
                    "burst_count": len(recent_cooldowns),
                    "window_started_at": (
                        event_counts["window_started_at"] or now_text
                    ),
                    "window_ended_at": now_text,
                    "samples": [
                        {
                            "reason_code": sample["reason_code"] or "",
                            "matched_rule_id": (
                                sample["matched_rule_id"] or ""
                            ),
                        }
                        for sample in samples
                    ],
                    "thresholds": {
                        "burst_threshold": burst_threshold,
                        "burst_window_seconds": burst_window_seconds,
                        "cooldown_seconds": cooldown_seconds,
                        "strike_threshold": strike_threshold,
                        "autoban_window_seconds": (
                            autoban_window_seconds
                        ),
                    },
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            auto_banned_at = now_text
            ban_cursor = conn.execute(
                """
                UPDATE users
                SET librarian_access = 'banned',
                    librarian_enforcement_epoch =
                        librarian_enforcement_epoch + 1
                WHERE id = ?
                  AND librarian_enforcement_epoch < ?
                """,
                (user_id, _SQLITE_MAX_INTEGER),
            )
            if ban_cursor.rowcount != 1:
                raise RuntimeError(
                    "auto-ban could not advance Librarian epoch"
                )

        if burst_triggered:
            # Re-arm the next burst. Production natural-expiry code is the
            # only other place that clears this ring.
            recent_rejects = []

        conn.execute(
            """
            UPDATE librarian_abuse_state
            SET recent_rejects_json = ?,
                recent_cooldowns_json = ?,
                cooldown_until = ?,
                active_auto_ban = ?,
                auto_banned_at = ?,
                evidence_json = ?,
                updated_at = ?
            WHERE user_id = ?
            """,
            (
                json.dumps(recent_rejects, separators=(",", ":")),
                json.dumps(recent_cooldowns, separators=(",", ":")),
                next_cooldown_until,
                1 if auto_banned else 0,
                auto_banned_at,
                evidence_json,
                now_text,
                user_id,
            ),
        )
        resulting_epoch = enforcement_epoch_at_decision
        if burst_triggered and not auto_banned:
            resulting_epoch = _increment_librarian_enforcement_epoch(
                conn,
                user_id=user_id,
            )
        elif auto_banned:
            epoch_row = conn.execute(
                """
                SELECT librarian_enforcement_epoch
                FROM users
                WHERE id = ?
                """,
                (user_id,),
            ).fetchone()
            if epoch_row is None:
                raise RuntimeError("auto-banned Librarian user disappeared")
            resulting_epoch = int(
                epoch_row["librarian_enforcement_epoch"]
            )
        if auto_banned:
            return {
                "status": "auto_banned",
                "event_id": None if duplicate else event_id,
                "duplicate": duplicate,
                "enforcement_epoch": resulting_epoch,
            }
        if burst_triggered:
            return {
                "status": "cooldown_started",
                "event_id": None if duplicate else event_id,
                "duplicate": duplicate,
                "strike_recorded": not duplicate_only_burst,
                "retry_after_seconds": cooldown_seconds,
                "enforcement_epoch": resulting_epoch,
            }
        return {
            "status": "rejected",
            "event_id": None if duplicate else event_id,
            "duplicate": duplicate,
            "enforcement_epoch": resulting_epoch,
        }
    except Exception:
        raise


def record_librarian_evaluation_failure(
    *,
    user_id: int,
    meeting_id: Optional[int],
    query_hash: str,
    gate_version: str,
    expected_epoch: int,
    evaluation_ms: Optional[float],
    error_class: str,
) -> Optional[str]:
    """Audit a stencil evaluator crash without touching abuse accounting."""
    event_id = str(uuid.uuid4())
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now_text = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        _materialize_librarian_cooldown_expiry(
            conn,
            user_id=user_id,
            now_text=now_text,
        )
        row = conn.execute(
            """
            SELECT librarian_access, librarian_enforcement_epoch
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        if (
            row is None
            or not _librarian_access_allows_query(
                row["librarian_access"]
            )
            or int(row["librarian_enforcement_epoch"]) != expected_epoch
        ):
            conn.commit()
            return None
        policy = _read_librarian_policy_snapshot(conn)
        conn.execute(
            """
            INSERT INTO librarian_gate_events (
                event_id,
                user_id,
                meeting_id,
                query_hash,
                gate_version,
                stencil_result,
                reason_code,
                evaluation_ms,
                error_class,
                enforcement_epoch_at_decision,
                policy_revision
            ) VALUES (?, ?, ?, ?, ?, 'rejected', 'evaluation_error',
                      ?, ?, ?, ?)
            """,
            (
                event_id,
                user_id,
                meeting_id,
                query_hash,
                gate_version,
                evaluation_ms,
                error_class,
                expected_epoch,
                policy.revision,
            ),
        )
        conn.commit()
        return event_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class StencilVerdict(Protocol):
    ok: bool
    canonical_query: Optional[str]
    reason_code: Optional[str]
    message: Optional[str]
    matched_rule_id: Optional[str]
    gate_version: str


@dataclass(frozen=True, slots=True)
class AdmittedResult:
    event_id: str
    enforcement_epoch: int
    policy_revision: int
    cap: int
    remaining: int
    status: Literal["admitted"] = "admitted"


@dataclass(frozen=True, slots=True)
class RejectedResult:
    event_id: Optional[str]
    rejection_status: Literal[
        "rejected",
        "cooldown_started",
        "auto_banned",
    ]
    duplicate: bool
    enforcement_epoch: int
    policy_revision: int
    retry_after_seconds: int = 0
    status: Literal["rejected"] = "rejected"


@dataclass(frozen=True, slots=True)
class AccessDeniedResult:
    reason: Literal["not_granted", "auto_banned"]
    status: Literal["access_denied"] = "access_denied"


@dataclass(frozen=True, slots=True)
class CooldownDeniedResult:
    retry_after_seconds: int
    status: Literal["cooldown_denied"] = "cooldown_denied"


@dataclass(frozen=True, slots=True)
class QuotaExhaustedResult:
    cap: int
    used: int
    retry_after_seconds: int
    unlock_at_utc: str
    status: Literal["quota_exhausted"] = "quota_exhausted"


@dataclass(frozen=True, slots=True)
class EpochChanged:
    current_epoch: int
    status: Literal["epoch_changed"] = "epoch_changed"


EvaluationResult: TypeAlias = (
    AdmittedResult
    | RejectedResult
    | AccessDeniedResult
    | CooldownDeniedResult
    | QuotaExhaustedResult
    | EpochChanged
)


def evaluate_and_record_librarian_query(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    meeting_id: int,
    raw_query: str,
    expected_epoch: int,
    thresholds: AbusePolicySnapshot,
    stencil_verdict: StencilVerdict,
) -> EvaluationResult:
    """Linearize access, epoch, quota, audit, and abuse state in one tx."""
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise TypeError("user_id must be an integer")
    if isinstance(meeting_id, bool) or not isinstance(meeting_id, int):
        raise TypeError("meeting_id must be an integer")
    if not isinstance(raw_query, str):
        raise TypeError("raw_query must be a string")
    if (
        isinstance(expected_epoch, bool)
        or not isinstance(expected_epoch, int)
        or expected_epoch < 0
    ):
        raise ValueError("expected_epoch must be a nonnegative integer")
    if not isinstance(thresholds, AbusePolicySnapshot):
        raise TypeError("thresholds must be an AbusePolicySnapshot")

    try:
        conn.execute("BEGIN IMMEDIATE")
        now_text = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        now_utc = _parse_librarian_timestamp(now_text)
        _materialize_librarian_cooldown_expiry(
            conn,
            user_id=user_id,
            now_text=now_text,
        )
        access = conn.execute(
            """
            SELECT u.librarian_access,
                   u.librarian_enforcement_epoch,
                   COALESCE(las.active_auto_ban, 0) AS active_auto_ban,
                   las.cooldown_until
            FROM users AS u
            LEFT JOIN librarian_abuse_state AS las
                   ON las.user_id = u.id
            WHERE u.id = ?
            """,
            (user_id,),
        ).fetchone()
        if access is None or not _librarian_access_allows_query(
            access["librarian_access"]
        ):
            conn.commit()
            return AccessDeniedResult(reason="not_granted")
        if access["active_auto_ban"]:
            conn.commit()
            return AccessDeniedResult(reason="auto_banned")
        if access["cooldown_until"]:
            cooldown_utc = _parse_librarian_timestamp(
                access["cooldown_until"]
            )
            conn.execute(
                """
                UPDATE librarian_abuse_state
                SET cooldown_blocked_count = MIN(
                        cooldown_blocked_count + 1,
                        ?
                    ),
                    updated_at = ?
                WHERE user_id = ?
                """,
                (_LIBRARIAN_ABUSE_COUNTER_CAP, now_text, user_id),
            )
            conn.commit()
            return CooldownDeniedResult(
                retry_after_seconds=max(
                    1,
                    math.ceil((cooldown_utc - now_utc).total_seconds()),
                )
            )

        current_epoch = int(access["librarian_enforcement_epoch"])
        if expected_epoch != current_epoch:
            conn.commit()
            return EpochChanged(current_epoch=current_epoch)

        # The parameter is a typed callsite contract. SQLite remains
        # authoritative, so a concurrent policy PATCH is resolved by using
        # the complete fresh row read under this write transaction.
        policy = _read_librarian_policy_snapshot(conn)
        if thresholds.revision == policy.revision and thresholds != policy:
            raise RuntimeError(
                "caller supplied a forged Librarian policy revision"
            )
        query_material = (
            stencil_verdict.canonical_query
            if stencil_verdict.ok
            and isinstance(stencil_verdict.canonical_query, str)
            else " ".join(raw_query.strip().split())
        )
        query_hash = hashlib.sha256(
            query_material.encode("utf-8")
        ).hexdigest()

        if not stencil_verdict.ok:
            if not stencil_verdict.reason_code:
                raise ValueError("rejected stencil verdict has no reason")
            recorded = record_librarian_rejection_and_update_abuse(
                conn,
                user_id=user_id,
                meeting_id=meeting_id,
                query_hash=query_hash,
                gate_version=stencil_verdict.gate_version,
                reason_code=stencil_verdict.reason_code,
                matched_rule_id=stencil_verdict.matched_rule_id,
                evaluation_ms=None,
                policy=policy,
                enforcement_epoch_at_decision=current_epoch,
            )
            conn.commit()
            return RejectedResult(
                event_id=recorded.get("event_id"),
                rejection_status=recorded["status"],
                duplicate=bool(recorded.get("duplicate", False)),
                enforcement_epoch=int(recorded["enforcement_epoch"]),
                policy_revision=policy.revision,
                retry_after_seconds=int(
                    recorded.get("retry_after_seconds", 0)
                ),
            )

        cutoff_text = (
            now_utc - timedelta(seconds=policy.quota_window_seconds)
        ).strftime(_LIBRARIAN_TIMESTAMP_FORMAT)
        used = int(conn.execute(
            """
            SELECT COUNT(*)
            FROM librarian_gate_events
            WHERE user_id = ?
              AND stencil_result = 'accepted'
              AND created_at > ?
            """,
            (user_id, cutoff_text),
        ).fetchone()[0])

        if used >= policy.daily_query_cap:
            unlock_row = conn.execute(
                """
                SELECT created_at
                FROM librarian_gate_events
                WHERE user_id = ?
                  AND stencil_result = 'accepted'
                  AND created_at > ?
                ORDER BY created_at ASC, rowid ASC
                LIMIT 1 OFFSET ?
                """,
                (
                    user_id,
                    cutoff_text,
                    used - policy.daily_query_cap,
                ),
            ).fetchone()
            if unlock_row is None:
                raise RuntimeError(
                    "active Librarian quota row disappeared in transaction"
                )
            unlock_utc = datetime.strptime(
                unlock_row["created_at"],
                _LIBRARIAN_TIMESTAMP_FORMAT,
            ).replace(tzinfo=timezone.utc) + timedelta(
                seconds=policy.quota_window_seconds
            )
            retry_after_seconds = max(
                1,
                math.ceil((unlock_utc - now_utc).total_seconds()),
            )
            conn.commit()
            return QuotaExhaustedResult(
                cap=policy.daily_query_cap,
                used=used,
                retry_after_seconds=retry_after_seconds,
                unlock_at_utc=unlock_utc.isoformat().replace(
                    "+00:00",
                    "Z",
                ),
            )

        event_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO librarian_gate_events (
                event_id,
                user_id,
                meeting_id,
                query_hash,
                gate_version,
                stencil_result,
                enforcement_epoch_at_decision,
                policy_revision,
                created_at
            ) VALUES (?, ?, ?, ?, ?, 'accepted', ?, ?, ?)
            """,
            (
                event_id,
                user_id,
                meeting_id,
                query_hash,
                stencil_verdict.gate_version,
                current_epoch,
                policy.revision,
                now_text,
            ),
        )
        conn.execute(
            """
            UPDATE librarian_abuse_state
            SET recent_rejects_json = '[]',
                updated_at = ?
            WHERE user_id = ?
            """,
            (now_text, user_id),
        )
        conn.commit()
        return AdmittedResult(
            event_id=event_id,
            enforcement_epoch=current_epoch,
            policy_revision=policy.revision,
            cap=policy.daily_query_cap,
            remaining=policy.daily_query_cap - used - 1,
        )
    except Exception:
        conn.rollback()
        raise


_LIBRARIAN_TERMINAL_FAILURE_REASONS = frozenset({
    "retrieval_failed",
    "revoked_before_retrieval",
    "revoked_during_retrieval",
    "envelope_build_failed",
    "envelope_persist_failed",
    "revoked_before_dispatch",
    "revoked_after_dispatch",
    "provider_dispatch_failed_terminal",
    "stream_aborted_terminal",
    "envelope_expired",
    "attempts_exhausted",
})


def _mark_librarian_event_terminal_failure_in_tx(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    reason: str,
    now_text: str,
) -> None:
    if reason not in _LIBRARIAN_TERMINAL_FAILURE_REASONS:
        raise ValueError(f"invalid Librarian terminal reason: {reason!r}")
    row = conn.execute(
        """
        SELECT stencil_result, terminal_failure_reason
        FROM librarian_gate_events
        WHERE event_id = ?
        """,
        (event_id,),
    ).fetchone()
    if row is None or row["stencil_result"] != "accepted":
        raise RuntimeError(
            "accepted Librarian gate event was not found for failure"
        )
    existing = row["terminal_failure_reason"]
    if existing == reason:
        return
    if existing is not None:
        raise RuntimeError(
            "Librarian event already has a different terminal failure: "
            f"{existing}"
        )
    cursor = conn.execute(
        """
        UPDATE librarian_gate_events
        SET terminal_failure_reason = ?,
            terminal_failed_at = ?
        WHERE event_id = ?
          AND stencil_result = 'accepted'
          AND terminal_failure_reason IS NULL
        """,
        (reason, now_text, event_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("Librarian terminal failure transition lost race")


def mark_librarian_event_terminal_failure(
    *,
    event_id: str,
    reason: str,
) -> None:
    """Preserve one accepted quota row and record its terminal outcome."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now_text = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        _mark_librarian_event_terminal_failure_in_tx(
            conn,
            event_id=event_id,
            reason=reason,
            now_text=now_text,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim_librarian_retrieval(
    *,
    event_id: str,
    retrieval_run_id: str,
) -> tuple[bool, str]:
    """Bind the run id at retrieval start iff the admitted epoch is current."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now_text = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        row = conn.execute(
            """
            SELECT gle.user_id,
                   gle.enforcement_epoch_at_decision,
                   gle.terminal_failure_reason,
                   u.librarian_access,
                   u.librarian_enforcement_epoch
            FROM librarian_gate_events AS gle
            JOIN users AS u ON u.id = gle.user_id
            WHERE gle.event_id = ?
              AND gle.stencil_result = 'accepted'
            """,
            (event_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("accepted Librarian retrieval row disappeared")
        _materialize_librarian_cooldown_expiry(
            conn,
            user_id=int(row["user_id"]),
            now_text=now_text,
        )
        state = conn.execute(
            """
            SELECT gle.enforcement_epoch_at_decision,
                   gle.terminal_failure_reason,
                   u.librarian_access,
                   u.librarian_enforcement_epoch,
                   COALESCE(las.active_auto_ban, 0) AS active_auto_ban,
                   las.cooldown_until
            FROM librarian_gate_events AS gle
            JOIN users AS u ON u.id = gle.user_id
            LEFT JOIN librarian_abuse_state AS las
                   ON las.user_id = gle.user_id
            WHERE gle.event_id = ?
            """,
            (event_id,),
        ).fetchone()
        if state is None:
            raise RuntimeError("accepted Librarian retrieval row disappeared")
        current = (
            state["terminal_failure_reason"] is None
            and _librarian_access_allows_query(
                state["librarian_access"]
            )
            and not state["active_auto_ban"]
            and not state["cooldown_until"]
            and state["enforcement_epoch_at_decision"] is not None
            and int(state["enforcement_epoch_at_decision"])
            == int(state["librarian_enforcement_epoch"])
        )
        if not current:
            if state["terminal_failure_reason"] is None:
                _mark_librarian_event_terminal_failure_in_tx(
                    conn,
                    event_id=event_id,
                    reason="revoked_before_retrieval",
                    now_text=now_text,
                )
            conn.commit()
            return False, "admission_state_changed"
        cursor = conn.execute(
            """
            UPDATE librarian_gate_events
            SET retrieval_run_id = ?
            WHERE event_id = ?
              AND terminal_failure_reason IS NULL
            """,
            (retrieval_run_id, event_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Librarian retrieval claim row disappeared")
        conn.commit()
        return True, ""
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def librarian_result_epoch_is_current(
    *,
    event_id: str,
    terminal_reason: str,
) -> bool:
    """Linearize release of a buffered result against revocation."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now_text = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        row = conn.execute(
            """
            SELECT user_id
            FROM librarian_gate_events
            WHERE event_id = ?
              AND stencil_result = 'accepted'
            """,
            (event_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("accepted Librarian result row disappeared")
        _materialize_librarian_cooldown_expiry(
            conn,
            user_id=int(row["user_id"]),
            now_text=now_text,
        )
        state = conn.execute(
            """
            SELECT gle.enforcement_epoch_at_decision,
                   gle.terminal_failure_reason,
                   u.librarian_access,
                   u.librarian_enforcement_epoch,
                   COALESCE(las.active_auto_ban, 0) AS active_auto_ban,
                   las.cooldown_until
            FROM librarian_gate_events AS gle
            JOIN users AS u ON u.id = gle.user_id
            LEFT JOIN librarian_abuse_state AS las
                   ON las.user_id = gle.user_id
            WHERE gle.event_id = ?
            """,
            (event_id,),
        ).fetchone()
        if state is None:
            raise RuntimeError("accepted Librarian result row disappeared")
        current = (
            state["terminal_failure_reason"] is None
            and _librarian_access_allows_query(
                state["librarian_access"]
            )
            and not state["active_auto_ban"]
            and not state["cooldown_until"]
            and state["enforcement_epoch_at_decision"] is not None
            and int(state["enforcement_epoch_at_decision"])
            == int(state["librarian_enforcement_epoch"])
        )
        if not current and state["terminal_failure_reason"] is None:
            _mark_librarian_event_terminal_failure_in_tx(
                conn,
                event_id=event_id,
                reason=terminal_reason,
                now_text=now_text,
            )
        conn.commit()
        return current
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim_librarian_provider_dispatch(user_id: int) -> bool:
    """Fresh access claim for signed-in provider calls without an envelope."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now_text = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        _materialize_librarian_cooldown_expiry(
            conn,
            user_id=user_id,
            now_text=now_text,
        )
        row = conn.execute(
            """
            SELECT u.librarian_access,
                   COALESCE(las.active_auto_ban, 0) AS active_auto_ban,
                   las.cooldown_until
            FROM users AS u
            LEFT JOIN librarian_abuse_state AS las
                   ON las.user_id = u.id
            WHERE u.id = ?
            """,
            (user_id,),
        ).fetchone()
        allowed = bool(
            row
            and _librarian_access_allows_query(row["librarian_access"])
            and not row["active_auto_ban"]
            and not row["cooldown_until"]
        )
        conn.commit()
        return allowed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_byok_audit_run(run_id: str) -> Optional[Dict[str, Any]]:
    """Load a single BYOK audit-run by run_id; return None if absent.

    Returns the raw fields as stored. Caller is responsible for stripping
    any fields the public surface shouldn't see per BYOK_ARCHITECTURE_SPEC
    § 5.5.3 (Z-SPAN doesn't store cleartext queries / IPs / display names
    on this row, so the current shape is already safe to expose publicly —
    but if future kinds add sensitive payloads, the caller's strip-list
    must keep pace).
    """
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT run_id, kind, meeting_id, timestamp_utc,
                   prompt_template_version, prompt_template_hash,
                   query_hash, vector_ids_json,
                   provider, model, supersedes, child_run_ids_json,
                   created_at
            FROM byok_audit_runs
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    d = dict(row)
    try:
        d["vector_ids"] = json.loads(d.pop("vector_ids_json"))
    except (json.JSONDecodeError, TypeError):
        d["vector_ids"] = []
    raw_children = d.pop("child_run_ids_json", None)
    if raw_children:
        try:
            d["child_run_ids"] = json.loads(raw_children)
        except (json.JSONDecodeError, TypeError):
            d["child_run_ids"] = []
    else:
        d["child_run_ids"] = []
    return d


# ── S-122 Report-V0-1 — report_runs (cited-report generator run state) ──
# One row per report generation. The daemon thread spawned by
# POST /api/report-runs mutates the row as it progresses; the ReportModal
# polls GET /api/report-runs/<id>. artifact_html is the single-file
# report (typically 50-300KB) — excluded from the poll payload and served
# by its own endpoint.


def init_episode_audit_runs_schema():
    """Create the episode_audit_runs table + indexes if absent. Idempotent."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS episode_audit_runs (
            run_id TEXT PRIMARY KEY,
            meeting_id INTEGER NOT NULL,
            outputs_snapshot_hash TEXT NOT NULL,
            auditor_version TEXT NOT NULL,
            prompt_sha256 TEXT NOT NULL,
            model TEXT NOT NULL,
            effort TEXT,
            run_status TEXT NOT NULL,
            verdict TEXT NOT NULL,
            findings_count INTEGER NOT NULL DEFAULT 0,
            open_findings_count INTEGER NOT NULL DEFAULT 0,
            suggestions_count INTEGER NOT NULL DEFAULT 0,
            deterministic_flags_count INTEGER NOT NULL DEFAULT 0,
            report_json TEXT NOT NULL,
            started_at_utc TEXT NOT NULL,
            duration_seconds REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(meeting_id, outputs_snapshot_hash, auditor_version)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_episode_audit_runs_meeting_id ON episode_audit_runs(meeting_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_episode_audit_runs_created ON episode_audit_runs(created_at)")
    conn.commit()
    conn.close()


def save_episode_audit_run(**fields: Any) -> None:
    """Insert or replace one episode-auditor observation."""
    columns = (
        "run_id",
        "meeting_id",
        "outputs_snapshot_hash",
        "auditor_version",
        "prompt_sha256",
        "model",
        "effort",
        "run_status",
        "verdict",
        "findings_count",
        "open_findings_count",
        "suggestions_count",
        "deterministic_flags_count",
        "report_json",
        "started_at_utc",
        "duration_seconds",
    )
    missing = [column for column in columns if column not in fields]
    if missing:
        raise ValueError(
            "missing episode audit run fields: " + ", ".join(missing)
        )
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO episode_audit_runs (
                run_id, meeting_id, outputs_snapshot_hash, auditor_version,
                prompt_sha256, model, effort, run_status, verdict,
                findings_count, open_findings_count, suggestions_count,
                deterministic_flags_count, report_json, started_at_utc,
                duration_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(fields[column] for column in columns),
        )
        conn.commit()
    finally:
        conn.close()


def get_latest_episode_audit_run(meeting_id: int) -> Optional[Dict[str, Any]]:
    """Return the newest episode audit run, tolerating corrupt report JSON."""
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT *
            FROM episode_audit_runs
            WHERE meeting_id = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (meeting_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    result = dict(row)
    try:
        result["report"] = json.loads(result["report_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        result["report_json_raw"] = result.get("report_json")
        result["report"] = None
    return result


def get_episode_audit_run(run_id: str) -> Optional[Dict[str, Any]]:
    """Return one episode audit run by ID, tolerating corrupt report JSON."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM episode_audit_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    result = dict(row)
    try:
        result["report"] = json.loads(result["report_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        result["report_json_raw"] = result.get("report_json")
        result["report"] = None
    return result


def init_episode_audit_fix_events_schema():
    """Create the append-only episode audit fix-event table and indexes."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS episode_audit_fix_events (
            event_id TEXT PRIMARY KEY,
            meeting_id INTEGER NOT NULL,
            run_id TEXT NOT NULL,
            proposal_id TEXT NOT NULL,
            disposition TEXT NOT NULL,
            reason TEXT,
            actor TEXT NOT NULL,
            target_output TEXT NOT NULL,
            before_text TEXT NOT NULL,
            after_text TEXT NOT NULL,
            pre_content_sha256 TEXT,
            post_content_sha256 TEXT,
            validation_json TEXT,
            was_published INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS "
        "idx_episode_audit_fix_events_meeting_id "
        "ON episode_audit_fix_events(meeting_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS "
        "idx_episode_audit_fix_events_run_proposal "
        "ON episode_audit_fix_events(run_id, proposal_id)"
    )
    conn.commit()
    conn.close()


def save_episode_audit_fix_event(**fields: Any) -> None:
    """Append one immutable episode audit fix event."""
    columns = (
        "event_id",
        "meeting_id",
        "run_id",
        "proposal_id",
        "disposition",
        "reason",
        "actor",
        "target_output",
        "before_text",
        "after_text",
        "pre_content_sha256",
        "post_content_sha256",
        "validation_json",
        "was_published",
    )
    missing = [column for column in columns if column not in fields]
    if missing:
        raise ValueError(
            "missing episode audit fix event fields: " + ", ".join(missing)
        )
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO episode_audit_fix_events (
                event_id, meeting_id, run_id, proposal_id, disposition,
                reason, actor, target_output, before_text, after_text,
                pre_content_sha256, post_content_sha256, validation_json,
                was_published
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(fields[column] for column in columns),
        )
        conn.commit()
    finally:
        conn.close()


def get_episode_audit_fix_events(
    meeting_id: int,
) -> List[Dict[str, Any]]:
    """Return a meeting's fix events newest first with tolerant validation."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM episode_audit_fix_events
            WHERE meeting_id = ?
            ORDER BY created_at DESC, rowid DESC
            """,
            (meeting_id,),
        ).fetchall()
    finally:
        conn.close()

    events: List[Dict[str, Any]] = []
    for row in rows:
        event = dict(row)
        try:
            event["validation"] = json.loads(event["validation_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            event["validation_json_raw"] = event.get("validation_json")
            event["validation"] = None
        events.append(event)
    return events


def init_report_runs_schema():
    """Create the report_runs table + indexes if absent. Idempotent."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS report_runs (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            status TEXT NOT NULL,
            progress TEXT,
            current_section TEXT,
            query TEXT NOT NULL,
            interpretation_json TEXT,
            meeting_ids_json TEXT,
            sections_json TEXT,
            citations_json TEXT,
            leg_outcomes_json TEXT,
            run_id TEXT,
            child_run_ids_json TEXT,
            artifact_html TEXT,
            error TEXT
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_report_runs_created ON report_runs(created_at)"
    )
    # Report-Stitch-1 (V0.5) — generative-chrome state rides the same row
    # (one stitch attempt per report, latest-wins; the edit history JSON
    # is the audit trail mirroring fractal-framework's StitchEdit[]).
    for col, decl in [
        ("stitch_status", "TEXT"),
        ("stitch_progress", "TEXT"),
        ("stitch_project_id", "TEXT"),
        ("stitch_edits_json", "TEXT"),
        ("stitch_artifact_html", "TEXT"),
        ("stitch_error", "TEXT"),
    ]:
        if not _column_exists(cursor, "report_runs", col):
            cursor.execute(f"ALTER TABLE report_runs ADD COLUMN {col} {decl}")
    conn.commit()
    conn.close()


_REPORT_RUN_JSON_FIELDS = {
    "interpretation": "interpretation_json",
    "meeting_ids": "meeting_ids_json",
    "sections": "sections_json",
    "citations": "citations_json",
    "leg_outcomes": "leg_outcomes_json",
    "child_run_ids": "child_run_ids_json",
    "stitch_edits": "stitch_edits_json",
}

_REPORT_RUN_PLAIN_FIELDS = {
    "status", "progress", "current_section", "run_id", "artifact_html", "error",
    "stitch_status", "stitch_progress", "stitch_project_id",
    "stitch_artifact_html", "stitch_error",
}


def create_report_run(
    report_run_id: str,
    query: str,
    interpretation: Optional[Dict[str, Any]],
    meeting_ids: List[int],
) -> Dict[str, Any]:
    """Insert a pending report_runs row; returns the loaded row."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO report_runs (
                id, created_at, updated_at, status, progress,
                query, interpretation_json, meeting_ids_json
            ) VALUES (?, ?, ?, 'pending', 'Queued...', ?, ?, ?)
            """,
            (
                report_run_id, now, now, query,
                json.dumps(interpretation) if interpretation is not None else None,
                json.dumps(meeting_ids),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return get_report_run(report_run_id) or {}


def get_report_run(
    report_run_id: str, include_artifact: bool = False
) -> Optional[Dict[str, Any]]:
    """Load one report run. artifact_html excluded unless requested (poll
    payloads stay small); has_artifact flags availability either way."""
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM report_runs WHERE id = ?", (report_run_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    d = dict(row)
    for public, col in _REPORT_RUN_JSON_FIELDS.items():
        raw = d.pop(col, None)
        try:
            d[public] = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            d[public] = None
    d["has_artifact"] = bool(d.get("artifact_html"))
    d["has_stitch_artifact"] = bool(d.get("stitch_artifact_html"))
    if not include_artifact:
        d.pop("artifact_html", None)
        d.pop("stitch_artifact_html", None)
    return d


def update_report_run(report_run_id: str, **fields: Any) -> None:
    """Patch a report_runs row. JSON-typed kwargs (interpretation,
    meeting_ids, sections, citations, leg_outcomes, child_run_ids) are
    serialized; plain kwargs land as-is. Always bumps updated_at."""
    sets: List[str] = ["updated_at = ?"]
    params: List[Any] = [datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")]
    for key, value in fields.items():
        if key in _REPORT_RUN_JSON_FIELDS:
            sets.append(f"{_REPORT_RUN_JSON_FIELDS[key]} = ?")
            params.append(json.dumps(value) if value is not None else None)
        elif key in _REPORT_RUN_PLAIN_FIELDS:
            sets.append(f"{key} = ?")
            params.append(value)
        else:
            raise ValueError(f"update_report_run: unknown field {key!r}")
    params.append(report_run_id)
    conn = get_connection()
    try:
        conn.execute(
            f"UPDATE report_runs SET {', '.join(sets)} WHERE id = ?", params
        )
        conn.commit()
    finally:
        conn.close()


# ─── RR-4 — Public corrections log helpers (S-043 B-4) ───

_CORRECTION_STATUSES = {"under_review", "corrected", "record_stands", "disputed_ambiguous"}


def list_corrections(include_internal: bool = False) -> list[dict]:
    """Public corrections log, newest first, joined to meeting context.

    `include_internal` adds detail_internal (owner-side notes) — the
    public endpoint must NEVER pass True for anonymous callers.
    """
    internal_col = ", c.detail_internal" if include_internal else ""
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT c.id, c.meeting_id, c.corrected_surface, c.status,
                   c.summary_public, c.reported_at, c.resolved_at,
                   m.city_name, m.meeting_date, m.meeting_title{internal_col}
            FROM corrections c
            LEFT JOIN meetings m ON m.id = c.meeting_id
            ORDER BY c.reported_at DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def create_correction(
    meeting_id: int | None,
    corrected_surface: str | None,
    status: str = "under_review",
    summary_public: str | None = None,
    detail_internal: str | None = None,
) -> int:
    """Owner-side: log a correction row. Returns the new row id."""
    if status not in _CORRECTION_STATUSES:
        raise ValueError(f"create_correction: bad status {status!r}")
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO corrections "
            "(meeting_id, corrected_surface, status, summary_public, detail_internal) "
            "VALUES (?, ?, ?, ?, ?)",
            (meeting_id, corrected_surface, status, summary_public, detail_internal),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_correction(correction_id: int, **fields) -> bool:
    """Owner-side: update status / summaries on a logged correction.

    Setting a terminal status (anything but under_review) stamps
    resolved_at automatically unless the caller passed it explicitly.
    Returns False when the row doesn't exist.
    """
    allowed = {"status", "summary_public", "detail_internal", "corrected_surface", "resolved_at"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"update_correction: unknown fields {sorted(unknown)}")
    status = fields.get("status")
    if status is not None and status not in _CORRECTION_STATUSES:
        raise ValueError(f"update_correction: bad status {status!r}")
    if status and status != "under_review" and "resolved_at" not in fields:
        fields["resolved_at"] = None  # placeholder; swapped to now() below
    sets, params = [], []
    for k, v in fields.items():
        if k == "resolved_at" and v is None and status and status != "under_review":
            sets.append("resolved_at = CURRENT_TIMESTAMP")
        else:
            sets.append(f"{k} = ?")
            params.append(v)
    params.append(correction_id)
    conn = get_connection()
    try:
        cur = conn.execute(
            f"UPDATE corrections SET {', '.join(sets)} WHERE id = ?", params
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# Initialize on import
init_db()

# V1.5-Verify-1 — wire into the init chain AFTER init_db() so the canonical
# schema (cities + meetings + scrape_log + ...) and the notebook schema both
# land first. The byok_audit_runs table is independent of those (no foreign
# keys back), but init order discipline matters for any future schema that
# DOES need to reference notebook_outputs / meetings.
init_byok_audit_runs_schema()
init_librarian_gate_events_schema()
init_librarian_abuse_state_schema()
init_librarian_policy_schema()
init_episode_audit_runs_schema()
init_episode_audit_fix_events_schema()

# S-122 Report-V0-1 — same independence as byok_audit_runs (no FKs).
init_report_runs_schema()
