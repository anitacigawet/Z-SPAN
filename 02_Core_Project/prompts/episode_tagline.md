---
output_type: text
target: NotebookLM — Text Query (Episode Tagline)
status: DRAFT (Claude scaffolded — needs James to review/rewrite per the no-AI-prompts rule in CLAUDE.md) + james_reviewed 2026-07-19 (official-capacity guard)
last_edited: 2026-07-19
addendum_added: 2026-07-19 (official-capacity guard — the tagline is the page's most prominent line and must never headline a private person's words)
description: Single-sentence ~10-word descriptor for the channel sidebar. Acts like a TV-guide subtitle — what this episode is "about" in the smallest possible space.
---

# Episode Tagline — Channel Sidebar Descriptor

A single short line that sits under the meeting title in the channel sidebar (the streaming-network episode list). The user reads dozens of these at a glance to decide which episode to open. Must be punchy, factual, and concrete.

## Instructions (sent as the chat query / configure prompt)

Write a single descriptive line of 8 to 14 words that captures the most concrete, citizen-relevant action of this meeting. Lead with the topic, not "Council discusses" or "Members debate." Name a specific project, dollar amount, or geographic area when one anchors the meeting. Stay neutral and factual — no editorializing adjectives. Output only the line itself, no quotes, no period at the end if possible, no markdown, no labels.

Good examples (style reference, not real meetings):
- New residential permits for Northgate district
- $25M street improvement package for Airway Avenue corridor
- Variance request for downtown Main Street cafe expansion
- Annexation of 4,238-acre Hualapai Mountain Foothills parcel
- Half-cent sales tax proposal advanced to November ballot

Avoid:
- "The Council met to discuss…"
- "Various items were considered…"
- Anything longer than 14 words
- Anything a private individual said (see the guard below)

### Official-capacity guard

The tagline is the most prominent line on the public episode page, so it must never headline a private person's words. Draw it only from official council action — a decision, vote, motion, appropriation, or an item formally before the body — or from city staff presenting in their official role.

Never build the tagline from something said during public comment or "Call to the Public," from an audience member, or from anyone speaking in a personal capacity. Never carry a private individual's assertion, accusation, or allegation into the tagline, and never name a private individual. Role-only attribution does not make it safe — "Business owner alleges competitors skirt insurance rule" is prohibited.

If the only notable thing in the retrieved context is a private person's statement, fall back to the meeting's most concrete official action instead. If a speaker's role is ambiguous, treat them as a private individual and fail closed.
- Trailing periods, quotes, or "Tagline:" labels

<!-- ZSPAN_MODEL_CONTENT_END -->
