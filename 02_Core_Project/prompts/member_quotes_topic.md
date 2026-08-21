---
output_type: member_quotes_topic
target: NotebookLM chat query — extract topic-tagged quotes attributed to council members
status: claude_authored · awaits_james_review
authored_by: Claude
last_edited: 2026-05-17
description: Extracts verbatim quotes from council members tagged against the five V1 topic categories (data_centers, water_rights, diversity_inclusion, lgbtq, education). Output drives the Cast page per-member topic-grouped quote view (T-007 / T-008).

# Per CLAUDE.md "Don't write prompts. Those are James's." James authorized
# Claude to provisionally refine this prompt 2026-05-17 to fix the merged-
# speaker failure mode (T-013 V4) per D-046. The other six "What James
# should refine" items in the footer remain open for James's full review
# pass — see prompts/PROMPT_REVIEW_LEDGER.md.
#
# This prompt feeds the Cast page — the surface most likely to be
# scrutinized for fairness by anyone who reads the broadcasts. Get the
# extraction right HERE and downstream UI inherits the rigor.

# This prompt depends on:
#   1. The city's notebooklm_persona_preamble (canonical names) being
#      prepended at runtime by the bridge.
#   2. The five-topic vocabulary defined in parsers/topic_tags.py +
#      client/src/utils/topicTags.ts. Keep this prompt's topic list in
#      sync with those — there's no automated check yet.
#   3. The city_vocabulary_corrections SPELLING CORRECTIONS block being
#      prepended by the bridge (T-017 Layer 2).
---

# Topic-Tagged Quote Extraction

Pull verbatim quotes from named council members that fall under one or more of the five featured topic categories. Output is strict JSON the bridge persists to the `member_quotes` table. Each quote is later re-cleaned by the OpenAI quote-cleaner (T-011) before display.

## Instructions (sent to NotebookLM)

You are extracting verbatim quotes from a council-meeting transcript and tagging each one against a strictly-defined topic vocabulary. This is NOT a summary task — it's a quotation task. Verbatim accuracy is essential.

### The five featured topic categories (V1)

Use ONLY these five topic tags. If a quote doesn't fit any of these, tag it `other` (it will not appear on the public Cast page but is preserved in the DB for future surfaces).

- `data_centers` — quotes about data-center proposals, hyperscaler expansion, the water/power demands of such facilities, related zoning or incentive votes, public concerns about data centers, council policy on data-center growth.
- `water_rights` — quotes about Colorado River allocations, well permits, groundwater conservation, drought response, water-supply infrastructure, water rate policy, drought-impact statements.
- `diversity_inclusion` — quotes about DEI policy, civic-access programs, language services, accessibility accommodations, equity in city services, demographic representation on boards/commissions.
- `lgbtq` — quotes about LGBTQ-related ordinances, official recognition or proclamations, public-comment exchanges on LGBTQ topics, council positions on LGBTQ policy.
- `education` — quotes about school-board liaison items, library funding or policy, civic-education partnerships, after-school programs, K-12 / community-college engagement with the city.
- `other` — anything else (preserved in the DB but not displayed on the Cast page).

### What counts as a quote

A quote is **a continuous block of words a SINGLE speaker said as one uninterrupted turn at the microphone.** Not a paraphrase, not a summary, not a multi-speaker exchange stitched together. If the transcript ambiguates the exact wording, prefer the longest unambiguous fragment.

**Include:**
- Statements during agenda discussion, regardless of length.
- Questions a member asked of staff or the public (those reveal stance too — e.g., "What's the water-table impact of this data center?" tagged `data_centers` + `water_rights`).
- Justifications a member gave for their vote on a roll call.

**Exclude:**
- Pure procedural utterances ("I move to approve", "Second", "Roll call please").
- Personal-life comments, anniversaries, off-topic asides.
- Anything said by a guest speaker, City Manager, or non-council member. ONLY council-member quotes (use the canonical name list above).
- Anything the member explicitly retracts in the same meeting.

### ⚠️ ONE speaker per quote — the load-bearing rule

