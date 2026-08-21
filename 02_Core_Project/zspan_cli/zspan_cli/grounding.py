"""Deterministic grounding primitives — ported lean from the pipeline's
neutrality-audit engine (stdlib-only there, stdlib-only here — keep
semantics in sync when the flagship's evolve).

What the CLI gate needs from the engine:

  - reference extraction + transcript-side grounding patterns
    (resolution/ordinance numbers, matched against Whisper's actual
    spoken renderings — 'resolution number 2026 r -15' etc.)
  - dollar extraction + the separator-stripped haystack (Whisper writes
    '$199 ,750 ,036'; absence still never refutes — spoken-number gap)
  - the vote/adoption signature library + strong-founder clustering
    (a vote moment is only founded by a strong anchor; weak-only
    clusters are dropped — the probe-3 tally-false-positive fix)
  - quoted-span extraction (a "quote" that isn't a substring of the
    record is a determinate refutation)

The three-state doctrine travels with the code: pass / FAIL only on
positive refutation / UNCHECKABLE otherwise. A spoken transcript can't
refute a digit-string it would have said in words.
"""
from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field

# ── Signature library — ported verbatim (data, not code) ─────────────

SIGNATURES: list[dict[str, str]] = [
    {"id": "carries", "kind": "vote_outcome", "strength": "strong",
     "pattern": r"\bmotion\s+(?:carries|passes|passed|fails|failed|is\s+approved)\b|\b(?:carries|passes)\s+unanimously\b"},
    {"id": "outcome_generic", "kind": "vote_outcome", "strength": "strong",
     "pattern": r"\bmotion\s+is\s+(?:tabled|withdrawn|approved|denied)\b|\bby\s+a\s+vote\s+of\b"},
    {"id": "so_ordered", "kind": "vote_outcome", "strength": "strong",
     "pattern": r"\bwithout\s+objection\b|\bso\s+ordered\b"},
    {"id": "approved_unanimously", "kind": "vote_outcome", "strength": "strong",
     "pattern": r"\bapproved\s+unanimously\b|\bunanimously\s+approved\b"},
    {"id": "motion_outcome_gapped", "kind": "vote_outcome", "strength": "strong",
     "pattern": r"\bmotion\s+to\s+[a-z0-9 ,'-]{0,80}?\b(?:passes|carries|fails|is\s+approved)\b"},
    {"id": "spoken_tally", "kind": "tally", "strength": "weak",
     "pattern": r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen)\s+to\s+(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen)\b"},
    {"id": "in_favor", "kind": "vote_call", "strength": "strong",
     "pattern": r"\ball\s+(?:those\s+)?in\s+favor\b|\bsignify\s+by\s+saying\s+aye\b"},
    {"id": "opposed", "kind": "vote_call", "strength": "weak",
     "pattern": r"\b(?:all|any|those)\s+opposed\b"},
    {"id": "roll_call", "kind": "vote_call", "strength": "strong",
     "pattern": r"\broll\s?call(?:\s+vote)?\b|\bcall\s+the\s+roll\b"},
    {"id": "adopt_instrument", "kind": "adoption", "strength": "strong",
     "pattern": r"\badopt(?:s|ed|ing)?\s+(?:the\s+)?(?:resolution|ordinance|budget)\b"},
    {"id": "consent_agenda", "kind": "adoption", "strength": "weak",
     "pattern": r"\bconsent\s+agenda\b"},
    {"id": "approval_of", "kind": "adoption", "strength": "weak",
     "pattern": r"\bapproval\s+of\s+(?:the\s+)?(?:consent\s+agenda|minutes|agenda)\b"},
    {"id": "second", "kind": "second", "strength": "weak",
     "pattern": r"\bi(?:'?ll)?\s+second\b|\bseconded\s+by\b|\bis\s+there\s+a\s+second\b|\bdo\s+i\s+have\s+a\s+second\b|\bsecond\s+the\s+motion\b"},
    {"id": "motion", "kind": "motion", "strength": "weak",
     "pattern": r"\bi\s+move\s+(?:that|to|for)\b|\bso\s+moved\b|\b(?:i'?(?:ll|d)?\s+)?make\s+a\s+motion\b|\bmy\s+motion\s+is\b"},
    {"id": "tally", "kind": "tally", "strength": "weak",
     "pattern": r"\b\d{1,2}\s*(?:to|[-–])\s*\d{1,2}\b"},
]

