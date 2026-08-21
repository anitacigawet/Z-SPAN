---
output_type: motions
target: NotebookLM chat query — extract parliamentary motions made on the floor
status: claude_authored · awaits_james_review
authored_by: Claude
last_edited: 2026-06-05
description: Extracts `Motion` nodes for the Conversational Compiler's typed IR (S-023 / CONVERSATIONAL_COMPILER_SPEC § Node types). One row per "I move that..." event; the operator-facing CFG view renders each as a Motion node connected to the Second + Vote nodes that follow via `responds_to` edges.

# Per CLAUDE.md "Don't write prompts. Those are James's."
# James explicitly authorized Claude to author this provisionally
# (2026-06-05) as the first prompt in the Track B sequence (per
# CONVERSATIONAL_COMPILER_SPEC § Decision #8a — NotebookLM, NOT
# gpt-4o-mini). James reviews + adjusts to his voice; queued in
# prompts/PROMPT_REVIEW_LEDGER.md.
#
# This prompt depends on:
#   1. The city's notebooklm_persona_preamble (canonical names) being
#      prepended at runtime by the bridge (T-006 / T-007).
#   2. The city_vocabulary_corrections SPELLING CORRECTIONS block
#      being prepended by the bridge (T-017 Layer 2).
#
# Output is persisted as transcript_nodes rows with node_type='Motion'
# per CONVERSATIONAL_COMPILER_SPEC § IR schema V0 (table created in
# Chunk B-0, commit 843b116).
---

# Motion Extraction

Pull every **motion** made on the floor of the meeting. A motion is a formal statement of the form *"I move that..."* / *"I move to..."* / *"So moved"* — the parliamentary-procedure event where a body member proposes a specific action for the council to vote on. Output is strict JSON the bridge persists to the `transcript_nodes` table with `node_type='Motion'`.

This is NOT a tracked-claims task (that's `tracked_claims.md`'s job). It's a structural extraction of procedural events. Most council meetings produce 5-20 motions; many are mechanical (approve consent agenda, adjourn) and many are substantive (approve a rezoning, accept a contract, direct staff to do X).

## Instructions (sent to NotebookLM)

You are scanning a council-meeting transcript for **motions** — formal parliamentary statements where a council member moves that the body take a specific action. Each motion you extract becomes a `Motion` node in the meeting's Control Flow Graph.

### What counts as a motion

A motion is **any verbal statement by a body member that proposes a specific action for the body to act on**, recognizable by phrasing patterns including but not limited to:

- *"I move that..."* / *"I move to..."*
- *"So moved"* (typically when seconding the chair's proposed action)
- *"I'd like to make a motion to..."*
- *"My motion is..."*
- *"I make a motion that..."*

**Include:**

- All formal motions made by council members, regardless of whether they passed, failed, or were withdrawn. The compiler preserves the record; the operator's downstream view shows outcome via the linked `Vote` node.
- Motions to amend, table, postpone, or reconsider — these are real parliamentary events even though they don't directly enact policy.
- Motions made and immediately withdrawn ("Actually, I withdraw that motion") — extract the original, note the withdrawal in `context`.
- Substitute motions (a motion replacing a pending one).

**Exclude:**

- "I'd recommend we..." or "I'd like to suggest..." — these are statements of preference, not formal motions. Council members signal a real motion by saying "I move."
- Staff or city-attorney *recommendations* that the council later votes on — staff recommendations aren't motions until a council member formally moves to adopt them.
- Public-commenter statements asking the council to take action — only council members on the body can make motions.
- Chairs' *announcements* of what they're about to ask the body to do ("We have a motion on the floor to..." said by the chair restating someone else's motion) — that's a restatement, not a new motion. Extract the original.

### The procedural vs substantive distinction

This drives the `motion_type` field. Use exactly ONE per motion:

- **`procedural`** — Motions about HOW the body conducts its business, not WHAT it decides. Examples: *"I move to adjourn,"* *"I move to recess,"* *"I move to approve the agenda as presented,"* *"I move to approve the consent agenda,"* *"I move the call to the question,"* *"I move to table this item,"* *"I move to enter executive session."* Most consent-agenda items, adjournments, and meeting-management motions are procedural.

- **`substantive`** — Motions about the actual matter being decided. Examples: *"I move to approve the rezoning at 123 Main Street,"* *"I move to accept the proposed budget amendment,"* *"I move to direct staff to bring back a revised ordinance,"* *"I move to award the contract to ABC Construction."* These are the motions that actually change policy / spend money / direct action.

When in doubt: ask whether the body would have to *decide something substantive* if this motion passes. If yes → substantive. If it's about meeting flow → procedural.

### Required output: strict JSON, no surrounding prose, no markdown fence

```json
{
  "motions": [
    {
      "speaker": "<exact name from the canonical list>",
      "motion_text": "<verbatim words of the motion as spoken — the 'I move that...' sentence>",
      "motion_type": "<procedural | substantive>",
      "summary_sentence": "<one short plain-English sentence (max 100 chars) describing what's being moved — for the IR node's label>",
      "agenda_item": "<optional, max 120 chars — the agenda item number + title the motion relates to, or null if the motion is standalone (e.g. adjournment)>",
      "context": "<optional, max 200 chars — was the motion seconded? withdrawn? amended? Any procedural notes that help the operator understand the flow>"
    },
    ...one row per motion...
  ],
  "extraction_notes": "<optional, max 200 chars — anything the operator should know about THIS run>"
}
```

Return an empty array (`"motions": []`) when no motions were made. Valid result. Some study sessions / public hearings produce zero motions.

### Field-level strictness

- **`speaker`** — MUST match the canonical name list exactly. Same rule as `tracked_claims.md`. If you can't resolve the mover to the roster with confidence, DROP the motion. Misattributing who made a motion is a defamation-adjacent error — better to drop than guess.

- **`motion_text`** — verbatim words of the motion itself. Include the leading "I move that" / "I move to" / "So moved" phrase. Leave the speaker's fillers in (the gpt-4o-mini cleaner strips them post-hoc per T-011). Verbatim accuracy matters because this becomes evidence of what was formally proposed.

- **`motion_type`** — exactly one of `procedural` or `substantive`. When ambiguous (e.g., a motion to direct staff that has substantive policy implications), prefer `substantive` — the operator can downgrade in review.

- **`summary_sentence`** — YOUR plain-English one-liner for the IR node's label, NOT the speaker's words. Max 100 characters. Examples:
  - For *"I move to approve the consent agenda as presented"* → `"Approve the consent agenda"`
  - For *"I move that we direct staff to bring back a revised ordinance on short-term rentals at the next meeting"* → `"Direct staff to revise the short-term-rentals ordinance"`
  - For *"I move to adjourn"* → `"Adjourn the meeting"`
  - For *"I move to approve the rezoning at 123 Main Street from R-1 to C-1"* → `"Approve rezoning at 123 Main Street (R-1 → C-1)"`

  Keep it scannable. The operator should be able to read 20 motions in a single CFG view and understand each at a glance.

- **`agenda_item`** — optional. When the motion is tied to a numbered agenda item, include the number + a short title. When the motion is standalone (adjournment, recess, agenda approval), use null. Format examples: `"Item 7 - Short-Term Rental Ordinance"`, `"Item 4 - FY26 Budget Amendment"`.

- **`context`** — optional, ≤200 chars. Procedural notes that help the operator understand the flow: *"Withdrawn by mover after staff clarification"*, *"Amended by Council Member Smith before vote"*, *"Followed lengthy public-comment session"*. Don't repeat what's in `motion_text` or `summary_sentence`.

### Edge cases

- **Failed motions** — extract them. The Vote node downstream will record the failure; the Motion itself still happened.
- **Amended motions** — extract the original motion. The amendment is itself a new motion (motion to amend). Note the amendment in the original's `context` field.
- **Substitute motions** — extract both. Note in `context` that one supersedes the other.
- **Multiple motions in the same breath** ("I move to approve items 3, 4, and 5 together") — one motion. The unification IS the motion.
- **Restated motions** — when the chair restates a motion before the vote, that's NOT a new motion. Extract only the original.
- **Inaudible / unintelligible motions** — if the motion's wording can't be transcribed with confidence, DROP. Better to miss one than misrecord it.

### Failure modes the bridge handles for you

- Malformed JSON → bridge logs error, operator sees a fail pill in the work order.
- Non-canonical speaker name → bridge drops the row, logs a warning. Don't try to repair names.
- Empty `motions` array → valid. Means no motions were made this meeting.
- The bridge applies `city_vocabulary_corrections` to your output mechanically — you don't need to "fix" proper-noun spellings. Stay verbatim to the audio.

### Mental check before emitting each motion

For each candidate motion, before adding it to the output, ask:

1. Did a named council member, in their official capacity, formally say "I move..." or equivalent parliamentary trigger phrase?
2. Is the motion text specific enough that the body could actually act on it?
3. Is the speaker a body member (not staff, not a public commenter, not a chair restating)?
4. Could I summarize the motion in one sentence the operator would understand at a glance?

If any answer is "no" or "not sure," DROP the row. Same discipline as `tracked_claims.md`: precision beats recall. Council meetings produce a manageable number of motions; better to miss one than fabricate one.

<!-- ZSPAN_MODEL_CONTENT_END -->
