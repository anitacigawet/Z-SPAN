"""
review_response_parser — parses Gemini Pro RESPONSE.md files from T-013 V2.
==========================================================================

Reads a `batch_NN/RESPONSE.md` produced by the human reviewer pasting
Gemini Pro's structured reply, returns a Python-friendly per-clip record
list. Used by `zspan_pipeline.scripts.ingest_review_response` to apply
verdicts + mechanical corrections to `member_quotes`.

Per D-043, this is the LAST step in the triple-source verification
chain: Gemini's structured output → parsed → applied to the DB. No new
LLM is invoked here. Find-and-replace patterns Gemini surfaces (e.g.,
`"POSOS systems" should be "POS systems"`) are applied mechanically;
prose-style differences ("Transcript merges dialogue from three
speakers") are surfaced as-is for human review and route the quote to
`verified_status='disputed'`.

The response file format (defined by `prompts/verification_batch_prompt_template.md`):

    ## clip: <filename>

    * speaker_attribution: yes | no | uncertain
    * speaker_attribution_notes: <one short line>
    * text_accuracy: yes | mostly | no
    * text_differences: <quoted differences, or "none">
    * clip_integrity: ok | cuts-mid-word | audio-issue | other
    * other_concerns: <one short line, or "none">

    ## clip: <next filename>
    ...

    ## BATCH COMPLETE

The audit metadata at the top of the RESPONSE.md (between the audit
header and the marker line `## Gemini response (paste below this line ...)`)
is also extracted so the ingestion can preserve the response-received
timestamp + the reviewer's notes.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── Parsing constants ───────────────────────────────────────────────


# `## clip: <filename>` block header. Tolerates trailing whitespace and
# any reasonable filename extension.
_CLIP_HEADER_RE = re.compile(
    r"^\s*##\s*clip\s*:\s*(?P<filename>[^\s]+\.[A-Za-z0-9]+)\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# `* field_name: value` bullet inside a clip block. Tolerates `-` and `*`
# bullets and case-insensitive field name (Gemini sometimes capitalizes).
_FIELD_RE = re.compile(
    r"^\s*[*\-]\s*(?P<key>[a-z_]+)\s*:\s*(?P<value>.+?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# `"<wrong>" should be "<right>"` — the clean substitution pattern that
# `extract_substitutions` mechanically applies. Accepts ASCII straight
# quotes AND curly quotes (Gemini sometimes uses one, sometimes the
# other). Multiple substitutions in one `text_differences` field are
# typically semicolon-separated; the regex matches each occurrence so we
# just `findall`.
_SUBSTITUTION_RE = re.compile(
    r"[\"“”]([^\"“”]+)[\"“”]"
    r"\s+should\s+be\s+"
    r"[\"“”]([^\"“”]+)[\"“”]",
    re.IGNORECASE,
)

# `**Response received:** <date+time>` field in the RESPONSE.md audit
# header. The reviewer fills this in when saving Gemini's reply.
_RESPONSE_RECEIVED_RE = re.compile(
    r"^\s*-?\s*\*\*Response received:\*\*\s*(?P<value>.+?)\s*$",
    re.MULTILINE,
)

# The marker line that separates the audit metadata header from the
# pasted Gemini response below. Everything before this line is audit
# metadata; everything after is Gemini's verbatim reply.
_GEMINI_MARKER_RE = re.compile(
    r"^\s*##\s*Gemini response\b.*$",
    re.MULTILINE | re.IGNORECASE,
)

# The "I haven't filled this in yet" placeholder in the response-received
# field. Used to distinguish "reviewer saved real timestamp" from "stub".
_RESPONSE_RECEIVED_PLACEHOLDER_PREFIX = "_[REPLACE THIS"


# ── Data classes ────────────────────────────────────────────────────


@dataclass
class ClipVerdict:
    """One clip's verdict as parsed from a Gemini RESPONSE.md block.

    `raw_fields` preserves whatever Gemini wrote, even if it deviated
    from the expected enum values — the ingestion script makes final
    decisions about how to interpret them.
    """
    filename: str
    raw_fields: dict[str, str] = field(default_factory=dict)

    # Normalized convenience accessors (lowercased + whitespace-trimmed)
    @property
    def speaker_attribution(self) -> str:
        return (self.raw_fields.get("speaker_attribution") or "").strip().lower()

    @property
    def text_accuracy(self) -> str:
        return (self.raw_fields.get("text_accuracy") or "").strip().lower()

    @property
    def text_differences(self) -> str:
        return (self.raw_fields.get("text_differences") or "").strip()

    @property
    def clip_integrity(self) -> str:
        return (self.raw_fields.get("clip_integrity") or "").strip().lower()

    @property
    def other_concerns(self) -> str:
        return (self.raw_fields.get("other_concerns") or "").strip()


@dataclass
class ParsedResponse:
    """The full parsed RESPONSE.md."""
    source_path: Path
    response_received: Optional[str]      # the reviewer's filled-in timestamp, or None
    response_received_is_placeholder: bool  # True iff still the `_[REPLACE THIS...]` stub
    has_batch_complete_marker: bool       # True iff Gemini's reply ended with `## BATCH COMPLETE`
    clips: list[ClipVerdict] = field(default_factory=list)


# ── Public functions ────────────────────────────────────────────────


def extract_substitutions(text_differences: str) -> list[tuple[str, str]]:
    """Find every `"X" should be "Y"` pattern in `text_differences`.

    Returns a list of `(wrong, right)` tuples in the order they appear.
    Empty list if no clean substitution pattern matches (e.g., Gemini
    used prose like "Transcript merges dialogue from three speakers").

    Tolerates curly quotes AND straight quotes (Gemini's output varies).
    """
    if not text_differences:
        return []
    out: list[tuple[str, str]] = []
    for match in _SUBSTITUTION_RE.finditer(text_differences):
        wrong = match.group(1).strip()
        right = match.group(2).strip()
        if wrong and right and wrong != right:
            out.append((wrong, right))
    return out


def parse_response_file(path: Path) -> ParsedResponse:
    """Parse a `batch_NN/RESPONSE.md` file.

    Returns a `ParsedResponse` with the audit metadata + per-clip
    verdicts. If the reviewer hasn't pasted Gemini's reply yet (the file
    still has the empty stub below the marker), `clips` is empty and
    `response_received_is_placeholder` is True — the caller should skip
    the file.

    Robust to common formatting drift:
      - Bullets can be `*` or `-`
      - Field names are case-insensitive
      - Curly OR straight quotes inside text_differences
      - Reviewer's pasted reply may include extra whitespace / explanatory
        prose between blocks (ignored — only matched fields count)
    """
    text = path.read_text(encoding="utf-8")

    # Find the "## Gemini response (paste below ...)" marker. Everything
    # ABOVE is audit metadata (response-received, batch list, etc.).
    # Everything BELOW is Gemini's verbatim reply we want to parse.
    marker_match = _GEMINI_MARKER_RE.search(text)
    if marker_match is None:
        # Malformed file — try to parse the whole thing, but the file
        # may not be in the expected shape. Flag for caller via
        # response_received_is_placeholder=True.
        audit_section = text
        gemini_section = ""
    else:
        audit_section = text[: marker_match.start()]
        gemini_section = text[marker_match.end():]

    # Extract response-received from the audit section.
    response_received: Optional[str] = None
    received_match = _RESPONSE_RECEIVED_RE.search(audit_section)
    if received_match:
        response_received = received_match.group("value").strip()

    is_placeholder = (
        not response_received
        or response_received.startswith(_RESPONSE_RECEIVED_PLACEHOLDER_PREFIX)
    )

    has_batch_complete = bool(
        re.search(r"^\s*##\s*BATCH\s+COMPLETE\b", gemini_section, re.MULTILINE | re.IGNORECASE)
    )

    # Parse Gemini's per-clip blocks from `gemini_section`.
    clips: list[ClipVerdict] = []
    # Find all `## clip:` headers and their start positions; each block
    # extends from one header to the next (or to BATCH COMPLETE / EOF).
    header_matches = list(_CLIP_HEADER_RE.finditer(gemini_section))
    for i, m in enumerate(header_matches):
        filename = m.group("filename")
        block_start = m.end()
        block_end = (
            header_matches[i + 1].start()
            if i + 1 < len(header_matches)
            else len(gemini_section)
        )
        block_text = gemini_section[block_start:block_end]

        fields = {}
        for field_match in _FIELD_RE.finditer(block_text):
            key = field_match.group("key").strip().lower()
            value = field_match.group("value").strip()
            # Strip trailing punctuation that some reviewers add
            # (unlikely from Gemini directly but tolerate it).
            value = value.rstrip(".,;")
            fields[key] = value

        clips.append(ClipVerdict(filename=filename, raw_fields=fields))

    return ParsedResponse(
        source_path=path,
        response_received=response_received,
        response_received_is_placeholder=is_placeholder,
        has_batch_complete_marker=has_batch_complete,
        clips=clips,
    )


def classify_decision(verdict: ClipVerdict) -> str:
    """Map a clip verdict to a `verified_status` value.

    Logic (per D-043, refined by the m101091 validation 2026-05-16):
      - speaker_attribution: no OR text_accuracy: no
            → `rejected` (do not publish — misattribution or merged speakers)
      - speaker_attribution: uncertain
            → `disputed` (human needs to weigh in)
      - text_accuracy: mostly + text_differences has clean substitution pattern(s)
            → `verified` (after corrections are applied)
      - text_accuracy: mostly + text_differences is prose / non-substitution
            → `disputed` (couldn't auto-correct; human eyes needed)
      - speaker yes + text yes
            → `verified`
      - clip_integrity != ok
            → `disputed` regardless of other fields (clip itself is suspect)
    """
    if verdict.speaker_attribution == "no" or verdict.text_accuracy == "no":
        return "rejected"
    if verdict.clip_integrity and verdict.clip_integrity != "ok":
        return "disputed"
    if verdict.speaker_attribution == "uncertain":
        return "disputed"
    if verdict.text_accuracy == "mostly":
        if extract_substitutions(verdict.text_differences):
            return "verified"
        # Common acceptable "mostly" cases that don't need substitution:
        # disfluency removal, filler-word smoothing — these are EXACTLY
        # what the cleaner already does, so accept them.
        td_lower = verdict.text_differences.lower()
        if any(
            phrase in td_lower
            for phrase in (
                "filler word",
                "false start",
                "stutter",
                "smoothed out",
                "disfluen",  # disfluencies / disfluency
            )
        ):
            return "verified"
        # Otherwise the prose describes a difference we can't mechanically
        # apply (missing word, merged speakers, omitted measurement,
        # etc.) — flag for human review.
        return "disputed"
    if verdict.speaker_attribution == "yes" and verdict.text_accuracy == "yes":
        return "verified"
    # Fallback: anything else (missing/unparsed fields) → disputed.
    return "disputed"


def apply_substitutions(
    quote_text: str, substitutions: list[tuple[str, str]]
) -> tuple[str, list[dict]]:
    """Apply find-and-replace substitutions to `quote_text`. Mechanical
    string replacement — NO LLM in the loop (per D-043).

    Returns (new_text, applied_log). Each entry in applied_log is a dict
    `{from, to, count}` describing what happened. Substitutions whose
    "wrong" string doesn't actually appear in `quote_text` are logged
    with `count=0` so the audit trail records that Gemini suggested a
    change we couldn't find a match for (usually because Gemini's
    "wrong" was paraphrased rather than verbatim).
    """
    new_text = quote_text
    applied_log: list[dict] = []
    for wrong, right in substitutions:
        count = new_text.count(wrong)
        if count > 0:
            new_text = new_text.replace(wrong, right)
        applied_log.append({"from": wrong, "to": right, "count": count})
    return new_text, applied_log
