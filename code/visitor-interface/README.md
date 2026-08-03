[TEMPORARILY DRAFTED BY AI. WILL BE REWRITTEN BY 8/4/2026]

# Visitor-interface reference code

This shelf contains selected source from the public-facing Z-SPAN interface.
It is organized for reading, comparison, and modular inspiration.

The files are not presented as a complete application. The private project
contains application wiring, services, dependencies, and configuration that
are intentionally absent here. An unresolved import is therefore a boundary,
not an invitation to guess what the unpublished file contains.

## Browse by experience

- [`src/pages/`](src/pages/) contains the opening, place, search, guide,
  ledger, audit, scan, and verification views.
- [`src/components/guide/`](src/components/guide/) contains the cards, map,
  inline playback, expanded playback, and visual field used by the guide.
- [`src/player/`](src/player/) contains the shared player boundary,
  host-specific adapters, YouTube loader, and timed-word strip.
- [`src/index.css`](src/index.css), [`src/App.css`](src/App.css), and
  [`src/pages/guide.css`](src/pages/guide.css) contain the selected visual
  foundations.

For a path based on the question a visitor is asking, use the
[`CATALOG.md`](../../CATALOG.md). For a file-by-file reading order, use the
[`repository guide`](../../docs/REPOSITORY_GUIDE.md).
