"""
local_fs — operator-controlled local filesystem actions.

Single-user dev convenience: when the operator is reviewing a meeting's
T-013 clip batches, they want to jump straight to the clips folder
without hunting through the file tree. This module resolves the meeting's
review_queue folder (or a specific batch_NN subfolder) and opens it in
the OS file explorer.

SECURITY: paths are validated to live under the canonical
`media/review_queue/` directory before any subprocess fires, so a
hostile or buggy client can't misuse the endpoint to open
arbitrary system paths. Path resolution is structured (meeting_id +
optional batch_index) — no client-supplied raw paths.

Future: this whole module gets retired when volunteer reviewers come
online via OAuth (T-018 scope). The Windows-Explorer affordance is
intentionally local-dev-only.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# `media/review_queue/` lives at .../council_navigator/media/review_queue.
# This module is in parsers/, so the root is parent's `media` subdir.
def _review_queue_root() -> Path:
    return Path(__file__).resolve().parent.parent / "media" / "review_queue"


def find_meeting_folder(meeting_id: int) -> Optional[Path]:
    """Locate the review_queue folder for a meeting by walking the
    BATCH_MANIFEST.json files. Matches the same pattern as
    `zspan_pipeline.scripts.ingest_review_response._find_response_files_for_meeting`
    so the two paths can't diverge.

    Returns None when no manifest references this meeting_id (i.e., the
    review queue hasn't been built yet — operator should run
    `build_review_queue --meeting-id N` first).
    """
    root = _review_queue_root()
    if not root.exists():
        return None
    for manifest_path in root.rglob("BATCH_MANIFEST.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if manifest.get("meeting_id") == meeting_id:
            return manifest_path.parent
    return None


def resolve_target_path(
    meeting_id: int, batch_index: Optional[int] = None
) -> Optional[Path]:
    """Resolve `{meeting_id, batch_index?}` to a folder under the
    review_queue tree.

    - `batch_index=None` → the meeting folder (containing the manifest,
      REVIEW_GUIDE.md, source.mp4, and per-batch subfolders).
    - `batch_index=N` → the `batch_NN/` subfolder if it exists.

    Returns None when the folder doesn't exist (meeting not built, or
    batch number out of range). The caller decides whether to 404 or
    fall back to the meeting folder.
    """
    meeting_dir = find_meeting_folder(meeting_id)
    if meeting_dir is None:
        return None
    if batch_index is None:
        return meeting_dir
    # build_review_queue.py names batches `batch_01`, `batch_02`, ...
    # — two-digit zero-pad. Mirror that convention here.
    batch_dir = meeting_dir / f"batch_{batch_index:02d}"
    if not batch_dir.exists():
        return None
    return batch_dir


def is_safe_path(path: Path) -> bool:
    """Validate that `path` (after .resolve() symlink expansion) is
    inside the canonical `media/review_queue/` tree. Returns False for
    anything outside — including via symlinks, `..` traversal, or
    absolute-path injection."""
    try:
        resolved = path.resolve(strict=True)
        root = _review_queue_root().resolve()
    except (OSError, FileNotFoundError):
        return False
    try:
        resolved.relative_to(root)
        return True
    except ValueError:
        return False


def source_cache_size_bytes() -> dict:
    """Walk the review_queue tree and sum the size of every `source.mp4`
    file. Returns `{total_bytes, file_count}`. Used by the operator
    terminal to render a disk-usage badge ("~180 MB across 4 meetings").

    Other artifacts (per-batch clips, sidecar JSONs, PROMPT.md/RESPONSE.md)
    are NOT counted — they're small in aggregate and load-bearing for
    re-review or audit. Only the bulky source caches get totaled.
    """
    root = _review_queue_root()
    if not root.exists():
        return {"total_bytes": 0, "file_count": 0}
    total = 0
    n = 0
    for p in root.rglob("source.mp4"):
        try:
            total += p.stat().st_size
            n += 1
        except OSError:
            continue
    return {"total_bytes": total, "file_count": n}


def delete_meeting_source_cache(meeting_id: int) -> dict:
    """Delete the `source.mp4` cache for one meeting. Returns
    `{deleted, path, bytes_freed, existed}`. Idempotent — calling on a
    meeting whose source.mp4 was already removed returns existed=False
    and bytes_freed=0.

    Preserves the per-batch clips + manifest + RESPONSE.md files — only
    the source mp4 (the bulky one that regenerates on next [BUILD]) is
    removed. The clips stay because they're already-paid-for review
    evidence and need to remain accessible for re-review or audit.
    """
    meeting_dir = find_meeting_folder(meeting_id)
    if meeting_dir is None:
        return {
            "deleted": False, "path": None, "bytes_freed": 0,
            "existed": False, "reason": "no review queue for this meeting",
        }
    source_path = meeting_dir / "source.mp4"
    if not source_path.exists():
        return {
            "deleted": False, "path": str(source_path), "bytes_freed": 0,
            "existed": False, "reason": "source.mp4 already cleared",
        }
    if not is_safe_path(source_path):
        # Defensive — shouldn't happen because find_meeting_folder
        # returned a manifest-anchored path inside the tree, but a
        # symlink could in theory point outside. Refuse to delete.
        return {
            "deleted": False, "path": str(source_path), "bytes_freed": 0,
            "existed": True, "reason": "path failed safety check",
        }
    try:
        size = source_path.stat().st_size
        source_path.unlink()
        logger.info(
            "deleted source.mp4 for meeting %s (%d bytes)", meeting_id, size,
        )
        return {
            "deleted": True, "path": str(source_path),
            "bytes_freed": size, "existed": True,
        }
    except OSError as e:
        return {
            "deleted": False, "path": str(source_path), "bytes_freed": 0,
            "existed": True, "reason": f"OS error: {e}",
        }


def open_in_file_explorer(path: Path) -> None:
    """Open `path` in the OS file explorer. Blocking only on the
    subprocess spawn (the explorer window itself runs independently).

    Caller MUST validate `path` via `is_safe_path()` first — this
    function does NOT validate (separation of concerns: validation is
    policy, this is mechanism).
    """
    p = str(path)
    system = platform.system()
    if system == "Windows":
        # `explorer.exe` returns non-zero exit codes even on success
        # (e.g., 1 when it spawned a new window vs. focused an existing
        # one), which is why we don't `check=True`. Popen-and-forget is
        # the right semantic — we don't want to block on the GUI.
        # Use `os.startfile` instead of subprocess because it's the
        # Windows-blessed way and handles UNC paths, spaces, etc.
        os.startfile(p)  # noqa: S606 — only called with validated paths
    elif system == "Darwin":
        subprocess.Popen(["open", p])
    else:
        # Assume freedesktop-compatible (Linux). xdg-open is the standard.
        subprocess.Popen(["xdg-open", p])
    logger.info("opened in file explorer: %s", p)
