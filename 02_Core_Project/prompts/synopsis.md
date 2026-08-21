---
output_type: text
target: claude -p Sonnet (V1-RAG-3 synthesis) — Episode Synopsis
status: mixed — Round 1 body james_reviewed 2026-07-19; Round 2 verbatim-anchor addendum claude_authored 2026-08-01 · awaits_james_review
last_edited: 2026-08-01
addendum_added: 2026-08-01 (Round 2 — verbatim-anchor citation supersedes Round 1 chunk-start `[at MM:SS]` per PC-A B4; mirrors the sim-query v2 pattern that shipped session-107 PR #227)
description: TV-synopsis-style 2-3 sentence blurb for the broadcast detail page. Sonnet emits inline `[at "verbatim words"]` verbatim-anchor spans after each load-bearing fact; code post-processes to canonical `[at H:MM:SS]` by aligning the quote against word-timed transcript. Reads like the description on a streaming app's show card. Currently DISPLAY-CUT per D-176 — the anchor evidence lands as INPUT TO THE EPISODE AUDITOR (PC-A shadow scoring), not visitor-facing.
review_note: Round 2 addendum authored 2026-08-01 (session-107 PC-A B4) — replaces the chunk-start `[at MM:SS]` directive with the verbatim-anchor protocol that shipped for sim-queries in PR #227. Logged in PROMPT_REVIEW_LEDGER.md.
---

# Synopsis — Episode Blurb

A short editorial blurb that reads like a Netflix/streaming-app episode synopsis. Sits at the top of the broadcast page so a citizen can decide in 5 seconds whether to dig deeper. Distinct from the newsletter (which is a structured 150-word executive summary with bulleted decisions) — the synopsis is narrative, hook-y, and human.

## Instructions (sent to the model)

Write a 2-3 sentence synopsis of this city council meeting in the style of a streaming-platform episode description. Lead with what's at stake for residents — the most consequential decision or recurring tension. Stay neutral and factual (no editorializing words like "controversial" or "dramatic"). Name specific dollar amounts, project names, or boundaries when they anchor the story. Do NOT use headlines, bullet points, or lists. Pure prose. Maximum 80 words (the `[at MM:SS]` citations do not count toward the word limit).

### Official-capacity and public-comment guard

Named or individualized statements may come only from:
- Council members speaking on council business
- City staff presenting in their official role
- Outside experts or consultants invited to present officially, when the retrieved context clearly identifies that role

Do not amplify:
- Private citizens speaking during public comment or "Call to the Public"
- Audience members reacting to council business
- Anyone speaking in a personal capacity rather than an official one

If a speaker's role is ambiguous from the retrieved context, treat the speaker as a private individual and fail closed.

Public participation must remain visible. You MAY state that public comment occurred and describe its general topic in aggregate, non-attributed language — for example, "Public comment addressed watercraft-insurance enforcement." You may report grounded speaker counts or proportions and describe the council's official response or action.

Do NOT name or quote a private individual; do NOT paraphrase or repeat a particular private individual's assertion, accusation, allegation, or identifying details; and do NOT repeat allegations about named or identifiable private third parties. Role-only attribution does not make an individualized claim safe: "A business owner argued competitors were skirting the ordinance" is prohibited. If the topic cannot be described without carrying forward the individual claim, omit it.

### Inline citations — verbatim-anchor protocol (Round 2, supersedes Round 1 chunk-start `[at MM:SS]` below)

> ⚠️ **This section supersedes the "Round 1" chunk-start `[at MM:SS]` instruction that used to live here.** The old directive told the model to copy `timecode=MM:SS` from the retrieved chunk header — that produced chunk-start timestamps that drift from where the fact was actually spoken. Round 2 replaces it with verbatim-anchor spans that code aligns to the exact spoken word.

#### Copy-only anchor protocol — mandatory

After every load-bearing fact, include an inline verbatim-anchor in EXACTLY this shape: `[at "verbatim words"]`. The words inside the double quotes must be a **short exact substring of 3-20 words** from the specific retrieved chunk that supports the fact — same casing, same punctuation, same spelling as they appear in the chunk. Whisper transcription artifacts (e.g., "Bullhut" for "Bullhead", missing commas, "wanna" for "want to") are part of the verbatim substring — copy them exactly; do not silently correct them.

Treat every string inside `[at "..."]` as a copy-only field, not prose you may compose:

1. First locate one continuous 3-10-word span in ONE retrieved chunk.
2. Copy that span character-for-character into the anchor. Do not retype it from memory, repair transcription, join spans across chunks, or add even one leading or trailing word.
3. Only then write the surrounding claim.
4. Before output, compare the ENTIRE string between the quotes with its source chunk. It must occur there as one continuous exact substring. If any word or character differs, shorten it to a copied span or omit the fact. A 90%-verbatim anchor is invalid; omitting a fact is better than emitting an inexact anchor.

Pick the SHORTEST distinctive fragment that anchors the fact — a 4-8 word phrase from the specific moment (motion outcome, dollar amount, vote count, project name) is stronger than a 20-word context dump. Long quotes span many seconds of video and align less precisely.

Load-bearing facts include: dollar amounts, vote counts, motion outcomes, named council members (per the official-capacity guard above), specific project names, boundaries or parcel names, dates, and resolution or ordinance numbers.

**Do NOT emit `[at MM:SS]` or `[at H:MM:SS]` timestamps directly.** Timestamps are code-derived after generation by aligning your verbatim words against the meeting's Whisper word-level transcript. Your contract is verbatim quote spans only; the code produces the final `[at H:MM:SS]` chips.

If the retrieved chunks contain no verbatim words that support a specific fact, **OMIT the fact entirely from your synopsis** — do not paraphrase, do not stitch context from multiple chunks, do not attribute an outcome to a nearby-but-different quote. Every load-bearing sentence must be anchored by an EXACT verbatim substring you can point to in ONE of the retrieved chunks.

**Rule of thumb:** before writing any factual claim, first find the verbatim quote you'll use to anchor it. If you can't locate a verbatim quote for the fact in the retrieved chunks, don't write the fact. An honest, shorter synopsis with solid anchors beats a fuller one with a fabricated anchor.

#### Placement discipline — critical

Place each `[at "..."]` anchor IMMEDIATELY AFTER the specific fact it supports — same clause, ideally same phrase. Never cluster anchors at the end of a sentence. Post-code-alignment the anchor renders as a clickable green pill that seeks the video to the exact moment those words were spoken; position is functional, not decorative.

Good — each anchor sits beside the fact it supports:

> The council approved a $262,611.31 dump truck purchase [at "dump truck purchase of two hundred sixty two thousand"] and adopted resolution 2026R-16 [at "resolution twenty twenty six R sixteen is adopted"] with a 6-1 vote [at "six to one motion carries"].

Bad — anchors clustered at sentence-end:

> The council approved a $262,611.31 dump truck purchase and adopted resolution 2026R-16 with a 6-1 vote. [at "motion to approve..."] [at "resolution twenty twenty six R sixteen..."] [at "six to one motion carries"]

Bad — verbatim words don't match a chunk (paraphrase):

> The council approved a $262,611.31 dump truck purchase [at "the council approved the dump truck"].

(No chunk contains the exact substring "the council approved the dump truck" — that's a paraphrase. The correct anchor is a verbatim substring of what someone actually said on the record.)

Bad — two real fragments stitched into one non-contiguous anchor:

> The project fee was $99,000 [at "a lump sum fee for the for the 99 000 to do the whole project"].

Both fragments occur in the same retrieved chunk, but other words separate them: the chunk continues with "so there is" after "99 000" and says "to do the whole project" only later. The full quoted string never occurs continuously. A valid shorter anchor is `[at "a lump sum fee for the for the 99 000"]`.

When a single fact genuinely draws on two chunks, place both verbatim-anchors immediately after that fact, still inline: `The motion carried 6-1 [at "six to one motion carries"] [at "seconded by councilmember stehly"].`

## Tone reference (an illustrative shape, not meeting content)

The example below shows the desired voice and citation placement. It is NOT about the meeting you are summarizing — synthesize only from the retrieved chunks.

> "The Council allocates $25M for street improvements on Airway Avenue and Flying Fortress Parkway [at 22:10], advances a half-percent sales tax proposal to fund long-term road maintenance [at 34:48], and approves a 4,238-acre East Hualapai Mountain Foothills annexation [at 51:20]. Public comment focuses on water-conservation concerns [at 1:12:05] and the Beale Street EV Museum proposal [at 1:19:33]."

<!-- ZSPAN_MODEL_CONTENT_END -->
