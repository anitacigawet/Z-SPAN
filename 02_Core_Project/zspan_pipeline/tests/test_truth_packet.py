"""Unit tests for S-009 truth-packet gate.

Covers spec § 6 verdict semantics across all three paths (pass / halt /
ambiguous), the schema validator, and the JSON-fence stripping.

Run via:
    python3.11 -m pytest 02_Core_Project/zspan_pipeline/tests/test_truth_packet.py -v
"""
from __future__ import annotations

import json

import pytest

from zspan_pipeline.truth_packet import (
    TRUTH_PACKET_SCHEMA,
    TruthPacketResult,
    _strip_json_fence,
    _validate,
    gate_truth_packet,
)


# ── Sample observation builders ────────────────────────────────────────


def _valid_observations(**overrides):
    """Build a schema-valid observations dict; override fields as needed."""
    base = {
        "event_type": "city_council_meeting",
        "event_type_evidence": "Council dais visible with 7 seated members behind nameplates. Agenda items projected behind the dais.",
        "jurisdiction_observed": "City of Kingman",
        "jurisdiction_evidence": "City of Kingman seal visible on the front of the dais.",
        "apparent_substantive_duration_seconds": 4500,
        "apparent_total_duration_seconds": 4800,
        "speakers_observed_count": 7,
        "observations": [
            "A council dais with 7 seated members.",
            "Discussion of a water-allocation resolution occupies ~12 minutes.",
            "Public comment segment lasts ~8 minutes.",
        ],
        "anomalies": [],
    }
    base.update(overrides)
    return base


def _valid_response(**overrides) -> str:
    return json.dumps(_valid_observations(**overrides))


# ── Pass path ──────────────────────────────────────────────────────────


def test_valid_council_meeting_passes():
    result = gate_truth_packet(_valid_response(), expected_jurisdiction="Kingman")
    assert result.verdict == "pass"
    assert "city_council_meeting" in result.reason
    assert result.observations["event_type"] == "city_council_meeting"


def test_pass_without_expected_jurisdiction():
    """When the caller doesn't supply expected_jurisdiction, the gate skips
    the cross-check and a valid response still passes."""
    result = gate_truth_packet(_valid_response(), expected_jurisdiction=None)
    assert result.verdict == "pass"


def test_pass_with_fuzzy_jurisdiction_match():
    """'Kingman' is a substring of 'City of Kingman' — passes."""
    result = gate_truth_packet(
        _valid_response(jurisdiction_observed="City of Kingman, Arizona"),
        expected_jurisdiction="Kingman",
    )
    assert result.verdict == "pass"


def test_pass_with_not_observed_jurisdiction():
    """If the model couldn't observe a jurisdiction, the cross-check is
    skipped (not_observed is a sentinel, not a failure)."""
    result = gate_truth_packet(
        _valid_response(jurisdiction_observed="not_observed"),
        expected_jurisdiction="Kingman",
    )
    assert result.verdict == "pass"


# ── Halt path ──────────────────────────────────────────────────────────


def test_halt_on_press_conference():
    result = gate_truth_packet(
        _valid_response(
            event_type="press_conference",
            event_type_evidence="Single speaker at a podium with logo backdrop; no dais or seated council visible.",
        ),
        expected_jurisdiction="Kingman",
    )
    assert result.verdict == "halt"
    assert "press_conference" in result.reason
    # Evidence should appear in the reason for operator visibility
    assert "podium" in result.reason or "Evidence" in result.reason


def test_halt_on_non_government():
    result = gate_truth_packet(
        _valid_response(event_type="non_government"),
        expected_jurisdiction="Kingman",
    )
    assert result.verdict == "halt"


def test_halt_on_unclear_event_type():
    result = gate_truth_packet(
        _valid_response(event_type="unclear"),
        expected_jurisdiction="Kingman",
    )
    assert result.verdict == "halt"


