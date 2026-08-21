#!/usr/bin/env python3.11
"""
migrate_to_unified_quotes — backfill the unified `quotes` table from the
two existing siloed streams.

Chunk 2 of the Quotes Unification Refactor (2026-05-26).
See 01_Project_Overview/REFACTOR_QUOTES_UNIFICATION.md for the full plan.

Sources:
    1. `member_quotes` table — every row migrates to `quotes` with
       speaker_class='council_member', is_broadcast_hero=0, and all
       verification + alignment + correction state PRESERVED. The
       speaker_name is resolved by joining to council_members.name via
       member_id.

    2. `notebook_outputs` rows with output_type='council_quotes' — parse the
       JSON blob, derive speaker_class from speaker_role, and UPSERT into
       `quotes` with is_broadcast_hero=1.

       - Quotes that match an existing migrated member_quote (by content_hash)
         UPDATE is_broadcast_hero=1 (preserving verification state) — the same
         quote ends up flagged as broadcast-hero with its member_quote-side
         attribution + verification intact.
       - Quotes with no match (e.g., Police Captain quotes that only appear
         in the council_quotes blob, since member_quotes is council-member-only)
         INSERT as fresh rows with their speaker_class derived from speaker_role.

Idempotent: re-running is a no-op thanks to UNIQUE(meeting_id, content_hash)
+ explicit pre-check before INSERT/UPDATE.

Usage:
    cd 02_Core_Project
    python3.11 -m zspan_pipeline.scripts.migrate_to_unified_quotes
    python3.11 -m zspan_pipeline.scripts.migrate_to_unified_quotes --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

_PARSERS_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "council_navigator"
    / "parsers"
)
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

from database import (  # noqa: E402
    DB_PATH,
    _compute_content_hash,
    _lookup_member_id_via_cursor,
)
import sqlite3  # noqa: E402

logger = logging.getLogger(__name__)


# Map speaker_role strings to speaker_class. Anything not in this map gets
# 'staff' (the prudent default; council_quotes blobs only ever extracted
# from official-capacity speakers, so 'staff' is the right fallback when
# the role doesn't match a council-member title).
_ROLE_TO_CLASS = {
    'mayor': 'council_member',
    'vice mayor': 'council_member',
    'councilmember': 'council_member',
    'council member': 'council_member',
    'councilman': 'council_member',
    'councilwoman': 'council_member',
    'council': 'council_member',
}


def _classify_speaker(speaker_role: str | None) -> str:
    """Map a speaker_role string to a speaker_class enum value."""
    role_norm = (speaker_role or '').strip().lower()
    return _ROLE_TO_CLASS.get(role_norm, 'staff')


def _strip_markdown_fence(text: str) -> str:
    """council_quotes content sometimes arrives wrapped in ```json ... ```."""
    m = re.search(r'```(?:json)?\s*(.+?)\s*```', text, re.DOTALL)
    return m.group(1) if m else text


def migrate_member_quotes(dry_run: bool = False) -> dict:
    """Phase 1: member_quotes → quotes.

    All rows get speaker_class='council_member', is_broadcast_hero=0.
    Verification + alignment + correction state preserved.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    rows = cursor.execute("""
        SELECT mq.id, mq.member_id, mq.meeting_id, mq.quote_text,
               mq.quote_text_original, mq.topic_tags, mq.minutes_page_ref,
               mq.video_timestamp_seconds, mq.proof_clip_url,
               mq.verified_status, mq.verified_by, mq.verified_at,
               mq.gemini_correction_notes, mq.extracted_at, mq.word_timings,
               cm.name AS speaker_name, cm.role AS speaker_role
        FROM member_quotes mq
        LEFT JOIN council_members cm ON cm.id = mq.member_id
    """).fetchall()

    migrated = 0
    skipped_missing_member = 0
    idempotent_skips = 0

    for r in rows:
        speaker_name = r['speaker_name']
        if not speaker_name:
            # member_id points to a council_members row we can't find — skip
            # rather than guess. Operator can backfill manually if needed.
            skipped_missing_member += 1
            continue

        speaker_role = r['speaker_role']
        quote_text = (r['quote_text'] or '').strip()
        if not quote_text:
            skipped_missing_member += 1
            continue
        content_hash = _compute_content_hash(speaker_name, quote_text)

        existing = cursor.execute(
            "SELECT id FROM quotes WHERE meeting_id = ? AND content_hash = ?",
            (r['meeting_id'], content_hash),
        ).fetchone()

        if existing:
            idempotent_skips += 1
            continue

        if dry_run:
            migrated += 1
            continue

        cursor.execute("""
            INSERT INTO quotes (
                meeting_id, member_id, speaker_name, speaker_role, speaker_class,
                quote_text, quote_text_original, topic_tags, minutes_page_ref,
                video_timestamp_seconds, word_timings,
                verified_status, verified_by, verified_at, gemini_correction_notes,
                proof_clip_url,
                is_broadcast_hero, content_hash, extracted_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, 'council_member',
                ?, ?, ?, ?,
                ?, ?,
                ?, ?, ?, ?,
                ?,
                0, ?, ?, CURRENT_TIMESTAMP
            )
        """, (
            r['meeting_id'], r['member_id'], speaker_name, speaker_role,
            quote_text, r['quote_text_original'], r['topic_tags'], r['minutes_page_ref'],
            r['video_timestamp_seconds'], r['word_timings'],
            r['verified_status'] or 'pending', r['verified_by'], r['verified_at'],
            r['gemini_correction_notes'],
            r['proof_clip_url'],
            content_hash, r['extracted_at'],
        ))
        migrated += 1

    if not dry_run:
        conn.commit()
    conn.close()

    return {
        'migrated': migrated,
        'skipped_missing_member': skipped_missing_member,
        'idempotent_skips': idempotent_skips,
    }


