# Z-SPAN: Curated NotebookLM Prompts

This folder holds the canonical, version-controlled prompts that Z-SPAN's `notebooklm_bridge` sends to NotebookLM Studio for each meeting.

## Conventions

Each prompt file uses YAML front-matter:

```yaml
---
output_type: text | audio | video | image
target: NotebookLM Studio — [...]
status: canonical | PLACEHOLDER
last_edited: YYYY-MM-DD
description: one-line summary
---
```

Filename = output type. The bridge code reads these at runtime — edit prompt, restart bridge, get new output.

## Current Status

| File | Status |
|------|--------|
| [`video_explainer.md`](./video_explainer.md) | ✅ Canonical (provided by James) |
| [`newsletter.md`](./newsletter.md) | 🔧 Placeholder — needs canonical from Gemini transcript |
| [`audio_overview.md`](./audio_overview.md) | 🔧 Placeholder — needs canonical from Gemini transcript |
| [`infographic.md`](./infographic.md) | 🔧 Placeholder — needs canonical from Gemini transcript |

## Note for James

The placeholder files contain visible TODO blocks. When you're ready, pull the refined prompts from the Gemini transcript and replace the placeholder content. The bridge code is designed to read whatever's in these files at runtime — no code changes needed.

## Important

**Do not generate prompts via AI.** All prompts in this folder are hand-curated by the project lead. The neutrality, tone, and specific output characteristics of Z-SPAN depend on these being deliberate human choices.
