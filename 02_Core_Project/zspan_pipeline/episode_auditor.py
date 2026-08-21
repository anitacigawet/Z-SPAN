#!/usr/bin/env python3.11
"""Audit-only observer for one meeting's published episode outputs.

This module may persist observations only to ``episode_audit_runs``. It never
changes generated outputs, meeting state, work orders, or publication state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


logger = logging.getLogger(__name__)


_PROJECT_DIR = Path(__file__).resolve().parent.parent
_PARSERS_DIR = _PROJECT_DIR / "council_navigator" / "parsers"
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

from zspan_pipeline.db_backend import install_db_backend  # noqa: E402

install_db_backend()

from database import (  # noqa: E402
    get_meeting_with_notebook,
    save_episode_audit_run,
)
from zspan_pipeline import local_vector_store  # noqa: E402
from zspan_pipeline.citation_validator import allowed_seconds  # noqa: E402
from zspan_pipeline.cluster_roster_mapper import _load_roster  # noqa: E402
from zspan_pipeline.output_contracts import (  # noqa: E402
    FLAGSHIP_PRODUCTION_CONTRACT,
    HONEST_EMPTY_OUTPUTS,
)
from zspan_pipeline.qdrant_synthesizer import (  # noqa: E402
    NO_TIMEOUT,
    PREVIEW_DIR,
    SONNET_MODEL_ID,
    synthesize_via_claude_p,
)
from zspan_pipeline.transcript_quarantine import (  # noqa: E402
    EntropyConfig,
    is_quarantined_word,
    profile_token_entropy,
)


AUDITOR_VERSION = "episode-auditor-v2"
AUDIT_MODEL_ID = SONNET_MODEL_ID
AUDIT_EFFORT = "max"
SEGMENTATION_WORD_THRESHOLD = 55_000

OUTPUT_ORDER: tuple[str, ...] = (
    "episode_tagline",
    "synopsis",
    "newsletter",
    "key_decisions",
    "whats_next",
    "council_sentiment",
    "tracked_claims",
    "community_calls_to_action",
)
if frozenset(OUTPUT_ORDER) != FLAGSHIP_PRODUCTION_CONTRACT:
    raise RuntimeError(
        "episode auditor output order has drifted from "
        "FLAGSHIP_PRODUCTION_CONTRACT"
    )

AUDIT_PROMPT_TEMPLATE = """You are the episode auditor for Z-SPAN, a virtual library for local politics. A city-council meeting was processed into published outputs (synopsis, tagline, newsletter, key decisions, what's next, council sentiment, tracked claims, community calls to action). Your mission: protect the integrity of a public civic record. Find anything that would make this episode wrong, unfair, unsupported, or harmful to a private person. Think as deeply as you need to; take your full reasoning capacity.

You receive the COMPLETE meeting transcript (timecoded), the same complete evidence available to the generator, and every output.

Known failure families from our history — these are a FLOOR for your attention, never a ceiling:
1. Omission or misframing: something consequential in the transcript missing or distorted in the outputs.
2. Citation support: an [at H:MM:SS] citation whose transcript moment does not actually support the claim beside it (a citation can be temporally valid yet evidentially wrong).
3. Private citizens: any private person (public commenter, audience member, non-official) named, quoted, paraphrased individually, or identifiable in any output. Officials and staff in official capacity are fine.
4. Transcription artifacts: stretches that read like speech-recognition hallucination (looping phrases, coherent-but-alien passages) that leaked into outputs as fact.
5. Cross-output contradiction: outputs disagreeing on dates, tallies, dollar amounts, outcomes, or substance.
6. Neutrality: editorializing, loaded framing, or advocacy in what must be a neutral civic record.

Rules of evidence:
- Every finding must cite its evidence: quote the output text AND the transcript timecode/passage that grounds your judgment.
- Label your certainty honestly per finding: CONFIRMED (evidence conclusive) or SUSPECTED (worth a human look; say what is uncertain).
- ZERO findings is a fully acceptable answer. Do not manufacture suspicion to appear useful. A clean episode reported clean is a success.
- If something is uncheckable from what you were given, say UNCHECKABLE and why — never guess.

Proposed fixes:
- For each CONFIRMED finding whose correction is one or more single localized text edits, append to that finding one PROPOSED_FIX block per edit, in EXACTLY this shape:
PROPOSED_FIX
target_output: <output type>
before: <<<the exact text currently in that output, copied character-for-character, long enough to be unique within it>>>
after: <<<the corrected replacement text>>>
fix_rationale: <one line>
- The before text must be an exact copy from the output under audit — never paraphrase, trim punctuation, or normalize it. Keep each edit minimal: change only what the finding's evidence proves wrong.
- If a CONFIRMED finding cannot be fixed by localized edits — it would require removing a whole item, resolving an open question first, restructuring an output, or judgment beyond the cited evidence — write NO_SAFE_PROPOSAL and one line why, instead of guessing.
- SUSPECTED findings never carry proposals.

Report in these sections (prose welcome; number findings so each is individually addressable):
FINDINGS — numbered, per-family-tagged, with evidence + certainty labels. Or "none".
OPEN_FINDINGS — anything outside the six families you judge worth attention. Or "none".
SUGGESTIONS — open-ended: anything you want to tell us — prompt improvements, pipeline observations, new check ideas, anything at all. This feeds an iterative learning corpus.
VERDICT — one line: publishable-as-is / publishable-with-noted-flags / needs-human-review.
"""

_PROMPT_SHA256 = hashlib.sha256(AUDIT_PROMPT_TEMPLATE.encode("utf-8")).hexdigest()
_CITATION_RE = re.compile(
    r"\[at\s+(?:(?P<hours>\d+):)?(?P<minutes>\d{1,2}):(?P<seconds>\d{2})\]",
    re.IGNORECASE,
)
_SYNOPSIS_ANCHOR_AUDIT_HEADER_RE = re.compile(
    r"^[^\S\r\n]*<!--[^\S\r\n]*synopsis_anchor_audit\s+"
    r"(?P<version>v\d+)\b",
    re.IGNORECASE | re.MULTILINE,
)
_SYNOPSIS_ANCHOR_AUDIT_FOOTER_RE = re.compile(
    r"^[^\S\r\n]*audit[^\S\r\n]*-->[^\S\r\n]*\Z",
    re.IGNORECASE | re.MULTILINE,
)
_SYNOPSIS_ANCHOR_FAILURE_STATES = frozenset(
    {"degraded", "nonconforming", "uncheckable"}
)
_SECTION_HEADER_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?"
    r"(?P<header>FINDINGS|OPEN[_ -]FINDINGS|SUGGESTIONS|VERDICT)"
    r"\s*(?:(?:—|:|-)\s*(?P<inline>.*))?\s*$",
    re.IGNORECASE,
)
_ITEM_MARKER_RE = re.compile(
    r"^[^\S\r\n]*(?:(?:[->][^\S\r\n]+)|\*{1,2}[^\S\r\n]*)*"
    r"(?:\d+[.)]|finding[^\S\r\n]+\d+|[os]\d+)"
    r"(?=(?:\*{1,2})?(?:[^\S\r\n]|—|$))",
    re.IGNORECASE | re.MULTILINE,
)
_NONE_EQUIVALENT_RE = re.compile(
    r"^[^\S\r\n]*(?:(?:[->][^\S\r\n]+)|\*{1,2}[^\S\r\n]*)*"
    r"none\.?[^\S\r\n]*\*{0,2}[^\S\r\n]*$",
    re.IGNORECASE,
)
_PROPOSAL_START_RE = re.compile(
    r"^[^\S\r\n]*(?:[*_]{1,3})?PROPOSED_FIX(?:[*_]{1,3})?"
    r"[^\S\r\n]*(?::|—|-)?[^\S\r\n]*$",
    re.IGNORECASE | re.MULTILINE,
)
_NO_SAFE_PROPOSAL_RE = re.compile(
    r"^[^\S\r\n]*(?:[*_]{1,3})?NO_SAFE_PROPOSAL(?:[*_]{1,3})?"
    r"[^\S\r\n]*(?:(?::|—|-)[^\S\r\n]*(?P<inline>.*))?$",
    re.IGNORECASE | re.MULTILINE,
)
_PROPOSAL_FIELD_RE = re.compile(
    r"^[^\S\r\n]*(?:[*_]{1,3})?"
    r"(?P<field>target_output|before|after|fix_rationale)"
    r"(?:[*_]{1,3})?[^\S\r\n]*:[^\S\r\n]*(?P<value>.*)$",
    re.IGNORECASE | re.MULTILINE,
)
_EVIDENCE_NUMBER_RE = re.compile(
    r"(?<![\w.])\$?\d[\d,]*(?:\.\d+)?%?(?![\w.])"
)
_EVIDENCE_DATE_RE = re.compile(
    r"(?<!\d)\d{1,2}/\d{1,2}(?:/\d{2,4})?(?!\d)"
)
_APPLY_GATED_OUTPUTS = frozenset(
    {"key_decisions", "tracked_claims", "community_calls_to_action"}
)
_NAME_TOKEN_RE = re.compile(r"\b[A-Z][a-z]+(?:[-'][A-Z]?[a-z]+)?\b")
_CIVIC_NAME_STOPS = frozenset(
    {
        "Agenda",
        "Board",
        "Budget",
        "Chair",
        "City",
        "Committee",
        "Community",
        "Council",
        "County",
        "Decision",
        "Decisions",
        "Department",
        "Director",
        "District",
        "Episode",
        "Finding",
        "Findings",
        "Mayor",
        "Meeting",
        "Minutes",
        "Newsletter",
        "Office",
        "Open",
        "Public",
        "Staff",
        "State",
        "Suggestion",
        "Suggestions",
        "Synopsis",
        "Town",
        "Tracked",
        "Verdict",
    }
)


@dataclass(frozen=True)
class AuditInputs:
    meeting_id: int
    meeting: Mapping[str, Any]
    outputs: Mapping[str, str]
    output_row_ids: Mapping[str, Any]
    missing_outputs: tuple[str, ...]
    transcript_words: tuple[Mapping[str, Any], ...]
    outputs_snapshot_hash: str


def _content_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def compute_outputs_snapshot_hash(outputs: Mapping[str, str]) -> str:
    """Hash sorted output type/content pairs with explicit NUL separators."""
    digest = hashlib.sha256()
    for output_type, content in sorted(outputs.items()):
        digest.update(output_type.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(content.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _load_meeting_outputs(
    meeting_id: int,
) -> tuple[Mapping[str, Any], dict[str, str], dict[str, Any], tuple[str, ...]]:
    meeting = get_meeting_with_notebook(meeting_id)
    if meeting is None:
        raise LookupError(f"meeting_not_found:{meeting_id}")
    cached = meeting.get("notebook_outputs") or {}
    if not isinstance(cached, Mapping):
        cached = {}

    outputs: dict[str, str] = {}
    row_ids: dict[str, Any] = {}
    for output_type in OUTPUT_ORDER:
        row = cached.get(output_type)
        if not isinstance(row, Mapping):
            continue
        outputs[output_type] = _content_text(row.get("content"))
        row_ids[output_type] = row.get("id")
    missing = tuple(
        output_type for output_type in OUTPUT_ORDER if output_type not in outputs
    )
    return meeting, outputs, row_ids, missing


def load_audit_inputs(meeting_id: int) -> AuditInputs:
    """Load the canonical meeting/output/transcript read surfaces."""
    meeting, outputs, row_ids, missing = _load_meeting_outputs(meeting_id)
    transcript = local_vector_store.load_transcript_words(meeting_id)
    words = transcript["words"]
    return AuditInputs(
        meeting_id=meeting_id,
        meeting=meeting,
        outputs=outputs,
        output_row_ids=row_ids,
        missing_outputs=missing,
        transcript_words=tuple(words),
        outputs_snapshot_hash=compute_outputs_snapshot_hash(outputs),
    )


def _format_timecode(total_seconds: float) -> str:
    whole_seconds = max(0, int(total_seconds))
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def render_timecoded_transcript(
    words: Sequence[Mapping[str, Any]],
    *,
    bucket_seconds: int = 15,
) -> list[str]:
    """Render non-quarantined transcript words in start-time buckets."""
    buckets: dict[int, list[str]] = {}
    for word in words:
        if is_quarantined_word(word):
            continue
        token = str(word.get("word") or "").strip()
        start = word.get("start")
        if (
            not token
            or not isinstance(start, (int, float))
            or isinstance(start, bool)
            or not math.isfinite(float(start))
            or float(start) < 0
        ):
            continue
        bucket_start = int(float(start) // bucket_seconds) * bucket_seconds
        buckets.setdefault(bucket_start, []).append(token)
    return [
        f"[{_format_timecode(bucket_start)}] {' '.join(tokens)}"
        for bucket_start, tokens in sorted(buckets.items())
    ]


def build_audit_prompt(
    meeting: Mapping[str, Any],
    outputs: Mapping[str, str],
    transcript_lines: Sequence[str],
    *,
    segment_note: str | None = None,
) -> str:
    """Build the validated prototype prompt plus deterministic input blocks."""
    parts = [AUDIT_PROMPT_TEMPLATE.rstrip()]
    if segment_note:
        parts.append(segment_note)
    parts.append("=== OUTPUTS UNDER AUDIT ===")
    for output_type in OUTPUT_ORDER:
        if output_type in outputs:
            parts.append(
                f"--- OUTPUT: {output_type} ---\n{outputs[output_type]}"
            )
    city = (
        meeting.get("city_name")
        or meeting.get("city")
        or meeting.get("municipality")
        or ""
    )
    title = (
        meeting.get("meeting_title")
        or meeting.get("title")
        or meeting.get("meeting_type")
        or "meeting"
    )
    date = meeting.get("meeting_date") or meeting.get("date") or ""
    parts.append(
        f"=== FULL TIMECODED TRANSCRIPT ({city} {title}, {date}) ==="
    )
    parts.append("\n".join(transcript_lines))
    return "\n\n".join(parts)


def _section_items(body: str) -> list[str]:
    matches = list(_ITEM_MARKER_RE.finditer(body))
    items: list[str] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        items.append(body[match.start():end].strip())
    return items


def _proposal_field_value(
    raw_value: str,
    *,
    delimited: bool,
) -> tuple[str, bool]:
    if not delimited:
        return raw_value.strip(), True
    candidate = raw_value.lstrip()
    if candidate.startswith("<<<"):
        closing = candidate.rfind(">>>")
        if closing >= 3 and not candidate[closing + 3:].strip():
            return candidate[3:closing], True
    fallback = candidate.strip()
    if fallback.startswith("<<<"):
        fallback = fallback[3:]
    if fallback.endswith(">>>"):
        fallback = fallback[:-3]
    return fallback.strip(), False


def _parse_proposal_block(
    raw_block: str,
    *,
    finding_number: int,
    sequence: int,
) -> dict[str, Any]:
    start = _PROPOSAL_START_RE.search(raw_block)
    body = raw_block[start.end():] if start else raw_block
    field_matches = list(_PROPOSAL_FIELD_RE.finditer(body))
    values: dict[str, str] = {}
    delimiters: dict[str, bool] = {}
    for index, match in enumerate(field_matches):
        field = match.group("field").casefold()
        end = (
            field_matches[index + 1].start()
            if index + 1 < len(field_matches)
            else len(body)
        )
        raw_value = body[match.start("value"):end].rstrip()
        value, delimiter_ok = _proposal_field_value(
            raw_value,
            delimited=field in {"before", "after"},
        )
        values[field] = value
        if field in {"before", "after"}:
            delimiters[field] = delimiter_ok

    required = {"target_output", "before", "after", "fix_rationale"}
    parse_ok = (
        required.issubset(values)
        and len(field_matches) == len(required)
    )
    proposal: dict[str, Any] = {
        "id": f"p{finding_number}.{sequence}",
        "finding_number": finding_number,
        "target_output": values.get("target_output", ""),
        "before": values.get("before", ""),
        "after": values.get("after", ""),
        "fix_rationale": values.get("fix_rationale", ""),
        "delimiters_ok": (
            delimiters.get("before", False)
            and delimiters.get("after", False)
        ),
        "parse_ok": parse_ok,
    }
    if not parse_ok:
        proposal["raw_block"] = raw_block
    return proposal


def _finding_proposals(
    finding: str,
    finding_number: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    proposal_starts = list(_PROPOSAL_START_RE.finditer(finding))
    no_safe_starts = list(_NO_SAFE_PROPOSAL_RE.finditer(finding))
    boundaries = sorted(
        [*proposal_starts, *no_safe_starts],
        key=lambda match: match.start(),
    )
    boundary_ends = {
        match.start(): (
            boundaries[index + 1].start()
            if index + 1 < len(boundaries)
            else len(finding)
        )
        for index, match in enumerate(boundaries)
    }

    proposals = [
        _parse_proposal_block(
            finding[match.start():boundary_ends[match.start()]].strip(),
            finding_number=finding_number,
            sequence=sequence,
        )
        for sequence, match in enumerate(proposal_starts, 1)
    ]
    no_safe: list[dict[str, Any]] = []
    for match in no_safe_starts:
        reason = (match.group("inline") or "").strip()
        if not reason:
            remainder = finding[
                match.end():boundary_ends[match.start()]
            ].strip()
            reason = remainder.splitlines()[0].strip() if remainder else ""
        no_safe.append(
            {
                "finding_number": finding_number,
                "reason": reason,
            }
        )
    return proposals, no_safe


def parse_audit_response(response: str) -> dict[str, Any]:
    """Tolerantly parse auditor prose while keeping F8 failure semantics."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    recognized = 0
    for line in response.splitlines():
        match = _SECTION_HEADER_RE.match(line)
        if match:
            header = match.group("header").upper().replace("-", "_").replace(" ", "_")
            current = header
            sections.setdefault(header, [])
            recognized += 1
            inline = (match.group("inline") or "").strip()
            if inline:
                sections[header].append(inline)
            continue
        if current is not None:
            sections[current].append(line)

    if recognized == 0:
        return {
            "raw_response": response,
            "findings": [],
            "open_findings": [],
            "suggestions": [],
            "verdict_line": "",
            "proposals": [],
            "no_safe_proposals": [],
            "run_status": "parse_failed",
            "verdict": "incomplete",
            "recognizable_sections": 0,
        }

    def parsed_items(name: str) -> list[str]:
        body = "\n".join(sections.get(name, [])).strip()
        if not body or _NONE_EQUIVALENT_RE.fullmatch(body):
            return []
        return _section_items(body) or [body]

    verdict_body = "\n".join(sections.get("VERDICT", [])).strip()
    verdict_line = verdict_body.splitlines()[0].strip() if verdict_body else ""
    findings = parsed_items("FINDINGS")
    proposals: list[dict[str, Any]] = []
    no_safe_proposals: list[dict[str, Any]] = []
    for finding_number, finding in enumerate(findings, 1):
        finding_proposals, finding_no_safe = _finding_proposals(
            finding,
            finding_number,
        )
        proposals.extend(finding_proposals)
        no_safe_proposals.extend(finding_no_safe)
    return {
        "raw_response": response,
        "findings": findings,
        "open_findings": parsed_items("OPEN_FINDINGS"),
        "suggestions": parsed_items("SUGGESTIONS"),
        "verdict_line": verdict_line,
        "proposals": proposals,
        "no_safe_proposals": no_safe_proposals,
        "run_status": "complete",
        "verdict": "flags" if findings else "no_catches",
        "recognizable_sections": recognized,
    }


def _edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, 1):
        current = [left_index]
        for right_index, right_char in enumerate(right, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def _consonant_skeleton(value: str) -> str:
    return "".join(
        character
        for character in value.casefold()
        if character.isalpha() and character not in "aeiou"
    )


def _name_candidates(text: str) -> set[str]:
    tokens = list(_NAME_TOKEN_RE.finditer(text))
    candidates: set[str] = set()
    for index, token_match in enumerate(tokens):
        token = token_match.group()
        prefix = text[:token_match.start()].rstrip()
        sentence_start = not prefix or prefix[-1:] in ".!?\n"
        if token not in _CIVIC_NAME_STOPS and not sentence_start:
            candidates.add(token)
        if index + 1 >= len(tokens):
            continue
        next_match = tokens[index + 1]
        between = text[token_match.end():next_match.start()]
        if between and not between.isspace():
            continue
        next_token = next_match.group()
        if token in _CIVIC_NAME_STOPS or next_token in _CIVIC_NAME_STOPS:
            continue
        candidates.add(f"{token} {next_token}")
    return candidates


def check_entity_consistency(
    outputs: Mapping[str, str],
    city: str,
) -> dict[str, Any]:
    """Find cross-output spelling variants and annotate roster membership."""
    appearances: dict[str, set[str]] = {}
    for output_type, content in outputs.items():
        for candidate in _name_candidates(content):
            appearances.setdefault(candidate, set()).add(output_type)

    names = sorted(appearances)
    collisions: list[dict[str, Any]] = []
    single_token_variants: list[dict[str, Any]] = []
    for index, left in enumerate(names):
        for right in names[index + 1:]:
            if left.casefold() == right.casefold():
                continue
            if not any(
                left_output != right_output
                for left_output in appearances[left]
                for right_output in appearances[right]
            ):
                continue
            left_low = left.casefold()
            right_low = right.casefold()
            left_skeleton = _consonant_skeleton(left)
            right_skeleton = _consonant_skeleton(right)
            if (
                _edit_distance(left_low, right_low) != 1
                and not (
                    len(left_skeleton) >= 3
                    and left_skeleton == right_skeleton
                )
            ):
                continue
            variant = {
                "kind": "FLAG",
                "spellings": [left, right],
                "outputs": {
                    left: sorted(appearances[left]),
                    right: sorted(appearances[right]),
                },
            }
            if " " in left and " " in right:
                collisions.append(variant)
            else:
                variant["kind"] = "OBSERVATION"
                variant["classification"] = "single_token_variant"
                single_token_variants.append(variant)

    roster_status = "recorded"
    roster_error = ""
    roster_names: list[str] = []
    try:
        roster_names = [
            str(row.get("name") or "").strip()
            for row in _load_roster(city)
            if str(row.get("name") or "").strip()
        ]
    except Exception as exc:
        roster_status = "uncheckable"
        roster_error = str(exc)

    roster_tokens = {
        token.casefold()
        for roster_name in roster_names
        for token in roster_name.split()
    }
    roster_surnames = {
        roster_name.split()[-1].casefold()
        for roster_name in roster_names
        if roster_name.split()
    }
    non_roster = []
    if roster_status == "recorded":
        for name in names:
            parts = name.casefold().split()
            surname = parts[-1]
            if surname in roster_surnames or (
                len(parts) == 1 and parts[0] in roster_tokens
            ):
                continue
            non_roster.append(
                {
                    "name": name,
                    "outputs": sorted(appearances[name]),
                    "classification": "OBSERVATION",
                }
            )
    result: dict[str, Any] = {
        "status": "completed",
        "variant_collisions": collisions,
        "single_token_variants": single_token_variants,
        "non_roster_names": non_roster,
        "roster_status": roster_status,
        "roster_member_count": len(roster_names),
        "names_scanned": len(names),
    }
    if roster_error:
        result["roster_reason"] = roster_error
    return result


def _citation_seconds(match: re.Match[str]) -> int:
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    return hours * 3600 + minutes * 60 + seconds


def _transcript_end_seconds(words: Sequence[Mapping[str, Any]]) -> float | None:
    values: list[float] = []
    for word in words:
        for field in ("end", "start"):
            value = word.get(field)
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
                and float(value) >= 0
            ):
                values.append(float(value))
    return max(values) if values else None


