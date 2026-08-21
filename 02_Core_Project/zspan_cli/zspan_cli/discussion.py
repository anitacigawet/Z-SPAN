"""Deterministic decision-Discussion assembly — the local receipts.

For each key_decisions item, locate the transcript window where the
decision actually happened and emit the site's own Discussion shapes
(the .preview sidecar contracts: quotes with word timings + routing
entries) so BroadcastPage's Discussion accordion and SyncedQuote
karaoke light up unchanged over the private workspace.

No LLM anywhere. The locator is anchor search using the same grounding
primitives the gate checks with — resolution/ordinance references
(matched against Whisper's spoken renderings), dollar amounts (against
the separator-stripped digit stream), and quoted spans (verbatim in
the normalized text). When those stronger anchors are absent, a
conservative exact phrase derived from the decision's <core> span may
locate it. The vote/adoption signature library extends a window through
the moment the body actually acted. A decision whose anchors don't
locate gets NO Discussion entry: honest absence, never a guessed window.

The workspace holds FULL word timestamps (richer than the flagship's
paste-era sidecars), so every located window karaoke-plays.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from zspan_cli.gate import split_key_decisions
from zspan_cli.grounding import (
    SIGNATURES,
    extract_dollars,
    extract_quoted_spans,
    extract_refs,
    norm_text,
    ref_grounding_pattern,
    strip_output_markup,
)

# The client renders at most 5 key decisions (BroadcastPage's
# parseNumberedList caps there); windows past that would never show.
MAX_DECISION_ITEMS = 5

# Window shaping, in words. LEAD/TAIL give the caught moment breathing
# room; CLUSTER_GAP groups anchors that belong to the same stretch of
# the meeting; VOTE_REACH is how far past the last anchor the body's
# vote moment may sit and still belong to this decision's window;
# WINDOW_CAP keeps a karaoke chip a listenable moment, not a chapter.
LEAD_WORDS = 40
TAIL_WORDS = 40
CLUSTER_GAP_WORDS = 200
VOTE_REACH_WORDS = 300
WINDOW_CAP_WORDS = 220

_ANCHOR_WEIGHT = {"ref": 3, "quote": 2, "dollar": 1, "core_phrase": 1}

MIN_CORE_PHRASE_TOKENS = 2
MIN_CORE_PHRASE_CHARS = 12

_CORE_SPAN_RE = re.compile(r"<core>(.*?)</core>", re.IGNORECASE | re.DOTALL)
_CORE_ACTION_PREFIX_RE = re.compile(
    r"^(?:(?:the\s+)?(?:council|board|commission)\s+)?"
    r"(?:approved|adopted|authorized|awarded|accepted|denied|rejected|"
    r"continued|tabled|amended|ratified|appointed|confirmed|selected|"
    r"recommended|passed|enacted|directed|voted(?:\s+to)?)\b"
    r"(?:\s+(?:to|for|on|the|a|an)\b)*",
)
_CORE_BOILERPLATE_PREFIX_RE = re.compile(
    r"^(?:(?:lease|intergovernmental)\s+agreement)\s+"
    r"(?:for|to|with|between)\s+(?:the\s+|a\s+|an\s+)?",
)


@dataclass
class _Anchor:
    word_idx: int  # token-space index
    kind: str      # ref | dollar | quote | core_phrase
    name: str      # what found it, in plain words ("resolution 2026R-15")


class _SearchIndex:
    """The transcript joined + normalized with OFFSET MAPS, so a match
    in any haystack (low text / digit stream / normalized text) walks
    back to a word index — grounding.py's haystacks, made locatable."""

    def __init__(self, words: list[dict]):
        self.words = words
        # Token space: the non-empty tokens + their original indices
        # (timings live on the original rows).
        self.kept: list[int] = []
        tokens: list[str] = []
        for i, w in enumerate(words):
            token = str(w.get("word") or "").strip()
            if token:
                self.kept.append(i)
                tokens.append(token)
        self.tokens = tokens
        self.low = " ".join(tokens).lower()

        # char offset → token index
        self._cum: list[int] = []
        n = 0
        for t in tokens:
            self._cum.append(n)
            n += len(t) + 1

        # Digit stream with per-digit char offsets (the dollar check's
        # haystack, offset-mapped).
        self.digits = ""
        self._digit_offsets: list[int] = []
        # Normalized text with per-char offsets — EXACTLY norm_text's
        # output for this input (low is already single-space-joined, so
        # norm_text's whitespace collapse is the identity here and only
        # the character filter applies).
        self.norm = ""
        self._norm_offsets: list[int] = []
        norm_chars: list[str] = []
        for idx, ch in enumerate(self.low):
            if ch.isdigit():
                self.digits += ch
                self._digit_offsets.append(idx)
            if ("a" <= ch <= "z") or ch.isdigit() or ch == " ":
                norm_chars.append(ch)
                self._norm_offsets.append(idx)
        self.norm = "".join(norm_chars)

    def token_at_char(self, char_offset: int) -> int:
        from bisect import bisect_right
        return max(0, bisect_right(self._cum, char_offset) - 1)

    def token_at_digit(self, digit_pos: int) -> int:
        return self.token_at_char(self._digit_offsets[digit_pos])

    def token_at_norm(self, norm_pos: int) -> int:
        return self.token_at_char(self._norm_offsets[norm_pos])

    def strong_vote_token_indices(self) -> list[int]:
        out = []
        for sig in SIGNATURES:
            if sig["strength"] != "strong":
                continue
            for m in re.finditer(sig["pattern"], self.low):
                out.append(self.token_at_char(m.start()))
        return sorted(out)


