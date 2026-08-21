#!/usr/bin/env python3.11
"""audit_silent_emissions — post-generation AST audit gate for Codex-generated parsers.

Detects three classes of silent-emission failure that the v6 Codex doctrine
(`AGENTS.md` § "Required guards beyond the 10-criterion audit") prescribes
against. The v1-v6 trial track empirically showed that each doctrine iteration
closes the named surface but the bug-shape family migrates to whichever
architectural altitude is not explicitly policed yet. Six iterations of
that pattern (per `01_Project_Overview/CODEX_DOCTRINE_PASS_V6_2026-06-16.md`)
is strong enough evidence that doctrine-by-extension converges too slowly.

This script is the methodology-evolution call: a deterministic AST sweep that
catches the *shape* of the bug family (silent emission decision not in the
trail) at every architectural altitude in one mechanical pass, instead of
chasing individual altitudes through more doctrine refinement.

Three sweeps:

1. **Case L — per-field extraction helpers.** Every function shaped like
   ``_extract_X`` / ``_classify_X`` / ``_meeting_X`` / ``_field_X`` that has
   a silent ``return ""`` / ``return ''`` branch MUST have at least one
   ``logger.warning(...)`` call in its body. If the helper returns empty
   without any warning anywhere in the function, that's a Case L violation.

2. **Regex pitfalls.** Every ``re.compile(<pattern>, ...)`` call whose pattern
   ends in ``\\b`` MUST have a word-character preceding the ``\\b`` OR be the
   doctrine-locked safe pattern ``\\bcancell?ed\\b``. The Willcox/Peoria/Sedona
   trial parsers all had ``[AP]\\.?M\\.?\\b`` — ``\\b`` after a literal ``.``
   silently fails to match dotted-suffix variants (``5:30 a.m.``) because
   ``.`` is non-word and ``\\b`` between two non-word characters is not a
   boundary.

3. **Schema-declaration omission.** Every parser's emitted-dict keyset MUST
   match the AGENTS.md canonical 11-field schema. Missing canonical fields
   are allowed ONLY if there's a ``logger.warning`` call with the literal
   string ``absent_by_construction`` (or ``absent-by-construction``) AND the
   missing field name appears in that warning's message. The v6 trial round
   showed Willcox v2 + Miami both silently omitted ``ecomment_url`` (and
   Miami also ``meeting_id``) without any startup declaration.

Exit codes:
    0 — all parsers audited clean. Safe to PR-merge.
    1 — at least one violation found. Structured report on stdout naming
        every silent-emission site with file:line refs.

Stdlib only (``ast`` + ``re`` + ``argparse`` + ``pathlib``). No LLM
dependency, deterministic, runs in seconds per parser. Composes with the
S-057 distributed-contribution model as a CI gate that bots can run
identically against every PR.

Usage::

    python3.11 parsers/scripts/audit_silent_emissions.py <parser.py> [<parser.py> ...]

Per `AGENTS.md § Invocation discipline` (added 2026-06-16), this script is
the doctrine-evolution complement to Case L, NOT a replacement. Case L's
per-field-helper discipline remains the rule Codex aims to honor at
generation time; this audit is the safety net catching what doctrine
internalization missed.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


# Canonical 11 fields per AGENTS.md § "Canonical parser contract"
# AND `02_Core_Project/council_navigator/CLAUDE.md` § Parser System.
CANONICAL_FIELDS: frozenset[str] = frozenset({
    "meeting_title",
    "meeting_date",
    "meeting_time",
    "meeting_location",
    "meeting_status",
    "agenda_url",
    "minutes_url",
    "video_url",
    "agenda_packet_url",
    "ecomment_url",
    "meeting_id",
})

# Helper-function name prefixes that indicate per-field extraction per Case L.
PER_FIELD_HELPER_PREFIXES: tuple[str, ...] = (
    "_extract_",
    "_classify_",
    "_meeting_",
    "_field_",
)

# Doctrine-locked safe regex patterns allowed to end in `\b`. These are the
# specific patterns the AGENTS.md doctrine explicitly endorses.
DOCTRINE_LOCKED_TRAILING_B_PATTERNS: frozenset[str] = frozenset({
    r"\bcancell?ed\b",
})

# Substrings the audit looks for to identify an `absent_by_construction`
# declaration. The doctrine canonicalizes the underscore form; the hyphen
# form is allowed as a graceful alternative.
ABSENT_BY_CONSTRUCTION_TOKENS: tuple[str, ...] = (
    "absent_by_construction",
    "absent-by-construction",
)


@dataclass
class Violation:
    """One silent-emission failure site."""

    kind: str  # "case_l_silent_default" | "regex_pitfall" | "schema_omission"
    file: str
    line: int
    detail: str


@dataclass
class ParserAuditResult:
    """All violations found in one parser file + meta."""

    path: Path
    parse_error: str | None = None
    violations: list[Violation] = field(default_factory=list)


# ----------------------------------------------------------------------------
# Sweep 1 — Case L: per-field extraction helpers
# ----------------------------------------------------------------------------

def _is_per_field_helper(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in PER_FIELD_HELPER_PREFIXES)


def _function_has_logger_warning(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if the function body contains any call shaped like ``logger.warning(...)``
    or ``self.logger.warning(...)`` or ``LOG.warning(...)`` etc."""
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if isinstance(callee, ast.Attribute) and callee.attr in {"warning", "warn"}:
            # Accept any *.warning(...) or *.warn(...) chain — covers logger.warning,
            # self.logger.warning, LOG.warning, log.warning.
            return True
    return False