def _load_local_chunk_starts(meeting_id: int) -> tuple[list[float], str]:
    with local_vector_store.connect() as conn:
        table = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'local_retrieval_chunks'
            """
        ).fetchone()
        if table is None:
            return [], "local_retrieval_chunks_table_absent"
        rows = conn.execute(
            """
            SELECT start_seconds
            FROM local_retrieval_chunks
            WHERE meeting_id = ?
            ORDER BY chunk_index
            """,
            (meeting_id,),
        ).fetchall()
    return [float(row["start_seconds"]) for row in rows], ""


def check_locator_existence(
    meeting_id: int,
    outputs: Mapping[str, str],
    transcript_words: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    transcript_end = _transcript_end_seconds(transcript_words)
    citations: list[dict[str, Any]] = []
    out_of_range: list[dict[str, Any]] = []
    for output_type, content in outputs.items():
        for match in _CITATION_RE.finditer(content):
            seconds = _citation_seconds(match)
            item = {
                "output_type": output_type,
                "citation": match.group(),
                "seconds": seconds,
            }
            citations.append(item)
            if transcript_end is not None and not (0 <= seconds <= transcript_end):
                out_of_range.append({**item, "kind": "FLAG"})

    chunk_status = "recorded"
    chunk_reason = ""
    not_chunk_start: list[dict[str, Any]] = []
    try:
        starts, chunk_reason = _load_local_chunk_starts(meeting_id)
        if not starts:
            chunk_status = "not_applicable"
        else:
            allowed = allowed_seconds(starts)
            not_chunk_start = [
                {**citation, "classification": "OBSERVATION"}
                for citation in citations
                if citation["seconds"] not in allowed
            ]
    except Exception as exc:
        starts = []
        chunk_status = "uncheckable"
        chunk_reason = str(exc)

    result: dict[str, Any] = {
        "status": "completed" if transcript_end is not None else "uncheckable",
        "reason": "" if transcript_end is not None else "no_transcript_timing",
        "transcript_end_seconds": transcript_end,
        "citations_checked": len(citations),
        "out_of_range": out_of_range,
        "chunk_start_status": chunk_status,
        "chunk_starts_count": len(starts),
        "not_a_chunk_start": not_chunk_start,
    }
    if chunk_reason:
        result["chunk_start_reason"] = chunk_reason
    return result


def _normalized_text(text: str) -> str:
    return " ".join(text.casefold().split())


def _quoted_evidence_after_timecode(finding: str) -> list[str]:
    quote_pattern = re.compile(r'"([^"]+)"|“([^”]+)”')
    passages: list[str] = []
    for match in quote_pattern.finditer(finding):
        passage = (match.group(1) or match.group(2)).strip()
        if len(passage) >= 25:
            passages.append(passage)
    return passages


def check_quote_existence(
    findings: Sequence[str],
    transcript_words: Sequence[Mapping[str, Any]],
    outputs: Mapping[str, str],
) -> dict[str, Any]:
    def normalize(text: str) -> str:
        alphanumeric_and_whitespace = "".join(
            character
            for character in text.casefold()
            if character.isalnum() or character.isspace()
        )
        return _normalized_text(alphanumeric_and_whitespace)

    transcript_text = normalize(
        " ".join(
            str(word.get("word") or "")
            for word in transcript_words
            if not is_quarantined_word(word)
        )
    )
    outputs_text = normalize(" ".join(outputs.values()))
    checks: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for finding_index, finding in enumerate(findings, 1):
        for quote in _quoted_evidence_after_timecode(finding):
            fragments = [
                normalized
                for fragment in re.split(r"\.{3}|…", quote)
                if len(normalized := normalize(fragment)) >= 25
            ]
            if not fragments:
                continue
            if any(fragment in transcript_text for fragment in fragments):
                matched_in = "transcript"
            elif any(fragment in outputs_text for fragment in fragments):
                matched_in = "outputs"
            else:
                matched_in = "none"
            item = {
                "finding_number": finding_index,
                "quote": quote,
                "found": matched_in != "none",
                "matched_in": matched_in,
            }
            checks.append(item)
            if matched_in == "none":
                missing.append(
                    {
                        **item,
                        "kind": "FLAG",
                        "flag": "llm_evidence_not_found",
                    }
                )
    return {
        "status": "completed" if transcript_words or outputs else "uncheckable",
        "reason": "" if transcript_words or outputs else "no_evidence_corpora",
        "quotes_checked": checks,
        "llm_evidence_not_found": missing,
    }


def check_provenance(
    meeting_id: int,
    *,
    preview_dir: Path | None = None,
) -> dict[str, Any]:
    directory = preview_dir or PREVIEW_DIR
    path = directory / f"m{meeting_id}_synthesis_provenance.json"
    if not path.exists():
        return {"status": "uncheckable", "reason": "provenance_absent"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "uncheckable",
            "reason": "provenance_corrupt",
            "error": str(exc),
        }

    output_payload = payload.get("outputs", payload) if isinstance(payload, dict) else {}
    if isinstance(output_payload, list):
        output_payload = {
            str(item.get("output_type") or ""): item
            for item in output_payload
            if isinstance(item, Mapping) and item.get("output_type")
        }
    recorded: dict[str, dict[str, bool]] = {}
    for output_type in OUTPUT_ORDER:
        item = (
            output_payload.get(output_type)
            if isinstance(output_payload, Mapping)
            else None
        )
        chunk_ids = None
        if isinstance(item, Mapping):
            chunk_ids = (
                item.get("chunk_ids")
                or item.get("retrieved_chunk_ids")
                or item.get("retrieval_chunk_ids")
                or item.get("chunk_indices")
            )
        recorded[output_type] = {
            "prompt_sha256_present": (
                bool(item.get("prompt_sha256"))
                if isinstance(item, Mapping)
                else False
            ),
            "chunk_ids_present": isinstance(chunk_ids, list) and bool(chunk_ids),
        }
    return {"status": "recorded", "outputs": recorded}


def _parse_json_content(content: str) -> Any:
    stripped = content.strip()
    fence = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```",
        stripped,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return json.loads(fence.group(1) if fence else stripped)


def check_valid_empty(outputs: Mapping[str, str]) -> dict[str, Any]:
    valid: list[str] = []
    for output_type in HONEST_EMPTY_OUTPUTS:
        if output_type not in outputs:
            continue
        content = outputs[output_type]
        if not content.strip():
            valid.append(output_type)
            continue
        try:
            parsed = _parse_json_content(content)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if parsed == [] or (
            isinstance(parsed, Mapping)
            and any(
                parsed.get(key) == []
                for key in (
                    output_type,
                    "calls_to_action",
                    "community_calls_to_action",
                )
            )
        ):
            valid.append(output_type)

    key_decisions_state = "not_empty"
    content = outputs.get("key_decisions")
    if content is not None:
        try:
            parsed = _parse_json_content(content)
            if parsed == [] or (
                isinstance(parsed, Mapping)
                and (
                    parsed.get("decisions") == []
                    or parsed.get("key_decisions") == []
                )
            ):
                key_decisions_state = "valid_empty"
                valid.append("key_decisions")
        except (TypeError, ValueError, json.JSONDecodeError):
            key_decisions_state = "not_well_formed"
    return {
        "status": "completed",
        "valid_empty": sorted(set(valid)),
        "key_decisions": key_decisions_state,
        "flags_count": 0,
    }


def _parse_synopsis_anchor_audit(content: str) -> dict[str, Any] | None:
    """Parse the final code-generated synopsis anchor audit v1 block."""
    header = None
    for match in _SYNOPSIS_ANCHOR_AUDIT_HEADER_RE.finditer(content):
        header = match
    if header is None:
        return None
    version = header.group("version").casefold()
    if version != "v1":
        raise ValueError(
            f"synopsis_anchor_audit_unsupported_version:{version}"
        )
    footer = _SYNOPSIS_ANCHOR_AUDIT_FOOTER_RE.search(content, header.end())
    if footer is None:
        raise ValueError("synopsis_anchor_audit_footer_missing")
    payload_text = content[header.end():footer.start()].strip()
    if not payload_text:
        raise ValueError("synopsis_anchor_audit_payload_missing")
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise ValueError("synopsis_anchor_audit_invalid_json") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("synopsis_anchor_audit_payload_not_object")
    return dict(payload)


def _synopsis_anchor_check_result(
    resolution_state: str,
    anchors_total: int,
    failures_count: int,
    *,
    status: str,
    error: str = "",
) -> dict[str, Any]:
    flags: list[dict[str, Any]] = []
    if resolution_state in _SYNOPSIS_ANCHOR_FAILURE_STATES:
        reason = f"synopsis_anchor_failure:{resolution_state}"
        flags.append(
            {
                "kind": "FLAG",
                "flag": reason,
                "reason": reason,
                "resolution_state": resolution_state,
                "anchors_total": anchors_total,
                "failures_count": failures_count,
            }
        )
    result: dict[str, Any] = {
        "status": status,
        "resolution_state": resolution_state,
        "anchors_total": anchors_total,
        "failures_count": failures_count,
        "flags": flags,
    }
    if error:
        result["error"] = error
    logger.info(
        "synopsis_anchor_audit resolution_state=%s anchors_total=%d failures=%d",
        resolution_state,
        anchors_total,
        failures_count,
    )
    return result


def check_synopsis_anchor_audit(
    outputs: Mapping[str, str],
) -> dict[str, Any]:
    """Turn the stored synopsis anchor audit state into one deterministic flag."""
    content = outputs.get("synopsis")
    if not isinstance(content, str):
        return _synopsis_anchor_check_result(
            "absent", 0, 0, status="not_applicable"
        )
    try:
        payload = _parse_synopsis_anchor_audit(content)
        if payload is None:
            return _synopsis_anchor_check_result(
                "absent", 0, 0, status="not_applicable"
            )
        resolution_state = payload.get("resolution_state")
        if resolution_state not in {
            "resolved",
            "degraded",
            "nonconforming",
            "uncheckable",
        }:
            raise ValueError("synopsis_anchor_audit_invalid_resolution_state")
        anchors_total = payload.get("anchors_total")
        if (
            not isinstance(anchors_total, int)
            or isinstance(anchors_total, bool)
            or anchors_total < 0
        ):
            raise ValueError("synopsis_anchor_audit_invalid_anchors_total")
        failures = payload.get("failures")
        if not isinstance(failures, list):
            raise ValueError("synopsis_anchor_audit_invalid_failures")
        aligned = payload.get("aligned")
        if not isinstance(aligned, list):
            raise ValueError("synopsis_anchor_audit_invalid_aligned")
        if len(aligned) + len(failures) != anchors_total:
            raise ValueError("synopsis_anchor_audit_inconsistent_counts")
        if resolution_state == "resolved" and (
            anchors_total == 0 or failures
        ):
            raise ValueError("synopsis_anchor_audit_invalid_resolved_state")
        return _synopsis_anchor_check_result(
            resolution_state,
            anchors_total,
            len(failures),
            status="completed",
        )
    except (TypeError, ValueError) as exc:
        return _synopsis_anchor_check_result(
            "uncheckable",
            0,
            0,
            status="uncheckable",
            error=str(exc),
        )


def _outputs_without_synopsis_anchor_audit(
    outputs: Mapping[str, str],
) -> dict[str, str]:
    """Exclude hidden anchor metadata from published-prose grounding checks."""
    cleaned = dict(outputs)
    synopsis = cleaned.get("synopsis")
    if not isinstance(synopsis, str):
        return cleaned
    header = None
    for match in _SYNOPSIS_ANCHOR_AUDIT_HEADER_RE.finditer(synopsis):
        header = match
    if header is not None:
        cleaned["synopsis"] = synopsis[:header.start()].rstrip()
    return cleaned


def _evidence_tokens(text: str) -> dict[str, str]:
    tokens: dict[str, str] = {}
    date_spans: list[tuple[int, int]] = []
    citation_spans = [match.span() for match in _CITATION_RE.finditer(text)]

    def inside_citation(start: int, end: int) -> bool:
        return any(
            citation_start <= start and end <= citation_end
            for citation_start, citation_end in citation_spans
        )

    for match in _EVIDENCE_DATE_RE.finditer(text):
        if inside_citation(*match.span()):
            continue
        token = match.group()
        tokens.setdefault(token.casefold(), token)
        date_spans.append(match.span())
    for match in _EVIDENCE_NUMBER_RE.finditer(text):
        if inside_citation(*match.span()):
            continue
        if any(
            start <= match.start() and match.end() <= end
            for start, end in date_spans
        ):
            continue
        token = match.group()
        tokens.setdefault(token.casefold(), token)
    for token in _name_candidates(text):
        tokens.setdefault(token.casefold(), token)
    return tokens


def _collision_keys(result: Mapping[str, Any]) -> set[tuple[str, ...]]:
    return {
        tuple(
            sorted(
                (
                    str(spelling).casefold()
                    for spelling in collision.get("spellings", [])
                )
            )
        )
        for collision in result.get("variant_collisions", [])
        if isinstance(collision, Mapping)
    }


def validate_proposals(
    proposals: Sequence[Mapping[str, Any]],
    inputs: AuditInputs | Mapping[str, str],
    transcript_words: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Validate proposed single replacements without mutating stored outputs."""
    if isinstance(inputs, AuditInputs):
        outputs = inputs.outputs
        meeting = inputs.meeting
    else:
        outputs = inputs
        meeting = {}
    city = str(
        meeting.get("city_name")
        or meeting.get("city")
        or meeting.get("municipality")
        or ""
    )
    transcript_text = " ".join(
        str(word.get("word") or "") for word in transcript_words
    )
    roster_names: list[str] = []
    try:
        roster_names = [
            str(row.get("name") or "").strip()
            for row in _load_roster(city)
            if str(row.get("name") or "").strip()
        ]
    except Exception:
        roster_names = []
    supported_tokens = _evidence_tokens(
        f"Transcript: {transcript_text}\nRoster: {'; '.join(roster_names)}"
    )
    transcript_end = _transcript_end_seconds(transcript_words)
    try:
        original_collisions = _collision_keys(
            check_entity_consistency(outputs, city)
        )
    except Exception:
        original_collisions = set()

    validated_proposals: list[dict[str, Any]] = []
    for source in proposals:
        proposal = dict(source)
        errors: list[str] = []
        checks: dict[str, bool] = {}

        parse_ok = source.get("parse_ok", True) is True
        checks["parse_ok"] = parse_ok
        if not parse_ok:
            errors.append("parse_failed")

        target_output = source.get("target_output")
        before = source.get("before")
        after = source.get("after")
        target_known = (
            isinstance(target_output, str)
            and target_output in outputs
        )
        checks["target_known"] = target_known
        if not target_known:
            errors.append("target_unknown")

        current = outputs.get(target_output, "") if target_known else ""
        before_unique = (
            target_known
            and isinstance(before, str)
            and bool(before)
            and current.count(before) == 1
        )
        checks["before_unique"] = before_unique
        if not before_unique:
            errors.append("before_not_unique")

        changed = (
            isinstance(before, str)
            and isinstance(after, str)
            and bool(before)
            and bool(after)
            and before != after
        )
        checks["changed"] = changed
        if not isinstance(before, str) or not before:
            errors.append("empty_before")
        if not isinstance(after, str) or not after:
            errors.append("empty_after")
        elif before == after:
            errors.append("unchanged")

        candidate: str | None = None
        if before_unique and isinstance(after, str):
            candidate = current.replace(before, after, 1)

        structure_ok = candidate is not None
        if candidate is not None:
            try:
                _parse_json_content(current)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            else:
                try:
                    _parse_json_content(candidate)
                except (TypeError, ValueError, json.JSONDecodeError):
                    structure_ok = False
                    errors.append("structure_invalid")
        checks["structure_ok"] = structure_ok

        citations_ok = candidate is not None
        if candidate is not None:
            candidate_citations = list(_CITATION_RE.finditer(candidate))
            for match in candidate_citations:
                seconds = _citation_seconds(match)
                if transcript_end is None or not (
                    0 <= seconds <= transcript_end
                ):
                    citations_ok = False
                    errors.append(
                        f"citation_out_of_range: {match.group()}"
                    )
            before_citations = len(_CITATION_RE.findall(str(before or "")))
            after_citations = len(_CITATION_RE.findall(str(after or "")))
            if after_citations < before_citations:
                citations_ok = False
                errors.append("citation_lost")
        checks["citations_ok"] = citations_ok

        introduced_tokens: list[str] = []
        if isinstance(before, str) and isinstance(after, str):
            before_tokens = _evidence_tokens(before)
            after_tokens = _evidence_tokens(after)
            introduced_tokens = [
                token
                for key, token in sorted(after_tokens.items())
                if key not in before_tokens and key not in supported_tokens
            ]
        no_new_evidence_tokens = not introduced_tokens
        checks["no_new_evidence_tokens"] = no_new_evidence_tokens
        errors.extend(
            f"unsupported_token: {token}"
            for token in introduced_tokens
        )

        delta_bounded = (
            isinstance(before, str)
            and isinstance(after, str)
            and len(after) <= 3 * max(80, len(before))
        )
        checks["delta_bounded"] = delta_bounded
        if not delta_bounded:
            errors.append("delta_unbounded")

        no_new_flags = candidate is not None
        if candidate is not None:
            candidate_outputs = dict(outputs)
            candidate_outputs[str(target_output)] = candidate
            try:
                candidate_collisions = _collision_keys(
                    check_entity_consistency(candidate_outputs, city)
                )
                no_new_flags = not (
                    candidate_collisions - original_collisions
                )
            except Exception:
                no_new_flags = False
            if not no_new_flags:
                errors.append("new_entity_variant_collision")
        checks["no_new_flags"] = no_new_flags

        proposal["checks"] = checks
        proposal["validation_errors"] = errors
        proposal["validated"] = all(checks.values())
        proposal["apply_gated"] = target_output in _APPLY_GATED_OUTPUTS
        validated_proposals.append(proposal)
    return validated_proposals


