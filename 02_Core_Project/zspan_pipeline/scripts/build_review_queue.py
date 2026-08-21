#!/usr/bin/env python3.11
"""
Build a local review queue for a meeting — the T-013 human-review workflow.
==========================================================================

For a given meeting_id, extracts a video clip for every featured quote
that has aligned `word_timings`, writes each clip + a sidecar JSON of
metadata to a per-meeting folder under `media/review_queue/`, and
generates a `REVIEW_GUIDE.md` checklist the reviewer works through.

The intended flow:

  1. Run this script after a meeting has been fully processed (transcript
     + alignment + unified `quotes` table populated).
  2. Open the resulting folder in File Explorer (or hand the package off
     to a Mac-side Claude — see `build_mac_handoff_package.py`).
  3. For each clip in REVIEW_GUIDE.md: drag-drop into Gemini Pro alongside
     the verification prompt template, get a verdict, tick the box.
  4. Round-trip via `ingest_review_response.py` — paste verdicts into
     `batch_NN/RESPONSE.md`, then run ingest to apply to `quotes` rows.

Reads from the unified `quotes` table (post-D-052 refactor, 2026-05-26).
Replaces the legacy `member_quotes`-table read path.

Output structure:

    media/review_queue/
      <city_slug>/
        <YYYY-MM-DD>__<meeting_slug>/
          source.mp4                          (cached full video, shared across clips)
          quote_<id>__<member_slug>.mp4
          quote_<id>__<member_slug>.json      (sidecar metadata)
          ... (one pair per quote with word_timings)
          REVIEW_GUIDE.md                     (markdown checklist + prompt template)

Usage:
    cd 02_Core_Project
    python3.11 -m zspan_pipeline.scripts.build_review_queue --meeting-id 101091

Flags:
    --meeting-id N        Required. The meeting whose quotes get extracted.
    --base-dir PATH       Override the default `media/review_queue/` root.
    --include-other       Include quotes tagged 'other' (default: True; the
                          'other' bucket is most of the quote volume for now).
    --max-quotes N        Cap how many clips to extract (default: all aligned).
                          Useful for sampling a few before committing to all.

Cost: free at the margin. yt-dlp downloads the source video once (~45 MB
at 144p for a typical Mohave meeting); each per-quote ffmpeg cut is
~1 second of CPU + ~400 KB of disk. No API quota consumed.
"""
from __future__ import annotations

import argparse
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

# Make parsers/ importable
_PARSERS_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "council_navigator"
    / "parsers"
)
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

from database import get_connection  # noqa: E402
from proofs_uploader import (  # noqa: E402
    CLIP_LEAD_SECONDS,
    CLIP_TRAIL_SECONDS,
    ProofClipMetadata,
    ProofsError,
    _slugify,
    build_description,
    build_title,
    compute_sha256,
    extract_clip,
    save_clip_sidecar,
)
from zspan_pipeline.prompt_loader import strip_explicit_model_boundaries  # noqa: E402


# ── Constants ────────────────────────────────────────────────────────


# `media/review_queue/` lives next to parsers/. Resolves to the project's
# council_navigator/media/review_queue/ directory.
DEFAULT_BASE_DIR = _PARSERS_DIR.parent / "media" / "review_queue"

# Gemini Pro caps attachments at 10 per chat upload (as of 2026-05-16).
# That's the natural batch size for the review workflow.
DEFAULT_BATCH_SIZE = 10

# Path to the single-clip verification prompt template (legacy / ad-hoc path).
PROMPT_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "prompts"
    / "verification_prompt_template.md"
)

# Path to the batch-level verification prompt template (T-013 V2 — the
# 10-at-a-time workflow). Loaded fresh on each build so prompt iteration
# doesn't require restarting anything.
BATCH_PROMPT_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "prompts"
    / "verification_batch_prompt_template.md"
)


# ── Quote selection ─────────────────────────────────────────────────


