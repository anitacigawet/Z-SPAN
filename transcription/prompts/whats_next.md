---
output_type: text
target: NotebookLM — Text Query (Looking-Ahead actions, formatted as a numbered list)
status: canonical (Round 1)
last_edited: 2026-05-05
description: A short numbered list of upcoming actions, deadlines, follow-ups, or items returning at the next meeting. Same visual treatment as Key Decisions (numbered bullets with one bold key fact each), positioned below it on the broadcast page.
---

# What's Next — Looking-Ahead Panel

Sits below Key Decisions on the broadcast page, in the same numbered-bullet style. Where Key Decisions answers "what was decided," this answers "what's coming." Citizen-facing accountability — when the next meeting is, what's on its agenda, what deadlines apply, what got tabled with a return date.

## Instructions (sent as the chat query / configure prompt)

List the 3 to 5 most important upcoming actions, deadlines, or follow-up items mentioned in this city council meeting. Format each as a single self-contained sentence. Output ONLY the list — no headings, no preamble, no closing line, no source citations.

For each item, include in the sentence:
- The action verb (Public hearing scheduled / Returns to council / Comments due / Construction begins / Vote scheduled / Staff to report back).
- The exact date when given.
- Specific street names, project names, or boundaries when relevant.
- A bold key fact via **double asterisks** — typically the date, the project name, or the dollar amount that anchors the item.

Use neutral language — no editorializing. Do not invent dates or details that weren't on the record. If the meeting did not include enough forward-looking content for 3-5 items, output fewer (minimum 1 item is fine; output nothing else if there are zero).

Output format (no other text, exactly this shape, one blank line between items):

1. First upcoming item with **one bold key fact**.

2. Second upcoming item with **one bold key fact**.

3. Third upcoming item with **one bold key fact**.

Maximum 5 items. Do not output anything before or after the numbered list.

<!-- ZSPAN_MODEL_CONTENT_END -->
