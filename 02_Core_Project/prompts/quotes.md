---
output_type: quotes
target: NotebookLM chat query — extract verbatim quotes from official-capacity speakers, classify by speaker_class, tag with topic vocab, flag broadcast-hero subset
status: claude_authored · awaits_james_review
authored_by: Claude
last_edited: 2026-05-26

description: |
  Unified extraction that supersedes council_quotes.md (officials-only,
  broadcast hero) + member_quotes_topic.md (council-members-only, Cast page).
  Produces a single canonical quote stream — every official-capacity speaker
  (council members + staff + outside experts) with speaker_class tagging,
  topic-vocab tagging, AND a curated broadcast_hero_ordinals subset for the
  BroadcastPage hero section.

  Persisted via parsers/database.save_quotes_batch into the unified `quotes`
  table. Replaces the V1 silo where council_quotes lived as a JSON blob in
  notebook_outputs (no verification infrastructure) and member_quotes lived
  as structured rows (verification chain only ran here).

# Per CLAUDE.md "Don't write prompts. Those are James's." James authorized
# Claude to provisionally author this prompt 2026-05-26 per D-046 as part of
# the Quotes Unification Refactor — see
# 01_Project_Overview/REFACTOR_QUOTES_UNIFICATION.md. Awaits James review pass.
#
# This prompt feeds BOTH the BroadcastPage hero section AND the Cast page
# per-member panels. It's the highest-touch quote-extraction surface in the
# system. Get the extraction right HERE and downstream UI inherits the rigor.

# This prompt depends on:
#   1. The city's notebooklm_persona_preamble (canonical names) being
#      prepended at runtime by the bridge.
#   2. The five-topic vocabulary defined in parsers/topic_tags.py +
#      client/src/utils/topicTags.ts. Keep this prompt's topic list in
#      sync with those — there's no automated check yet.
#   3. The city_vocabulary_corrections SPELLING CORRECTIONS block being
#      prepended by the bridge (T-017 Layer 2).
---

# Verbatim Quote Extraction — Unified Stream

Pull verbatim quotes from speakers acting in official capacity at this meeting. Tag each quote with: speaker classification, topic vocab, and a hero-or-not flag for the BroadcastPage. This is NOT a summary task — it's a quotation task. Verbatim accuracy is essential.

## Instructions (sent to NotebookLM)

You are extracting verbatim quotes from a council-meeting transcript. The output is a structured JSON object the bridge persists to a single canonical `quotes` table — the SAME quotes power the BroadcastPage hero section AND the Cast page per-member panels.

### Who counts as an "official-capacity speaker"

Include quotes from:
- **Council members** (Mayor, Vice Mayor, Councilmembers) speaking on council business
- **City staff presenting in their role** — City Manager, City Attorney, City Clerk, planning director, finance director, engineering director, parks director, department heads, etc.
- **Outside experts or consultants invited to present officially** — where their presenting role is identified in the agenda or named by the council during the meeting
- **Council attorney or clerk** when speaking substantively on agenda items

Do NOT include quotes from:
- **Private citizens** speaking during "Call to the Public", public comment, or public hearing input segments
- Anyone speaking from the audience rather than as a presenter or council member
- Anyone speaking in a personal-opinion capacity rather than an official one

If a speaker's role is ambiguous, EXCLUDE them — only include quotes where you can confidently identify the speaker as one of the acceptable categories.

### What counts as a quote

A quote is **a continuous block of words a SINGLE speaker said as one uninterrupted turn at the microphone.** Not a paraphrase, not a summary, not a multi-speaker exchange stitched together. If the transcript ambiguates the exact wording, prefer the longest unambiguous fragment.

**Include:**
- Statements during agenda discussion, regardless of length.
- Questions a member asked of staff or the public (those reveal stance too — e.g., "What's the water-table impact of this data center?" tagged `data_centers` + `water_rights`).
- Justifications a member gave for their vote on a roll call.
- Substantive staff explanations of agenda items (these inform public understanding).

**Exclude:**
- Pure procedural utterances ("I move to approve", "Second", "Roll call please").
- Personal-life comments, anniversaries, off-topic asides.
- Anything the speaker explicitly retracts in the same meeting.

### ⚠️ Where to start the quote — preserve cautionary preambles + rhetorical framings

When a speaker begins a substantive statement with a **cautionary preamble**, **hedging framing**, or **rhetorical posture marker**, INCLUDE those opening words. They carry the speaker's stance toward what they're about to say, and stripping them misrepresents how the speaker actually said it — turning a "cautious concern" into a flat "assertion of concern."

**Examples of opening words to PRESERVE (start the quote here, not after):**

