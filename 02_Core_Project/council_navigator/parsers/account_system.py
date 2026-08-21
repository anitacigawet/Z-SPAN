"""account_system — data-access helpers for the S-012 + D-095 schema.

Per [`ACCOUNT_SYSTEM_SPEC.md`](../../01_Project_Overview/ACCOUNT_SYSTEM_SPEC.md)
chunks 1 + 7-9. The auth flow (chunks 2-3) ships once James provides
Google Cloud Web OAuth client credentials; these helpers are usable
end-to-end at that point with no further DB work.

Per [D-100](../../01_Project_Overview/DECISIONS.md#d-100): defensive
data-access primitives — no LLM calls.

Helpers:
  Foundation (chunk 1):
    - upsert_user_from_google(google_sub, email, display_name, avatar_url)
    - get_user(user_id)
    - get_user_by_google_sub(google_sub)
    - follow_add / follow_remove / list_follows
    - revival_request_add / list_revival_requests
    - set_notification_prefs / get_notification_prefs

  Creator extension (chunks 7-9):
    - promote_user_to_creator(user_id, tos_version, disclaimer_version,
        signup_ip_hash)
    - revoke_creator_role(user_id, reason)
    - get_active_agreement(user_id)
    - log_creator_download(user_id, asset_id, asset_type,
        download_source_ip_hash)
    - get_creator_download_summary(user_id)
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Literal, Optional

logger = logging.getLogger(__name__)

# Resolve the `database` module under both runtime contexts:
#   - tests run from council_navigator/ → `from parsers import database`
#   - Flask runs from parsers/ cwd (sibling) → `import database`
# Either form leaves the same module bound to the local name `database`
# for every helper below.
try:
    from parsers import database  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — Flask sibling-import fallback
    import database  # type: ignore[no-redef]

try:
    from parsers.topic_tags import TOPIC_TAG_IDS
except ImportError:  # pragma: no cover — Flask sibling-import fallback
    from topic_tags import TOPIC_TAG_IDS  # type: ignore[no-redef]


# ── Dataclasses ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class User:
    id: int
    google_sub: Optional[str]
    email: str
    display_name: Optional[str]
    avatar_url: Optional[str]
    role: str  # 'light' | 'creator' | 'verified-creator'
    created_at: str
    last_seen_at: str


@dataclass(frozen=True)
class CreatorAgreement:
    id: int
    user_id: int
    tos_version: str
    disclaimer_version: str
    disclaimer_acknowledged_at: str
    signed_at: str
    revoked_at: Optional[str]
    revoked_reason: Optional[str]
    signup_ip_hash: Optional[str]


@dataclass(frozen=True)
class CreatorDownload:
    id: int
    user_id: int
    asset_id: str
    asset_type: str
    tos_version_at_download: str
    download_source_ip_hash: Optional[str]
    downloaded_at: str


@dataclass(frozen=True)
class CreatorDownloadSummary:
    """V0 aggregate view per the SPEC's redline decision 2."""

    user_id: int
    total_downloads: int
    most_recent_at: Optional[str]


# ── Foundation helpers (chunk 1) ──────────────────────────────────────


def normalize_account_email(email: str) -> str:
    """Canonical email form used by every account identity provider."""
    return email.strip().casefold()


