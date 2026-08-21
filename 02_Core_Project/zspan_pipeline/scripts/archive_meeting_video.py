#!/usr/bin/env python3
"""Archive a meeting's source video to the local mirror (D-155, PLAYER-2).

The archival mirror is Z-SPAN's shield against embed-disabled and
source-deleted recordings: an opt-in, per-meeting local copy fetched
while the source is still alive. It is NOT a playback path — routine
playback stays zero-egress via the client-side player adapters, and
rescue SERVING is deliberately unbuilt at V1 (D-155 § 5: offline-first,
zero cloud billing; a rescue activation is a per-incident operator
decision).

Storage roles (D-155 § 4): the local archive directory is the MASTER
(`zspan_video_archive_dir` user-setting; default ~/zspan-video-archive/),
RED DRIVE snapshots cover durability, and any cloud copy is a cold
off-site COPY only. The archive lives outside every repo.

Verification claims (D-155 § 6 / D-146): a YouTube-sourced copy cannot
byte-hash-verify against what the city published (YouTube re-encodes) —
its sha256 attests OUR copy's chain of custody. A vendor-direct MP4 is
the byte-exact published file — its sha256 verifies externally. The
`source_kind` column records which claim each row carries.

Usage:
    # One meeting, now:
    python -m zspan_pipeline.scripts.archive_meeting_video --meeting-id 103225

    # Flag for archival (the opt-in bit) without fetching yet:
    python -m zspan_pipeline.scripts.archive_meeting_video --set-flag 103225

    # Fetch everything flagged but not yet archived (defrag-paced):
    python -m zspan_pipeline.scripts.archive_meeting_video --flagged

    # Archive the published showcase set (sources still alive = the
    # practical first cohort per D-155 § 3):
    python -m zspan_pipeline.scripts.archive_meeting_video --backfill-published

    # Inventory:
    python -m zspan_pipeline.scripts.archive_meeting_video --list

All fetch modes accept --dry-run (resolve + report, no download).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Windows cp1252 stdout chokes on em-dashes in help strings; mirror the
# sibling scripts' UTF-8 reconfigure so console invocation never crashes.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

# Make parsers/ importable (sibling-script bootstrap pattern).
_PARSERS_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "council_navigator"
    / "parsers"
)
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

from database import get_connection  # noqa: E402
from env_config import load_user_settings  # noqa: E402

logger = logging.getLogger(__name__)

YT_DLP_BIN = os.environ.get("YT_DLP_BIN", "yt-dlp")
# Video downloads run long on multi-hour council meetings; 2h ceiling
# (the transcriber's 30 min is audio-only).
DOWNLOAD_TIMEOUT_SECONDS = int(os.environ.get("ZSPAN_ARCHIVE_TIMEOUT", str(2 * 3600)))
# Refuse to start a download when the archive volume has less than this
# much headroom — a full disk mid-write corrupts nothing but wastes an
# hour and leaves a partial to clean up.
MIN_FREE_BYTES = 10 * 1024**3


def archive_root() -> Path:
    """Resolve the archive master directory (D-155 § 4).

    Order: ZSPAN_VIDEO_ARCHIVE_DIR env → `zspan_video_archive_dir` user
    setting → ~/zspan-video-archive. Created on first use; lives outside
    every repo by design (operator-side data, tooling-public split per
    D-155 § 8).
    """
    env = os.environ.get("ZSPAN_VIDEO_ARCHIVE_DIR")
    if env:
        return Path(env).expanduser()
    setting = load_user_settings().get("zspan_video_archive_dir")
    if setting:
        return Path(setting).expanduser()
    return Path.home() / "zspan-video-archive"


def classify_source(url: str) -> str:
    """Mirror of the client-side classifier's taxonomy (videoSource.ts),
    collapsed to the three archive-relevant classes."""
    lowered = url.lower()
    if "youtube.com" in lowered or "youtu.be" in lowered:
        return "youtube"
    if lowered.split("?")[0].endswith(".mp4"):
        return "direct_mp4"
    return "vendor_page"


def resolve_video_url(cursor, meeting_id: int) -> str | None:
    """work_orders.youtube_video_url (latest WO) preferred,
    meetings.video_url fallback — the fetcher.py resolution order."""
    row = cursor.execute(
        "SELECT youtube_video_url FROM work_orders "
        "WHERE meeting_id = ? AND youtube_video_url IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (meeting_id,),
    ).fetchone()
    if row and row[0]:
        return row[0]
    row = cursor.execute(
        "SELECT video_url FROM meetings WHERE id = ?", (meeting_id,)
    ).fetchone()
    return row[0] if row and row[0] else None


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def upsert_archive_row(cursor, meeting_id: int, **fields) -> None:
    """Idempotent per-meeting upsert that NEVER clobbers rescue state.

    Re-fetching a meeting updates the provenance columns but preserves
    rescue_reason / rescue_activated_at / serving_url — those belong to
    the operator's rescue decision, not the fetch.
    """
    existing = cursor.execute(
        "SELECT id FROM meeting_media_archive WHERE meeting_id = ?",
        (meeting_id,),
    ).fetchone()
    if existing:
        sets = ", ".join(f"{k} = ?" for k in fields)
        cursor.execute(
            f"UPDATE meeting_media_archive SET {sets}, "
            "fetched_at = CURRENT_TIMESTAMP WHERE meeting_id = ?",
            (*fields.values(), meeting_id),
        )
    else:
        cols = ", ".join(["meeting_id", *fields.keys()])
        marks = ", ".join(["?"] * (len(fields) + 1))
        cursor.execute(
            f"INSERT INTO meeting_media_archive ({cols}) VALUES ({marks})",
            (meeting_id, *fields.values()),
        )


def fetch_one(meeting_id: int, *, max_height: int, dry_run: bool) -> str:
    """Archive one meeting. Returns the F8-honest status: ok | empty | error.

    `empty` = no resolvable source URL (recorded, so the inventory says
    "tried, nothing to fetch" instead of silently omitting the meeting).
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        url = resolve_video_url(cursor, meeting_id)
        if not url:
            print(f"m{meeting_id}: no resolvable video URL (honest-empty)")
            upsert_archive_row(
                cursor, meeting_id,
                source_url="", source_kind="none", status="empty",
                error="no resolvable video URL at fetch time",
            )
            conn.commit()
            return "empty"

        kind = classify_source(url)
        root = archive_root()
        dest_dir = root / str(meeting_id)
        dest_rel = f"{meeting_id}/source.mp4"
        dest = root / dest_rel

        if dest.exists():
            existing = cursor.execute(
                "SELECT status FROM meeting_media_archive WHERE meeting_id = ?",
                (meeting_id,),
            ).fetchone()
            if existing and existing[0] == "ok":
                print(f"m{meeting_id}: already archived ({dest}) — skipping")
                return "ok"

        if dry_run:
            print(f"m{meeting_id}: WOULD fetch [{kind}] {url} → {dest}")
            return "ok"

        root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(root).free
        if free < MIN_FREE_BYTES:
            msg = f"archive volume has {free / 1024**3:.1f} GB free (< 10 GB floor)"
            print(f"m{meeting_id}: REFUSED — {msg}")
            upsert_archive_row(
                cursor, meeting_id,
                source_url=url, source_kind=kind, status="error", error=msg,
            )
            conn.commit()
            return "error"
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Proven flag set from mac_transcriber (V1-Repair-1): retries +
        # linear backoff for YouTube's segmented downloads; explicit JS
        # runtime required by yt-dlp 2026.06.09+ for YouTube extraction.
        js_runtimes = os.environ.get("YT_DLP_JS_RUNTIMES", "node:/usr/local/bin/node")
        fmt = (
            f"bestvideo[height<={max_height}]+bestaudio"
            f"/best[height<={max_height}]/best"
        )
        cmd = [
            YT_DLP_BIN,
            "-f", fmt,
            "--merge-output-format", "mp4",
            "--retries", "10",
            "--fragment-retries", "10",
            "--retry-sleep", "linear=1:60",
            "--js-runtimes", js_runtimes,
            "--print-to-file", "%(format)s", str(dest_dir / "format.txt"),
            "-o", str(dest_dir / "source.%(ext)s"),
            url,
        ]
        print(f"m{meeting_id}: fetching [{kind}] {url} (≤{max_height}p) …")
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=DOWNLOAD_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            upsert_archive_row(
                cursor, meeting_id,
                source_url=url, source_kind=kind, status="error",
                error=f"yt-dlp timed out after {DOWNLOAD_TIMEOUT_SECONDS}s",
            )
            conn.commit()
            print(f"m{meeting_id}: TIMEOUT")
            return "error"

        if r.returncode != 0 or not dest.exists():
            err = (r.stderr or "")[-2000:] or "yt-dlp produced no source.mp4"
            upsert_archive_row(
                cursor, meeting_id,
                source_url=url, source_kind=kind, status="error", error=err,
            )
            conn.commit()
            print(f"m{meeting_id}: FAILED — {err[:200]}")
            return "error"

        digest = sha256_of(dest)
        size = dest.stat().st_size
        fmt_file = dest_dir / "format.txt"
        fmt_desc = fmt_file.read_text().strip()[:200] if fmt_file.exists() else None
        upsert_archive_row(
            cursor, meeting_id,
            source_url=url, source_kind=kind, sha256=digest, bytes=size,
            resolution=f"{max_height}p", format=fmt_desc,
            archive_path_rel=dest_rel, status="ok", error=None,
        )
        conn.commit()
        print(
            f"m{meeting_id}: archived {size / 1024**2:.0f} MB "
            f"sha256={digest[:16]}… → {dest}"
        )
        return "ok"
    finally:
        conn.close()


