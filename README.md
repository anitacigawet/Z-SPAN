[TEMPORARILY DRAFTED BY AI. WILL BE REWRITTEN BY 8/10/2026]

# Z-SPAN

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

It is not a complete copy of the production system, and it is not intended to
be cloned and launched as another Z-SPAN instance. The useful unit here is
smaller: a navigation idea, a playback boundary, a way of keeping source
material visible, or a design principle that can travel into independent
work.

---

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
6. See [the current snapshot record](docs/snapshots/2026-08-02.md) for the exact
   size and review state of this release.

## 🗂️ What is on the shelf

The published source currently demonstrates six parts of the visitor
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

[FUTURE VISUAL SHOWCASE HERE]

The [repository guide](docs/REPOSITORY_GUIDE.md) links each of these ideas to
the relevant files.

## A note about running the code

You will not find installation, hosting, Docker, or deployment instructions in
this repository. That is deliberate.

The published files are selected from a larger private working system. Some of
their imports, services, application wiring, and runtime configuration are not
included. The source is here to be read and studied; it is not presented as a
standalone application or supported distribution.

## How the repository is organized

- [`docs/`](docs/) explains the project model, reusable patterns, reading
  routes, and dated public snapshots.
- [`code/`](code/) contains the selected visitor-interface reference code,
  organized separately from the private working-project path.
- [`prompts/`](prompts/) contains three reviewed, unchanged prompt examples
  that can be studied or adapted one at a time.
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