def test_halt_on_too_short_substantive_duration():
    """Below the default 600s floor halts as a truncated/empty upload."""
    result = gate_truth_packet(
        _valid_response(apparent_substantive_duration_seconds=240),
        expected_jurisdiction="Kingman",
    )
    assert result.verdict == "halt"
    assert "240s" in result.reason
    assert "600s" in result.reason


def test_halt_on_zero_substantive_duration():
    result = gate_truth_packet(
        _valid_response(apparent_substantive_duration_seconds=0),
        expected_jurisdiction="Kingman",
    )
    assert result.verdict == "halt"


def test_custom_min_substantive_seconds():
    """A 300s recording passes when the caller lowers the floor."""
    result = gate_truth_packet(
        _valid_response(apparent_substantive_duration_seconds=400),
        expected_jurisdiction="Kingman",
        min_substantive_seconds=300,
    )
    assert result.verdict == "pass"


# ── Ambiguous path ────────────────────────────────────────────────────


def test_ambiguous_on_gov_other():
    """city_government_meeting_other escalates rather than auto-passing or
    auto-halting — operator decides whether it counts for this WO."""
    result = gate_truth_packet(
        _valid_response(event_type="city_government_meeting_other"),
        expected_jurisdiction="Kingman",
    )
    assert result.verdict == "ambiguous"
    assert "city_government_meeting_other" in result.reason


def test_ambiguous_on_jurisdiction_mismatch():
    """If the WO is for Kingman but the recording shows Bullhead, escalate."""
    result = gate_truth_packet(
        _valid_response(jurisdiction_observed="City of Bullhead"),
        expected_jurisdiction="Kingman",
    )
    assert result.verdict == "ambiguous"
    assert "Bullhead" in result.reason
    assert "Kingman" in result.reason


def test_ambiguous_on_non_empty_anomalies():
    """Any non-empty anomalies array escalates."""
    result = gate_truth_packet(
        _valid_response(
            anomalies=["Recording cuts off mid-sentence after 14 minutes."]
        ),
        expected_jurisdiction="Kingman",
    )
    assert result.verdict == "ambiguous"
    assert "cuts off" in result.reason or "1 anomaly" in result.reason


def test_ambiguous_on_malformed_json():
    """A non-JSON response is ambiguous, NOT halt — model may have glitched."""
    result = gate_truth_packet(
        "I'm happy to help! Here's the analysis: this is a council meeting.",
        expected_jurisdiction="Kingman",
    )
    assert result.verdict == "ambiguous"
    assert "non-JSON" in result.reason
    assert result.observations == {}


def test_ambiguous_on_empty_response():
    result = gate_truth_packet("", expected_jurisdiction="Kingman")
    assert result.verdict == "ambiguous"


def test_ambiguous_on_json_array_instead_of_object():
    """JSON that parses but isn't an object is ambiguous."""
    result = gate_truth_packet('[1, 2, 3]', expected_jurisdiction="Kingman")
    assert result.verdict == "ambiguous"
    assert "object" in result.reason


def test_ambiguous_on_schema_violation_missing_field():
    bad = _valid_observations()
    del bad["event_type"]
    result = gate_truth_packet(json.dumps(bad), expected_jurisdiction="Kingman")
    assert result.verdict == "ambiguous"
    assert "event_type" in result.reason


def test_ambiguous_on_schema_violation_wrong_type():
    bad = _valid_observations(apparent_substantive_duration_seconds="not an int")
    result = gate_truth_packet(json.dumps(bad), expected_jurisdiction="Kingman")
    assert result.verdict == "ambiguous"


def test_ambiguous_on_disallowed_event_type():
    bad = _valid_observations(event_type="something_made_up")
    result = gate_truth_packet(json.dumps(bad), expected_jurisdiction="Kingman")
    assert result.verdict == "ambiguous"
    assert "allowed set" in result.reason


def test_ambiguous_on_substantive_duration_over_max():
    """Schema max is 36000s (10 hours); over that is a schema violation,
    which is ambiguous, not halt."""
    bad = _valid_observations(apparent_substantive_duration_seconds=99999)
    result = gate_truth_packet(json.dumps(bad), expected_jurisdiction="Kingman")
    assert result.verdict == "ambiguous"


