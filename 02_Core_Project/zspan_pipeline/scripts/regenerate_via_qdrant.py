"""
V1-RAG-3 maintenance one-shot — regenerate a single text output for a
single meeting from the complete indexed transcript.

This script is the retained operator CLI descended from the original
V1-RAG-3 proof-of-concept. ``fetcher.py`` + ``worker.py`` are now the
canonical production path; this one-shot remains useful for deliberately
regenerating one cached output without running a whole work order.

CLI usage:

    # Regenerate key_decisions for Bullhead 5/19 (m103225)
    .venv-worker/bin/python \
        -m zspan_pipeline.scripts.regenerate_via_qdrant \
        --meeting-id 103225 --output key_decisions

    # Dry-run — synthesize but DON'T write to the cache
    .venv-worker/bin/python \
        -m zspan_pipeline.scripts.regenerate_via_qdrant \
        --meeting-id 103225 --output key_decisions --dry-run

Every whole-meeting output receives all hash-verified indexed chunks in
chronological order. Query-shaped Librarian/search paths remain retrieval-based.

Composes V1-RAG-1 + V1-RAG-2 + the qdrant_synthesizer module +
[D-126](../../../01_Project_Overview/DECISIONS.md#d-126) +
[S-033](../../../01_Project_Overview/FUTURE_THOUGHTS.md#s-033).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Make zspan_pipeline importable when this script is run directly.
_THIS_DIR = Path(__file__).resolve().parent
_BRIDGE_DIR = _THIS_DIR.parent
_CORE_PROJECT_DIR = _BRIDGE_DIR.parent
if str(_CORE_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_PROJECT_DIR))

# Make parsers importable for database access.
_PARSERS_DIR = (
    _CORE_PROJECT_DIR / "council_navigator" / "parsers"
)
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

from zspan_pipeline import qdrant_synthesizer  # noqa: E402
from database import (  # noqa: E402
    apply_city_corrections,
    get_connection,
    save_notebook_output,
)

logger = logging.getLogger(__name__)


def load_meeting_metadata(meeting_id: int) -> tuple[str, str]:
    """Read legacy provenance and city metadata from the local database.

    The one-shot already writes ``notebook_outputs`` through the direct
    database layer. Reading the same database through the owner-gated Flask
    presentation endpoint adds an unnecessary availability and authentication
    dependency, so keep this maintenance path local end-to-end.
    """
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT notebook_id, city_name FROM meetings WHERE id = ?",
            (meeting_id,),
        ).fetchone()
    finally:
        connection.close()

    if row is None:
        raise ValueError(f"No meeting found with id={meeting_id}")

    meeting = dict(row)
    notebook_id = str(meeting.get("notebook_id") or "")
    if not notebook_id:
        logger.warning(
            "Meeting %d has no existing notebook_id — saving with empty value. "
            "This is fine for meetings never processed by the retired bridge.",
            meeting_id,
        )
    city_name = str(meeting.get("city_name") or "").strip()
    return notebook_id, city_name


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "V1-RAG-3 maintenance CLI — regenerate one text output for one "
            "meeting from the complete indexed transcript."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--meeting-id",
        type=int,
        required=True,
        help="Meeting ID to regenerate (e.g., 103225 for Bullhead 5/19).",
    )
    parser.add_argument(
        "--output",
        required=True,
        choices=sorted(qdrant_synthesizer.WHOLE_MEETING_OUTPUT_TYPES),
        help="Which output type to regenerate.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the synthesized content but do NOT write to the cache.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="DEBUG-level logging.",
    )

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )

    output_type = args.output
    meeting_id = args.meeting_id

    logger.info("─" * 60)
    logger.info(
        "V1-RAG-3 regenerate: meeting=%d output=%s evidence=complete dry_run=%s",
        meeting_id, output_type, args.dry_run,
    )

    notebook_id, city_name = load_meeting_metadata(meeting_id)

    try:
        result = qdrant_synthesizer.synthesize_output(
            meeting_id=meeting_id,
            output_type=output_type,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Synthesis failed")
        return 1

    logger.info(
        "Synthesis complete: %d chars content, %d complete chunks, model=%s",
        len(result.content), len(result.chunks), result.model_id,
    )

    # Post-synthesis city corrections — mirrors the discipline in
    # fetcher.py:1597 (qdrant_synthesize strategy). The standalone CLI
    # was missing this hook (V1-Repair-1 audit 2026-06-22 caught the
    # divergence on m103753 — Sonnet rendered "Antivine Avenue" but the
    # city_vocabulary_corrections substitution never ran). City metadata was
    # loaded with notebook_id before synthesis, from the same local DB row.
    if city_name:
        corrected, log = apply_city_corrections(city_name, result.content)
        applied = [e for e in (log or []) if e.get("count", 0) > 0]
        if applied:
            logger.info(
                "city corrections applied: %s",
                ", ".join(f"{e['from']!r}->{e['to']!r}(x{e['count']})" for e in applied),
            )
        result_content = corrected
    else:
        result_content = result.content

    logger.info("─" * 60)
    logger.info("Synthesized content (post-corrections):")
    print(result_content)
    logger.info("─" * 60)

    if args.dry_run:
        logger.info("DRY RUN: not writing to notebook_outputs cache.")
        return 0

    save_notebook_output(
        meeting_id=meeting_id,
        notebook_id=notebook_id,
        output_type=output_type,
        content=result_content,
        prompt_filename=result.prompt_filename,
        prompt_version=f"v1-rag-3-{result.model_id}",
    )
    logger.info(
        "Cache updated for meeting=%d output=%s; reload the broadcast page "
        "to see the new content.",
        meeting_id, output_type,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
