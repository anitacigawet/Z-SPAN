"""Quote router — Phase 1 of the D-132-era post-extraction stage.

Takes the two upstream sidecars (quote extraction + decisions extraction)
and routes each quote into one of three buckets:

  - standalone — definitive personal stance, stands alone in the
    bottom Quotes section
  - decision_bound(N) — nested under Key Decision N as Discussion context
  - drop — passed extraction but neither stance nor decision-context;
    dropped from display

Per [D-131](../../01_Project_Overview/DECISIONS.md#d-131): the upstream
selection-discipline gates WHAT CAN APPEAR; this router decides WHERE
WHAT APPEARS.

Output sidecar: .preview/m<meeting_id>_routing.json — a routing payload
keyed by quote_index with bucket assignment + optional decision_index +
one-line rationale. The BroadcastPage preview UI reads this to do the
nested-Discussion-accordion render.

Usage:
    python -m zspan_pipeline.quote_router_runner \\
        --meeting-id 103753

Reads from <repo-root>/.preview/m103753.json +
m103753_decisions.json; writes m103753_routing.json next to them.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from .prompt_loader import strip_explicit_model_boundaries

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
PREVIEW_DIR = REPO_ROOT / ".preview"
PROMPT_PATH = REPO_ROOT / "02_Core_Project" / "prompts" / "quote_router.md"


def _load_prompt() -> str:
    text = PROMPT_PATH.read_text()
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            text = parts[2]
    return strip_explicit_model_boundaries(text)


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


def build_router_prompt(*, quotes: list, decisions_prose: str, decisions_audit: list,
                        instructions: str) -> str:
    """Compose the router prompt: decisions + quotes + instructions in
    that order so Sonnet sees the binding targets before the quotes that
    might bind to them."""
    decisions_block = "DECISIONS (1-indexed, prose + audit):\n"
    decisions_block += decisions_prose.strip()
    if decisions_audit:
        decisions_block += "\n\nAudit:\n" + json.dumps(decisions_audit, indent=2)

    quotes_compact = []
    for i, q in enumerate(quotes):
        quotes_compact.append({
            "quote_index": i,
            "speaker_name": q.get("speaker_name"),
            "speaker_role": q.get("speaker_role"),
            "speaker_class": q.get("speaker_class"),
            "quote_text": q.get("quote_text"),
            "topic_tags": q.get("topic_tags") or [],
            "chunk_index": q.get("chunk_index"),
            "news_values": q.get("news_values") or [],
            "selection_rationale": q.get("selection_rationale"),
        })
    quotes_block = "QUOTES (0-indexed):\n" + json.dumps(quotes_compact, indent=2)

    return f"{decisions_block}\n\n{quotes_block}\n\n{instructions}"


def route_quotes_for_meeting(meeting_id: int) -> dict:
    quotes_path = PREVIEW_DIR / f"m{meeting_id}.json"
    decisions_path = PREVIEW_DIR / f"m{meeting_id}_decisions.json"
    out_path = PREVIEW_DIR / f"m{meeting_id}_routing.json"

    if not quotes_path.exists():
        raise FileNotFoundError(f"Quote sidecar missing: {quotes_path}")
    if not decisions_path.exists():
        raise FileNotFoundError(f"Decisions sidecar missing: {decisions_path}")

    qdata = json.loads(quotes_path.read_text())
    ddata = json.loads(decisions_path.read_text())

    quotes = qdata.get("quotes", [])
    decisions_prose = ddata.get("prose_output", "")
    decisions_audit = ddata.get("audit_json") or []

    logger.info(
        "Router input: %d quotes, %d decisions",
        len(quotes), len(decisions_audit),
    )

    if not quotes:
        payload = {
            "meeting_id": meeting_id,
            "router_started": "complete",
            "elapsed_seconds": 0,
            "summary": {"standalone_count": 0, "decision_bound_count": 0, "drop_count": 0},
            "routing": [],
        }
        out_path.write_text(json.dumps(payload, indent=2))
        logger.info("No quotes to route; emitted empty routing sidecar")
        return payload

    sys.path.insert(0, str(REPO_ROOT / "02_Core_Project"))
    from zspan_pipeline import qdrant_synthesizer  # type: ignore

    instructions = _load_prompt()
    prompt = build_router_prompt(
        quotes=quotes,
        decisions_prose=decisions_prose,
        decisions_audit=decisions_audit,
        instructions=instructions,
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
    routing = parsed.get("routing", [])
    summary = parsed.get("summary", {})

    # Validate: every quote_index appears exactly once
    seen = set()
    for r in routing:
        idx = r.get("quote_index")
        if idx in seen:
            logger.warning("Duplicate quote_index in routing output: %d", idx)
        seen.add(idx)
    missing = set(range(len(quotes))) - seen
    if missing:
        logger.warning("Routing missing quote_index entries: %s", sorted(missing))

    payload = {
        "meeting_id": meeting_id,
        "router_started": "complete",
        "elapsed_seconds": elapsed,
        "routing_total": len(routing),
        "summary": summary,
        "routing": routing,
    }
    out_path.write_text(json.dumps(payload, indent=2))
    logger.info("Routing sidecar written: %s", out_path)
    logger.info(
        "Summary: standalone=%d decision_bound=%d drop=%d",
        summary.get("standalone_count", 0),
        summary.get("decision_bound_count", 0),
        summary.get("drop_count", 0),
    )
    return payload


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(description="Route extracted quotes into display buckets")
    parser.add_argument("--meeting-id", type=int, required=True)
    args = parser.parse_args()
    route_quotes_for_meeting(args.meeting_id)


if __name__ == "__main__":
    main()
