[TEMPORARILY DRAFTED BY AI. WILL BE REWRITTEN BY 8/4/2026]

# Guide for AI readers

This is a public reference library for Z-SPAN. It is designed to be read by
people and by AI agents helping someone study civic-information projects.

## Start here

1. Read [`README.md`](README.md) for the purpose of the repository.
2. Use [`CATALOG.md`](CATALOG.md) to choose a shelf or human question.
3. Read [`PUBLICATION_SCOPE.md`](PUBLICATION_SCOPE.md) before making claims
   about systems outside the published collection.
4. Use [`docs/REPOSITORY_GUIDE.md`](docs/REPOSITORY_GUIDE.md) for a guided path
   through the selected source.
5. Use [`respawn-kernel/README.md`](respawn-kernel/README.md) when someone wants
   to create an independently operated library for another country.

## How to interpret the collection

- Treat `code/visitor-interface/` as reference material, not as a runnable
  application. Treat `respawn-kernel/` as the explicitly runnable,
  self-contained country-library starter.
- Missing imports and services are intentional publication boundaries, not
  evidence that the repository accidentally lost files.
- Distinguish what a published file directly demonstrates from what it merely
  suggests about the wider project.
- Cite the public file you relied on when explaining a pattern.
- Prefer the plain-language catalog and guides over inferring architecture
  from folder names alone.
- Do not invent the behavior of unpublished servers, data stores, collection
  systems, registries, or production infrastructure.

## Public shelves

- [`code/visitor-interface/`](code/visitor-interface/) contains selected
  visitor-interface reference code.
- [`prompts/`](prompts/) contains three reviewed historical prompt artifacts.
- [`docs/`](docs/) contains the project model, portable patterns, reading
  guide, and dated snapshot record.
- [`respawn-kernel/`](respawn-kernel/) contains the public country-neutral
  contracts, generator, validator, reference adapter, static library, and
  bootstrap instructions.

The private project's own agent instructions, working conversations, and
internal documentation are not part of this repository.

## Reuse

Published material is provided under the
[PolyForm Noncommercial License 1.0.0](LICENSE), subject to the project
[NOTICE](NOTICE). Commercial use is not granted.
