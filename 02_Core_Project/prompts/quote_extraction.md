---
output_type: quote_extraction
target: claude -p Sonnet 4.6 — V1-RAG-3 per-member attributed-quote extraction from Qdrant-retrieved Whisper transcript chunks
status: claude_authored · awaits_james_review
authored_by: Claude
last_edited: 2026-06-23

description: |
  V1-RAG-3 successor to the NotebookLM-bridge quote extraction path that
  V1 mode (ZSPAN_V1_RAG3_ONLY=1) disabled per D-126. The NotebookLM
  extraction was acting as a linker pass — it consumed the [SYMBOLS]
  block (canonical-name-to-aliases table from current_members +
  whisper_vocabulary_hints) and used LLM-reasoning to attribute
  imperfectly-transcribed surnames to canonical council_members rows.
  Dropping that pass left V1-RAG-3 meetings (Bullhead trio + CC) with
  populated council_members but zero member-attributed quotes, breaking
  the existing TruthBook surface for everything past Kingman.

  Updated 2026-06-23: research-grounded selection discipline added (the
  six-dimension / three-tier matrix from
  03_Research/QUOTES_journalism_grounded_selection_research_2026-06-23.md).
  Grounds quote selection in Harcup & O'Neill 2017 news values + the
  American Journalism Handbook's quote-vs-paraphrase test. Before this
  update, the selection criteria were loose enough that mundane
  procedural statements + factual reports could slip onto officials'
  permanent public records. The discipline now filters to publish-tier
  quotes only — utterances that pass three gating tests (substantive
  content, on-record context, journalism-grade quotability) AND match
  at least one news value from the Z-SPAN load-bearing subset (Power
  elite / Magnitude / Relevance / Conflict / Follow-up / Bad news /
  Good news). Operator-authorized 2026-06-23 to author under the
  claude_authored exception pending James review.

  This prompt restores the linker pass on Sonnet over Qdrant chunks
  instead of NotebookLM over the full transcript. Sonnet receives a
  batch of chunks + the SAME symbols block the bridge already builds +
  the canonical roster, and emits a JSON list of attributed quote
  candidates. The downstream D4 persistence path resolves speaker_name
  to member_id, runs quote_align for word_timings against the
  meeting's transcript_words, and INSERTs into the canonical quotes
  table — same shape NotebookLM was producing, just powered by
  Sonnet+Qdrant.

  Pure extraction, NO synthesis. Every quote_text returned MUST appear
  verbatim in one of the provided chunks; the trust model is that an
  operator can listen to the karaoke timecode and verify the
  attribution. No narrative inference, no voting-pattern summary, no
  accountability claim — D-126's AI-slop-trust-gap discipline holds.

# This prompt depends on:
#   1. The CANONICAL_ROSTER section being populated by the caller from
#      database.get_council_members(city_name) with name + role + seat_id.
#   2. The SYMBOLS_BLOCK being built by notebooklm_bridge.symbols.build_symbols_block
#      — same generator the NotebookLM pass uses. The block contains
#      canonical-name + accepted-aliases entries for each council member,
#      derived from whisper_vocabulary_hints + the _derive_member_aliases
#      helper.
#   3. CHUNKS being one batch of Qdrant-retrieved chunks for ONE meeting,
#      each tagged with chunk_index + start_seconds timecode metadata.
#      D3 batches a meeting's full chunk set across multiple calls; this
#      prompt processes one batch.
#   4. The five-topic vocabulary in parsers/topic_tags.py +
#      client/src/utils/topicTags.ts. Keep in sync; no automated check.

# Per CLAUDE.md "Don't write prompts. Those are James's." — Claude
# provisionally authored 2026-06-20 with James's session-explicit
# greenlight on the D1-D8 chunk plan after the C5 smoke surfaced the
# V1-mode linker-pass gap. Awaits James review pass per the existing
# PROMPT_REVIEW_LEDGER queue.
---

# V1-RAG-3 Attributed Quote Extraction

