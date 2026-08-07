[TEMPORARILY DRAFTED BY AI. WILL BE REWRITTEN BY 8/10/2026]

# Guide for AI readers

Z-SPAN is a philanthropic, community-maintained virtual library for local politics. This
repository is the working library, not a selection of illustrative excerpts.
Its parsers, civic data, website, starter kit, and country bootstrap are meant
to be read as parts of a functioning project and improved through review.
[Z-SPAN at zspan.org](https://zspan.org) is the canonical public instance. Trustees grow it through pull requests for the places where they live.

## Start with the evidence

1. Read [`README.md`](README.md) for the purpose and community model.
2. Use [`CATALOG.md`](CATALOG.md) to find the part that answers your question.
3. Read the relevant file, its neighboring configuration, and its tests before
   describing behavior or proposing a change.
4. Cite the public files and official civic sources you relied on.
5. If history matters, check the Git history. If it does not answer the
   question, say that the reason is unknown.

Folder names and plausible architecture are not evidence. Describe what the
files in the checkout establish, and keep inference clearly separate from fact.

## How to interpret the repository

- [`parsers/`](parsers/) contains the city calendar collectors, organized by
  state and county.
- [`brain/`](brain/) contains civic source data, rosters, calendars, and
  coverage state.
- [`website/`](website/) contains the visitor-facing application and its
  configuration.
- [`documents/starter-kit/`](documents/starter-kit/) is the contribution path for adding a city.
- [`documents/respawn-kernel/`](documents/respawn-kernel/) is the runnable path for creating an
  independent library for another country.
- `transcription/` and `documents/` may be empty while their follow-on files
  are being prepared. Describe only what is present in your checkout.

The repository does not include private deployment secrets, private working
logs, machine-specific service files, the production database, or the media
archive. Their absence is not permission to invent their shape or behavior.

## Contribution covenant

- A city contribution should use official public sources and remain narrow
  enough for its trustee and reviewers to verify.
- Protect private citizens. Do not aggregate profiles about residents or turn
  public-comment participation into a personal record.
- Do not editorialize about public officials. Preserve attribution, source
  links, and the reader's ability to inspect the original record.
- Prefer an honest empty field or empty result to an unsupported claim.
- A human reviews material before it is published. Do not remove or
  work around that review boundary.
- Refer to the person who tends a city's shelf as its **trustee**.

## Parser honesty

- A parser must return meetings found in the source, not examples of what a
  meeting might look like.
- Return `[]` only when a source was read successfully and no meetings were
  found. Let fetch or structure failures surface clearly.
- Never create a fallback meeting, reuse a dated sample row, or turn fixture
  data into runtime output.
- Keep sample meetings in tests or fixtures only, clearly separated from the
  parser's production path.
- Do not fill unknown dates, times, locations, statuses, or URLs from
  assumptions. Empty is honest; invented completeness is not.
- When reviewing an AI-authored parser, inspect every error and fallback path,
  not only its successful output.

## What not to invent

Do not invent unpublished services, hidden data stores, project history,
maintainer motives, or backstory. Do not fill gaps from convention alone. Read
the code and history, cite what they support, and name what remains unknown.

## License

The repository is available under the [PolyForm Noncommercial License 1.0.0](LICENSE),
subject to the project [`NOTICE`](NOTICE). Commercial use is not granted.