def _returns_empty_string(node: ast.Return) -> bool:
    """True if this Return returns an empty-string literal."""
    if node.value is None:
        return False
    return isinstance(node.value, ast.Constant) and node.value.value == ""


def _audit_case_l(tree: ast.AST, source_lines: list[str]) -> list[Violation]:
    """Find per-field helpers with silent return-empty branches AND no logger.warning."""
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_per_field_helper(node.name):
            continue
        if _function_has_logger_warning(node):
            continue  # Helper has at least one warning somewhere; passes Case L V0.
        # No warning anywhere in the helper. Find each silent return-empty branch.
        silent_returns = [
            child for child in ast.walk(node) if isinstance(child, ast.Return) and _returns_empty_string(child)
        ]
        for ret in silent_returns:
            violations.append(
                Violation(
                    kind="case_l_silent_default",
                    file="",  # filled by caller
                    line=ret.lineno,
                    detail=(
                        f"helper `{node.name}` has `return \"\"` at line "
                        f"{ret.lineno} but no logger.warning anywhere in its body "
                        f"(per AGENTS.md Case L)"
                    ),
                )
            )
    return violations


# ----------------------------------------------------------------------------
# Sweep 2 — Regex pitfalls
# ----------------------------------------------------------------------------

def _extract_regex_compile_calls(tree: ast.AST) -> list[tuple[str, int]]:
    """Yield (pattern_string, line_no) for every ``re.compile(<str_literal>, ...)`` call."""
    results: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        is_re_compile = False
        if isinstance(callee, ast.Attribute) and callee.attr == "compile":
            base = callee.value
            if isinstance(base, ast.Name) and base.id == "re":
                is_re_compile = True
            elif isinstance(base, ast.Attribute) and base.attr == "re":
                is_re_compile = True
        if not is_re_compile or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            results.append((first.value, node.lineno))
    return results


# Match a pattern that ends with `\b` AND the character immediately before
# `\b` is a literal `.` (or its escaped form `\.`). These are the silent-fail
# patterns: `\b` after a non-word character is not a word boundary.
_REGEX_PITFALL_DOT_THEN_B = re.compile(r"\\\.\\b$|\\\.\?\\b$|\.\\b$")


