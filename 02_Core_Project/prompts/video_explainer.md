---
output_type: video
target: NotebookLM Studio — Video Overview (Civic Briefing — Corpo)
status: canonical (Round 3 — cinematic experiment + dark-mode tighten)
last_edited: 2026-05-11
description: Z-SPAN's Corpo (Corporate Dark Mode) video briefing — a documentary alternative to watching the meeting, NOT a friendly explainer. This is the PRIMARY video variant; the secondary Kawaii variant lives in video_explainer_kawaii.md once authored.

# Studio config (forwarded to client.generate_video)
studio:
  # 2026-05-12: reverted CINEMATIC → EXPLAINER as the default. The Round 3
  # CINEMATIC experiment hit the Pro-tier daily Cinematics quota during the
  # first end-to-end Kingman batch (canary WO #100588). Empty task_id was
  # Google's way of saying "you've used today's Cinematic allotment." CINEMATIC
  # remains supported via the bridge — when the cinematic-on-demand operator
  # workflow ships (queued in TASKS.md), it'll let the operator opt into
  # CINEMATIC per-WO when they want the premium treatment within daily limits.
  # See DECISIONS.md § D-036 for the full investigation.
  video_format: EXPLAINER   # EXPLAINER | BRIEF | CINEMATIC (cinematic = daily-quota gated on Pro)
  video_style: HERITAGE     # Used by EXPLAINER + BRIEF; ignored by CINEMATIC
  language: en
  include_section_guidance: true

# All available video styles for EXPLAINER format (pick by audience):
#   AUTO_SELECT, CUSTOM, CLASSIC, WHITEBOARD, KAWAII, ANIME, WATERCOLOR,
#   RETRO_PRINT, HERITAGE, PAPER_CRAFT
# CINEMATIC format does not use this enum.
# Secondary audience variants (Kawaii etc.) live in their own prompt files
# registered against new output_types in notebooklm_bridge/fetcher.py.
---

# Video Briefing — Documentary Alternative (Round 2)

A complete documentary-style **briefing** alternative to watching the council meeting. Not an explainer, not a tutorial — a direct, dense, authoritative briefing.

**Round 1 issues this prompt is correcting:**
- Tone was "friendly explainer of what these things are" — too tutorial-y
- Explained civic concepts more than reporting what happened
- Felt more like a generic explainer than a legitimate briefing alternative

**Round 2 dials:**
- `video_style: HERITAGE` (was CLASSIC / AUTO_SELECT) — archival/serious archival look
- Instructions completely rewritten to demand briefing tone, prohibit explainer framing

## Instructions (sent to Studio)

**Visual Style:** A strict, 100% dark-mode 'Corpo' aesthetic — sustained throughout every frame, no light-background interludes. Deep blacks (#050505 to #0A0A0A) and charcoal slates (#111-#1A) as primary surfaces. Text and data overlays in high-contrast white or near-white, with a single restrained accent color (a muted civic green or amber) reserved for vote counts, dollar figures, and named officials. Typography: clean modern sans, tight tracking, generous line height — premium-publication restraint, not stock-template. Sleek, beautiful, highly readable. The reference is a premium financial-news network's municipal segment or a high-end civic-dashboard product — **not** an educational explainer, **not** a cartoon, **not** a friendly walkthrough, **never** a bright or pastel frame.

**Content Approach:** This is a civic news briefing, NOT an explainer video. Two professional civic news anchors delivering authoritative, dense, direct coverage of what happened in this meeting. Imagine a financial-news network's nightly municipal segment — measured tone, facts in priority order, high information density per minute.

**Forbidden:**
- Tutorial framing ("Let me break this down", "Here's why this matters for you", "First, let's understand…").
- Conceptual asides explaining what a city council, consent agenda, TPT, or any civic mechanism is. Assume the viewer knows.
- Friendly conversational filler. Measured handoffs between anchors only — no banter.
- Editorialized characterizations ("controversial", "wisely", "narrowly", "thankfully").

**Required:**
- Cover the meeting chronologically by section. Do not skip sections — even procedural items (call to order, invocation, approvals) get a brief beat.
- For each agenda item: topic, named proposer/dissenter, exact vote count (e.g., "Passed 5-2"), dollar amount, affected streets/boundaries/neighborhoods, and a single-sentence impact statement.
- Maximize detail density. Use the full runtime to surface specifics — names, numbers, dates, boundary descriptions — rather than narrative-style scene-setting.
- Strict neutrality: vote counts exact, dissent named with reason, public-comment ratios reflected proportionally.

<!-- ZSPAN_MODEL_CONTENT_END -->
