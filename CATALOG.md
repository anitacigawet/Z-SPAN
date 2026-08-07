[TEMPORARILY DRAFTED BY AI. WILL BE REWRITTEN BY 8/10/2026]

# Library catalog

This catalog groups the working parts of the library by the question they can
help you explore. You do not need to read the repository from top to bottom or
adopt the whole project. If you want to understand one idea or carry one part
into your own work, start with the question closest to yours.

## Watch and understand

| If you are thinking about… | Start here |
|---|---|
| Watching the complete project walkthrough | [**Z-SPAN Is Born**](https://www.youtube.com/watch?v=HTpR9jRl314) |
| Understanding the library and its community model | [`README.md`](README.md) |

## Read or adapt the interface

| If you are thinking about… | Start here |
|---|---|
| Seeing how the visitor-facing application fits together | [`App.tsx`](website/client/src/App.tsx) |
| Making local meetings easier to find | [`HomePage.tsx`](website/client/src/pages/HomePage.tsx), then [`ChannelsPage.tsx`](website/client/src/pages/ChannelsPage.tsx) |
| Organizing records around a place | [`CityPage.tsx`](website/client/src/pages/CityPage.tsx) and [`CityLedgerPage.tsx`](website/client/src/pages/CityLedgerPage.tsx) |
| Letting people search by subject | [`SearchPage.tsx`](website/client/src/pages/SearchPage.tsx) |
| Building a browsable meeting guide | [`GuideRoot.tsx`](website/client/src/pages/GuideRoot.tsx) and [`components/guide/`](website/client/src/components/guide/) |
| Supporting several video hosts | [`ZspanPlayer.tsx`](website/client/src/player/ZspanPlayer.tsx) and [`adapters.ts`](website/client/src/player/adapters.ts) |
| Relating timed text to source video | [`KaraokeStrip.tsx`](website/client/src/player/KaraokeStrip.tsx) |
| Explaining an integrity-related result | [`AuditPage.tsx`](website/client/src/pages/AuditPage.tsx) |
| Scanning and checking a visible integrity ribbon | [`WatermarkScanPage.tsx`](website/client/src/pages/WatermarkScanPage.tsx) and [`WatermarkVerifyPage.tsx`](website/client/src/pages/WatermarkVerifyPage.tsx) |

## Work with city sources

| If you are thinking about… | Start here |
|---|---|
| Browsing Arizona parsers by county | [`parsers/Arizona/`](parsers/Arizona/) |
| Comparing a smaller county shelf with a larger one | [`Mohave/`](parsers/Arizona/Mohave/) and [`Maricopa/`](parsers/Arizona/Maricopa/) |
| Understanding a complete RSS-based parser | [`kingman_parser.py`](parsers/Arizona/Mohave/kingman_parser.py) |
| Reading a parser that uses a structured civic-calendar API | [`surprise_parser.py`](parsers/Arizona/Maricopa/surprise_parser.py) |
| Seeing how a county's rosters, calendars, and coverage fit together | [`brain/Arizona/Mohave.json`](brain/Arizona/Mohave.json) |
| Checking the library-wide coverage summary | [`brain/coverage_index.json`](brain/coverage_index.json) |

## Add or review a city

| If you are thinking about… | Start here |
|---|---|
| Adding the place where you live | [`documents/starter-kit/README.md`](documents/starter-kit/README.md) |
| Writing a new city parser | [`_TEMPLATE_parser.py`](documents/starter-kit/_TEMPLATE_parser.py) |
| Recording a council roster and its official sources | [`_TEMPLATE_city_intelligence.json`](documents/starter-kit/_TEMPLATE_city_intelligence.json) and [`_TEMPLATE_endpoint_row.json`](documents/starter-kit/_TEMPLATE_endpoint_row.json) |
| Checking a starter-kit parser before opening a pull request | [`freshness_probe.py`](documents/starter-kit/freshness_probe.py) |
| Reviewing an existing parser for source handling and honest empty results | [`kingman_parser.py`](parsers/Arizona/Mohave/kingman_parser.py) |

## Build for another country

| If you are thinking about… | Start here |
|---|---|
| Starting an independent public-meeting library for another country | [`documents/respawn-kernel/README.md`](documents/respawn-kernel/README.md), then [`documents/respawn-kernel/BOOTSTRAP.md`](documents/respawn-kernel/BOOTSTRAP.md) |

## Shelves still being prepared

The `transcription/` and `documents/` shelves are reserved for follow-on
publication. This catalog will point to individual files once those shelves
are populated.
