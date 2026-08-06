# Bootstrap a country library

This document is for an AI coding agent working with a human maintainer. Read it completely before changing the generated repository.

## First question

Ask:

> What country are we building this public-meeting library for?

The answer starts research; it never determines whether the project is allowed to exist.

Any country, no exceptions. From the United States of America, all the way to China. Treat every country name as a request to research and build, not as a political classification or a reason to refuse the repository.

## Non-negotiable principles

1. **No country veto.** Research local conditions to adapt the project, never to deny a country a repository.
2. **No invented structure.** Discover the country’s real administrative hierarchy and governing bodies from cited sources.
3. **No false coverage.** A jurisdiction is covered only after its source path and output have passed independent verification.
4. **Public records only.** Do not build profiles of private people or convert public participation into a people-search system.
5. **Human publication authority.** Automation may research, collect, translate, transcribe, and prepare material. A human decides what becomes public.
6. **Local ownership.** The country project has its own name, maintainers, infrastructure, decision log, and sealed recipe registry.
7. **Transparent uncertainty.** Unknown or disputed facts remain marked unknown or disputed. The build continues around uncertainty without silently converting it into fact.
8. **Community responsibility.** The kernel supplies a library and a transparent method. The people who operate each independent project decide how it is used and remain responsible for its claims and publication choices.

## Stage 0 — create the seed

Run `tools/create_seed.py` with the country name, country code, local project name, and primary language tag. Commit the untouched seed as the repository’s reproducible starting point.

## Stage 1 — country research

Replace `research_pending` fields in `country/profile.json` using multiple cited sources. Research:

- Official and widely used languages, scripts, and text direction
- Administrative levels and any mixed or asymmetric branches
- Which levels hold public meetings or publish deliberative records
- Governing-body terminology in each relevant language
- Official calendars, document portals, video archives, and common vendors
- Public-record and public-meeting rules as documented facts
- Source-access, hosting, censorship, identity-exposure, and publication constraints
- Adaptations that reduce needless exposure without weakening the public record

The operating-context section is descriptive. It must not contain an `approved`, `safe`, `unsafe`, `go`, or `no_go` field. It exists to inform architecture and maintainer judgment, not to grant permission.

Every consequential claim needs a provenance entry with a title, publisher, URL, and access date. If sources disagree, preserve the disagreement in `notes`.

## Stage 2 — jurisdiction graph

Define the country’s level vocabulary in `country/profile.json`, then enumerate real jurisdictions in `data/jurisdictions.json`.

- Use stable country-scoped IDs.
- Link each child through `parent_id`; do not infer ancestry from names.
- Permit multiple roots or skipped levels when the country requires them.
- Store translated names by BCP 47 language tag.
- Keep statistical places separate from jurisdictions with governing authority.
- Cite the official or best-available source for every node.
- Run the validator to catch missing parents, cycles, unknown levels, and untranslated primary names.

## Stage 3 — governing bodies and sources

Add bodies to `data/governing-bodies.json` and source definitions to `data/sources.json`.

Each source definition describes what an adapter may collect, where it comes from, which jurisdiction or body it belongs to, and whether its private recipe belongs in the sealed registry. Public source URLs may be cited; credentials, discovery notes that create bulk-acquisition risk, and executable per-site recipes stay under local custody.

Do not assume that a “city council” exists. Model the actual deliberative body and its local name.

## Stage 4 — one vertical slice

Before country-wide fan-out, complete one representative jurisdiction end to end:

1. Collect a meeting from an official public source.
2. Preserve the source URL and retrieval provenance.
3. Normalize it into the meeting contract.
4. Render it in the primary locale.
5. Exercise private-person and neutrality checks.
6. Require a human publication decision.
7. Independently compare the result with the source.

The slice becomes the country project’s reference example. Do not generalize a vendor assumption until a second independent signal supports it.

Preserve the source’s actual time precision. A known meeting date with no published time is a date-precision record, not an unknown meeting and not an invitation to insert a customary meeting time.

## Stage 5 — recursive expansion

Expand by the country’s actual jurisdiction graph, not by a hardcoded state loop:

```text
research candidate
  → build candidate adapter
    → self-verify
      → independent audit
        → human-controlled merge
```

Automation may fan out across jurisdictions only when requests to the same host remain globally paced. A separate process does not create permission to burst the same public server.

Every candidate ends in one of these states:

- `research_pending`
- `adapter_candidate`
- `awaiting_independent_audit`
- `verified`
- `source_unavailable`
- `source_changed`

An empty result is valid only when the audit trail distinguishes “the source contains no meetings” from “the collector failed to understand the source.”

## Stage 6 — localization

English is a source locale for the starter, not a required public language. Add the country project’s primary locale before presenting the repository as locally usable.

- Keep visible strings out of application code.
- Preserve placeholders and source links during translation.
- Record translator and review status without exposing personal details.
- Test right-to-left layout when any supported locale declares `direction: rtl`.
- Use a second-language back-check for consequential civic terminology.
- Let local terminology override literal translations.

## Stage 7 — recurring verification

Run `tools/validate_seed.py` whenever country data changes. The country project should add its own live-source tests, locale completeness checks, and independent audit pass before claiming new coverage.

Build `tools/build_site.py . --output dist` for a local no-index preview. A human may build with `--publication-approved` only after reviewing the repository’s claims and translations. The public build filters the visible library through confirmed jurisdictions, active governing bodies, and verified sources; research candidates remain on disk without becoming coverage claims.

Periodically compare the project with the upstream kernel. Adopt improvements deliberately and record the decision; country projects are peers, not automatically synchronized replicas.

## Completion condition

A country seed is ready for a local maintainer when:

- Its hierarchy and operating context are researched and cited.
- The primary locale is complete.
- At least one jurisdiction works end to end.
- Expansion candidates are enumerated without being advertised as covered.
- Private recipes and credentials are separated from the public tree.
- Human publication authority is enforced.
- The conformance validator and the country’s own tests pass.

The repository can then remain in a cold, maintainable state: one person, assisted by automation, can continue expanding it without reconstructing the project’s architecture.