def align_verbatim_quote(
    quote_text: str,
    words: list[dict],
    timestamp_hint: float | int | None,
) -> list[dict]:
    """Map one claimed-verbatim quote onto transcript word timings.

    Every normalized exact match is considered. When the quote occurs more
    than once, its structured video timestamp selects the nearest occurrence;
    without a usable hint the first occurrence wins deterministically. An
    unlocatable quote returns honest-empty rather than approximate timings.
    """
    if not quote_text or not words:
        return []
    index = _SearchIndex(words)
    needle = norm_text(quote_text)
    if not needle or not index.norm:
        return []

    pattern = rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])"
    matches = list(re.finditer(pattern, index.norm))
    if not matches:
        return []

    hint: float | None = None
    if isinstance(timestamp_hint, (int, float)) and not isinstance(timestamp_hint, bool):
        candidate = float(timestamp_hint)
        if math.isfinite(candidate) and candidate >= 0:
            hint = candidate

    if hint is None:
        match = matches[0]
    else:
        def distance_from_hint(candidate_match) -> float:
            token_idx = index.token_at_norm(candidate_match.start())
            original_idx = index.kept[token_idx]
            return abs(float(words[original_idx].get("start") or 0.0) - hint)

        match = min(matches, key=distance_from_hint)

    start_token = index.token_at_norm(match.start())
    end_token = index.token_at_norm(match.end() - 1) + 1
    aligned_words = [words[index.kept[i]] for i in range(start_token, end_token)]
    return [
        {
            "word": str(word.get("word") or "").strip(),
            "start_ms": int(float(word.get("start") or 0.0) * 1000),
            "end_ms": int(float(word.get("end") or 0.0) * 1000),
        }
        for word in aligned_words
    ]


def _unique_core_phrase_anchor(item_text: str, index: _SearchIndex) -> _Anchor | None:
    """Return one exact, unique transcript phrase from one <core> span.

    Action language and two transactional lead-ins are presentation
    boilerplate, not the topic. Candidate phrases remain contiguous and
    exact; shrinking only finds the longest source phrase that Whisper
    actually preserved. Two-token anchors face an additional character
    floor so short/common cores do not become locators.
    """
    spans = _CORE_SPAN_RE.findall(item_text or "")
    if len(spans) != 1:
        return None
    core = norm_text(strip_output_markup(spans[0]))
    core = _CORE_ACTION_PREFIX_RE.sub("", core, count=1).strip()
    core = _CORE_BOILERPLATE_PREFIX_RE.sub("", core, count=1).strip()
    tokens = core.split()

    for size in range(len(tokens), MIN_CORE_PHRASE_TOKENS - 1, -1):
        for start in range(0, len(tokens) - size + 1):
            phrase = " ".join(tokens[start:start + size])
            if len(phrase.replace(" ", "")) < MIN_CORE_PHRASE_CHARS:
                continue
            pattern = rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])"
            matches = list(re.finditer(pattern, index.norm))
            if len(matches) == 1:
                return _Anchor(
                    index.token_at_norm(matches[0].start()),
                    "core_phrase",
                    f'the exact core phrase "{phrase}"',
                )
    return None


