"""CLI for the deterministic neutrality audit (S-133 v0.1 / D-144 layer).

Run from 02_Core_Project with the project's Python environment:

  python -m zspan_pipeline.scripts.audit_vote_frames --corpus-scan
  python -m zspan_pipeline.scripts.audit_vote_frames --meeting-id 103225 --stability 2
  python -m zspan_pipeline.scripts.audit_vote_frames --batch 103225,103224
  python -m zspan_pipeline.scripts.audit_vote_frames --all
  python -m zspan_pipeline.scripts.audit_vote_frames --rollup

--corpus-scan and --rollup are zero-LLM. --meeting-id/--batch/--all spend
two LLM calls per meeting (claude -p Sonnet + gpt-4o-mini), reusing saved
raw extractions on re-runs unless --no-reuse.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from zspan_pipeline.neutrality_audit import runner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--meeting-id", type=int, help="full audit loop on one meeting")
    parser.add_argument("--batch", type=str, help="comma-separated meeting ids, full loop each")
    parser.add_argument("--all", action="store_true",
                        help="full loop on every transcript-bearing meeting")
    parser.add_argument("--corpus-scan", action="store_true",
                        help="zero-LLM signature scan across the corpus")
    parser.add_argument("--rollup", action="store_true",
                        help="aggregate saved per-meeting reports (zero LLM)")
    parser.add_argument("--stability", type=int, default=1,
                        help="run family A this many times on the meeting (noise control)")
    parser.add_argument("--no-reuse", action="store_true",
                        help="re-extract even when a saved raw extraction exists")
    parser.add_argument("--pace", type=float, default=3.0,
                        help="seconds between LLM calls (default 3)")
    parser.add_argument("--timeout", type=float, default=420.0,
                        help="per-LLM-call timeout in seconds (vote-dense meetings need more)")
    parser.add_argument("--db", type=Path, default=runner.DEFAULT_DB_PATH)
    parser.add_argument("--out", type=Path, default=runner.DEFAULT_OUT_DIR)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    reuse = not args.no_reuse

    if args.corpus_scan:
        result = runner.corpus_scan(db_path=args.db, out_dir=args.out)
        print(f"corpus signature scan — {result['meetings']} meetings")
        print(f"{'meeting':>8} {'city':<18} {'words':>8} {'anchors':>7} "
              f"{'moments':>7}  {'kd':>2}  top signatures")
        for r in result["rows"]:
            top = sorted(r["signature_hits"].items(), key=lambda kv: -kv[1])[:3]
            top_s = " ".join(f"{k}×{v}" for k, v in top) or "—"
            print(f"{r['meeting_id']:>8} {r['city']:<18} {r['words']:>8,} "
                  f"{r['anchors']:>7} {r['vote_moments']:>7}  "
                  f"{'✓' if r['has_key_decisions'] else '·':>2}  {top_s}")
        if result["with_zero_moments"]:
            print(f"\n⚠ zero vote moments: {result['with_zero_moments']}")
        else:
            print("\n✓ every transcript has ≥1 vote moment")
        print(f"\nsaved: {args.out / 'corpus_signature_scan.json'}")
        return 0

    if args.rollup:
        result = runner.corpus_rollup(out_dir=args.out)
        print(json.dumps(result["totals"], indent=2))
        print(f"saved: {args.out / 'corpus_rollup.json'}")
        return 0

    ids: list[int] = []
    if args.meeting_id:
        ids = [args.meeting_id]
    elif args.batch:
        ids = [int(x) for x in args.batch.replace(" ", "").split(",") if x]
    elif args.all:
        ids = [m["id"] for m in runner.list_corpus_meetings(args.db)]
    if not ids:
        parser.print_help()
        return 2

    failures: list[tuple[int, str]] = []
    for n, mid in enumerate(ids):
        print(f"\n[{n + 1}/{len(ids)}] auditing m{mid} …", flush=True)
        t0 = time.time()
        try:
            report = runner.audit_meeting(
                mid, stability_runs=args.stability if mid == ids[0] else 1,
                reuse=reuse, pace=args.pace, llm_timeout=args.timeout,
                db_path=args.db, out_dir=args.out)
        except Exception as exc:  # keep the batch going; report at the end
            print(f"  ✗ m{mid} failed: {exc}")
            failures.append((mid, str(exc)))
            continue
        print(runner.render_meeting_summary(report))
        print(f"  ({round(time.time() - t0, 1)}s)")

    if len(ids) > 1:
        print("\n=== rollup ===")
        result = runner.corpus_rollup(out_dir=args.out)
        print(json.dumps(result["totals"], indent=2))
    if failures:
        print(f"\n✗ {len(failures)} meeting(s) failed: {failures}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