def _audit_regex_pitfalls(tree: ast.AST) -> list[Violation]:
    violations: list[Violation] = []
    for pattern, line in _extract_regex_compile_calls(tree):
        if pattern in DOCTRINE_LOCKED_TRAILING_B_PATTERNS:
            continue
        if not pattern.endswith(r"\b"):
            continue
        # Pattern ends in `\b`. Inspect what precedes it.
        if _REGEX_PITFALL_DOT_THEN_B.search(pattern):
            violations.append(
                Violation(
                    kind="regex_pitfall",
                    file="",
                    line=line,
                    detail=(
                        f"regex pattern `{pattern}` ends in `\\b` after a "
                        f"literal `.` — silently fails to match dotted-suffix "
                        f"variants (the Willcox/Peoria/Sedona bug). Use "
                        f"`(?=\\s|$|[^\\w.])` lookahead instead."
                    ),
                )
            )
            continue
        # Non-dot non-word boundary uses are advisory (e.g., `\)` `\]` `\+`
        # before `\b` could be intentional). V0 of the audit only flags the
        # specific dot-then-B pitfall the trial track empirically caught.
    return violations


# ----------------------------------------------------------------------------
# Sweep 3 — Schema-declaration omission
# ----------------------------------------------------------------------------

def _collect_dict_keyset(tree: ast.AST) -> set[str]:
    """Walk every dict literal in the module; if it has >= 3 keys that intersect
    the canonical 11-field set, treat it as a meeting dict and return the union
    of all such keysets observed.

    Heuristic: a parser's `scrape_calendar` returns a list of meeting dicts.
    The dict literals constructing those rows are the ground-truth source for
    what fields the parser actually emits. Module-level constants like
    `FIELD_NAMES` are nice-to-have but not load-bearing — the AST tells us
    what data actually flows out.
    """
    observed: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys: set[str] = set()
        for k in node.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)
        # Only count dicts with at least 3 canonical-field keys as "meeting dicts."
        # Avoids false positives on small unrelated dicts (config, kwargs, etc.).
        if len(keys & CANONICAL_FIELDS) >= 3:
            observed.update(keys)
    return observed


def _collect_module_field_tuples(tree: ast.AST) -> set[str]:
    """Find module-level tuples/sets/lists assigned to names like
    ``FIELD_NAMES`` / ``CANONICAL_KEYS`` / ``SCHEMA_KEYS`` / ``OUTPUT_FIELDS``
    and collect their string element values.

    These are the explicit "schema declarations" some parsers ship. When
    present they're authoritative — the dict-literal heuristic confirms.
    """
    suggestive = {
        "FIELD_NAMES",
        "CANONICAL_KEYS",
        "SCHEMA_KEYS",
        "OUTPUT_FIELDS",
        "MEETING_FIELDS",
        "ROW_FIELDS",
    }
    declared: set[str] = set()
    for node in tree.body if hasattr(tree, "body") else []:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if not any(name in suggestive for name in targets):
            continue
        value = node.value
        if isinstance(value, (ast.Tuple, ast.List, ast.Set)):
            for elt in value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    declared.add(elt.value)
    return declared


def _collect_absent_by_construction_fields(tree: ast.AST) -> set[str]:
    """Find logger.warning(...) calls whose first string argument contains
    `absent_by_construction` (or hyphen variant) and extract canonical field
    names mentioned in those calls.

    Looks at:
      - The first arg if it's a string literal.
      - String literals nested inside f-strings (JoinedStr / FormattedValue).
      - Keyword arg values if string literals.

    For each such call, finds any canonical-field name as a substring.
    """
    declared_absent: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if not (isinstance(callee, ast.Attribute) and callee.attr in {"warning", "warn"}):
            continue
        # Gather all string literals from this call's args/kwargs.
        strings: list[str] = []
        for arg in node.args:
            strings.extend(_collect_string_literals(arg))
        for kw in node.keywords:
            strings.extend(_collect_string_literals(kw.value))
        combined = " ".join(strings)
        if not any(tok in combined for tok in ABSENT_BY_CONSTRUCTION_TOKENS):
            continue
        # This call declares absent-by-construction; find canonical fields named.
        for canon in CANONICAL_FIELDS:
            if canon in combined:
                declared_absent.add(canon)
    return declared_absent


