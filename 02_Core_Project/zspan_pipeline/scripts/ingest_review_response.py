#!/usr/bin/env python3.11
"""
Ingest Gemini Pro RESPONSE.md files — apply verdicts + mechanical corrections.

T-013 V3, per D-043. Closes the human-review round trip:

  - Reads `batch_NN/RESPONSE.md` for every batch under a meeting's
    review queue (or one specific RESPONSE.md if `--response-file` is
    passed).
  - For each clip the reviewer's Gemini Pro session evaluated:
      * Maps the filename → `quotes.id` via the BATCH_MANIFEST.json
      * Mechanically applies `"X" should be "Y"` substitutions Gemini
        identified, preserving the original extraction in
        `quotes.quote_text_original` for audit.
      * Sets `verified_status` per `review_response_parser.classify_decision`:
          - speaker:no  OR text:no       → `rejected`
          - speaker:uncertain            → `disputed`
          - text:mostly with clean diffs → `verified` (after substitutions)
          - text:mostly with prose diffs → `disputed`
          - speaker:yes + text:yes        → `verified`
      * Writes `quotes.gemini_correction_notes` as a JSON audit
        blob (source response file, response-received timestamp, raw
        Gemini fields, applied substitutions, unapplied differences,
        decision, ingestion timestamp).
      * On text correction, `content_hash` is recomputed (so subsequent
        re-extractions match the corrected form and UPDATE rather than
        orphan) and `word_timings` is NULLed for re-alignment.

Reads/writes the unified `quotes` table (post-D-052 refactor, 2026-05-26).
The legacy `member_quotes`-table write path is gone; the legacy table is
in archive-only mode.

  - NO new LLM is invoked. Substitutions are mechanical string
    replacements. The chain stays within the doctrinal constraint
    that the pipeline is the sole content generator (D-001 / D-043).

Usage:
    cd 02_Core_Project

    # Process every RESPONSE.md under a meeting's review queue:
    python3.11 -m zspan_pipeline.scripts.ingest_review_response --meeting-id 101091

    # Process one specific RESPONSE.md:
    python3.11 -m zspan_pipeline.scripts.ingest_review_response \
        --response-file ".../batch_01/RESPONSE.md"

    # Dry-run (preview changes without modifying the DB):
    python3.11 -m zspan_pipeline.scripts.ingest_review_response --meeting-id 101091 --dry-run

Idempotency: the script writes the audit JSON with the source path of
the RESPONSE.md. Re-running on a quote whose audit JSON already references
the SAME response file (matched by SHA256 of the response content) skips
the row unless `--force` is passed. This makes the script safe to run
multiple times — e.g., after the reviewer edits a RESPONSE.md and saves
again, re-run picks up the changes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Windows cp1252 stdout chokes on em-dashes / arrows / box-drawing chars in
# the docstring + help strings. Reconfigure to UTF-8 with `errors='replace'`
# so this script can be invoked from any console without surprise crashes.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass  # Non-TextIOWrapper streams (tests, piped buffers) — leave as-is.

_PARSERS_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "council_navigator"
    / "parsers"
)
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

from database import (  # noqa: E402
    get_connection,
    update_quote_verification,
    upsert_vocabulary_correction,
)
from review_response_parser import (  # noqa: E402
    ClipVerdict,
    apply_substitutions,
    classify_decision,
    extract_substitutions,
    parse_response_file,
)


# ── Helpers ──────────────────────────────────────────────────────────


def _file_sha256(path: Path) -> str:
    """SHA256 of the response file content. Used for idempotency — re-runs
    against the SAME file content are skipped; if the reviewer edits the
    file, the hash changes and we re-process."""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _find_response_files_for_meeting(meeting_id: int) -> list[Path]:
    """Walk `media/review_queue/**/BATCH_MANIFEST.json`, filter by
    meeting_id, then collect the sibling `batch_*/RESPONSE.md` files.
    """
    media_root = _PARSERS_DIR.parent / "media" / "review_queue"
    if not media_root.exists():
        return []
    out: list[Path] = []
    for manifest_path in media_root.rglob("BATCH_MANIFEST.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if manifest.get("meeting_id") != meeting_id:
            continue
        meeting_dir = manifest_path.parent
        for batch in manifest.get("batches", []):
            response_rel = batch.get("response_file")
            if not response_rel:
                continue
            response_path = meeting_dir / response_rel
            if response_path.exists():
                out.append(response_path)
    return sorted(out)


def _find_manifest_for_response(response_path: Path) -> dict | None:
    """Walk up from the response file to find the BATCH_MANIFEST.json
    that covers it. The manifest is at the meeting-level (parent of
    batch_NN/)."""
    meeting_dir = response_path.parent.parent
    manifest_path = meeting_dir / "BATCH_MANIFEST.json"
    if not manifest_path.exists():
        return None
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _filename_to_quote_id_map(manifest: dict) -> dict[str, int]:
    """Build `{clip_filename: quote_id}` from the manifest."""
    out: dict[str, int] = {}
    for batch in manifest.get("batches", []):
        for clip in batch.get("clips", []):
            fn = clip.get("filename")
            qid = clip.get("quote_id")
            if fn and isinstance(qid, int):
                out[fn] = qid
    return out


# ── Per-clip ingestion ───────────────────────────────────────────────


def _existing_audit_hash(notes_json: str | None) -> str | None:
    """Pull the source-response file SHA from a stored gemini_correction_notes
    JSON, if any. Returns None if no audit yet or shape is unexpected."""
    if not notes_json:
        return None
    try:
        notes = json.loads(notes_json)
    except (json.JSONDecodeError, TypeError):
        return None
    return notes.get("source_response_sha256")


def _ingest_clip(
    cur,
    quote_id: int,
    verdict: ClipVerdict,
    response_path: Path,
    response_sha: str,
    response_received: str | None,
    *,
    city_name: str | None,
    manifest_meeting_id: int | None,
    reviewer_kind: str,
    force: bool,
    dry_run: bool,
) -> dict:
    """Apply one clip's verdict + corrections to the corresponding
    `quotes` row. Returns a per-row summary dict for the report.

    Updates flow through the `update_quote_verification` helper in
    `database.py`, which handles the content_hash recomputation +
    word_timings NULLing on text correction automatically. This script's
    job is to compute the verdict + audit JSON; the DB-side write logic
    lives in one place per the Quotes Unification Refactor.

    Side effect (T-017 Layer 2): every `"X" should be "Y"` substitution
    Gemini surfaced is upserted into `city_vocabulary_corrections` for
    `city_name` so future Studio outputs for this city auto-apply the
    correction. We upsert based on what Gemini OBSERVED, not on whether
    this specific clip's decision was verified — a `disputed` or
    `rejected` clip can still teach us a real spelling for the city.
    """
    # Cross-check against manifest's meeting_id so a stale manifest pointing
    # at IDs from a different table (pre-Quotes Unification batches had
    # `member_quotes.id` values that happen to overlap numerically with
    # `quotes.id` for unrelated quotes) cannot misfire verdicts onto
    # wrong rows. If the manifest didn't carry meeting_id, fall back to
    # lookup-by-id-only and accept the historical risk.
    if manifest_meeting_id is not None:
        row = cur.execute(
            """
            SELECT id, quote_text, quote_text_original, verified_status,
                   gemini_correction_notes
            FROM quotes WHERE id = ? AND meeting_id = ?
            """,
            (quote_id, manifest_meeting_id),
        ).fetchone()
    else:
        row = cur.execute(
            """
            SELECT id, quote_text, quote_text_original, verified_status,
                   gemini_correction_notes
            FROM quotes WHERE id = ?
            """,
            (quote_id,),
        ).fetchone()
    if not row:
        return {
            "quote_id": quote_id, "filename": verdict.filename,
            "skipped": True,
            "reason": (
                f"quote id={quote_id} not in `quotes` table for "
                f"meeting_id={manifest_meeting_id} (likely a pre-Unification "
                f"manifest — regenerate via build_review_queue.py)"
                if manifest_meeting_id is not None
                else "quote not found in DB"
            ),
        }

    existing_audit_sha = _existing_audit_hash(row["gemini_correction_notes"])
    if existing_audit_sha == response_sha and not force:
        return {
            "quote_id": quote_id, "filename": verdict.filename,
            "skipped": True, "reason": "already ingested (same response sha)",
            "current_status": row["verified_status"],
        }

    current_text = row["quote_text"] or ""

    decision = classify_decision(verdict)
    substitutions = extract_substitutions(verdict.text_differences)

    # Only apply substitutions on the `verified` path. For `disputed`
    # or `rejected` we DON'T modify the text — the human will look at
    # it and decide what to do. (The clean-substitution case for
    # `verified` already passed `classify_decision`'s check that
    # substitutions exist OR the differences are accepted disfluency
    # phrases.)
    new_text = current_text
    applied_log: list[dict] = []
    if decision == "verified" and substitutions:
        new_text, applied_log = apply_substitutions(current_text, substitutions)

    text_changed = (new_text != current_text) and bool(applied_log)
    # `update_quote_verification` NULLs word_timings whenever it detects a
    # text change, so a successful correction triggers re-alignment on the
    # post-ingest pass below.
    invalidate_word_timings = text_changed

    # T-017 Layer 2 — record every observed substitution to the city's
    # vocabulary dictionary. Runs even when decision != 'verified' (a
    # rejected quote can still surface a real spelling correction for
    # the city) but skipped on dry_run so the preview is non-mutating.
    vocab_upserts: list[dict] = []
    if substitutions and city_name and not dry_run:
        for wrong, right in substitutions:
            try:
                upsert = upsert_vocabulary_correction(
                    city_name=city_name,
                    wrong=wrong,
                    right=right,
                    source_response_file=str(response_path),
                )
                vocab_upserts.append({
                    "wrong": wrong,
                    "right": right,
                    "was_new": upsert["was_new"],
                    "applied_count": upsert["applied_count"],
                })
            except ValueError:
                # Empty strings, etc. — already filtered by extract_substitutions
                # but defensive anyway.
                continue

    audit = {
        "source_response_file": str(response_path),
        "source_response_sha256": response_sha,
        "response_received": response_received,
        "raw_gemini_verdict": dict(verdict.raw_fields),
        "applied_substitutions": applied_log,
        "city_vocabulary_upserts": vocab_upserts,
        "decision": decision,
        "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    if dry_run:
        return {
            "quote_id": quote_id, "filename": verdict.filename,
            "skipped": False, "dry_run": True,
            "decision": decision,
            "text_changed": text_changed,
            "applied_substitutions": applied_log,
            "vocab_upserts_would_be": len(substitutions) if substitutions and city_name else 0,
            "word_timings_would_be_invalidated": invalidate_word_timings,
            "current_status_was": row["verified_status"],
        }

    # Single write path — the helper detects text-change-vs-not internally
    # and runs the right SQL (with content_hash recomputation when needed).
    update_quote_verification(
        quote_id=quote_id,
        verified_status=decision,
        verified_by=reviewer_kind,
        gemini_correction_notes=audit,
        corrected_quote_text=new_text if text_changed else None,
    )

    return {
        "quote_id": quote_id, "filename": verdict.filename,
        "skipped": False, "dry_run": False,
        "decision": decision,
        "text_changed": text_changed,
        "applied_substitutions": applied_log,
        "vocab_upserts": vocab_upserts,
        "word_timings_invalidated": invalidate_word_timings,
        "current_status_was": row["verified_status"],
    }


# ── CLI ──────────────────────────────────────────────────────────────


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--meeting-id", type=int, help="Ingest every RESPONSE.md under this meeting's review queue.")
    group.add_argument("--response-file", type=Path, help="Ingest a single RESPONSE.md.")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to DB.")
    parser.add_argument("--force", action="store_true", help="Re-ingest even if the response file's SHA already matches what's in the audit JSON.")
    parser.add_argument(
        "--reviewer-kind",
        default="human via Gemini Pro",
        help=(
            "Stamped into `quotes.verified_by` for audit. Default 'human via Gemini Pro' "
            "matches the original V3 flow. For the Mac-handoff workflow pass something "
            "like 'Claude on Mac driving Gemini Pro' so the audit trail captures who "
            "actually attested."
        ),
    )
    args = parser.parse_args()

    # Find target RESPONSE files
    if args.meeting_id:
        response_paths = _find_response_files_for_meeting(args.meeting_id)
        if not response_paths:
            print(f"No RESPONSE.md files found for meeting_id={args.meeting_id}.")
            print("Has build_review_queue run for this meeting? Has the reviewer pasted Gemini's replies?")
            return 1
    else:
        if not args.response_file.exists():
            print(f"ERROR: response file not found: {args.response_file}")
            return 1
        response_paths = [args.response_file]

    print("=" * 64)
    print(f"  Ingest review responses ({'dry-run' if args.dry_run else 'live'})")
    print("=" * 64)
    print(f"  Response files: {len(response_paths)}")
    for p in response_paths:
        print(f"    - {p}")
    print()

    conn = get_connection()
    cur = conn.cursor()
    total_summary = {
        "files_processed": 0,
        "clips_processed": 0,
        "clips_skipped": 0,
        "verified": 0,
        "disputed": 0,
        "rejected": 0,
        "text_changes_applied": 0,
        "vocab_corrections_upserted": 0,
        "vocab_corrections_new": 0,
        "word_timings_invalidated": 0,
    }

    # Meetings where at least one quote had its text changed by V3 —
    # alignment needs to be re-run for these so word_timings reflect
    # the corrected display tokens (otherwise the karaoke renders the
    # PRE-correction words even though quote_text is fixed).
    meetings_needing_realign: set[int] = set()

    # D-054 follow-up: quote_ids that ended in 'disputed' status. After the
    # ingest loop finishes, pre-compute the polish + emphasis display caches
    # in parallel so the operator opens DisputedQuotesPage to fully-populated
    # rows (no 30s cold load). Lazy-compute in /api/disputed-quotes stays as
    # defense-in-depth fallback.
    newly_disputed_quote_ids: list[int] = []

    for response_path in response_paths:
        manifest = _find_manifest_for_response(response_path)
        if manifest is None:
            print(f"WARN: no BATCH_MANIFEST.json found for {response_path}; skipping")
            continue

        # Manifest version guard FIRST — fails loud-and-fast on stale
        # manifests before any of the placeholder-mtime / batch-complete /
        # parse-error warnings fire (those are noise for batches we're
        # rejecting anyway). v1 manifests (no `manifest_version` field)
        # were generated by build_review_queue.py BEFORE the Quotes
        # Unification Refactor — their `quote_id` values reference the
        # legacy `member_quotes` table. Those IDs overlap numerically
        # with `quotes.id` but point to UNRELATED rows. Refuse them.
        manifest_version = manifest.get("manifest_version", 1)
        if manifest_version < 2:
            print(
                f"SKIP: {response_path.relative_to(_PARSERS_DIR.parent)}: "
                f"stale manifest (manifest_version={manifest_version}, "
                f"pre-Quotes-Unification). The clip IDs reference the legacy "
                f"`member_quotes` table and would misfire onto unrelated rows "
                f"in the unified `quotes` table.\n"
                f"      Remediation: archive this batch directory and regenerate "
                f"via build_review_queue.py against the unified `quotes` table."
            )
            continue

        parsed = parse_response_file(response_path)
        if not parsed.clips:
            print(f"SKIP: {response_path.name}: no clip blocks parsed (file may be empty or malformed).")
            continue
        if not parsed.has_batch_complete_marker:
            print(f"WARN: {response_path.name}: missing '## BATCH COMPLETE' marker — Gemini may have been cut off. Proceeding anyway.")

        # Tolerance: if reviewer didn't fill in the "Response received"
        # field (still has the `_[REPLACE THIS...]` placeholder), infer
        # from the file's mtime — that's when the reviewer last saved,
        # which is functionally the moment-of-record.
        response_received = parsed.response_received
        if parsed.response_received_is_placeholder:
            mtime_iso = datetime.fromtimestamp(
                response_path.stat().st_mtime, tz=timezone.utc,
            ).isoformat(timespec="seconds")
            print(
                f"WARN: {response_path.name}: 'Response received' field still has placeholder. "
                f"Inferring from file mtime: {mtime_iso}"
            )
            response_received = f"{mtime_iso} (inferred from file mtime)"
        parsed.response_received = response_received  # for audit JSON

        filename_map = _filename_to_quote_id_map(manifest)
        response_sha = _file_sha256(response_path)
        manifest_city = manifest.get("city")
        manifest_meeting_id = manifest.get("meeting_id")
        if not manifest_city:
            print(f"WARN: {response_path.name}: manifest has no 'city' field — vocabulary upserts will be skipped for this batch.")
        if not isinstance(manifest_meeting_id, int):
            print(
                f"WARN: {response_path.name}: manifest missing 'meeting_id' — "
                f"the cross-check guard against stale-ID misfires can't run for this batch."
            )
            manifest_meeting_id = None

        print(f"\n--- {response_path.relative_to(_PARSERS_DIR.parent)}")
        print(f"    Response received: {parsed.response_received}")
        print(f"    Clip verdicts parsed: {len(parsed.clips)}")

        for verdict in parsed.clips:
            quote_id = filename_map.get(verdict.filename)
            if quote_id is None:
                print(f"    {verdict.filename:50s}  WARN no quote_id in manifest")
                continue

            result = _ingest_clip(
                cur, quote_id, verdict, response_path, response_sha,
                parsed.response_received,
                city_name=manifest_city,
                manifest_meeting_id=manifest_meeting_id,
                reviewer_kind=args.reviewer_kind,
                force=args.force, dry_run=args.dry_run,
            )
            total_summary["clips_processed"] += 1

            if result.get("skipped"):
                total_summary["clips_skipped"] += 1
                print(f"    {verdict.filename:50s}  SKIP  {result.get('reason')}")
                continue

            decision = result.get("decision", "?")
            total_summary[decision] = total_summary.get(decision, 0) + 1
            change_str = " [text-changed]" if result.get("text_changed") else ""
            applied = result.get("applied_substitutions") or []
            applied_summary = ""
            if applied:
                applied_summary = " | subs: " + ", ".join(
                    f"{s['from']!r}->{s['to']!r}(x{s['count']})" for s in applied
                )
            vocab_upserts = result.get("vocab_upserts") or []
            if vocab_upserts:
                total_summary["vocab_corrections_upserted"] += len(vocab_upserts)
                total_summary["vocab_corrections_new"] += sum(
                    1 for v in vocab_upserts if v.get("was_new")
                )
                applied_summary += " | vocab: " + ", ".join(
                    f"{v['wrong']!r}->{v['right']!r}"
                    + ("(new)" if v["was_new"] else f"(x{v['applied_count']})")
                    for v in vocab_upserts
                )
            print(f"    {verdict.filename:50s}  {decision:9s}{change_str}{applied_summary}")
            if result.get("text_changed"):
                total_summary["text_changes_applied"] += 1
            if result.get("word_timings_invalidated"):
                total_summary["word_timings_invalidated"] += 1
                # Look up the quote's meeting_id so we know which meetings
                # to realign at the end. Cheap point-lookup.
                mid = cur.execute(
                    "SELECT meeting_id FROM quotes WHERE id = ?",
                    (quote_id,),
                ).fetchone()
                if mid and mid["meeting_id"]:
                    meetings_needing_realign.add(int(mid["meeting_id"]))
            # Collect IDs of quotes that ended in 'disputed' so we can
            # eagerly populate their D-054 display caches after the loop.
            # Either-or: a verified or rejected quote drops off
            # DisputedQuotesPage and doesn't need the helpers.
            if decision == "disputed":
                newly_disputed_quote_ids.append(quote_id)

        total_summary["files_processed"] += 1

    if not args.dry_run:
        conn.commit()
    conn.close()

    # Re-run alignment per affected meeting. `align_quotes_for_meeting` is
    # idempotent — it skips quotes whose word_timings is already populated,
    # so only the rows the helper NULLed will be reprocessed. The cost per
    # meeting is one transcript_words load + one SequenceMatcher run per
    # quote (~ms each).
    if not args.dry_run and meetings_needing_realign:
        print()
        print(
            f"Re-running alignment for {len(meetings_needing_realign)} meeting(s) "
            f"with invalidated word_timings..."
        )
        try:
            from quote_align import align_quotes_for_meeting  # noqa: E402
            for mid in sorted(meetings_needing_realign):
                stats = align_quotes_for_meeting(mid)
                print(f"  meeting_id={mid}: {stats}")
        except Exception as e:
            print(f"  WARN: realignment failed ({e}); manual re-run required:")
            for mid in sorted(meetings_needing_realign):
                print(
                    f"    python3.11 -c \"import sys; "
                    f"sys.path.insert(0, 'council_navigator/parsers'); "
                    f"from quote_align import align_quotes_for_meeting; "
                    f"print(align_quotes_for_meeting({mid}))\""
                )

    # D-054 follow-up: eagerly pre-compute polish + verdict-emphasis for every
    # newly-disputed quote. Runs in parallel (ThreadPoolExecutor inside the
    # helper). When the operator opens DisputedQuotesPage, the rows already
    # have their display caches populated — no 30s cold-load wait.
    if not args.dry_run and newly_disputed_quote_ids:
        print()
        print(
            f"Pre-computing display caches (polish + verdict-emphasis) "
            f"for {len(newly_disputed_quote_ids)} newly-disputed quote(s)..."
        )
        try:
            from quote_display_precompute import precompute_display_cache_for_quote_ids
            stats = precompute_display_cache_for_quote_ids(newly_disputed_quote_ids)
            print(f"  {stats}")
        except Exception as e:
            print(
                f"  WARN: display-cache pre-compute failed ({e}); operator "
                f"will see lazy-compute on first DisputedQuotesPage load."
            )

    print()
    print("Summary:")
    for k, v in total_summary.items():
        print(f"  {k}: {v}")
    print()
    if args.dry_run:
        print("(dry-run — no DB writes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
