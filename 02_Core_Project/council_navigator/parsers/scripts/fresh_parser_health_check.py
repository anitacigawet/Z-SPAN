#!/usr/bin/env python3.11
"""Fresh parser-health probe — GROUND TRUTH, subprocess-isolated.

Runs each city's parser via `parser_loader.scrape_city_calendar` (the same entry
point Flask's `/scrape/<city>` uses), **each in an isolated subprocess with a
hard timeout + process-group kill**, so a parser that hangs in a non-Python call
(e.g. a Playwright/chromium subprocess) is killed by the OS instead of hanging
the whole sweep.

**This is the ground-truth parser-health tool + the mandatory FIRST GATE for any
freshness classification.** Any "this parser is broken/stale" label from a
marker-probe, a vendor-API freshness sweep, or a stale `parser_index.json` flag
is a HYPOTHESIS until this tool runs the actual parser and observes its real
output. See `council_navigator/CLAUDE.md` + memory `ground-truth-run-beats-marker-classification`.
(The 2026-07-01 S-105 marker-probe skipped this and produced 11 false positives —
parsers returning real meetings, flagged "broken" by a regex over the HTML page.)

Why subprocess-isolated (2026-07-08 upgrade): the prior `signal.alarm` approach
could not interrupt a parser blocked in a C extension or a spawned subprocess, so
the full-fleet sweep hung at the first Playwright parser (Chino Valley). Isolation
+ `os.killpg` makes the full sweep actually complete. The `test_one_parser(city)`
signature + the report JSON shape are unchanged (drop-in).

Usage:
    fresh_parser_health_check.py                      # all registered cities
    fresh_parser_health_check.py --state az           # Arizona only
    fresh_parser_health_check.py --cities "Coolidge,Globe,Sedona"
    fresh_parser_health_check.py --timeout 25         # per-parser hard cap (default 20s)
    # (internal) --worker "<City>"  runs ONE parser in-process, prints one marked JSON line
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

_HERE = Path(__file__).resolve().parent
_PARSERS_DIR = _HERE.parent
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

PER_PARSER_TIMEOUT_SEC = 20
HEALTH_REPORTS_DIR = _PARSERS_DIR
_RESULT_MARKER = "###PARSER_HEALTH_RESULT###"

# AZ county set for the --state az filter (parser_index county field carries "<Name> County").
_AZ_COUNTIES = {
    "Mohave", "Maricopa", "Pinal", "Pima", "Coconino", "Navajo", "Yavapai",
    "Cochise", "Gila", "Graham", "Greenlee", "Apache", "La Paz", "Santa Cruz", "Yuma",
}


def _is_az(info: dict) -> bool:
    county = (info.get("county", "") or "").replace(" County", "").strip()
    state = (info.get("state", "") or "").lower()
    if state and state not in ("az", "arizona"):
        return False
    return county in _AZ_COUNTIES


# --------------------------------------------------------------------------- #
# Worker: runs ONE parser in-process, prints exactly one marked JSON line.
# Invoked as a subprocess by test_one_parser so a hang can be killed by the OS.
# --------------------------------------------------------------------------- #
def _worker_run(city: str) -> Dict:
    from parser_loader import scrape_city_calendar  # imported here so import errors are caught per-city
    start = time.monotonic()
    try:
        meetings = scrape_city_calendar(city)
        duration = round(time.monotonic() - start, 2)
        count = len(meetings) if isinstance(meetings, list) else 0
        if count > 0:
            return {"status": "healthy", "meeting_count": count, "error": None, "duration_sec": duration}
        return {"status": "empty", "meeting_count": 0, "error": "scrape returned 0 meetings", "duration_sec": duration}
    except Exception as e:  # noqa: BLE001 — the whole point is to capture ANY parser failure as data
        return {
            "status": "error",
            "meeting_count": 0,
            "error": f"{type(e).__name__}: {str(e)[:200]}",
            "duration_sec": round(time.monotonic() - start, 2),
        }


def _worker_main(city: str) -> None:
    result = _worker_run(city)
    # Marked line on stdout so stray print()s inside a parser can't corrupt the parse.
    sys.stdout.write(_RESULT_MARKER + json.dumps(result) + "\n")
    sys.stdout.flush()


def test_one_parser(city: str, timeout_sec: int = PER_PARSER_TIMEOUT_SEC) -> Dict:
    """Run a single parser in an isolated subprocess; hard timeout + process-group kill.

    Returns {status, meeting_count, error, duration_sec, timestamp} — same shape as before.
    status ∈ {healthy, empty, timeout, error}.
    """
    ts = datetime.now().isoformat(timespec="seconds")
    start = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--worker", city],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,  # parser logs (field_absence etc.) go to stderr — discard
        text=True,
        start_new_session=True,  # own process group, so we can kill chromium/playwright children
    )
    try:
        out, _ = proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.communicate()
        return {"status": "timeout", "meeting_count": 0, "error": f"timeout after {timeout_sec}s",
                "duration_sec": timeout_sec, "timestamp": ts}
    # Parse the marked result line
    for line in (out or "").splitlines():
        if line.startswith(_RESULT_MARKER):
            r = json.loads(line[len(_RESULT_MARKER):])
            r["timestamp"] = ts
            return r
    return {"status": "error", "meeting_count": 0,
            "error": f"worker produced no result (exit={proc.returncode})",
            "duration_sec": round(time.monotonic() - start, 2), "timestamp": ts}


def run_all(cities: Optional[List[str]] = None, out_dir: Path = HEALTH_REPORTS_DIR,
            timeout_sec: int = PER_PARSER_TIMEOUT_SEC, label: str = "all") -> Dict:
    from parser_loader import load_parser_index
    idx = load_parser_index()
    if cities is None:
        cities = sorted(idx.keys())
    results: Dict[str, Dict] = {}
    started_at = datetime.now()
    print(f"Testing {len(cities)} parsers ({label}) — subprocess-isolated, {timeout_sec}s hard timeout each")
    print("=" * 74)
    counts = {"healthy": 0, "empty": 0, "timeout": 0, "error": 0}
    for i, city in enumerate(cities, 1):
        info = idx.get(city, {})
        fmt = info.get("calendar_format", "?")
        print(f"[{i:>2}/{len(cities)}] {city:<24} ({str(fmt)[:24]:<24})", end=" ", flush=True)
        r = test_one_parser(city, timeout_sec)
        r["calendar_format"] = fmt
        r["county"] = info.get("county", "?")
        results[city] = r
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        marker = {"healthy": "OK", "empty": "EMPTY", "timeout": "TIMEOUT", "error": "ERR"}.get(r["status"], "?")
        detail = f" {r['meeting_count']:>4} meetings" if r["status"] == "healthy" else f" ({(r.get('error') or '')[:46]})"
        print(f"{marker}{detail} [{r['duration_sec']}s]")
    ended_at = datetime.now()
    duration_min = (ended_at - started_at).total_seconds() / 60
    print("=" * 74)
    print(f"Healthy: {counts['healthy']} | Empty: {counts['empty']} | Timeout: {counts['timeout']} | Error: {counts['error']}")
    print(f"Duration: {duration_min:.1f} min")

    report = {
        "started_at": started_at.isoformat(timespec="seconds"),
        "ended_at": ended_at.isoformat(timespec="seconds"),
        "duration_min": round(duration_min, 1),
        "scope": label,
        "city_count": len(cities),
        "summary": {**counts, "healthy_pct": round(100 * counts["healthy"] / max(1, len(cities)), 1)},
        "results": results,
    }
    ts = started_at.strftime("%Y%m%d_%H%M%S")
    (out_dir / f"health_report_{ts}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out_dir / "health_report_latest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Report written: health_report_{ts}.json (+ health_report_latest.json)")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="Ground-truth parser-health probe (subprocess-isolated).")
    ap.add_argument("--worker", help="INTERNAL: run one city in-process + print a marked JSON line.")
    ap.add_argument("--state", choices=["az"], help="Restrict the sweep to one state (currently: az).")
    ap.add_argument("--cities", help="Comma-separated city list to restrict the sweep to.")
    ap.add_argument("--timeout", type=int, default=PER_PARSER_TIMEOUT_SEC, help="Per-parser hard timeout (s).")
    args = ap.parse_args()

    if args.worker:
        _worker_main(args.worker)
        return

    from parser_loader import load_parser_index
    if args.cities:
        cities = [c.strip() for c in args.cities.split(",") if c.strip()]
        label = f"cities={len(cities)}"
    elif args.state == "az":
        idx = load_parser_index()
        cities = sorted(c for c, info in idx.items() if isinstance(info, dict) and _is_az(info))
        label = "arizona"
    else:
        cities = None
        label = "all"
    run_all(cities=cities, timeout_sec=args.timeout, label=label)


if __name__ == "__main__":
    main()
