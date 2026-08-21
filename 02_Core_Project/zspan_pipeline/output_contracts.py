"""Executable output contracts for production, publication, and contribution.

Provenance: D-164 sections 6 and 7 (2026-07-13). Producer contracts govern
which outputs a producer must fulfill; ``PUBLICATION_CONTRACT`` is the one
universal publication floor. A meeting's ``source`` never changes what
"publishable" means.

Every flagship production member must remain coupled to a live ``qdrant_*``
strategy in ``fetcher.OUTPUT_TYPE_REGISTRY``. The database DDL's
``requested_outputs`` default string deliberately remains untouched here: it
is inert because the worker filters requested outputs before dispatch (the
larger S-143 registry cleanup remains deferred).
"""

from __future__ import annotations


# D-157 display-cut outputs remain produced: hide-not-delete keeps them
# available to the operator without exposing them on the public surface.
#
# Incident history carried over from worker.py's original literal (the set's
# prior home) — load-bearing precedents, do not prune:
#   - community_calls_to_action was missing from the worker set 2026-06-29 to
#     2026-07-06 while present in the fetcher registry + schema default + heal
#     list — the worker silently dropped it on every WO (the lone CCTA row in
#     the DB was generated ad-hoc). Sibling-branch mismatch class; fixed
#     2026-07-06 (DEEP_CLEAN Phase 2). This registry existing is that bug
#     class's structural fix.
#   - suggested_questions retired from worker generation per D-157
#     (2026-07-08): the public surface renders a standardized per-meeting-TYPE
#     question set (client-side const in lib/suggestedQuestions.ts), not
#     per-meeting-generated Q&A. Also ended the S-119 cached-answer exposure
#     (a cached chip named a resident). The fetcher OUTPUT_TYPE_REGISTRY entry
#     + the prompt file stay (hide-not-delete).
#     ⚠️ SUPERSEDED IN PART by D-186 (2026-07-31): three cited factual canned
#     answers per public meeting were restored for signed-out simulated
#     Librarian queries — SAME standardized question set (first 3 per bucket
#     from suggestedQuestions.ts, excluding slot 4's public-comment question),
#     pre-generated Sonnet answers via prompts/sim_query_answer.md (which
#     carries the load-bearing private-citizen guard), stored in a NEW
#     standalone episode_sim_queries table (NOT via THIS registry or the
#     dormant fetcher.OUTPUT_TYPE_REGISTRY['suggested_questions'] entry — do
#     NOT reactivate that). Generator: zspan_pipeline/scripts/generate_sim_queries.py.
#     Endpoint: GET /public-api/broadcasts/<public_id>/sim-queries.
#     Frontend: SignedOutSimQueryBody on desktop + mobile.
#   - quote_extraction retired from worker generation per D-157: the
#     standalone Key Quotes surface was cut; the kept decision-bound quotes
#     read the .preview sidecar, NOT this worker output. Removing it also
#     ended the session-32 F5 double-extraction (sidecar + worker both ran
#     the same Sonnet pass). The sidecar (run_pipeline) is untouched.
FLAGSHIP_PRODUCTION_CONTRACT: frozenset[str] = frozenset(
    {
        "episode_tagline",
        "synopsis",
        "newsletter",
        "key_decisions",
        "community_calls_to_action",
        "whats_next",
        "council_sentiment",
        "tracked_claims",
    }
)

# Universal publication floor. newsletter + whats_next deliberately stay:
# both are canon-KEPT per D-157 and worker-produced, so they cannot deadlock
# readiness. When the contribution era arrives, their floor membership is
# revisited alongside their render (a one-line change here per D-164/S-145).
# transcript_words is a custody-side prerequisite supplied by the flagship;
# it is never a synthesis output.
PUBLICATION_CONTRACT: tuple[str, ...] = (
    "synopsis",
    "newsletter",
    "key_decisions",
    "whats_next",
    "episode_tagline",
    "transcript_words",
)

# Deferred contribution-era producer contract (S-145). Nothing consumes this
# yet; defining it in the registry establishes the single home, not activation.
CONTRIBUTION_CONTRACT: tuple[str, ...] = (
    "synopsis",
    "key_decisions",
    "community_calls_to_action",
    "episode_tagline",
)

# Empty community calls to action can be the honest result for a meeting.
# Honest-empty outputs must never be floor-required, or readiness deadlocks.
HONEST_EMPTY_OUTPUTS: frozenset[str] = frozenset(
    {"community_calls_to_action"}
)
