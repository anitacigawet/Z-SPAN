"""
zspan_pipeline.symbols — build the [SYMBOLS] linker contract block
prepended to synthesis queries for output types that depend on
canonical names (motions, votes, tracked_claims, quotes,
member_quotes_topic, member_attendance, etc.).

Replaces the older prose persona preamble (T-006), which
was framed as an informational hint ("here are the names, use these
spellings") and empirically failed — models still emitted "Mayor
Watkins" / "Counselor Stehly" / "Councilmember Dykins" because the
soft directive didn't bind output.

The new block is framed as a LINKER CONTRACT: a structured symbol table
with canonical forms + accepted_aliases, plus directive language that
treats the table as a hard binding — "your output MUST link every
symbolic reference through this table" instead of "please use these
spellings." Mirrors the compiler/linker metaphor of CONVERSATIONAL_
COMPILER_SPEC.md: the synthesis model is the front-end producing typed-IR
output; the symbols block is the link-time symbol table it resolves
references against before emitting.

Per James 2026-06-05: this extends the compiler vocabulary from Track
A (rendering) into Track B (extraction). Model-as-linker. The
block is generated dynamically from authoritative sources
(`council_members` table + `whisper_vocabulary_hints` from
city_intelligence + auto-apply rows from `city_vocabulary_corrections`)
so it auto-stays-fresh when the roster changes or new corrections are
promoted.

Public API:
    build_symbols_block(city_name: str) -> str
        Returns the full [SYMBOLS] ... [/SYMBOLS] block ready to
        prepend. Returns "" when there's no city or no data.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)


_PARSERS_DIR = Path(__file__).resolve().parent.parent / "council_navigator" / "parsers"
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))


# Predictable role-prefix variants models emit when echoing council
# member names. The motions extraction 2026-06-05 surfaced "Counselor"
# (which `_NAME_TITLE_PREFIXES` already handles post-hoc); listing them
# here as accepted_aliases moves the same knowledge UPSTREAM into the
# linker contract so the model can use the canonical form directly
# instead of us correcting after the fact.
_COUNCIL_MEMBER_ROLE_PREFIXES = (
    "Council Member",
    "Councilmember",
    "Councilman",
    "Councilwoman",
    "Councilor",
    "Counselor",  # model-emitted variant of Councilor — caught empirically
)


def _derive_member_aliases(canonical: str, role: str) -> List[str]:
    """Generate the predictable alias variants for a single council
    member. Order matters: last-name-only first (most-frequently-emitted
    form), then short-form, then role-prefixed variants. The bridge's
    matcher and the model both benefit from order-as-signal.

    Examples (Ken Watkins / Mayor):
        ['Watkins', 'Mayor Watkins', 'Mayor Ken Watkins', 'the Mayor']
    Examples (Jamie Scott Stehly / Council Member):
        ['Stehly', 'Jamie Stehly', 'Council Member Stehly',
         'Councilmember Stehly', 'Councilman Stehly', 'Councilwoman Stehly',
         'Councilor Stehly', 'Counselor Stehly', 'Council Member Jamie Scott Stehly']
    """
    parts = canonical.split()
    if not parts:
        return []
    last = parts[-1]
    first = parts[0]
    aliases: List[str] = [last]
    if len(parts) > 2:
        # Short form skipping middle names ("Jamie Scott Stehly" → "Jamie Stehly")
        aliases.append(f"{first} {last}")

    role_lower = (role or "").strip().lower()
    if role_lower == "mayor":
        aliases.extend([f"Mayor {last}", f"Mayor {canonical}", "the Mayor"])
    elif role_lower == "vice mayor":
        aliases.extend([f"Vice Mayor {last}", f"Vice Mayor {canonical}", "the Vice Mayor"])
    else:
        # Council member (or unknown role — same variants)
        for prefix in _COUNCIL_MEMBER_ROLE_PREFIXES:
            aliases.append(f"{prefix} {last}")
        aliases.append(f"Council Member {canonical}")
    return aliases


def _dedupe_preserve_first(items: List[str]) -> List[str]:
    """Case-insensitive dedup preserving first-seen casing."""
    seen = set()
    out = []
    for it in items:
        k = (it or "").lower()
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def _categorize_hints(hints) -> tuple[List[str], List[str]]:
    """Split whisper_vocabulary_hints into (streets, proper_nouns)."""
    streets: List[str] = []
    proper_nouns: List[str] = []
    if not isinstance(hints, list):
        return streets, proper_nouns
    for h in hints:
        if isinstance(h, dict):
            term = (h.get("term") or "").strip()
            if not term:
                continue
            cat = (h.get("category") or "").lower()
            if cat == "street":
                streets.append(term)
            elif cat == "person":
                # Persons in vocab hints (e.g., a non-council figure
                # the meeting references) don't belong in the council
                # roster but DO benefit from canonical-spelling listing.
                proper_nouns.append(term)
            else:
                proper_nouns.append(term)
        elif isinstance(h, str) and h.strip():
            proper_nouns.append(h.strip())
    return streets, proper_nouns


def build_symbols_block(city_name: str) -> str:
    """Build the [SYMBOLS] linker contract block for a city.

    Pulls from three authoritative sources:
      1. `council_members` table → canonical names + role + derived aliases
      2. `city_intelligence/<slug>.json#whisper_vocabulary_hints` →
         streets + proper nouns
      3. `city_vocabulary_corrections` (auto_apply=1) → `wrong` is added
         as an `accepted_alias` of `right` so the model links the
         misspelling to the canonical form directly (e.g., Dykins becomes
         an accepted alias of Dykens). Corrections whose `right` doesn't
         match a roster member or vocab hint land in a residual
         `additional_spellings` section.

    Returns "" when the city has no council_members data — the bridge
    falls through to the no-preamble path gracefully (same as today).
    """
    if not city_name:
        return ""

    try:
        from database import get_council_members, load_city_intelligence, load_vocabulary_corrections
    except Exception as e:
        logger.warning("symbols.build_symbols_block: DB import failed (%s); returning empty", e)
        return ""

    try:
        members = get_council_members(city_name) or []
    except Exception as e:
        logger.warning("symbols.build_symbols_block: get_council_members(%s) raised (%s)", city_name, e)
        members = []
    try:
        intel = load_city_intelligence(city_name) or {}
    except Exception as e:
        logger.warning("symbols.build_symbols_block: load_city_intelligence(%s) raised (%s)", city_name, e)
        intel = {}
    try:
        corrections = load_vocabulary_corrections(city_name, auto_apply_only=True) or []
    except Exception as e:
        logger.warning("symbols.build_symbols_block: load_vocabulary_corrections(%s) raised (%s)", city_name, e)
        corrections = []

    if not members and not intel:
        logger.info("symbols.build_symbols_block: no data for %s; skipping block", city_name)
        return ""

    # Index corrections by the canonical (`right`) value AND by its
    # last-name suffix so a "Dykins → Dykens" correction surfaces under
    # both "Dykens" and "Jim Dykens".
    corr_by_right: Dict[str, List[str]] = {}
    for c in corrections:
        right = (c.get("right") or "").strip()
        wrong = (c.get("wrong") or "").strip()
        if not right or not wrong:
            continue
        corr_by_right.setdefault(right, []).append(wrong)
        # Also index by last-name suffix for members whose canonical is
        # "Jim Dykens" but the correction's right is just "Dykens".
        last_word = right.split()[-1]
        if last_word != right:
            corr_by_right.setdefault(last_word, []).append(wrong)

    state = (intel.get("state") or "").strip()
    county = (intel.get("county") or "").strip()
    canonical_city = (intel.get("canonical_name") or city_name).strip()

    lines: List[str] = []
    lines.append("[SYMBOLS — authoritative canonical references for this meeting]")
    lines.append("")
    lines.append(
        "Your output MUST link every symbolic reference (council members, "
        "streets, proper nouns) through the table below. Treat this as a "
        "HARD LINKER CONTRACT, not a soft hint:"
    )
    lines.append("")
    lines.append(
        "  - NEVER output a name or proper noun that is not listed below "
        "as either a `canonical` form or one of its `accepted_aliases`."
    )
    lines.append(
        "  - When the transcript uses an accepted alias (e.g., \"Mayor "
        "Watkins\" or \"Counselor Stehly\"), output the CANONICAL form "
        "(e.g., \"Ken Watkins\", \"Jamie Scott Stehly\") instead."
    )
    lines.append(
        "  - If a candidate reference does not link to any canonical "
        "form or accepted alias, use the closest canonical form from "
        "this table — do not invent new names."
    )
    lines.append(
        "  - This rule applies to every output field that names a person, "
        "place, or proper noun: `speaker`, `per_member_votes[].member`, "
        "`motion_text`, `claim_text`, `context`, free-form prose — all of it."
    )
    lines.append("")

    # City-level context (replaces the old prose preamble's opening sentence)
    if state or county:
        ctx_parts = [canonical_city]
        if county:
            ctx_parts.append(f"located in {county}")
        if state:
            ctx_parts.append(f"in {state}")
        lines.append(f"## city_context")
        lines.append(f"  {', '.join(ctx_parts)}.")
        lines.append("")

    # Build the full member entry list FIRST (before emitting any other
    # section) so we know which terms are already covered via member
    # canonicals + aliases. Anything in this set must be excluded from
    # proper_nouns / streets / additional_spellings to avoid surfacing
    # the same term in multiple linker sections (which would confuse
    # the model about which one is authoritative).
    covered_terms_ci: set[str] = set()
    member_entries: List[tuple[str, str, List[str]]] = []  # (canonical, role, aliases)

    if members:
        role_order = {"mayor": 0, "vice mayor": 1, "council member": 2}
        sorted_members = sorted(
            members,
            key=lambda m: (
                role_order.get((m.get("role") or "council member").lower(), 9),
                m.get("name") or "",
            ),
        )
        for m in sorted_members:
            canonical = (m.get("name") or "").strip()
            if not canonical:
                continue
            role = (m.get("role") or "Council Member").strip()
            aliases = _derive_member_aliases(canonical, role)
            last = canonical.split()[-1]
            if last in corr_by_right:
                aliases.extend(corr_by_right[last])
            if canonical in corr_by_right:
                aliases.extend(corr_by_right[canonical])
            aliases = _dedupe_preserve_first(aliases)
            member_entries.append((canonical, role, aliases))
            covered_terms_ci.add(canonical.lower())
            covered_terms_ci.add(last.lower())
            for a in aliases:
                covered_terms_ci.add(a.lower())

    # ── council_members section emission ──────────────────────────
    if member_entries:
        lines.append("## council_members")
        for canonical, role, aliases in member_entries:
            lines.append(f"- canonical: {json.dumps(canonical)}")
            lines.append(f"  role: {role}")
            lines.append(f"  accepted_aliases: {json.dumps(aliases, ensure_ascii=False)}")
        lines.append("")

    # ── streets + proper_nouns from whisper_vocabulary_hints ──────
    hints = intel.get("whisper_vocabulary_hints") or []
    streets, proper_nouns = _categorize_hints(hints)

    # Drop hint entries already absorbed by the council_members section
    # (e.g., someone promoted "Councilmember Stehly" or "Dykens" to
    # whisper_vocabulary_hints — they're already aliases of a member).
    streets = [s for s in streets if s.lower() not in covered_terms_ci]
    proper_nouns = [p for p in proper_nouns if p.lower() not in covered_terms_ci]

    if streets:
        lines.append("## streets")
        for s in streets:
            entry_aliases = _dedupe_preserve_first(corr_by_right.get(s, []))
            lines.append(f"- canonical: {json.dumps(s)}")
            if entry_aliases:
                lines.append(f"  accepted_aliases: {json.dumps(entry_aliases, ensure_ascii=False)}")
            covered_terms_ci.add(s.lower())
            for a in entry_aliases:
                covered_terms_ci.add(a.lower())
        lines.append("")

    if proper_nouns:
        lines.append("## proper_nouns")
        for p in proper_nouns:
            entry_aliases = _dedupe_preserve_first(corr_by_right.get(p, []))
            lines.append(f"- canonical: {json.dumps(p)}")
            if entry_aliases:
                lines.append(f"  accepted_aliases: {json.dumps(entry_aliases, ensure_ascii=False)}")
            covered_terms_ci.add(p.lower())
            for a in entry_aliases:
                covered_terms_ci.add(a.lower())
        lines.append("")

    # ── additional_spellings — residual corrections whose `right` AND
    # `wrong` are both unrepresented above. Common cases: multi-word
    # phrases ("Sedated Streets" → "the stated streets") or civic
    # vocabulary ("POSOS systems" → "POS systems") not yet in
    # whisper_vocabulary_hints.
    residual = []
    for c in corrections:
        right = (c.get("right") or "").strip()
        wrong = (c.get("wrong") or "").strip()
        if not right or not wrong:
            continue
        if right.lower() in covered_terms_ci or wrong.lower() in covered_terms_ci:
            # Both sides already represented somewhere above — skip to
            # avoid triple-coverage.
            continue
        residual.append((right, wrong))

    if residual:
        lines.append("## additional_spellings")
        for right, wrong in residual:
            lines.append(f"- canonical: {json.dumps(right)} (NOT {json.dumps(wrong)})")
        lines.append("")

    # Trailing reinforcement (recency bias — repeat the constraint near
    # the end so it's in the model's working memory when it starts
    # generating output).
    lines.append(
        "Before emitting any output, re-check every name and proper "
        "noun against the table above. If a candidate doesn't link, "
        "substitute the canonical form. Do not output role-prefixed "
        "forms (\"Mayor Watkins\") or partial names (\"Watkins\") "
        "when a canonical (\"Ken Watkins\") exists."
    )
    lines.append("")
    lines.append("[/SYMBOLS]")

    return "\n".join(lines)
