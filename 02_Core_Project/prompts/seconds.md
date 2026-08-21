---
output_type: seconds
target: NotebookLM chat query — extract motion second events
status: claude_authored · awaits_james_review
authored_by: Claude
last_edited: 2026-06-04
description: Extracts `Second` nodes for the Conversational Compiler's typed IR (S-023 / CONVERSATIONAL_COMPILER_SPEC § Node types). Completes the Robert's Rules procedural triad — Motion → Second → Vote. One row per "Second" / "I second" event; the constraint-checker pass adds a `responds_to` edge from each Second to its originating Motion.

# Per CLAUDE.md "Don't write prompts. Those are James's."
# James authorized Claude to author this provisionally (2026-06-04)
# as the fourth prompt in the Track B sequence per the same
# "continue with the other stuff" authorization that covered
# agenda_transitions.md. Per CONVERSATIONAL_COMPILER_SPEC § Decision
# #8a — NotebookLM, NOT gpt-4o-mini. James reviews + adjusts to
# his voice; queued in prompts/PROMPT_REVIEW_LEDGER.md.
#
# This prompt depends on:
#   1. The city's [SYMBOLS] linker contract block being prepended
#      at runtime by the bridge (D-087 / notebooklm_bridge/symbols.py).
#   2. The city_vocabulary_corrections SPELLING CORRECTIONS block
#      being prepended by the bridge (T-017 Layer 2).
#
# Output is persisted as transcript_nodes rows with node_type='Second'
# via save_seconds_batch. Completes the Motion → Second → Vote
# procedural triad; the constraint-checker pass infers a `responds_to`
# edge from each Second to its originating Motion via the `motion_
# reference` hint and the agenda-item key match.
---

# Second Extraction

Pull every **second** made on the floor of the meeting. A second is a body member's brief affirmation that they support advancing a pending motion to discussion + vote. Output is strict JSON the bridge persists to the `transcript_nodes` table with `node_type='Second'`.

This is NOT a motion-extraction task (`motions.md` handles that). Seconds are the second leg of the Robert's Rules procedural triad — *a motion is on the floor; a second is required before the body discusses or votes on it*. Most motions get a second within seconds (literally) and the vote follows; the chair routinely says *"motion by Member X, seconded by Member Y. Discussion?"*.

## Instructions (sent to NotebookLM)

You are scanning a council-meeting transcript for **seconds** — moments when a body member affirms that a pending motion may proceed. Each second you extract becomes a `Second` node in the meeting's Control Flow Graph, linked to its originating Motion via a `responds_to` edge (the constraint-checker pass resolves the link via the `motion_reference` hint).

### What counts as a second

A second is **any body-member affirmation that a pending motion may proceed**, recognizable by patterns including but not limited to:

- *"Second."*
- *"I second."*
- *"I'll second that."*
- *"Second the motion."*
- *"I second the motion, Mr. Chair."*
- The chair calling for a second and a member responding with raised hand + voice confirmation (transcript will show the chair acknowledging the seconder).
- Chair-acknowledged seconds: *"Motion by Council Member Smith, seconded by Council Member Jones"* — extract from the chair's restatement when the seconder's own voice isn't captured separately.

**Include:**

- Every distinct second to a motion, in order of occurrence.
- Seconds to procedural motions (adjournment, recess, agenda approval) as well as substantive motions.
- Seconds offered for motions that ultimately failed or were withdrawn — the Second still happened.
- Seconds where the seconder is identified ONLY by the chair's restatement — extract the seconder's name from the chair's words.

**Exclude:**

- Discussion-phase comments that AGREE with a motion ("I support that") — agreement is NOT a procedural second.
- Public-comment expressions of support — only body members can second.
- The chair's pre-vote announcement when no second-event is in the transcript (*"Motion is on the floor"* without a named seconder) — if no second occurred or its source can't be identified, do NOT fabricate.
- Mid-discussion calls of *"second"* meaning "I want to be next to speak" (rare but possible in some bodies) — only seconds to a PENDING MOTION count.

### Required output: strict JSON, no surrounding prose, no markdown fence

```json
{
  "seconds": [
    {
      "speaker": "<exact name of the seconder from the canonical list>",
      "motion_reference": "<short description of the motion being seconded, max 120 chars — the bridge uses this to link back to the Motion node>",
      "summary_sentence": "<one short plain-English sentence (max 100 chars) describing what's being seconded — for the IR node label>",
      "second_text": "<verbatim words of the second as spoken — usually just 'Second' or 'I second' but capture exactly what was said, max 120 chars>",
      "agenda_item": "<optional, max 120 chars — the agenda item number + title the seconded motion relates to, or null if standalone>",
      "context": "<optional, max 200 chars — anything that helps the operator (e.g. 'pro-forma — chair announced the seconder; seconder did not speak', 'seconded only after the chair called twice for a second')>"
    },
    ...one row per second...
  ],
  "extraction_notes": "<optional, max 200 chars — anything the operator should know about THIS run>"
}
```

