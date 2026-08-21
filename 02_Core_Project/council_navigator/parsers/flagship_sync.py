"""Flagship content pump — local-to-cloud sync for published broadcasts.

Per DECISIONS.md § D-049: the flagship serves curated review-gated outputs
(the pipeline worker stays local). Per § D-051: a single OWNER_EMAIL identity
gates the operator surface; only the owner pushes content from local to
cloud. This module is the engine for that push.

Two-sided shape (same code runs on both):
  ──────────────────────────────────────────────────────────────────────
  Local Flask (the sender):
    • gather_meeting_payload(meeting_id)        → JSON-serializable dict
    • list_media_files_to_sync(meeting_id)      → [(filename, Path), ...]
    • push_meeting_to_flagship(meeting_id, ...) → POSTs to cloud,
                                                   records to flagship_sync_log
  ──────────────────────────────────────────────────────────────────────
  Cloud Flask (the receiver):
    • apply_meeting_payload(payload)            → UPSERTs meeting + outputs
    • save_media_file(meeting_id, filename, data) → writes /data/media/<id>/<file>
  ──────────────────────────────────────────────────────────────────────

Auth shape (per D-051):
  • The HTTP transit between local + cloud goes through Cloudflare Pages.
  • Cf-Access at the edge gates /api/sync/* via a service-token policy;
    the local client sends CF-Access-Client-Id + CF-Access-Client-Secret
    headers (issued in Cloudflare Zero Trust).
  • The cloud Flask additionally validates a shared X-Sync-Token header —
    defense in depth in case the Railway hostname is reachable
    bypassing Cf-Access.

V1 scope (per James's call 2026-05-23): meeting + notebook_outputs +
top-level media files (audio_overview / video_explainer / infographic).
Cast pages + tracked_claims + proof clips queued for V1.5.

Module layout:
  • Constants + config helpers at top
  • Sender side (gather + push) in the middle
  • Receiver side (apply + save) at the bottom
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from uuid import UUID

import database

try:
    from parsers import notification_pipeline, operator_identity, resend_adapter
except ImportError:  # Direct imports from parsers/ at runtime.
    import notification_pipeline
    import operator_identity
    import resend_adapter

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────

# Additive optional payload sections preserve compatibility at version 1:
# an older receiver ignores them, and a newer receiver treats absence as
# "preserve receiver state" rather than "clear receiver state."
PAYLOAD_SCHEMA_VERSION = 1

SIM_QUERY_WIRE_FIELDS: Tuple[str, ...] = (
    "query_slot",
    "question_text",
    "answer_text",
    "prompt_name",
    "prompt_version",
    "prompt_hash",
    "vocab_version",
    "query_hash",
    "answer_digest",
    "model_id",
    "retrieved_chunk_ids",
    "run_id",
    "generated_at",
)
_SIM_QUERY_SLOTS = (0, 1, 2)
_SIM_QUERY_SHARED_PROVENANCE_FIELDS = (
    "prompt_name",
    "prompt_version",
    "prompt_hash",
    "vocab_version",
    "model_id",
    "run_id",
    "generated_at",
)
_SIM_QUERY_TEXT_FIELDS = tuple(
    field for field in SIM_QUERY_WIRE_FIELDS if field != "query_slot"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Filenames the sender will mirror to the flagship. Anything else in
# media/<id>/ (whisper/ subdir, source caches, dry-run artifacts, etc.)
# is sender-local operator state — not part of the published broadcast.
SYNCABLE_MEDIA_FILENAMES: Tuple[str, ...] = (
    "audio_overview.mp4",
    "audio_overview.mp3",
    "video_explainer.mp4",
    "infographic.png",
    "infographic.jpg",
    "infographic.jpeg",
)

# Notebook output types pushed in V1 + V1.5. Studio outputs + text outputs
# that the BroadcastPage renders, plus extraction-blob audit trails for the
# Cast page + accountability ledger. V1.5 additions (2026-05-26) per the
# Quotes Unification Refactor + queued Cast page sync:
#   - quotes:               new unified extraction (raw JSON blob; the
#                           live data lives in the structured `quotes` table
#                           synced separately via gather_meeting_payload)
#   - member_attendance:    Cast page attendance (raw blob; live data in
#                           `member_attendance` table synced separately)
#   - member_quotes_topic:  legacy Cast page extraction (raw blob; live
#                           data in `member_quotes` table synced separately)
#   - tracked_claims:       Accountability ledger (raw blob; live data in
#                           `tracked_claims` table synced separately)
#   - transcript_words:     Whisper transcript (large — ~830KB for a 77-min
#                           meeting — but enables cloud-side re-alignment
#                           and karaoke fallback if the structured
#                           word_timings ever need rebuilding)
SYNCABLE_OUTPUT_TYPES: Tuple[str, ...] = (
    "synopsis",
    "newsletter",
    "key_decisions",
    "community_calls_to_action",
    "whats_next",
    "council_sentiment",
    "council_quotes",
    "suggested_questions",
    "episode_tagline",
    "episode_tags",
    "audio_overview",
    "video_explainer",
    "infographic",
    # V1.5 additions:
    "quotes",
    "member_attendance",
    "member_quotes_topic",
    "tracked_claims",
    "transcript_words",
)


def _media_root() -> Path:
    """The base media directory. Local: <repo>/media. Cloud Railway:
    /data/media (start.sh sets ZSPAN_MEDIA_ROOT)."""
    env = os.environ.get("ZSPAN_MEDIA_ROOT")
    if env:
        return Path(env)
    # parsers/ → ../media
    return Path(__file__).resolve().parent.parent / "media"


def _meeting_media_dir(meeting_id: int) -> Path:
    return _media_root() / str(meeting_id)


# ── Preview sidecars — the decision-Discussion karaoke, decision-bound
#    quotes, routing, and recusals JSON that BroadcastPage renders under each
#    generation. These live as gitignored .preview/m<id>*.json files, so the
#    sync push is their ONLY path to the hosted flagship — they never deploy
#    with the repo (the 2026-07-10 fix: the hosted BroadcastPage was showing
#    bare generations because .preview/ wasn't on Railway). Mirrors the media
#    pattern: local root is the repo's .preview/; on Railway ZSPAN_PREVIEW_ROOT
#    points at the volume, where the receiver writes and Express reads.
_SIDECAR_TYPES: Tuple[str, ...] = ("quotes", "decisions", "routing", "recusals")


def _preview_root() -> Path:
    """Local: <repo>/.preview. Cloud Railway: ZSPAN_PREVIEW_ROOT (the
    volume). Kept in lockstep with server/index.ts's previewRoot."""
    env = os.environ.get("ZSPAN_PREVIEW_ROOT")
    if env:
        return Path(env)
    # parsers/ → council_navigator → 02_Core_Project → repo root → .preview
    return Path(__file__).resolve().parents[3] / ".preview"