Extract verbatim quotes by council members + city staff acting in official capacity from a batch of meeting transcript chunks. Attribute each quote to a canonical council_members row using the provided symbols block + canonical roster. Output strict JSON.

## Instructions

You are extracting attributed quotes from one batch of Whisper-transcribed chunks of a U.S. municipal city council meeting. The chunks come from one meeting's full transcript; you may receive a subset of the meeting's total chunks in this call. Other batches in this meeting are extracted independently and combined downstream.

### Who counts as an official-capacity speaker

INCLUDE quotes from:
- Council members (Mayor, Vice Mayor, Councilmembers) speaking on council business
- City staff presenting in their role — City Manager, City Attorney, City Clerk, planning director, finance director, engineering director, parks director, department heads
- Outside experts or consultants invited to present officially when the chunk identifies them as such

EXCLUDE quotes from:
- Private citizens speaking during public comment or "Call to the Public"
- Audience members reacting to council business
- Anyone speaking in a personal capacity rather than an official one

If a speaker's role is ambiguous from the chunk context, EXCLUDE the quote.

### The attribution discipline (the load-bearing part)

The chunks you receive are Whisper-transcribed and may mis-spell council member surnames. The CANONICAL_ROSTER + SYMBOLS_BLOCK below give you the ground-truth member names + every accepted alias variant. When a chunk text contains a near-miss surname ("Doman" for "Dallman", "Newland" for "Newlin", "Diamond" for "Dykens"), attribute the quote to the closest canonical roster member, NOT to the literal mistranscribed string.

Your `speaker_name` output field MUST be the EXACT canonical name from the roster ("Karen Dallman", "Jamie Scott Stehly", etc.) — never the alias form, never the mistranscribed form. The downstream member_id resolver matches on canonical names.

When a chunk contains no clear speaker attribution at all (no surname mention, no role indicator like "Mayor" or "Councilmember", no procedural cue like "the chair recognizes"), and you cannot reasonably infer who is speaking from the chunk's content alone, EXCLUDE the quote. Honest-empty over fabricated attribution.

### Diarized speaker labels (Phase 2, added 2026-06-24)

Chunks MAY arrive in two formats depending on whether the meeting was diarized:

**Undiarized format (legacy, pre-Phase-2):**

```
[chunk_index=12 start_seconds=347 timecode=05:47]
We have a recommendation from the planning commission. Mayor Watkins, would you like to open this item? I move to approve the resolution. Second...
```

You attribute via proximity + CANONICAL_ROSTER inference per the discipline above.

**Diarized format (Phase 2, when available):**

```
[chunk_index=12 start_seconds=347 timecode=05:47]
  SPEAKER_03: "We have a recommendation from the planning commission. Mayor Watkins, would you like to open this item?"
  SPEAKER_00: "Thank you. I'd like to entertain a motion to approve the resolution."
  SPEAKER_01: "I move to approve the resolution."
  SPEAKER_03: "Second."
```

Each `SPEAKER_NN:` block is one contiguous speaker turn. The cluster labels (`SPEAKER_00`, `SPEAKER_01`, etc.) are anonymous — assigned by pyannote per meeting; SPEAKER_00 in this meeting has no relationship to SPEAKER_00 in any other meeting.

**When a CLUSTER_ROSTER block is provided below**, it maps these anonymous cluster labels to canonical roster members for THIS meeting (e.g., `SPEAKER_00 → "Ken Watkins"`). In that case: read the cluster label off the chunk's `SPEAKER_NN: "..."` block, look up the canonical name in CLUSTER_ROSTER, and use that as `speaker_name`. Stop inferring from textual proximity — the cluster label is the authoritative attribution signal.

