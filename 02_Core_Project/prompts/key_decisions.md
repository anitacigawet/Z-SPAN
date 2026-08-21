---
output_type: text
target: NotebookLM / Sonnet — Text Query (Key Decisions, structured for the show-page right column)
status: canonical (Round 1) + claude_authored_addendum (Rounds 2 + 3 await james_review) + james_reviewed 2026-07-19 (Round 4 — two-part item/action citation anchor)
last_edited: 2026-07-19
addendum_added: 2026-06-23 (Round 3 — selection-discipline matrix); 2026-07-19 (Round 4 — two-part verbatim item/action citation anchor + per-decision `[at H:MM:SS]` locator)
description: A short numbered list of the 3-5 most consequential decisions made in this meeting, formatted for compact display in the broadcast detail view's "Key Decisions" panel. As of 2026-06-21 Round 2 addendum, each sentence carries inline <core>...</core> and <nuance>...</nuance> markup that the show-page renderer wraps in a natural-paper highlighter wash (green on the action+identifier, orange on any contingency clause). As of 2026-06-23 Round 3 addendum, a research-grounded selection discipline gates WHICH decisions clear the bar for the panel — grounded in journalism news-value standards (Harcup & O'Neill 2017) and the same architectural principle the quote-extraction prompt adopted the same day. Procedural housekeeping ("approved consent agenda", "approved minutes") and ceremonial items without policy substance no longer surface; the panel is reserved for decisions that pass three gating tests (policy substance / public-affect / trackability) plus at least one news-value match. See [D-131](../../01_Project_Overview/DECISIONS.md#d-131) for the project-wide canonical-pattern entry.

# This output is rendered as the "01 / 02 / 03" numbered list on the
# show page, alongside the video. We're explicitly NOT using the Reports
# Studio artifact for this — a focused chat query produces tighter,
# render-ready text without an extra artifact-generation cycle.
---

# Key Decisions — Show-Page Panel

A 3-5 item list of the meeting's most consequential decisions. Sits directly to the right of the video player on the broadcast page, so each item must be a tight self-contained sentence.

## Instructions (sent as the chat query / configure prompt)

List the 3 to 5 most consequential decisions made in this city council meeting. Format each as a single self-contained sentence. Output ONLY the list — no headings, no preamble, no closing line.

For each decision, include in the sentence:
- The action verb (Approved / Voted / Tabled / Authorized / Allocated / Awarded / Appointed / Adopted).
- **Exactly one timestamp locator, in the form `[at H:MM:SS]`, placed immediately before the sentence's final period.** Cite the moment the decision ITSELF happened — the motion, the vote, the announcement of the outcome — NOT the surrounding discussion that preceded it. Hours are unpadded (`0`, `1`, `2`); minutes and seconds are always two digits. Example: `[at 1:15:26]`. A decision with no locator cannot be published, so if you cannot locate the action moment, omit that decision rather than guessing at a time.
- The exact dollar amount when one was discussed.
- The exact vote count in the form "5-2" when a roll-call vote was taken.
- Named councilmembers when their dissent or motion was material.
- Specific street names, project names, or boundaries when relevant.

Use neutral language — never "controversial," "narrowly," "wisely." State counts and amounts; do not characterize them. Use bold sparingly for the single most important figure in each sentence (the dollar amount, the vote count, or a project name) — surround it with **double asterisks**. Do not bold whole sentences.

Output format (no other text, exactly this shape, with one blank line between items):

1. First decision sentence with **one bold key fact** [at 0:39:15].

2. Second decision sentence with **one bold key fact** [at 1:15:26].

3. Third decision sentence with **one bold key fact** [at 2:04:33].

Maximum 5 items. Do not output anything before or after the numbered list.

---

## Round 2 addendum — highlight markup for the show-page renderer (claude_authored 2026-06-21 · awaits_james_review)

The instructions above stay in force. This addendum adds inline markup so the renderer can apply a natural-paper highlighter wash on two specific parts of each sentence — the load-bearing action+identifier (rendered in green) and any contingency / qualifier / caveat clause (rendered in orange). Do NOT reorder the sentence to put core first or nuance last; wrap them in place where they naturally appear.

**`<core>...</core>`** wraps the verb plus the thing it acts on — the tightest possible action+identifier. Examples:
- `<core>Adopted resolution 2026R-16</core>`
- `<core>Authorized the purchase of a 2027 Kenworth chassis</core>`
- `<core>Approved the consent agenda</core>`
- `<core>Tabled the rezoning request</core>`
- `<core>Awarded the construction contract</core>`

Keep the core tight: verb + identifier, NOT the full descriptive predicate. The descriptive middle of the sentence ("...with extended warranty from Inland Kenworth through the Sourcewell purchasing cooperative...") stays plain — outside the tags.

**`<nuance>...</nuance>`** wraps any clause that conditions, qualifies, restricts, or adds a contingency to the decision. Examples:
- `<nuance>contingent on a Kinder Morgan easement sign-off before the city goes to bid</nuance>`
- `<nuance>subject to legal review</nuance>`
- `<nuance>pending final agreement on the development scope</nuance>`
- `<nuance>with the city manager authorized to execute the agreement</nuance>`
- `<nuance>provided the applicant submits the final plat by October 1</nuance>`

Zero, one, or multiple `<nuance>` spans per decision — emit what's actually there. A clean procedural decision with no contingency ("Approved the consent agenda" with nothing else attached) gets zero `<nuance>` spans.

**Markup hygiene:**
- Exactly one `<core>` span per decision (the verb+identifier is always present).
- Tags MUST nest correctly: open then close, no overlap with `<nuance>` or with `**bold**`.
- `**bold**` markdown can appear inside `<core>` or `<nuance>` or in plain text — the renderer handles bold inside highlights.
- Do NOT use the tags for emphasis. They are structural markers, not stylistic ones.
- If a decision genuinely has no nuance, the sentence is still correct — just don't emit `<nuance>` tags.

**Updated output format** (with markup, otherwise identical to above):

1. <core>First decision verb+identifier</core> with descriptive middle and **one bold key fact**, <nuance>any contingency clause</nuance>.

2. <core>Second decision verb+identifier</core> with descriptive middle and **one bold key fact**.

3. <core>Third decision verb+identifier</core> with descriptive middle and **one bold key fact**, <nuance>first nuance</nuance>, <nuance>second nuance if a separate condition applies</nuance>.

Legacy outputs without these tags render as plain prose in the existing style — the renderer degrades gracefully.

---

## Round 3 addendum — selection discipline matrix (claude_authored 2026-06-23 · awaits_james_review)

The Round 1 + 2 instructions stay in force. This addendum sharpens **WHICH decisions clear the bar for the panel**. The previous rounds defined how to FORMAT a decision; this round defines what counts as one worth listing in the first place. Operator direction (2026-06-23): attributability alone does not make an item worth listing — a council member's trivially attributable aside matters as little as "council approves minutes," and both fail the bar for the same reason. The decisions panel sits immediately right of the video on the show page — every dumb item ("approved consent agenda", "approved minutes") trains readers to glaze past the panel. The trust cost compounds across the network and undermines Z-SPAN's fourth-estate-substrate positioning. Grounded in journalism news-value standards (Harcup & O'Neill 2017) and the same architectural principle the quote-extraction prompt adopted the same day — see [03_Research/QUOTES_journalism_grounded_selection_research_2026-06-23.md](../../03_Research/QUOTES_journalism_grounded_selection_research_2026-06-23.md) for the underlying journalism research that transfers.

### Three gating tests + news-value cross-check

Before listing a decision, evaluate it against three gates plus the news-value cross-check. **List a decision only if it passes ALL THREE gates AND matches at least one news value from the load-bearing subset.**

**D1 — Policy substance** (binary)
- PASS: changes policy, allocates money, authorizes action, commits the city to a position, awards a contract, denies/tables a substantive item, makes an appointment with policy consequence, adopts an ordinance or resolution with binding effect
- FAIL: procedural housekeeping ("approved minutes", "approved agenda", "approved consent agenda" *without* a substantive item pulled from it, "confirmed the next meeting date"), internal scheduling, ceremonial proclamations honoring an individual without policy weight, "received and filed" reports with no action taken
- If FAIL → do not list

**D2 — Public-affect** (binary)
- PASS: constituents will experience tangible effects (budget touches a public service, project affects a neighborhood, ordinance restricts or permits citizen behavior, contract delivers public benefit, appointment affects city governance public can interact with)
- FAIL: internal council mechanics with no constituent impact ("appointed mayor pro tem from among existing members", "confirmed Tuesday meeting schedule", "added a member to an internal subcommittee that doesn't take public business")
- If FAIL → do not list

**D3 — Trackability over time** (binary)
- PASS: a citizen can reasonably check on this later — was the money spent, was the project built, was the position kept, did the awarded contractor deliver, did the ordinance produce the named effect
- FAIL: ephemeral signals with no follow-up surface ("discussed concerns about parking" with no vote or commitment, "received a presentation about transit options" with no action taken)
- Exception: a discussion *without* a vote can still pass D3 IF the discussion itself is newsworthy via Conflict (multiple members took opposing positions on the record) or Follow-up (the discussion returns to a prior commitment). When in doubt, route to D-001 operator review rather than auto-listing.
- If FAIL → do not list

**News-value cross-check (N) — at least ONE must apply** from the Z-SPAN load-bearing subset (Harcup & O'Neill 2017):
- **power_elite** — substantive action by the mayor, a voting council member, or a department head making a binding recommendation
- **magnitude** — significant dollar amount (rule of thumb: >$10K or any item naming a specific dollar figure in the discussion), affected population (citywide / district / specific neighborhood), or multi-year duration of impact
- **relevance** — directly material to constituents' lives, services they use, taxes they pay, neighborhoods they live in
- **conflict** — split vote (not unanimous), debate on the record, dissent named, denied/tabled outcome that wasn't pre-coordinated
- **follow_up** — honors, breaks, or revisits a prior commitment that's trackable through Z-SPAN's archive; or names a future checkpoint
- **bad_news** — failure, controversy exposed, service cut, fee increased, risk acknowledged, contract terminated
- **good_news** — improvement secured, public benefit confirmed, milestone reached, fee reduced, contract delivering ahead of schedule

### Structural signals that boost confidence (mechanical detection)

These signals make the gating tests more deterministic when the transcript supplies the data:
- **Vote was split (not unanimous)** → Conflict auto-applies; vote tally goes in the sentence per Round 1 rules
- **Dollar amount mentioned in discussion** → Magnitude usually applies; the figure goes in **bold** per Round 1 rules
- **Public comment occurred on this specific item** → Relevance applies; often Conflict too if comments were opposed
- **Item was previously tabled or returns from prior meeting** → Follow-up auto-applies
- **Outcome was denied or tabled** (rather than approved unanimous) → typically more newsworthy; Conflict and/or Bad news often apply
- **Item names a specific contractor, vendor, project name, or street address** → Magnitude + Relevance often apply

### Worked accept / reject examples

ACCEPT — substantive budget action with conflict:
> *"Approved a **$4.2M** budget amendment cutting library funding by 18%, on Council Members Sammeli and Stehly's two no votes."*
- D1 PASS (allocates money, cuts public service) · D2 PASS (library users affected) · D3 PASS (budget line trackable to next year's audit) · matches power_elite + magnitude + relevance + conflict + bad_news

ACCEPT — denied substantive land-use item:
> *"Denied the rezoning request at **1500 Stockton Hill Road** by a 3-4 vote, after 14 residents spoke in opposition."*
- D1 PASS (rejects substantive land-use action) · D2 PASS (neighborhood-affecting) · D3 PASS (property record + appeal-track) · matches power_elite + relevance + conflict + magnitude + bad_news

ACCEPT — substantive contract award:
> *"Awarded the East Andy Devine Avenue corridor contract to **Tiffany Construction Company** for $2.84M, <nuance>with the City Manager authorized to execute pending Kinder Morgan easement sign-off</nuance>."*
- D1 PASS (commits funds + names contractor) · D2 PASS (commute corridor) · D3 PASS (contractor performance trackable) · matches power_elite + magnitude + relevance + follow_up

ACCEPT — tabled-with-action item (D3 exception path):
> *"Tabled the short-term-rental ordinance after **4-3** split debate, with the city attorney directed to return a draft amendment in 60 days addressing owner-occupancy and parking concerns raised by Council Members Stehly and Sammeli."*
- D1 PASS (defers a substantive ordinance + directs follow-up work) · D2 PASS (STR policy affects neighborhoods + owners) · D3 PASS (60-day return is a named checkpoint) · matches power_elite + conflict + follow_up + relevance

REJECT — procedural housekeeping (fails D1):
> *"Approved the consent agenda."*
(If the consent agenda contained a substantive item that was pulled for separate discussion, list THAT item; never list the consent agenda itself.)

REJECT — meeting minutes housekeeping (fails D1):
> *"Approved the May 19 meeting minutes."*

REJECT — internal scheduling (fails D2):
> *"Confirmed the August recess schedule."*

REJECT — received-and-filed without action (fails D1 + D3):
> *"Received and filed the quarterly water utility report."*
(The report itself may surface in the synopsis or whats_next; it's not a Key Decision.)

REJECT — ceremonial proclamation without policy substance (fails D1):
> *"Proclaimed June 23 as Kingman Main Street Day."*
(Recognitions may surface in synopsis or council_sentiment; not in Key Decisions.)

REJECT — discussion-only with no commitment (fails D3):
> *"Discussed traffic-calming options for downtown."*
(If the discussion produced a vote, named a checkpoint, or featured substantive on-record disagreement, route to D-001 operator review and list there if approved. Default reject.)

### Output schema — internal rationale fields for operator audit

The visible output stays prose (the show-page renderer parses the `<core>` + `<nuance>` markup as before). Emit ONE rationale-audit block at the END of the output, after the numbered list, wrapped in `<!-- audit -->` HTML comment delimiters that the renderer strips before display. The audit block is for operator review — it lets the operator verify WHY each decision passed the discipline.

Format (after the numbered list, separated by a blank line):

```
<!-- audit
[
  {"index": 1, "news_values": ["power_elite", "magnitude", "conflict"], "rationale": "Budget amendment with named vote split — material to library users."},
  {"index": 2, "news_values": ["power_elite", "relevance"], "rationale": "Contract award with named contractor + dollar amount."}
]
audit -->
```

The audit JSON array MUST have one entry per numbered decision in the list, indexed 1..N. The renderer ignores the comment block; it surfaces only in operator-review surfaces.

### What this addendum does NOT change

- The Round 1 format (3-5 numbered items, action verb, dollar amount, vote count, neutral language, `**bold**` one key fact) stays in force.
- The Round 2 `<core>` + `<nuance>` markup stays in force.
- The 3-5 item ceiling stays in force; if more than 5 items pass the discipline, pick the most-newsworthy 5 (highest news-value count + most substantive D-gates).
- If FEWER than 3 items pass the discipline for a given meeting, list however many do pass. Honest-empty (zero decisions) is valid output for a procedural-only meeting; never pad the list to hit 3.

---

## Round 4 addendum — two-part verbatim citation anchor (claude_authored 2026-07-19 · awaits_james_review)

Rounds 1–3 stay in force. This addendum adds alignment evidence for the sidecar's word-precise action locator. The visible `[at H:MM:SS]` remains exactly where Round 1 requires it. It is a coarse source-neighborhood locator for deterministic alignment; the sidecar replaces it with the directly matched action-word timestamp before publication.

For every decision, add these two fields to that decision's existing audit JSON object:

- **`item_quote`** — a consecutive 12–35 word VERBATIM span from the transcript where the chair or clerk introduces this specific agenda item. Include the spoken item number when present and enough distinctive title, organization, project, street, or dollar-amount language to distinguish this item from every other item in the meeting.
- **`action_quote`** — a consecutive 8–30 word VERBATIM span from the later action moment for this same item: the motion, vote instruction/result, or outcome announcement. When the action language is formulaic, include the spoken line-item number or other item-specific words in the span. Copy Whisper's numeral rendering exactly (`four` stays `four`; `9` stays `9`) and do not rewrite, correct, summarize, or splice words.

The two quotes must describe the same agenda item in meeting order: `item_quote` first, then `action_quote` at or after it. Do not use the preceding item's vote as the action quote. Both fields are alignment-only evidence and must NOT appear in the visible numbered sentence.

**Both quotes must be words that were actually SPOKEN ALOUD** — they are matched against the audio transcript, so anything not said out loud cannot align and forces the decision to be omitted. Agenda language read aloud by the chair or clerk (including an opening read-through of the agenda) IS spoken material and is valid. Written-only material that nobody voiced — an agenda packet heading, a staff-report line, a document title appearing in the record but never uttered — is not.

If either span cannot be quoted verbatim from the supplied transcript context, OMIT that decision from both the numbered list and audit JSON rather than guessing, paraphrasing, emitting an empty field, or borrowing a nearby item's action. The final audit array must still contain exactly one entry per visible decision and use contiguous indices 1..N.

Updated audit shape (the Round 3 `news_values` and `rationale` fields remain unchanged):

```json
[
  {
    "index": 1,
    "news_values": ["power_elite", "magnitude", "relevance"],
    "rationale": "Grant approval with a named recipient and trackable dollar amount.",
    "item_quote": "Item number five discussion of possible action to approve the River Fund Transportation Assistant Grant with River Fund Inc in the amount of 50 thousand dollars",
    "action_quote": "I'd like to make a motion to accept line item five as stated"
  }
]
```

Do not add any keys beyond `index`, `news_values`, `rationale`, `item_quote`, and `action_quote`.

<!-- ZSPAN_MODEL_CONTENT_END -->