def _collect_string_literals(node: ast.AST) -> list[str]:
    """Extract every string-constant literal from a node, walking into JoinedStr."""
    strings: list[str] = []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        strings.append(node.value)
    elif isinstance(node, ast.JoinedStr):
        for piece in node.values:
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                strings.append(piece.value)
    return strings


def _audit_schema_omission(tree: ast.AST) -> list[Violation]:
    """Compare emitted-dict keyset to CANONICAL_FIELDS; flag missing fields
    not declared absent-by-construction. Module-level FIELD_NAMES tuples are
    consulted as supplementary evidence."""
    emitted = _collect_dict_keyset(tree)
    declared_tuples = _collect_module_field_tuples(tree)
    if not emitted and not declared_tuples:
        # Parser has no meeting-shaped dict and no schema tuple. Either it's
        # a non-parser file the user pointed us at, or it returns dicts built
        # piece-by-piece in a way the heuristic can't see. Don't flag — the
        # operator gets a 0-violation result and can investigate manually.
        return []
    declared_present = emitted | declared_tuples
    declared_absent = _collect_absent_by_construction_fields(tree)
    missing = CANONICAL_FIELDS - declared_present - declared_absent
    violations: list[Violation] = []
    for canon in sorted(missing):
        violations.append(
            Violation(
                kind="schema_omission",
                file="",
                line=0,
                detail=(
                    f"canonical field `{canon}` is neither emitted in any "
                    f"meeting-shaped dict NOR declared in a module-level "
                    f"FIELD_NAMES-style tuple NOR named in any "
                    f"`absent_by_construction` logger.warning — supervisor "
                    f"cannot tell whether this is intentional omission or "
                    f"silent contract drift (per AGENTS.md Case L exemption "
                    f"clause + v6 schema-declaration migration finding)"
                ),
            )
        )
    return violations


# ----------------------------------------------------------------------------
# Orchestrator
# ----------------------------------------------------------------------------

def audit_parser(path: Path) -> ParserAuditResult:
    result = ParserAuditResult(path=path)
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        result.parse_error = f"cannot read file: {exc}"
        return result
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        result.parse_error = f"syntax error at line {exc.lineno}: {exc.msg}"
        return result
    source_lines = source.splitlines()
    sweeps = (
        _audit_case_l(tree, source_lines),
        _audit_regex_pitfalls(tree),
        _audit_schema_omission(tree),
    )
    for sweep_violations in sweeps:
        for v in sweep_violations:
            v.file = str(path)
            result.violations.append(v)
    return result


def format_report(results: list[ParserAuditResult]) -> str:
    """Build a human-readable report. Grouped per-file, then per-sweep within."""
    out: list[str] = []
    total_violations = 0
    for result in results:
        out.append(f"=== {result.path} ===")
        if result.parse_error is not None:
            out.append(f"  PARSE ERROR: {result.parse_error}")
            continue
        if not result.violations:
            out.append("  clean — no Case L / regex-pitfall / schema-omission violations")
            continue
        per_kind: dict[str, list[Violation]] = {}
        for v in result.violations:
            per_kind.setdefault(v.kind, []).append(v)
        for kind in ("case_l_silent_default", "regex_pitfall", "schema_omission"):
            items = per_kind.get(kind, [])
            if not items:
                continue
            out.append(f"  [{kind}] {len(items)} violation(s):")
            for v in items:
                line_ref = f"line {v.line}" if v.line else "module-level"
                out.append(f"    - {line_ref}: {v.detail}")
            total_violations += len(items)
    out.append("")
    out.append(f"=== TOTAL: {total_violations} violation(s) across {len(results)} file(s) ===")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Post-generation AST audit gate for Codex-generated parsers. "
            "Detects Case L silent-default branches, regex pitfalls (\\b after "
            "literal `.`), and schema-declaration omissions vs the AGENTS.md "
            "canonical 11-field schema."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="parser file paths to audit (one or many)",
    )
    args = parser.parse_args(argv)
    results = [audit_parser(Path(p)) for p in args.paths]
    print(format_report(results))
    any_violations = any(r.violations or r.parse_error for r in results)
    return 1 if any_violations else 0


if __name__ == "__main__":
    sys.exit(main())
