"""Stage-2 validation: everything here is deterministic, stdlib-only, zero LLM.

Four passes over (transcript words, extracted vote frames, cached output):

  1. Signature anchor scan — the v0 vote/adoption signature library run over
     the raw transcript. This is the S-133 "self-learning signature library"
     seed: patterns live as DATA (SIGNATURES) so the future LLM-teacher loop
     can propose additions that get backtested before promotion, per the
     city_vocabulary_corrections precedent.
  2. Frame grounding — each extracted frame is located in the transcript by
     its checkable entities and tested for proximity to a vote moment. Checks
     are three-state (pass / fail / uncheckable): a spoken transcript can't
     refute a digit-string it would have said in words.
  3. Consensus-convergence — align two independent families' frame sets and
     classify per-field convergence. Determinate fields (vote_result, tally,
     resolution refs) diverging is the neutrality flag; latitude fields
     (phrasing) diverging is expected and only measured.
  4. Output audit — map each claimed decision in the cached key_decisions
     output onto the grounded/consensus frame set. Ungrounded claims are the
     D-144 signal; frames absent from the output are selection latitude
     (key_decisions deliberately picks a subset) and are reported, not flagged.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# ── Signature library (v0 — data, not code) ──────────────────────────
#
# strength "strong" = the pattern alone marks a vote moment;
# "weak" = corroborating only (joins a cluster, never founds one).
# Ported from probe 3 + the S-133 finding that real meetings decide via
# adoption/consent/"carries" far more than formal "I move" (m103225:
# "I move" ×0 vs "carries" ×7).

SIGNATURES: list[dict[str, str]] = [
    # -- vote outcome (the announcement that resolves a question) --
    {"id": "carries", "kind": "vote_outcome", "strength": "strong",
     "pattern": r"\bmotion\s+(?:carries|passes|passed|fails|failed|is\s+approved)\b|\b(?:carries|passes)\s+unanimously\b"},
    {"id": "outcome_generic", "kind": "vote_outcome", "strength": "strong",
     "pattern": r"\bmotion\s+is\s+(?:tabled|withdrawn|approved|denied)\b|\bby\s+a\s+vote\s+of\b"},
    {"id": "so_ordered", "kind": "vote_outcome", "strength": "strong",
     "pattern": r"\bwithout\s+objection\b|\bso\s+ordered\b"},
    {"id": "approved_unanimously", "kind": "vote_outcome", "strength": "strong",
     "pattern": r"\bapproved\s+unanimously\b|\bunanimously\s+approved\b"},
    # first teacher-loop entries (2026-07-09): derived from the m103995 gap
    # (LHC P&Z "the motion to approve the minutes passes seven to zero"),
    # corpus-backtested before promotion per the S-133 guardrail
    {"id": "motion_outcome_gapped", "kind": "vote_outcome", "strength": "strong",
     "pattern": r"\bmotion\s+to\s+[a-z0-9 ,'-]{0,80}?\b(?:passes|carries|fails|is\s+approved)\b"},
    {"id": "spoken_tally", "kind": "tally", "strength": "weak",
     "pattern": r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen)\s+to\s+(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen)\b"},
    # -- vote call (the chair putting the question) --
    {"id": "in_favor", "kind": "vote_call", "strength": "strong",
     "pattern": r"\ball\s+(?:those\s+)?in\s+favor\b|\bsignify\s+by\s+saying\s+aye\b"},
    {"id": "opposed", "kind": "vote_call", "strength": "weak",
     "pattern": r"\b(?:all|any|those)\s+opposed\b"},
    {"id": "roll_call", "kind": "vote_call", "strength": "strong",
     "pattern": r"\broll\s?call(?:\s+vote)?\b|\bcall\s+the\s+roll\b"},
    # -- adoption / consent (decision events that skip the motion frame) --
    {"id": "adopt_instrument", "kind": "adoption", "strength": "strong",
     "pattern": r"\badopt(?:s|ed|ing)?\s+(?:the\s+)?(?:resolution|ordinance|budget)\b"},
    {"id": "consent_agenda", "kind": "adoption", "strength": "weak",
     "pattern": r"\bconsent\s+agenda\b"},
    {"id": "approval_of", "kind": "adoption", "strength": "weak",
     "pattern": r"\bapproval\s+of\s+(?:the\s+)?(?:consent\s+agenda|minutes|agenda)\b"},
    # -- second (corroborates a pending question) --
    {"id": "second", "kind": "second", "strength": "weak",
     "pattern": r"\bi(?:'?ll)?\s+second\b|\bseconded\s+by\b|\bis\s+there\s+a\s+second\b|\bdo\s+i\s+have\s+a\s+second\b|\bsecond\s+the\s+motion\b"},
    # -- motion (kept for grammar context; sparse in practice per S-133) --
    {"id": "motion", "kind": "motion", "strength": "weak",
     "pattern": r"\bi\s+move\s+(?:that|to|for)\b|\bso\s+moved\b|\b(?:i'?(?:ll|d)?\s+)?make\s+a\s+motion\b|\bmy\s+motion\s+is\b"},
    # -- numeric tally; weak on purpose (probe 3 false-positived ranges) --
    {"id": "tally", "kind": "tally", "strength": "weak",
     "pattern": r"\b\d{1,2}\s*(?:to|[-–])\s*\d{1,2}\b"},
]

STOPWORDS = frozenset(
    "the and for with that this from into then than have has been were was are is of to in on at by an a or as it its be we they he she you i not no yes so if but do did done will would could should may might must shall city council meeting motion second vote item number all those please thank okay".split()
)

WINDOW_WORDS = 400          # grounding proximity window (probe 3's)
CLUSTER_GAP_WORDS = 120     # max gap between anchors in one vote moment

VOTE_RESULT_ENUM = frozenset({"passed", "failed", "tabled", "withdrawn", "tied"})
VOTE_METHOD_ENUM = frozenset({"voice", "roll_call", "unanimous_consent", "none"})
MEMBER_VOTE_ENUM = frozenset({"aye", "nay", "abstain", "absent", "recused"})


# ── Transcript primitives ─────────────────────────────────────────────


@dataclass
class Transcript:
    """Ordered word list + derived lookups. Word index is the audit's
    'program counter' — every anchor/grounding position is a word index."""

    words: list[str]
    text: str = ""
    low: str = ""
    norm: str = ""
    _cum: list[int] = field(default_factory=list)
    _token_index: Optional[dict[str, list[int]]] = None

    @classmethod
    def from_words(cls, words: list[str]) -> "Transcript":
        words = [w for w in words if w]
        text = " ".join(words)
        t = cls(words=words, text=text, low=text.lower(), norm=norm_text(text))
        n = 0
        for w in words:
            t._cum.append(n)
            n += len(w) + 1
        return t

    def word_index(self, char_offset: int) -> int:
        return max(0, bisect_right(self._cum, char_offset) - 1)

    def token_positions(self, token: str) -> list[int]:
        if self._token_index is None:
            idx: dict[str, list[int]] = {}
            for i, w in enumerate(self.words):
                nw = norm_text(w)
                if nw:
                    idx.setdefault(nw, []).append(i)
            self._token_index = idx
        return self._token_index.get(token, [])

    def locate_tokens(self, tokens: set[str], window: int = 200) -> tuple[Optional[int], int]:
        """Best co-occurrence window: the word index where the most DISTINCT
        frame tokens appear together. Median-of-first-occurrences localization
        was the v0 bug this replaces — common tokens' first hits scatter to
        the transcript head and poison anchor proximity."""
        hits: list[tuple[int, str]] = []
        for tok in tokens:
            hits.extend((pos, tok) for pos in self.token_positions(tok))
        if not hits:
            return None, 0
        hits.sort()
        best_center, best_distinct = None, 0
        left = 0
        counts: dict[str, int] = {}
        for right, (pos, tok) in enumerate(hits):
            counts[tok] = counts.get(tok, 0) + 1
            while pos - hits[left][0] > window:
                ltok = hits[left][1]
                counts[ltok] -= 1
                if not counts[ltok]:
                    del counts[ltok]
                left += 1
            if len(counts) > best_distinct:
                best_distinct = len(counts)
                best_center = (hits[left][0] + pos) // 2
        return best_center, best_distinct


def norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", " ".join((s or "").lower().split()))


def content_tokens(s: str) -> set[str]:
    return {t for t in norm_text(s).split() if len(t) >= 4 and t not in STOPWORDS}


def extract_refs(s: str) -> set[str]:
    """Resolution/ordinance references, normalized (e.g. 'r-15', 'ord-2026-6').
    Two-part ordinance refs are matched first and their spans suppressed for
    the one-part pattern, so 'Ordinance 2026-6' never also emits 'ord-2026'."""
    refs: set[str] = set()
    low = (s or "").lower()
    consumed: list[tuple[int, int]] = []
    for m in re.finditer(r"\bordinance\s+(?:number\s+|no\.?\s*)?(\d{4})[-\s](\d{1,3})\b", low):
        refs.add(f"ord-{m.group(1)}-{int(m.group(2))}")
        consumed.append(m.span())
    for m in re.finditer(r"\bordinance\s+(?:number\s+|no\.?\s*)?(\d{1,4})\b", low):
        if not any(a <= m.start() < b for a, b in consumed):
            refs.add(f"ord-{int(m.group(1))}")
    # two-part resolution numbering (LHC 'Resolution 26-3923') before the
    # one-part patterns, same span-suppression as ordinances — otherwise the
    # year-half leaks out as a bogus one-part ref and poisons grounding
    for m in re.finditer(r"\bresolution\s+(?:number\s+|no\.?\s*)?(\d{2,4})[-\s](\d{2,4})\b", low):
        refs.add(f"r-{int(m.group(1))}-{int(m.group(2))}")
        consumed.append(m.span())
    for m in re.finditer(r"\b(?:20\d\d[-\s]*)?r[-\s]{0,3}(\d{1,3})\b", low):
        if not any(a <= m.start() < b for a, b in consumed):
            refs.add(f"r-{int(m.group(1))}")
    for m in re.finditer(r"\bresolution\s+(?:number\s+|no\.?\s*)?(?:20\d\d\s*)?r?[-\s]?(\d{1,4})\b", low):
        if not any(a <= m.start() < b for a, b in consumed) \
           and not re.match(r"20\d\d$", m.group(1)):
            refs.add(f"r-{int(m.group(1))}")
    return refs


def ref_grounding_pattern(ref: str) -> str:
    """Transcript-side search pattern for a normalized ref, matched against
    Whisper's actual spoken renderings — 'resolution number number 2026 r -15'
    (spaced dash), 'ordinance number 2026 -6' (the word 'number' between head
    and year). Patterns verified against the m103225 transcript forms."""
    if ref.startswith("ord-"):
        parts = ref.split("-")
        head = r"\bordinance(?:\s+(?:number|no\.?)){0,3}"
        if len(parts) == 3:
            year, num = parts[1], parts[2]
            return (head + r"[^a-z]{0,10}" + re.escape(year)
                    + r"[^a-z0-9]{0,4}0*" + re.escape(num) + r"\b")
        return head + r"[^a-z]{0,10}0*" + re.escape(parts[1]) + r"\b"
    parts = ref.split("-")
    if len(parts) == 3:                      # two-part: r-26-3923
        year, num = parts[1], parts[2]
        return (r"\bresolution(?:\s+(?:number|no\.?)){0,3}[^a-z]{0,10}"
                + re.escape(year) + r"[^a-z0-9]{0,4}0*" + re.escape(num)
                + r"\b|\b" + re.escape(year) + r"\s?-\s?0*" + re.escape(num) + r"\b")
    core = parts[1]
    return (r"(?:\br[-\s]{0,3}|resolution[^a-z0-9]{0,15})0*" + re.escape(core) + r"\b")


def extract_dollars(s: str) -> set[str]:
    return {re.sub(r"[,$]", "", d) for d in re.findall(r"\$[\d,]+(?:\.\d+)?", s or "")}


# ── Pass 1: signature anchor scan ─────────────────────────────────────


@dataclass
class Anchor:
    word_index: int
    sig_id: str
    kind: str
    strength: str
    text: str


@dataclass
class VoteMoment:
    start_wi: int
    end_wi: int
    anchor_count: int
    strong_count: int
    kinds: list[str]
    sample: str


def scan_anchors(t: Transcript) -> list[Anchor]:
    anchors: list[Anchor] = []
    for sig in SIGNATURES:
        for m in re.finditer(sig["pattern"], t.low):
            anchors.append(Anchor(
                word_index=t.word_index(m.start()),
                sig_id=sig["id"], kind=sig["kind"], strength=sig["strength"],
                text=m.group(0),
            ))
    anchors.sort(key=lambda a: a.word_index)
    return anchors


def cluster_vote_moments(anchors: list[Anchor], t: Transcript) -> list[VoteMoment]:
    """Group nearby anchors into vote moments. A moment is only founded by a
    strong anchor; weak anchors corroborate. Clusters made entirely of weak
    anchors are dropped (that's the probe-3 tally-false-positive fix)."""
    moments: list[VoteMoment] = []
    cluster: list[Anchor] = []

    def flush() -> None:
        if not cluster:
            return
        strong = [a for a in cluster if a.strength == "strong"]
        if not strong:
            cluster.clear()
            return
        s, e = cluster[0].word_index, cluster[-1].word_index
        mid = strong[0].word_index
        sample = " ".join(t.words[max(0, mid - 6): mid + 10])
        moments.append(VoteMoment(
            start_wi=s, end_wi=e, anchor_count=len(cluster),
            strong_count=len(strong),
            kinds=sorted({a.kind for a in cluster}),
            sample=sample,
        ))
        cluster.clear()

    for a in anchors:
        if cluster and a.word_index - cluster[-1].word_index > CLUSTER_GAP_WORDS:
            flush()
        cluster.append(a)
    flush()
    return moments


def signature_hit_summary(anchors: list[Anchor]) -> dict[str, int]:
    out: dict[str, int] = {}
    for a in anchors:
        out[a.sig_id] = out.get(a.sig_id, 0) + 1
    return out


# ── Pass 2: frame grounding (three-state) ──────────────────────────────


@dataclass
class EntityCheck:
    entity: str
    kind: str            # ref | dollar | token
    verdict: str         # pass | fail | uncheckable
    positions: list[int] = field(default_factory=list)


@dataclass
class FrameGrounding:
    frame_index: int
    entity_checks: list[EntityCheck]
    located_at: Optional[int]      # representative word index, if locatable
    near_vote_moment: Optional[bool]
    shape_flags: list[str]
    verdict: str                   # grounded | ungrounded | unlocatable


def _frame_search_text(frame: dict[str, Any]) -> str:
    return " ".join(str(frame.get(k) or "") for k in
                    ("motion_reference", "summary_sentence", "agenda_item", "context"))


def _frame_topic_text(frame: dict[str, Any]) -> str:
    """The vote's identity fields only. Alignment/location keyed on the full
    search text was the v0 dilution bug — agenda_item/context verbosity
    differs so much across families that identical votes scored ~0.25."""
    return " ".join(str(frame.get(k) or "") for k in
                    ("motion_reference", "summary_sentence"))


def check_frame_shape(frame: dict[str, Any]) -> list[str]:
    """Grammar/enum checks — the constraint-checker half of Stage 2."""
    flags: list[str] = []
    result = frame.get("vote_result")
    if result not in VOTE_RESULT_ENUM:
        flags.append(f"vote_result_outside_enum:{result!r}")
    method = frame.get("vote_method")
    if method is not None and method not in VOTE_METHOD_ENUM:
        flags.append(f"vote_method_outside_enum:{method!r}")
    tally = frame.get("tally") or {}
    if not isinstance(tally, dict):
        flags.append("tally_not_object")
        tally = {}
    for k, v in tally.items():
        if not isinstance(v, int) or v < 0:
            flags.append(f"tally_value_invalid:{k}={v!r}")
    pmv = frame.get("per_member_votes") or []
    for entry in pmv:
        if isinstance(entry, dict) and entry.get("vote") not in MEMBER_VOTE_ENUM:
            flags.append(f"member_vote_outside_enum:{entry.get('vote')!r}")
    aye, nay = tally.get("aye", 0), tally.get("nay", 0)
    if isinstance(aye, int) and isinstance(nay, int) and (aye or nay):
        if result == "passed" and nay > aye:
            flags.append("result_tally_mismatch:passed_with_nay_majority")
        if result == "failed" and aye > nay:
            flags.append("result_tally_mismatch:failed_with_aye_majority")
        if result == "tied" and aye != nay:
            flags.append("result_tally_mismatch:tied_with_unequal_tally")
    if pmv and isinstance(pmv, list):
        counted: dict[str, int] = {}
        for entry in pmv:
            if isinstance(entry, dict):
                counted[entry.get("vote", "?")] = counted.get(entry.get("vote", "?"), 0) + 1
        for cat in ("aye", "nay"):
            tv = tally.get(cat, 0)
            if tv and counted.get(cat, 0) and tv != counted[cat]:
                flags.append(f"tally_permember_mismatch:{cat}={tv}_vs_{counted[cat]}")
    return flags


def ground_frame(idx: int, frame: dict[str, Any], t: Transcript,
                 moments: list[VoteMoment]) -> FrameGrounding:
    search = _frame_search_text(frame)
    checks: list[EntityCheck] = []
    ref_positions: list[int] = []

    for ref in sorted(extract_refs(search)):
        hits = [t.word_index(m.start())
                for m in re.finditer(ref_grounding_pattern(ref), t.low)]
        checks.append(EntityCheck(ref, "ref", "pass" if hits else "fail", hits[:5]))
        ref_positions.extend(hits)

    # Whisper writes figures with spaced separators ('$199 ,750 ,036'), so the
    # haystack strips all of [,$ ] — a rare false digit-join is acceptable for
    # pass/uncheckable semantics (absence still never refutes, per three-state)
    stripped = re.sub(r"[,$\s]", "", t.low)
    for amount in sorted(extract_dollars(search)):
        digits = amount.split(".")[0]
        if stripped.find(digits) >= 0:
            approx = t.low.find(digits[:4])
            hits = [t.word_index(approx)] if approx >= 0 else []
            checks.append(EntityCheck(amount, "dollar", "pass", hits))
            ref_positions.extend(hits)
        else:
            # spoken-number normalization gap: absence isn't refutation
            checks.append(EntityCheck(amount, "dollar", "uncheckable", []))

    tokens = content_tokens(_frame_topic_text(frame) + " " + str(frame.get("agenda_item") or ""))
    window_center, distinct = t.locate_tokens(tokens) if tokens else (None, 0)
    density_floor = min(3, len(tokens)) if tokens else 0
    token_ok = bool(tokens) and distinct >= max(density_floor, round(0.4 * len(tokens)))
    if tokens:
        checks.append(EntityCheck(
            f"{distinct}/{len(tokens)} tokens co-occur in one window", "token",
            "pass" if token_ok else "fail",
            [window_center] if window_center is not None else []))

    # a ref/dollar hit is a sharper locator than the token window
    located_at = (int(sorted(ref_positions)[len(ref_positions) // 2])
                  if ref_positions else window_center)
    near = None
    if located_at is not None:
        near = any(m.start_wi - WINDOW_WORDS <= located_at <= m.end_wi + WINDOW_WORDS
                   for m in moments)

    ref_dollar = [c for c in checks if c.kind in ("ref", "dollar")]
    hard_fail = any(c.verdict == "fail" for c in ref_dollar)
    hard_pass = any(c.verdict == "pass" for c in ref_dollar)

    if located_at is None:
        verdict = "unlocatable"
    elif (hard_pass or token_ok) and not hard_fail and near is not False:
        verdict = "grounded"
    elif hard_fail or near is False:
        verdict = "ungrounded"
    else:
        verdict = "unlocatable"

    return FrameGrounding(
        frame_index=idx, entity_checks=checks, located_at=located_at,
        near_vote_moment=near, shape_flags=check_frame_shape(frame),
        verdict=verdict,
    )


# ── Pass 3: cross-family consensus ─────────────────────────────────────

DETERMINATE_FIELDS = ("vote_result", "tally", "refs")
JACCARD_MATCH_THRESHOLD = 0.25


def _frame_key_tokens(frame: dict[str, Any]) -> set[str]:
    return content_tokens(_frame_topic_text(frame))


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a | b) else 0.0


@dataclass
class FramePair:
    index_a: int
    index_b: int
    match_score: float
    matched_on: str                  # refs | tokens
    field_convergence: dict[str, Any] = field(default_factory=dict)
    determinate_divergence: list[str] = field(default_factory=list)


@dataclass
class ConsensusResult:
    pairs: list[FramePair]
    only_a: list[int]
    only_b: list[int]
    converged_pairs: int
    diverged_pairs: int


def align_frames(frames_a: list[dict], frames_b: list[dict]) -> ConsensusResult:
    """Greedy best-match alignment: shared resolution/ordinance refs dominate,
    content-token Jaccard breaks the rest. Unmatched frames are the coverage
    divergence the grounding pass then disambiguates (under-coverage by the
    other family vs over-extraction by this one)."""
    refs_a = [extract_refs(_frame_search_text(f)) for f in frames_a]
    refs_b = [extract_refs(_frame_search_text(f)) for f in frames_b]
    toks_a = [_frame_key_tokens(f) for f in frames_a]
    toks_b = [_frame_key_tokens(f) for f in frames_b]

    candidates: list[tuple[float, str, int, int]] = []
    for i in range(len(frames_a)):
        for j in range(len(frames_b)):
            if refs_a[i] and refs_b[j] and (refs_a[i] & refs_b[j]):
                candidates.append((2.0 + _jaccard(toks_a[i], toks_b[j]), "refs", i, j))
            else:
                sc = _jaccard(toks_a[i], toks_b[j])
                if sc >= JACCARD_MATCH_THRESHOLD:
                    candidates.append((sc, "tokens", i, j))
    candidates.sort(reverse=True)

    used_a: set[int] = set()
    used_b: set[int] = set()
    pairs: list[FramePair] = []
    for score, how, i, j in candidates:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        pairs.append(FramePair(i, j, round(score, 3), how))

    for pair in pairs:
        fa, fb = frames_a[pair.index_a], frames_b[pair.index_b]
        conv: dict[str, Any] = {}
        div: list[str] = []

        ra, rb = refs_a[pair.index_a], refs_b[pair.index_b]
        if ra or rb:
            conv["refs"] = {"a": sorted(ra), "b": sorted(rb), "equal": ra == rb}
            if ra and rb and ra != rb:
                div.append("refs")

        va, vb = fa.get("vote_result"), fb.get("vote_result")
        conv["vote_result"] = {"a": va, "b": vb, "equal": va == vb}
        if va != vb:
            div.append("vote_result")

        ta, tb = fa.get("tally") or {}, fb.get("tally") or {}
        sum_a = sum(v for v in ta.values() if isinstance(v, int))
        sum_b = sum(v for v in tb.values() if isinstance(v, int))
        if sum_a and sum_b:
            # determinate core = cast positions (aye/nay/abstain). Whether an
            # absent member appears in a vote's tally is bookkeeping convention
            # (m103996: families agreed 6-0 on every vote, differed only on
            # counting the absent councilmember) — noted, never flagged.
            equal = all(ta.get(k, 0) == tb.get(k, 0) for k in ("aye", "nay", "abstain"))
            conv["tally"] = {"a": ta, "b": tb, "equal": equal,
                            "absent_convention_differs":
                                ta.get("absent", 0) != tb.get("absent", 0)}
            if not equal:
                div.append("tally")
        else:
            conv["tally"] = {"a": ta, "b": tb, "equal": None}

        ma, mb = fa.get("vote_method"), fb.get("vote_method")
        conv["vote_method"] = {"a": ma, "b": mb, "equal": ma == mb}

        conv["summary_similarity"] = round(_jaccard(
            content_tokens(fa.get("summary_sentence", "")),
            content_tokens(fb.get("summary_sentence", ""))), 3)

        pair.field_convergence = conv
        pair.determinate_divergence = div

    only_a = [i for i in range(len(frames_a)) if i not in used_a]
    only_b = [j for j in range(len(frames_b)) if j not in used_b]
    diverged = sum(1 for p in pairs if p.determinate_divergence)
    return ConsensusResult(
        pairs=pairs, only_a=only_a, only_b=only_b,
        converged_pairs=len(pairs) - diverged, diverged_pairs=diverged,
    )


# ── Pass 4: output audit (key_decisions ↔ frames) ─────────────────────


@dataclass
class DecisionAudit:
    ordinal: int
    text: str
    refs: list[str]
    dollars: list[str]
    matched_frame: Optional[int]
    match_score: float
    transcript_ref_grounding: str      # pass | fail | none-claimed
    verdict: str                       # backed | unbacked | weakly_backed


@dataclass
class OutputAudit:
    decisions: list[DecisionAudit]
    backed: int
    unbacked: int
    frames_not_in_output: list[int]
    has_core_markup: bool


def parse_key_decisions(kd_text: str) -> tuple[list[str], bool]:
    """Split a cached key_decisions output into per-decision texts. Handles
    both live shapes: plain numbered lists (worker path) and <core>/<nuance>
    marked-up lists (CLI regeneration path, per the S-128 loader split)."""
    has_markup = "<core>" in kd_text
    clean = re.sub(r"</?(?:core|nuance)>", "", kd_text)
    clean = clean.replace("**", "")
    items = re.split(r"(?m)^\s*\d+[.)]\s+", clean)
    decisions = [it.strip().replace("\n", " ") for it in items[1:] if it.strip()]
    return decisions, has_markup


def audit_output(kd_text: str, frames: list[dict], groundings: list[FrameGrounding],
                 t: Transcript) -> OutputAudit:
    decisions, has_markup = parse_key_decisions(kd_text)
    frame_refs = [extract_refs(_frame_search_text(f)) for f in frames]
    frame_toks = [_frame_key_tokens(f) for f in frames]

    audits: list[DecisionAudit] = []
    matched_frames: set[int] = set()
    for n, dtext in enumerate(decisions, start=1):
        drefs = extract_refs(dtext)
        ddollars = sorted(extract_dollars(dtext))
        dtoks = content_tokens(dtext)

        best, best_score, matched_on = None, 0.0, "none"
        for fi in range(len(frames)):
            if drefs and frame_refs[fi] and (drefs & frame_refs[fi]):
                score = 2.0 + _jaccard(dtoks, frame_toks[fi])
                how = "refs"
            else:
                score = _jaccard(dtoks, frame_toks[fi])
                how = "tokens"
            if score > best_score:
                best, best_score, matched_on = fi, score, how
        if best is not None and (matched_on == "refs" or best_score >= 0.18):
            matched_frames.add(best)
        else:
            best = None

        if drefs:
            ref_hits = [bool(re.search(ref_grounding_pattern(ref), t.low))
                        for ref in drefs]
            ref_grounding = "pass" if all(ref_hits) else "fail"
        else:
            ref_grounding = "none-claimed"

        if best is not None and (ref_grounding != "fail"):
            verdict = "backed"
        elif ref_grounding == "pass" or (dtoks and _token_presence(dtoks, t) >= 0.6):
            verdict = "weakly_backed"      # grounds in transcript, no frame match
        else:
            verdict = "unbacked"

        audits.append(DecisionAudit(
            ordinal=n, text=dtext[:220], refs=sorted(drefs), dollars=ddollars,
            matched_frame=best, match_score=round(best_score, 3),
            transcript_ref_grounding=ref_grounding, verdict=verdict,
        ))

    grounded_frames = {g.frame_index for g in groundings if g.verdict == "grounded"}
    not_in_output = sorted(grounded_frames - matched_frames)
    return OutputAudit(
        decisions=audits,
        backed=sum(1 for a in audits if a.verdict == "backed"),
        unbacked=sum(1 for a in audits if a.verdict == "unbacked"),
        frames_not_in_output=not_in_output,
        has_core_markup=has_markup,
    )


def _token_presence(tokens: set[str], t: Transcript) -> float:
    if not tokens:
        return 0.0
    return sum(1 for tok in tokens if tok in t.norm) / len(tokens)


# ── Teacher input: signature gaps ──────────────────────────────────────


def signature_gap_windows(frames: list[dict], groundings: list[FrameGrounding],
                          moments: list[VoteMoment], t: Transcript,
                          radius: int = 60) -> list[dict[str, Any]]:
    """Windows around frames that ground by entity but sit near NO vote
    moment — the deterministic input channel for the future LLM-teacher
    loop (propose → backtest → promote, never auto-promote)."""
    gaps: list[dict[str, Any]] = []
    for g in groundings:
        if g.located_at is not None and g.near_vote_moment is False:
            lo = max(0, g.located_at - radius)
            hi = min(len(t.words), g.located_at + radius)
            gaps.append({
                "frame_index": g.frame_index,
                "word_index": g.located_at,
                "window": " ".join(t.words[lo:hi]),
            })
    return gaps


def to_jsonable(obj: Any) -> Any:
    if isinstance(obj, (Anchor, VoteMoment, FrameGrounding, EntityCheck,
                        FramePair, ConsensusResult, DecisionAudit, OutputAudit)):
        return asdict(obj)
    if isinstance(obj, list):
        return [to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    return obj
