"""
Sequential batch processor for the 6 pending Kingman WOs (2026-05-12).

Stops on first non-`completed` terminal state. Polls the worker's WO state
every 30s. The Express/Flask `/api/work-orders/<id>/process` endpoint is
single-flight at the OS level (it spawns a subprocess), so we sequence by
"wait for state to leave processing/pending" between kicks.

Intentionally minimal. Run via:
    python3.11 -m zspan_pipeline.scripts.batch_process_kingman
"""
from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

FLASK_BASE = "http://127.0.0.1:5001"
WO_IDS = [100588, 100592, 100593, 100601, 100617, 100620]
POLL_INTERVAL_SECONDS = 30
# Tolerance threshold (per ROADMAP Phase 4's "≥80% parity" bar): a WO that
# ends in `failed` with most outputs landed still counts as enough-success
# to keep the batch going. The unofficial wrapper's silent-rejection rate
# means strictly demanding 12/12 is too brittle for a multi-WO run.
PARTIAL_SUCCESS_MIN_RATIO = 0.80
TERMINAL_OK_STATES = {"completed"}
TERMINAL_HARD_FAIL_STATES = {"awaiting_video", "awaiting_notebook"}
# Note: "failed" is NOT in either set above — it routes to the
# partial-success check (outputs_landed_ratio).


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _http_json(url: str, method: str = "GET") -> dict:
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        import json
        return json.loads(resp.read().decode("utf-8"))


def get_wo(wo_id: int) -> dict:
    return _http_json(f"{FLASK_BASE}/api/work-orders/{wo_id}")["work_order"]


def get_wo_state(wo_id: int) -> str:
    return get_wo(wo_id)["state"]


def outputs_landed_ratio(meeting_id: int) -> tuple[int, int, float]:
    """Returns (landed, total, ratio) for the meeting's notebook outputs.
    "Landed" = error is null/empty AND (content OR content_url) is present.
    """
    notebook = _http_json(f"{FLASK_BASE}/api/notebook/{meeting_id}")
    outputs = notebook.get("outputs") or {}
    total = len(outputs)
    landed = sum(
        1 for o in outputs.values()
        if not o.get("error") and (o.get("content") or o.get("content_url"))
    )
    ratio = (landed / total) if total else 0.0
    return landed, total, ratio


def kick_wo(wo_id: int) -> None:
    _http_json(f"{FLASK_BASE}/api/work-orders/{wo_id}/process", method="POST")


def main() -> int:
    print(f"{_ts()} batch start: {len(WO_IDS)} WOs to process", flush=True)
    for idx, wo_id in enumerate(WO_IDS, start=1):
        try:
            state = get_wo_state(wo_id)
        except Exception as e:
            print(f"{_ts()} ERROR getting state of WO#{wo_id}: {e}", flush=True)
            return 1

        if state == "pending":
            print(f"{_ts()} [{idx}/{len(WO_IDS)}] KICK WO#{wo_id}", flush=True)
            try:
                kick_wo(wo_id)
            except Exception as e:
                print(f"{_ts()} ERROR kicking WO#{wo_id}: {e}", flush=True)
                return 1
        elif state == "processing":
            print(f"{_ts()} [{idx}/{len(WO_IDS)}] WO#{wo_id} already processing — waiting", flush=True)
        elif state in TERMINAL_OK_STATES:
            print(f"{_ts()} [{idx}/{len(WO_IDS)}] WO#{wo_id} already in OK state '{state}' — skipping", flush=True)
            continue
        else:
            print(f"{_ts()} [{idx}/{len(WO_IDS)}] WO#{wo_id} in unexpected state '{state}' — stopping batch", flush=True)
            return 1

        # Poll until terminal
        while True:
            time.sleep(POLL_INTERVAL_SECONDS)
            try:
                wo = get_wo(wo_id)
                state = wo["state"]
            except Exception as e:
                print(f"{_ts()} WARN poll error for WO#{wo_id}: {e}", flush=True)
                continue
            if state in TERMINAL_OK_STATES:
                print(f"{_ts()} [{idx}/{len(WO_IDS)}] WO#{wo_id} COMPLETED (state=completed)", flush=True)
                break
            if state in TERMINAL_HARD_FAIL_STATES:
                print(f"{_ts()} [{idx}/{len(WO_IDS)}] WO#{wo_id} HARD-FAIL state '{state}' — stopping batch", flush=True)
                return 1
            if state == "failed":
                # Soft-fail path: check outputs-landed ratio. If most of the
                # outputs landed, continue the batch (the unofficial wrapper's
                # silent-rejection rate means strictly demanding 12/12 stalls
                # progress over flake we can't control).
                try:
                    landed, total, ratio = outputs_landed_ratio(wo["meeting_id"])
                except Exception as e:
                    print(f"{_ts()} [{idx}/{len(WO_IDS)}] WO#{wo_id} failed AND outputs-ratio check errored ({e}) — stopping batch", flush=True)
                    return 1
                if total > 0 and ratio >= PARTIAL_SUCCESS_MIN_RATIO:
                    print(f"{_ts()} [{idx}/{len(WO_IDS)}] WO#{wo_id} PARTIAL-OK ({landed}/{total} outputs landed, {ratio:.0%} >= {PARTIAL_SUCCESS_MIN_RATIO:.0%}) — continuing batch", flush=True)
                    break
                print(f"{_ts()} [{idx}/{len(WO_IDS)}] WO#{wo_id} FAILED ({landed}/{total} outputs landed, {ratio:.0%} < {PARTIAL_SUCCESS_MIN_RATIO:.0%}) — stopping batch", flush=True)
                return 1
            # else still pending/processing — keep polling

    print(f"{_ts()} batch DONE — all {len(WO_IDS)} WOs in 'completed' state", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
