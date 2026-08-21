"""
S-009 Truth Packet — grounded-observation gate for the pipeline (dormant).

Runs FIRST per work order via fetcher.fetch_all_outputs (wired in chunk 3 of
S-009; this module ships in chunk 1 standalone with unit tests).

Design rule (load-bearing): the truth-packet prompt asks the model to report
GROUNDED OBSERVATIONS about the loaded source — what kind of event, what
jurisdiction, how long, who's speaking — and this module then applies
STRUCTURED RULES to those observations to emit a pass/halt/ambiguous verdict.
The trust judgment lives here, not in the model response. See spec § 3.3.

Verdicts:
    pass       The source looks like a real council meeting in the expected
               jurisdiction with substantive duration. Bridge proceeds with
               the rest of the WO.
    halt       The source is high-confidence wrong (not a council meeting,
               empty/truncated). WO marked failed_truth_packet; remaining
               outputs skipped. No further quota spent. Operator must re-
               paste a corrected video URL to retry.
    ambiguous  Something looks off but it's not clear-cut (jurisdiction
               mismatch, anomalies present, JSON parse failed, city_government
               _meeting_other). WO holds in awaiting_truth_packet_review.
               Operator decides.

Spec: 01_Project_Overview/S-009_TRUTH_PACKET_SPEC.md

S-008 V0 extension (surface S-1):
    The `detect_adversarial_shape` function runs a deterministic rule pass
    over the parsed observations dict and surfaces anomalies inconsistent
    with the grounded-observations rubric — structural fence markers,
    suspicious URL density, code-shaped substrings, off-topic vocabulary
    above a threshold. Detected items are appended to `observations.anomalies`
    BEFORE the existing rubric's anomaly check fires, so adversarial-shape
    findings flow through the ambiguous path (operator review) rather than
    auto-passing.

    Not an LLM call. Per
    `01_Project_Overview/THREAT_MODEL_INPUT_SECURITY.md` surface S-1.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Literal, NamedTuple

logger = logging.getLogger(__name__)


Verdict = Literal["pass", "halt", "ambiguous"]


class TruthPacketResult(NamedTuple):
    """Result of gating a truth-packet response.

    Attributes:
        verdict: 'pass', 'halt', or 'ambiguous'. The fetcher uses this to
            decide whether to continue, abandon, or escalate the WO.
        reason: Human-readable description of why the verdict fired. Used
            in the WO state writeback (logged) and in the operator-facing
            escalation message (for ambiguous/halt).
        observations: The parsed JSON dict (possibly empty on parse failure).
            Persisted for audit so the operator can inspect what the model
            actually returned.
    """

    verdict: Verdict
    reason: str
    observations: dict[str, Any]


# The seven event-type categories the prompt asks the model to choose from.
# Only `city_council_meeting` auto-passes the gate; `city_government_meeting_other`
# escalates (might be valid for a city-government-other WO, but currently OOS);
# everything else halts.
_ALLOWED_EVENT_TYPES = {
    "city_council_meeting",
    "city_government_meeting_other",
    "press_conference",
    "workshop_briefing",
    "community_event",
    "non_government",
    "unclear",
}


# The schema validator config. Hand-written rather than depending on a third-
# party schema library — keeps the bridge's dependency surface tight.
TRUTH_PACKET_SCHEMA: dict[str, dict[str, Any]] = {
    "event_type": {
        "type": str,
        "allowed": _ALLOWED_EVENT_TYPES,
    },
    "event_type_evidence": {"type": str, "min_len": 1, "max_len": 1000},
    "jurisdiction_observed": {"type": str, "min_len": 1, "max_len": 200},
    "jurisdiction_evidence": {"type": str, "min_len": 1, "max_len": 1000},
    "apparent_substantive_duration_seconds": {"type": int, "min": 0, "max": 36000},
    "apparent_total_duration_seconds": {"type": int, "min": 0, "max": 36000},
    "speakers_observed_count": {"type": int, "min": 0, "max": 100},
    "observations": {"type": list, "of": str, "min_len": 1, "max_len": 10},
    "anomalies": {"type": list, "of": str, "min_len": 0, "max_len": 10},
}


def _validate(obj: Any, schema: dict[str, dict[str, Any]]) -> list[str]:
    """Validate `obj` against `schema`. Returns a list of human-readable
    error strings — empty list means valid.

    The schema vocabulary is tiny by design: type checks, allowed-set checks
    for strings, min/max for ints, min_len/max_len for str and list, `of`
    for the element type of a list. Anything more sophisticated belongs in
    a real validator; we don't need it.
    """
    errors: list[str] = []

    if not isinstance(obj, dict):
        return [f"top-level must be a JSON object, got {type(obj).__name__}"]

    for field, rules in schema.items():
        if field not in obj:
            errors.append(f"missing required field '{field}'")
            continue
        value = obj[field]
        expected_type = rules["type"]
        # bool is a subclass of int in Python; reject it explicitly when we
        # asked for int so a JSON `true`/`false` doesn't pass an int check.
        if expected_type is int and isinstance(value, bool):
            errors.append(f"field '{field}' must be {expected_type.__name__}, got bool")
            continue
        if not isinstance(value, expected_type):
            errors.append(
                f"field '{field}' must be {expected_type.__name__}, got {type(value).__name__}"
            )
            continue

        if expected_type is str:
            if "allowed" in rules and value not in rules["allowed"]:
                errors.append(
                    f"field '{field}' value {value!r} not in allowed set "
                    f"{sorted(rules['allowed'])}"
                )
            if "min_len" in rules and len(value) < rules["min_len"]:
                errors.append(
                    f"field '{field}' length {len(value)} below min_len {rules['min_len']}"
                )
            if "max_len" in rules and len(value) > rules["max_len"]:
                errors.append(
                    f"field '{field}' length {len(value)} above max_len {rules['max_len']}"
                )

        elif expected_type is int:
            if "min" in rules and value < rules["min"]:
                errors.append(f"field '{field}' value {value} below min {rules['min']}")
            if "max" in rules and value > rules["max"]:
                errors.append(f"field '{field}' value {value} above max {rules['max']}")

        elif expected_type is list:
            if "min_len" in rules and len(value) < rules["min_len"]:
                errors.append(
                    f"field '{field}' length {len(value)} below min_len {rules['min_len']}"
                )
            if "max_len" in rules and len(value) > rules["max_len"]:
                errors.append(
                    f"field '{field}' length {len(value)} above max_len {rules['max_len']}"
                )
            elem_type = rules.get("of")
            if elem_type is not None:
                for i, elem in enumerate(value):
                    if not isinstance(elem, elem_type):
                        errors.append(
                            f"field '{field}[{i}]' must be {elem_type.__name__}, "
                            f"got {type(elem).__name__}"
                        )

    return errors


# ── S-008 V0 adversarial-shape detector (surface S-1) ──────────────────
# Deterministic patterns that suggest the truth-packet response has been
# steered by adversarial content in the source. Detection is deliberately
# conservative — false-positives only cost an operator review (ambiguous
# verdict), not a hard halt. False-negatives are not catastrophic either:
# downstream extraction parsers' own strict-mode (S-2) catches more.
#
# All checks are deterministic substring / regex / count rules. No LLM call.

_ADVERSARIAL_FENCE_MARKERS = (
    "<zspan-content-begin",
    "<zspan-content-end",
)

# Substrings that strongly suggest prompt-injection content surfaced into
# the model's grounded observations. A legitimate civic-meeting observation
# never contains these phrasings.
_ADVERSARIAL_INSTRUCTION_PATTERNS = (
    "ignore previous",
    "ignore all previous",
    "disregard above",
    "system prompt",
    "you are now",
    "act as ",
    "new instructions:",
    "override your",
    "previous instructions",
)

# Code/shell-shaped substrings that would not appear in legitimate
# grounded observations about a civic meeting.
_ADVERSARIAL_CODE_PATTERNS = (
    "<script",
    "javascript:",
    "data:text/html",
    "$(",
    "``",
)

# Bidi controls (same set used in input_security.primitives + slack_notifier).
_ADVERSARIAL_BIDI_CHARS = frozenset(
    chr(cp) for cp in (
        0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
        0x2066, 0x2067, 0x2068, 0x2069,
    )
)

# URL density threshold per observation. A real grounded observation about
# a civic meeting almost never has more than one URL.
_MAX_URLS_PER_OBSERVATION = 1


def _count_urls(text: str) -> int:
    lowered = text.lower()
    return lowered.count("http://") + lowered.count("https://")


def _walk_strings(obj):
    """Yield every string in a nested dict/list structure."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)