- `"but I would just caution"` — the speaker is signaling that what follows is a warning, not a fact-claim. The phrase carries the cautious-warning posture; without it, the same words read as a flat assertion.
- `"I want to be careful here"` — explicit hedging that frames what follows as tentative.
- `"let me just note"` / `"I should add"` — flags that the speaker is adding to (not contradicting) the prior discussion. Carries the speaker's collaborative-rather-than-oppositional posture.
- `"I'm not entirely sure but"` / `"if I'm understanding correctly"` — explicit uncertainty markers. Dropping them misrepresents the speaker's confidence level.
- `"to be clear,"` / `"just to be clear,"` — emphasis on precision. The speaker is signaling they're choosing words carefully.
- `"with respect,"` / `"respectfully,"` — the speaker is signaling disagreement with prior comments. Drops change a measured-disagreement quote into a confrontational one.
- `"I would just say"` / `"all I would say is"` — the speaker is downplaying their own assertion. Strip and the assertion sounds more forceful than the speaker delivered it.
- `"in my view"` / `"my take is"` / `"as I see it"` — explicit personal-stance markers. Stripping them turns the speaker's opinion into a factual claim.

**The principle:** if removing the opening words would change what kind of statement this is — turning a cautious warning into a flat assertion, a tentative observation into a declaration, a respectful disagreement into a confrontation — KEEP the opening words. Filler ("uh", "um") between the cautionary preamble and the substantive content is fine for the cleaner to strip in the second pass; the preamble itself must reach the cleaner intact.

**Where a quote SHOULD start: the first word of the speaker's turn on this topic.** Not the first "substantive" word — the first word. The cleaner (T-011, gpt-4o-mini) handles disfluency removal in a downstream pass; your job is verbatim extraction of the complete spoken turn.

