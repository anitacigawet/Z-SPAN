# Civic Meeting Section Guidance

Appended to long-form Z-SPAN output prompts (audio_overview, video_explainer) when their front-matter sets `include_section_guidance: true`. The bridge concatenates this content onto the user's instructions before sending to NotebookLM.

**Why this exists:** city council agendas are remarkably standardized. Different cities use different labels, but the *categories* of items recur almost universally. By telling NotebookLM about this canonical structure, long-form outputs walk listeners/viewers through the meeting chronologically by section — feeling like a guided tour of the actual proceedings rather than a highlight reel.

The text below the marker is what gets sent. Edit it freely to refine the structural guidance.

---

## STRUCTURAL GUIDANCE — sent to Studio

**Walk the listener through the meeting chronologically by section. City council meetings follow a standardized agenda structure. Identify each section as it occurs and present its content in order, even when sections are brief or procedural. Do not skip sections — citizens unable to attend deserve to experience the full arc of the meeting.**

The standard sections, in typical order:

1. **Call to Order, Roll Call, Invocation, Pledge of Allegiance** — Open the broadcast by noting these procedural openings briefly. Name attending council members.
2. **Approval of Minutes / Approval of Agenda** — Note any modifications to the agenda or minutes from the prior meeting.
3. **Presentations, Awards, Recognitions, Proclamations** — These are ceremonial, non-voting items. Cover them as they occurred (e.g., "The council proclaimed April Fair Housing Month and recognized [name] for [reason]"). Tone: respectful, brief, not skipped.
4. **Public Comment — General (citizens speaking on non-agenda items)** — Summarize each speaker's concern and the topic, by name when stated. If the council took no action, say so. Reflect speaker counts proportionally.
5. **Consent Agenda / Routine Approvals** — These are bundled routine items voted on as a batch. List them succinctly with vote count. Examples: liquor permits, routine purchase orders, minor easements.
6. **Public Hearings** — Formal hearings on specific items (zoning changes, budget amendments, etc.). For each: state the matter, summarize staff presentation, summarize public comment proportionally (e.g., "Twelve residents spoke against and four spoke in favor"), then the council action and vote count.
7. **Action Items / New Business / Old Business / Resolutions** — The substantive votes. For each: state the topic, the staff/sponsor recommendation, councilmember discussion (attributed by name), the exact vote count (e.g., "Approved 5-2"), any dissent (named with reason), and the practical impact on residents.
8. **Departmental / Staff / City Manager Reports** — Routine updates from departments. Cover the substance briefly.
9. **Council Member Comments / Future Agenda Items** — Brief mention of items members raised for the future or general remarks.
10. **Adjournment** — Note the meeting's close.

**Cross-cutting requirements:**

- Names matter. Attribute every position, motion, second, and dissent to the named councilmember or speaker.
- Vote counts are sacred. State them exactly, never paraphrase ("Approved 5-2" not "passed with a majority"). Always name dissenters and their stated reason.
- Public comment is summarized proportionally. If 8 spoke for and 2 against, that ratio is preserved — not equal-time false balance.
- Explain civic terminology when first used (consent agenda, infill incentive, CDBG, TPT, executive session, etc.). The listener may be new to local government.
- Use city/region-specific names (street names, project names, neighborhood boundaries) verbatim. They anchor the meeting in place.
- Maintain the chronological arc. The meeting started with X, then Y, then Z. Don't reorder for narrative effect.

**What NOT to do:**

- Do not editorialize. No "in a controversial vote," no "wisely approved," no "narrowly passed." State the count.
- Do not skip the procedural opening (Call to Order, Pledge, etc.) — it sets the tone.
- Do not lump all action items into a single summary paragraph. Each significant decision deserves its own beat.
- Do not invent context not in the source. If something is unclear, say "the council's stated reason was X" or "the source did not record Y."

<!-- ZSPAN_MODEL_CONTENT_END -->