def validate_single_proposal(
    proposal: Mapping[str, Any],
    outputs: Mapping[str, str],
    transcript_words: Sequence[Mapping[str, Any]],
    city: str,
) -> dict[str, Any]:
    """Run the complete proposal-validation battery against current inputs."""
    inputs = AuditInputs(
        meeting_id=0,
        meeting={"city_name": city},
        outputs=outputs,
        output_row_ids={},
        missing_outputs=(),
        transcript_words=tuple(transcript_words),
        outputs_snapshot_hash=compute_outputs_snapshot_hash(outputs),
    )
    return validate_proposals([proposal], inputs, transcript_words)[0]


def _entropy_observation(
    transcript_words: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if not transcript_words:
        return {"status": "uncheckable", "reason": "no_transcript"}
    profile = profile_token_entropy(
        transcript_words,
        config=EntropyConfig(),
    )
    return {
        key: profile.get(key)
        for key in (
            "status",
            "detector_version",
            "thresholds",
            "windows_evaluated",
            "invalid_windows",
            "min_entropy_bits",
            "median_entropy_bits",
            "low_entropy_window_count",
            "regions",
        )
    }


def _uncheckable(exc: Exception) -> dict[str, Any]:
    return {"status": "uncheckable", "reason": str(exc)}


def run_deterministic_wrapper(
    inputs: AuditInputs,
    llm_findings: Sequence[str],
    *,
    preview_dir: Path | None = None,
) -> dict[str, Any]:
    """Run every grounding check independently so one failure cannot cascade."""
    deterministic: dict[str, Any] = {}
    prose_outputs = _outputs_without_synopsis_anchor_audit(inputs.outputs)
    try:
        deterministic["entropy"] = _entropy_observation(inputs.transcript_words)
    except Exception as exc:
        deterministic["entropy"] = _uncheckable(exc)
    try:
        city = str(
            inputs.meeting.get("city_name")
            or inputs.meeting.get("city")
            or ""
        )
        deterministic["entity_consistency"] = check_entity_consistency(
            prose_outputs, city
        )
    except Exception as exc:
        deterministic["entity_consistency"] = _uncheckable(exc)
    try:
        deterministic["locator_existence"] = check_locator_existence(
            inputs.meeting_id,
            prose_outputs,
            inputs.transcript_words,
        )
    except Exception as exc:
        deterministic["locator_existence"] = _uncheckable(exc)
    try:
        deterministic["quote_existence"] = check_quote_existence(
            findings=llm_findings,
            transcript_words=inputs.transcript_words,
            outputs=prose_outputs,
        )
    except Exception as exc:
        deterministic["quote_existence"] = _uncheckable(exc)
    try:
        deterministic["provenance"] = check_provenance(
            inputs.meeting_id,
            preview_dir=preview_dir,
        )
    except Exception as exc:
        deterministic["provenance"] = _uncheckable(exc)
    try:
        deterministic["valid_empty"] = check_valid_empty(prose_outputs)
    except Exception as exc:
        deterministic["valid_empty"] = _uncheckable(exc)
    try:
        deterministic["synopsis_anchor"] = check_synopsis_anchor_audit(
            inputs.outputs
        )
    except Exception as exc:
        deterministic["synopsis_anchor"] = _synopsis_anchor_check_result(
            "uncheckable",
            0,
            0,
            status="uncheckable",
            error=str(exc),
        )
    return deterministic


def count_deterministic_flags(deterministic: Mapping[str, Any]) -> int:
    entity = deterministic.get("entity_consistency") or {}
    locators = deterministic.get("locator_existence") or {}
    quotes = deterministic.get("quote_existence") or {}
    synopsis_anchor = deterministic.get("synopsis_anchor") or {}
    return (
        len(entity.get("variant_collisions") or [])
        + len(locators.get("out_of_range") or [])
        + len(quotes.get("llm_evidence_not_found") or [])
        + len(synopsis_anchor.get("flags") or [])
    )


def _segment_words(
    words: Sequence[Mapping[str, Any]],
    segment_count: int,
) -> list[list[Mapping[str, Any]]]:
    if segment_count == 1:
        return [list(words)]
    end_seconds = _transcript_end_seconds(words) or 0.0
    if end_seconds <= 0:
        segment_size = math.ceil(len(words) / segment_count)
        return [
            list(words[index * segment_size:(index + 1) * segment_size])
            for index in range(segment_count)
        ]
    segments: list[list[Mapping[str, Any]]] = [
        [] for _ in range(segment_count)
    ]
    for word in words:
        start = word.get("start")
        numeric_start = (
            float(start)
            if isinstance(start, (int, float))
            and not isinstance(start, bool)
            and math.isfinite(float(start))
            else 0.0
        )
        index = min(
            segment_count - 1,
            int((numeric_start / end_seconds) * segment_count),
        )
        segments[index].append(word)
    return segments


def _segment_count(word_count: int) -> int:
    if word_count <= SEGMENTATION_WORD_THRESHOLD:
        return 1
    if word_count <= 2 * SEGMENTATION_WORD_THRESHOLD:
        return 2
    return 3


def _inputs_report(inputs: AuditInputs, prompt_char_count: int) -> dict[str, Any]:
    return {
        "output_row_ids": dict(inputs.output_row_ids),
        "missing_outputs": list(inputs.missing_outputs),
        "transcript_word_count": len(inputs.transcript_words),
        "outputs_snapshot_hash": inputs.outputs_snapshot_hash,
        "prompt_char_count": prompt_char_count,
    }


def _empty_llm_report(
    *,
    raw_response: str = "",
    segments: int = 0,
    error: str = "",
) -> dict[str, Any]:
    result = {
        "raw_response": raw_response,
        "findings": [],
        "open_findings": [],
        "suggestions": [],
        "verdict_line": "",
        "proposals": [],
        "no_safe_proposals": [],
        "segments": segments,
    }
    if error:
        result["error"] = error
    return result


def _persist(
    *,
    meeting_id: int,
    inputs: AuditInputs,
    report: Mapping[str, Any],
    run_status: str,
    verdict: str,
    started_at_utc: str,
    duration_seconds: float,
) -> str:
    run_id = str(uuid.uuid4())
    llm = report["llm"]
    deterministic_flags_count = count_deterministic_flags(
        report["deterministic"]
    )
    save_episode_audit_run(
        run_id=run_id,
        meeting_id=meeting_id,
        outputs_snapshot_hash=inputs.outputs_snapshot_hash,
        auditor_version=AUDITOR_VERSION,
        prompt_sha256=_PROMPT_SHA256,
        model=AUDIT_MODEL_ID,
        effort=AUDIT_EFFORT,
        run_status=run_status,
        verdict=verdict,
        findings_count=len(llm["findings"]),
        open_findings_count=len(llm["open_findings"]),
        suggestions_count=len(llm["suggestions"]),
        deterministic_flags_count=deterministic_flags_count,
        report_json=json.dumps(report, ensure_ascii=False, sort_keys=True),
        started_at_utc=started_at_utc,
        duration_seconds=duration_seconds,
    )
    return run_id


def _failed_inputs(meeting_id: int) -> AuditInputs:
    outputs: dict[str, str] = {}
    return AuditInputs(
        meeting_id=meeting_id,
        meeting={},
        outputs=outputs,
        output_row_ids={},
        missing_outputs=OUTPUT_ORDER,
        transcript_words=(),
        outputs_snapshot_hash=compute_outputs_snapshot_hash(outputs),
    )


def run_episode_audit(
    meeting_id: int,
    *,
    dry_run: bool = False,
    preview_dir: Path | None = None,
) -> dict[str, Any]:
    """Run one non-mutating audit and, unless dry-run, persist its observation."""
    started = time.monotonic()
    started_at_utc = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    try:
        meeting, outputs, row_ids, missing = _load_meeting_outputs(meeting_id)
        base_inputs = AuditInputs(
            meeting_id=meeting_id,
            meeting=meeting,
            outputs=outputs,
            output_row_ids=row_ids,
            missing_outputs=missing,
            transcript_words=(),
            outputs_snapshot_hash=compute_outputs_snapshot_hash(outputs),
        )
    except Exception as exc:
        base_inputs = _failed_inputs(meeting_id)
        report = {
            "llm": _empty_llm_report(error=str(exc)),
            "deterministic": run_deterministic_wrapper(
                base_inputs, [], preview_dir=preview_dir
            ),
            "inputs": _inputs_report(base_inputs, 0),
            "reason": "meeting_load_failed",
        }
        duration = time.monotonic() - started
        run_id = ""
        if not dry_run:
            run_id = _persist(
                meeting_id=meeting_id,
                inputs=base_inputs,
                report=report,
                run_status="runtime_failed",
                verdict="incomplete",
                started_at_utc=started_at_utc,
                duration_seconds=duration,
            )
        return {
            "run_id": run_id,
            "run_status": "runtime_failed",
            "verdict": "incomplete",
            "findings_count": 0,
            "open_findings_count": 0,
            "suggestions_count": 0,
            "proposals_validated_count": 0,
            "deterministic_flags_count": count_deterministic_flags(
                report["deterministic"]
            ),
            "report": report,
        }

    try:
        transcript = local_vector_store.load_transcript_words(meeting_id)
        words = tuple(transcript["words"])
        inputs = AuditInputs(
            meeting_id=meeting_id,
            meeting=base_inputs.meeting,
            outputs=base_inputs.outputs,
            output_row_ids=base_inputs.output_row_ids,
            missing_outputs=base_inputs.missing_outputs,
            transcript_words=words,
            outputs_snapshot_hash=base_inputs.outputs_snapshot_hash,
        )
    except Exception as exc:
        reason = "no_transcript" if isinstance(exc, LookupError) else "invalid_transcript"
        report = {
            "llm": _empty_llm_report(error=reason),
            "deterministic": run_deterministic_wrapper(
                base_inputs, [], preview_dir=preview_dir
            ),
            "inputs": _inputs_report(base_inputs, 0),
            "reason": reason,
            "error": str(exc),
        }
        duration = time.monotonic() - started
        run_id = ""
        if not dry_run:
            run_id = _persist(
                meeting_id=meeting_id,
                inputs=base_inputs,
                report=report,
                run_status="runtime_failed",
                verdict="incomplete",
                started_at_utc=started_at_utc,
                duration_seconds=duration,
            )
        return {
            "run_id": run_id,
            "run_status": "runtime_failed",
            "verdict": "incomplete",
            "findings_count": 0,
            "open_findings_count": 0,
            "suggestions_count": 0,
            "proposals_validated_count": 0,
            "deterministic_flags_count": count_deterministic_flags(
                report["deterministic"]
            ),
            "report": report,
        }

    segments = _segment_words(
        inputs.transcript_words,
        _segment_count(len(inputs.transcript_words)),
    )
    prompts = [
        build_audit_prompt(
            inputs.meeting,
            inputs.outputs,
            render_timecoded_transcript(segment),
            segment_note=(
                None
                if len(segments) == 1
                else (
                    f"NOTE: transcript segment {index} of {len(segments)} — "
                    "cross-segment omissions are checked separately."
                )
            ),
        )
        for index, segment in enumerate(segments, 1)
    ]
    prompt_char_count = sum(len(prompt) for prompt in prompts)

    if dry_run:
        deterministic = run_deterministic_wrapper(
            inputs, [], preview_dir=preview_dir
        )
        report = {
            "llm": _empty_llm_report(segments=len(segments)),
            "deterministic": deterministic,
            "inputs": _inputs_report(inputs, prompt_char_count),
        }
        return {
            "run_id": "",
            "run_status": "dry_run",
            "verdict": "incomplete",
            "findings_count": 0,
            "open_findings_count": 0,
            "suggestions_count": 0,
            "proposals_validated_count": 0,
            "deterministic_flags_count": count_deterministic_flags(
                deterministic
            ),
            "report": report,
        }

    parsed_segments: list[dict[str, Any]] = []
    runtime_error = ""
    raw_responses: list[str] = []
    try:
        for prompt in prompts:
            response = synthesize_via_claude_p(
                prompt,
                model=AUDIT_MODEL_ID,
                effort=AUDIT_EFFORT,
                timeout_seconds=NO_TIMEOUT,
            )
            raw_responses.append(response)
            parsed_segments.append(parse_audit_response(response))
    except Exception as exc:
        runtime_error = str(exc)

    findings = [
        item
        for parsed in parsed_segments
        for item in parsed["findings"]
    ]
    open_findings = [
        item
        for parsed in parsed_segments
        for item in parsed["open_findings"]
    ]
    suggestions = [
        item
        for parsed in parsed_segments
        for item in parsed["suggestions"]
    ]
    proposals: list[dict[str, Any]] = []
    no_safe_proposals: list[dict[str, Any]] = []
    finding_offset = 0
    for parsed in parsed_segments:
        for source in parsed["proposals"]:
            proposal = dict(source)
            finding_number = (
                int(proposal["finding_number"]) + finding_offset
            )
            sequence = proposal["id"].rsplit(".", 1)[-1]
            proposal["finding_number"] = finding_number
            proposal["id"] = f"p{finding_number}.{sequence}"
            proposals.append(proposal)
        for source in parsed["no_safe_proposals"]:
            no_safe = dict(source)
            no_safe["finding_number"] = (
                int(no_safe["finding_number"]) + finding_offset
            )
            no_safe_proposals.append(no_safe)
        finding_offset += len(parsed["findings"])
    verdict_lines = [
        parsed["verdict_line"]
        for parsed in parsed_segments
        if parsed["verdict_line"]
    ]
    raw_response = (
        raw_responses[0]
        if len(raw_responses) == 1
        else "\n\n".join(
            f"=== SEGMENT {index} ===\n{response}"
            for index, response in enumerate(raw_responses, 1)
        )
    )
    llm = {
        "raw_response": raw_response,
        "findings": findings,
        "open_findings": open_findings,
        "suggestions": suggestions,
        "verdict_line": " | ".join(verdict_lines),
        "proposals": proposals,
        "no_safe_proposals": no_safe_proposals,
        "segments": len(segments),
    }
    if runtime_error:
        llm["error"] = runtime_error

    if runtime_error:
        run_status = "runtime_failed"
    elif any(
        parsed["run_status"] == "parse_failed"
        for parsed in parsed_segments
    ):
        run_status = "parse_failed"
    else:
        run_status = "complete"

    deterministic = run_deterministic_wrapper(
        inputs,
        findings,
        preview_dir=preview_dir,
    )
    llm["proposals"] = validate_proposals(
        proposals,
        inputs,
        inputs.transcript_words,
    )
    proposals_validated_count = sum(
        proposal["validated"] for proposal in llm["proposals"]
    )
    deterministic_flags_count = count_deterministic_flags(deterministic)
    if run_status != "complete":
        verdict = "incomplete"
    elif findings or deterministic_flags_count:
        verdict = "flags"
    else:
        verdict = "no_catches"
    report = {
        "llm": llm,
        "deterministic": deterministic,
        "inputs": _inputs_report(inputs, prompt_char_count),
    }
    duration = time.monotonic() - started
    run_id = _persist(
        meeting_id=meeting_id,
        inputs=inputs,
        report=report,
        run_status=run_status,
        verdict=verdict,
        started_at_utc=started_at_utc,
        duration_seconds=duration,
    )
    return {
        "run_id": run_id,
        "run_status": run_status,
        "verdict": verdict,
        "findings_count": len(findings),
        "open_findings_count": len(open_findings),
        "suggestions_count": len(suggestions),
        "proposals_validated_count": proposals_validated_count,
        "deterministic_flags_count": deterministic_flags_count,
        "report": report,
    }


def _print_summary(result: Mapping[str, Any], *, dry_run: bool) -> None:
    if dry_run:
        inputs = result["report"]["inputs"]
        deterministic = result["report"]["deterministic"]
        print(
            "dry-run "
            f"prompt_chars={inputs['prompt_char_count']} "
            f"outputs_found={len(inputs['output_row_ids'])} "
            f"proposals_validated_count="
            f"{result['proposals_validated_count']} "
            f"deterministic_flags={result['deterministic_flags_count']} "
            f"checks={','.join(deterministic)}"
        )
        return
    print(
        f"verdict={result['verdict']} "
        f"findings={result['findings_count']} "
        f"open={result['open_findings_count']} "
        f"suggestions={result['suggestions_count']} "
        f"proposals_validated_count={result['proposals_validated_count']} "
        f"deterministic_flags={result['deterministic_flags_count']} "
        f"run_id={result['run_id']}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meeting-id", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_episode_audit(
            args.meeting_id,
            dry_run=args.dry_run,
        )
        _print_summary(result, dry_run=args.dry_run)
    except Exception as exc:
        # Audit-only means a failed observation never becomes a pipeline error.
        print(f"verdict=incomplete runtime_error={exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