def _load_meeting_metadata(cur, meeting_id: int) -> dict | None:
    cur.execute(
        """
        SELECT m.id, m.city_name, m.meeting_title, m.meeting_date, m.county,
               m.state, m.notebook_id,
               COALESCE(wo.youtube_video_url, m.video_url) AS source_video_url,
               wo.state AS wo_state
        FROM meetings m
        LEFT JOIN work_orders wo ON wo.meeting_id = m.id
        WHERE m.id = ?
        """,
        (meeting_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _load_quotes_for_review(
    cur, meeting_id: int, include_other: bool, hero_only: bool = False,
) -> list[dict]:
    """Pull every aligned quote (non-null word_timings) for the meeting
    from the unified `quotes` table. Skips already-verified rows (no need
    to re-verify) and rejected rows (operator-decided no-go). Skips quotes
    whose only topic_tag is 'other' if include_other=False (typically
    left True since 'other' is most of the volume in V1).

    When `hero_only=True`, restricts to `is_broadcast_hero=1` — the
    publish-gate verification scope per D-053. Cast-page quotes that
    aren't broadcast heroes aren't gated on publish, so this lets the
    operator scope verification work to "just what blocks the next push."

    speaker_name + speaker_role are denormalized on the unified table
    (no JOIN with council_members needed; staff + external speakers
    have no council_members row but still have a speaker_name).
    """
    where = [
        "meeting_id = ?",
        "word_timings IS NOT NULL",
        "word_timings != ''",
        "verified_status IN ('pending', 'disputed')",
    ]
    params: list = [meeting_id]
    if hero_only:
        where.append("is_broadcast_hero = 1")
    cur.execute(
        f"""
        SELECT id, quote_text, topic_tags, word_timings,
               video_timestamp_seconds,
               speaker_name, speaker_role, speaker_class,
               is_broadcast_hero
        FROM quotes
        WHERE {" AND ".join(where)}
        ORDER BY COALESCE(video_timestamp_seconds, 999999), id
        """,
        params,
    )
    quotes = []
    for r in cur.fetchall():
        try:
            timings = json.loads(r["word_timings"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(timings, list) or not timings:
            continue
        try:
            tags = json.loads(r["topic_tags"]) if r["topic_tags"] else []
        except (json.JSONDecodeError, TypeError):
            tags = []
        if not include_other:
            non_other = [t for t in tags if t and t != "other"]
            if not non_other:
                continue
        topic_tag = (tags[0] if tags else "other") or "other"

        first_ms = timings[0].get("start_ms", 0)
        last_ms = timings[-1].get("end_ms", first_ms)
        clip_start = max(0.0, (first_ms / 1000.0) - CLIP_LEAD_SECONDS)
        clip_end = (last_ms / 1000.0) + CLIP_TRAIL_SECONDS

        quotes.append({
            "row": dict(r),
            "tags": tags,
            "topic_tag": topic_tag,
            "clip_start": clip_start,
            "clip_end": clip_end,
        })
    return quotes


# ── Batch generator (T-013 V2 — 10-at-a-time Gemini Pro workflow) ──


def _load_batch_prompt_template() -> str:
    """Read the batch-level verification prompt template. Falls back to
    a minimal default if the file is missing.
    """
    if BATCH_PROMPT_TEMPLATE_PATH.exists():
        raw = BATCH_PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
        return strip_explicit_model_boundaries(_strip_frontmatter(raw))
    # Minimal fallback — should never hit this in practice.
    return (
        "# Batch {batch_index} of {batch_total}\n\n"
        "Review the {batch_count} attached clips and respond per-clip in "
        "structured form. See the project's verification_batch_prompt_template.md.\n\n"
        "{clips_list}\n"
    )


def _render_clips_list(records: list[dict]) -> str:
    """Per-batch clip-detail block injected into the prompt template's
    `{clips_list}` placeholder."""
    lines: list[str] = []
    for i, rec in enumerate(records, start=1):
        meta = rec["meta"]
        lines.append(f"### Clip {i} — filename: `{rec['clip_filename']}`")
        lines.append("")
        lines.append(f"- **Speaker (attributed):** {meta.speaker_name}")
        lines.append(f"- **Topic tag:** {meta.topic_tag}")
        lines.append(f"- **Quote text (extracted):**")
        lines.append("")
        lines.append(f"  > {meta.quote_text}")
        lines.append("")
    return "\n".join(lines)


def _render_batch_prompt(
    template: str,
    batch_index: int,
    batch_total: int,
    meeting: dict,
    records: list[dict],
) -> str:
    try:
        return template.format(
            batch_index=f"{batch_index:02d}",
            batch_count=len(records),
            batch_total=batch_total,
            city=meeting["city_name"],
            meeting_date=meeting["meeting_date"],
            meeting_title=meeting["meeting_title"],
            clips_list=_render_clips_list(records),
        )
    except KeyError as e:
        return (
            f"[NOTE: batch prompt template references unknown field {e}; "
            f"falling back to template as-is]\n\n{template}\n\n"
            + _render_clips_list(records)
        )


def _render_response_stub(
    batch_index: int,
    batch_total: int,
    meeting: dict,
    records: list[dict],
    generated_at_iso: str,
) -> str:
    """Empty stub file the reviewer pastes Gemini's response into.
    Carries the audit-trail metadata above the paste-area.
    """
    lines: list[str] = []
    lines.append(f"# Batch {batch_index:02d} of {batch_total:02d} · "
                 f"{meeting['city_name']} · {meeting['meeting_date']} · response")
    lines.append("")
    lines.append("## Audit metadata")
    lines.append("")
    lines.append(f"- **Meeting:** {meeting['meeting_title']}")
    lines.append(f"- **Source video:** {meeting.get('source_video_url') or '(none)'}")
    lines.append(f"- **Batch clips ({len(records)}):**")
    for rec in records:
        lines.append(f"    - quote_{rec['meta'].quote_id} · {rec['meta'].speaker_name} · `{rec['clip_filename']}`")
    lines.append(f"- **Reviewer kind:** Human via Gemini Pro")
    lines.append(f"- **Prompt generated:** {generated_at_iso}")
    lines.append(f"- **Response received:** _[REPLACE THIS WITH THE DATE+TIME YOU SAVED GEMINI'S REPLY]_")
    lines.append(f"- **Reviewer notes (free text):** _[optional]_")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## How to complete this file")
    lines.append("")
    lines.append(f"1. Open `batch_{batch_index:02d}_PROMPT.md` and copy its full content.")
    lines.append("2. Open a fresh Gemini Pro chat.")
    lines.append("3. Drag the clips listed above into the chat (all at once).")
    lines.append("4. Paste the prompt content into the same chat. Send.")
    lines.append("5. When Gemini responds, paste its **full** reply below the marker.")
    lines.append("6. Update **Response received** above with today's date+time.")
    lines.append("7. Save this file.")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Gemini response (paste below this line — do NOT edit above)")
    lines.append("")
    lines.append("")
    return "\n".join(lines)


def _write_batches(
    out_dir: Path,
    meeting: dict,
    clip_records: list[dict],
    batch_size: int,
    generated_at_iso: str,
) -> dict:
    """Partition `clip_records` into batches of `batch_size`. For each batch,
    create a `batch_NN/` subdir under `out_dir` containing the batch's
    clip mp4s + sidecar JSONs + a `PROMPT.md` + a `RESPONSE.md` stub.
    Write a top-level `BATCH_MANIFEST.json` at `out_dir` referencing
    everything. Returns the manifest dict for downstream use.

    Clips are EXPECTED to already exist at `out_dir / <clip_filename>`
    (the caller extracted them there). This function MOVES each clip + its
    sidecar JSON into the batch subdir; it does not re-extract.
    """
    template = _load_batch_prompt_template()

    batches: list[list[dict]] = [
        clip_records[i:i + batch_size]
        for i in range(0, len(clip_records), batch_size)
    ]
    batch_total = len(batches)

    manifest = {
        # `manifest_version=2` signals the manifest was built against the
        # unified `quotes` table (post-D-052 refactor). `ingest_review_response`
        # refuses to ingest version-1-or-absent manifests because their
        # `quote_id` values reference the legacy `member_quotes` table and
        # would misfire onto unrelated rows in `quotes` (ID numbers overlap
        # but the content doesn't).
        "manifest_version": 2,
        "source_table": "quotes",
        "meeting_id": meeting["id"],
        "city": meeting["city_name"],
        "meeting_date": meeting["meeting_date"],
        "meeting_title": meeting["meeting_title"],
        "source_video_url": meeting.get("source_video_url"),
        "total_clips": len(clip_records),
        "batch_size": batch_size,
        "batch_total": batch_total,
        "generated_at": generated_at_iso,
        "batches": [],
    }

    for idx, records in enumerate(batches, start=1):
        batch_dir = out_dir / f"batch_{idx:02d}"
        batch_dir.mkdir(parents=True, exist_ok=True)

        # Move each clip + sidecar JSON into its batch folder. The
        # extraction step wrote them at out_dir top level; now they
        # live with their batch's prompt + response files. Idempotent —
        # if the file is already in the batch folder (re-run case),
        # skip the move.
        for rec in records:
            src_clip = out_dir / rec["clip_filename"]
            sidecar_filename = rec["clip_filename"].replace(".mp4", ".json")
            src_sidecar = out_dir / sidecar_filename
            dst_clip = batch_dir / rec["clip_filename"]
            dst_sidecar = batch_dir / sidecar_filename
            if src_clip.exists() and src_clip != dst_clip:
                if dst_clip.exists():
                    dst_clip.unlink()
                src_clip.rename(dst_clip)
            if src_sidecar.exists() and src_sidecar != dst_sidecar:
                if dst_sidecar.exists():
                    dst_sidecar.unlink()
                src_sidecar.rename(dst_sidecar)

        prompt_path = batch_dir / "PROMPT.md"
        response_path = batch_dir / "RESPONSE.md"

        prompt_content = _render_batch_prompt(
            template, idx, batch_total, meeting, records,
        )
        prompt_path.write_text(prompt_content, encoding="utf-8")

        # Only write a response stub if one doesn't already exist — never
        # overwrite a reviewer's pasted Gemini response.
        if not response_path.exists():
            stub = _render_response_stub(
                idx, batch_total, meeting, records, generated_at_iso,
            )
            response_path.write_text(stub, encoding="utf-8")

        manifest["batches"].append({
            "batch_index": idx,
            "batch_folder": batch_dir.name,
            "prompt_file": f"{batch_dir.name}/PROMPT.md",
            "response_file": f"{batch_dir.name}/RESPONSE.md",
            "clip_count": len(records),
            "clips": [
                {
                    "quote_id": rec["meta"].quote_id,
                    "filename": rec["clip_filename"],
                    "path": f"{batch_dir.name}/{rec['clip_filename']}",
                    "speaker_name": rec["meta"].speaker_name,
                    "topic_tag": rec["meta"].topic_tag,
                    "sha256": rec["sha256"],
                }
                for rec in records
            ],
        })

    manifest_path = out_dir / "BATCH_MANIFEST.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


# ── REVIEW_GUIDE.md generator ───────────────────────────────────────


def _strip_frontmatter(text: str) -> str:
    """Strip leading `---\\n...\\n---\\n` YAML frontmatter, if present.
    Matches the convention used by `prompts/*.md` files across the project.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            # Frontmatter spans lines[0..i]; body starts at i+1.
            body = "".join(lines[i + 1:])
            # Drop a single leading blank line if present (common after `---`).
            return body.lstrip("\n")
    return text  # malformed frontmatter — return as-is


def _load_prompt_template() -> str:
    """Read the single-clip verification prompt template (legacy path).
    Used for ad-hoc single-clip reviews; the batch workflow uses
    `_load_batch_prompt_template()`.
    """
    if PROMPT_TEMPLATE_PATH.exists():
        raw = PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
        return strip_explicit_model_boundaries(_strip_frontmatter(raw))
    return (
        "I'm verifying a quote from a public council meeting recording.\n\n"
        "Speaker (as attributed by our system): {speaker_name}\n"
        "Topic: {topic_tag}\n"
        "Quote text (verbatim, cleaned of disfluencies): \"{quote_text}\"\n\n"
        "Please review the attached clip and answer:\n"
        "1. Does the speaker in the video match the attribution above?\n"
        "2. Does the spoken audio match the quote text, allowing for minor "
        "disfluency removal?\n"
        "3. If \"with-differences\", what's different?\n"
        "4. Any other concerns (audio quality, attribution issues, mid-sentence cuts)?\n"
    )


def _render_review_guide(
    meeting: dict,
    clip_records: list[dict],
    manifest: dict,
) -> str:
    """Generate the REVIEW_GUIDE.md content (T-013 V2 — batch-first).

    Leads with the batch-based Gemini Pro workflow. The per-clip detail
    table below the batch summary is reference-only — the rendered
    prompts live in the per-batch PROMPT.md files.
    """
    lines: list[str] = []
    lines.append(f"# Review queue - {meeting['city_name']} - {meeting['meeting_date']}")
    lines.append("")
    lines.append(f"**Meeting:** {meeting['meeting_title']}")
    lines.append(f"**Source video:** {meeting.get('source_video_url') or '(none on file)'}")
    lines.append(f"**Clips to review:** {len(clip_records)}")
    lines.append(f"**Batches:** {manifest['batch_total']} (size {manifest['batch_size']})")
    lines.append(f"**Generated:** {manifest['generated_at']}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## How to review (batch workflow)")
    lines.append("")
    lines.append(
        "For each batch folder below, do the following in Gemini Pro:"
    )
    lines.append("")
    lines.append("1. Open the `batch_NN/` folder in this meeting. Everything you need is inside it.")
    lines.append("2. Open `batch_NN/PROMPT.md` and copy its full content.")
    lines.append("3. Open a fresh Gemini Pro chat.")
    lines.append("4. Drag all the `.mp4` files from `batch_NN/` into the chat at once.")
    lines.append("5. Paste the prompt content alongside the clips. Send.")
    lines.append("6. When Gemini responds, paste its **full** reply into `batch_NN/RESPONSE.md`")
    lines.append("   below the marker line, fill in the `Response received` timestamp, and save.")
    lines.append("7. Tick the batch's checkbox below.")
    lines.append("")
    lines.append(
        "Each batch's RESPONSE.md is the audit record for that review session — "
        "timestamped, with the exact clip set listed, and Gemini's verbatim response preserved."
    )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Batches")
    lines.append("")
    lines.append("| ☐ | Batch | Clips | Folder |")
    lines.append("|---|---|---|---|")
    for b in manifest["batches"]:
        ids = ", ".join(str(c["quote_id"]) for c in b["clips"])
        lines.append(
            f"| ☐ | {b['batch_index']:02d} | {b['clip_count']} (quote ids: {ids}) | "
            f"`{b['batch_folder']}/` |"
        )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## All clips in this meeting (reference)")
    lines.append("")
    lines.append("| # | Speaker | Topic | File | Quote preview |")
    lines.append("|---|---|---|---|---|")
    for i, rec in enumerate(clip_records, start=1):
        meta = rec["meta"]
        preview = (meta.quote_text or "").replace("\n", " ").replace("|", "\\|")
        if len(preview) > 80:
            preview = preview[:77] + "..."
        lines.append(
            f"| {i} | {meta.speaker_name} | {meta.topic_tag} | "
            f"`{rec['clip_filename']}` | {preview} |"
        )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Files in this folder")
    lines.append("")
    lines.append("- `source.mp4` — cached full meeting video (45 MB, shared by all batches; delete to force re-download)")
    lines.append("- `BATCH_MANIFEST.json` — machine-readable record of which clips are in which batch")
    lines.append("- `batch_NN/PROMPT.md` — the prompt to paste into Gemini Pro for that batch")
    lines.append("- `batch_NN/RESPONSE.md` — paste Gemini's reply here; this file IS the audit record")
    lines.append("- `batch_NN/quote_<id>__<speaker>.mp4` — the verification clip (drag into Gemini Pro)")
    lines.append("- `batch_NN/quote_<id>__<speaker>.json` — sidecar metadata (SHA256, timestamps, full quote text, source deep-link)")
    lines.append("")
    lines.append("*Generated by `build_review_queue.py` (T-013 V2 - S-001 Accountability Engine).*")
    lines.append("")
    return "\n".join(lines)


# ── Main orchestrator ───────────────────────────────────────────────


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meeting-id", type=int, required=True)
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=DEFAULT_BASE_DIR,
        help=f"Output root (default: {DEFAULT_BASE_DIR})",
    )
    parser.add_argument(
        "--include-other",
        action="store_true",
        default=True,
        help="Include quotes tagged 'other' (default: True).",
    )
    parser.add_argument(
        "--no-other",
        dest="include_other",
        action="store_false",
        help="Skip quotes whose only topic tag is 'other'.",
    )
    parser.add_argument(
        "--max-quotes",
        type=int,
        default=None,
        help="Cap how many clips to extract. Useful for sampling.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Clips per Gemini Pro batch (default {DEFAULT_BATCH_SIZE} — Gemini's attachment cap).",
    )
    parser.add_argument(
        "--hero-only",
        action="store_true",
        default=False,
        help=(
            "Restrict to is_broadcast_hero=1 quotes — the publish-gate "
            "verification scope per D-053. Default False (verify every "
            "aligned pending/disputed quote so the Cast page benefits too)."
        ),
    )
    args = parser.parse_args()

    conn = get_connection()
    cur = conn.cursor()
    meeting = _load_meeting_metadata(cur, args.meeting_id)
    if not meeting:
        print(f"ERROR: no meeting with id={args.meeting_id}")
        return 1
    if not meeting.get("source_video_url"):
        print(f"ERROR: meeting {args.meeting_id} has no YouTube URL.")
        return 1

    quotes = _load_quotes_for_review(
        cur, args.meeting_id,
        include_other=args.include_other,
        hero_only=args.hero_only,
    )
    if not quotes:
        scope = "broadcast-hero pending/disputed" if args.hero_only else "pending/disputed"
        print(f"No {scope} aligned quotes found for meeting {args.meeting_id}.")
        print("Has alignment run? Check `quotes.word_timings` for this meeting.")
        print("(If you expected hero quotes here but the publish-gate has flipped them")
        print(" to verified already, this is a no-op — there's nothing left to verify.)")
        return 2

    if args.max_quotes:
        quotes = quotes[: args.max_quotes]
    conn.close()

    # Folder layout
    city_slug = _slugify(meeting["city_name"])
    meeting_slug = _slugify(meeting["meeting_title"])
    out_dir = args.base_dir / city_slug / f"{meeting['meeting_date']}__{meeting_slug}"
    out_dir.mkdir(parents=True, exist_ok=True)

    scope_label = "broadcast-hero only" if args.hero_only else "all aligned (hero + non-hero)"
    print("=" * 64)
    print(f"  Review queue - {meeting['city_name']} - {meeting['meeting_date']}")
    print("=" * 64)
    print(f"  Meeting: {meeting['meeting_title']}")
    print(f"  Source : {meeting['source_video_url']}")
    print(f"  Output : {out_dir}")
    print(f"  Scope  : {scope_label}")
    print(f"  Quotes : {len(quotes)} aligned (pending/disputed)")
    print()

    prompt_template = _load_prompt_template()
    clip_records: list[dict] = []

    for i, q in enumerate(quotes, start=1):
        row = q["row"]
        member_slug = _slugify(row["speaker_name"])
        clip_filename = f"quote_{row['id']}__{member_slug}.mp4"
        clip_path = out_dir / clip_filename
        sidecar_path = out_dir / f"quote_{row['id']}__{member_slug}.json"

        meta = ProofClipMetadata(
            quote_id=row["id"],
            quote_text=row["quote_text"],
            speaker_name=row["speaker_name"],
            topic_tag=q["topic_tag"],
            city_name=meeting["city_name"],
            meeting_date=meeting["meeting_date"],
            meeting_title=meeting["meeting_title"],
            source_video_url=meeting["source_video_url"],
            clip_start_seconds=q["clip_start"],
            clip_end_seconds=q["clip_end"],
        )

        print(f"  [{i}/{len(quotes)}] quote {row['id']} - {row['speaker_name']} - {q['topic_tag']}")
        try:
            extract_clip(
                meta.source_video_url,
                meta.clip_start_seconds,
                meta.clip_end_seconds,
                clip_path,
            )
        except ProofsError as e:
            print(f"      ERROR extracting clip: {e}")
            continue

        sha = compute_sha256(clip_path)
        size_bytes = clip_path.stat().st_size
        save_clip_sidecar(sidecar_path, meta, clip_filename, sha, size_bytes)
        print(f"      -> {clip_filename} ({size_bytes / (1024 * 1024):.2f} MB)")

        clip_records.append({
            "meta": meta,
            "clip_filename": clip_filename,
            "sha256": sha,
            "clip_size_bytes": size_bytes,
        })

    if not clip_records:
        print()
        print("No clips were successfully extracted. Check errors above.")
        return 3

    # T-013 V2 — partition clips into Gemini-friendly batches. Each
    # batch is a self-contained subdir: `batch_NN/` with PROMPT.md,
    # RESPONSE.md, plus the batch's clips + sidecars. `_write_batches`
    # MOVES the clips out of out_dir/ into their batch folders.
    generated_at_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest = _write_batches(
        out_dir, meeting, clip_records, args.batch_size, generated_at_iso,
    )

    guide_path = out_dir / "REVIEW_GUIDE.md"
    guide_content = _render_review_guide(meeting, clip_records, manifest)
    guide_path.write_text(guide_content, encoding="utf-8")

    print()
    print("Done.")
    print(f"  Clips written : {len(clip_records)}")
    print(f"  Batches       : {manifest['batch_total']} (size {args.batch_size})")
    print(f"  Manifest      : {out_dir / 'BATCH_MANIFEST.json'}")
    print(f"  Guide         : {guide_path}")
    print()
    print("Workflow (per batch):")
    print("  1. Open batch_NN/ - contains its prompt, response stub, and clips.")
    print("  2. Open PROMPT.md - copy full content.")
    print("  3. Drag all .mp4 files from the batch folder into a fresh Gemini Pro chat.")
    print("  4. Paste the prompt content alongside the clips. Send.")
    print("  5. Paste Gemini's reply into RESPONSE.md, save.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
