#!/usr/bin/env python3.11
"""Self-tests for claude_p_metered (D-119 metering wrapper).

Pure-function unit tests (no DB, no claude -- claude_p_metered lazy-imports
database, so importing it here pulls in nothing heavy) + one dry-run fixture
smoke that exercises the full gate/parse/summary path WITHOUT writing the
ledger or invoking claude.

Run: python3.11 test_claude_p_metered.py
"""
import datetime as dt
import json
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import claude_p_metered as m  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# --- parse_cost_cents ------------------------------------------------------ #
sj = "\n".join([
    '{"type":"system","subtype":"init"}',
    'not-json-noise-line',
    '{"type":"assistant","message":{"content":"working"}}',
    '{"type":"result","subtype":"success","total_cost_usd":0.0734,'
    '"usage":{"input_tokens":1200,"output_tokens":340}}',
])
cents, usage = m.parse_cost_cents(sj)
check("parse: 0.0734 usd -> 7 cents", cents == 7)
check("parse: usage extracted", bool(usage) and usage.get("input_tokens") == 1200)
check("parse: no result event -> None", m.parse_cost_cents('{"type":"system"}')[0] is None)
check("parse: empty transcript -> None", m.parse_cost_cents("")[0] is None)
check("parse: 0.005 -> 1 cent (round)",
      m.parse_cost_cents('{"type":"result","total_cost_usd":0.005}')[0] == 1)
check("parse: 1.239 -> 124 cents",
      m.parse_cost_cents('{"type":"result","total_cost_usd":1.239}')[0] == 124)
check("parse: last result wins",
      m.parse_cost_cents('{"type":"result","total_cost_usd":0.10}\n'
                         '{"type":"result","total_cost_usd":0.25}')[0] == 25)

# --- gate_decision --------------------------------------------------------- #
check("gate: below ceiling -> allow", m.gate_decision(250, 300) is True)
check("gate: at ceiling -> refuse", m.gate_decision(300, 300) is False)
check("gate: over ceiling -> refuse", m.gate_decision(305, 300) is False)
check("gate: zero spend -> allow", m.gate_decision(0, 300) is True)

# --- load_thresholds ------------------------------------------------------- #
d, c = m.load_thresholds(Path("/nonexistent/settings.json"))
check("thresholds: defaults $3/$1", d == 300 and c == 100)
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
    json.dump({"anthropic_daily_ceiling_usd": 6, "anthropic_per_call_cap_usd": 2}, f)
    over = f.name
d, c = m.load_thresholds(Path(over))
check("thresholds: override $6/$2", d == 600 and c == 200)
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
    json.dump({"anthropic_daily_ceiling_usd": "bad", "anthropic_per_call_cap_usd": -5}, f)
    bad = f.name
d, c = m.load_thresholds(Path(bad))
check("thresholds: bad/negative -> safe defaults", d == 300 and c == 100)

# --- utc_today_start_unix -------------------------------------------------- #
noon = int(dt.datetime(2026, 6, 17, 14, 0, 0, tzinfo=dt.timezone.utc).timestamp())
midnight = int(dt.datetime(2026, 6, 17, 0, 0, 0, tzinfo=dt.timezone.utc).timestamp())
check("utc_today_start: collapses to UTC midnight", m.utc_today_start_unix(noon) == midnight)

# --- dry-run fixture e2e (no claude call, no ledger write) ----------------- #
# Hermetic: isolate the wrapper against a FRESH temp DB (ZSPAN_DB_PATH) so the
# gate's today-spend reads 0 regardless of the real production ledger. Without
# this the allow-path assertions flip to the refuse path once real fleet spend
# exceeds the day's ceiling (found 2026-06-17 — real validation fires pushed the
# production anthropic ledger over $3/day).
import os  # noqa: E402
_tmpdb = Path(tempfile.mkdtemp()) / "metered_selftest.db"
_env = {**os.environ, "ZSPAN_DB_PATH": str(_tmpdb)}
# Create the balance_ledger schema in the temp DB (separate process so this
# test process's already-imported `database` keeps its own DB_PATH).
subprocess.run([sys.executable, "-c", "import database; database.init_db()"],
               cwd=str(_HERE.parent), env=_env, capture_output=True, text=True)
fix = Path(tempfile.mktemp(suffix=".jsonl"))
fix.write_text(sj, encoding="utf-8")
proc = subprocess.run(
    [sys.executable, str(_HERE / "claude_p_metered.py"),
     "--role", "selftest", "--dry-run", "--claude-output-fixture", str(fix)],
    input="", capture_output=True, text=True, env=_env,
)
out = {}
try:
    out = json.loads(proc.stdout)
except ValueError:
    pass
check("e2e dry-run: exit 0", proc.returncode == 0)
check("e2e dry-run: metered cost 7c", out.get("meter_ok") is True and out.get("call_cost_cents") == 7)
check("e2e dry-run: not refused", out.get("refused") is False)
check("e2e dry-run: balance not computed in dry-run", out.get("balance_cents") is None)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
