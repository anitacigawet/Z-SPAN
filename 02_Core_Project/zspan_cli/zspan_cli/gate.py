"""The deterministic post-synthesis audit.

Every synthesis is checked before caching, but the textual detector is
observation-only: it records determinate findings and uncheckable notes
without retrying, stripping, rebuilding, or otherwise correcting the
model's civic output. The user's approved frontier model remains the
author; the audit trail remains evidence for review.

The one display-only normalization retained here removes the canonical
key_decisions prompt's private ``<!-- audit ... audit -->`` envelope.
That metadata was never decision prose. If it is the only content, the
report is ``observed_empty``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from zspan_cli.grounding import (
    TranscriptIndex,
    extract_dollars,
    extract_quoted_spans,
    extract_refs,
    is_case_id_ref,
    norm_text,
    ref_grounding_pattern,
    strip_output_markup,
)

# Outcome verbs that make a sentence claim a body action — the m105310
# class check (claims a vote; record holds zero vote moments).
_VOTE_CLAIM_PATTERN = re.compile(
    r"\b(?:approved|passed|adopted|carried|denied|rejected|failed|tabled)\b"
    r"|\bvoted?\s+\d|\bunanimous(?:ly)?\b|\b\d{1,2}\s*[-–]\s*\d{1,2}\s+vote\b",
    re.IGNORECASE,
)

# The canonical key_decisions prompt appends a private, trailing audit
# envelope for the pipeline.  It is metadata, never decision prose.  Match
# the delimiters rather than the JSON shape so malformed audit JSON is still
# kept off every local display surface.
_KEY_DECISIONS_AUDIT_BLOCK_RE = re.compile(
    r"(?:\r?\n\s*)?<!--\s*audit\b.*?\baudit\s*-->\s*\Z",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class UnitVerdict:
    raw: str                       # the unit's original text (rebuild uses this)
    failures: list[str] = field(default_factory=list)      # determinate only
    uncheckable: list[str] = field(default_factory=list)   # noted, never acted on


@dataclass
class GateReport:
    status: str  # observed_clean | observed_findings | observed_empty
    retried: bool = False
    determinate_failures: list[str] = field(default_factory=list)   # final round
    stripped_units: list[str] = field(default_factory=list)
    uncheckable_notes: list[str] = field(default_factory=list)
    detail: str = ""

    def to_json(self) -> str:
        return json.dumps({
            "status": self.status,
            "retried": self.retried,
            "determinate_failures": self.determinate_failures,
            "stripped_units": [u[:160] for u in self.stripped_units],
            "uncheckable_notes": self.uncheckable_notes[:20],
            "detail": self.detail,
        }, ensure_ascii=False)


# ---------------------------------------------------------------- units


def strip_key_decisions_audit(content: str) -> str:
    """Return display/gate prose without the prompt's trailing audit block."""
    return _KEY_DECISIONS_AUDIT_BLOCK_RE.sub("", content or "").strip()


def _split_key_decisions(content: str) -> Optional[list[str]]:
    """Numbered items with their raw text preserved (markup included) so
    surviving items rebuild verbatim. None when the content isn't a
    numbered list — whole-output unit then."""
    matches = list(re.finditer(r"(?m)^\s*\d+[.)]\s+", content))
    if not matches:
        return None
    items: list[str] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        items.append(content[m.end():end].strip())
    return [it for it in items if it]


def split_ccta(content: str) -> Optional[list[dict]]:
    """community_calls_to_action is a JSON array per its prompt. Parse
    tolerantly (fenced or bare); None → whole-content unit."""
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    if not text.startswith("["):
        start = text.find("[")
        if start >= 0 and text.rstrip().endswith("]"):
            text = text[start:]
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    return [d for d in data if isinstance(d, (dict, str))]


def _check_text(unit: str) -> str:
    """The unit's checkable text — markup stripped so <core>/<nuance>
    tags and bold never poison entity extraction. (Canonical helper
    lives in grounding.py; the Discussion locator shares it.)"""
    return strip_output_markup(unit)


def _check_entities(check: str, verdict: UnitVerdict, t: TranscriptIndex) -> None:
    """The shared determinate checks: refs must ground, dollars may only
    pass or stay uncheckable, quoted spans must be verbatim."""
    for ref in sorted(extract_refs(check)):
        if not re.search(ref_grounding_pattern(ref), t.low):
            if is_case_id_ref(ref):
                # Determinate strengthening is deferred until spoken-number
                # normalization can bridge Whisper's case-id decode variance.
                verdict.uncheckable.append(
                    f"case id {ref} not verbatim-locatable (spoken-rendering gap)"
                )
            else:
                verdict.failures.append(
                    f"reference {ref} does not appear in the record"
                )

    for amount in sorted(extract_dollars(check)):
        # Full digit string, cents included — the integer part alone
        # ("$19.99" → "19") trivially matches almost any transcript and
        # checks nothing.
        digits = re.sub(r"\D", "", amount)
        if digits and digits not in t.digits:
            # spoken-number normalization gap — absence is not refutation
            verdict.uncheckable.append(f"${amount} not verbatim-locatable (spoken-number gap)")

    for span in extract_quoted_spans(check):
        if norm_text(span) not in t.norm:
            verdict.failures.append(
                f'quoted text "{span[:80]}" is not verbatim in the record'
            )