def set_flag(meeting_id: int, value: int) -> None:
    conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE meetings SET archive_video = ? WHERE id = ?",
            (value, meeting_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            print(f"m{meeting_id}: no such meeting")
        else:
            print(f"m{meeting_id}: archive_video = {value}")
    finally:
        conn.close()


def batch_ids(mode: str) -> list[int]:
    """Meeting ids for --flagged / --backfill-published, skipping rows
    already archived ok."""
    conn = get_connection()
    try:
        if mode == "flagged":
            where = "m.archive_video = 1"
        else:  # backfill-published
            where = "m.is_published = 1"
        rows = conn.execute(
            f"""
            SELECT m.id FROM meetings m
            LEFT JOIN meeting_media_archive a
                   ON a.meeting_id = m.id AND a.status = 'ok'
            WHERE {where} AND a.id IS NULL
            ORDER BY m.meeting_date DESC
            """
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def list_inventory() -> None:
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT a.meeting_id, m.city_name, m.meeting_date, a.source_kind,
                   a.status, a.bytes, a.rescue_reason
            FROM meeting_media_archive a
            JOIN meetings m ON m.id = a.meeting_id
            ORDER BY a.fetched_at DESC
            """
        ).fetchall()
        if not rows:
            print("archive ledger is empty")
            return
        total = 0
        for mid, city, date, kind, status, size, rescue in rows:
            size_mb = (size or 0) / 1024**2
            total += size or 0
            rescue_s = f" RESCUE:{rescue}" if rescue else ""
            print(
                f"m{mid}  {city} {date}  [{kind}] {status}"
                f"  {size_mb:,.0f} MB{rescue_s}"
            )
        print(f"— {len(rows)} rows, {total / 1024**3:.1f} GB total")
    finally:
        conn.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--meeting-id", type=int, help="archive one meeting now")
    g.add_argument("--set-flag", type=int, metavar="MEETING_ID", help="mark a meeting archive_video=1 (opt-in) without fetching")
    g.add_argument("--clear-flag", type=int, metavar="MEETING_ID", help="clear a meeting's archive_video flag")
    g.add_argument("--flagged", action="store_true", help="fetch all flagged, not-yet-archived meetings")
    g.add_argument("--backfill-published", action="store_true", help="fetch all published, not-yet-archived meetings")
    g.add_argument("--list", action="store_true", help="print the archive ledger")
    p.add_argument("--max-height", type=int, default=720, help="resolution cap (default 720)")
    p.add_argument("--dry-run", action="store_true", help="resolve + report, no downloads")
    p.add_argument("--pace-seconds", type=int, default=60, help="sleep between batch downloads (defrag pacing, default 60)")
    args = p.parse_args()

    if args.list:
        list_inventory()
        return 0
    if args.set_flag is not None:
        set_flag(args.set_flag, 1)
        return 0
    if args.clear_flag is not None:
        set_flag(args.clear_flag, 0)
        return 0
    if args.meeting_id is not None:
        status = fetch_one(args.meeting_id, max_height=args.max_height, dry_run=args.dry_run)
        return 0 if status in ("ok", "empty") else 1

    ids = batch_ids("flagged" if args.flagged else "backfill-published")
    if not ids:
        print("nothing to fetch (all candidates archived or none match)")
        return 0
    print(f"{len(ids)} meeting(s) to archive: {ids}")
    results = {"ok": 0, "empty": 0, "error": 0}
    for i, mid in enumerate(ids):
        results[fetch_one(mid, max_height=args.max_height, dry_run=args.dry_run)] += 1
        if not args.dry_run and i < len(ids) - 1:
            time.sleep(args.pace_seconds)  # defrag pacing — one at a time, gently
    print(json.dumps(results))
    return 0 if results["error"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
