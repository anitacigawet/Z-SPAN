---
output_type: text
target: NotebookLM — Text Query (Council Quotes for dedicated UI component)
status: in use (Claude-drafted from T-002 experiment runs; James fine with as-is, refine when desired)
last_edited: 2026-05-07
description: Verbatim quotes from official-capacity speakers, returned as JSON. The bridge persists the raw response to notebook_outputs; the <CouncilQuotes> component on BroadcastPage parses and renders. Replaces council_sentiment per D-031.
notes: |
  Drafted by Claude using the T-002 experimentation as a baseline. The same
  wording reproducibly produced clean officials-only extractions with ordinal
  IDs across 4 experiment runs. James is fine with this version as-is; refine
  whenever a refinement is wanted (no review gate on the prompt itself).
  Architecture: FUTURE_THOUGHTS.md § T-003.
  Constraints: DECISIONS.md § D-028 (provenance), § D-029 (ordinal IDs,
  no timestamps while on the unofficial wrapper), § D-030 (own surface),
  § D-031 (replaces council_sentiment).
---

# Council Quotes — Verbatim Quotes from Officials (for Show-Page Component)

Returns 5–8 verbatim quotes from official-capacity speakers from the meeting,
as a single JSON code block. The bridge persists the raw response to
`notebook_outputs` with `output_type='council_quotes'`; the `<CouncilQuotes>`
component on `BroadcastPage` parses the JSON and renders each quote as a
styled callout. Replaces `council_sentiment` per `DECISIONS.md § D-031`.

**Hard requirements:**
- Officials-only — no private citizens or public commenters
- Ordinal Quote IDs — no timestamps (per D-029, wrapper-safety)
- Confidence marker per quote so the human reviewer (D-028) can prioritize
  spot-checks

## Instructions (sent as the chat query / configure prompt)

Extract 5–8 verbatim quotes from this council meeting from speakers acting
ONLY in OFFICIAL CAPACITY. The output of these quotes will appear on a
public-facing official civic broadcast page, so the speaker classification
rule below is a hard requirement.

ACCEPTABLE SPEAKERS:
- Mayor, Vice Mayor, and any Councilmember (elected officials)
- City staff presenting in their role (planning director, finance director,
  city manager, city attorney, city clerk, department heads, etc.)
- Outside experts or consultants invited to present officially, where their
  presenting role is identified in the agenda or by the council
- Council attorney or clerk when speaking substantively on agenda items

DO NOT include quotes from:
- Private citizens speaking during "Call to the Public", public comment, or
  public hearing input segments
- Anyone speaking from the audience rather than as a presenter or council member
- Anyone speaking in a personal-opinion capacity rather than an official one

If a speaker's role is ambiguous, EXCLUDE them — only include quotes where
you can confidently identify the speaker as one of the acceptable categories.

Output format — return ONLY a JSON code block, no preamble or explanation:

```json
{
  "quotes": [
    {
      "id": "Quote one",
      "text": "the verbatim words spoken",
      "speaker_name": "Mayor Ken Watkins",
      "speaker_role": "Mayor",
      "topic": "Walapai Foothills annexation",
      "confidence": "verbatim"
    }
  ]
}
```

Per-quote field requirements:
- `id`: ordinal label "Quote one", "Quote two", … ascending in meeting order
- `text`: the verbatim words in plain text (no markdown, no surrounding quotes)
- `speaker_name`: the speaker's name; first-name-only acceptable if that's
  what the source provides (e.g., "Robert", "Sarah")
- `speaker_role`: clear role identifier (e.g., "Mayor", "Vice Mayor",
  "Councilmember", "City Staff", "City Manager", "City Attorney",
  "Financial Services Staff")
- `topic`: the agenda item or decision the quote relates to, brief
- `confidence`: `"verbatim"` if you're confident the words are exact;
  `"paraphrase"` if you've smoothed or condensed at all

Skip purely procedural moments (motions to approve minutes, roll call) unless
directly tied to a substantive decision. If you cannot find 5–8 quotes from
official-capacity speakers, return as many as you can find — quality over
quantity. If you find more than 8 strong candidates, prioritize ones tied to
substantive decisions over reflective or closing remarks.

Return only the JSON code block.

<!-- ZSPAN_MODEL_CONTENT_END -->
