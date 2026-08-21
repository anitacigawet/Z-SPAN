# Contributing to Z-SPAN

Z-SPAN is a maintainer-led application. Clear bug reports, accessibility
findings, parser breakage reports, and security disclosures are useful even
when you do not intend to write code.

## Choose the right repository

The projects have separate responsibilities:

- Use the
  [National Civics Catalog](https://github.com/anitacigawet/national-civics-catalog)
  for new official meeting sources, moved or broken endpoints, source status,
  and provenance corrections.
- Use this repository for Z-SPAN's application, parsers, API, pipeline, CLI,
  and interface.
- Use the corrections path on [zspan.org](https://zspan.org) for errors in a
  meeting Z-SPAN has already published.

The catalog contains endpoints. Z-SPAN contains implementations. Neither
repository should receive copied meeting transcripts, summaries, or generated
meeting content as catalog data.

## Reporting a parser problem

Please include:

1. the government or named public body;
2. the collection-level source URL;
3. what changed or failed;
4. the date you observed it;
5. a public source that supports any proposed replacement URL.

Do not include credentials, cookies, access tokens, personal contact lists,
private working notes, or unpublished meeting output.

## Code changes

Keep changes narrow and explain the public evidence behind them. Parser code
must use `polite_http.make_session()` for paced requests with TLS verification,
bound every fetch and loop, preserve empty values rather than inventing data,
and fail visibly when a source changes unexpectedly.

Before proposing a code change, run the checks relevant to the files you
touched. The repository's primary gates are:

```bash
# Python pipeline
cd 02_Core_Project
python -m pytest zspan_pipeline/tests -q

# Parser and API security checks
cd council_navigator/parsers
python scripts/run_input_security_tests.py

# Web interface
cd ../
pnpm check
```

## License and branding

Z-SPAN is available under the PolyForm Noncommercial License 1.0.0. By
submitting a contribution, you represent that you have the right to submit it
and agree that it may be distributed as part of Z-SPAN under that license.

The license does not grant rights in the Z-SPAN name, logo, or other
trademarks beyond uses permitted by applicable law. Independent projects
should use distinct branding and must not imply endorsement.
