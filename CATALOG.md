[TEMPORARILY DRAFTED BY AI. WILL BE REWRITTEN BY 8/4/2026]

# Library catalog

This catalog groups the published material by the question it can help a
reader explore. You do not need to read the repository from top to bottom or
adopt the whole project. Each shelf can stand on its own.

## Begin with an idea

| If you are thinking about… | Start here |
|---|---|
| Making local meetings easier to find | [`HomePage.tsx`](code/visitor-interface/src/pages/HomePage.tsx), then [`ChannelsPage.tsx`](code/visitor-interface/src/pages/ChannelsPage.tsx) |
| Organizing records around a place | [`CityPage.tsx`](code/visitor-interface/src/pages/CityPage.tsx) and [`CityLedgerPage.tsx`](code/visitor-interface/src/pages/CityLedgerPage.tsx) |
| Letting people search by subject | [`SearchPage.tsx`](code/visitor-interface/src/pages/SearchPage.tsx) |
| Building a browsable meeting guide | [`GuideRoot.tsx`](code/visitor-interface/src/pages/GuideRoot.tsx) and [`components/guide/`](code/visitor-interface/src/components/guide/) |
| Supporting several video hosts | [`ZspanPlayer.tsx`](code/visitor-interface/src/player/ZspanPlayer.tsx) and [`adapters.ts`](code/visitor-interface/src/player/adapters.ts) |
| Relating timed text to source video | [`KaraokeStrip.tsx`](code/visitor-interface/src/player/KaraokeStrip.tsx) |
| Explaining an integrity-related result | [`AuditPage.tsx`](code/visitor-interface/src/pages/AuditPage.tsx), [`WatermarkScanPage.tsx`](code/visitor-interface/src/pages/WatermarkScanPage.tsx), and [`WatermarkVerifyPage.tsx`](code/visitor-interface/src/pages/WatermarkVerifyPage.tsx) |
| Turning a meeting into a short written digest | [`newsletter.md`](prompts/newsletter.md) |
| Showing what happens after a meeting | [`whats_next.md`](prompts/whats_next.md) |
| Describing the overall tone without editorializing | [`council_sentiment.md`](prompts/council_sentiment.md) |

## Code shelf

The selected code lives under [`code/visitor-interface/`](code/visitor-interface/).
Its internal `src/` layout remains intact so relative relationships among
pages, guide components, playback adapters, and styles are still visible.

The code is published for close reading. It is not a complete application and
does not include the private services, application wiring, package manifest,
or configuration needed to run it.

## Prompt shelf

The [`prompts/`](prompts/) shelf contains three canonical prompt artifacts
copied unchanged from the working project. They are small, self-contained
examples that can be studied or adapted separately.

Their NotebookLM labels record the setting in which they were developed. They
do not describe Z-SPAN's current private runtime, and the private prompt
collection around them is not published.

## Ideas and reading guides

- [`docs/PROJECT_MODEL.md`](docs/PROJECT_MODEL.md) explains the project in the
  simplest conceptual terms.
- [`docs/DESIGN_PATTERNS.md`](docs/DESIGN_PATTERNS.md) collects portable design
  ideas visible in the selected interface.
- [`docs/REPOSITORY_GUIDE.md`](docs/REPOSITORY_GUIDE.md) follows several paths
  through the code in a useful reading order.
- [`PUBLICATION_SCOPE.md`](PUBLICATION_SCOPE.md) explains what this collection
  can and cannot establish about the wider project.

## Snapshot record

[`docs/snapshots/2026-08-02.md`](docs/snapshots/2026-08-02.md) records the
origin, size, and review state of the first approved source snapshot. Git
history records the later organization of that source into this library.