def migrate_council_quotes(dry_run: bool = False) -> dict:
    """Phase 2: council_quotes JSON blobs → quotes.

    Quotes matching existing rows by content_hash get UPDATE is_broadcast_hero=1
    (preserving verification state). Non-matching quotes (e.g., Police Captain)
    INSERT as fresh rows with speaker_class derived from speaker_role.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    rows = cursor.execute("""
        SELECT no.meeting_id, no.content, m.city_name
        FROM notebook_outputs no
        LEFT JOIN meetings m ON m.id = no.meeting_id
        WHERE no.output_type = 'council_quotes'
          AND no.content IS NOT NULL
          AND no.content != ''
    """).fetchall()

    blobs_seen = 0
    flagged_existing = 0
    inserted_new = 0
    skipped_invalid_blob = 0
    skipped_invalid_quote = 0
    idempotent_skips = 0

    for r in rows:
        blobs_seen += 1
        txt = _strip_markdown_fence(r['content'])
        try:
            data = json.loads(txt)
        except (json.JSONDecodeError, TypeError):
            skipped_invalid_blob += 1
            continue

        quotes_list = data.get('quotes') if isinstance(data, dict) else data
        if not isinstance(quotes_list, list):
            skipped_invalid_blob += 1
            continue

        for q in quotes_list:
            if not isinstance(q, dict):
                skipped_invalid_quote += 1
                continue
            speaker_name = (q.get('speaker_name') or q.get('speaker') or '').strip()
            quote_text = (q.get('text') or q.get('quote_text') or '').strip()
            if not speaker_name or not quote_text:
                skipped_invalid_quote += 1
                continue

            speaker_role = (q.get('speaker_role') or '').strip() or None
            speaker_class = _classify_speaker(speaker_role)
            content_hash = _compute_content_hash(speaker_name, quote_text)

            existing = cursor.execute(
                "SELECT id, is_broadcast_hero FROM quotes WHERE meeting_id = ? AND content_hash = ?",
                (r['meeting_id'], content_hash),
            ).fetchone()

            if existing:
                if existing['is_broadcast_hero'] == 1:
                    idempotent_skips += 1
                    continue
                if not dry_run:
                    cursor.execute(
                        "UPDATE quotes SET is_broadcast_hero = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (existing['id'],),
                    )
                flagged_existing += 1
            else:
                if not dry_run:
                    # Look up member_id if speaker_class is council_member
                    member_id = None
                    if speaker_class == 'council_member' and r['city_name']:
                        member_id = _lookup_member_id_via_cursor(
                            cursor, r['city_name'], speaker_name
                        )
                    topic = q.get('topic')
                    topic_tags_str = json.dumps([topic]) if topic else None
                    cursor.execute("""
                        INSERT INTO quotes (
                            meeting_id, member_id, speaker_name, speaker_role, speaker_class,
                            quote_text, topic_tags,
                            is_broadcast_hero, content_hash, extracted_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """, (
                        r['meeting_id'], member_id, speaker_name, speaker_role, speaker_class,
                        quote_text, topic_tags_str,
                        content_hash,
                    ))
                inserted_new += 1

    if not dry_run:
        conn.commit()
    conn.close()

    return {
        'blobs_seen': blobs_seen,
        'flagged_existing': flagged_existing,
        'inserted_new': inserted_new,
        'skipped_invalid_blob': skipped_invalid_blob,
        'skipped_invalid_quote': skipped_invalid_quote,
        'idempotent_skips': idempotent_skips,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dry-run', action='store_true',
                        help="Report what would happen without writing.")
    args = parser.parse_args()

    print("=" * 64)
    print("Quotes Unification — Migration (Chunk 2)")
    if args.dry_run:
        print("DRY RUN — no changes will be written")
    print("=" * 64)

    print()
    print("Phase 1: member_quotes -> quotes")
    p1 = migrate_member_quotes(dry_run=args.dry_run)
    for k, v in p1.items():
        print(f"  {k}: {v}")

    print()
    print("Phase 2: council_quotes blobs -> quotes")
    p2 = migrate_council_quotes(dry_run=args.dry_run)
    for k, v in p2.items():
        print(f"  {k}: {v}")

    print()
    if args.dry_run:
        print("DRY RUN complete - no changes written.")
    else:
        print("Migration complete.")


if __name__ == '__main__':
    main()