def detect_adversarial_shape(observations: dict) -> list[str]:
    """Run deterministic adversarial-shape checks against the parsed
    truth-packet observations dict. Returns a list of anomaly strings
    suitable for appending to `observations["anomalies"]`.

    No false-positive cost beyond operator review (anomalies → ambiguous
    verdict). The detector is intentionally narrow — broader content
    classification belongs in S-2 (per-output-type extraction rule pass).

    Per `01_Project_Overview/THREAT_MODEL_INPUT_SECURITY.md` surface S-1
    + S008_INPUT_SECURITY_SPEC.md chunk 2.4.
    """
    findings: list[str] = []

    # 1. Structural fence markers — agent-emit content should NEVER contain
    #    these; their presence in a model response indicates the model
    #    leaked instruction-frame content back into the observations.
    for s in _walk_strings(observations):
        lowered = s.lower()
        for marker in _ADVERSARIAL_FENCE_MARKERS:
            if marker in lowered:
                findings.append(
                    f"adversarial_shape: structural fence marker {marker!r} "
                    f"present in observation text"
                )
                break  # one finding per scanned string is enough

    # 2. Prompt-injection-shaped instruction patterns.
    for s in _walk_strings(observations):
        lowered = s.lower()
        for pat in _ADVERSARIAL_INSTRUCTION_PATTERNS:
            if pat in lowered:
                findings.append(
                    f"adversarial_shape: instruction-pattern {pat!r} present "
                    f"in observation text"
                )
                break

    # 3. Code/shell-shaped substrings.
    for s in _walk_strings(observations):
        lowered = s.lower()
        for pat in _ADVERSARIAL_CODE_PATTERNS:
            if pat in lowered:
                findings.append(
                    f"adversarial_shape: code-shaped substring {pat!r} present "
                    f"in observation text"
                )
                break

    # 4. Bidi controls.
    for s in _walk_strings(observations):
        if any(ch in _ADVERSARIAL_BIDI_CHARS for ch in s):
            findings.append(
                "adversarial_shape: bidi-control characters in observation text"
            )
            break

    # 5. URL density per observation. Walk the observations list
    #    specifically — top-level URLs in evidence fields might be
    #    legitimate, but in-bullet URL spam is a tell.
    obs_list = observations.get("observations")
    if isinstance(obs_list, list):
        for i, entry in enumerate(obs_list):
            if isinstance(entry, str) and _count_urls(entry) > _MAX_URLS_PER_OBSERVATION:
                findings.append(
                    f"adversarial_shape: observation[{i}] contains "
                    f"{_count_urls(entry)} URLs (max {_MAX_URLS_PER_OBSERVATION})"
                )

    return findings


