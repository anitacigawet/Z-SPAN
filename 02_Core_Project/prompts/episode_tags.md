---
output_type: text
target: NotebookLM — Text Query (Episode Tags)
status: DRAFT (Claude scaffolded — needs James to review/rewrite per the no-AI-prompts rule in CLAUDE.md)
last_edited: 2026-05-06
description: 2-5 short topic tags for the channel sidebar. Each tag has a category from a fixed enum that drives its color in the UI. Like TV-guide genre badges.
---

# Episode Tags — Sidebar Topic Labels

A small set of short topic tags that summarize what this meeting is *about* at a glance — like genre badges on a streaming card. Replaces the prose tagline in the channel sidebar. Category determines color in the UI; the actual tag text is the human-facing content.

## Instructions (sent as the chat query / configure prompt)

Identify 2 to 5 specific topics this meeting covered. For each, output one line in this exact format (no extra text, no markdown, no numbering):

```
TAG: <2-4 word topic> | CATEGORY: <one of the allowed categories>
```

The TAG should be specific and concrete — name the actual project, dollar amount, or domain. Avoid generic words like "discussion" or "meeting." Keep each tag under 4 words.

The CATEGORY must be exactly one of these values (lowercase, single word/underscored):

- `zoning` — land use, rezoning, variances, subdivisions, planning
- `infrastructure` — roads, bridges, public works, civic facilities (NOT water/sewer)
- `utilities` — water, sewer, electric, gas, telecom
- `budget` — appropriations, taxes, fees, audits, contracts, financial reports
- `public_safety` — police, fire, EMS, emergency services
- `environment` — open space, conservation, parks, environmental review, climate
- `community` — events, recreation, libraries, arts, community programs
- `legislation` — ordinances, resolutions, code amendments, ballot measures
- `personnel` — appointments, contracts, staffing, council seats
- `transit` — public transit, airports, rail, parking
- `hearing` — public hearings, formal comment periods, quasi-judicial proceedings
- `miscellaneous` — anything that doesn't fit cleanly above

Output 2-5 lines, each in the exact format above. No header, no intro, no outro, no explanation. Just the tag lines.

Good example output (illustrative, not from a real meeting):

```
TAG: Northgate housing | CATEGORY: zoning
TAG: $25M road bond | CATEGORY: budget
TAG: Hualapai annexation | CATEGORY: zoning
TAG: half-cent sales tax | CATEGORY: legislation
```

Avoid:
- Vague tags like "discussion", "items", "various topics"
- Tags longer than 4 words
- Categories outside the allowed enum
- More than 5 tags or fewer than 2
- Any text outside the TAG/CATEGORY lines

<!-- ZSPAN_MODEL_CONTENT_END -->
