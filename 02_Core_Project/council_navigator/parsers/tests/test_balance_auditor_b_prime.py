"""Unit tests for Balance Auditor B-prime (today_spend anomaly check).

The B-prime reframe (James 2026-05-31, commit 92a318e) replaced the
original "$0.50 observed-vs-expected drift" check with a today_spend vs
trailing-7d-avg × 2 anomaly detection, $1 floor, operating on today's
in-progress bucket.

This module tests the `_check_today_spend_anomaly` function in
`scripts/balance_auditor_balance_check.py` — the load-bearing decision
function. Coverage targets:

  * Cold-start safety: no trailing data → no anomaly (no baseline)
  * Floor binding: small trailing avg → $1 floor is the threshold
  * Multiplier binding: large trailing avg → 2× avg is the threshold
  * Anomaly detection: todays_cents > threshold → anomaly_detected=True
  * Schema stability: returned dict has the expected keys for the
    PowerShell heartbeat consumer
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Make parsers/scripts/ importable when invoked from cwd=parsers/
_PARSERS_DIR = Path(__file__).resolve().parent.parent
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))
_SCRIPTS_DIR = _PARSERS_DIR / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import balance_auditor_balance_check as bc  # noqa: E402


# ── Fixture helpers ───────────────────────────────────────────────


def _spend_row(amount_cents: int, bucket_end_time: int = 1700000000) -> dict:
    """Build a synthetic spend_observed row for the trailing-data mock."""
    return {
        "amount_cents": amount_cents,
        "bucket_end_time": bucket_end_time,
        "provider": "openai",
        "event_type": "spend_observed",
    }


# ── Cold-start path ───────────────────────────────────────────────


def test_anomaly_check_cold_start_no_trailing_data():
    """If no trailing spend rows exist (auditor first-run), suppress
    the anomaly signal entirely — there's no baseline to compare against.
    A naive implementation might treat 'no trailing data' as 'avg=0,
    threshold=$1, every nonzero spend is an anomaly' — that would spam
    alerts during the first week. The cold-start exit avoids this.
    """
    with patch.object(bc, "get_trailing_spend_observed", return_value=[]):
        result = bc._check_today_spend_anomaly(todays_cents=500)
    assert result["anomaly_detected"] is False
    assert "cold-start" in result["reason"]
    assert result["trailing_days_observed"] == 0
    assert result["trailing_avg_cents"] == 0
    assert result["todays_cents"] == 500


def test_anomaly_check_cold_start_with_zero_today():
    """Even with todays_cents=0, cold-start path returns no-anomaly."""
    with patch.object(bc, "get_trailing_spend_observed", return_value=[]):
        result = bc._check_today_spend_anomaly(todays_cents=0)
    assert result["anomaly_detected"] is False


# ── Floor-binding path (trailing avg near zero) ───────────────────


def test_anomaly_floor_binds_when_trailing_avg_is_zero():
    """When the trailing average is $0 (all-zero spend buckets, common
    during idle days), the threshold should be the $1 floor, not $0.
    Otherwise any $0.01 of spend would be infinitely over the threshold.
    """
    rows = [_spend_row(0) for _ in range(7)]
    with patch.object(bc, "get_trailing_spend_observed", return_value=rows):
        result = bc._check_today_spend_anomaly(todays_cents=50)
    # 50 cents is below the $1 floor → no anomaly
    assert result["anomaly_detected"] is False
    assert result["threshold_cents"] == 100  # the floor
    assert result["trailing_avg_cents"] == 0


def test_anomaly_floor_binds_and_triggers_above_floor():
    """Above the $1 floor with a $0 trailing average → anomaly."""
    rows = [_spend_row(0) for _ in range(7)]
    with patch.object(bc, "get_trailing_spend_observed", return_value=rows):
        result = bc._check_today_spend_anomaly(todays_cents=200)
    # 200 cents > 100-cent floor → anomaly
    assert result["anomaly_detected"] is True
    assert result["threshold_cents"] == 100
    assert "$2.00" in result["reason"]
    assert "$1.00" in result["reason"]  # floor mentioned in humanized reason


def test_anomaly_floor_binds_with_small_nonzero_avg():
    """Trailing avg of 30 cents → 2× avg = 60 cents → still below the
    $1 floor, so the floor binds (threshold = $1.00).
    """
    rows = [_spend_row(30) for _ in range(7)]
    with patch.object(bc, "get_trailing_spend_observed", return_value=rows):
        result = bc._check_today_spend_anomaly(todays_cents=80)
    # 80 < 100-cent floor → no anomaly, threshold is floor not 2*avg
    assert result["anomaly_detected"] is False
    assert result["threshold_cents"] == 100


# ── Multiplier-binding path (trailing avg above $0.50) ────────────


def test_anomaly_multiplier_binds_when_trailing_avg_above_floor():
    """When trailing avg is e.g. 200 cents ($2), threshold = max(100, 400)
    = 400 cents. Multiplier binds, not floor.
    """
    rows = [_spend_row(200) for _ in range(7)]
    with patch.object(bc, "get_trailing_spend_observed", return_value=rows):
        result = bc._check_today_spend_anomaly(todays_cents=350)
    assert result["anomaly_detected"] is False
    assert result["threshold_cents"] == 400
    assert result["trailing_avg_cents"] == 200


def test_anomaly_multiplier_binds_and_triggers():
    """Trailing avg of $2, today spend of $5 → 500 > 400 → anomaly."""
    rows = [_spend_row(200) for _ in range(7)]
    with patch.object(bc, "get_trailing_spend_observed", return_value=rows):
        result = bc._check_today_spend_anomaly(todays_cents=500)
    assert result["anomaly_detected"] is True
    assert result["threshold_cents"] == 400


# ── Threshold-exact boundary ──────────────────────────────────────


def test_anomaly_today_exactly_at_threshold_is_not_anomaly():
    """The check is strict greater-than: at threshold = not an anomaly.
    This matters at the boundary — exactly hitting 2× trailing avg
    shouldn't fire the alarm.
    """
    rows = [_spend_row(200) for _ in range(7)]
    with patch.object(bc, "get_trailing_spend_observed", return_value=rows):
        result = bc._check_today_spend_anomaly(todays_cents=400)  # exactly 2× avg
    assert result["anomaly_detected"] is False


def test_anomaly_one_cent_over_threshold_triggers():
    """One cent over the threshold IS an anomaly (strict gt)."""
    rows = [_spend_row(200) for _ in range(7)]
    with patch.object(bc, "get_trailing_spend_observed", return_value=rows):
        result = bc._check_today_spend_anomaly(todays_cents=401)
    assert result["anomaly_detected"] is True


# ── Partial trailing window (fewer than 7 days) ───────────────────


def test_anomaly_with_partial_trailing_window():
    """When auditor has run for fewer than 7 days, average over what
    exists. Don't fail; don't treat as cold-start.
    """
    rows = [_spend_row(100), _spend_row(200), _spend_row(300)]
    with patch.object(bc, "get_trailing_spend_observed", return_value=rows):
        result = bc._check_today_spend_anomaly(todays_cents=500)
    # avg = 200, threshold = 400, today = 500 → anomaly
    assert result["anomaly_detected"] is True
    assert result["trailing_days_observed"] == 3
    assert result["trailing_avg_cents"] == 200


# ── Defensive: None / missing amount_cents in rows ────────────────


def test_anomaly_tolerates_row_with_none_amount():
    """If a spend row has amount_cents=None (shouldn't happen, but be
    defensive), it counts as 0 in the average rather than crashing.
    """
    rows = [_spend_row(100), {"amount_cents": None, "provider": "openai"}, _spend_row(200)]
    with patch.object(bc, "get_trailing_spend_observed", return_value=rows):
        result = bc._check_today_spend_anomaly(todays_cents=500)
    # avg = (100 + 0 + 200) / 3 = 100, threshold = max(100, 200) = 200
    assert result["trailing_avg_cents"] == 100
    assert result["threshold_cents"] == 200
    assert result["anomaly_detected"] is True


# ── Schema stability ─────────────────────────────────────────────


def test_anomaly_result_schema_is_stable():
    """The PowerShell heartbeat (ops/balance-auditor-heartbeat.ps1) reads
    specific fields from this dict. If we ever rename them, the heartbeat
    breaks silently. Lock the schema with an explicit test.
    """
    rows = [_spend_row(100) for _ in range(7)]
    with patch.object(bc, "get_trailing_spend_observed", return_value=rows):
        result = bc._check_today_spend_anomaly(todays_cents=300)

    expected_keys = {
        "anomaly_detected", "reason",
        "trailing_days_observed", "trailing_avg_cents",
        "threshold_cents", "todays_cents",
        "multiplier", "floor_cents",
    }
    # Subset check — implementation may add fields (e.g., _pretty
    # variants for humanized output) but must keep the load-bearing ones.
    assert expected_keys.issubset(set(result.keys())), (
        f"Schema regression: missing keys {expected_keys - set(result.keys())}"
    )

    # Pretty-formatted variants present in non-cold-start path:
    if result["trailing_days_observed"] > 0:
        assert "trailing_avg_pretty" in result
        assert "threshold_pretty" in result
        assert "todays_pretty" in result


def test_anomaly_constants_match_documented_defaults():
    """The B-prime spec (James 2026-05-31): trailing 7 days, multiplier
    2.0, floor $1.00. If anyone changes these constants without realizing,
    it shifts the alert sensitivity for the whole project. Lock them.
    """
    assert bc.SPEND_ANOMALY_TRAILING_DAYS == 7
    assert bc.SPEND_ANOMALY_MULTIPLIER == 2.0
    assert bc.SPEND_ANOMALY_FLOOR_CENTS == 100


# ── Humanized reason format ──────────────────────────────────────


def test_anomaly_reason_mentions_money_amounts_pretty():
    """The humanized 'reason' string is what surfaces in Slack escalations.
    It should mention dollar amounts in pretty format ($X.YZ), not raw
    cents, so operators read it cleanly.
    """
    rows = [_spend_row(200) for _ in range(7)]
    with patch.object(bc, "get_trailing_spend_observed", return_value=rows):
        result = bc._check_today_spend_anomaly(todays_cents=500)
    assert "$5.00" in result["reason"]  # today
    assert "$4.00" in result["reason"]  # threshold
    assert "$2.00" in result["reason"]  # trailing avg