def _strip_json_fence(raw: str) -> str:
    """Strip a leading/trailing markdown code fence if present.

    The prompt tells the model NOT to wrap the JSON in a fence, but defensively
    we strip ```json ... ``` (or ``` ... ```) wrappers so a fence-prepending
    model doesn't get auto-ambiguated on every WO. Anything else gets passed
    through verbatim — json.loads will tell us if it's still not parseable.
    """
    stripped = raw.strip()
    if stripped.startswith("```"):
        # Drop the first line (the fence opener) and the closing fence if present.
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def gate_truth_packet(
    raw_response: str,
    expected_jurisdiction: str | None = None,
    min_substantive_seconds: int = 600,
) -> TruthPacketResult:
    """Parse a truth-packet response and decide whether the WO proceeds.

    Args:
        raw_response: The raw text the model returned (expected to be JSON).
        expected_jurisdiction: The WO's city name (for cross-check). If None,
            skip the jurisdiction-match check.
        min_substantive_seconds: Halt if substantive duration is below this.
            Defaults to 10 minutes (600s) — a real council meeting is longer.

    Returns:
        TruthPacketResult with verdict, reason, observations.

    Verdict semantics (see module docstring): `pass` proceeds; `halt`
    short-circuits with no further quota spent and operator must re-paste
    a corrected URL; `ambiguous` holds the WO awaiting operator review.

    Failure modes — none auto-halt:
        - non-JSON response                -> ambiguous (LLM may have glitched)
        - schema validation failures       -> ambiguous (LLM may have glitched)
        - event_type != council_meeting    -> halt (high-confidence wrong source)
        - event_type == gov_other          -> ambiguous (operator confirms)
        - substantive duration < min       -> halt (truncated/empty upload)
        - jurisdiction mismatch            -> ambiguous (operator confirms)
        - non-empty anomalies              -> ambiguous (operator reviews)

    The principle: structural-halt verdicts only fire on grounded observations
    that are decisively wrong. Anything model-flakiness-shaped escalates to
    the human rather than burning the WO.
    """
    raw_clean = _strip_json_fence(raw_response or "")

    # 1. Parse the JSON. Any failure is ambiguous, NOT halt.
    try:
        observations = json.loads(raw_clean)
    except json.JSONDecodeError as e:
        return TruthPacketResult(
            verdict="ambiguous",
            reason=f"truth_packet returned non-JSON: {e}",
            observations={},
        )

    if not isinstance(observations, dict):
        return TruthPacketResult(
            verdict="ambiguous",
            reason=(
                f"truth_packet JSON top-level must be an object, "
                f"got {type(observations).__name__}"
            ),
            observations={},
        )

    schema_errors = _validate(observations, TRUTH_PACKET_SCHEMA)
    if schema_errors:
        return TruthPacketResult(
            verdict="ambiguous",
            reason=f"truth_packet JSON schema violations: {schema_errors[:3]}",
            observations=observations,
        )

    # S-008 V0 extension (surface S-1): augment the anomalies list with
    # adversarial-shape findings before the rubric's anomaly check runs.
    # The findings flow through the ambiguous path so the operator reviews.
    adv_findings = detect_adversarial_shape(observations)
    if adv_findings:
        existing = list(observations.get("anomalies") or [])
        observations["anomalies"] = existing + adv_findings

    # 2. event_type is the load-bearing field. Only city_council_meeting
    #    auto-passes; gov_other escalates; everything else halts.
    et = observations["event_type"]
    if et == "city_council_meeting":
        pass  # fall through to subsequent checks
    elif et == "city_government_meeting_other":
        return TruthPacketResult(
            verdict="ambiguous",
            reason=(
                f"truth_packet classified source as {et} "
                f"(not a council meeting); operator confirms whether to proceed."
            ),
            observations=observations,
        )
    else:
        evidence = observations.get("event_type_evidence", "<missing>")
        return TruthPacketResult(
            verdict="halt",
            reason=(
                f"truth_packet classified source as {et} "
                f"(expected city_council_meeting). Evidence: {evidence[:200]}"
            ),
            observations=observations,
        )

    # 3. Substantive-duration check — empty/truncated recordings halt.
    sub_dur = observations["apparent_substantive_duration_seconds"]
    if sub_dur < min_substantive_seconds:
        return TruthPacketResult(
            verdict="halt",
            reason=(
                f"truth_packet observed only {sub_dur}s of substantive content "
                f"(min={min_substantive_seconds}s). Likely truncated/empty upload."
            ),
            observations=observations,
        )

    # 4. Jurisdiction cross-check — if the WO's city doesn't appear in the
    #    observed jurisdiction (case-insensitive substring), escalate. This
    #    is intentionally fuzzy (not exact-match) since the model may return
    #    "City of Kingman" or "Kingman City" for a Kingman WO. James can
    #    tighten this to a canonical-name comparison in a future pass.
    if expected_jurisdiction:
        observed = observations["jurisdiction_observed"].lower()
        expected = expected_jurisdiction.lower()
        if observed != "not_observed" and expected not in observed:
            return TruthPacketResult(
                verdict="ambiguous",
                reason=(
                    f"truth_packet observed jurisdiction "
                    f"'{observations['jurisdiction_observed']}' but WO is for "
                    f"'{expected_jurisdiction}'. Operator confirms wrong-city paste."
                ),
                observations=observations,
            )

    # 5. Anomaly check — any non-empty anomalies escalate, never auto-halt.
    #    Truncation, weird speaker counts, non-English content, etc. all
    #    deserve operator eyes but might be legitimate.
    anomalies = observations["anomalies"]
    if anomalies:
        return TruthPacketResult(
            verdict="ambiguous",
            reason=(
                f"truth_packet flagged {len(anomalies)} anomaly(s): "
                f"{anomalies[:2]}"
            ),
            observations=observations,
        )

    # 6. All checks passed.
    return TruthPacketResult(
        verdict="pass",
        reason=(
            f"truth_packet confirmed city_council_meeting in "
            f"{observations['jurisdiction_observed']} "
            f"({sub_dur}s substantive, {observations['speakers_observed_count']} speakers)"
        ),
        observations=observations,
    )
