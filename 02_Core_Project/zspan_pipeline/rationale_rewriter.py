"""Rationale rewriter — converts operator-debug rationale strings on
already-extracted quotes into reader-facing one-line summaries.

Per operator-direction 2026-06-24: the `selection_rationale` field
shipped with the new-discipline quote extraction reads like an operator
audit log ("Vice Mayor commits council to transit solution while
flagging funding accountability gap; 'speed of government' framing is
distinctive"). On the public-facing accordion-summary UI the rationale
needs to read like a newspaper sub-headline — present-tense action
verb, subject-elided, substance-not-meta. This script does a single
Sonnet pass over an existing quote sidecar and rewrites each rationale
in place WITHOUT re-running the full extraction (which would be ~80k
tokens; this is ~5-8k).

Usage:
    python -m zspan_pipeline.rationale_rewriter --meeting-id 103753
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
PREVIEW_DIR = REPO_ROOT / ".preview"

REWRITE_INSTRUCTIONS = """Rewrite each quote's `selection_rationale` field into a reader-facing one-line summary in newspaper-sub-headline style. The current rationales were authored as operator-debug audit text; they need to read as scannable public-UI summaries.

Style rules — apply to every rewrite:

  - **Present-tense action verb opening** ("Declares...", "Presses for...", "Frames... as...", "Recuses from...", "Voices support for...", "Warns that...", "Calls on...").
  - **Subject elided** (the speaker is named separately on the card; don't repeat "Council member X declares" — just "Declares...").
  - **Substance, not meta-criteria** — name WHAT the speaker is saying. NOT why it passed the discipline gates.
  - **≤90 characters**.
  - Should read like a newspaper sub-headline or pull-quote chyron.

Canonical worked examples (this is the target style):

  - "Declares police vacancy recruitment is failing"
  - "Strong concerns over grill losses"
  - "Recuses from Main Street trail vote"
  - "Pushes for transit solution; flags funding accountability gap"
  - "Frames transit vs police/fire as budget tradeoff; names affordability as priority"
  - "Presses for timeline on golf course privatization study"
  - "Supports Route 66 trail but raises water-cost accountability questions"
  - "Voices on-record support for Route 66 Nature Trail"
  - "Warns CR-252 would constitutionally bar all AZ cities from raising taxes"

Anti-pattern (avoid):

  - "Vice Mayor commits council to transit solution while flagging funding accountability gap; 'speed of government' framing is distinctive" (too long, meta-commentary, redundantly names speaker)
  - "Council member places an explicit on-record statement of support for the Route 66 Nature Trail project" (verbose; "places an explicit on-record statement of support for" → "voices support for")
  - "Substantive vote-rationale committing speaker to specific position on water-rights ordinance" (meta-commentary; doesn't name actual position)

For each quote in the input, emit ONE rewritten `selection_rationale` keyed by `quote_index`. Output strict JSON, no preamble, no closing line, no markdown code fence.

Output schema:

```json
{
  "rewrites": [
    {"quote_index": 0, "new_rationale": "Declares police vacancy recruitment is failing"},
    {"quote_index": 1, "new_rationale": "Strong concerns over grill losses"}
  ]
}
```

The `rewrites` array MUST have one entry per input quote, indexed 0..N-1 matching the input array order. Output ONLY the JSON object."""


def _strip_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def rewrite_rationales_for_meeting(meeting_id: int) -> dict:
    sidecar_path = PREVIEW_DIR / f"m{meeting_id}.json"
    if not sidecar_path.exists():
        raise FileNotFoundError(f"Quote sidecar missing: {sidecar_path}")
    data = json.loads(sidecar_path.read_text())
    quotes = data.get("quotes", [])
    if not quotes:
        logger.info("No quotes to rewrite; sidecar unchanged")
        return data

    logger.info("Rewriting rationales for %d quotes", len(quotes))

    # Compact representation for the rewrite prompt — speaker + quote text
    # + current rationale so Sonnet has the substance to rewrite from.
    payload = []
    for i, q in enumerate(quotes):
        payload.append({
            "quote_index": i,
            "speaker_name": q.get("speaker_name"),
            "speaker_role": q.get("speaker_role"),
            "quote_text": q.get("quote_text"),
            "current_rationale": q.get("selection_rationale"),
            "news_values": q.get("news_values") or [],
        })

    sys.path.insert(0, str(REPO_ROOT / "02_Core_Project"))
    from zspan_pipeline import qdrant_synthesizer  # type: ignore

    prompt = (
        "INPUT QUOTES (with current rationales to rewrite):\n"
        + json.dumps(payload, indent=2)
        + "\n\n"
        + REWRITE_INSTRUCTIONS
    )
    logger.info("Prompt size: %d chars", len(prompt))

    t0 = time.time()
    raw = qdrant_synthesizer.synthesize_via_claude_p(
        prompt,
        model=qdrant_synthesizer.SONNET_MODEL_ID,
        timeout_seconds=300.0,
    )
    elapsed = time.time() - t0
    raw = _strip_fence(raw)
    logger.info("Sonnet response: %d chars in %.0fs", len(raw), elapsed)

    parsed = json.loads(raw)
    rewrites = parsed.get("rewrites", [])
    by_index = {r["quote_index"]: r["new_rationale"] for r in rewrites if "quote_index" in r and "new_rationale" in r}
    logger.info("Got %d rewrites", len(by_index))

    # Apply in place, preserving everything else
    changed = 0
    for i, q in enumerate(quotes):
        if i in by_index:
            old = q.get("selection_rationale", "")
            new = by_index[i].strip()
            if new and new != old:
                q["selection_rationale_original"] = old  # preserve for audit
                q["selection_rationale"] = new
                changed += 1

    logger.info("Rewrote %d / %d rationales", changed, len(quotes))
    data["rationale_rewrite_elapsed_seconds"] = elapsed
    data["rationale_rewritten_count"] = changed

    sidecar_path.write_text(json.dumps(data, indent=2))
    logger.info("Sidecar updated in place: %s", sidecar_path)

    # Print a sample for operator inspection
    print()
    print("=" * 60)
    print("Sample rewrites:")
    for i, q in enumerate(quotes[:6]):
        print(f"\n  [{i}] {q.get('speaker_name')} ({q.get('speaker_role')})")
        print(f"      OLD: {q.get('selection_rationale_original', '')[:120]}")
        print(f"      NEW: {q.get('selection_rationale', '')[:120]}")
    return data


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(description="Rewrite rationale strings in place")
    parser.add_argument("--meeting-id", type=int, required=True)
    args = parser.parse_args()
    rewrite_rationales_for_meeting(args.meeting_id)


if __name__ == "__main__":
    main()