**A misextracted example** (surfaced by the 2026-05-26 Disputed Quotes Reviewer pilot, m101091 quote #46):

- Audio: `"but I would just caution um, the more the broader we get, we run into some concerns as far as statute..."`
- WRONG extraction: `"the more the broader we get, we run into some concerns as far as statute..."` (cautionary preamble stripped)
- RIGHT extraction: `"but I would just caution, the more the broader we get, we run into some concerns as far as statute..."` (preamble preserved; the "um" between will be stripped by the cleaner)

The first form makes the speaker (City Manager Bennett Walsh) sound like he's flatly asserting concerns; the second form correctly reflects that he was offering a cautious warning. The substance is identical; the posture is not.

### ⚠️ ONE speaker per quote — the load-bearing rule

**Never concatenate text spoken by different people into a single quote.** This is the single most dangerous failure mode in this extraction. If person A says something, then person B responds, then person A says something else, that is at MINIMUM two separate quote rows (A's two turns), NOT one merged block attributed to A.

A `quote_text` is **one continuous monologue from one speaker, ending the moment another speaker begins talking.** When a different voice enters the transcript — even for a single word, even for an "okay" or "thank you" — your quote MUST end at that boundary. Start a new row if the same speaker resumes after a brief response from another.

**WRONG — three speakers merged into one attribution:**
```json
{
  "speaker_name": "Smiley Ward",
  "quote_text": "that would be my thoughts too okay for now anyway just go with the medium and the four focused areas and Okay anybody else focus on generating sorry council Mike I'm sorry I agree but I would add I really want to push the generating sales tax new revenues..."
}
```
The "Okay anybody else" is the Mayor calling for the next speaker. The text after that is a different councilmember entirely. This block is THREE quotes, not one, and only the first sentence is actually Smiley Ward's.

**WRONG — back-and-forth Q&A flattened into one quote:**
```json
{
  "speaker_name": "Jamie Scott Stehly",
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

### Speaker classification

Every quote must carry a `speaker_class` from this closed set:

- `council_member` — Mayor, Vice Mayor, Councilmembers. Use the **canonical name** from the preamble at the top of this prompt (e.g., "Jamie Scott Stehly", not "J. Stehly" or "Councilmember Stehly"). The persona preamble at the top of this prompt lists the canonical names for this city.
- `staff` — City employees presenting in their official role. Use the name as given in the meeting if known (e.g., "Wendy Sheer" or "Bennett Walsh"); use the role title if the name isn't clear (e.g., "Police Captain", "Planning Director").
- `external` — Outside consultants, contractors, invited experts. Use the name + affiliation as given (e.g., "Jane Smith, ABC Consulting"); if no name, use the affiliation alone.

Set `speaker_role` to a clear role label (`"Mayor"`, `"Vice Mayor"`, `"Councilmember"`, `"City Manager"`, `"City Attorney"`, `"Police Captain"`, `"Planning Director"`, `"Outside Consultant"`, etc.) regardless of class.

### The five featured topic categories (V1)

Use these five topic tags for the `topic_tags` array. If a quote doesn't fit any, tag it `["other"]` (preserved in the DB; surfaces on the Cast page's "Other" section but not the topic-grouped views).

- `data_centers` — quotes about data-center proposals, hyperscaler expansion, the water/power demands of such facilities, related zoning or incentive votes, public concerns about data centers, council policy on data-center growth.
- `water_rights` — quotes about Colorado River allocations, well permits, groundwater conservation, drought response, water-supply infrastructure, water rate policy, drought-impact statements.
- `diversity_inclusion` — quotes about DEI policy, civic-access programs, language services, accessibility accommodations, equity in city services, demographic representation on boards/commissions.
- `lgbtq` — quotes about LGBTQ-related ordinances, official recognition or proclamations, public-comment exchanges on LGBTQ topics, council positions on LGBTQ policy.
- `education` — quotes about school-board liaison items, library funding or policy, civic-education partnerships, after-school programs, K-12 / community-college engagement with the city.
- `other` — anything else (preserved in the DB but not grouped on the Cast page).

Multi-tag is fine when a quote crosses topics — e.g., "We can't approve another data center without checking what it does to our wells" is `["data_centers", "water_rights"]`. Order doesn't matter.

### Broadcast hero subset — the 5-8 most substantive

After extracting all qualifying quotes, choose **5 to 8 of them** as the broadcast-hero subset. These will render on the BroadcastPage's hero quote section (the headline broadcast surface — fewer, more substantive). The rest still go to the Cast page per-member panels (where exhaustiveness matters).

**Criteria for hero selection (in priority order):**

1. **Substantive decisions or stances** — a quote that captures the speaker's clear position on an agenda item, especially on a vote. Procedural questions are NOT hero material.
2. **Topical importance** — quotes on the five featured topics outrank `other`-tagged quotes.
3. **Speaker mix** — try to include at least one quote per major speaker if the meeting had clear contributors. If only two members spoke substantively, that's fine — 5 hero quotes from two speakers beats 8 hero quotes diluted with procedural noise.
4. **Standalone readability** — a hero quote should be intelligible on its own without the surrounding meeting context. Quotes that depend on "as I was saying" or "to your earlier point" make poor hero rows.
5. **Multi-class coverage** — when relevant, include at least one staff quote alongside member quotes. A planning director explaining a zoning decision is often the most informative quote in the meeting.

Identify the hero subset by listing their `quote_ordinal_id` values in the top-level `broadcast_hero_ordinals` array (see output format below). If fewer than 5 substantive quotes exist in the whole meeting, list however many qualify; if more than 8 strong candidates exist, pick the 8 most substantive.

### Required output: strict JSON, no surrounding prose, no markdown fence

```json
{
  "quotes": [
    {
      "quote_ordinal_id": "Quote one",
      "speaker_name": "Ken Watkins",
      "speaker_role": "Mayor",
      "speaker_class": "council_member",
      "quote_text": "the verbatim words spoken",
      "topic_tags": ["water_rights"],
      "context": "Resolution 26-04 water-allocation discussion",
      "minutes_page_ref": null,
      "approximate_timestamp_seconds": 120
    },
    {
      "quote_ordinal_id": "Quote two",
      "speaker_name": "Wendy Sheer",
      "speaker_role": "Assistant Finance Director",
      "speaker_class": "staff",
      "quote_text": "...",
      "topic_tags": ["other"],
      "context": "Q1 budget variance presentation",
      "minutes_page_ref": null,
      "approximate_timestamp_seconds": 240
    }
  ],
  "broadcast_hero_ordinals": ["Quote one", "Quote two", "Quote four", "Quote seven", "Quote nine"],
  "extraction_notes": ""
}
```

### Per-field rules

- `quote_ordinal_id`: ordinal label `"Quote one"`, `"Quote two"`, … ascending in meeting order. Used to match against `broadcast_hero_ordinals`.
- `speaker_name`: canonical name for council members (no honorific prefix — match the preamble list at the top of this prompt); name-as-given for staff/external. If a speaker is unidentifiable but clearly in an official capacity (e.g., "the planning director"), use the role title as the name (e.g., `"Planning Director"`).
- `speaker_role`: clear role label.
- `speaker_class`: one of `council_member`, `staff`, `external`. Required.
- `quote_text`: verbatim words in plain text. No markdown. No surrounding quotes. Verbatim including disfluencies — the OpenAI cleaner (T-011) strips fillers in a SECOND pass.
- `topic_tags`: array, one or more from the closed five-topic vocab + `other`.
- `context`: optional, max 200 chars. The agenda item or topic being discussed. Neutral language only — "Resolution 26-04, water-allocation discussion" is fine; "tense discussion of controversial water vote" is NOT fine.
- `minutes_page_ref`: page number in the meeting minutes if identifiable; otherwise `null`.
- `approximate_timestamp_seconds`: integer seconds from meeting start, or `null` if NotebookLM can't determine it. (Whisper alignment will overwrite this with precise per-word timings in a downstream pass — see T-009 Phase 0b.)
- `broadcast_hero_ordinals`: array of 5-8 strings matching the `quote_ordinal_id` values of the hero subset. Required (may be empty array if the meeting has no quotes qualifying as hero — rare).
- `extraction_notes`: optional, max 200 chars — notes about the meeting that affected extraction (e.g., "audio was inaudible from 0:42:00 to 0:45:00").

### Strict rules summary

- **Speaker name MUST match the canonical name list for council_member class** (from the persona preamble at the top of this prompt). Don't paraphrase, don't add titles, don't shorten. If you encounter a council-attributed name that's not on the canonical list, drop the quote rather than guess.
- **ONE speaker per row, NO exceptions.** A `quote_text` ends the moment any other speaker begins talking. See the load-bearing rule above for examples.
- **One quote per row.** If a speaker made several distinct statements on different topics, return them as separate rows.
- **Multi-tag is fine.**
- **Don't editorialize in `context`.** Factual reference only.
- **`other` tag is fine.** Don't force-fit. The Cast page handles `other`-tagged quotes in its own section.
- **No pre-cleaning.** Give the verbatim transcript text including disfluencies; the cleaner runs later.
- **Hero subset selection is required.** Even if you produce 30 quotes, choose 5-8 as the broadcast hero. If the meeting was purely procedural and no quote qualifies as hero, return an empty `broadcast_hero_ordinals` array (rare — most meetings have at least 2-3 hero-grade quotes).
- **Return ONLY the JSON object.** No preamble, no closing thoughts, no markdown fence.

<!-- ZSPAN_MODEL_CONTENT_END -->

## Failure modes the bridge handles for you

- Malformed JSON → bridge logs error, row goes to `notebook_outputs.error`, operator sees a fail pill in the operator terminal.
- Non-canonical speaker name on a `council_member` row → bridge saves the row with `member_id=NULL` and logs a warning. Operator can correct via a future UI affordance.
- Empty `quotes` array → valid output. Means no official-capacity speaker said anything quote-worthy. The BroadcastPage and Cast page handle empty gracefully.
- Empty `broadcast_hero_ordinals` → valid output. BroadcastPage shows a "no hero quotes available" placeholder.

## What James should refine before this ships canonical

1. **Topic definitions** — Claude wrote one-paragraph definitions per topic, inherited from member_quotes_topic.md. James knows Kingman-specific context that should sharpen these. For example: is a quote about "data-center water usage" tagged `data_centers` OR `water_rights` OR both? Claude said both; James may have a stronger preference.

2. **"What counts as a quote" rules** — Claude included questions-as-quotes (a member asking staff "what's the water impact" gets tagged). Defensible call; James may prefer to limit quotes to declarative statements only.

3. **The retraction-handling rule** — Claude said exclude retracted quotes. James may prefer to keep BOTH the heated statement AND the retraction if they appear in the same meeting (the retraction itself can be newsworthy).

4. **Staff-quote scope** — Claude included substantive staff explanations (planning director on zoning, finance director on budget). The original council_quotes.md prompt was more permissive ("city staff presenting in their role"); the new prompt should make sure this scope is what James wants for the Cast page surface too (today member_quotes was council-members-only). James may prefer staff quotes to ONLY appear on BroadcastPage hero, not Cast page.

5. **Hero subset criteria** — Claude's 5 priority criteria are reasonable defaults but James may have stronger preferences (e.g., "always include the highest-vote-margin decision", "always include the longest-debated agenda item").

6. **The questions-reveal-stance judgment call** — same as in member_quotes_topic.md. Claude included questions; James may prefer to exclude them.

7. **The deflection / "no comment" rule** — what if a member is asked a direct question on a featured topic and says only "I'd rather not comment"? Currently dropped as non-substantive. James may want a more sophisticated rule.

## Migration context

Replaces:
- `prompts/council_quotes.md` (officials-only, 5-8 curation, no verification chain support) → retired in Chunk 9
- `prompts/member_quotes_topic.md` (council-members-only, exhaustive on 5 featured topics) → retired in Chunk 9

Persisted by:
- `parsers/database.save_quotes_batch(meeting_id, items, broadcast_hero_ordinals, city_name)`

Wired via (Chunk 4):
- `notebooklm_bridge/fetcher.py § OUTPUT_TYPE_REGISTRY` — add `quotes` as a `text` strategy
- `notebooklm_bridge/fetcher.py § _PERSONA_PREAMBLE_OUTPUTS` — add `quotes` for canonical-name binding
- Sidecar persist in `_maybe_persist_member_output` (rename / extend)
- Alignment trigger in `_fetch_transcript_words` (extend `align_meeting_quotes` to read from the new `quotes` table)
