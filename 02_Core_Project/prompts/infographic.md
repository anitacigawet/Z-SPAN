---
output_type: image
target: NotebookLM Studio — Infographic (Civic Data Dashboard)
status: canonical (Round 2 — flat dashboard, tan parchment, vote counts mandatory)
last_edited: 2026-05-05
description: A single-glance civic data dashboard. Legal-document tan parchment, no illustrations, mandatory vote counts.

# Studio config (forwarded to client.generate_infographic)
studio:
  orientation: PORTRAIT     # LANDSCAPE | PORTRAIT | SQUARE
                            # NOTE: also restated in instructions as "9:16 vertical"
                            # because some image models misinterpret "portrait".
  detail_level: DETAILED    # CONCISE | STANDARD | DETAILED
  style: PROFESSIONAL       # Round 2: pinned PROFESSIONAL (was AUTO_SELECT picking
                            # SimCity-illustration styles) — see style menu below.
  language: en

# All available infographic styles (pick by audience):
#   AUTO_SELECT, SKETCH_NOTE, PROFESSIONAL, BENTO_GRID, EDITORIAL,
#   INSTRUCTIONAL, BRICKS, CLAY, ANIME, KAWAII, SCIENTIFIC
# For audience variants, copy this file and change the style.
---

# Infographic — Civic Data Dashboard (Round 2)

A single-glance civic data dashboard summarizing the meeting. Designed to look like a **legal brief or premium municipal dashboard** — calm, authoritative, scannable on mobile.

**Round 1 issues this prompt is correcting (per Gemini audit + James review):**
- Came out **landscape (16:9)** despite `orientation: PORTRAIT` — likely the model
  interpreted "portrait-oriented" as "a portrait of a person." Round 2 restates
  it as "strict vertical 9:16 layout for mobile scrolling."
- **Cartoonish/SimCity-style illustration** instead of a flat data dashboard.
  Round 2 forbids vector landscapes / 3D illustrations / cartoons / mascots
  outright and pins style to `PROFESSIONAL`.
- **No vote counts and no named dissenters** — Round 2 makes both mandatory.
- **Editorializing title** ("Shaping the City's Infrastructure and Future") —
  Round 2 mandates a neutral title format.
- **Pure white background** — Round 2 specifies warm tan parchment (legal-brief
  feel) for eye comfort and civic-document aesthetic.

## Instructions (sent to Studio)

Generate a single-glance civic data dashboard summarizing this city council meeting. **NOT a poster, NOT a stylized illustration, NOT a cartoon, NOT a scenic image.** A flat, scannable data dashboard resembling a legal brief or premium municipal dashboard.

**Layout & format:**
- Aspect ratio: strict vertical 9:16 layout designed for mobile scrolling.
- Background: warm light tan parchment (approximately #F5F1E8 — the color of a legal-brief or law-school casebook page). Do NOT use pure white. Do NOT use stylized gradients.
- Color palette: Civic Blue (#1A3A7C) for headings, section dividers, and primary accents; Engagement Orange (#E85C41) sparingly for highlight callouts (max ~10% of color usage); charcoal gray (#2C2C2C) for body text. Use grayscale for non-data structural elements. No other colors.
- Typography: clean sans-serif. Establish hierarchy via size and weight only, not decorative fonts.

**Forbidden visual elements:**
- Vector landscapes, cityscapes, or scenic backgrounds.
- 3D illustrations, claymation, isometric scenes.
- Cartoon characters, mascots, faces, or playful imagery of any kind.
- Decorative icons that aren't directly representing data (basic flat icons for vote, money, location are fine).

**Required content (every item below MUST appear):**
- **Title** at top: neutral format `"[City] City Council · [Meeting Date]"`. Do not use editorializing or promotional titles like "Shaping the Future" or "City of Progress."
- **For each major decision or action item:** topic name, exact vote count (e.g., "Approved 5-2"), names of dissenting council members with their stated reason, dollar amount where applicable, affected boundaries/streets/neighborhoods.
- **Public comment summary:** total speakers and the proportional split (e.g., "Public Comment: 8 in favor, 2 opposed, 4 neutral").
- **Next meeting** date at bottom.

**Tone:** Civic-legitimate. Like a legal brief or premium municipal dashboard. No promotional or PR framing. Neutral throughout.

<!-- ZSPAN_MODEL_CONTENT_END -->
