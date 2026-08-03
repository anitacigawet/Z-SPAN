[TEMPORARILY DRAFTED BY AI. WILL BE REWRITTEN BY 8/4/2026]

# Patterns worth carrying elsewhere

This is not a blueprint for recreating Z-SPAN. It is a collection of patterns
visible in the published interface that can help someone think through an
independent civic-information project.

## Begin with the human question

The underlying system may contain routes, identifiers, statuses, and several
types of record. The interface begins somewhere simpler.

| A person is asking | The published surface to study |
|---|---|
| What is happening near me? | [`ChannelsPage.tsx`](../code/visitor-interface/src/pages/ChannelsPage.tsx) |
| Can I find a meeting about this subject? | [`SearchPage.tsx`](../code/visitor-interface/src/pages/SearchPage.tsx) |
| What records belong to this place? | [`CityPage.tsx`](../code/visitor-interface/src/pages/CityPage.tsx) |
| What can I watch or explore? | [`GuideRoot.tsx`](../code/visitor-interface/src/pages/GuideRoot.tsx) |
| How can I inspect an integrity claim? | [`AuditPage.tsx`](../code/visitor-interface/src/pages/AuditPage.tsx) |

This is the human-first rule in practical form: the database may shape the
implementation, but it should not become the language a visitor has to learn.

## Reveal complexity in layers

A first screen does not need to explain the entire system. It needs to help
someone take the next useful step.

The published pages use progressively deeper views: a place list leads to a
channel, a channel leads to meetings, a meeting opens its records, and
specialized audit views remain available without becoming the entrance for
everyone.

An independent project can use the same principle even with a completely
different visual design: show the next meaningful choice, then reveal detail
when it becomes relevant.

## Keep source material close

Official videos, agendas, and minutes are not decorative citations. They are
the path by which a visitor can leave the project's interpretation and inspect
the public record directly.

The city and search views are useful references for this pattern because
source links stay attached to the meeting rather than being placed on a remote
credits page.

## Give different video hosts one visitor-facing contract

Public meetings are hosted in many places. The visitor should not have to
learn a new set of controls merely because one city uses YouTube and another
uses a municipal archive.

[`ZspanPlayer.tsx`](../code/visitor-interface/src/player/ZspanPlayer.tsx)
defines the visitor-facing player. The files in
[`src/player/`](../code/visitor-interface/src/player/)
separate that experience from host-specific behavior.

The portable principle is straightforward: keep provider differences behind a
small adapter boundary, and let the surrounding interface speak in terms of
play, pause, seek, and source—not vendor internals.

## Make time-based text answer to the recording

[`KaraokeStrip.tsx`](../code/visitor-interface/src/player/KaraokeStrip.tsx)
shows timed words alongside playback. The important pattern is the connection:
text is more useful when a person can relate it to the moment in the source
recording instead of treating it as an isolated quotation.

## Treat missing material honestly

A civic archive will always contain uneven coverage. One city may have video,
another only minutes, and another may have records that have not yet been
processed into the same presentation.

An honest empty state tells the visitor what is absent without inventing
content or exposing an internal status code. The empty-channel work in
[`ChannelsPage.tsx`](../code/visitor-interface/src/pages/ChannelsPage.tsx)
is one example of giving absence a readable shape.

## Put the explanation beside the claim

If an interface says that something was verified, scanned, or matched, the
reader should have an immediate route to understand what happened and what the
result does not mean.

The audit, scan, and verification pages are published as examples of that
visitor-facing boundary. The private mechanisms behind them are not part of
this release, so the source should be studied as presentation logic rather
than as a complete verification implementation.

## A quick human-first test

When adapting any of these patterns, ask:

- Can a reader tell what this screen is for without knowing the API?
- Do the labels sound like phrases a person would naturally use?
- Is the next useful action obvious?
- Can the reader reach the official source without hunting for it?
- Does missing information appear as an honest absence rather than a broken
  promise?

If those answers are clear, the interface is probably serving the person
before the schema.
