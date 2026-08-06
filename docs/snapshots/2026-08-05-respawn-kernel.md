# Respawn Kernel public snapshot — 2026-08-05

This snapshot records the first runnable Respawn Kernel published in the
Z-SPAN public library.

## What this release contains

- Seven Draft 2020-12 JSON contracts covering the country profile,
  jurisdictions, governing bodies, public sources, meetings, locale packs,
  and the generated-repository manifest.
- A non-overwriting country-seed generator.
- A deterministic structural validator.
- A bridge from normalized Z-SPAN — Arizona meeting records into the portable
  meeting contract, without carrying the fixed American jurisdiction model.
- A deterministic multilingual static-site builder with right-to-left layout
  support and a no-index preview mode.
- A repository template that carries its own contracts, validator, builder,
  locale source, and private-registry boundary.
- Twenty-two focused kernel tests.

## Publication boundary

The public kernel contains no production credentials, source-specific parser
recipes, private registries, Arizona database, internal governance corpus, or
deployment configuration. Generated projects keep executable per-source
recipes and credentials under their own local custody.

The static-site builder displays only records connected through a confirmed
jurisdiction lineage, an active governing body, and a verified public source.
Preview builds are marked `noindex`. A separate
`--publication-approved` invocation records the local human decision to build
a public artifact.

## Honest maturity statement

The kernel is independently runnable and can create, validate, and render a
country repository. It is not a country-neutral copy of every feature in the
private Arizona application. Z-SPAN — Arizona remains the reference
implementation; richer shared features can move behind the portable contracts
over time.

## Verification at publication

- Respawn Kernel suite: 22 passed.
- Draft 2020-12 schema self-validation: 7 passed.
- Generated seeds with English, non-English left-to-right, and right-to-left
  locale configurations: structurally valid; draft translations remained
  visibly unreviewed; text direction was selected correctly.
- Standalone generated-repository validator and site builder: exercised
  successfully.
- Google Chrome visual check: desktop and narrow-screen layouts rendered
  cleanly.
- Z-SPAN private pipeline tests: 290 passed plus 78 subtests.
- Input-security suite: 573 passed, 4 skipped, 0 failures.
- Frontend and server checks: 28 files and 405 tests passed.
