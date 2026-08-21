"""extraction_postcheck — deterministic rule pass over LLM extractions.

S-008 V0 / surface S-2 per
[`01_Project_Overview/S008_INPUT_SECURITY_SPEC.md`](../../01_Project_Overview/S008_INPUT_SECURITY_SPEC.md)
chunk 2.5 + [`01_Project_Overview/THREAT_MODEL_INPUT_SECURITY.md`](../../01_Project_Overview/THREAT_MODEL_INPUT_SECURITY.md)
surface S-2.

This module runs AFTER an extraction output parses cleanly. It applies
deterministic rules to flag content that does not match the expected shape
of legitimate civic extraction output:
- quote_text containing URLs that are not in the meeting's source context
- synopsis / agenda_transitions containing imperative directives addressed
  to external systems (suggesting prompt-leak)
- tracked_claims with speakers not on the meeting's roster
- any field containing structural fence markers (a sign the model leaked
  instruction-frame content back into its response)
- any field containing bidi controls

Findings persist to the `extraction_anomalies` table (created in C2.0).
The operator reviews via OperatorTerminal.

Per [D-100](../../01_Project_Overview/DECISIONS.md#d-100), this module
contains only defensive enumeration + structural rules.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Substrings that suggest prompt-leak content surfaced into an extraction
# output. A legitimate civic synopsis or agenda_transition never contains
# these phrasings.
_INSTRUCTION_PATTERNS = (
    "ignore previous",
    "ignore all previous",
    "disregard above",
    "system prompt",
    "you are now",
    "act as ",
    "new instructions:",
    "override your",
    "previous instructions",
    "as an ai language model",
)

# Code/shell-shaped substrings that should not appear in civic extraction.
_CODE_PATTERNS = (
    "<script",
    "javascript:",
    "data:text/html",
    "$(",
)

_FENCE_MARKERS = (
    "<zspan-content-begin",
    "<zspan-content-end",
)

_BIDI_CHARS = frozenset(
    chr(cp) for cp in (
        0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
        0x2066, 0x2067, 0x2068, 0x2069,
    )
)

# Crude URL pattern. Same shape as input_security.primitives uses.
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.IGNORECASE)


@dataclass
class ExtractionAnomaly:
    """One finding from the post-extraction rule pass.

    Suitable for persisting to extraction_anomalies via
    `persist_anomalies()` or for operator-side review via
    OperatorTerminal.
    """

    output_type: str
    anomaly_kind: str
    anomaly_detail: str
    raw_excerpt: str = ""
    meeting_id: Optional[int] = None
    notebook_output_id: Optional[int] = None


@dataclass
class PostcheckContext:
    """Optional context the rule pass uses to assess findings.

    All fields are optional; when context is missing the corresponding
    rule degrades to a less-strict check (e.g., URL allowlist check
    skipped when source_urls is unknown).
    """

    meeting_id: Optional[int] = None
    notebook_output_id: Optional[int] = None
    source_urls: Optional[frozenset[str]] = None
    roster_speakers: Optional[frozenset[str]] = field(default=None)


def _excerpt(text: str, span: int = 120) -> str:
    if not isinstance(text, str):
        return repr(text)[:span]
    cleaned = text.strip()
    if len(cleaned) <= span:
        return cleaned
    return cleaned[: span - 1] + "…"


def _walk_strings(obj: Any):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)


def _extract_urls(text: str) -> list[str]:
    if not isinstance(text, str):
        return []
    return _URL_RE.findall(text)


def _check_fence_markers(
    output_type: str, payload: Any
) -> list[ExtractionAnomaly]:
    findings: list[ExtractionAnomaly] = []
    for s in _walk_strings(payload):
        lowered = s.lower()
        for marker in _FENCE_MARKERS:
            if marker in lowered:
                findings.append(ExtractionAnomaly(
                    output_type=output_type,
                    anomaly_kind="fence_marker_in_extraction",
                    anomaly_detail=(
                        f"structural fence marker {marker!r} surfaced in "
                        f"{output_type} extraction"
                    ),
                    raw_excerpt=_excerpt(s),
                ))
                break
    return findings


def _check_instruction_patterns(
    output_type: str, payload: Any
) -> list[ExtractionAnomaly]:
    findings: list[ExtractionAnomaly] = []
    for s in _walk_strings(payload):
        lowered = s.lower()
        for pat in _INSTRUCTION_PATTERNS:
            if pat in lowered:
                findings.append(ExtractionAnomaly(
                    output_type=output_type,
                    anomaly_kind="instruction_pattern_in_extraction",
                    anomaly_detail=(
                        f"instruction-pattern {pat!r} present in "
                        f"{output_type} extraction"
                    ),
                    raw_excerpt=_excerpt(s),
                ))
                break
    return findings


def _check_code_patterns(
    output_type: str, payload: Any
) -> list[ExtractionAnomaly]:
    findings: list[ExtractionAnomaly] = []
    for s in _walk_strings(payload):
        lowered = s.lower()
        for pat in _CODE_PATTERNS:
            if pat in lowered:
                findings.append(ExtractionAnomaly(
                    output_type=output_type,
                    anomaly_kind="code_pattern_in_extraction",
                    anomaly_detail=(
                        f"code-shaped substring {pat!r} present in "
                        f"{output_type} extraction"
                    ),
                    raw_excerpt=_excerpt(s),
                ))
                break
    return findings


def _check_bidi(output_type: str, payload: Any) -> list[ExtractionAnomaly]:
    findings: list[ExtractionAnomaly] = []
    for s in _walk_strings(payload):
        if any(ch in _BIDI_CHARS for ch in s):
            findings.append(ExtractionAnomaly(
                output_type=output_type,
                anomaly_kind="bidi_control_in_extraction",
                anomaly_detail=(
                    f"bidi-control characters surfaced in {output_type} "
                    f"extraction"
                ),
                raw_excerpt=_excerpt(s),
            ))
            break
    return findings


def _check_urls_not_in_source(
    output_type: str,
    payload: Any,
    source_urls: Optional[frozenset[str]],
) -> list[ExtractionAnomaly]:
    """quote_text + tracked_claims shouldn't include URLs that aren't in
    the meeting's source context. Skipped when source_urls is unknown."""
    if source_urls is None:
        return []
    findings: list[ExtractionAnomaly] = []
    for s in _walk_strings(payload):
        for url in _extract_urls(s):
            if url not in source_urls and url.rstrip("/") not in source_urls:
                findings.append(ExtractionAnomaly(
                    output_type=output_type,
                    anomaly_kind="url_not_in_source",
                    anomaly_detail=(
                        f"URL {url!r} surfaced in {output_type} extraction "
                        f"but is not in the meeting's source-context URL set"
                    ),
                    raw_excerpt=_excerpt(s),
                ))
                # one per string is sufficient signal
                break
    return findings