def _check_unit(text: str, t: TranscriptIndex, *, vote_check: bool) -> UnitVerdict:
    verdict = UnitVerdict(raw=text)
    check = _check_text(text)
    _check_entities(check, verdict, t)

    if vote_check and _VOTE_CLAIM_PATTERN.search(check):
        if t.vote_moment_count == 0:
            verdict.failures.append(
                "claims a body action (vote/approval) but the record holds "
                "zero deterministic vote moments"
            )
        # moments exist → per-claim proximity stays uncheckable at v0;
        # a conservative gate never refutes on a heuristic it can't prove.

    return verdict


# The CCTA element's claimed-verbatim field (its prompt: "calls-to-action
# are VERBATIM, not synthesized" — quote_text IS the category contract).
# The extra names are defensive against model-side field drift.
_VERBATIM_KEYS = ("quote_text", "quote", "verbatim")


def _check_ccta_element(el, t: TranscriptIndex) -> UnitVerdict:
    """One community_calls_to_action element. Dict elements are checked
    over their string VALUES (never the JSON encoding — JSON's own quote
    marks must not read as claimed quotes), and the quote_text field is
    substring-checked as claimed-verbatim per the category's contract."""
    if isinstance(el, str):
        return _check_unit(el, t, vote_check=False)

    verdict = UnitVerdict(raw=json.dumps(el, ensure_ascii=False))
    values_text = _check_text(
        " ".join(str(v) for v in el.values() if isinstance(v, str))
    )
    _check_entities(values_text, verdict, t)

    for key in _VERBATIM_KEYS:
        val = el.get(key)
        if not isinstance(val, str):
            continue
        normed = norm_text(val)
        if len(normed) < 12:
            continue  # too short to refute — uncheckable, not fail
        if normed not in t.norm:
            verdict.failures.append(
                f'claimed-verbatim {key} "{val[:80]}" is not in the record'
            )
    return verdict


def run_gate(output_type: str, content: str, t: TranscriptIndex) -> list[UnitVerdict]:
    """Split the output into checkable units and check each. The unit
    grain is what makes strip surgical instead of all-or-nothing."""
    # The vote-fabrication check runs on every narrative output that can
    # assert a body action, not just key_decisions — "the council
    # unanimously approved the tax" in a synopsis or an episode_tagline is
    # the same fabrication class. CCTA is excluded (structured verbatim
    # asks, checked element-wise below).
    vote_check = output_type in ("key_decisions", "synopsis", "episode_tagline")

    if output_type == "key_decisions":
        items = _split_key_decisions(content)
        if items:
            return [_check_unit(it, t, vote_check=True) for it in items]

    if output_type == "community_calls_to_action":
        elements = split_ccta(content)
        if elements is not None:
            return [_check_ccta_element(el, t) for el in elements]

    return [_check_unit(content, t, vote_check=vote_check)]


# Public name for the numbered-item splitter — the Discussion locator
# (discussion.py) must cut decisions at EXACTLY the boundaries the gate
# (and the client's parseNumberedList) use, or decision_index drifts.
def split_key_decisions(content: str):
    return _split_key_decisions(content)


def normalize_ccta(content: str) -> str:
    """Canonical JSON for a parseable community_calls_to_action output —
    small models wrap the array in markdown fences; the workspace caches
    the parsed array so the render
    layer never re-derives the tolerant parse. Unparseable content passes
    through unchanged."""
    elements = split_ccta(content)
    if elements is None:
        return content
    return json.dumps(elements, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- observation


def gate_and_retry(
    output_type: str,
    content: str,
    t: TranscriptIndex,
    *,
    resynthesize: Optional[Callable[[str], str]] = None,
    progress: Callable[[str], None] = print,
) -> tuple[str, GateReport]:
    """Observe deterministic findings and return the synthesis unchanged.

    ``resynthesize`` remains in the signature for caller compatibility but
    is deliberately never called. The key_decisions private audit envelope
    is removed as display metadata before observation and caching.
    """
    if output_type == "key_decisions":
        content = strip_key_decisions_audit(content)
        if not content:
            return "", GateReport(
                status="observed_empty",
                detail=(
                    "no decision prose remained after removing the prompt's "
                    "private audit metadata"
                ),
            )

    verdicts = run_gate(output_type, content, t)
    all_uncheckable = [n for v in verdicts for n in v.uncheckable]
    failures = [f for v in verdicts for f in v.failures]

    if not failures:
        return content, GateReport(
            status="observed_clean",
            uncheckable_notes=all_uncheckable,
            detail=f"{len(verdicts)} unit(s) checked, no determinate findings",
        )

    progress(
        f"  gate audit: observed {len(failures)} determinate finding(s); "
        "original content preserved"
    )
    return content, GateReport(
        status="observed_findings",
        retried=False,
        determinate_failures=failures,
        stripped_units=[],
        uncheckable_notes=all_uncheckable,
        detail=(
            "deterministic findings observed and logged; audit-only mode "
            "did not retry, strip, or rewrite the synthesis"
        ),
    )
