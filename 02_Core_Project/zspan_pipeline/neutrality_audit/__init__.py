"""Deterministic neutrality audit — the D-144 measurement layer, v0.1.

First real build out of the S-133 investigation (2026-07-09 probes at
03_Research/neutrality_audit_probes_2026-07-09/). Two-stage architecture
per S-133 / CONVERSATIONAL_COMPILER_SPEC Application 3:

  Stage 1 (cheap constrained LLM): vote/adoption frame extraction from the
    meeting transcript via prompts/votes.md, run by TWO independent model
    families (claude -p Sonnet + gpt-4o-mini @ temp 0).
  Stage 2 (deterministic, zero tokens): signature anchor scan, entity/anchor
    grounding, cross-family consensus-convergence, and the key_decisions
    output audit. Everything in deterministic.py runs offline forever.

The audit measures; it never gates publication by itself (D-006 unchanged)
and it never edits outputs. Reports land operator-side (untracked), the
tooling ships in the public tree (D-154 tooling/data split).
"""

from . import deterministic, extraction, runner  # noqa: F401
