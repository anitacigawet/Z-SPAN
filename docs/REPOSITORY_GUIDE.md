[TEMPORARILY DRAFTED BY AI. WILL BE REWRITTEN BY 8/4/2026]

# A guide to the published source

The public tree groups selected visitor-interface files under a readable code
shelf. It does not contain the full application around them.

That distinction matters while reading: an import that cannot be resolved from
this repository may belong to the unpublished application shell. The file can
still show how an approved public surface is composed, but the repository does
not claim that the component can be built in isolation.

## Follow a visitor journey

For the basic path from arrival to a local meeting, read:

1. [`HomePage.tsx`](../code/visitor-interface/src/pages/HomePage.tsx)
   for the opening choices and project-wide entry points.
2. [`ChannelsPage.tsx`](../code/visitor-interface/src/pages/ChannelsPage.tsx)
   for place-first navigation and grouped meeting presentation.
3. [`CityPage.tsx`](../code/visitor-interface/src/pages/CityPage.tsx)
   for a city's meetings, official documents, video, and public contact
   information.
4. [`SearchPage.tsx`](../code/visitor-interface/src/pages/SearchPage.tsx)
   for the subject-first route into the same kind of records.
5. [`CityLedgerPage.tsx`](../code/visitor-interface/src/pages/CityLedgerPage.tsx)
   for a longer-running city record presented separately from ordinary search.

## Follow the guide experience

Start with
[`GuideRoot.tsx`](../code/visitor-interface/src/pages/GuideRoot.tsx).
It coordinates the guide's main modes.

Then read the components in
[`components/guide/`](../code/visitor-interface/src/components/guide/):

- `GuideCard.tsx` presents one item in the guide.
- `AggregateMap.tsx` presents available items geographically.
- `InlinePlayer.tsx` keeps playback inside the guide.
- `CinematicTakeover.tsx` expands the selected item.
- `Starfield.tsx` provides the guide's visual field.

The guide-specific visual system is in
[`guide.css`](../code/visitor-interface/src/pages/guide.css).

## Follow the playback boundary

The playback files are small enough to read as a sequence:

1. [`ZspanPlayer.tsx`](../code/visitor-interface/src/player/ZspanPlayer.tsx)
   defines the React-facing player and the controls the rest of the interface
   expects.
2. [`adapters.ts`](../code/visitor-interface/src/player/adapters.ts)
   contains the host-specific playback adapters behind that common shape.
3. [`youtubeApi.ts`](../code/visitor-interface/src/player/youtubeApi.ts)
   loads and types the YouTube player boundary.
4. [`KaraokeStrip.tsx`](../code/visitor-interface/src/player/KaraokeStrip.tsx)
   relates timed words to the active playback position.

This is the most self-contained modular pattern in the published source.

## Follow the integrity-facing views

These files show how integrity-related results are presented to a visitor:

- [`AuditPage.tsx`](../code/visitor-interface/src/pages/AuditPage.tsx)
  introduces the available inspection paths.
- [`WatermarkScanPage.tsx`](../code/visitor-interface/src/pages/WatermarkScanPage.tsx)
  manages the camera-scanning experience and its visible states.
- [`WatermarkVerifyPage.tsx`](../code/visitor-interface/src/pages/WatermarkVerifyPage.tsx)
  manages file-based verification and result presentation.

The API implementations behind these pages are intentionally outside the
public tree.

## Follow the visual foundations

- [`index.css`](../code/visitor-interface/src/index.css)
  contains the main design tokens and shared visual rules visible in this
  snapshot.
- [`App.css`](../code/visitor-interface/src/App.css)
  contains the smaller application-level styling layer.
- [`guide.css`](../code/visitor-interface/src/pages/guide.css)
  contains the guide's separate cinematic presentation.

## What not to look for here

There is no complete route map, package manifest, application entry point,
server, database, scraper collection, production configuration, or deployment
recipe in this public repository. Their absence is part of the release design,
not an invitation to infer or reconstruct them.

For the plain-language boundary, return to
[`PUBLICATION_SCOPE.md`](../PUBLICATION_SCOPE.md).