def upsert_user_from_google(
    google_sub: str,
    email: str,
    display_name: Optional[str] = None,
    avatar_url: Optional[str] = None,
) -> User:
    """Insert or link a verified Google identity to one canonical account.

    Called from the OAuth callback (chunk 2) when an authenticated
    Google identity arrives. If an invited email/password account already
    owns the same verified email, Google is attached to that row rather than
    creating a duplicate account.
    """
    normalized_email = normalize_account_email(email)
    conn = database.get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        by_sub = conn.execute(
            "SELECT id FROM users WHERE google_sub = ?",
            (google_sub,),
        ).fetchone()
        by_email = conn.execute(
            "SELECT id FROM users WHERE lower(email) = ?",
            (normalized_email,),
        ).fetchone()

        if by_sub is not None and by_email is not None and by_sub[0] != by_email[0]:
            raise ValueError("Google identity conflicts with an existing account")

        existing = by_sub or by_email
        if existing is None:
            cursor = conn.execute(
                """
                INSERT INTO users (
                    google_sub, email, display_name, avatar_url, role
                ) VALUES (?, ?, ?, ?, 'light')
                """,
                (google_sub, normalized_email, display_name, avatar_url),
            )
            user_id = int(cursor.lastrowid)
        else:
            user_id = int(existing[0])
            conn.execute(
                """
                UPDATE users
                SET google_sub = ?,
                    email = ?,
                    display_name = COALESCE(?, display_name),
                    avatar_url = COALESCE(?, avatar_url),
                    last_seen_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    google_sub,
                    normalized_email,
                    display_name,
                    avatar_url,
                    user_id,
                ),
            )

        conn.commit()
        fetched = conn.execute(
            "SELECT id, google_sub, email, display_name, avatar_url, role, "
            "created_at, last_seen_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if fetched is None:
            raise RuntimeError("Google account upsert did not persist")
        return User(*fetched)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_user(user_id: int) -> Optional[User]:
    conn = database.get_connection()
    row = conn.execute(
        "SELECT id, google_sub, email, display_name, avatar_url, role, "
        "created_at, last_seen_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    conn.close()
    return User(*row) if row else None


def get_user_by_google_sub(google_sub: str) -> Optional[User]:
    conn = database.get_connection()
    row = conn.execute(
        "SELECT id, google_sub, email, display_name, avatar_url, role, "
        "created_at, last_seen_at FROM users WHERE google_sub = ?",
        (google_sub,),
    ).fetchone()
    conn.close()
    return User(*row) if row else None


def get_user_by_email(email: str) -> Optional[User]:
    conn = database.get_connection()
    row = conn.execute(
        "SELECT id, google_sub, email, display_name, avatar_url, role, "
        "created_at, last_seen_at FROM users WHERE lower(email) = ?",
        (normalize_account_email(email),),
    ).fetchone()
    conn.close()
    return User(*row) if row else None


# Per-user follow cap — bounded to prevent runaway row-spam from a
# signed-in user. Session-103 product-slice2 sizing: 100 is comfortably
# above any real civic-interest fleet (all AZ cities + 5 topic tags +
# starred meetings) while low enough that the payload embedded in every
# /api/auth/me response stays small. Deletes are always allowed, even
# past the cap, so a user can never lock themselves out of unfollowing.
FOLLOW_CAP_PER_USER = 100


def _user_follow_count(conn, user_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM follows WHERE user_id = ?", (user_id,)
    ).fetchone()
    return int(row[0]) if row else 0


def follow_add(
    user_id: int,
    target_type: Literal["city", "county", "topic", "meeting"],
    target_key: str,
) -> bool:
    """Idempotent follow. Returns True if a new row was inserted, False
    if the user was already following this target.

    Raises FollowCapExceeded if the caller has already reached
    FOLLOW_CAP_PER_USER active follows AND the requested target is not
    already in the set (so idempotent re-adds of an existing follow are
    always allowed).
    """
    conn = database.get_connection()
    try:
        cursor = conn.cursor()
        # Cheap dedup check first — an idempotent re-add of an existing
        # row should never be blocked by the cap.
        existing = cursor.execute(
            "SELECT 1 FROM follows WHERE user_id = ? AND target_type = ? "
            "AND target_key = ? LIMIT 1",
            (user_id, target_type, target_key),
        ).fetchone()
        if existing is None and _user_follow_count(conn, user_id) >= FOLLOW_CAP_PER_USER:
            raise FollowCapExceeded(
                f"user {user_id} has {FOLLOW_CAP_PER_USER} follows; "
                "remove one before adding another"
            )
        cursor.execute(
            "INSERT OR IGNORE INTO follows (user_id, target_type, target_key) "
            "VALUES (?, ?, ?)",
            (user_id, target_type, target_key),
        )
        inserted = cursor.rowcount > 0
        conn.commit()
        return inserted
    finally:
        conn.close()


class FollowCapExceeded(Exception):
    """Raised by follow_add when the user has hit FOLLOW_CAP_PER_USER."""


def follow_remove(
    user_id: int,
    target_type: Literal["city", "county", "topic", "meeting"],
    target_key: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Remove one follow, optionally inside a caller-owned transaction."""
    own_connection = conn is None
    if conn is None:
        conn = database.get_connection()
    try:
        if target_type == "city":
            cursor = conn.execute(
                "DELETE FROM follows "
                "WHERE user_id = ? AND target_type = ? "
                "AND target_key COLLATE NOCASE = ?",
                (user_id, target_type, target_key),
            )
        else:
            cursor = conn.execute(
                "DELETE FROM follows "
                "WHERE user_id = ? AND target_type = ? AND target_key = ?",
                (user_id, target_type, target_key),
            )
        if own_connection:
            conn.commit()
        return cursor.rowcount > 0
    except Exception:
        if own_connection:
            conn.rollback()
        raise
    finally:
        if own_connection:
            conn.close()


def list_follows(user_id: int) -> list[dict]:
    conn = database.get_connection()
    rows = conn.execute(
        "SELECT target_type, target_key, created_at FROM follows "
        "WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()
    conn.close()
    return [
        {"target_type": r[0], "target_key": r[1], "created_at": r[2]}
        for r in rows
    ]


def list_city_topics(user_id: int) -> dict[str, list[str]]:
    """Return a map of canonical city keys to enabled topic tag ids.

    Returns an empty dict when the user has no per-city topic preferences.
    """
    conn = database.get_connection()
    try:
        rows = conn.execute(
            "SELECT city_key, tag_id FROM follow_city_topics "
            "WHERE user_id = ? ORDER BY city_key, tag_id",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    city_topics: dict[str, list[str]] = {}
    for city_key, tag_id in rows:
        city_topics.setdefault(city_key, []).append(tag_id)
    return city_topics


def set_city_topics(
    user_id: int,
    city_key: str,
    tag_ids: list[str],
) -> list[str]:
    """Replace one city's enabled topics and return the canonical stored list.

    Tag ids are lowercased, deduplicated, and intersected with
    ``TOPIC_TAG_IDS`` before the replacement is committed.
    """
    allowed_tag_ids = frozenset(TOPIC_TAG_IDS)
    canonical_tag_ids = sorted({
        normalized
        for tag_id in tag_ids
        if isinstance(tag_id, str)
        if (normalized := tag_id.strip().lower()) in allowed_tag_ids
    })

    conn = database.get_connection()
    try:
        with conn:
            conn.execute(
                "DELETE FROM follow_city_topics "
                "WHERE user_id = ? AND city_key = ?",
                (user_id, city_key),
            )
            conn.executemany(
                "INSERT INTO follow_city_topics (user_id, city_key, tag_id) "
                "VALUES (?, ?, ?)",
                (
                    (user_id, city_key, tag_id)
                    for tag_id in canonical_tag_ids
                ),
            )
    finally:
        conn.close()
    return canonical_tag_ids


def clear_city_topics(
    user_id: int,
    city_key: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Remove a city's topics, optionally in a caller-owned transaction."""
    own_connection = conn is None
    if conn is None:
        conn = database.get_connection()
    try:
        cursor = conn.execute(
            "DELETE FROM follow_city_topics "
            "WHERE user_id = ? AND city_key = ?",
            (user_id, city_key),
        )
        if own_connection:
            conn.commit()
        return cursor.rowcount
    except Exception:
        if own_connection:
            conn.rollback()
        raise
    finally:
        if own_connection:
            conn.close()


def revival_request_add(
    user_id: int,
    target_type: Literal["city", "county"],
    target_key: str,
) -> bool:
    conn = database.get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO channel_revival_requests "
        "(user_id, target_type, target_key) VALUES (?, ?, ?)",
        (user_id, target_type, target_key),
    )
    inserted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return inserted


def list_revival_requests(user_id: int) -> list[dict]:
    conn = database.get_connection()
    rows = conn.execute(
        "SELECT target_type, target_key, created_at FROM channel_revival_requests "
        "WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()
    conn.close()
    return [
        {"target_type": r[0], "target_key": r[1], "created_at": r[2]}
        for r in rows
    ]


def set_notification_prefs(
    user_id: int,
    digest_cadence: Literal["off", "daily", "weekly", "monthly"] = "weekly",
    email_enabled: bool = True,
) -> None:
    conn = database.get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO notification_prefs (user_id, digest_cadence, email_enabled, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            digest_cadence = excluded.digest_cadence,
            email_enabled = excluded.email_enabled,
            updated_at = CURRENT_TIMESTAMP
        """,
        (user_id, digest_cadence, 1 if email_enabled else 0),
    )
    conn.commit()
    conn.close()


def get_notification_prefs(user_id: int) -> dict:
    conn = database.get_connection()
    row = conn.execute(
        "SELECT digest_cadence, email_enabled, updated_at "
        "FROM notification_prefs WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    conn.close()
    if row is None:
        return {
            "digest_cadence": "weekly",
            "email_enabled": True,
            "updated_at": None,
        }
    return {
        "digest_cadence": row[0],
        "email_enabled": bool(row[1]),
        "updated_at": row[2],
    }


# ── Creator extension helpers (chunks 7-9) ────────────────────────────


class CreatorPromotionError(RuntimeError):
    """Raised when a promotion attempt is malformed (user already a
    creator, user does not exist, etc.)."""


def get_active_agreement(user_id: int) -> Optional[CreatorAgreement]:
    """Return the user's CURRENTLY-ACTIVE creator agreement (revoked_at
    IS NULL), or None if the user has no active agreement."""
    conn = database.get_connection()
    row = conn.execute(
        """
        SELECT id, user_id, tos_version, disclaimer_version,
               disclaimer_acknowledged_at, signed_at, revoked_at,
               revoked_reason, signup_ip_hash
        FROM creator_agreements
        WHERE user_id = ? AND revoked_at IS NULL
        ORDER BY signed_at DESC LIMIT 1
        """,
        (user_id,),
    ).fetchone()
    conn.close()
    return CreatorAgreement(*row) if row else None


def promote_user_to_creator(
    user_id: int,
    tos_version: str,
    disclaimer_version: str,
    disclaimer_acknowledged_at: str,
    signup_ip_hash: Optional[str] = None,
    operator_review_needed: bool = False,
    moderation_reason: Optional[str] = None,
    moderation_normalized_text: Optional[str] = None,
) -> CreatorAgreement:
    """Insert a creator_agreements row + flip users.role to 'creator' in
    a single transaction.

    Idempotent against an existing ACTIVE agreement for the same user +
    tos_version pair: returns the existing agreement unchanged. To start
    a new active agreement after revocation, the caller must call
    promote_user_to_creator again with the new (or same) tos_version.

    When the upstream moderation pass flagged the signup but accepted it
    (i.e. `moderation.accept and moderation.reason == 'flagged'`), the
    caller passes operator_review_needed=True + the verdict's reason +
    normalized_text so the row carries the evidence the operator review
    queue surfaces. The legacy CreatorAgreement dataclass is unchanged
    — these fields ride on the row in the DB and are read by the
    review-queue endpoint via direct SQL.
    """

    existing = get_active_agreement(user_id)
    if existing is not None and existing.tos_version == tos_version:
        return existing

    if get_user(user_id) is None:
        raise CreatorPromotionError(
            f"user_id={user_id} does not exist"
        )

    conn = database.get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN")
        cursor.execute(
            """
            INSERT INTO creator_agreements (
                user_id, tos_version, disclaimer_version,
                disclaimer_acknowledged_at, signup_ip_hash,
                operator_review_needed, moderation_reason,
                moderation_normalized_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, tos_version, disclaimer_version,
                disclaimer_acknowledged_at, signup_ip_hash,
                1 if operator_review_needed else 0,
                moderation_reason,
                moderation_normalized_text,
            ),
        )
        agreement_id = cursor.lastrowid
        cursor.execute(
            "UPDATE users SET role = 'creator' WHERE id = ?",
            (user_id,),
        )
        cursor.execute("COMMIT")
    except Exception:
        cursor.execute("ROLLBACK")
        conn.close()
        raise

    cursor.execute(
        """
        SELECT id, user_id, tos_version, disclaimer_version,
               disclaimer_acknowledged_at, signed_at, revoked_at,
               revoked_reason, signup_ip_hash
        FROM creator_agreements WHERE id = ?
        """,
        (agreement_id,),
    )
    row = cursor.fetchone()
    conn.close()
    return CreatorAgreement(*row)


def revoke_creator_role(user_id: int, reason: str) -> bool:
    """Set revoked_at on the user's currently-active agreement + flip
    users.role back to 'light'. Returns True if a revocation happened,
    False if the user had no active agreement.

    Does NOT delete any row — audit trail preserved.
    """

    active = get_active_agreement(user_id)
    if active is None:
        return False

    conn = database.get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN")
        cursor.execute(
            """
            UPDATE creator_agreements
            SET revoked_at = CURRENT_TIMESTAMP, revoked_reason = ?
            WHERE id = ?
            """,
            (reason, active.id),
        )
        cursor.execute(
            "UPDATE users SET role = 'light' WHERE id = ?",
            (user_id,),
        )
        cursor.execute("COMMIT")
    except Exception:
        cursor.execute("ROLLBACK")
        conn.close()
        raise
    conn.close()
    return True


def log_creator_download(
    user_id: int,
    asset_id: str,
    asset_type: Literal["clip", "summary", "infographic", "audio", "video", "other"],
    download_source_ip_hash: Optional[str] = None,
) -> CreatorDownload:
    """Insert a creator_downloads row. Stamps tos_version_at_download
    from the user's currently-active agreement.

    Raises CreatorPromotionError if the user has no active agreement —
    the upstream Flask endpoint should have rejected the call already
    on role=='creator' + active agreement; this is the defensive layer.
    """

    active = get_active_agreement(user_id)
    if active is None:
        raise CreatorPromotionError(
            f"user_id={user_id} has no active creator agreement; cannot log download"
        )

    conn = database.get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO creator_downloads (
            user_id, asset_id, asset_type, tos_version_at_download,
            download_source_ip_hash
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, asset_id, asset_type, active.tos_version,
         download_source_ip_hash),
    )
    download_id = cursor.lastrowid
    conn.commit()
    cursor.execute(
        """
        SELECT id, user_id, asset_id, asset_type, tos_version_at_download,
               download_source_ip_hash, downloaded_at
        FROM creator_downloads WHERE id = ?
        """,
        (download_id,),
    )
    row = cursor.fetchone()
    conn.close()
    return CreatorDownload(*row)


def get_creator_download_summary(user_id: int) -> CreatorDownloadSummary:
    """V0 aggregate-only view per redline decision 2."""
    conn = database.get_connection()
    row = conn.execute(
        """
        SELECT COUNT(*), MAX(downloaded_at)
        FROM creator_downloads WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()
    conn.close()
    total = int(row[0]) if row and row[0] is not None else 0
    most_recent = row[1] if row else None
    return CreatorDownloadSummary(
        user_id=user_id,
        total_downloads=total,
        most_recent_at=most_recent,
    )
