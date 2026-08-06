# Working rules for {{PROJECT_NAME}}

This is an independent country library generated from the Z-SPAN Respawn Kernel.

## Mission

Make public meetings in {{COUNTRY_NAME}} easier to find, understand, and verify without editorializing the underlying record.

## Session opening

Read `RESPAWN.md`, `TASKS.md`, `DECISIONS.md`, `country/profile.json`, and `manifest.json` before changing the repository.

## Required boundaries

- Use public records and official public sources.
- Never invent a jurisdiction, governing body, meeting, date, or URL.
- Keep private people out of generated civic profiles and summaries unless their inclusion is necessary to represent an official public record and has passed human review.
- Preserve provenance from every normalized record back to its source.
- Require human approval before publication.
- Keep credentials and executable per-source recipes in `registry/private/`.
- Treat country-condition research as adaptation input, never as permission to refuse the country.
- Preserve unknowns as unknowns.
- Do not claim coverage until an independent audit passes.

## Work loop

```text
pick → research → plan → build → verify → review → commit → update TASKS.md
```

One work item at a time. A candidate collector’s own test is not its independent audit.

## Collection behavior

- Use a neutral, static user agent.
- Pace every host conservatively and coordinate pacing across parallel workers.
- Bound pagination and retry loops.
- Log what was observed, emitted, rejected, and why.
- Empty output must distinguish “no records” from “collector failure.”
- Reject unsafe URL schemes and unapproved cross-origin output URLs.

## Localization

- Keep visible strings in locale files.
- Preserve text direction from the locale contract.
- Require local terminology review and a back-check for consequential civic language.
- Never describe an untranslated English placeholder as a completed translation.

## Git and publication

Use feature branches and pull requests. Do not force-push, rewrite history, publish, deploy, or delete project files without the local maintainer’s explicit approval.
