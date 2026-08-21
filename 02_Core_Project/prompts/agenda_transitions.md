---
output_type: agenda_transitions
target: NotebookLM chat query — extract agenda-item transition events
status: claude_authored · awaits_james_review
authored_by: Claude
last_edited: 2026-06-04
description: Extracts `AgendaTransition` nodes for the Conversational Compiler's typed IR (S-023 / CONVERSATIONAL_COMPILER_SPEC § Node types). One row per chair-initiated move to a new agenda item; enables Decision #2's layered abstraction by giving Motion/Vote/Commit_P nodes a `parent_node_id` anchor (the AgendaTransition they sit under).

# Per CLAUDE.md "Don't write prompts. Those are James's."
# James authorized Claude to author this provisionally (2026-06-04)
# as the third prompt in the Track B sequence — "continue with the
# other stuff" (2026-06-05) extends the motions.md + votes.md
# authorization to the remaining sibling extractions. Per
# CONVERSATIONAL_COMPILER_SPEC § Decision #8a — NotebookLM, NOT
# gpt-4o-mini. James reviews + adjusts to his voice; queued in
# prompts/PROMPT_REVIEW_LEDGER.md.
#
# This prompt depends on:
#   1. The city's [SYMBOLS] linker contract block being prepended
#      at runtime by the bridge (D-087 / notebooklm_bridge/symbols.py).
#   2. The city_vocabulary_corrections SPELLING CORRECTIONS block
#      being prepended by the bridge (T-017 Layer 2).
#
# Output is persisted as transcript_nodes rows with
# node_type='AgendaTransition' via save_agenda_transitions_batch.
# Enables SPEC build sequence item 3 (layered node abstraction) by
# providing the logical-block parents that Motion / Vote / Commit_P
# nodes hang under via parent_node_id.
---

# Agenda Transition Extraction

Pull every **agenda-item transition** the chair makes during the meeting. An agenda transition is the chair (mayor / vice mayor / presiding officer) moving the body from one agenda item to the next — *"Moving on to Item 4A, the FY26 CDBG project,"* or *"Next we have Item 7, the short-term rental ordinance."* Output is strict JSON the bridge persists to the `transcript_nodes` table with `node_type='AgendaTransition'`.

