---
output_type: votes
target: NotebookLM chat query — extract recorded votes (the body's response to a motion)
status: claude_authored · awaits_james_review
authored_by: Claude
last_edited: 2026-06-05
description: Extracts `Vote` nodes for the Conversational Compiler's typed IR (S-023 / CONVERSATIONAL_COMPILER_SPEC § Node types). One row per recorded vote outcome; the CFG view renders each Vote node connected to its originating Motion node via a `responds_to` edge (edge inference is a downstream pass).

# Per CLAUDE.md "Don't write prompts. Those are James's."
# James authorized Claude to author this provisionally (2026-06-05)
# as the second prompt in the Track B sequence (per
# CONVERSATIONAL_COMPILER_SPEC § Decision #8a). James reviews +
# adjusts to his voice; queued in prompts/PROMPT_REVIEW_LEDGER.md
# alongside motions.md.
#
# This prompt depends on:
#   1. The city's notebooklm_persona_preamble (canonical names) being
#      prepended at runtime by the bridge (T-006 / T-007).
#   2. The city_vocabulary_corrections SPELLING CORRECTIONS block
#      being prepended by the bridge (T-017 Layer 2).
#   3. Eventual link inference to Motion nodes via `motion_reference`
#      hint field (resolved post-extraction by a constraint-checker
#      pass; not the prompt's job to assert the link).
#
# Output is persisted as transcript_nodes rows with node_type='Vote'
# per CONVERSATIONAL_COMPILER_SPEC § IR schema V0.
---

# Vote Extraction

Pull every **recorded vote** taken on the floor of the meeting. A vote is the body's response to a motion: the chair calls for the vote, members vote (voice / roll call / unanimous consent), and the chair announces the outcome. Output is strict JSON the bridge persists to the `transcript_nodes` table with `node_type='Vote'`.

This is NOT a motion-extraction task (`motions.md` handles that). Votes are the parliamentary EVENT that resolves a pending motion. Most council meetings produce 5-20 votes — many on consent-agenda bundles, some on substantive items.

## Instructions (sent to NotebookLM)

You are scanning a council-meeting transcript for **recorded votes** — moments when the body collectively decides whether to pass, fail, table, or withdraw a motion. Each vote you extract becomes a `Vote` node in the meeting's Control Flow Graph, linked to its originating Motion via a `responds_to` edge (the bridge resolves the link post-extraction using the `motion_reference` hint you provide).

### What counts as a vote

A vote is **any formal body-level decision** recognizable by patterns including but not limited to:

- *"All in favor say aye... opposed... motion carries."*
- *"All those in favor please indicate by saying aye. Any opposed? Motion passes."*
- *"Madam Clerk, please call the roll."* (followed by roll-call results)
- *"Without objection, so ordered."* (unanimous consent — count as a vote)
- *"Motion fails. There were three ayes and four nays."*
- *"The motion is tabled."* / *"The motion is withdrawn."* (procedural outcomes — also count as Vote nodes)

**Include:**

- All recorded vote outcomes: passed, failed, tabled, withdrawn, tied.
- Voice votes ("all in favor say aye"), roll-call votes (per-member tallies), and unanimous consent ("without objection, so ordered").
- Votes on procedural motions (adjournment, agenda approval, recess) as well as substantive motions.
- Failed motions — these are real parliamentary outcomes even though they didn't pass.
- Withdrawals of motions before vote — extract as `vote_result='withdrawn'`, `vote_method='none'`.
- Consent-agenda block votes (multiple items voted on together) as ONE Vote node with multiple `motion_reference` items in the context field.

**Exclude:**

- Straw polls or informal show-of-hands ("how many of us would support…") — not formal votes.
- Public-comment expressions of opinion — not body votes.
- The chair's restatement of what's being voted on ("the motion is to approve item 5") — that's the motion, not the vote.
- Discussion-phase statements of how a member intends to vote ("I'll be voting no on this") — those are quotes / commentary, not the vote itself. Extract the vote only when it's CAST.

### Required output: strict JSON, no surrounding prose, no markdown fence

```json
{
  "votes": [
    {
      "motion_reference": "<short description of the motion being voted on, max 120 chars — the bridge uses this to link back to the Motion node>",
      "summary_sentence": "<one short plain-English sentence (max 100 chars) describing the vote outcome — for the IR node label>",
      "vote_result": "<passed | failed | tabled | withdrawn | tied>",
      "vote_method": "<voice | roll_call | unanimous_consent | none>",
      "per_member_votes": [
        {"member": "<canonical name>", "vote": "<aye | nay | abstain | absent | recused>"}
      ],
      "tally": {"aye": <int>, "nay": <int>, "abstain": <int>, "absent": <int>},
      "agenda_item": "<optional, max 120 chars — agenda item number + title>",
      "context": "<optional, max 200 chars — anything that helps the operator (e.g., 'amended before vote', 'second attempt after first roll call failed')>"
    },
    ...one row per vote...
  ],
  "extraction_notes": "<optional, max 200 chars — anything the operator should know about THIS run>"
}
```

Return an empty array (`"votes": []`) when no recorded votes were taken. Valid result (e.g., a public-hearing-only session with no action items).

### Field-level strictness

- **`motion_reference`** — short description of the motion this vote is resolving. Used by the constraint-checker pass to link Vote → Motion via a `responds_to` edge. Be specific enough that a human reading both the Motion and the Vote can recognize the match. Examples:
  - For a vote on a rezoning motion: `"Approve rezoning at 123 Main Street (R-1 → C-1)"`
  - For a vote on adjournment: `"Adjourn the meeting"`
  - For a consent-agenda block: `"Consent agenda items 3, 4, 5 (combined)"`

  If you cannot identify which motion the vote was responding to with confidence, write `"unclear"` — the operator will resolve in review.

- **`vote_result`** — exactly one of `passed | failed | tabled | withdrawn | tied`.
  - `passed` — motion carries; ayes prevail.
  - `failed` — motion fails; nays prevail OR insufficient quorum.
  - `tabled` — motion postponed (separate from `failed`; the motion may return).
  - `withdrawn` — motion withdrawn by the mover before a vote was taken.
  - `tied` — equal ayes and nays; the chair's tie-breaking vote (if any) becomes its own outcome — extract as a SEPARATE Vote node with the chair's vote casting the result.

- **`vote_method`** — exactly one of `voice | roll_call | unanimous_consent | none`.
  - `voice` — "all in favor say aye, opposed nay" — most common.
  - `roll_call` — clerk reads each member's name; each votes individually. `per_member_votes` array MUST be populated.
  - `unanimous_consent` — "without objection, so ordered" — the body proceeds without a vote because no one objected. `per_member_votes` may be empty.
  - `none` — for `vote_result='withdrawn'` situations where no vote was taken.

- **`per_member_votes`** — when the vote was a roll call OR individual votes were called out, list each member's vote. When the transcript only records the aggregate ("motion carries five to two"), leave this array EMPTY and populate `tally` instead. NEVER fabricate per-member votes; if you don't know who voted which way, leave it empty.

- **`tally`** — total counts. Set to zero for categories with no recorded count (don't omit the field; emit `{"aye": 5, "nay": 2, "abstain": 0, "absent": 0}`). When the transcript only records the outcome ("motion carries unanimously") and you can infer all-present-voted-aye, populate from the meeting's attending-member count if recorded; else leave aye/nay at 0 and signal via `vote_method='unanimous_consent'`.

- **`agenda_item`** — same format as `motions.md`: numbered item + short title, or `null` for standalone votes (adjournment, agenda approval).

- **`context`** — optional, ≤200 chars. Helps the operator understand the parliamentary flow: *"Second attempt after first vote tied 2-2"*, *"Council Member Smith recused due to financial interest"*, *"Vote followed lengthy discussion + amendment"*.

### Edge cases

- **Reconsidered votes** — when the body re-takes a vote on the same motion (e.g., second attempt after a tie), extract BOTH votes. Distinguish in `context`.
- **Amended motions voted on** — extract the vote on the amended form. Note "as amended" in `context`. The earlier Motion node carries the original wording; the Vote refers to the final form.
- **Roll-call votes where the clerk reads names but the transcript is unclear** — populate `per_member_votes` with whatever you can resolve confidently. NEVER guess. Better to leave per_member_votes empty + populate tally than to misattribute a vote.
- **Recusals** — a member who recuses isn't voting; record as `vote='recused'`. Recusal is parliamentarily distinct from `absent` (member is present but conflicted out).
- **Chair-only tie-breaker** — extract the tied initial vote, then extract the chair's tie-break as a SEPARATE Vote node referencing the same motion. The `responds_to` edges will both point back to the same Motion node.

### Failure modes the bridge handles for you

- Malformed JSON → bridge logs error, operator sees a fail pill in the work order.
- Non-canonical member names in `per_member_votes` → bridge drops those entries from the per-member array (but keeps the Vote node if `vote_result` is recorded). Don't try to repair names.
- Empty `votes` array → valid. Means no votes were taken this meeting.
- The bridge applies `city_vocabulary_corrections` to your output mechanically.

### Mental check before emitting each vote

For each candidate vote, before adding it to the output, ask:

1. Did the body formally take a vote (voice, roll-call, or unanimous-consent declaration), and is the outcome stated in the transcript?
2. Can I name the motion being voted on with reasonable specificity (for `motion_reference`)?
3. If I'm populating `per_member_votes`, did the transcript actually record each member's individual vote — or am I guessing?
4. Is `vote_result` one of the five exact values? Anything outside the enum signals I'm extrapolating beyond what the transcript supports.

If any answer is "no" or "not sure," DROP the row. Same precision-over-recall discipline as `motions.md` + `tracked_claims.md`. The Vote ledger's integrity depends on never fabricating a body action.

<!-- ZSPAN_MODEL_CONTENT_END -->