Return an empty array (`"seconds": []`) when no seconds were made. Valid result (e.g., a meeting with motions that died for lack of a second — extract those situations as null seconds in `motions.md`'s context, not here).

### Field-level strictness

- **`speaker`** — MUST match the canonical name list exactly. Same rule as `motions.md`. If the seconder's identity is unclear (e.g., the chair's restatement is ambiguous or the seconder's voice isn't captured), DROP the second rather than guess. Misattributing who seconded a motion is parliamentarily wrong (it affects the formal record of who took procedural action).

- **`motion_reference`** — short description of the motion being seconded. Used by the constraint-checker pass to link Second → Motion via a `responds_to` edge. Be specific enough that a human reading both the Motion and the Second can recognize the match. Examples:
  - For a second to a rezoning motion: `"Approve rezoning at 123 Main Street (R-1 → C-1)"`
  - For a second to adjournment: `"Adjourn the meeting"`
  - For a second to a consent-agenda motion: `"Approve the consent agenda (items 2A-2N)"`

  If you cannot identify which motion the second was responding to with confidence, write `"unclear"` — the operator will resolve in review.

- **`summary_sentence`** — YOUR plain-English one-liner for the IR node's label, NOT the seconder's words. Max 100 characters. Examples:
  - For a second to approve rezoning → `"Second the motion to approve rezoning at 123 Main"`
  - For a second to the consent agenda → `"Second the consent agenda motion"`
  - For a second to adjournment → `"Second the motion to adjourn"`

  Keep it scannable. The IR node's job is to make the Motion → Second → Vote triad legible at a glance in the CFG view.

- **`second_text`** — verbatim words of the actual second. Usually short (*"Second"*, *"I second"*) but capture exactly what was said. Max 120 chars. When the second is extracted from the chair's restatement (*"seconded by Council Member Jones"*) rather than the seconder's own voice, transcribe what the chair said and note this in `context`.

- **`agenda_item`** — optional. When the seconded motion is tied to a numbered agenda item, include the number + a short title (same format as `motions.md`). When the seconded motion is standalone (adjournment, recess), use null.

- **`context`** — optional, ≤200 chars. Anything that helps the operator understand procedural flow. Examples:
  - `"Pro-forma — chair announced the seconder; seconder did not speak"`
  - `"Seconded after a 3-second silence; chair re-called for a second"`
  - `"Seconded by the mover's frequent ally — pattern worth noting"`

  Don't repeat `motion_reference` content; this field is for procedural texture the bare data misses.

### Edge cases

- **Motions that died for lack of a second** — do NOT extract a phantom Second. The motion's `context` field (in `motions.md`) is where that's noted. The absence-of-second is a Motion-level attribute, not a Second-level event.
- **Multiple seconds offered simultaneously** — extract one Second per seconder. When the chair recognizes a specific seconder and the others stand down, extract only the recognized one. When the chair accepts the first audible second, extract that one.
- **Self-seconding** — when the chair offers and seconds their own motion (unusual but happens in small bodies), extract the second as a Second node with the chair as `speaker` and note in `context` that it's a self-second.
- **Chair-only seconds (rare)** — typically the chair doesn't second (they're presiding), but in some bodies they can. Extract per the body's actual practice from the transcript.
- **Seconds after amendment** — when a motion is amended and then re-seconded for the amended form, extract BOTH seconds. The first seconded the original motion; the second seconded the amended form. Note in `context` of the second-second that it's for the amended form.
- **Implicit seconds via unanimous consent** — when the chair says *"Without objection..."* and no second is offered because the body proceeds by unanimous consent, do NOT extract a Second. The procedural move is unanimous consent, not a motion-and-second sequence.

### Failure modes the bridge handles for you

- Malformed JSON → bridge logs error, operator sees a fail pill in the work order.
- Non-canonical speaker name → bridge drops the row, logs a warning. Don't try to repair names.
- Empty `seconds` array → valid. Means no seconds were extractable (which usually means motions died for lack of a second, OR the transcript didn't capture seconds clearly).
- The bridge applies `city_vocabulary_corrections` to your output mechanically.

### Mental check before emitting each second

For each candidate second, before adding it to the output, ask:

1. Did a named body member, in their official capacity, formally affirm a pending motion may proceed?
2. Can I name the motion being seconded with reasonable specificity (for `motion_reference`)?
3. Is the seconder a body member (not staff, not a public commenter, not the chair restating)?
4. Could I identify the seconder's exact canonical name from the transcript or the chair's recognition?

If any answer is "no" or "not sure," DROP the row. Same precision-over-recall discipline as `motions.md` + `votes.md`. The procedural record's integrity depends on never fabricating a second.

### Companion note — why this exists

Robert's Rules requires a motion → second → vote sequence for the body to act formally. Without the Second node, the Conversational Compiler's CFG view shows a procedural gap between Motion and Vote that's structurally inaccurate. The Second node also surfaces patterns the operator should see: which members second whose motions (alliance / faction signals), which motions died for lack of a second (procedural friction), and how often the chair has to re-call for a second (body engagement). These are real civic-procedure observables the SPEC's Surface B aggregate dashboard will eventually visualize across meetings.

<!-- ZSPAN_MODEL_CONTENT_END -->