def _sidecar_path(root: Path, meeting_id: int, sidecar_type: str) -> Path:
    suffix = "" if sidecar_type == "quotes" else f"_{sidecar_type}"
    return root / f"m{int(meeting_id)}{suffix}.json"


def _gather_preview_sidecars(meeting_id: int) -> Dict[str, Any]:
    """Sender: read the meeting's local sidecar JSON for the wire payload.
    Only the allowlisted types; a missing or unreadable sidecar is simply
    omitted (many meetings legitimately have none)."""
    root = _preview_root()
    out: Dict[str, Any] = {}
    for t in _SIDECAR_TYPES:
        p = _sidecar_path(root, meeting_id, t)
        if not p.is_file():
            continue
        try:
            out[t] = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("skipping unreadable sidecar %s: %s", p, e)
    return out


def _write_preview_sidecars(meeting_id: int, sidecars: Dict[str, Any]) -> int:
    """Receiver: write pushed sidecars to _preview_root() so the hosted
    BroadcastPage's /api/preview/* endpoints serve them. The type allowlist +
    int meeting_id guard against path traversal; atomic write per file so the
    Express server never reads a half-written sidecar."""
    if not sidecars:
        return 0
    root = _preview_root()
    root.mkdir(parents=True, exist_ok=True)
    written = 0
    for t, data in sidecars.items():
        if t not in _SIDECAR_TYPES:
            continue  # allowlist — never write an arbitrary filename
        target = _sidecar_path(root, int(meeting_id), t)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
        written += 1
    return written


def _normalize_sim_query_payload(items: Any) -> List[Dict[str, Any]]:
    """Validate a complete sim-query generation and project its wire fields."""
    if not isinstance(items, list):
        raise ValueError("sim_queries must be a list")
    if not items:
        return []
    if len(items) != len(_SIM_QUERY_SLOTS):
        raise ValueError(f"expected 3 sim-query rows, found {len(items)}")

    normalized: List[Dict[str, Any]] = []
    slots: List[int] = []
    for raw in items:
        if not isinstance(raw, dict):
            raise ValueError("each sim-query row must be a dict")
        item = {field: raw.get(field) for field in SIM_QUERY_WIRE_FIELDS}
        slot = item["query_slot"]
        if isinstance(slot, bool) or not isinstance(slot, int):
            raise ValueError(f"invalid sim-query slot {slot!r}")
        slots.append(slot)
        for field in _SIM_QUERY_TEXT_FIELDS:
            value = item[field]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"sim-query slot {slot} has invalid {field}")

        if item["prompt_name"] != "sim_query_answer":
            raise ValueError(f"sim-query slot {slot} has unknown prompt_name")
        for field in ("prompt_hash", "query_hash", "answer_digest"):
            if _SHA256_RE.fullmatch(item[field]) is None:
                raise ValueError(f"sim-query slot {slot} has invalid {field}")
        expected_query_hash = hashlib.sha256(
            item["question_text"].encode("utf-8")
        ).hexdigest()
        expected_answer_digest = hashlib.sha256(
            item["answer_text"].encode("utf-8")
        ).hexdigest()
        if item["query_hash"] != expected_query_hash:
            raise ValueError(f"sim-query slot {slot} query_hash mismatch")
        if item["answer_digest"] != expected_answer_digest:
            raise ValueError(f"sim-query slot {slot} answer_digest mismatch")

        try:
            UUID(item["run_id"])
        except (ValueError, AttributeError):
            raise ValueError(f"sim-query slot {slot} has invalid run_id") from None
        generated_at = item["generated_at"]
        if not generated_at.endswith("Z"):
            raise ValueError(f"sim-query slot {slot} generated_at is not UTC-Z")
        timestamp_body = generated_at[:-1]
        if "T" not in timestamp_body:
            raise ValueError(
                f"sim-query slot {slot} has invalid generated_at"
            )
        try:
            parsed_generated_at = datetime.fromisoformat(
                timestamp_body + "+00:00"
            )
        except ValueError:
            raise ValueError(
                f"sim-query slot {slot} has invalid generated_at"
            ) from None
        if (
            parsed_generated_at.tzinfo is None
            or parsed_generated_at.utcoffset()
            != timezone.utc.utcoffset(None)
        ):
            raise ValueError(
                f"sim-query slot {slot} has invalid generated_at"
            )

        try:
            chunk_ids = json.loads(item["retrieved_chunk_ids"])
        except json.JSONDecodeError:
            raise ValueError(
                f"sim-query slot {slot} has invalid retrieved_chunk_ids"
            ) from None
        if (
            not isinstance(chunk_ids, list)
            or not chunk_ids
            or any(
                isinstance(chunk_id, bool)
                or not isinstance(chunk_id, int)
                or chunk_id < 0
                for chunk_id in chunk_ids
            )
        ):
            raise ValueError(
                f"sim-query slot {slot} has invalid retrieved_chunk_ids"
            )
        normalized.append(item)

    if tuple(sorted(slots)) != _SIM_QUERY_SLOTS:
        raise ValueError(f"invalid sim-query slot set {sorted(slots)!r}")
    for field in _SIM_QUERY_SHARED_PROVENANCE_FIELDS:
        if len({item[field] for item in normalized}) != 1:
            raise ValueError(f"sim-query generation has mixed {field}")
    return sorted(normalized, key=lambda item: item["query_slot"])