This is NOT an extraction of motions or votes (those have their own prompts). Agenda transitions are the **structural skeleton** of the meeting — the navigational frame inside which motions, seconds, votes, and commitments occur. The compiler uses them as logical-block parents: every Motion / Vote / Commit_P node hangs under the AgendaTransition that opened the item it belongs to (per CONVERSATIONAL_COMPILER_SPEC Decision #2).

## Instructions (sent to NotebookLM)

You are scanning a council-meeting transcript for **agenda-item transitions** — moments when the chair explicitly moves the body from one numbered agenda item to the next. Each transition you extract becomes an `AgendaTransition` node in the meeting's Control Flow Graph and serves as the parent for every Motion / Vote / Commit_P that occurs while that item is on the floor.

### What counts as an agenda transition

An agenda transition is **any explicit chair-led navigation to a new numbered agenda item**, recognizable by patterns including but not limited to:

- *"Moving on to Item 4A..."* / *"Next we have Item 7..."* / *"Our next item is Item 5B..."*
- *"Let's take up Item 3, the rezoning..."*
- *"Item 2 on the agenda is the consent agenda."*
- *"That brings us to Item 6, the budget amendment."*
- *"We'll now consider Item 9..."*
- The chair calling an item up by NAME when the number is implicit in the agenda packet *"Now we'll take up the Beale Street streetscape resolution"* — extract with whatever item number can be resolved from context, or null.

**Include:**

- Every transition into a new numbered agenda item, in order of occurrence.
- Returns to items previously tabled or postponed (when the chair re-opens an earlier item, that's a new transition — note in `context` that it's a reconsideration).
- Public-hearing openings + closings — these are agenda-item-shaped procedural transitions (*"This is the public hearing on Item 4..."*) and serve as logical block boundaries.
- Consent-agenda introduction as a single transition (the consent agenda is one logical block even if it covers multiple sub-items).

**Exclude:**

- Mid-discussion procedural asides ("Could we hold on a second?") — not a transition to a new item.
- Substantive discussion of the current item — the transition is the chair's MOVE, not the discussion that follows.
- Items that are read into the record but not deliberated (e.g., council reports / staff announcements that don't have their own deliberation slot) — extract these only if the chair explicitly names them as agenda items.
- Mid-item amendments or motion-to-amend transitions — those are sub-events under the current AgendaTransition, not new transitions.

### Required output: strict JSON, no surrounding prose, no markdown fence

```json
{
  "agenda_transitions": [
    {
      "agenda_item_number": "<the item identifier, e.g. '2E', '4A', '7', '10B'. Use null when the item has no numbered identifier (e.g. an unnumbered staff report deliberated as an item)>",
      "agenda_item_title": "<short plain-English title of the item, max 120 chars — what the agenda packet calls it>",
      "summary_sentence": "<one short plain-English sentence (max 100 chars) describing what the body is moving on to — for the IR node label>",
      "chair_speaker": "<exact name of the chair (mayor / vice mayor / presiding officer) from the canonical list, or null if unresolvable>",
      "transition_text": "<verbatim words of the chair's transition phrase (the actual 'Moving on to...' sentence) — max 240 chars>",
      "context": "<optional, max 200 chars — anything that helps the operator understand the flow (e.g. 'pulled from consent agenda for separate discussion', 'reconsideration of earlier-tabled item')>"
    },
    ...one row per transition...
  ],
  "extraction_notes": "<optional, max 200 chars — anything the operator should know about THIS run>"
}
```

Return an empty array (`"agenda_transitions": []`) when no agenda transitions could be identified. Valid result (e.g., a study session with no formal agenda structure).

### Field-level strictness

- **`agenda_item_number`** — the agenda identifier the council itself uses (`2E`, `4A`, `7`, `10B`, etc.). When the agenda uses sub-numbering (Item 2 → Items 2A through 2N as consent items), each pulled item gets its own AgendaTransition with the sub-identifier. When no number exists (e.g., the chair takes up a topic by name), use `null` — do NOT invent a number.

- **`agenda_item_title`** — the title from the agenda packet, lowercased style of the original. Max 120 chars. Examples: `"MOU with United States Capitol Police"`, `"FY 2026 CDBG regional account fund project"`, `"Ordinance No. 1993 Amending Zoning Code"`. Keep it as-spoken-or-as-packet-says; don't editorialize.

- **`summary_sentence`** — YOUR plain-English one-liner for the IR node's label, NOT the chair's words. Max 100 characters. Examples:
  - For *"Moving on to Item 4A, the CDBG regional account fund project"* → `"Take up the FY26 CDBG fund project"`
  - For *"Item 2 is our consent agenda — items A through N"* → `"Open the consent agenda"`
  - For *"That brings us to the public hearing on short-term rentals"* → `"Open the short-term rentals public hearing"`

  Keep it scannable. The operator should read 12 transitions in a single CFG view and understand the meeting structure at a glance.

- **`chair_speaker`** — MUST match the canonical name list exactly. Same rule as `motions.md`. The chair is typically the Mayor or Vice Mayor; if a council member presides because the chair is absent, name them. If the chair's identity can't be resolved with confidence, set to `null` — the operator will resolve in review.

- **`transition_text`** — verbatim words of the chair's actual transition phrase. Include the full sentence (*"Moving on to Item 4A, the FY26 CDBG project, staff please present"*). Max 240 chars. Verbatim accuracy matters because this anchors the boundary precisely.

- **`context`** — optional, ≤200 chars. Anything that helps the operator understand procedural flow that's not in `transition_text`. Examples: `"Reconsidered after being tabled at Item 5"`, `"Pulled from consent agenda for separate discussion"`, `"Public hearing opened immediately after the transition"`.

### Edge cases

- **Consent-agenda pull-outs** — when the body pulls a consent-agenda item for separate deliberation, that's TWO transitions: the original consent-agenda transition (which still covers the un-pulled items) and a new AgendaTransition for the pulled item. Note the pull in the pulled item's `context`.
- **Mid-item recesses** — a recess that interrupts an item doesn't reset the agenda position. Don't extract recesses as transitions. The next chair-led move IS the transition.
- **Tabled / postponed items** — extract the original transition (the body took up the item even if it didn't decide it). If the item returns later in the meeting, extract that as a SECOND transition with `context: "Reconsidered after being tabled earlier in the meeting"`.
- **Multiple items handled together** — when the chair announces a block (*"We'll take up Items 5 through 7 together"*), extract as one AgendaTransition with `agenda_item_number` capturing the range (`"5–7"`) and `agenda_item_title` listing them.
- **Subtle transitions** — sometimes the chair moves on without an explicit announcement, letting the agenda packet flow guide the next item. Extract these conservatively — only when the start of a new item is clearly recognizable from speaker turn changes + the topic shifting to that item's substance.

### Failure modes the bridge handles for you

- Malformed JSON → bridge logs error, operator sees a fail pill in the work order.
- Non-canonical chair name → bridge sets the chair_speaker to null (does NOT drop the transition; the AgendaTransition itself is structural and stands without a resolved chair).
- Empty `agenda_transitions` array → valid. Means no formal agenda structure was identifiable (study session, special workshop, etc.).
- The bridge applies `city_vocabulary_corrections` to your output mechanically.

### Mental check before emitting each transition

For each candidate transition, before adding it to the output, ask:

1. Did the chair (or a council member acting as chair) verbally move the body to a new agenda item?
2. Can I identify the item number (or confirm there is no number) and a plain-English title?
3. Is the transition the START of deliberation on the item, not a mid-deliberation aside?
4. Would removing this AgendaTransition break the structural understanding of when the body started discussing this item?

If any answer is "no" or "not sure," DROP the row. Same precision-over-recall discipline as `motions.md` + `votes.md`. The agenda skeleton's integrity depends on never fabricating a transition.

### Companion note — why this exists

`agenda_transitions` is the **structural backbone** the Conversational Compiler uses to give Motion / Vote / Commit_P nodes a meaningful parent. The constraint-checker pass that infers `responds_to` + `satisfies` edges (D-088) uses agenda-key prefix matching as the primary signal — every Motion/Vote/Commit_P inherits the `agenda_item` of its parent AgendaTransition automatically, which compounds the linker accuracy. If you can confidently extract clean AgendaTransitions, every downstream edge inference gets stronger.

<!-- ZSPAN_MODEL_CONTENT_END -->