CLUSTER_GAP_WORDS = 120


def norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", " ".join((s or "").lower().split()))


def strip_output_markup(s: str) -> str:
    """Checkable text out of a rendered output unit — the <core>/<nuance>
    washes and bold never poison entity extraction. Shared by the gate's
    unit checks and the Discussion locator."""
    clean = re.sub(r"</?(?:core|nuance)>", "", s or "")
    return clean.replace("**", "")


_CASE_ID_RE = re.compile(
    r"\b([A-Z]{1,4})[ -]?(\d{1,4})[ -](\d{1,4})"
    r"(?:[ -](\d{1,4}))?\b(?![ -]\d{1,4}\b)"
)


def _case_id_match(s: str) -> re.Match[str] | None:
    match = _CASE_ID_RE.fullmatch(s)
    if match is None:
        return None
    groups = [group for group in match.groups()[1:] if group is not None]
    return match if sum(len(group) for group in groups) >= 3 else None


def is_case_id_ref(ref: str) -> bool:
    """Whether an extracted ref belongs to the municipal case-id family."""
    return _case_id_match(ref) is not None


def extract_refs(s: str) -> set[str]:
    """Resolution/ordinance refs plus original-cased municipal case ids.

    Existing families are normalized. Two-part refs match first with span
    suppression so ``Ordinance 2026-6`` never also emits ``ord-2026``.
    Case ids run last and cannot claim a span already owned by an existing
    family.
    """
    refs: set[str] = set()
    source = strip_output_markup(s or "")
    low = source.lower()
    consumed: list[tuple[int, int]] = []

    def overlaps(span: tuple[int, int]) -> bool:
        start, end = span
        return any(start < claimed_end and claimed_start < end
                   for claimed_start, claimed_end in consumed)

    for m in re.finditer(r"\bordinance\s+(?:number\s+|no\.?\s*)?(\d{4})[-\s](\d{1,3})\b", low):
        refs.add(f"ord-{m.group(1)}-{int(m.group(2))}")
        consumed.append(m.span())
    for m in re.finditer(r"\bordinance\s+(?:number\s+|no\.?\s*)?(\d{1,4})\b", low):
        if not overlaps(m.span()):
            refs.add(f"ord-{int(m.group(1))}")
            consumed.append(m.span())
    for m in re.finditer(r"\bresolution\s+(?:number\s+|no\.?\s*)?(\d{2,4})[-\s](\d{2,4})\b", low):
        refs.add(f"r-{int(m.group(1))}-{int(m.group(2))}")
        consumed.append(m.span())
    for m in re.finditer(r"\b(?:20\d\d[-\s]*)?r[-\s]{0,3}(\d{1,3})\b", low):
        if not overlaps(m.span()):
            refs.add(f"r-{int(m.group(1))}")
            consumed.append(m.span())
    for m in re.finditer(r"\bresolution\s+(?:number\s+|no\.?\s*)?(?:20\d\d\s*)?r?[-\s]?(\d{1,4})\b", low):
        if not overlaps(m.span()) and not re.match(r"20\d\d$", m.group(1)):
            refs.add(f"r-{int(m.group(1))}")
            consumed.append(m.span())

    for m in _CASE_ID_RE.finditer(source):
        groups = [group for group in m.groups()[1:] if group is not None]
        if sum(len(group) for group in groups) < 3 or overlaps(m.span()):
            continue
        refs.add(m.group(0))
        consumed.append(m.span())
    return refs


