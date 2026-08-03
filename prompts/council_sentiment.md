---
output_type: text
target: NotebookLM — Text Query (Council Sentiment, structured for color-coded display)
status: canonical (Round 2 — LLM-chosen tone color)
last_edited: 2026-05-05
description: A 1-2 sentence neutral observation of the dominant theme/tension PLUS an LLM-chosen hex color on a red→yellow→green spectrum that represents the tension level. The color drives the visual accent of the sentiment block on the show page.
---

# Council Sentiment — Color-Coded Show-Page Callout

Two-line structured output: a neutral text observation, plus a hex color that the LLM picks based on how unified vs. tense the meeting was. The frontend uses the color as the accent bar of a small sentiment block on the broadcast page — a citizen can glance at the color and instantly read "harmonious" vs. "contentious" without reading text.

## Instructions (sent as the chat query / configure prompt)

Observe the dominant theme of this council meeting's discussion in 1-2 short sentences, then emit a hex color code that represents the tension level on a red→yellow→green spectrum.

Output **EXACTLY** this format with these exact field labels and nothing else — no preamble, no closing line, no explanation:

TEXT: <your 1-2 sentence neutral observation goes here on one line>
COLOR: #XXXXXX

Color spectrum guide (you pick the most appropriate hex):
- **#22C55E** (green) — unified meeting, members aligned, smooth votes, no visible tension
- **#84CC16** (lime green) — mostly aligned, minor differences expressed but resolved
- **#EAB308** (yellow) — mixed: meaningful debate, some 4-3 / 5-2 split votes, but civil
- **#F59E0B** (amber) — pronounced disagreement on multiple items, multiple split votes
- **#EF4444** (red) — high tension, repeated dissent, items tabled or sent back, visible friction

Pick the SINGLE hex code that best matches the meeting's overall tone. You may use any hex on this spectrum; the five values above are anchors, not the only allowed values.

Hard rules for the TEXT line:
- Do NOT use words like "controversial," "heated," "wisely," "narrowly," "concerning," "praised," "criticized."
- Do NOT name individual councilmembers' personal motivations — only stated reasons on the record.
- Do NOT make predictions about future meetings.
- One single line. No newlines inside the TEXT value. No bullet points, no formatting.
- Maximum 2 sentences, total ~30 words.

Examples of correct format (do NOT copy these literally — they are tone references):

TEXT: Discussion focused on long-term infrastructure costs versus near-term funding constraints. Members differed on the appropriate tax mechanism.
COLOR: #EAB308

TEXT: The session moved through routine approvals and a unanimous vote on the parks budget without visible disagreement.
COLOR: #22C55E

TEXT: Multiple action items were tabled after extended debate; members repeatedly pressed staff on enforcement timelines.
COLOR: #F59E0B

End of instructions. Output only the TEXT and COLOR lines.

<!-- ZSPAN_MODEL_CONTENT_END -->
