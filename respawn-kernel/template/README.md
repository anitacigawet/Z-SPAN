# {{PROJECT_NAME}}

{{PROJECT_NAME}} is an independent public-meeting library for {{COUNTRY_NAME}}. It is beginning with the country’s real governing structure and public sources, then expanding one independently verified jurisdiction at a time.

This repository was created from the Z-SPAN Respawn Kernel. Z-SPAN — Arizona is the reference implementation; this project has its own identity, maintainers, infrastructure, and decisions.

The kernel is powered by people who research, verify, maintain, and improve public information together. This project belongs to its own community. The people operating it decide how the library develops and are responsible for what it publishes.

The generated repository carries the kernel repository’s current `LICENSE` and `NOTICE`. Historical descriptions of earlier Z-SPAN licenses do not override those files.

## Current status

The repository is a research-ready seed. It does not claim national coverage yet.

- Country profile: research pending
- Primary language: `{{PRIMARY_LOCALE}}`
- Verified jurisdictions: 0
- Public-source adapters: 0
- Publication: human approval required

## Start here

1. Read `RESPAWN.md` and `AGENTS.md`.
2. Research and cite `country/profile.json`.
3. Model real jurisdictions in `data/jurisdictions.json`.
4. Complete the primary locale under `locales/`.
5. Build and independently verify one governing body end to end.
6. Expand through the jurisdiction graph without advertising unverified coverage.

Run the repository’s own conformance check:

```bash
python3 tools/validate_seed.py .
```

Build a local preview at any point. Preview pages are marked `noindex` and show the human-review notice:

```bash
python3 tools/build_site.py . --output dist
```

After a human approves publication, build the public artifact explicitly:

```bash
python3 tools/build_site.py . --output dist --publication-approved
```

The publication build refuses research-pending seeds, unfinished primary translations, and data that has not completed the confirmed-jurisdiction → active-body → verified-source chain.

## Data boundaries

Public records, source citations, normalized meeting data, and coverage status may be tracked here. Credentials and executable per-source recipes belong in `registry/private/`, which is ignored by Git.

Automation can prepare material, but a human decides what is published.