def _find_anchors(item_text: str, index: _SearchIndex) -> list[_Anchor]:
    anchors: list[_Anchor] = []
    check = strip_output_markup(item_text)

    for ref in sorted(extract_refs(check)):
        pattern = ref_grounding_pattern(ref)
        for m in re.finditer(pattern, index.low):
            anchors.append(_Anchor(index.token_at_char(m.start()), "ref",
                                   ref.replace("r-", "resolution ", 1)
                                   if ref.startswith("r-")
                                   else ref.replace("ord-", "ordinance ", 1)))

    for amount in sorted(extract_dollars(check)):
        digits = re.sub(r"\D", "", amount)
        if not digits:
            continue
        # extract_dollars strips separators; put them back for the
        # human-readable rationale line.
        whole, dot, cents = amount.partition(".")
        try:
            pretty = f"${int(whole):,}{dot}{cents}"
        except ValueError:
            pretty = f"${amount}"
        pos = index.digits.find(digits)
        while pos != -1:
            anchors.append(_Anchor(index.token_at_digit(pos), "dollar",
                                   f"the {pretty} amount"))
            pos = index.digits.find(digits, pos + 1)

    for span in extract_quoted_spans(check):
        normed = norm_text(span)
        pos = index.norm.find(normed)
        while pos != -1:
            anchors.append(_Anchor(index.token_at_norm(pos), "quote",
                                   "a verbatim quote"))
            pos = index.norm.find(normed, pos + 1)

    # References, amounts, and explicit quotes remain authoritative. The
    # core phrase is a fallback only, never a competing signal that can pull
    # a stronger anchor into a different cluster.
    if not anchors:
        core_anchor = _unique_core_phrase_anchor(item_text, index)
        if core_anchor is not None:
            anchors.append(core_anchor)

    anchors.sort(key=lambda a: a.word_idx)
    return anchors


def _best_cluster(anchors: list[_Anchor]) -> list[_Anchor]:
    """Group anchors that sit within CLUSTER_GAP of each other; return
    the highest-scoring group (refs outweigh quotes outweigh dollars;
    ties go to the LATER cluster — decisions culminate in the vote)."""
    clusters: list[list[_Anchor]] = []
    for a in anchors:
        if clusters and a.word_idx - clusters[-1][-1].word_idx <= CLUSTER_GAP_WORDS:
            clusters[-1].append(a)
        else:
            clusters.append([a])

    def score(cluster: list[_Anchor]) -> int:
        return sum(_ANCHOR_WEIGHT[a.kind] for a in cluster)

    best = clusters[0]
    for c in clusters[1:]:
        if score(c) >= score(best):
            best = c
    return best


def _shape_window(cluster: list[_Anchor], vote_indices: list[int],
                  token_count: int) -> tuple[int, int]:
    """[start, end) in token space: lead-in before the first anchor,
    tail after the last, extended through the nearest strong vote
    moment ahead, capped to a listenable length (the cap slides to keep
    the most anchors, preferring the vote end when present)."""
    first = cluster[0].word_idx
    last = cluster[-1].word_idx

    vote_end = None
    for vi in vote_indices:
        if last <= vi <= last + VOTE_REACH_WORDS:
            vote_end = vi
            break
    start = max(0, first - LEAD_WORDS)
    end = min(token_count, (vote_end if vote_end is not None else last) + TAIL_WORDS)

    if end - start > WINDOW_CAP_WORDS:
        # Keep the ending (the vote / the last anchor) and cap backward.
        start = max(0, end - WINDOW_CAP_WORDS)
    return start, end


