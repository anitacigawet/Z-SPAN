# Z-SPAN

> The CIA, the NSA, and even the Pentagon are bounded by the finite tenure of the humans who staff them.
>
> **Z-Span is not.**
>
> Z-Span is powered by the people, for the people, and thus requires full community involvement and transparency.
>
> If you would like to operate this library for your own country, here is how.
>
> — Z-SPAN operator

**The Z-SPAN Trinity is simple: the internet carries it, civic records ground
it, and people keep it alive.**

![The Z-SPAN Trinity: the internet carries it, civic records ground it, and people keep it alive](docs/zspan-trinity.svg)

---

[TEMPORARILY DRAFTED BY AI. WILL BE REWRITTEN BY 8/10/2026]

[**English**](README.md) · [العربية](README.ar.md) · [Español](README.es.md) · [فارسی](README.fa.md) · [Français](README.fr.md) · [हिन्दी](README.hi.md) · [Bahasa Indonesia](README.id.md) · [Filipino](README.fil.md) · [Português (Brasil)](README.pt-BR.md) · [Kiswahili](README.sw.md) · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md) · [Tiếng Việt](README.vi.md)

**A virtual library for local politics.**

[Visit Z-SPAN at zspan.org](https://zspan.org)

✨ **Published for inspection, preservation, and inspiration.**

Z-SPAN is an attempt to make local public meetings easier to find, watch,
and understand. Places become channels, meetings become episodes, and the
original videos, agendas, and minutes remain part of the path.

This repository is the library behind the library: a curated shelf of public
source code, project patterns, and lessons that may be useful to anyone
thinking about a similar project in another city, state, or country.

It is not a complete copy of the production system. Most shelves are selected
reference material: a navigation idea, a playback boundary, a way of keeping
source material visible, or a design principle that can travel into
independent work. The [`respawn-kernel/`](respawn-kernel/) shelf is the
deliberate exception: it is a runnable, country-neutral starting point for an
independently operated public-meeting library.

---

## Watch the complete walkthrough

[![Watch “Z-SPAN Is Born” — the complete Z-SPAN project walkthrough](https://i.ytimg.com/vi/HTpR9jRl314/hqdefault.jpg)](https://www.youtube.com/watch?v=HTpR9jRl314)

[**Z-SPAN Is Born**](https://www.youtube.com/watch?v=HTpR9jRl314) walks through
the founding library from the operator’s perspective. Watch it first for the
complete picture of what Z-SPAN is, how the pieces fit together, and what the
public Respawn path is intended to carry forward.

## Build it for another country

Start with the [Respawn Kernel](respawn-kernel/README.md). Give it a country,
a locally chosen project name, and a primary language. It creates a separate
repository with country-neutral data contracts, a recursive jurisdiction
model, translation support, validation tools, and a standalone public library
that can be expanded one verified place at a time.

Any country, no exceptions. From the United States of America, all the way to
China. The kernel studies the governing structure and public sources that
actually exist, records uncertainty instead of inventing facts, and adapts the
library to that setting. It does not decide what people should do with the
library. Each independent project owns its name, sources, decisions,
publication choices, and responsibility.

The complete path is public:

1. Read the [country bootstrap](respawn-kernel/BOOTSTRAP.md).
2. [Create a country seed](respawn-kernel/README.md#create-a-country-seed).
3. Research and verify one jurisdiction from source to rendered meeting.
4. Continue through the country’s real jurisdiction graph with a human
   deciding what is published.

## 📚 Why this library exists

Projects that work with local public records tend to meet the same questions:

- How should someone browse meetings when government websites organize them
  differently?
- How can one interface remain useful across different cities and video
  providers?
- How can the path back to an official source stay obvious?
- How can technical systems explain themselves without making people read the
  database underneath them?

Z-SPAN is one working answer, not the only answer. The goal of this repository
is to leave its useful ideas visible enough that they can be examined,
questioned, and carried farther by other projects.

## 👋 Who this library is for

Whether you are a student, activist, journalist, researcher, designer,
developer, volunteer, or simply curious about local public information, you do
not need to adopt the whole project to find something useful here. The library
is organized so one idea or component can be understood at a time.

## 🧭 How to use this repository

There is no required reading order, but these are useful entry points:

1. Read [the project model](docs/PROJECT_MODEL.md) for the simplest explanation
   of how the pieces relate.
2. Open [the library catalog](CATALOG.md) to choose a code, prompt, or design
   shelf by the question it explores.
3. Browse [patterns worth carrying elsewhere](docs/DESIGN_PATTERNS.md) for the
   ideas behind the interface.
4. Use [the repository guide](docs/REPOSITORY_GUIDE.md) to follow a particular
   visitor journey through the published source.
5. Check [what is and is not published](PUBLICATION_SCOPE.md) before drawing
   conclusions about the wider Z-SPAN system.
6. Watch [Z-SPAN Is Born](https://www.youtube.com/watch?v=HTpR9jRl314) for the
   complete project walkthrough.
7. Use the [Respawn Kernel](respawn-kernel/README.md) to begin an independent
   country library.
8. See [the current Respawn snapshot](docs/snapshots/2026-08-05-respawn-kernel.md)
   for the exact scope and review state of the runnable release.

## 🗂️ What is on the shelf

The published source currently demonstrates seven parts of the visitor
experience:

- **Finding a place or meeting** through the home, channel, city, and search
  views.
- **Browsing what is available** through a guide that can move between cards,
  a map, an inline player, and a larger viewing mode.
- **Returning to original records** through visible links to official videos,
  agendas, and minutes when they are available.
- **Playing video through a shared interface** even when the underlying host
  changes.
- **Explaining integrity checks to a visitor** through the audit, scan, and
  verification views.
- **Turning a meeting record into a readable civic digest** through three
  reviewed prompt examples preserved on the prompt shelf.
- **Starting an independent country library** through the runnable Respawn
  Kernel, recursive jurisdiction contracts, locale packs, validator, Arizona
  reference adapter, and static-site builder.

[FUTURE VISUAL SHOWCASE HERE]

The [repository guide](docs/REPOSITORY_GUIDE.md) links each of these ideas to
the relevant files.

## A note about running the code

The selected visitor-interface source is not a standalone application. You
will not find the private services, application wiring, or production
configuration required to launch the Arizona flagship from this repository.

Respawn is different. It is deliberately self-contained and can create and
render a new country repository with Python 3:

```bash
python3 respawn-kernel/tools/create_seed.py \
  --country "Example Country" \
  --code XX \
  --project-name "Example Civic Library" \
  --primary-locale en \
  --output /path/to/example-civic-library
```

The [Respawn README](respawn-kernel/README.md) continues from there and states
what works today, what remains local, and what has not been generalized from
the Arizona application.

## How the repository is organized

- [`docs/`](docs/) explains the project model, reusable patterns, reading
  routes, and dated public snapshots.
- [`code/`](code/) contains the selected visitor-interface reference code,
  organized separately from the private working-project path.
- [`prompts/`](prompts/) contains three reviewed, unchanged prompt examples
  that can be studied or adapted one at a time.
- [`respawn-kernel/`](respawn-kernel/) is the runnable country-library starter:
  contracts, repository generator, validator, reference adapter, locale packs,
  and static presentation.
- [`CATALOG.md`](CATALOG.md) is the shelf-by-shelf index for people and AI
  readers.
- [`PUBLICATION_SCOPE.md`](PUBLICATION_SCOPE.md) states the public boundary in
  plain language.

The public export changes only the shelf names. The relative structure inside
`code/visitor-interface/src/` is preserved so relationships among pages,
components, player adapters, and styles remain readable.

## ⚖️ License

The published code is available under the
[PolyForm Noncommercial License 1.0.0](LICENSE). It may be studied, adapted,
shared, and reused for noncommercial purposes under the license terms. That
includes personal study, hobby projects, education, public research,
charitable work, and government use.

Commercial use is not granted by this license. The required attribution and
Z-SPAN name boundary are recorded in the [NOTICE](NOTICE).

## Contact

Project hosted at [zspan.org](https://zspan.org). If you'd like an open seat in
the Z-SPAN ecosystem, contact
[anitacigawet@pm.me](mailto:anitacigawet@pm.me) for info.