def _gather_sim_queries(meeting_id: int) -> Optional[List[Dict[str, Any]]]:
    """Return a complete generation, or omit and log an incomplete artifact."""
    conn = database.get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT {', '.join(SIM_QUERY_WIRE_FIELDS)}
            FROM episode_sim_queries
            WHERE meeting_id = ?
            ORDER BY query_slot
            """,
            (meeting_id,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    try:
        return _normalize_sim_query_payload([dict(row) for row in rows])
    except ValueError as exc:
        logger.error(
            "omitting corrupt/incomplete sim-query generation for meeting_id=%s: %s",
            meeting_id,
            exc,
        )
        return None


def _replace_sim_queries_for_meeting(
    meeting_id: int,
    items: List[Dict[str, Any]],
) -> int:
    """Atomically replace or explicitly clear a receiver's three slots."""
    conn = database.get_connection()
    try:
        conn.execute("BEGIN")
        conn.execute(
            "DELETE FROM episode_sim_queries WHERE meeting_id = ?",
            (meeting_id,),
        )
        if items:
            conn.executemany(
                f"""
                INSERT INTO episode_sim_queries (
                    meeting_id, {', '.join(SIM_QUERY_WIRE_FIELDS)}
                ) VALUES ({', '.join('?' for _ in range(len(SIM_QUERY_WIRE_FIELDS) + 1))})
                """,
                [
                    (meeting_id, *(item[field] for field in SIM_QUERY_WIRE_FIELDS))
                    for item in items
                ],
            )
        conn.commit()
        return len(items)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────
# Sender side — gather + push
# ─────────────────────────────────────────────────────────────────