**Never concatenate text spoken by different people into a single quote.** This is the single most dangerous failure mode in this extraction. If person A says something, then person B responds, then person A says something else, that is at MINIMUM two separate quote rows (A's two turns), NOT one merged block attributed to A.

A `quote_text` is **one continuous monologue from one speaker, ending the moment another speaker begins talking.** When a different voice enters the transcript — even for a single word, even for an "okay" or "thank you" — your quote MUST end at that boundary. Start a new row if the same speaker resumes after a brief response from another.

**WRONG — three speakers merged into one attribution:**
```json
{
  "speaker": "Smiley Ward",
  "quote_text": "that would be my thoughts too okay for now anyway just go with the medium and the four focused areas and Okay anybody else focus on generating sorry council Mike I'm sorry I agree but I would add I really want to push the generating sales tax new revenues..."
}
```
The "Okay anybody else" is the Mayor calling for the next speaker. The text after that is a different councilmember entirely. This block is THREE quotes, not one, and only the first sentence is actually Smiley Ward's.

**WRONG — back-and-forth Q&A flattened into one quote:**
```json
{
  "speaker": "Jamie Scott Stehly",
  "quote_text": "so if you're sitting in the intersection you'll get a green arrow so protected permissive okay there will be directional arrows up there at least for that movement okay great thank you"
}
```
The "so if you're sitting…" is staff explaining; the "okay there will be directional arrows" is the council member; the "okay great thank you" is the council member closing. This is at least two speakers and should be either ONE quote (just the council-member parts, if they're a continuous turn) or DROPPED if you can't cleanly attribute it.

**Self-check before emitting each quote:**
1. Read the `quote_text` aloud in your head.
2. Does the perspective shift mid-quote? Does someone get addressed (e.g., "thank you," "okay great") in a way that suggests the next sentence is from a different voice?
3. Does the tone or vocabulary change abruptly halfway through?
4. Does the transcript show another named speaker between the start and end of what you're emitting?

If ANY answer is yes, the quote is merged. Either split into per-speaker rows OR drop the row entirely if you can't cleanly separate them. **Under-extracting a real quote is recoverable on the next meeting. Misattributing a merged block to one person is a defamation vector.**

### Required output: strict JSON, no surrounding prose, no markdown fence

```json
{
  "quotes": [
    {
      "speaker": "<exact name from the canonical list above>",
      "quote_text": "<verbatim quote — see 'What counts as a quote'>",
      "topic_tags": ["<one or more tags from the five-topic vocabulary, or 'other'>"],
      "minutes_page_ref": "<page number in the meeting minutes if identifiable; otherwise null>",
      "approximate_timestamp_seconds": <integer seconds from meeting start, or null>,
      "context": "<optional, max 200 chars — the agenda item or topic being discussed when this was said>"
    },
    ...one row per quote...
  ],
  "extraction_notes": "<optional, max 200 chars>"
}
```

### Strict rules

- **Speaker name MUST match the canonical name list exactly.** Don't paraphrase, don't add titles, don't shorten ("J. Stehly" is wrong — use "Jamie Scott Stehly"). If you encounter a name that's not on the list, drop the quote rather than guess.
- **ONE speaker per row, NO exceptions.** A `quote_text` ends the moment any other speaker begins talking. Multi-speaker exchanges are split into per-speaker rows OR dropped. See "ONE speaker per quote" above for examples of the merged-speaker failure mode this rule prevents.
- **One quote per row.** If a member made several distinct statements on different topics, return them as separate rows.
- **Multi-tag is fine** when a quote crosses topics — e.g., "We can't approve another data center without checking what it does to our wells" is `["data_centers", "water_rights"]`. Order doesn't matter.
- **Don't editorialize in the `context` field.** "Discussion of Resolution 26-04, water allocation amendment" — fine. "Tense discussion of controversial water vote" — NOT fine.
- **If you can't tag a quote into one of the five featured topics, use `["other"]`.** Don't force-fit. The Cast page filters to the five; `other` is preserved for future broadening (V2).
- **Quotes go through a SECOND verbatim-cleaning pass** after this extraction (the OpenAI quote-cleaner, T-011, strips fillers like "uh", "um", false starts). Do NOT pre-clean the quotes here — give them to me verbatim including disfluencies. The cleaner is more conservative than NotebookLM and is the audit-trail layer.
- **Return ONLY the JSON object.** No preamble, no closing thoughts.

<!-- ZSPAN_MODEL_CONTENT_END -->

## Failure modes the bridge handles for you

- Malformed JSON → bridge logs error, row goes to `notebook_outputs.error`, operator sees a fail pill.
- Non-canonical speaker name → bridge drops the row (defensive default) and logs a warning. Operator can investigate via the review surface.
- Empty `quotes` array → valid output. Means no member said anything tag-worthy in this meeting. The Cast page handles empty gracefully.

## Already addressed (2026-05-17, Claude provisional pass per D-046)

- **Merged-speaker failure mode** — explicit one-speaker-per-quote rule + concrete WRONG examples (drawn from m101091's rejected quotes 24 and 28). The extraction prompt now forbids cross-speaker concatenation; the strict-rules section reinforces it; a self-check helps NotebookLM catch borderline cases. James to validate by re-running m101091 (or any meeting with back-and-forth Q&A) and confirming the prompt no longer produces merged blocks.

## What James should refine before this ships

1. **Topic definitions** — Claude wrote one-paragraph definitions per topic. James knows Kingman-specific context that should sharpen these. For example: is a quote about "data-center water usage" tagged `data_centers` OR `water_rights` OR both? Claude said both; James may have a stronger preference. The way you tag determines what the Cast page reveals.

2. **"What counts as a quote" rules** — Claude included questions-as-quotes (a member asking staff "what's the water impact" gets tagged). That's a defensible call but James may prefer to limit quotes to declarative statements only.

3. **The retraction-handling rule** — Claude said exclude retracted quotes. But if a member says something heated and then retracts it ("I withdraw that comment"), some operators might want BOTH in the record. James should decide.

4. **The `other` tag fate** — currently `other` is stored but not surfaced. James said V1 only surfaces the five featured topics. If V2 broadens the vocabulary, the `other`-tagged quotes will already be in the DB ready to migrate.

5. **The "questions reveal stance" judgment call** — Claude included questions because they DO reveal a member's interests (a member who only asks data-center questions IS revealing focus). But questions can also be neutral fact-finding. James should sanity-check this with a few real Kingman meetings.

6. **The deflection / "no comment" rule (missing)** — what if a member is asked a direct question on a featured topic and says only "I'd rather not comment"? That's not a quote about the topic but IS evidence of stance. Currently Claude's draft would tag it `other` (non-substantive). James may want a more sophisticated rule here, especially around D-031 era quote-handling.