def ref_grounding_pattern(ref: str) -> str:
    """Transcript-side search pattern for a normalized ref, tolerant of
    Whisper's spoken renderings (verified against real transcript forms
    in the flagship engine)."""
    case_id = _case_id_match(ref)
    if case_id is not None:
        prefix = re.escape(case_id.group(1))
        groups = [group for group in case_id.groups()[1:] if group is not None]
        numeric = [r"0*" + re.escape(str(int(group))) for group in groups]
        body = r"[ -]+".join(numeric)
        return (r"(?i:\b" + prefix + r"[ -]*" + body
                + r"\b(?![ -]+\d{1,4}\b))")
    if ref.startswith("ord-"):
        parts = ref.split("-")
        head = r"\bordinance(?:\s+(?:number|no\.?)){0,3}"
        if len(parts) == 3:
            year, num = parts[1], parts[2]
            return (head + r"[^a-z]{0,10}" + re.escape(year)
                    + r"[^a-z0-9]{0,4}0*" + re.escape(num) + r"\b")
        return head + r"[^a-z]{0,10}0*" + re.escape(parts[1]) + r"\b"
    parts = ref.split("-")
    if len(parts) == 3:
        year, num = parts[1], parts[2]
        return (r"\bresolution(?:\s+(?:number|no\.?)){0,3}[^a-z]{0,10}"
                + re.escape(year) + r"[^a-z0-9]{0,4}0*" + re.escape(num)
                + r"\b|\b" + re.escape(year) + r"\s?-\s?0*" + re.escape(num) + r"\b")
    core = parts[1]
    return (r"(?:\br[-\s]{0,3}|resolution[^a-z0-9]{0,15})0*" + re.escape(core) + r"\b")


def extract_dollars(s: str) -> set[str]:
    return {re.sub(r"[,$]", "", d) for d in re.findall(r"\$[\d,]+(?:\.\d+)?", s or "")}


def extract_quoted_spans(s: str, *, min_norm_chars: int = 12) -> list[str]:
    """Double-quoted spans (straight or curly) long enough to be worth
    checking verbatim. Short quotes stay uncheckable — punctuation and
    two-word fragments refute nothing."""
    spans: list[str] = []
    # One class covers straight AND curly double-quotes (open/close/mixed).
    # The prior `[""]…|"…"` pair was ASCII-only despite the docstring — a
    # curly-quoted fabrication ("…") was never extracted, so it skipped the
    # verbatim check entirely.
    for m in re.finditer(r'[“”"]([^“”"]{4,400})[“”"]', s or ""):
        text = m.group(1) or ""
        if len(norm_text(text)) >= min_norm_chars:
            spans.append(text)
    return spans


# ── Transcript index (built once per meeting, reused per output) ──────


@dataclass
class TranscriptIndex:
    low: str
    norm: str
    digits: str                   # digits-only haystack — the dollar check's
                                  # target (Whisper writes '$199 ,750 ,036';
                                  # separators and the decimal point drop out
                                  # on BOTH sides, so full amounts compare
                                  # cents-included)
    vote_moment_count: int
    word_count: int
    _cum: list[int] = field(default_factory=list, repr=False)

    def word_index(self, char_offset: int) -> int:
        return max(0, bisect_right(self._cum, char_offset) - 1)


def build_transcript_index(words: list[dict]) -> TranscriptIndex:
    tokens = [str(w.get("word") or "").strip() for w in words]
    tokens = [t for t in tokens if t]
    text = " ".join(tokens)
    low = text.lower()

    cum: list[int] = []
    n = 0
    for t in tokens:
        cum.append(n)
        n += len(t) + 1

    idx = TranscriptIndex(
        low=low,
        norm=norm_text(text),
        digits=re.sub(r"\D", "", low),
        vote_moment_count=0,
        word_count=len(tokens),
        _cum=cum,
    )
    idx.vote_moment_count = _count_vote_moments(idx)
    return idx


def _count_vote_moments(t: TranscriptIndex) -> int:
    """Anchor scan + strong-founder clustering (the flagship's
    cluster_vote_moments semantics, reduced to a count — the CLI gate's
    v0 vote check only needs 'does the record hold ANY vote moment')."""
    anchors: list[tuple[int, str]] = []
    for sig in SIGNATURES:
        for m in re.finditer(sig["pattern"], t.low):
            anchors.append((t.word_index(m.start()), sig["strength"]))
    anchors.sort()

    moments = 0
    cluster: list[tuple[int, str]] = []

    def flush() -> int:
        if cluster and any(s == "strong" for _, s in cluster):
            return 1
        return 0

    for wi, strength in anchors:
        if cluster and wi - cluster[-1][0] > CLUSTER_GAP_WORDS:
            moments += flush()
            cluster.clear()
        cluster.append((wi, strength))
    moments += flush()
    return moments