**When no CLUSTER_ROSTER is provided** (D6 mapper hasn't run for this meeting, or no high-confidence mapping was found), fall back to the existing CANONICAL_ROSTER + SYMBOLS_BLOCK proximity-inference discipline above. Don't use raw `SPEAKER_NN` as the speaker_name — those aren't human-readable.

**The `speaker_cluster_label` output field** — when extracting from diarized chunks, ALSO emit `speaker_cluster_label: "SPEAKER_NN"` alongside `speaker_name` in each quote object. This preserves the audit trail so a future operator can verify which cluster the quote was attributed from. Set to null when the chunk is undiarized.

The `OVERLAP` and `UNKNOWN` cluster sentinels indicate genuine cross-talk (mayor cutting off councilor mid-word) and pyannote-skipped audio respectively. EXCLUDE quotes from these sentinel speakers — they don't represent a clean single-speaker utterance.

### What counts as a quote

A quote is a continuous block of words a single speaker said as one uninterrupted turn at the microphone. Not a paraphrase, not a summary, not a multi-speaker exchange stitched together.

INCLUDE:
- Statements during agenda discussion (any length)
- Questions a member asked of staff or the public (those reveal stance — e.g., "What is the water-table impact of this proposal?")
- Justifications a member gave for their vote on a roll call
- Substantive staff explanations of agenda items

EXCLUDE:
- Pure procedural utterances ("I move to approve", "Second", "Roll call please")
- Personal-life comments, anniversaries, off-topic asides ("I like geez", "I had a long week", "Happy birthday to my granddaughter")
- Social pleasantries and scheduling utterances ("Good morning everyone", "Let's move to the next item")
- Factual reports that paraphrase cleanly with no loss of meaning (e.g., "The footing report came in yesterday" — better as reported speech downstream)
- Non-committal direction-pointers that commit to nothing ("I think we should look into that", "We should consider that going forward")
- Anything the speaker explicitly retracts in the same exchange

(The Selection discipline section below sharpens these exclusions with the publish-tier gating tests + worked accept/reject examples. The lists above are the structural filter; the section below is the editorial-judgment filter.)

### Where to start the quote — preserve hedging + cautionary preambles

When a speaker begins a substantive statement with a cautionary preamble or hedging framing ("but I would just caution", "I want to be careful here", "with respect", "in my view"), INCLUDE those opening words. They carry the speaker's stance. Stripping them turns a cautious warning into a flat assertion or a respectful disagreement into a confrontation. The principle: if removing the opening words changes WHAT KIND of statement this is, KEEP them.

Disfluencies ("uh", "um") inside a quote stay — a downstream T-011 cleaner pass handles disfluency removal. Your job is verbatim extraction.

### Selection discipline — what makes a quote publish-worthy (the V1 tier-1 test)

This section is the editorial-judgment filter. Grounded in newsroom standards (Harcup & O'Neill 2017 news values + the American Journalism Handbook's quote-vs-paraphrase principle). The structural filters above (speaker class, attribution, INCLUDE/EXCLUDE lists) gate WHO speaks and WHAT kinds of content are eligible. This section gates WHICH eligible utterances actually clear the bar for publication on the speaker's permanent public record.

Before emitting a quote, evaluate it against three gating tests + the news-value cross-check. **Only emit a quote if it passes ALL THREE gating tests AND matches at least one news value.**

**G1 — Substantive content** (binary)
- PASS: states a position, decision, factual claim with truth-value over time, judgment, vote rationale, commitment, or value-judgment
- FAIL: pure procedural ("Motion to approve", "Second"), social pleasantry ("Good morning everyone"), filler/aside ("I like geez", "I had a long week"), parliamentary mechanics, scheduling, or roll-call mechanics
- If FAIL → reject

**G2 — Record context** (binary)
- PASS: said during formal meeting deliberation, formal public comment, vote justification, or staff presentation in role
- FAIL: sidebar/aside while another speaker holds the floor, pre-call audio, post-adjournment, accidental hot-mic moment
- If FAIL → reject

**G3 — Journalism-grade quotability** (binary)
- PASS: the utterance conveys emotion, opinion, judgment, value, stance, or has distinctive wording that would lose its force in paraphrase
- FAIL: pure factual report where paraphrase would convey the same content with no loss
- Principle (American Journalism Handbook): *"Direct quotes are most useful for conveying emotions, opinions, and personal experiences. Paraphrased statements are particularly useful for conveying purely factual information."*
- If FAIL → reject

**News-value cross-check (N) — at least ONE must apply** from the Z-SPAN load-bearing subset (Harcup & O'Neill 2017):
- **power_elite** — quote is by a load-bearing local power figure (mayor, council member) speaking on a substantive matter
- **magnitude** — the topic affects significant budget, population, or duration of impact
- **relevance** — clearly material to constituents' lives or policy commitments
- **conflict** — split vote, disagreement, debate, dissent, contested position
- **follow_up** — connects to a prior commitment that's now being honored, broken, or revisited
- **bad_news** — failure, problem, risk, controversy, scandal exposure
- **good_news** — improvement, win, milestone reached, public benefit confirmed

If a quote passes G1 + G2 + G3 + at least one news value: **emit it.** Record which news value(s) matched in the `news_values` output field, with a one-line `selection_rationale` for operator audit.

If it passes G1 + G2 but fails G3 (factual report, paraphrasable with no loss): **do not emit.** The pipeline currently has no reported-speech layer; V1 omits these.

If it fails G1 OR G2: **do not emit.**

### Worked accept/reject examples

ACCEPT — substantive vote-rationale:
> *"I'm voting no on the budget amendment because it cuts library funding without justification."*
- G1 PASS · G2 PASS · G3 PASS · matches power_elite, magnitude, relevance, conflict, bad_news

ACCEPT — committal position-statement with distinctive wording:
> *"This rezoning proposal would put a hyperscaler data center between two residential neighborhoods — that's not a tradeoff this council should make."*
- G1 PASS · G2 PASS · G3 PASS · matches power_elite, magnitude, relevance, bad_news

REJECT — filler/aside (fails G1):
> *"I like geez."*

REJECT — pure procedural (fails G1):
> *"Motion to approve item 7B."*

REJECT — factual report that paraphrases cleanly (fails G3):
> *"The footing report came in yesterday morning."*
(Better as reported speech: "The City Engineer confirmed the footing report arrived yesterday.")

REJECT — non-committal direction-pointer (fails G3):
> *"I think we should look into that next quarter."*
(Doesn't commit to a position; no distinctive wording that paraphrase would lose.)

REJECT — social pleasantry (fails G1):
> *"Good morning everyone, thank you for being here."*

### Topic tagging

For each quote, assign one or more topic tags from this controlled vocabulary. A quote may carry multiple topics; pick the most specific:

- `data_centers` — hyperscaler facilities, data-center zoning, water/power demands of such facilities
- `water_rights` — Colorado River allocations, wells, groundwater, drought, water-supply infrastructure
- `diversity_inclusion` — DEI policy, language access, equity in city services
- `lgbtq` — LGBTQ ordinances, proclamations, public-comment exchanges on LGBTQ topics
- `education` — school-board liaison items, library funding, civic-education partnerships
- `other` — everything else (budget, zoning, public safety, infrastructure, procedural)

If a quote spans multiple topics, emit the full topic_tags list (e.g., `["data_centers", "water_rights"]` for a data-center proposal where someone raises the water-table concern).

### Output schema (strict JSON, no preamble, no closing line)

Emit exactly this shape — a top-level JSON object with one `quotes` array:

```json
{
  "quotes": [
    {
      "speaker_name": "Karen Dallman",
      "speaker_role": "Mayor",
      "speaker_class": "council_member",
      "speaker_cluster_label": "SPEAKER_03",
      "quote_text": "verbatim text from the chunk, no paraphrase",
      "topic_tags": ["water_rights"],
      "video_timestamp_seconds": 7588,
      "chunk_index": 27,
      "news_values": ["power_elite", "magnitude", "relevance"],
      "selection_rationale": "Vote-rationale committing Mayor to specific position on water-rights ordinance"
    }
  ]
}
```

Field rules:
- `speaker_name`: EXACT canonical name from CANONICAL_ROSTER. For staff or outside experts, use the name as it appears in the chunk. For mistranscribed surnames, return the canonical form.
- `speaker_cluster_label`: when extracting from a diarized chunk (one with `SPEAKER_NN: "..."` blocks), the cluster label of the speaker the quote came from. Set to `null` for undiarized chunks. Audit-trail field — surfaces in the operator-debug surface, not in the public UI.
- `speaker_role`: `Mayor` / `Vice Mayor` / `Council Member` / `Staff` / `Expert`. Use the roster's `role` field when speaker_class is `council_member`.
- `speaker_class`: `council_member` for council/mayor/vice-mayor; `staff` for city staff; `expert` for outside presenters.
- `quote_text`: verbatim from a chunk. Match the chunk text exactly (modulo case and punctuation normalization for sentence boundaries).
- `topic_tags`: array of 1+ tags from the controlled vocabulary above.
- `video_timestamp_seconds`: integer seconds, derived from the chunk's `start_seconds` metadata.
- `chunk_index`: integer chunk_index of the chunk this quote came from.
- `news_values`: array of 1+ news-value tokens from the load-bearing subset (`power_elite`, `magnitude`, `relevance`, `conflict`, `follow_up`, `bad_news`, `good_news`). The selection-discipline gate requires at least one match.
- `selection_rationale`: **reader-facing one-line summary of WHAT the speaker is saying** — the headline version of the stance, the substance compressed into a phrase a scanning citizen can absorb at a glance. This is NOT operator-debug audit language; it surfaces in the public UI alongside the speaker's name as the collapsed-card summary (full quote text on click). Style rules:
  - **Present-tense action verb opening** ("Declares...", "Presses for...", "Frames... as...", "Recuses from...", "Voices support for...", "Warns that...", "Calls on...").
  - **Subject elided** (the speaker is named separately in the card; don't repeat "Council member X declares" — just "Declares...").
  - **Substance, not meta-criteria** — name WHAT the speaker is saying, not WHY it passed the discipline gates. "Strong concerns over grill losses" YES; "Substantive question with fiscal-accountability framing" NO.
  - **≤90 characters**. Should read like a newspaper sub-headline or pull-quote chyron.
  - **Worked examples (canonical style)**:
    - *"Declares police vacancy recruitment is failing"*
    - *"Strong concerns over grill losses"*
    - *"Recuses from Main Street trail vote"*
    - *"Pushes for transit solution; flags funding accountability gap"*
    - *"Frames transit vs police/fire as budget tradeoff; names affordability as priority"*
    - *"Presses for timeline on golf course privatization study"*
    - *"Supports Route 66 trail but raises water-cost accountability questions"*
    - *"Voices on-record support for Route 66 Nature Trail"*
    - *"Warns CR-252 would constitutionally bar all AZ cities from raising taxes"*
  - **Anti-pattern (avoid)** — operator-debug schema-dump language:
    - ❌ *"Vice Mayor commits council to transit solution while flagging funding accountability gap; 'speed of government' framing is distinctive"* (too long; meta-commentary about distinctiveness; names the speaker redundantly)
    - ❌ *"Council member places an explicit on-record statement of support for the Route 66 Nature Trail project"* (verbose; "places an explicit on-record statement of support for" is operator-audit phrasing — say "voices support for")
    - ❌ *"Substantive vote-rationale committing speaker to specific position on water-rights ordinance"* (meta-commentary about substantiveness; doesn't name the actual position)

If a chunk contains MULTIPLE attributable quotes by different speakers (e.g., a back-and-forth exchange), emit one entry per speaker turn.

If a chunk contains NO attributable quotes (procedural, public comment, ambiguous speaker), emit nothing for that chunk.

If THE ENTIRE BATCH contains no attributable quotes, emit `{"quotes": []}` — never fabricate to fill the array. Honest-empty IS valid output.

Output ONLY the JSON object — no preamble, no `Answer:` label, no markdown code fence, no closing line. The downstream parser expects raw JSON on stdout.

<!-- ZSPAN_MODEL_CONTENT_END -->
