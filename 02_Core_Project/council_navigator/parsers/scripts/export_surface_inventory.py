"""Export THREAT_MODEL_INPUT_SECURITY.md's S-N surface catalog as JSON.

Part of the D-100 independent-verification scaffolding. The operator runs
this script + pastes the output (along with the audit prompt in
01_Project_Overview/HARDENING_FINDINGS_SCHEMA.md) into the
Antigravity-Jules-Gemini-Pro session that runs the actual independent
review pass. Gemini-Pro returns findings JSON in the schema documented in
that same SPEC; the capture CLI ingests it.

Deterministic-only — this script does NOT call any LLM. It parses
markdown headers + bold-prefixed subsections (the four-section shape
THREAT_MODEL_INPUT_SECURITY.md already uses for every S-N entry) and
emits structured JSON.

Usage:
  python3.11 parsers/scripts/export_surface_inventory.py --output inventory.json
  python3.11 parsers/scripts/export_surface_inventory.py --include S-1,S-7 --output scope.json
  python3.11 parsers/scripts/export_surface_inventory.py --stdout
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

# UTF-8 stdout so the threat model's unicode (em-dashes, geq, arrows) doesn't
# crash on Windows cp1252 consoles.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Threat-model doc lives under 01_Project_Overview/. Resolve relative to this script's
# location: parsers/scripts/ -> council_navigator/ -> 02_Core_Project/ -> ZSPAN/.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
_THREAT_MODEL_PATH = (
    _REPO_ROOT / "01_Project_Overview" / "THREAT_MODEL_INPUT_SECURITY.md"
)

# Matches an `### S-N — Title` header. Captures the S-id and the trailing title.
# Em-dash AND hyphen-dash both supported so the parser doesn't drift if a future
# edit changes the dash style.
_SURFACE_HEADER_RE = re.compile(
    r"^###\s+(S-\d+)\s+[—–\-]\s+(.+?)\s*$",
    re.MULTILINE,
)

# Each S-N section is split into bold-prefixed paragraphs. We map the
# threat-model's prose-style labels onto stable JSON field names.
# Subsection headers in the threat model are bold-prefixed "**Label.**" but
# some entries add parenthetical context — "**V0 gaps to close (V1-UI-3 work).**".
# Allow optional parenthetical content before the trailing period for each label.
_SUBSECTION_FIELDS = [
    ("description", re.compile(r"\*\*What it is\.\*\*", re.IGNORECASE)),
    ("capability_holding_stages", re.compile(r"\*\*Capability-holding stages it reaches\.\*\*", re.IGNORECASE)),
    ("existing_mitigations", re.compile(r"\*\*Existing structural mitigations\.\*\*", re.IGNORECASE)),
    ("v0_gaps", re.compile(r"\*\*V0 gaps to close[^*]*\.\*\*", re.IGNORECASE)),
    ("acceptance_tests", re.compile(r"\*\*Acceptance tests[^*]*\.\*\*", re.IGNORECASE)),
]

_SECTION_END = re.compile(r"^---\s*$", re.MULTILINE)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_threat_model(markdown_text: str) -> list[dict]:
    """Parse the threat-model markdown into a list of structured surface entries.

    Returns a list of dicts, each shape:
      {
        "id": "S-N",
        "name": "...",
        "description": "...",
        "capability_holding_stages": "...",
        "existing_mitigations": "...",
        "v0_gaps": "...",
        "acceptance_tests": "...",
      }

    Fields that don't appear in a given section are emitted as empty strings
    so the JSON shape is stable across surfaces.
    """
    headers = list(_SURFACE_HEADER_RE.finditer(markdown_text))
    if not headers:
        return []

    surfaces: list[dict] = []
    for i, match in enumerate(headers):
        start = match.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(markdown_text)
        body = markdown_text[start:end]

        # If a `---` divider falls inside this section, cut at the first one
        # (the threat model uses `---` between surfaces; the cuts are conservative).
        divider = _SECTION_END.search(body)
        if divider:
            body = body[:divider.start()]

        entry = {
            "id": match.group(1),
            "name": _clean(match.group(2)),
            "description": "",
            "capability_holding_stages": "",
            "existing_mitigations": "",
            "v0_gaps": "",
            "acceptance_tests": "",
        }

        # Walk each subsection regex; capture text between its match and the
        # next subsection's match (or end of section).
        subsection_matches: list[tuple[str, int, int]] = []
        for field, pattern in _SUBSECTION_FIELDS:
            m = pattern.search(body)
            if m:
                subsection_matches.append((field, m.start(), m.end()))
        subsection_matches.sort(key=lambda t: t[1])
        for j, (field, _ms, me) in enumerate(subsection_matches):
            sub_end = (
                subsection_matches[j + 1][1]
                if j + 1 < len(subsection_matches)
                else len(body)
            )
            entry[field] = _clean(body[me:sub_end])

        surfaces.append(entry)

    return surfaces


def filter_surfaces(
    surfaces: list[dict],
    include: Optional[list[str]],
) -> list[dict]:
    if not include:
        return surfaces
    wanted = {s.strip() for s in include}
    return [s for s in surfaces if s["id"] in wanted]


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export the D-100 surface catalog as JSON.",
    )
    parser.add_argument(
        "--threat-model",
        default=str(_THREAT_MODEL_PATH),
        help="path to THREAT_MODEL_INPUT_SECURITY.md (default: resolved relative to repo root)",
    )
    parser.add_argument(
        "--include",
        help="comma-separated list of surface IDs to emit (e.g., S-1,S-7). Default: all.",
    )
    parser.add_argument(
        "--output",
        help="path to write the JSON file. If omitted, prints to stdout.",
    )
    parser.add_argument(
        "--stdout", action="store_true",
        help="explicit stdout output (equivalent to omitting --output)",
    )
    parser.add_argument(
        "--pretty", action="store_true",
        help="indent the JSON for human reading (default: compact)",
    )
    args = parser.parse_args(argv)

    threat_model_path = Path(args.threat_model)
    if not threat_model_path.exists():
        print(f"ERROR: threat model not found at {threat_model_path}", file=sys.stderr)
        return 1

    markdown = threat_model_path.read_text(encoding="utf-8")
    surfaces = parse_threat_model(markdown)
    if not surfaces:
        print("ERROR: parsed zero surfaces from threat model", file=sys.stderr)
        return 1

    include = (
        [s.strip() for s in args.include.split(",")] if args.include else None
    )
    filtered = filter_surfaces(surfaces, include)

    payload = {
        "source": str(threat_model_path),
        "surface_count": len(filtered),
        "surface_ids": [s["id"] for s in filtered],
        "surfaces": filtered,
    }

    indent = 2 if args.pretty else None
    text = json.dumps(payload, indent=indent, ensure_ascii=False)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
        print(
            f"wrote {len(filtered)} surface entries to {out_path}",
            file=sys.stderr,
        )
    else:
        print(text)

    return 0


if __name__ == "__main__":
    sys.exit(main())
