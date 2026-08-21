<p align="center">
  <img src="repository-assets/banner-doodle.png" alt="Z-SPAN for All. A virtual library for local politics. Maintained by the people, for the people." width="1000">
</p>

> *Scientia potentia est.*
>
> **Knowledge is power.**
>
> — Francis Bacon

---

[**English**](README.md) · [العربية](translations/README.ar.md) · [Español](translations/README.es.md) · [فارسی](translations/README.fa.md) · [Français](translations/README.fr.md) · [हिन्दी](translations/README.hi.md) · [Bahasa Indonesia](translations/README.id.md) · [Filipino](translations/README.fil.md) · [Português (Brasil)](translations/README.pt-BR.md) · [Kiswahili](translations/README.sw.md) · [简体中文](translations/README.zh-CN.md) · [繁體中文](translations/README.zh-TW.md) · [Tiếng Việt](translations/README.vi.md)

## 🤔 What is this?

[Z-SPAN.org](https://zspan.org) is a virtual library for local politics, published in full for anyone. It makes city council meetings easier to find, watch, and understand.

Places become **channels**, meetings become **episodes**, with the original videos, agendas, and minutes intact for direct citations.

The directory of meeting sources lives separately in the [National Civics Catalog](https://github.com/anitacigawet/national-civics-catalog).

The National Civics Catalog maps continuing public endpoints for meeting calendars, agendas, minutes, video archives, APIs, and feeds. Z-SPAN uses those sources, and is just one example of what can be built from them.

## Take a tour

[![Watch "Z-SPAN Is Born" — the complete Z-SPAN project walkthrough](https://i.ytimg.com/vi/HTpR9jRl314/hqdefault.jpg)](https://www.youtube.com/watch?v=HTpR9jRl314)

[**Z-SPAN Is Born**](https://www.youtube.com/watch?v=HTpR9jRl314) walks through the Arizona library from the creator's perspective.
Give it a watch if you want to see the original picture of what Z-SPAN is.

## 🗺️ A national directory, built state by state

Arizona is currently the public proof of concept for Z-SPAN's processing capacity.

The channel directory also gives every state and territory its initial starting shape, organized around the relevant county, Tribal, regional, and local public bodies.

![Green status icon](repository-assets/status-green.svg) Green shelves indicate at least one published Z-SPAN meeting.

![Amber status icon](repository-assets/status-amber.svg) Amber shelves represent an honest work in progress.

## 📚 Why this library exists

The goal is simple: to have one useful interface that lets someone browse meetings across different places, despite the completely decentralized mix of video providers, website structures, calendar systems, and document formats those places use.

The library remains easy for the average person to read without requiring the visitor to understand the complexity behind the machine.

The reason for publishing is so that others can inspect, run, question, and audit the library in any way they desire.

## 👋 Who is this library for?

For everyone! Whether you are a student, an activist, a journalist, a researcher, a designer, a developer, or someone simply curious about the organization of local public information.

This repository contains the library itself: the website, the API, the parsers, the end-to-end processing pipeline, and even the local CLI client.

## 🗂️ How this repository is organized

- [`council_navigator`](02_Core_Project/council_navigator/) — the website, public API, local meeting cache, and public channel directory.
- [`parsers`](02_Core_Project/council_navigator/parsers/) — the source-specific calendar parsers that turn catalog endpoints into a common meeting shape.
- [`zspan_pipeline`](02_Core_Project/zspan_pipeline/) — the processing queue that turns a meeting recording into grounded, reviewable material.
- [`zspan_cli`](02_Core_Project/zspan_cli/) — the local client for using Z-SPAN from a person's own computer and workspace.
- [`prompts`](02_Core_Project/prompts/) — the published synthesis contracts used by the processing path.

## What this project commits to

These are constraints the project holds itself to, not aspirations:

- **No editorializing for public officials.** Their words are mirrored verbatim and attributed directly to the source. The interpretation or judgment of those words is completely up to the viewer.
- **Reading is never gated.** We will never require a subscription or registration—or put up a paywall—to read the published content.
- **No engagement optimization.** No infinite feeds, recommendation algorithms, or outrage mechanics. The library is for local meetings and the content of those meetings. That's all.

## 🏛️ Founding

Z-SPAN was founded in Arizona and is maintained by [@anitacigawet](https://github.com/anitacigawet).

---

## The Z-SPAN Trinity

![The Z-SPAN Trinity: the internet carries it, civic records ground it, and people keep it alive](repository-assets/zspan-trinity.svg)

---

> The CIA, the NSA, and even the Pentagon are bounded by the finite tenure of the humans who staff them.
>
> **Z-SPAN is not.**
>
> Z-SPAN is powered by the people, for the people, and thus requires full transparency.
>
> — [@anitacigawet](https://github.com/anitacigawet)

---

## Contact

Project hosted at [zspan.org](https://zspan.org). Questions and reproducible bug reports are welcome through this repository's [issue tracker](https://github.com/anitacigawet/Z-SPAN/issues).

## ⚖️ License

The published code is available under the [PolyForm Noncommercial License 1.0.0](LICENSE). It may be studied, adapted, shared, and reused for noncommercial purposes under the license terms. That includes personal study, hobby projects, education, public research, charitable work, and government use.

Commercial use is not granted by this license. The required notice and Z-SPAN name boundary are recorded in the [NOTICE](NOTICE).