def _rationale(cluster: list[_Anchor], vote_extended: bool) -> str:
    names: list[str] = []
    for a in cluster:
        if a.name not in names:
            names.append(a.name)
        if len(names) == 2:
            break
    located = " and ".join(names)
    tail = ", through the vote moment" if vote_extended else ""
    return f"located in the record by {located}{tail} — no AI selection"


def citation_coverage(key_decisions: str, routing: list[dict]) -> dict:
    """Per-decision citation coverage over the router's canonical routes."""
    items = split_key_decisions(key_decisions or "") or []
    required = list(range(1, len(items) + 1))
    required_set = set(required)
    produced = sorted({
        route.get("decision_index")
        for route in routing
        if isinstance(route, dict)
        and route.get("bucket") == "decision_bound"
        and route.get("decision_index") in required_set
    })
    missing = sorted(required_set - set(produced))
    state = (
        "no_decisions_pending_classification" if not required
        else "citation_incomplete" if missing
        else "valid"
    )
    return {
        "state": state,
        "complete": not missing,
        "required_decision_indices": required,
        "produced_decision_indices": produced,
        "missing_decision_indices": missing,
    }


def build_discussion(key_decisions: str, words: list[dict]) -> dict:
    """The preview-sidecar payloads for one meeting:
    {"quotes": [PreviewQuote...], "routing": [RoutingEntry...]} —
    exactly the shapes BroadcastPage's decision-bound Discussion
    consumes. Decisions whose anchors don't locate are absent."""
    items = split_key_decisions(key_decisions or "") or []
    items = items[:MAX_DECISION_ITEMS]
    if not items or not words:
        routing: list[dict] = []
        return {
            "quotes": [],
            "routing": routing,
            "summary": {
                "standalone_count": 0,
                "decision_bound_count": 0,
                "drop_count": 0,
                "citation_coverage": citation_coverage(key_decisions, routing),
            },
        }

    index = _SearchIndex(words)
    if not index.tokens:
        routing = []
        return {
            "quotes": [],
            "routing": routing,
            "summary": {
                "standalone_count": 0,
                "decision_bound_count": 0,
                "drop_count": 0,
                "citation_coverage": citation_coverage(key_decisions, routing),
            },
        }
    vote_indices = index.strong_vote_token_indices()

    quotes: list[dict] = []
    routing: list[dict] = []
    for item_number, item_text in enumerate(items, start=1):
        anchors = _find_anchors(item_text, index)
        if not anchors:
            continue  # honest absence — nothing located, nothing shown
        cluster = _best_cluster(anchors)
        start, end = _shape_window(cluster, vote_indices, len(index.tokens))
        vote_extended = any(
            cluster[-1].word_idx < vi < end for vi in vote_indices
        )

        original = [index.kept[i] for i in range(start, end)]
        window_words = [words[i] for i in original]
        quotes.append({
            "speaker_name": "From the meeting record",
            "speaker_role": None,
            "speaker_class": "record",
            "quote_text": " ".join(
                str(w.get("word") or "").strip() for w in window_words
            ),
            # The router-bound timestamp is the located evidence itself;
            # word_timings still carry the wider listening window.
            "video_timestamp_seconds": float(
                words[index.kept[cluster[0].word_idx]].get("start") or 0.0
            ),
            "selection_rationale": _rationale(cluster, vote_extended),
            "word_timings": [
                {
                    "word": str(w.get("word") or "").strip(),
                    "start_ms": int(float(w.get("start") or 0.0) * 1000),
                    "end_ms": int(float(w.get("end") or 0.0) * 1000),
                }
                for w in window_words
            ],
        })
        routing.append({
            "quote_index": len(quotes) - 1,
            "bucket": "decision_bound",
            "decision_index": item_number,
        })

    coverage = citation_coverage(key_decisions, routing)
    return {
        "quotes": quotes,
        "routing": routing,
        "summary": {
            "standalone_count": 0,
            "decision_bound_count": len(routing),
            "drop_count": 0,
            "citation_coverage": coverage,
        },
    }
