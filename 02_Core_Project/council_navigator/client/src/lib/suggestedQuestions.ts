/**
 * Standardized per-meeting-type suggested questions (D-157 neutrality cut).
 *
 * D-157 re-scoped `suggested_questions` from per-meeting-GENERATED → a fixed
 * set keyed on meeting TYPE, neutral-by-construction. These are display-only
 * seeds: the broadcast page renders them as a "questions worth asking" list;
 * the signed-in member's BYOK panel uses the same list as live-query seeds. Every
 * question asks about an OBJECTIVE, convergent fact (votes, money, tabled
 * items, public-comment topics, next steps) — the class the
 * decentralized-consensus audit (S-131) can verify.
 *
 * ⚠️ D-186 (2026-07-31) SUPERSEDES-IN-PART D-157's per-meeting-answer
 * retirement for the signed-out simulated Librarian surface only. Three
 * pre-computed cited factual answers per public meeting are now stored in the
 * new `episode_sim_queries` table and rendered to signed-out visitors by
 * `SignedOutSimQueryBody` (desktop + mobile). The safety floor is the
 * `prompts/sim_query_answer.md` prompt's private-citizen guard — S-119 is
 * closed by construction because the sim-query selection uses only positions
 * [0,1,2] of the per-bucket list (structurally excluding the position-4
 * public-comment question in every bucket). The Python mirror at
 * `02_Core_Project/zspan_pipeline/sim_query_vocab.py` MUST stay in sync with
 * this file — a golden parity test in `test_sim_queries.py` extracts both
 * sources and asserts identical bucket contents + ordering.
 *
 * Everything else D-157 established remains in force: standardized (not
 * per-meeting-generated) question wording, receipts-only editorial scope,
 * the "hide-not-delete" retirement of `quote_extraction` worker generation.
 *
 * STATUS: `claude_authored · awaits_james_review` (session-43, 2026-07-08).
 * The exact wording is drafted for operator review — see
 * `02_Core_Project/prompts/PROMPT_REVIEW_LEDGER.md § 2026-07-08`. Edit there
 * and here in lockstep when the review lands (and mirror the same change to
 * the Python vocab constant per the golden-parity test).
 */

export type MeetingQuestionBucket = "regular" | "work_study" | "special" | "fallback";

export const SUGGESTED_QUESTIONS_BY_TYPE: Record<MeetingQuestionBucket, string[]> = {
  regular: [
    "What did the council vote on, and how did each member vote?",
    "What money was approved, and for what?",
    "Were any items tabled, postponed, or sent back to staff?",
    "What did residents raise during the public-comment period?",
    "What is scheduled to come back to the council next?",
  ],
  work_study: [
    "What topics did the council discuss without taking a formal vote?",
    "What direction did the council give to staff?",
    "What questions or concerns did council members raise about these topics?",
    "What did residents raise during the public-comment period?",
    "Which items are expected to return for a formal decision later?",
  ],
  special: [
    "Why was this special meeting called, and what items were on the agenda?",
    "What did the council decide on those items?",
    "Were any items tabled or continued to a later date?",
    "What did residents raise during the public-comment period?",
    "What happens next on these items?",
  ],
  fallback: [
    "What were the main items this body considered?",
    "What did it decide, and how did the members vote?",
    "Were any items tabled, postponed, or sent back to staff?",
    "What did members of the public raise during the comment period?",
    "What is scheduled to come next?",
  ],
};

/**
 * Map a meeting title to its question bucket. Mirrors meetingTypeFromTitle's
 * loose title-prefix derivation, then keyword-routes to a bucket. Default =
 * regular (the voting council meeting).
 */
export function bucketForTitle(title: string | null | undefined): MeetingQuestionBucket {
  const t = (title || "").toLowerCase();
  if (/(work session|study session|workshop)/.test(t)) return "work_study";
  if (/\bspecial\b/.test(t)) return "special";
  if (/(board|commission|committee|planning|zoning|authority)/.test(t)) return "fallback";
  return "regular";
}

export function suggestedQuestionsForTitle(title: string | null | undefined): string[] {
  return SUGGESTED_QUESTIONS_BY_TYPE[bucketForTitle(title)];
}
