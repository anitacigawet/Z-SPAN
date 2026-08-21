#!/usr/bin/env python3
"""backfill_coordinates.py — Add city_lat / city_lng to parser_index entries
via the US Census Places Gazetteer (parsers/gazetteer.py).

Removes the need for a hand-curated city-coordinates table for the ~30K US
incorporated places + CDPs covered by the Census gazetteer. Contributors
who add a city to parser_index just need city + state + county; this
script resolves coordinates server-side without their involvement.

Companion to backfill_ocd_division_ids.py — same write-back pattern.

Idempotent. --dry-run reports without writing. --force re-resolves cities
that already have coordinates (use when the gazetteer file is refreshed
and you want the new INTPTLAT/INTPTLONG values written through).

Per S-067 resolution 2026-06-19.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
_PARSERS = _SCRIPT_DIR.parent
DEFAULT_PARSER_INDEX = _PARSERS / "parser_index.json"

# Make `parsers/` importable when running this script directly.
sys.path.insert(0, str(_PARSERS))
from gazetteer import lookup_city_coords, gazetteer_size  # noqa: E402


def _backfill_one(
    entry: dict,
    state_default: str,
    force: bool,
) -> tuple[str, Optional[tuple[float, float]]]:
    """Resolve coordinates for one parser_index entry.

    Returns ("kept", existing) when entry already has coords + not --force.
    Returns ("resolved", (lat, lng)) when gazetteer hit.
    Returns ("unresolved", None) when gazetteer miss.
    """
    has_existing = (
        isinstance(entry.get("city_lat"), (int, float))
        and isinstance(entry.get("city_lng"), (int, float))
    )
    if has_existing and not force:
        return ("kept", (entry["city_lat"], entry["city_lng"]))

    city = entry.get("city")
    state = entry.get("state", state_default)
    coords = lookup_city_coords(city, state)
    if coords is None:
        return ("unresolved", None)
    return ("resolved", coords)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--parser-index",
        type=Path,
        default=DEFAULT_PARSER_INDEX,
        help=f"Path to parser_index.json (default: {DEFAULT_PARSER_INDEX})",
    )
    ap.add_argument(
        "--state-default",
        default="AZ",
        help="State abbr used when an entry lacks a `state` field. "
        "parser_index is AZ-implicit today; revisit when Z-SPAN expands. "
        "(default: AZ)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Re-resolve cities that already have coordinates "
        "(use after refreshing the gazetteer TSV).",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing back to disk.",
    )
    args = ap.parse_args()

    print(f"gazetteer: {gazetteer_size()} places indexed", file=sys.stderr)
    print(f"reading: {args.parser_index}", file=sys.stderr)

    with args.parser_index.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    if not isinstance(data, dict):
        print(f"ERROR: parser_index root is not an object", file=sys.stderr)
        return 2

    resolved_count = 0
    kept_count = 0
    unresolved: list[str] = []

    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        status, coords = _backfill_one(entry, args.state_default, args.force)
        if status == "kept":
            kept_count += 1
            continue
        if status == "unresolved":
            unresolved.append(key)
            continue
        # status == "resolved"
        entry["city_lat"] = coords[0]
        entry["city_lng"] = coords[1]
        resolved_count += 1

    total = len(data)
    print("", file=sys.stderr)
    print(f"total entries:   {total}", file=sys.stderr)
    print(f"resolved (new):  {resolved_count}", file=sys.stderr)
    print(f"kept (existing): {kept_count}", file=sys.stderr)
    print(f"unresolved:      {len(unresolved)}", file=sys.stderr)
    if unresolved:
        print("", file=sys.stderr)
        print("Unresolved cities (gazetteer miss — check spelling / "
              "may be CDP vs incorporated mismatch):", file=sys.stderr)
        for name in unresolved:
            entry = data[name]
            print(f"  - {name!r} (county={entry.get('county')!r})",
                  file=sys.stderr)

    if args.dry_run:
        print("", file=sys.stderr)
        print("[--dry-run] no write performed.", file=sys.stderr)
        return 0

    with args.parser_index.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"\nwrote: {args.parser_index}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
