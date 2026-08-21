---
output_type: tracked_claims
target: NotebookLM chat query — extract forward-looking claims with verifiable outcomes
status: claude_authored · awaits_james_review
authored_by: Claude
last_edited: 2026-05-16
description: Extracts statements where an official commits to, predicts, or assures something about a future state of affairs. Output drives the Tracked Claims Ledger (T-012) — the long-term accountability layer surfaced on the Cast page Accountability section and the per-city Ledger page.

# Per CLAUDE.md "Don't write prompts. Those are James's."
# James authorized Claude to author this provisionally to unblock
# production (2026-05-16). James reviews + adjusts to his voice in
# the next pass — see prompts/PROMPT_REVIEW_LEDGER.md for the queue.
# Until that review, this prompt is the production extraction.
#
# This is the highest-stakes prompt in the system. A misidentified or
# misattributed tracked claim, surfaced as a status pill, is a
# defamation vector. Over-extraction is the worse failure mode —
# noise floods the ledger and trains the operator to stop reviewing.
# Bias the cutline toward exclusion. Under-extraction is recoverable
# (the next meeting catches new claims); over-extraction is not.

# This prompt depends on:
#   1. The city's notebooklm_persona_preamble (canonical names) being
#      prepended at runtime by the bridge (T-006 / T-007).
#   2. The five-topic vocabulary in parsers/topic_tags.py +
#      client/src/utils/topicTags.ts (for the topic_tags field).
#   3. The city_vocabulary_corrections SPELLING CORRECTIONS block
#      being prepended by the bridge (T-017 Layer 2).
---

# Tracked Claims Extraction

Pull statements from named officials that constitute forward-looking commitments, assurances, predictions, or promises that could later be checked for fulfillment or contradiction. Output is strict JSON the bridge persists to the `tracked_claims` table. Each claim is karaoke-aligned (T-009 Phase 0b) the same way `member_quotes_topic` quotes are.

## Instructions (sent to NotebookLM)

You are scanning a council-meeting transcript for **tracked claims** — statements that point forward in time and can later be checked for fulfillment, contradiction, or quiet abandonment. This is NOT a quote-extraction task (that's `member_quotes_topic`'s job). It's a structured extraction of statements that create future accountability for the speaker.

### The four claim categories

Use exactly ONE primary category per claim. If a statement spans categories, pick the dominant one.

- **`assurance`** — A negative future commitment: a statement that something WILL NOT happen, NOT change, or NOT be affected. Examples: *"We're not taking officers off the streets,"* *"Water rates won't increase this year,"* *"This won't impact the small-business permit process."*

- **`commitment`** — A positive future commitment with an implied or stated timeline: the speaker (or the body the speaker leads) WILL do something by some time. Examples: *"We'll bring this back for a vote in 30 days,"* *"Staff will deliver a budget proposal by Q3,"* *"We'll hire ten additional officers."*

- **`prediction`** — A causal claim about future state without an explicit commitment to act: X is GOING to happen as a result of Y. Examples: *"This rezoning will reduce traffic on Main Street,"* *"The budget will balance by year-end,"* *"This program will generate $500K in revenue."*

- **`promise`** — Explicit commitments to a constituent, community group, or named external party. Often in response to public comment or a question from outside the council itself. Examples: *"We'll fund the parks department this cycle,"* *"I personally will follow up on this code-enforcement issue,"* *"The council will address the homeless camping concerns at the next meeting."*

### What counts as a tracked claim

A tracked claim is **a specific, future-pointing statement made by an official in an official capacity that someone could later check for fulfillment or contradiction.**

**Include:**

- Statements with a time horizon (explicit: "by Q3" — or implicit: "the upcoming budget cycle" / "next meeting").
- Negative assurances on policy direction ("we won't raise rates").
- Specific operational commitments ("we'll bring this back," "we'll fund X").
- Predictions about measurable outcomes ("this will generate $500K," "traffic will decrease").
- Conditional promises ("if the funding comes through, we'll do X") — record the condition in the `context` field.
- Strong policy stances stated as commitments by a council member ("I'd vote against any tax increase that didn't include X") — record as `assurance` with `confidence: medium`.
- Statements that the speaker LATER walked back within the same meeting — extract the original statement; the operator will set its status to `withdrawn` during ledger review. The point of the ledger is preserving the record, not erasing reversals.

**Exclude:**