def _check_unrostered_speakers(
    output_type: str,
    payload: Any,
    roster: Optional[frozenset[str]],
) -> list[ExtractionAnomaly]:
    """tracked_claims + quotes that attribute statements to speakers not
    on the meeting's roster are suspicious. Skipped when roster is unknown."""
    if roster is None:
        return []
    findings: list[ExtractionAnomaly] = []
    if not isinstance(payload, dict):
        return findings

    # Common keys carrying speaker attribution in known output types.
    speaker_keys = ("speaker", "speaker_name", "attributed_to", "by")

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if key in speaker_keys and isinstance(val, str) and val.strip():
                    if val not in roster and val.lower() not in {
                        r.lower() for r in roster
                    }:
                        findings.append(ExtractionAnomaly(
                            output_type=output_type,
                            anomaly_kind="unrostered_speaker_in_extraction",
                            anomaly_detail=(
                                f"speaker {val!r} attributed in {output_type} "
                                f"is not in the meeting's roster"
                            ),
                            raw_excerpt=_excerpt(val),
                        ))
                else:
                    _walk(val)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(payload)
    return findings


def run_extraction_postcheck(
    output_type: str,
    payload: Any,
    context: Optional[PostcheckContext] = None,
) -> list[ExtractionAnomaly]:
    """Run the full deterministic rule pass over a parsed extraction payload.

    Args:
        output_type: the OUTPUT_TYPE_REGISTRY key (synopsis, quotes,
            tracked_claims, agenda_transitions, etc.). Stamped onto every
            finding.
        payload: the parsed JSON object (dict or list) the extraction
            produced. Already validated against the per-output-type schema
            by upstream parser strict-mode.
        context: optional PostcheckContext providing meeting_id +
            source_urls + roster_speakers for context-sensitive checks.

    Returns:
        A list of ExtractionAnomaly findings. Empty list = clean.
    """
    ctx = context or PostcheckContext()
    findings: list[ExtractionAnomaly] = []

    findings.extend(_check_fence_markers(output_type, payload))
    findings.extend(_check_instruction_patterns(output_type, payload))
    findings.extend(_check_code_patterns(output_type, payload))
    findings.extend(_check_bidi(output_type, payload))
    findings.extend(
        _check_urls_not_in_source(output_type, payload, ctx.source_urls)
    )
    findings.extend(
        _check_unrostered_speakers(output_type, payload, ctx.roster_speakers)
    )

    # Stamp meeting + notebook_output IDs onto every finding.
    for f in findings:
        f.meeting_id = ctx.meeting_id
        f.notebook_output_id = ctx.notebook_output_id

    return findings


def persist_anomalies(
    findings: list[ExtractionAnomaly],
) -> int:
    """Best-effort persistence to the extraction_anomalies table.

    Returns the count of rows successfully inserted. Failures are logged
    but never raise — the audit is observability, not gating.
    """
    if not findings:
        return 0
    try:
        from parsers import database  # noqa: PLC0415
        conn = database.get_connection()
        cursor = conn.cursor()
        inserted = 0
        for f in findings:
            cursor.execute(
                """
                INSERT INTO extraction_anomalies (
                    meeting_id, notebook_output_id, output_type,
                    anomaly_kind, anomaly_detail, raw_excerpt
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    f.meeting_id,
                    f.notebook_output_id,
                    f.output_type,
                    f.anomaly_kind,
                    f.anomaly_detail,
                    f.raw_excerpt,
                ),
            )
            inserted += 1
        conn.commit()
        conn.close()
        return inserted
    except Exception as e:
        logger.warning(
            "extraction_postcheck.persist_anomalies failed: %s", e,
        )
        return 0
