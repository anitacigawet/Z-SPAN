---
name: verification_prompt_template
status: claude_drafted_placeholder
NEEDS_HUMAN_REFINEMENT: true
description: |
  The prompt the human reviewer pastes into Gemini Pro alongside each
  clip when walking through `REVIEW_GUIDE.md`. T-013 / S-001 workflow.
  Field substitutions happen via Python `.format()` in
  `notebooklm_bridge/scripts/build_review_queue.py`:
    - {speaker_name}
    - {topic_tag}
    - {quote_text}
    - {meeting_date}
    - {meeting_title}
    - {city}
output_type: human_review_prompt
---

I'm verifying a quote that Z-SPAN extracted from a public council meeting recording. I've attached a short video clip. Please review the clip and answer the questions below.

**Meeting:** {meeting_title} ({city}, {meeting_date})

**Speaker (as attributed by Z-SPAN):** {speaker_name}

**Topic tag:** {topic_tag}

**Quote text** (as extracted by Z-SPAN — verbatim from the audio with minor disfluencies like "uh" / "um" removed by an automated cleaner):

> {quote_text}

---

Please answer:

1. **Speaker attribution.** Is the person speaking in the attached clip plausibly the named speaker above? (yes / no / I can't tell)

2. **Quote text accuracy.** Does the speech in the clip match the quote text above, allowing for the fact that small filler words like "uh" / "um" / "you know" have been intentionally removed? (yes / no / mostly — minor differences / no — substantially different)

3. **If "mostly" or "no":** what's different? Quote any portion the extracted text gets wrong, and what the speaker actually said.

4. **Clip integrity.** Does the clip start and end at sensible points, or does it cut someone off mid-word? Is the audio clear enough to verify the content?

5. **Any other concerns?** Misattribution to the wrong person, the speaker reading from prepared remarks vs. extemporaneous, public-comment-vs-official-capacity ambiguity, audio quality issues, etc.

Please be specific and concise. Your answer goes into Z-SPAN's verification record for this quote.

<!-- ZSPAN_MODEL_CONTENT_END -->