- Pure procedural language ("I move to approve," "We'll vote on this next," "Can you call the roll").
- Hopes, wishes, and rhetorical fluff ("I hope someday we'll have a better park," "wouldn't it be nice if...," "we really should think about...").
- Hypothetical or rhetorical questions ("What if we doubled the budget? Then we could…" — the speaker isn't committing to anything).
- Descriptions of what already happened or what is currently happening (those are quotes, not tracked claims).
- Statements by non-officials: public commenters, guest speakers, paid presenters, applicants standing for items. The roster's canonical name list defines who qualifies.
- Statements where confidence the speaker meant it as a commitment is LOW.
- Vague feel-good statements without a specific action or outcome ("we'll continue our great work on this").

When in doubt, **DROP.** Under-extraction is recoverable — the next meeting catches new claims, and the operator can manually add a claim that was missed. Over-extraction is not — it floods the ledger with noise, trains the operator to stop reviewing, and destroys the layer's trust.

Imagine the operator (a journalist or civic-watcher) reading the ledger six months from now. Every entry should be something they would WANT to check the outcome of. Entries that wouldn't pass that test should be left out.

### Required output: strict JSON, no surrounding prose, no markdown fence

```json
{
  "tracked_claims": [
    {
      "speaker": "<exact name from the canonical list>",
      "claim_type": "<assurance | commitment | prediction | promise>",
      "claim_text": "<verbatim words spoken — the claim itself, NOT a paraphrase>",
      "expected_outcome": "<short description of what would verify or contradict this claim — max 120 chars — OR null>",
      "time_horizon_months": <integer months from the meeting date, OR null if no horizon is clearly implied>,
      "topic_tags": ["<one or more tags from the five-topic vocabulary, or 'other'>"],
      "confidence": "<low | medium | high>",
      "context": "<optional, max 200 chars — the agenda item being discussed, or any conditions on the claim>"
    },
    ...one row per claim...
  ],
  "extraction_notes": "<optional, max 200 chars — anything the operator should know about THIS run>"
}
```

Return an empty array (`"tracked_claims": []`) when no qualifying statements were made. Valid result. The ledger handles empty meetings cleanly.

### Field-level strictness

- **`speaker`** — MUST match the canonical name list exactly. Same rule as `member_quotes_topic`. If you can't resolve a speaker to the roster with confidence, DROP the claim rather than guess. Misattribution is the worst failure mode.

- **`claim_text`** — verbatim words. Same disfluency policy as `member_quotes_topic`: leave the speaker's fillers in (the gpt-4o-mini cleaner strips them post-hoc — T-011). Verbatim accuracy matters MORE here than in normal quotes because this is the evidence of what was promised. If you'd be uncomfortable showing the claim_text side-by-side with the recording to the speaker themselves and saying "that's what you said," tighten it.

- **`expected_outcome`** — YOUR description of what would verify or contradict the claim, NOT the speaker's words. Write it crisply — this is what a journalist would check 6 months from now. Examples:
  - For *"We're not taking officers off the streets"* → `"Police-deployment data shows total officer-hours on patrol did not decrease YoY during the relevant window."`
  - For *"We'll bring this back for a vote in 30 days"* → `"The next regular council meeting agenda within 30 days includes the item for vote."`
  - For *"This rezoning will reduce traffic on Main Street"* → `"Traffic counts on Main Street show measurable decrease within 12 months of rezoning implementation."`

  If you cannot write a crisp verification predicate in ≤120 characters, return `null`. The operator will fill it in during ledger review. Better null than vague.

- **`time_horizon_months`** — when to check back. Be specific when the speaker said something ("by Q3" → 6 months from a Q1 meeting, etc.). Otherwise, use these defaults:
  - `commitment` without a stated timeline → 6 months
  - `assurance` (a "won't happen" statement) → 12 months
  - `prediction` with a measurable outcome but no stated timeline → 12 months
  - `promise` to a constituent → 3 months (these usually surface quickly or fade quickly)
  - `prediction` with no implicit horizon ("this will eventually…") → null
  - Conditional ("if X then Y") → null until the condition resolves, OR the operator's call

- **`topic_tags`** — same five-topic vocabulary as `member_quotes_topic`, plus `other` when none fit. Multi-tag is fine.

- **`confidence`**:
  - `high` — speaker was declarative, specific, and clearly meant this as a commitment (often agenda-tied or in direct response to a question). The kind of thing they'd own if asked about it months later.
  - `medium` — strong statement, but could be read as rhetorical. Includes strong policy stances ("I'd vote against any tax increase").
  - `low` — borderline cases. PREFER TO DROP. If you're emitting `low`, ask whether the operator would actually want to see it in the ledger.

- **`context`** — optional, ≤200 chars. The agenda item being discussed, conditions on the claim ("if state funding comes through"), or who the claim was made to ("in response to public comment from a Beale Street merchant"). Helps the operator understand the claim's frame months later.

### Avoid these wording patterns in `expected_outcome`

The `expected_outcome` will surface verbatim on the public ledger. Write it in **neutral, observable, third-person prose** — not a judgment.

- ❌ `"Council member broke their promise"`  →  ✓ `"The hiring did not occur within the stated 6-month window."`
- ❌ `"This will be a broken assurance if..."`  →  ✓ `"Police deployment data shows officer-hours on patrol decreased."`
- ❌ `"They lied about..."`  →  anything; do not write this.

The `status` field is set by the operator in the UI based on `expected_outcome` evaluation. The extraction prompt's job is to describe what would constitute verification, not to render a verdict.

### Failure modes the bridge handles for you

- Malformed JSON → bridge logs error, operator sees a fail pill in the work order.
- Non-canonical speaker name → bridge drops the row, logs a warning. Don't try to repair names.
- Empty `tracked_claims` array → valid. Means no official said anything forward-pointing enough to track this meeting.
- The bridge applies `city_vocabulary_corrections` to your output mechanically — you don't need to "fix" proper-noun spellings. Stay verbatim to the audio.

### Mental check before emitting each row

For each candidate claim, before adding it to the output, ask:

1. Did a named official, in their official capacity, say this verbatim or close to it?
2. Is it specifically about something in the future, not the past or present?
3. Could a reasonable person check whether it came true 6–24 months from now?
4. If a journalist printed this entry as-is on a "Things Mayor X promised" page, would the speaker recognize it as fair?

If any answer is "no" or "not sure," DROP the row. Recall is the wrong objective here. Precision is.

<!-- ZSPAN_MODEL_CONTENT_END -->
