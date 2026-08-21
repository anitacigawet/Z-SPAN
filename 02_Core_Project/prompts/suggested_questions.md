---
output_type: text
target: NotebookLM — multiple chat.ask queries cached as Q&A pairs
status: canonical (Round 1)
last_edited: 2026-05-05
description: Pre-canned Q&A pairs that power the public-host "suggested questions" chat mode. Each question is run against the meeting's notebook once during processing; the answer is cached and replayed when a citizen clicks the chip on the broadcast page. NO live API calls happen on a citizen's click.

# The list of questions below is canonical. Each ships as a clickable chip
# in the broadcast-page chat panel when chat_mode is set to "suggested".
# Edit this list and the worker will regenerate answers on the next run.
#
# Phrasing notes:
#   - First-person ("What did the council decide…") so the chip text reads
#     naturally to a citizen.
#   - Open-ended on purpose — yes/no answers are weak chips. We want the
#     model to give 2-4 sentences of context.
#   - Five is the sweet spot — enough to feel like a real conversation
#     starter, few enough that the chips fit horizontally on most screens.

questions:
  - "What were the most consequential financial decisions in this meeting?"
  - "What did residents who spoke during public comment care about most?"
  - "Did any items get tabled, postponed, or sent back to staff?"
  - "Was there any visible disagreement among council members? What was it about?"
  - "What's the most important thing for residents to know about what was decided?"
---

# Suggested Questions — Public-Host Chat Mode

<!-- ZSPAN_MODEL_CONTENT_START -->
<!-- ZSPAN_MODEL_CONTENT_END -->

The fetcher runs each question above through `chat.ask` against the meeting's notebook, caches the (question, answer) pairs as a JSON blob in `notebook_outputs.suggested_questions.content`, and the broadcast page renders them as clickable chips in `chat_mode = suggested`. Clicking a chip replays the cached answer with the same visual tempo as a live chat — typing dots, then the bubble — without making any live API call.

This is the pattern Z-SPAN uses when the deployment is **public-host** (anyone can land on the site). Direct chat passthrough (`chat_mode = direct`) is reserved for **dev/local** deployments where the operator knows what they're paying for and accepts the abuse surface.
