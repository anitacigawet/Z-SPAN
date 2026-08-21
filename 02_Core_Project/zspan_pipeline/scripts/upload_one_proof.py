#!/usr/bin/env python3.11
"""
Upload ONE proof clip to the Z-SPAN Proofs YouTube channel.

T-009 Phase 2. Prerequisite: run `setup_youtube_auth.py` first to grant
OAuth and save the refresh token.

Usage:
    cd 02_Core_Project
    python3.11 -m zspan_pipeline.scripts.upload_one_proof --quote-id 19

(Use `python3.11` explicitly — see note in setup_youtube_auth.py.)

Optional flags:
    --privacy public|unlisted|private   Default: unlisted (safe default;
                                        operator promotes to public after
                                        the two-gate review)
    --keep-clip                         Leave the extracted clip on disk
                                        for spot-checking (default: delete
                                        after successful upload)
    --dry-run                           Extract + hash the clip but DON'T
                                        upload. Useful for testing the
                                        clip boundaries without burning
                                        YouTube quota.

What it does end-to-end:
  1. Look up the quote in the DB (quote_text, speaker, topic, meeting,
     source video URL, word_timings).
  2. Extract a video+audio clip from the source YouTube URL using
     yt-dlp's download-ranges + ffmpeg keyframe-cut. Buffer 2s lead-in,
     2s lead-out around the quote.
  3. SHA256-hash the clip — for tamper-evidence embedded in the
     YouTube description.
  4. Upload to YouTube (privacy=unlisted by default) via the OAuth
     refresh token from `setup_youtube_auth.py`.
  5. Persist the resulting YouTube URL into `member_quotes.proof_clip_url`.

Cost: free (YouTube API quota = 1,600 units / upload, you have 10,000/day
default). The clip extraction is local CPU + bandwidth only.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_PARSERS_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "council_navigator"
    / "parsers"
)
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

from proofs_uploader import (  # noqa: E402
    PRIVACY_UNLISTED,
    ProofsError,
    _resolve_quote_metadata,
    build_description,
    build_title,
    compute_sha256,
    extract_clip,
    upload_proof_for_quote,
    CLIP_LEAD_SECONDS,
)
from youtube_oauth import is_authorized  # noqa: E402


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quote-id",
        type=int,
        required=True,
        help="`member_quotes.id` of the target quote. Pick one that "
             "has non-null word_timings (run align_meeting_quotes first).",
    )
    parser.add_argument(
        "--privacy",
        choices=["public", "unlisted", "private"],
        default=PRIVACY_UNLISTED,
        help="YouTube privacy status (default: unlisted).",
    )
    parser.add_argument(
        "--keep-clip",
        action="store_true",
        help="Leave the extracted clip file on disk after upload.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract + hash the clip; SKIP the YouTube upload step.",
    )
    args = parser.parse_args()

    # Pre-flight: confirm OAuth is set up (unless --dry-run).
    if not args.dry_run and not is_authorized():
        print("ERROR: OAuth not configured.")
        print()
        print("Run setup first:")
        print("  python -m zspan_pipeline.scripts.setup_youtube_auth")
        print()
        return 2

    if args.dry_run:
        # Dry-run path: do everything except the API call. Useful for
        # validating clip boundaries cheaply.
        meta = _resolve_quote_metadata(args.quote_id)
        if meta is None:
            print(f"ERROR: Could not resolve quote {args.quote_id}.")
            print("Causes: quote doesn't exist, no source video URL, or "
                  "alignment hasn't run (word_timings is null).")
            return 1

        work_dir = (
            _PARSERS_DIR.parent / "media" / "_dry_run_proofs"
        )
        work_dir.mkdir(parents=True, exist_ok=True)
        clip_target = work_dir / f"dry_run_quote_{args.quote_id}.mp4"

        print(f"Quote   : {meta.quote_text[:120]}...")
        print(f"Speaker : {meta.speaker_name}")
        print(f"Topic   : {meta.topic_tag}")
        print(f"Meeting : {meta.meeting_title} ({meta.meeting_date})")
        print(f"Source  : {meta.source_video_url}")
        print(f"Clip    : {meta.clip_start_seconds:.1f}s — "
              f"{meta.clip_end_seconds:.1f}s "
              f"({meta.clip_end_seconds - meta.clip_start_seconds:.1f}s)")
        print()
        print("Extracting clip (dry run — no upload)...")
        clip_path = extract_clip(
            meta.source_video_url,
            meta.clip_start_seconds,
            meta.clip_end_seconds,
            clip_target,
        )
        size_mb = clip_path.stat().st_size / (1024 * 1024)
        sha = compute_sha256(clip_path)
        title = build_title(meta)
        description = build_description(
            meta, sha,
            timestamp_in_source=meta.clip_start_seconds + CLIP_LEAD_SECONDS,
        )
        print()
        print(f"Clip path : {clip_path}")
        print(f"Clip size : {size_mb:.2f} MB")
        print(f"SHA256    : {sha}")
        print()
        print(f"Would-upload title:")
        print(f"  {title}")
        print()
        print(f"Would-upload description (first 5 lines):")
        for line in description.split("\n")[:5]:
            print(f"  {line}")
        print(f"  ...")
        print()
        print("Dry run complete. Clip left at:")
        print(f"  {clip_path}")
        return 0

    # Real upload path.
    try:
        result = upload_proof_for_quote(
            args.quote_id,
            privacy_status=args.privacy,
            keep_clip=args.keep_clip,
        )
    except ProofsError as e:
        print(f"ERROR: {e}")
        return 1
    except Exception as e:
        print(f"UNEXPECTED ERROR: {e}")
        return 1

    print()
    print("=" * 64)
    print("  Upload complete")
    print("=" * 64)
    print(f"  Quote ID        : {result['quote_id']}")
    print(f"  YouTube URL     : {result['youtube_url']}")
    print(f"  Privacy         : {result['privacy_status']}")
    print(f"  Title           : {result['title']}")
    print(f"  Clip SHA256     : {result['clip_sha256']}")
    if result.get("clip_path"):
        print(f"  Local clip kept : {result['clip_path']}")
    print()
    print("The URL is now persisted in member_quotes.proof_clip_url.")
    print("Open it in your browser to spot-check the clip before")
    print("promoting it to public (or running batch uploads).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
