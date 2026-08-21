---
output_type: member_attendance
target: NotebookLM chat query — extract roll-call attendance from the meeting transcript
status: claude_drafted_placeholder · NEEDS_HUMAN_REFINEMENT
last_edited: 2026-05-12
description: Extracts each council member's roll-call attendance status (present / absent / remote / excused) for the meeting being processed. Output is a strict JSON object the bridge persists to the `member_attendance` table. Powers the Cast page attendance section (T-007).

# ⚠️  CLAUDE-DRAFTED PLACEHOLDER — NOT human-curated  ⚠️
# Per CLAUDE.md "Don't write prompts. Those are James's." Scaffolded
# 2026-05-12 by Claude at James's explicit request, as a structural
# starting point. James should refine the Instructions body, the
# field-by-field definitions, and the failure-mode handling before
# this gets used to populate the Cast page on any published broadcast.
#
# What's safe-as-is: the JSON schema, the canonical-name preamble glue.
# What needs your eye: the tone, the "what if a member arrived late?"
# rules, the "what counts as 'present' vs 'remote'?" decisions.

# This prompt depends on the city's notebooklm_persona_preamble being
# prepended at runtime (the bridge handles that automatically using
# city_intelligence/<slug>.json). Without that preamble, names will
# drift and the JSON output will mis-attribute attendance.
---

# Roll-call Attendance Extraction (Claude-drafted starter, 2026-05-12)

Extract each council member's attendance for THIS meeting from the source transcript. Output is a strict JSON object — the bridge parses it directly into the `member_attendance` table.

## Instructions (sent to NotebookLM)

You are extracting verbatim factual data from a council-meeting transcript. This is NOT a narrative or summary task — it's a structured-data task where accuracy matters and inference is forbidden.

**The task:** For each member of the council listed in the canonical name list above, report their attendance status at THIS meeting based ONLY on what the transcript explicitly says.

**Status values (use exactly these):**

- `present` — the member is recorded as present at the roll call AND attended the meeting in person.
- `remote` — the member attended via phone, Zoom, or other remote-participation method.
- `absent` — the member is recorded as absent (not present at roll call and did not join later).
- `excused` — the member is recorded as absent BUT explicitly excused (advance notice, family emergency, etc., noted in the minutes or by the mayor/clerk).
- `late` — the member arrived after roll call but joined during the meeting. Note the approximate time of arrival in `notes`.
- `left_early` — the member was present at roll call but departed before adjournment. Note the approximate time of departure in `notes`.
- `unknown` — the transcript does not clearly indicate the member's status. **Use this honestly when uncertain — do NOT guess.**

**Required output: strict JSON, no surrounding prose, no markdown code fence.**

```json
{
  "attendance": [
    {
      "name": "<exact name from the canonical list above>",
      "status": "<one of: present|remote|absent|excused|late|left_early|unknown>",
      "notes": "<optional short note, max 100 chars — e.g., 'arrived at 6:42 PM', 'excused per Mayor's announcement'>"
    },
    ...one row per canonical-list member, in canonical-list order...
  ],
  "extraction_notes": "<optional, max 200 chars — e.g., 'roll call read at 6:30 PM', 'minutes confirmed two members joined late'>"
}
```

**Strict rules:**

- One row per canonical-list member. If the meeting has a guest (non-council) speaker, do NOT include them.
- If the transcript does not explicitly state a member's status, return `"status": "unknown"`. Do not infer from silence.
- Use the canonical name spelling EXACTLY as it appears in the preamble above. Do NOT correct or modernize names.
- `notes` is optional; omit if there's nothing to add. Don't fill it with "Member was present at the meeting" — that's redundant with status.
- If multiple roll calls happened (rare — sometimes after a recess), report the status at the FIRST roll call. Use `notes` to flag if the status changed later.
- Return ONLY the JSON object. No "Here is the attendance:" preamble, no closing thoughts.

<!-- ZSPAN_MODEL_CONTENT_END -->

## Failure modes the bridge handles for you

- If you return malformed JSON, the bridge logs an error and the row goes to `notebook_outputs.error` instead of `member_attendance` — the operator sees a fail pill on the row and can investigate.
- If you return canonical names that aren't on the seeded list, the bridge logs a warning but still ingests them — the operator's [REVIEW] surface will catch the mismatch.
- If you return `"status": "unknown"` for every member, the bridge still saves the output — the Cast page will show all-unknown for this meeting, which is honest signal.

## What James should refine before this ships

1. **The status taxonomy** — Claude proposed 7 values (present / remote / absent / excused / late / left_early / unknown). That's possibly too granular. James should decide whether to collapse `late` and `left_early` into `partial`, or whether all 7 are useful for the Cast page UI.

2. **The "what counts as remote" rule** — different cities have different policies on remote participation. Claude wrote a generic definition; James should adapt for Kingman's specific charter (some councils don't allow remote voting at all, just remote attendance).

3. **The handling of guest speakers** — Claude said "don't include them." If James wants to extract data on regularly-attending non-council figures (e.g., the City Manager, City Attorney), that's a different schema and probably deserves its own output type.

4. **`extraction_notes` usefulness** — Claude added this field thinking the operator might want extraction metadata. Possibly redundant with the worker's log; James can remove if unwanted.

5. **What happens when the transcript is incomplete** — sometimes Granicus uploads only have the first 30 min of a 3-hour meeting. The current prompt doesn't address this; James should decide whether to add a "transcript_completeness" field or handle this purely on the operator-review surface.