def gather_meeting_payload(meeting_id: int) -> Dict[str, Any]:
    """Build the JSON-serializable dict that ships to the flagship.

    Includes everything the cloud needs to render BroadcastPage for this
    meeting: the `meetings` row, every `notebook_outputs` row whose
    `output_type` is in `SYNCABLE_OUTPUT_TYPES`, and meta about the push
    (schema version, source).
    """
    meeting = database.get_meeting_with_notebook(meeting_id)
    if meeting is None:
        raise ValueError(f"No meeting found with id={meeting_id}")
    approved_at = meeting.get("wo_approved_at")
    if meeting.get("is_published") and not approved_at:
        raise ValueError(
            f"Refusing flagship payload for meeting id={meeting_id}: "
            "is_published is true but work-order approved_at is absent"
        )
    notes_violation = database.publication_text_violation(
        meeting.get("publish_notes")
    )
    if notes_violation:
        raise ValueError(
            f"Refusing flagship payload for meeting id={meeting_id}: "
            f"publish_notes {notes_violation}"
        )

    # The meeting dict from get_meeting_with_notebook embeds outputs as a
    # nested {output_type: {content, ...}} mapping. Re-flatten into a
    # list-of-dicts for the wire format; receiver UPSERTs one at a time.
    raw_outputs = meeting.get("notebook_outputs") or {}
    outputs: List[Dict[str, Any]] = []
    for output_type, payload in raw_outputs.items():
        if output_type not in SYNCABLE_OUTPUT_TYPES:
            continue
        if not isinstance(payload, dict):
            continue
        outputs.append({
            "output_type": output_type,
            "content": payload.get("content"),
            "content_url": payload.get("content_url"),
            "notebook_id": payload.get("notebook_id") or meeting.get("notebook_id") or "",
            "prompt_filename": payload.get("prompt_filename"),
            "prompt_version": payload.get("prompt_version"),
            "generated_at": payload.get("generated_at"),
            "error": payload.get("error"),
            "voided_at": payload.get("voided_at"),
            "voided_by": payload.get("voided_by"),
        })

    # ── V1.5: Cast page + accountability ledger + unified quotes ──
    # Sync the structured tables so the cloud's BroadcastPage / Cast page /
    # Accountability ledger render from canonical data rather than parsing
    # the raw JSON blobs in notebook_outputs. Additive over the V1 payload
    # shape; receivers tolerant of missing keys (no schema_version bump).
    city_name = meeting.get("city_name")
    council_members = _gather_council_members_for_city(city_name) if city_name else []
    quotes = _gather_quotes_for_meeting(meeting_id)
    member_attendance = _gather_member_attendance_for_meeting(meeting_id)
    member_quotes_legacy = _gather_member_quotes_legacy_for_meeting(meeting_id)
    tracked_claims = _gather_tracked_claims_for_meeting(meeting_id)
    sim_queries = _gather_sim_queries(meeting_id)

    payload = {
        "meta": {
            "schema_version": PAYLOAD_SCHEMA_VERSION,
            "source": "z-span-local-flask",
        },
        "meeting": {
            "id": meeting["id"],
            "city_name": meeting.get("city_name"),
            "county": meeting.get("county"),
            "state": meeting.get("state") or database.resolve_city_state(None, meeting.get("county")),
            "meeting_title": meeting.get("meeting_title"),
            "meeting_date": meeting.get("meeting_date"),
            "meeting_time": meeting.get("meeting_time"),
            "meeting_location": meeting.get("meeting_location"),
            "meeting_status": meeting.get("meeting_status"),
            "agenda_url": meeting.get("agenda_url"),
            "minutes_url": meeting.get("minutes_url"),
            "video_url": meeting.get("video_url"),
            "agenda_packet_url": meeting.get("agenda_packet_url"),
            "ecomment_url": meeting.get("ecomment_url"),
            "meeting_id": meeting.get("meeting_id"),
            "summary": meeting.get("summary"),
            "notebook_id": meeting.get("notebook_id"),
            "is_published": meeting.get("is_published"),
            "published_at": meeting.get("published_at"),
            "published_by": operator_identity.ROLE_IDENTITY,
            "publish_notes": meeting.get("publish_notes"),
        },
        "approval": {
            "approved_at": approved_at,
        },
        "outputs": outputs,
        # Preview sidecars (decision-Discussion karaoke, decision-bound
        # quotes / routing / recusals) — gitignored locally, so this push is
        # their only route to the hosted flagship. Optional in payload schema.
        "preview_sidecars": _gather_preview_sidecars(meeting_id),
        # V1.5 structured-row sections (optional in payload schema):
        "council_members": council_members,
        "quotes": quotes,
        "member_attendance": member_attendance,
        "member_quotes_legacy": member_quotes_legacy,
        "tracked_claims": tracked_claims,
    }
    # Absence is deliberately different from an explicit []: older senders
    # preserve a receiver's rows, while [] is the operator's clear signal.
    if sim_queries is not None:
        payload["sim_queries"] = sim_queries
    return payload


# ─────────────────────────────────────────────────────────────────
# V1.5 structured-row gatherers (sender side)
# ─────────────────────────────────────────────────────────────────
#
# These read all rows for the relevant scope (per-meeting or per-city) from
# the local SQLite, return as list-of-dicts for the wire format. Receivers
# UPSERT them via paired helpers in database.py. Missing/empty results are
# OK — receivers tolerate empty lists.