# ── JSON-fence stripping ──────────────────────────────────────────────


def test_passes_with_markdown_json_fence():
    """A fenced response is unwrapped and still passes."""
    fenced = "```json\n" + _valid_response() + "\n```"
    result = gate_truth_packet(fenced, expected_jurisdiction="Kingman")
    assert result.verdict == "pass"


def test_passes_with_bare_markdown_fence():
    """A bare ``` fence (no language tag) is also unwrapped."""
    fenced = "```\n" + _valid_response() + "\n```"
    result = gate_truth_packet(fenced, expected_jurisdiction="Kingman")
    assert result.verdict == "pass"


def test_strip_fence_passes_through_unfenced():
    raw = _valid_response()
    assert _strip_json_fence(raw) == raw


def test_strip_fence_handles_whitespace():
    raw = _valid_response()
    fenced = f"  \n```json\n{raw}\n```\n  "
    assert _strip_json_fence(fenced) == raw


# ── Schema validator unit tests ───────────────────────────────────────


def test_validator_accepts_valid():
    errors = _validate(_valid_observations(), TRUTH_PACKET_SCHEMA)
    assert errors == []


def test_validator_rejects_non_dict():
    errors = _validate([1, 2, 3], TRUTH_PACKET_SCHEMA)
    assert len(errors) == 1
    assert "object" in errors[0]


def test_validator_rejects_bool_for_int():
    """bool is a subclass of int in Python; we reject it explicitly so a
    JSON `true` doesn't sneak past the int check on a duration field."""
    bad = _valid_observations(apparent_substantive_duration_seconds=True)
    errors = _validate(bad, TRUTH_PACKET_SCHEMA)
    assert any("must be int" in e for e in errors)


def test_validator_rejects_int_below_min():
    bad = _valid_observations(speakers_observed_count=-1)
    errors = _validate(bad, TRUTH_PACKET_SCHEMA)
    assert any("below min" in e for e in errors)


def test_validator_rejects_int_above_max():
    bad = _valid_observations(speakers_observed_count=999)
    errors = _validate(bad, TRUTH_PACKET_SCHEMA)
    assert any("above max" in e for e in errors)


def test_validator_rejects_string_too_long():
    bad = _valid_observations(jurisdiction_observed="x" * 201)
    errors = _validate(bad, TRUTH_PACKET_SCHEMA)
    assert any("above max_len" in e for e in errors)


def test_validator_rejects_empty_observations_list():
    """observations has min_len=1."""
    bad = _valid_observations(observations=[])
    errors = _validate(bad, TRUTH_PACKET_SCHEMA)
    assert any("observations" in e and "below min_len" in e for e in errors)


def test_validator_rejects_non_string_in_observations():
    bad = _valid_observations(observations=["valid string", 42])
    errors = _validate(bad, TRUTH_PACKET_SCHEMA)
    assert any("observations[1]" in e for e in errors)


def test_validator_accepts_empty_anomalies():
    """anomalies has min_len=0 — empty array is the explicit 'nothing
    unusual' signal."""
    errors = _validate(_valid_observations(anomalies=[]), TRUTH_PACKET_SCHEMA)
    assert errors == []


# ── Result shape ──────────────────────────────────────────────────────


def test_result_is_namedtuple_with_three_fields():
    result = gate_truth_packet(_valid_response(), expected_jurisdiction="Kingman")
    assert isinstance(result, TruthPacketResult)
    assert hasattr(result, "verdict")
    assert hasattr(result, "reason")
    assert hasattr(result, "observations")


def test_halt_result_preserves_observations_for_audit():
    """Even on halt, the parsed observations are returned so the operator
    can see what the model actually reported."""
    result = gate_truth_packet(
        _valid_response(event_type="press_conference"),
        expected_jurisdiction="Kingman",
    )
    assert result.verdict == "halt"
    assert result.observations["event_type"] == "press_conference"
    assert result.observations["jurisdiction_observed"] == "City of Kingman"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
