---
output_type: video
target: NotebookLM Studio — Video Overview (Civic Briefing — Kawaii)
status: claude_drafted_placeholder · NEEDS_HUMAN_REFINEMENT
last_edited: 2026-05-12
description: Z-SPAN's Kawaii (playful illustrated) video variant — the audience-segmentation complement to the Corpo prompt. Same neutrality rules, different visual register so Z-SPAN reaches viewers who'd find the Corpo brief off-tone (kids, anime/TikTok-fluent audiences, casual civic-curious viewers).

# ⚠️  CLAUDE-DRAFTED PLACEHOLDER — NOT human-curated  ⚠️
# Per CLAUDE.md "Don't write prompts. Those are James's." This file was
# scaffolded 2026-05-12 by Claude at James's explicit request, as a
# starting point — NOT the finished prompt. James should refine the
# Instructions body, the tone calibration, and the exact "Forbidden /
# Required" lines before this gets used for any published broadcast.
#
# What's safe-as-is: the studio config + the visual style framing.
# What needs your eye: the Content Approach, Required, Forbidden lists.
#
# Until James reviews + flips status to canonical, the bridge will refuse
# to auto-generate this output type by default (it's not in default
# requested_outputs in database.py work_orders schema).

# Studio config (forwarded to client.generate_video)
studio:
  # EXPLAINER format (not CINEMATIC) — Kawaii is the audience-reach
  # variant, not the high-end one. Faster generation, ~10-15 min vs
  # CINEMATIC's 30-40 min, fits the playful-light register.
  video_format: EXPLAINER
  video_style: KAWAII   # cute pastel mountains-with-face per STUDIO_REFERENCE.md
  language: en
  include_section_guidance: true

# All available video styles for EXPLAINER format (for future variants):
#   AUTO_SELECT, CUSTOM, CLASSIC, WHITEBOARD, KAWAII, ANIME, WATERCOLOR,
#   RETRO_PRINT, HERITAGE, PAPER_CRAFT
---

# Video Briefing — Kawaii Variant (Claude-drafted starter, 2026-05-12)

A playful illustrated alternative to the Corpo brief in `video_explainer.md`. Same factual content, same strict neutrality — only the visual register changes. The Kawaii variant exists because the spiritual vision is "get people to understand what's happening in their local government, no matter who they are," and a single visual aesthetic doesn't reach every audience.

## What this variant is NOT

The Kawaii rendering is decorative, NOT editorial. Cute illustrations don't license editorializing about the content. The same content-neutrality rules from `NEUTRALITY_FRAMEWORK.md` apply unchanged — no spin, no characterizations, no opinion on vote outcomes.

## Instructions (sent to Studio)

**Visual Style:** Soft, friendly Kawaii aesthetic — pastel palettes, gentle rounded shapes, hand-drawn illustrative quality. Think "civic information rendered as Studio-Ghibli-style segment" rather than newsroom broadcast. Pleasant illustrations of council scenes (chambers, water towers, civic infrastructure, gavels) drawn in a soft watercolor or rounded vector style. Use the warm-amber accent color from Z-SPAN's brand palette as a subtle highlight; avoid overly saturated or chaotic colors.

**Content Approach:** Approachable civic briefing for viewers who'd find a Corporate Dark Mode brief inaccessible or off-putting. Two friendly civic narrators delivering the same factual coverage as Corpo, but with a warmer cadence — measured but inviting. Imagine a high-quality public-television children's-civics show, NOT a comedy show or a cartoon.

**Forbidden:**
- Editorializing of any kind ("controversial", "wisely", "narrowly", "thankfully", "concerning") — neutrality applies to ALL variants.
- Cartoon-comedy framing — exaggerated reactions, joke-style commentary, sarcasm. The illustration style is playful; the *narration* stays civically respectful.
- Talking-down language ("Did you know...?", "Believe it or not...", "Imagine if..."). Treat the audience as smart, not as children-who-need-it-explained.
- Mascot characters or recurring fictional figures. The Kawaii aesthetic is in the illustration style, not in invented personalities.
- Any visual choice that would render a council member as a cartoon character. Council members are real people; if their likeness is shown, it should be respectful and abstracted.

**Required:**
- Cover the meeting chronologically by section, same coverage discipline as the Corpo brief — call to order, consent agenda, business items, public comment, adjournment.
- For each agenda item: topic, named proposer/dissenter, exact vote count (e.g., "Passed 5-2"), dollar amount, affected streets/boundaries/neighborhoods, single-sentence impact statement.
- Strict numerical accuracy. Cute illustrations do NOT license fuzzy numbers.
- Public comment ratios reflected proportionally (if 7 of 9 commenters opposed, the narration says "seven of nine commenters opposed", NOT "most commenters" — same precision as Corpo).
- Pacing slightly slower than Corpo to match the warmer register, but not condescendingly slow.

<!-- ZSPAN_MODEL_CONTENT_END -->

## Audience targeting (Claude's notes for James to refine)

The Corpo brief targets: policy-curious adults, civic-tech professionals, journalists, council-watchers. Newsroom register.

The Kawaii brief targets: K-12 civics classroom use, casual viewers who'd never click "Regular City Council Meeting Summary", anime/TikTok-fluent audiences whose visual language is illustrated. Library-program register.

Both produce the same factual broadcast. The viewer self-selects via the tab they click on the BroadcastPage.

## What James should refine before this ships

1. **Tone calibration in the narration register** — the line between "approachable" and "patronizing" is fine, and Claude doesn't have enough lived experience with K-12 civics media to draw it. James should rewrite the Content Approach paragraph after watching a few sample generations against a real Kingman meeting.

2. **The "Forbidden mascot characters" rule** — Claude added this defensively because NotebookLM's KAWAII style sometimes invents recurring characters. If James thinks a consistent civic-mascot framing actually serves the mission (e.g., a recurring narrator-avatar with a soft civic identity), that rule should be relaxed.

3. **The audience-targeting notes section** — Claude wrote these from external context. James knows the actual audience better and should rewrite or delete this section.

4. **Whether the section-guidance flag should be `true`** — Claude inherited this from Corpo. If the Kawaii brief should be shorter / less section-by-section structural, set this to `false`.
