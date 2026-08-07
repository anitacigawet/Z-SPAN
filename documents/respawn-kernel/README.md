# Respawn Kernel

Respawn Kernel is a country-neutral starting point for building a local public-meeting library. It carries the reusable parts of Z-SPAN’s method without assuming that another country is organized into American states, counties, and cities.

Give the bootstrap a country. It researches that country’s real administrative structure, languages, public sources, and operating conditions; creates a locally named repository; and leaves a human or AI-assisted maintainer with a verified path for expanding it jurisdiction by jurisdiction.

Z-SPAN — Arizona remains the reference implementation. Projects created from this kernel are independent peers with their own names, maintainers, infrastructure, and decisions.

## Begin with the walkthrough

[![Watch “Z-SPAN Is Born” — the complete Z-SPAN project walkthrough](https://i.ytimg.com/vi/HTpR9jRl314/hqdefault.jpg)](https://www.youtube.com/watch?v=HTpR9jRl314)

[**Z-SPAN Is Born**](https://www.youtube.com/watch?v=HTpR9jRl314) walks through the founding library and how its parts fit together. Watch it for the complete project picture; this kernel is the public path for adapting that model to another country without copying Arizona’s administrative shape.

## Power to the people

> The CIA, the NSA, and even the Pentagon are bounded by the finite tenure of the humans who staff them.
>
> **Z-Span is not.**
>
> Z-Span is powered by the people, for the people, and thus requires full community involvement and transparency.
>
> If you would like to operate this library for your own country, here is how.
>
> — Z-SPAN operator

**The Z-SPAN Trinity is simple: the internet carries it, civic records ground it, and people keep it alive.**

![The Z-SPAN Trinity: the internet carries it, civic records ground it, and people keep it alive](../repository-assets/zspan-trinity.svg)

The kernel is the “here is how.” It publishes the reusable library structure without creating a central authority over the projects that grow from it. A country project belongs to the people who operate it, contribute to it, verify it, and decide what it publishes.

Any country, no exceptions. From the United States of America, all the way to the People's Republic of China. The bootstrap begins with the country’s real structure, languages, and available public sources. It records what can and cannot be verified, then gives the local project a path forward without telling its community what the library must become.

## What travels

- A recursive jurisdiction model rather than a fixed `state → county → city` hierarchy
- Public-source provenance and audit trails
- Neutral presentation and private-person protections
- Human approval before publication
- Localized interface contracts, including right-to-left support
- Separated public scaffolding and locally sealed collection recipes
- A repeatable `research → build → verify → expand` loop

## What stays local

- The project’s name and visual identity
- Administrative and governing-body terminology
- Languages, scripts, calendars, dates, and time zones
- Source systems and collection adapters
- Hosting and publication choices
- The private source registry and credentials
- Every decision about how the project operates in its own setting

Country conditions are researched so the implementation can adapt. They do not decide whether a country’s residents deserve the project, and they never cause the bootstrap to refuse a country.

## Repository layout

```text
respawn-kernel/
├── BOOTSTRAP.md               AI-and-maintainer build sequence
├── contracts/                 Country-neutral JSON contracts
├── reference_adapters/        Bridges from existing deployments
├── template/                  Minimal independent-country repository
└── tools/                     Seed creation and conformance checks
```

## Create a country seed

```bash
python3 respawn-kernel/tools/create_seed.py \
  --country "Example Country" \
  --code XX \
  --project-name "Example Civic Library" \
  --primary-locale en \
  --output /path/to/example-civic-library
```

The generator refuses to overwrite an existing path. It carries the contracts, validator, and multilingual reference-site builder into the new repository, so the country project does not depend on a hidden local Z-SPAN checkout. After creation:

```bash
cd /path/to/example-civic-library
python3 tools/validate_seed.py .
python3 tools/build_site.py . --output dist
```

The generated repository begins honestly: the country profile is marked `research_pending`, coverage is empty, publication requires a human decision, and no endpoints or public records are invented. The first site build is a visibly marked, `noindex` preview. A public artifact requires `--publication-approved` and includes only records connected through confirmed jurisdictions, active governing bodies, and verified sources.

## Contract boundary

The kernel’s canonical data path is:

```text
country profile
  → jurisdiction graph
    → governing bodies
      → meetings and source artifacts
```

Names such as “province,” “prefecture,” “municipality,” or “commune” are country data, not application schema. A country can have a mixed or asymmetric hierarchy, and every visible term can be translated independently.

Meeting dates and times are also represented without pretending every source has the same precision. A source may provide a date only, a date and minute, or a date and second; the contract preserves exactly what was observed.

## Status

This kernel establishes the country-neutral boundary, a conformance-tested seed, an Arizona reference adapter, and a standalone multilingual static library. It does not claim that the full Z-SPAN application has already been generalized. The Arizona application still contains American assumptions; country projects can use the independent reference library now while richer shared features move behind country-neutral adapters over time.
