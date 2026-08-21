---
name: verification_batch_prompt_template
status: claude_drafted_placeholder
NEEDS_HUMAN_REFINEMENT: true
description: |
  The batch-level verification prompt for T-013 V2. The human reviewer
  drag-drops up to 10 clips into a Gemini Pro chat and pastes the
  rendered version of this template alongside them. Gemini responds with
  a structured-output block per clip; the reviewer saves Gemini's
  response into `batches/batch_NN_RESPONSE.md`. The structured format
  is parseable by a future round-trip ingestion script that updates
  `member_quotes.verified_status`.

  Field substitutions happen in `build_review_queue.py`:
    {batch_index}      e.g. "01"
    {batch_count}      e.g. 10  (clips in THIS batch)
    {batch_total}      e.g. 2   (total batches in the meeting)
    {city}             e.g. "Kingman"
    {meeting_date}     e.g. "2026-04-21"
    {meeting_title}    e.g. "City Council - Apr 21, 2026"
    {clips_list}       rendered per-clip block; see below
output_type: human_review_batch_prompt
---

# Batch {batch_index} of {batch_total} · {city} · {meeting_date} · {meeting_title}

I'm verifying **{batch_count} clips** that Z-SPAN extracted from this public council meeting recording. I've attached the {batch_count} clips to this chat. Please review them and respond in the structured format below — your response is saved to disk as Z-SPAN's verification record for this batch.

## How to identify each clip

Each clip's filename appears in your attachment list. Match filenames to the per-clip details below. The filename is the stable identifier.

## Output format

For EACH attached clip, respond with EXACTLY this block (one block per clip, in the order listed below):

```
## clip: <filename>
- speaker_attribution: yes | no | uncertain
- speaker_attribution_notes: <one short line, or "ok">
- text_accuracy: yes | mostly | no
- text_differences: <quote any wrong portion of the extracted text + what the speaker actually said, or "none">
- clip_integrity: ok | cuts-mid-word | audio-issue | other
- other_concerns: <one short line, or "none">
```

After the last clip's block, end your response with exactly this single line so I can confirm you covered every clip:

```
## BATCH COMPLETE
```

Do not include preamble before the first block, do not insert extra commentary between blocks. Save the analysis for the structured fields above. If you have something important that doesn't fit, put it in `other_concerns`.

---

## Clips in this batch

{clips_list}

<!-- ZSPAN_MODEL_CONTENT_END -->
