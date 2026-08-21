---
output_type: community_calls_to_action
target: claude -p Sonnet 4.6 — V1-RAG-3 verbatim extraction of officials' public civic asks
status: claude_authored · awaits_james_review
authored_by: Claude
last_edited: 2026-06-29
description: |
  V1-CommunityCallsToAction-1 — the tri-category Key Quotes restructure
  splits the existing Key Quotes accordion into three distinct surfaces:
    (1) Key Decisions — substantive votes/motions (stays as-is)
    (2) Community Calls to Action — NEW; this prompt
    (3) Quotes — slim Fourth Estate set (existing extractor tightens)

  This prompt extracts ONLY verbatim civic asks made by officials
  directed at the public — volunteer opportunities, public-comment
  invitations, applications-due-by-date, "we need to hear from you on
  X." The framing flip is from *"Z-SPAN amplifies decisions made"* to
  *"Z-SPAN amplifies civic-action invitations from officials"* —
  officials get a megaphone, not just an audit. The platform becomes
  their ally, not just their accountability layer.

  **Worked anchor (load-bearing):** Tami Ring's food-bank ask at the
  m103753 Kingman 2026-06-02 City Council meeting — 40 lost Praise
  Chapel helpers; needs public to fill the gap. This is the canonical
  Community Call to Action that crystallized the category and becomes
  the first one published. Future extractions should be measured
  against the Tami Ring quote as the reference shape.

  Composes with [D-126](../../01_Project_Overview/DECISIONS.md#d-126)
  V1 no-AI-narrative — calls-to-action are VERBATIM, not synthesized.
  Composes with V1-Consensus-1 two-prong gate at the verification
  layer. Substrate for [S-096](../../01_Project_Overview/FUTURE_THOUGHTS.md#s-096)
  Portal (Portal aggregates calls-to-action across user's-city meetings
  into a per-user civic brief).

# Per CLAUDE.md "Don't write prompts. Those are James's." James
# authorized Claude to provisionally author this prompt 2026-06-29
# under the V1-CommunityCallsToAction-1 chunk green-light from the
# Z-SPAN-BRIDGE brainstorm session. Awaits James review pass — the
# extraction discipline, example calibration, and reject criteria are
# the highest-touch parts to verify against real meetings.
#
# This prompt depends on the city's notebooklm_persona_preamble
# (canonical names) and the city_vocabulary_corrections SPELLING
# CORRECTIONS block being prepended by the bridge at runtime.
---

# Community Calls to Action — Verbatim Civic Asks

Extract verbatim asks made by officials directed at the public — moments where someone at the dais invites citizens to do something specific, usable, and actionable. This is NOT a quote-curation task and NOT a decisions task. It's a narrow extraction of the platform's amplification surface for civic action.

## Instructions (sent as the synthesis prompt)

You are extracting Community Calls to Action from a council-meeting transcript. The output is a structured JSON list the bridge persists for rendering on the BroadcastPage's Community Calls to Action accordion (sits between Key Decisions and Key Quotes).

A Community Call to Action is a **verbatim ask from an official directed at the public**, with a **specific actionable hook**. The platform's role here is amplifier — when an official tells citizens "we need your help with X," Z-SPAN carries that ask to people who weren't in the room.

### Who counts as an "official"

Include asks from:
- **Council members** (Mayor, Vice Mayor, Councilmembers) speaking from the dais
- **City staff in role** — City Manager, Department Heads, Chief of Police, Fire Chief, City Attorney, etc.
- **Appointed officials presenting officially** — board chairs, commission members in their named capacity
- **Invited community-organization leaders** when the council has formally yielded the floor to them and they're naming a specific civic ask (this is how the Tami Ring food-bank moment qualifies — Praise Chapel Director invited to address the council on a public-need ask)

Do NOT include asks from:
- **Anonymous public commenters** during "Call to the Public" — their asks are valuable but go through a different surface
- **Vendors, lobbyists, or applicants** making business-interest asks (zoning support, contract favor, etc.)
- **Members in personal-opinion capacity** rather than addressing-the-public capacity

### What counts as a Community Call to Action

A Community Call to Action has THREE load-bearing properties:

1. **Verbatim** — extracted as the speaker actually said it. No paraphrase, no cleanup, no synthesis.
2. **Public-facing** — the ask is directed at citizens/residents/the general public, NOT at staff or fellow council members. "City manager, look into the budget" doesn't qualify. "Residents, we need volunteers for the food bank" does.
3. **Actionable hook** — the ask names a specific thing a citizen can do, with enough context to act on it. The hook can be:
   - A **volunteer opportunity** ("we need 40 volunteers for the food bank")
   - A **public-comment invitation tied to a future date or item** ("the rezoning hearing is July 8 at 6pm; written comments welcome until then")
   - An **application-with-deadline** ("planning commission seat opens; apply by August 15")
   - A **public-meeting attendance ask** ("the budget workshop is open to the public, Saturday morning at the library")
   - A **specific resource or information request** ("if you have photos of the historic Main Street depot before 1978, contact the museum")
   - A **survey, poll, or feedback channel** with a named URL or contact

**Examples that QUALIFY:**

- *"Praise Chapel has lost 40 of our regular helpers. We need community members to step up and fill those volunteer shifts — you can sign up at the food bank Saturday mornings."* (Tami Ring m103753 — canonical anchor)
- *"The Parks and Recreation board has three vacant seats. If you've ever wanted to shape what the city does with our parks, apply by August 15 — applications are on the city website."*
- *"We're holding a public hearing on the short-term rental ordinance July 8 at 6pm. If you can't make it, written comments accepted at clerk@kingman.gov until July 7."*

**Examples that DO NOT qualify:**

- *"I'd love to hear what residents think about this."* (Rhetorical; no actionable hook)
- *"City manager, please bring this back at the next meeting."* (Internal ask; not public-facing)
- *"If anyone has comments now would be the time."* (Procedural; the public-comment-segment opening line)
- *"Thank you to everyone who came out tonight."* (Appreciation; not an ask)
- *"We'd love your support on this."* (Vague; no specific actionable hook)

### Where to start and end the quote

Start at the **first word of the substantive ask**, including any cautionary preamble or framing that conveys the speaker's stance. End at the **last word of the actionable hook** — don't trail into the next agenda item or post-ask procedural remarks. The quote should be a single uninterrupted speaker turn; if the chair interjects mid-ask, end the quote there and (if the speaker resumes after the interjection) treat the resumed portion as a separate quote IF it carries a separate ask.

### Format

Output a JSON array (one entry per Community Call to Action, in chronological order of the meeting). Each entry has:

```json
{
  "speaker_name": "Tami Ring",
  "speaker_role": "Praise Chapel — Food Bank Director",
  "quote_text": "Praise Chapel has lost 40 of our regular helpers. We need community members to step up and fill those volunteer shifts — you can sign up at the food bank Saturday mornings.",
  "ask_kind": "volunteer_opportunity",
  "actionable_hook": "Sign up at Praise Chapel food bank Saturday mornings",
  "deadline": null,
  "contact": null,
  "video_timestamp_seconds": 1847.3,
  "chunk_index": 12
}
```

Field semantics:

- **speaker_name** — canonical name as resolved from the SYMBOLS block. Use the canonical roster name when the speaker is on the city's roster; otherwise the name as introduced in the meeting.
- **speaker_role** — official title or affiliation. Use the speaker's introduction as it appears in the transcript ("Mayor", "Vice Mayor", "Councilmember", "City Manager", "Praise Chapel — Food Bank Director", etc.).
- **quote_text** — verbatim, as actually spoken. Preserve the speaker's exact wording including hesitations only if they're load-bearing (usually skip "uh", "um", restart-trailing-words).
- **ask_kind** — one of: `volunteer_opportunity` · `public_comment_invitation` · `application_with_deadline` · `public_meeting_attendance` · `resource_request` · `feedback_channel` · `other` (use `other` sparingly; explain in `actionable_hook` if you do).
- **actionable_hook** — a single-sentence plain-language summary of what a citizen can do, distilled from the quote. NOT a paraphrase of the quote text — a render-friendly summary for the accordion-closed view. ~10-15 words target.
- **deadline** — ISO-date string (YYYY-MM-DD) if the ask names a specific deadline; otherwise `null`. Convert relative dates ("July 8th") to absolute based on the meeting date in context.
- **contact** — email / phone / URL / physical address mentioned in the ask, if any; otherwise `null`. Verbatim from the speaker.
- **video_timestamp_seconds** — start time of the quote in the meeting video, from the chunk's `start_seconds`.
- **chunk_index** — the chunk where the quote appears, from the chunk's `chunk_index` metadata.

### Honest-empty discipline

If the meeting contains NO Community Calls to Action that pass all three load-bearing properties (verbatim + public-facing + actionable hook), return an empty JSON array `[]`. **Do NOT** pad with weak candidates, do NOT downgrade a Key Decision or a Key Quote to fill the slot, do NOT synthesize an ask from a council member's general statement of concern. The honest-empty signal IS the right answer for procedural-only meetings, executive sessions, and meetings where the dais didn't make any direct public asks.

Most council meetings will yield 0–2 Community Calls to Action. Three or more is unusual. If you find five or more candidates, your discipline is too loose — re-read the qualify/disqualify examples above and tighten.

### Anti-hallucination guard

Every `quote_text` MUST appear verbatim in one of the provided chunks. Every `video_timestamp_seconds` and `chunk_index` MUST come from the actual chunk metadata. The trust model is that a citizen can listen to the karaoke timecode and verify the speaker said exactly what we say they said. If you can't find the verbatim text in the provided chunks, do NOT include that ask — exclude it and (if relevant) note in your reasoning what got dropped and why.

### Output format

Output ONLY the JSON array, no preamble, no closing line, no markdown fence. The bridge parser expects raw JSON.

```json
[
  {"speaker_name": "...", "speaker_role": "...", "quote_text": "...", "ask_kind": "...", "actionable_hook": "...", "deadline": null, "contact": null, "video_timestamp_seconds": 1847.3, "chunk_index": 12}
]
```

Empty result:
```json
[]
```

<!-- ZSPAN_MODEL_CONTENT_END -->

---

## What James should refine on review

1. **The qualify/disqualify cutline** — especially the "invited community-organization leaders" inclusion. The Tami Ring case is the anchor that authorizes this; should it be tighter (only city-employed officials)? Or looser (any invited speaker addressing the public)?
2. **The `ask_kind` taxonomy** — collapse / rename / add categories based on what real meetings actually produce. Run the Tami Ring + Bullhead trio backfill first and look at what comes back.
3. **Whether the structured fields are worth keeping** (`deadline`, `contact`, `actionable_hook`, `ask_kind`) vs going simpler (just speaker + role + quote + timestamp). Structured fields enable Portal aggregation per S-096 but add extraction complexity.
4. **The honest-empty calibration** — 0-2 is the expected per-meeting yield. If real meetings consistently yield 4-5, the discipline is too loose. If they consistently yield 0, too tight.
5. **The verbatim-vs-near-verbatim line** — the prompt says "preserve exact wording" but "skip hesitations." Is that the right line, or should hesitations stay (closer to a court reporter's transcription)?
6. **Cross-link to the Portal aggregation** — when S-096 ships, the Portal will aggregate these across the user's-city meetings. Should the prompt emit any per-call fields that would help the Portal pre-filter (urgency tier, geographic specificity, audience segment)? Defer if uncertain; the Portal can compute these post-hoc.
