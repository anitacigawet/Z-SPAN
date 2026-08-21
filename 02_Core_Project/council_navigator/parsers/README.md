# Council meeting parsers

This directory contains Z-SPAN's parser implementations for public meeting
calendars and related collection-level sources. Each active parser exports:

```python
def scrape_calendar(url: str) -> list[dict]:
    ...
```

The URL is supplied by the caller. Parsers return the meeting date, title,
time, location, status, document links, video link, public-comment link, and
source meeting identifier when those values are present.

## Where the source URLs live

The independent [National Civics Catalog](https://github.com/anitacigawet/national-civics-catalog)
collects official calendar and meeting-information endpoints for reuse in any
civic project. Z-SPAN is one application built from those kinds of sources.

The two repositories have different jobs:

- Update National Civics Catalog when an official endpoint moves, stops
  working, or needs better provenance.
- Update this directory when the page or feed still exists but its structure
  requires different extraction logic.

There is no automatic synchronization between the repositories. New states
are added deliberately as their sources and parsers are researched.

## Parser safeguards

Active parsers use `polite_http.make_session()` for bounded, paced requests
with TLS verification and a neutral browser user agent. They keep missing
values empty, reject unexpected source changes loudly, and return only the
current calendar month and later unless a parser documents a narrower source
window.

Run the parser-focused checks from this directory with the project's Python
environment before proposing changes.

## License

These parser implementations are part of Z-SPAN and use the repository's
PolyForm Noncommercial License 1.0.0. The government records and third-party
sites they read are not relicensed by Z-SPAN.