def _gather_council_members_for_city(city_name: str) -> List[Dict[str, Any]]:
    """Return all council_members for the meeting's city. The cloud needs
    these so member_id FK references in quotes / member_attendance /
    member_quotes / tracked_claims resolve to a real person on the Cast page.
    """
    conn = database.get_connection()
    rows = conn.execute(
        """
        SELECT id, city_name, name, title, email, phone, photo_url, ward,
               term_start, term_end, seat_id, role, source_url,
               term_started, term_ends
        FROM council_members
        WHERE city_name = ?
        ORDER BY id
        """,
        (city_name,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _gather_quotes_for_meeting(meeting_id: int) -> List[Dict[str, Any]]:
    """Return all rows from the unified `quotes` table for this meeting.

    Includes verification chain state (verified_status / verified_by /
    verified_at / gemini_correction_notes / quote_text_original) so the
    cloud doesn't lose audit context. The cloud's Cast / BroadcastPage
    API filters by verified_status at read time for public surfaces.

    `content_hash` ships unchanged so cloud-side UPSERT uses the same
    natural key for preservation across re-pushes.
    """
    conn = database.get_connection()
    rows = conn.execute(
        """
        SELECT id, meeting_id, member_id, speaker_name, speaker_role, speaker_class,
               quote_text, quote_text_original, topic_tags, minutes_page_ref, context,
               is_broadcast_hero, video_timestamp_seconds, word_timings,
               verified_status, verified_by, verified_at, gemini_correction_notes,
               proof_clip_url, proof_clip_sha256, content_hash,
               extracted_at, updated_at
        FROM quotes
        WHERE meeting_id = ?
        ORDER BY id
        """,
        (meeting_id,),
    ).fetchall()
    conn.close()
    gathered = []
    for row in rows:
        item = dict(row)
        item["verified_by"] = operator_identity.coerce_optional_role_identity(
            item.get("verified_by")
        )
        gathered.append(item)
    return gathered


def _gather_member_attendance_for_meeting(meeting_id: int) -> List[Dict[str, Any]]:
    """Return all member_attendance rows for this meeting. Powers the
    Cast page attendance heat-map."""
    conn = database.get_connection()
    rows = conn.execute(
        """
        SELECT id, member_id, meeting_id, status, notes, recorded_at
        FROM member_attendance
        WHERE meeting_id = ?
        ORDER BY id
        """,
        (meeting_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _gather_member_quotes_legacy_for_meeting(meeting_id: int) -> List[Dict[str, Any]]:
    """Return all legacy `member_quotes` rows for this meeting. During the
    Quotes Unification Refactor transition (until Chunk 9 retires the old
    table), Cast page may still read from member_quotes if the Chunk 7
    rewrite hasn't landed yet. Synced for backward compat; tagged 'legacy'
    in the payload to signal the receiver."""
    conn = database.get_connection()
    rows = conn.execute(
        """
        SELECT id, member_id, meeting_id, quote_text, topic_tags,
               minutes_page_ref, video_timestamp_seconds, proof_clip_url,
               verified_status, extracted_at, word_timings,
               quote_text_original, gemini_correction_notes,
               verified_by, verified_at
        FROM member_quotes
        WHERE meeting_id = ?
        ORDER BY id
        """,
        (meeting_id,),
    ).fetchall()
    conn.close()
    gathered = []
    for row in rows:
        item = dict(row)
        item["verified_by"] = operator_identity.coerce_optional_role_identity(
            item.get("verified_by")
        )
        gathered.append(item)
    return gathered


def _gather_tracked_claims_for_meeting(meeting_id: int) -> List[Dict[str, Any]]:
    """Return all tracked_claims rows for this meeting. Powers the
    Accountability Ledger on Cast page + City Ledger surfaces."""
    conn = database.get_connection()
    rows = conn.execute(
        """
        SELECT id, member_id, meeting_id, claim_type, claim_text,
               expected_outcome, time_horizon_months, topic_tags,
               confidence, context, word_timings,
               status, status_updated_at, status_updated_by, status_evidence,
               extracted_at
        FROM tracked_claims
        WHERE meeting_id = ?
        ORDER BY id
        """,
        (meeting_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_media_files_to_sync(meeting_id: int) -> List[Tuple[str, Path]]:
    """Return (filename, absolute_path) tuples for media files to mirror.
    Only top-level files in media/<id>/ matching `SYNCABLE_MEDIA_FILENAMES`
    are returned. Whisper transcript caches, source.mp4 yt-dlp caches,
    proof clips, review_queue artifacts — all excluded.
    """
    media_dir = _meeting_media_dir(meeting_id)
    if not media_dir.is_dir():
        return []
    files: List[Tuple[str, Path]] = []
    for name in SYNCABLE_MEDIA_FILENAMES:
        candidate = media_dir / name
        if candidate.is_file():
            files.append((name, candidate))
    return files


class FlagshipSyncError(Exception):
    """Raised by push_meeting_to_flagship on any failure step. The
    message names the failed step so the operator-terminal toast can
    show actionable context (`gather`, `payload-post`, `media-post`)."""


def push_meeting_to_flagship(
    meeting_id: int,
    pushed_by: str,
    flagship_url: Optional[str] = None,
    sync_token: Optional[str] = None,
    cf_access_client_id: Optional[str] = None,
    cf_access_client_secret: Optional[str] = None,
    request_timeout_seconds: float = 120.0,
) -> Dict[str, Any]:
    """Push one meeting's payload + media files to the flagship.

    Records every attempt in `flagship_sync_log`. Returns the result
    dict the operator-terminal renders.

    Auth resolution: any explicit arg → env var → user_settings.json.
    The same priority pattern Other parsers use for API keys (see
    `env_config.py`). Missing token → FlagshipSyncError("config: ...").
    """
    import ssl
    import urllib.request
    import urllib.error

    # SSL context: force certifi's CA bundle. On macOS + Homebrew Python +
    # OpenSSL 3 the default_verify_paths point at empty Homebrew paths, so
    # urllib.request.urlopen() with no explicit context fails cert-verify
    # against valid public certificates (session-95 sync smoke, 2026-07-26).
    # Passing an explicit certifi-backed context makes the sync robust to
    # whatever the system's OpenSSL install did or didn't do.
    try:
        import certifi
        _ssl_context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        # certifi should be in requirements — fall back to default context
        # (which is what urlopen used before this fix; noisy failure is
        # better than silent success under a stripped verifier).
        _ssl_context = ssl.create_default_context()

    try:
        import env_config  # noqa: PLC0415 — lazy
        settings = env_config.load_user_settings()
    except (ImportError, Exception):  # noqa: BLE001 — settings file is optional
        settings = {}

    def _resolve(env_name: str, override: Optional[str]) -> Optional[str]:
        """Priority: explicit override → env var → user_settings.json field
        (snake_case). Mirrors the pattern in env_config.get_youtube_data_api_key.
        """
        if override:
            return override
        val = os.environ.get(env_name)
        if val:
            return val
        return settings.get(env_name.lower()) or None

    flagship_url = _resolve("FLAGSHIP_SYNC_URL", flagship_url)
    sync_token = _resolve("ZSPAN_SYNC_TOKEN", sync_token)
    cf_id = _resolve("CF_ACCESS_CLIENT_ID", cf_access_client_id)
    cf_secret = _resolve("CF_ACCESS_CLIENT_SECRET", cf_access_client_secret)

    if not flagship_url:
        raise FlagshipSyncError(
            "config: FLAGSHIP_SYNC_URL not set (e.g., https://operator.zspan.org/api/sync"
            " — must be the OPERATOR hostname; the public-host /api/* gate rejects zspan.org)"
        )
    if not sync_token:
        raise FlagshipSyncError("config: ZSPAN_SYNC_TOKEN not set")
    if not cf_id or not cf_secret:
        raise FlagshipSyncError(
            "config: CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET not set "
            "(create a Cloudflare Access service token in Zero Trust)"
        )

    flagship_url = flagship_url.rstrip("/")

    # Record an in_progress row so the operator terminal can show
    # "syncing now" before the request completes.
    attempt_id = database.record_flagship_sync_attempt(
        meeting_id=meeting_id,
        status="in_progress",
        pushed_by=pushed_by,
    )
    started_at = time.time()

    def _finalize(
        status: str,
        error: Optional[str] = None,
        payload_bytes: Optional[int] = None,
        media_bytes: Optional[int] = None,
        response_body: Optional[str] = None,
    ) -> Dict[str, Any]:
        database.update_flagship_sync_attempt(
            attempt_id=attempt_id,
            status=status,
            error=error,
            payload_bytes=payload_bytes,
            media_bytes=media_bytes,
            flagship_response=response_body,
        )
        elapsed = round(time.time() - started_at, 2)
        return {
            "attempt_id": attempt_id,
            "status": status,
            "error": error,
            "payload_bytes": payload_bytes,
            "media_bytes": media_bytes,
            "flagship_response": response_body,
            "elapsed_seconds": elapsed,
        }

    # ── Step 1: Gather the payload ──────────────────────────────
    try:
        payload = gather_meeting_payload(meeting_id)
    except Exception as e:
        msg = f"gather: {e}"
        logger.exception("gather_meeting_payload failed for %s", meeting_id)
        return _finalize("failed", error=msg)

    payload_json = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    payload_bytes = len(payload_json)

    base_headers = {
        "X-Sync-Token": sync_token,
        "CF-Access-Client-Id": cf_id,
        "CF-Access-Client-Secret": cf_secret,
        # Cloudflare's edge bot fingerprinting rejects Python-urllib's
        # default UA with error code 1010 BEFORE Access validates the
        # service token. Set a stable, identifying UA so the sync client
        # is recognizable in logs but doesn't trip the bot heuristics.
        "User-Agent": "z-span-flagship-sync/1.0",  # internal Mac->zspan.org sync only; personal GitHub handle dropped (D-158 UA-hygiene)
    }

    # ── Step 2: POST the JSON metadata first ─────────────────────
    meta_url = f"{flagship_url}/meeting/{meeting_id}"
    req = urllib.request.Request(
        meta_url,
        data=payload_json,
        method="POST",
        headers={**base_headers, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=request_timeout_seconds, context=_ssl_context) as resp:
            response_body = resp.read().decode("utf-8", errors="replace")
            response_status = resp.status
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        return _finalize(
            "failed",
            error=f"payload-post: HTTP {e.code} — {body[:500]}",
            payload_bytes=payload_bytes,
        )
    except urllib.error.URLError as e:
        return _finalize(
            "failed",
            error=f"payload-post: network error — {e.reason}",
            payload_bytes=payload_bytes,
        )

    if response_status >= 400:
        return _finalize(
            "failed",
            error=f"payload-post: HTTP {response_status}",
            payload_bytes=payload_bytes,
            response_body=response_body,
        )

    # A 200 is NOT proof Flask answered. Cloudflare Access serves its
    # interstitial sign-in page as HTTP 200 when service-token auth fails,
    # so an unauthenticated push looked like a clean success — the
    # `outputs_pushed` count in the summary below is what we PACKED, not
    # what the flagship stored (session-95, 2026-07-26: a "success" whose
    # payload_response was Access HTML). The receiver always answers JSON;
    # anything else means the request never reached it. This is the
    # CLAUDE.md § F8 distinction — succeeded-empty vs failed-silent —
    # applied at the one seam that can silently lose a whole meeting.
    try:
        flagship_ack = json.loads(response_body) if response_body else None
    except (ValueError, TypeError):
        flagship_ack = None
    if not isinstance(flagship_ack, dict):
        snippet = (response_body or '')[:200].replace('\n', ' ')
        hint = ''
        low = (response_body or '').lower()
        if 'cloudflare access' in low or 'cf-access' in low:
            hint = (
                ' — this is the Cloudflare Access sign-in page, so the'
                ' service-token headers did not authenticate:'
                ' check CF_ACCESS_CLIENT_ID/SECRET against the Access'
                ' service-token policy for this hostname'
            )
        return _finalize(
            "failed",
            error=(
                f"payload-post: HTTP {response_status} but the body is not"
                f" JSON — the flagship receiver never answered{hint}."
                f" First 200 chars: {snippet}"
            ),
            payload_bytes=payload_bytes,
            response_body=response_body,
        )

    # ── Step 3: POST each media file as raw binary ──────────────
    media_files = list_media_files_to_sync(meeting_id)
    total_media_bytes = 0
    media_results: List[Dict[str, Any]] = []
    for filename, path in media_files:
        try:
            data = path.read_bytes()
        except OSError as e:
            return _finalize(
                "failed",
                error=f"media-read: {filename} — {e}",
                payload_bytes=payload_bytes,
                media_bytes=total_media_bytes,
            )

        content_type = _guess_media_content_type(filename)
        media_url = f"{flagship_url}/meeting/{meeting_id}/media/{filename}"
        req = urllib.request.Request(
            media_url,
            data=data,
            method="POST",
            headers={
                **base_headers,
                "Content-Type": content_type,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=request_timeout_seconds, context=_ssl_context) as resp:
                resp_body = resp.read().decode("utf-8", errors="replace")
                media_results.append({
                    "filename": filename,
                    "bytes": len(data),
                    "status": resp.status,
                    "response": resp_body[:300],
                })
                total_media_bytes += len(data)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            return _finalize(
                "failed",
                error=f"media-post: {filename} — HTTP {e.code} — {body[:300]}",
                payload_bytes=payload_bytes,
                media_bytes=total_media_bytes,
            )
        except urllib.error.URLError as e:
            return _finalize(
                "failed",
                error=f"media-post: {filename} — network error — {e.reason}",
                payload_bytes=payload_bytes,
                media_bytes=total_media_bytes,
            )

    # ── Done ─────────────────────────────────────────────────────
    summary = {
        # LOCAL counts — what this process packed and sent, NOT a receipt
        # from the flagship. The flagship's own acknowledgment is
        # `flagship_ack` below; read that when you need to know what the
        # far side actually stored.
        "outputs_sent": len(payload.get("outputs", [])),
        "media_sent": [m["filename"] for m in media_results],
        "flagship_ack": flagship_ack,
    }
    return _finalize(
        "success",
        payload_bytes=payload_bytes,
        media_bytes=total_media_bytes,
        response_body=json.dumps(summary),
    )


def _guess_media_content_type(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(".mp4"):
        return "video/mp4"
    if lower.endswith(".mp3"):
        return "audio/mpeg"
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".jpg") or lower.endswith(".jpeg"):
        return "image/jpeg"
    return "application/octet-stream"


# ─────────────────────────────────────────────────────────────────
# Receiver side — apply payload + save media
# ─────────────────────────────────────────────────────────────────


def _copy_work_order_approval(meeting_id: int, approved_at: str) -> None:
    """Copy an existing approval stamp without invoking the publish gate.

    A receiver may already have a richer work-order row; the conflict path
    changes only approved_at. Replaying the same payload therefore preserves
    the row and the original human approval timestamp.
    """
    conn = database.get_connection()
    try:
        conn.execute(
            """
            INSERT INTO work_orders (meeting_id, approved_at)
            VALUES (?, ?)
            ON CONFLICT(meeting_id) DO UPDATE SET
                approved_at = excluded.approved_at
            """,
            (meeting_id, approved_at),
        )
        conn.commit()
    finally:
        conn.close()

def apply_meeting_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Cloud-side receiver: UPSERT the meeting + outputs + V1.5 structured
    tables from the payload the sender posted. Idempotent — re-posting the
    same payload overwrites cleanly.

    V1.5 (Quotes Unification Refactor, 2026-05-26): payload may now include
    `council_members`, `quotes`, `member_attendance`, `member_quotes_legacy`,
    `tracked_claims`, and `sim_queries` as optional top-level keys. Receivers
    tolerate missing keys (older senders still work). For sim queries only,
    an explicit empty list clears receiver state.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")
    meta = payload.get("meta") or {}
    if meta.get("schema_version") != PAYLOAD_SCHEMA_VERSION:
        raise ValueError(
            f"schema_version mismatch: got {meta.get('schema_version')!r}, "
            f"expected {PAYLOAD_SCHEMA_VERSION}"
        )
    meeting = payload.get("meeting") or {}
    outputs = payload.get("outputs") or []
    if not meeting.get("id"):
        raise ValueError("payload.meeting.id is required")
    approval = payload.get("approval")
    if approval is None:
        approval = {}
    if not isinstance(approval, dict):
        raise ValueError("payload.approval must be a dict")
    approved_at = approval.get("approved_at")
    if approved_at is not None and (
        not isinstance(approved_at, str) or not approved_at.strip()
    ):
        raise ValueError("payload.approval.approved_at must be a non-empty string or null")
    if meeting.get("is_published") and not approved_at:
        raise ValueError(
            "Refusing flagship payload: payload.meeting.is_published is true "
            "but payload.approval.approved_at is absent"
        )
    sim_queries: Optional[List[Dict[str, Any]]] = None
    if "sim_queries" in payload:
        # Validate the complete generation before any receiver mutation.
        sim_queries = _normalize_sim_query_payload(payload["sim_queries"])

    # Step 1: meeting + notebook_outputs (V1 surface)
    meeting_id = database.upsert_meeting_from_flagship_payload(meeting)
    if approved_at:
        _copy_work_order_approval(meeting_id, approved_at)
    output_count = database.upsert_notebook_outputs_from_flagship_payload(
        meeting_id=meeting_id,
        outputs=outputs,
    )

    # Optional sim-query generation. The receiver-resolved meeting_id is
    # load-bearing: natural-key conflict resolution can differ from sender ID.
    sim_queries_upserted: Optional[int] = None
    if sim_queries is not None:
        sim_queries_upserted = _replace_sim_queries_for_meeting(
            meeting_id,
            sim_queries,
        )

    # Step 2: V1.5 structured tables. Order matters — council_members FIRST
    # so the remap is ready for FK translation on quotes/attendance/etc.
    council_members = payload.get("council_members") or []
    member_id_remap = database.upsert_council_members_from_flagship_payload(
        council_members,
    )

    quotes = payload.get("quotes") or []
    quotes_count = database.upsert_quotes_from_flagship_payload(
        meeting_id=meeting_id,
        items=quotes,
        member_id_remap=member_id_remap,
    )

    member_attendance = payload.get("member_attendance") or []
    attendance_count = database.upsert_member_attendance_from_flagship_payload(
        meeting_id=meeting_id,
        items=member_attendance,
        member_id_remap=member_id_remap,
    )

    member_quotes_legacy = payload.get("member_quotes_legacy") or []
    member_quotes_count = database.upsert_member_quotes_legacy_from_flagship_payload(
        meeting_id=meeting_id,
        items=member_quotes_legacy,
        member_id_remap=member_id_remap,
    )

    tracked_claims = payload.get("tracked_claims") or []
    tracked_claims_count = database.upsert_tracked_claims_from_flagship_payload(
        meeting_id=meeting_id,
        items=tracked_claims,
        member_id_remap=member_id_remap,
    )

    # Step 3: preview sidecars → the volume, so the hosted BroadcastPage's
    # /api/preview/* endpoints serve the decision-Discussion karaoke (older
    # senders omit the key; write is a no-op then).
    sidecars_written = _write_preview_sidecars(
        meeting_id, payload.get("preview_sidecars") or {}
    )

    # Step 4: rebuild deterministic tags, then enqueue exactly one fan-out
    # event if the meeting now passes the two-field public visibility gate.
    # The sync's load-bearing meeting/output writes are already committed;
    # notification trouble must never turn that successful receive into a
    # sender-visible 500.
    notify_tags_count = 0
    notify_enqueued = False
    notify_recipient_count = 0
    notify_skipped_reason: str | None = None
    try:
        notify_tags_count = len(
            notification_pipeline.recompute_meeting_topic_tags(meeting_id)
        )
        enqueue_result = (
            notification_pipeline.enqueue_published_meeting_notifications(
                meeting_id
            )
        )
        notify_enqueued = bool(enqueue_result["enqueued"])
        notify_recipient_count = int(enqueue_result["recipient_count"])
        notify_skipped_reason = enqueue_result["skipped_reason"]
    except Exception:
        notify_skipped_reason = "notification_pipeline_error"
        logger.exception(
            "notification classify/enqueue failed for meeting %s; "
            "flagship sync remains successful",
            meeting_id,
        )

    # Step 5: opportunistically drain a bounded outbox batch on every sync
    # tick. No RESEND_API_KEY is a normal no-op; unexpected adapter failures
    # are isolated from the already-completed sync just like Step 4.
    notify_drain: Dict[str, Any] = {
        "attempted": 0,
        "sent": 0,
        "failed": 0,
        "skipped_no_api_key": False,
    }
    try:
        notify_drain = resend_adapter.drain_notification_outbox()
    except Exception:
        notify_drain["error"] = "drain_failed"
        logger.exception(
            "notification outbox drain failed after meeting %s sync; "
            "flagship sync remains successful",
            meeting_id,
        )

    return {
        "meeting_id": meeting_id,
        "approval_copied": bool(approved_at),
        "preview_sidecars_written": sidecars_written,
        "outputs_upserted": output_count,
        "sim_queries_upserted": sim_queries_upserted,
        "council_members_upserted": len(council_members),
        "council_member_id_remap_size": len(member_id_remap),
        "quotes_upserted": quotes_count,
        "member_attendance_upserted": attendance_count,
        "member_quotes_legacy_upserted": member_quotes_count,
        "tracked_claims_upserted": tracked_claims_count,
        "notify_tags_count": notify_tags_count,
        "notify_enqueued": notify_enqueued,
        "notify_recipient_count": notify_recipient_count,
        "notify_skipped_reason": notify_skipped_reason,
        "notify_drain": notify_drain,
    }


def save_media_file(
    meeting_id: int, filename: str, data: bytes,
) -> Dict[str, Any]:
    """Cloud-side receiver: write the media file to /data/media/<id>/<name>.
    Filename is validated against `SYNCABLE_MEDIA_FILENAMES` to prevent
    path traversal + arbitrary uploads."""
    if filename not in SYNCABLE_MEDIA_FILENAMES:
        raise ValueError(
            f"filename {filename!r} is not in the allowlist "
            f"{SYNCABLE_MEDIA_FILENAMES!r}"
        )
    # Defense in depth: even though `filename` is allowlist-matched, also
    # reject any path separators.
    if "/" in filename or "\\" in filename or ".." in filename:
        raise ValueError(f"filename {filename!r} contains illegal characters")

    target_dir = _meeting_media_dir(meeting_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / filename
    # Atomic write — write to .tmp then rename so partial writes don't
    # leave a half-baked file the Express static server could serve.
    tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    tmp_path.write_bytes(data)
    os.replace(tmp_path, target_path)
    return {
        "meeting_id": meeting_id,
        "filename": filename,
        "bytes": len(data),
        "path": str(target_path),
    }
