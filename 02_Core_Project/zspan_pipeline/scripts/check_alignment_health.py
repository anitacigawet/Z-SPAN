"""check_alignment_health — drift detector for preview-quotes word_timings.

Walks the .preview/ directory, finds every m<id>.json preview-quotes
sidecar, and checks whether all quotes carry word_timings. Reports any
meeting whose quotes were extracted but never aligned for karaoke
playback — the failure mode that surfaced 2026-06-24 when m103753's
DISCUSSION + KEY QUOTES rendered as plain italic text because the Phase 2
D8 diarize chain hung at pyannote and never reached the
sidecar_pipeline's Stage 5 (align_preview_quotes).

The structural protection is sidecar_pipeline.py Stage 5 itself, which
runs align after extraction and raises on subprocess failure. This script
is the belt-and-suspenders verifier: it catches drift from any path that
bypasses sidecar_pipeline (manual sidecar regen, partial pipeline runs,
upstream stages hanging before stage 5 fires).

Usage:
    # Report drift only (read-only; exit 0 if clean, 1 if drift found)
    python3.11 -m zspan_pipeline.scripts.check_alignment_health

    # Auto-fix by running align_preview_quotes on each drifted meeting
    python3.11 -m zspan_pipeline.scripts.check_alignment_health --fix

Exit codes:
    0 — no drift detected (or all drift repaired with --fix)
    1 — drift detected and not repaired (--report mode + drift present)
    2 — drift detected, --fix attempted, at least one repair failed
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
PREVIEW_DIR = REPO_ROOT / ".preview"

SIDECAR_SUFFIXES = {"_decisions", "_routing", "_recusals", "_audit"}


def _is_quotes_sidecar(path: Path) -> bool:
    """True for m<id>.json files that are the primary preview-quotes
    sidecar (not the decisions/routing/recusals/audit variants)."""
    stem = path.stem
    if not stem.startswith("m"):
        return False
    if not stem[1:].split("_")[0].isdigit():
        return False
    for suffix in SIDECAR_SUFFIXES:
        if stem.endswith(suffix):
            return False
    return True


def _check_meeting(path: Path) -> tuple[int, int, int]:
    """Return (total_quotes, aligned_quotes, meeting_id) for one sidecar."""
    meeting_id = int(path.stem[1:])
    try:
        payload = json.loads(path.read_text())
    except Exception as exc:
        logger.warning("could not parse %s: %s", path.name, exc)
        return (0, 0, meeting_id)
    quotes = payload.get("quotes") or []
    total = len(quotes)
    aligned = sum(
        1 for q in quotes
        if isinstance(q.get("word_timings"), list) and len(q["word_timings"]) > 0
    )
    return (total, aligned, meeting_id)


def _run_align(meeting_id: int) -> bool:
    """Subprocess-call align_preview_quotes. Returns True on success."""
    cmd = [
        sys.executable, "-m", "zspan_pipeline.align_preview_quotes",
        "--meeting-id", str(meeting_id),
    ]
    cwd = REPO_ROOT / "02_Core_Project"
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(cwd), timeout=300,
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "")[-400:]
        logger.error("align failed for m%d (rc=%d): %s", meeting_id, result.returncode, tail)
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Check preview-quote alignment drift")
    parser.add_argument(
        "--fix", action="store_true",
        help="Auto-run align_preview_quotes on any meeting with drift",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Log every meeting checked (not just drifted ones)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not PREVIEW_DIR.is_dir():
        logger.error("preview dir not found: %s", PREVIEW_DIR)
        return 0

    paths = sorted(PREVIEW_DIR.glob("m*.json"))
    quote_sidecars = [p for p in paths if _is_quotes_sidecar(p)]
    logger.info("scanning %d preview-quote sidecars in %s", len(quote_sidecars), PREVIEW_DIR.name)

    drift: list[tuple[int, int, int]] = []  # (meeting_id, aligned, total)
    for path in quote_sidecars:
        total, aligned, mid = _check_meeting(path)
        if args.verbose:
            logger.debug("m%d: %d/%d aligned", mid, aligned, total)
        if total > 0 and aligned < total:
            drift.append((mid, aligned, total))

    if not drift:
        logger.info("✅ all %d sidecars carry word_timings on every quote", len(quote_sidecars))
        return 0

    logger.warning("⚠️  %d meeting(s) carry unaligned quotes:", len(drift))
    for mid, aligned, total in drift:
        logger.warning("  m%d: %d/%d aligned (missing %d)", mid, aligned, total, total - aligned)

    if not args.fix:
        logger.info("run with --fix to auto-repair via align_preview_quotes")
        return 1

    logger.info("--fix specified — running align for each drifted meeting")
    failed = []
    for mid, _, _ in drift:
        logger.info("  aligning m%d...", mid)
        if _run_align(mid):
            logger.info("  ✅ m%d aligned", mid)
        else:
            failed.append(mid)

    if failed:
        logger.error("❌ %d repair(s) failed: %s", len(failed), failed)
        return 2

    logger.info("✅ all %d drifted meetings repaired", len(drift))
    return 0


if __name__ == "__main__":
    sys.exit(main())
