"""Manual/background backfill runner for diarization and optional sidecars.

Usage:
    python -m zspan_pipeline.scripts.run_diarize_for_meeting --meeting-id 103753 --city Kingman

This is the canonical backfill entry point after synchronous worker diarization
was made default-off. Use ``--skip-sidecar`` to fill only diarization. Use
``--skip-diarize`` to resume sidecar processing; completed sidecar stages are
detected from their artifacts and skipped.
"""
from __future__ import annotations

import argparse
import logging
import sys

from zspan_pipeline import diarize_orchestrator, sidecar_pipeline

# diarize_orchestrator adds parsers/ to sys.path before this import.
from database import update_meeting_diarization_status  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run diarize_orchestrator + sidecar_pipeline for one meeting.",
    )
    parser.add_argument("--meeting-id", type=int, required=True)
    parser.add_argument(
        "--city", type=str, required=True,
        help="City name (e.g., 'Kingman'). Used for symbols block + roster.",
    )
    parser.add_argument("--skip-sidecar", action="store_true")
    parser.add_argument(
        "--skip-diarize",
        action="store_true",
        help="Resume/run sidecars only; completed stage artifacts are skipped.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not args.skip_diarize:
        print(f"=== diarize_orchestrator: meeting={args.meeting_id} ===")
        update_meeting_diarization_status(
            args.meeting_id, "running", "manual diarization backfill",
        )
        try:
            summary = diarize_orchestrator.run_full_diarize_step(
                args.meeting_id, args.city,
            )
            status, detail = diarize_orchestrator.classify_diarization_summary(summary)
        except Exception as exc:
            update_meeting_diarization_status(args.meeting_id, "failed", str(exc))
            raise
        update_meeting_diarization_status(args.meeting_id, status, detail)
        print(f"\ndiarize_orchestrator summary:")
        for k, v in summary.items():
            print(f"  {k}: {v}")
        print(f"  durable_status: {status}")

    if not args.skip_sidecar:
        print(f"\n=== sidecar_pipeline: meeting={args.meeting_id} ===")
        result = sidecar_pipeline.run_pipeline(args.meeting_id, args.city)
        print(f"\nsidecar_pipeline result:")
        for k, v in result.items():
            print(f"  {k}: {v}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
