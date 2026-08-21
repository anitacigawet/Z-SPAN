"""Meeting-level recusal detector — Phase 2 of the D-132-era
post-extraction stage.

Scans the m<id>.json (quotes) + m<id>_decisions.json (decisions) sidecars
for recusal events. Per James 2026-06-24: recusals are surfaced at the
MEETING level, not the per-decision level — a council member declaring
conflict-of-interest is accountability-relevant regardless of whether
the underlying matter rose to Key Decision tier-1 visibility on this
meeting's broadcast page.

Recusal in formal council settings is the procedurally-defined response
to a disclosed conflict of interest (Robert's Rules of Order; most state
ethics codes; parliamentary law generally). Detection is mechanical:
regex match against the recusal vocabulary in already-extracted prose,
plus speaker identification from the surrounding quote / decision audit.

Output sidecar: .preview/m<meeting_id>_recusals.json with a list of
recusal events:

  [
    {
      "speaker_name": "Ken Watkins",
      "speaker_role": "Mayor",
      "matter": "Kingman Main Street Route 66 native plant walk",
      "rationale": "Mayor's first-person declaration of recusal from
                    the Kingman Main Street native plant walk vote",
      "citation": {"source": "quote", "chunk_index": 22},
      "raw_text": "I am recusing myself in this item."
    },
    ...
  ]

The BroadcastPage UI reads this to surface a red ❗ next to the
KEY DECISIONS header with the recusal list in a hover/click popover.

Usage:
    python -m zspan_pipeline.recusal_detector --meeting-id 103753
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
PREVIEW_DIR = REPO_ROOT / ".preview"

# Recusal vocabulary. Council-room formal speech is conservative; the
# patterns below cover the common phrasings. False-positive risk is low
# because "recuse" is essentially a parliamentary term — it doesn't
# appear in conversational asides at any meaningful rate.
RECUSAL_PATTERNS = [
    re.compile(r"\brecus(?:e|ing|al|ed|als)\b", re.IGNORECASE),
    re.compile(r"\bstepping\s+(?:aside|down)\s+(?:from|on)\b", re.IGNORECASE),
    re.compile(r"\babstain(?:ing)?\s+due\s+to\s+(?:a\s+)?conflict\b", re.IGNORECASE),
    re.compile(r"\bdeclar(?:e|ing)\s+a\s+conflict\b", re.IGNORECASE),
    re.compile(r"\bhave\s+a\s+conflict\s+on\s+this\b", re.IGNORECASE),
]


def is_recusal_text(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in RECUSAL_PATTERNS)


def _scan_quotes(quotes: list) -> list[dict]:
    out = []
    for q in quotes:
        text = q.get("quote_text") or ""
        if is_recusal_text(text):
            out.append({
                "speaker_name": q.get("speaker_name"),
                "speaker_role": q.get("speaker_role"),
                "rationale": q.get("selection_rationale") or "Recusal declaration in extracted quote",
                "citation": {
                    "source": "quote",
                    "chunk_index": q.get("chunk_index"),
                    "video_timestamp_seconds": q.get("video_timestamp_seconds"),
                },
                "raw_text": text.strip(),
                "matter": None,  # filled in below from decision matching when possible
            })
    return out


def _scan_decisions(decisions_prose: str, decisions_audit: list) -> list[dict]:
    """The decisions prose carries recusal mentions inside `<nuance>` spans
    (e.g., 'with Mayor Watkins recusing himself from the vote'). We pull
    those out separately so a recusal is recorded even if the original
    quote-extraction discipline filtered out the speaker's recusal
    sentence (e.g., it was too short/structural to pass G3 quotability).
    """
    out = []
    if not decisions_prose:
        return out

    # Split by numbered-list markers so we can attach recusals to specific
    # decisions when possible.
    item_re = re.compile(r"^(\d+)\.\s+(.+?)(?=^\d+\.\s+|\Z)", re.MULTILINE | re.DOTALL)
    items = item_re.findall(decisions_prose)
    for idx_str, item_text in items:
        idx = int(idx_str)
        if not is_recusal_text(item_text):
            continue
        # Extract the recusal sentence (the surrounding <nuance> or
        # standalone clause). Walk from each recusal-pattern match
        # forward/backward to find the clause boundary.
        for pattern in RECUSAL_PATTERNS:
            for m in pattern.finditer(item_text):
                start = max(0, m.start() - 80)
                end = min(len(item_text), m.end() + 80)
                clause = item_text[start:end].strip()
                # Best-effort speaker extraction — look for a roster-style
                # capitalized-name preceding the recusal verb.
                speaker_match = re.search(
                    r"((?:[A-Z][a-z]+\s+){1,3}[A-Z][a-z]+)\s+(?:recus|abstain|stepping|declar|hav(?:ing|e))",
                    clause,
                )
                speaker = speaker_match.group(1) if speaker_match else None
                # Audit entry for this decision (if present) carries the
                # human-readable rationale we surface in the tooltip.
                audit_entry = next(
                    (a for a in (decisions_audit or []) if a.get("index") == idx),
                    None,
                )
                rationale = (
                    f"{speaker or 'Council member'} recusal noted in Decision {idx}"
                )
                if audit_entry and audit_entry.get("rationale"):
                    rationale = audit_entry["rationale"]
                # Match attempt: extract a "matter" string from the decision
                # text — the part before the recusal nuance.
                matter_match = re.match(r"(?:<core>)?([^<]+?)(?:</core>)?(?:[,\s]|$)", item_text)
                matter = matter_match.group(1).strip() if matter_match else None
                out.append({
                    "speaker_name": speaker,
                    "speaker_role": None,
                    "rationale": rationale,
                    "citation": {
                        "source": "decision",
                        "decision_index": idx,
                    },
                    "raw_text": clause,
                    "matter": matter,
                })
    return out


_ROLE_PREFIXES = re.compile(
    r"^(?:mayor|vice\s+mayor|council\s*member|councilman|councilwoman|"
    r"chair|chairwoman|chairman|vice\s+chair|alderman|alderwoman)\s+",
    re.IGNORECASE,
)


def _canonical_speaker_key(name: Optional[str]) -> str:
    """Normalize a speaker name to a dedup key. Strips role prefixes
    ('Mayor Watkins' → 'Watkins') and reduces to the LAST WORD when the
    full first+last name isn't available. So 'Ken Watkins' and
    'Mayor Watkins' both collapse to 'watkins'.
    """
    if not name:
        return "unknown"
    cleaned = _ROLE_PREFIXES.sub("", name.strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return "unknown"
    parts = cleaned.split()
    # Use the LAST token (typically the surname); reliably collapses
    # first-name-and-surname vs role-prefix-and-surname variants.
    return parts[-1].lower()


def _dedupe_events(events: list[dict]) -> list[dict]:
    """Merge events that name the same speaker — prefer the entry with the
    higher-confidence citation (quote > decision) and the most-specific
    matter binding. Speaker matching uses the canonical surname key so
    'Ken Watkins' and 'Mayor Watkins' collapse correctly.
    """
    by_speaker: dict[str, dict] = {}
    for ev in events:
        key = _canonical_speaker_key(ev.get("speaker_name"))
        if key not in by_speaker:
            by_speaker[key] = ev
            continue
        existing = by_speaker[key]
        # Prefer the quote-citation event (it carries the verbatim text
        # and timecode); preserve the matter binding from whichever event
        # has one. Also prefer the more-canonical speaker_name form (the
        # one without the role prefix baked in) when both are present.
        ev_is_quote = ev.get("citation", {}).get("source") == "quote"
        existing_is_quote = existing.get("citation", {}).get("source") == "quote"
        if ev_is_quote and not existing_is_quote:
            merged = dict(ev)
            if existing.get("matter") and not merged.get("matter"):
                merged["matter"] = existing["matter"]
            by_speaker[key] = merged
        elif existing_is_quote and not ev_is_quote:
            if ev.get("matter") and not existing.get("matter"):
                existing["matter"] = ev["matter"]
        else:
            # Both same citation tier — keep first, backfill any
            # missing fields from second.
            for field in ("matter", "speaker_role"):
                if not existing.get(field) and ev.get(field):
                    existing[field] = ev[field]
    return list(by_speaker.values())


def detect_recusals_for_meeting(meeting_id: int) -> dict:
    quotes_path = PREVIEW_DIR / f"m{meeting_id}.json"
    decisions_path = PREVIEW_DIR / f"m{meeting_id}_decisions.json"
    out_path = PREVIEW_DIR / f"m{meeting_id}_recusals.json"

    quotes = []
    decisions_prose = ""
    decisions_audit: list = []
    sources_scanned = []

    if quotes_path.exists():
        qdata = json.loads(quotes_path.read_text())
        quotes = qdata.get("quotes", [])
        sources_scanned.append(f"quotes ({len(quotes)})")
    if decisions_path.exists():
        ddata = json.loads(decisions_path.read_text())
        decisions_prose = ddata.get("prose_output", "")
        decisions_audit = ddata.get("audit_json") or []
        sources_scanned.append(f"decisions ({len(decisions_audit)})")

    if not sources_scanned:
        raise FileNotFoundError(
            f"Neither {quotes_path} nor {decisions_path} exists — nothing to scan"
        )

    logger.info("Recusal scan over: %s", ", ".join(sources_scanned))

    quote_events = _scan_quotes(quotes)
    decision_events = _scan_decisions(decisions_prose, decisions_audit)
    logger.info(
        "Raw matches: %d in quotes, %d in decisions",
        len(quote_events), len(decision_events),
    )

    merged = _dedupe_events(quote_events + decision_events)
    logger.info("After dedup by speaker: %d recusal events", len(merged))

    payload = {
        "meeting_id": meeting_id,
        "recusal_count": len(merged),
        "recusals": merged,
        "sources_scanned": sources_scanned,
    }
    out_path.write_text(json.dumps(payload, indent=2))
    logger.info("Recusals sidecar written: %s", out_path)
    for ev in merged:
        speaker = ev.get("speaker_name") or "(unknown)"
        role = ev.get("speaker_role") or ""
        logger.info("  %s %s — %s", speaker, f"({role})" if role else "", ev.get("rationale", "")[:80])
    return payload


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(description="Detect meeting-level recusal events")
    parser.add_argument("--meeting-id", type=int, required=True)
    args = parser.parse_args()
    detect_recusals_for_meeting(args.meeting_id)


if __name__ == "__main__":
    main()
