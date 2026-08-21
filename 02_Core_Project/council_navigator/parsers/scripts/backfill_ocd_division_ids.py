#!/usr/bin/env python3
"""backfill_ocd_division_ids.py — Recon-4: add OCD division IDs to existing schemas.

For each city in `parser_index.json` and `city_intelligence/*.json`, look up
its `ocd_division_id` from the vendored per-state OCD CSV (the same data
Recon-1 already uses for new states) and write it back to the schema. The
field becomes the durable cross-walk to every other OCD-using dataset
(Census, OpenStates, Code for America brigade tools, scrapers-us-municipal).

Per the 2026-06-14 RECON_SWARM_AUDIT Action 4 + D-108 (open-core distribution
— the AGPL scaffolding becomes structurally compatible with the broader
civic-data ecosystem at zero cost to the flagship dataset).

Idempotent: running multiple times produces the same result.
Safe: --dry-run + --json modes for inspection before write.

How it matches:
  1. Build a lookup keyed by SLUG (lowercase, punctuation stripped,
     spaces → underscores). OCD already stores names in slug form
     (e.g. `place:st_johns`), so this matches our `parser_index` keys
     like `"St. Johns"` deterministically.
  2. Walk parser_index.json entries + city_intelligence/*.json files;
     write ocd_division_id (or null) to each.
  3. Report unmatched cities — they're either OCD-catalog gaps (rare;
     small unincorporated places) or real name mismatches the operator
     should review.

Usage:
    python3.11 backfill_ocd_division_ids.py --state AZ
    python3.11 backfill_ocd_division_ids.py --state AZ --dry-run --json
    python3.11 backfill_ocd_division_ids.py --state AZ --no-city-intelligence
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
_PARSERS = _SCRIPT_DIR.parent
DEFAULT_PARSER_INDEX = _PARSERS / "parser_index.json"
DEFAULT_CITY_INTELLIGENCE_DIR = _PARSERS.parent / "city_intelligence"
DEFAULT_OCD_DIR = _SCRIPT_DIR / "data" / "ocd_division_ids"


# ---------------------------------------------------------------------------
# Slug normalization
# ---------------------------------------------------------------------------


_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)
_WS = re.compile(r"\s+")


def slug(name: str) -> str:
    """Normalize a city/place name to the OCD slug form.

    'St. Johns' -> 'st_johns'
    'Bullhead City' -> 'bullhead_city'
    'Lake Havasu City' -> 'lake_havasu_city'
    "Coeur d'Alene" -> 'coeur_dalene'
    """
    s = name.strip().lower()
    # Treat hyphen as a word-separator BEFORE punctuation-strip so
    # "Winston-Salem" → "winston_salem" (not "winstonsalem"). Same for
    # any hyphenated compound place name.
    s = s.replace("-", " ")
    s = _PUNCT.sub("", s)
    s = _WS.sub(" ", s).strip()
    s = s.replace(" ", "_")
    # Collapse repeated underscores from removed punctuation.
    s = re.sub(r"_+", "_", s).strip("_")
    return s


# ---------------------------------------------------------------------------
# OCD CSV loader
# ---------------------------------------------------------------------------


def load_ocd_lookup(ocd_csv: Path) -> dict[str, str]:
    """Read the per-state OCD CSV. Returns slug → ocd_division_id for 'place' rows.

    The CSV has both 'place' (incorporated municipalities) and 'county' rows.
    For city backfill, only 'place' rows matter — county IDs aren't a city
    identifier.
    """
    lookup: dict[str, str] = {}
    with ocd_csv.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            ocd_id = row["id"]
            if "/place:" not in ocd_id:
                continue
            # Slug is the part after `place:`.
            place_slug = ocd_id.split("/place:")[-1].split(",")[0]
            lookup[place_slug] = ocd_id
            # Also key by the human "name" column for fuzzy fallback (e.g.
            # OCD's display name might be "Phoenix city" — strip the suffix).
            name = row["name"].strip()
            # Strip OCD's trailing unit-type suffix ("city", "town", "CDP",
            # "village", "borough", "township") so the slugged name matches
            # parser_index entries.
            name_no_suffix = re.sub(
                r"\s+(city|town|village|borough|township|CDP)$",
                "",
                name,
                flags=re.IGNORECASE,
            )
            lookup.setdefault(slug(name_no_suffix), ocd_id)
            lookup.setdefault(slug(name), ocd_id)
    return lookup


# ---------------------------------------------------------------------------
# Backfill operations
# ---------------------------------------------------------------------------


def backfill_parser_index(
    parser_index_path: Path,
    lookup: dict[str, str],
    *,
    dry_run: bool = False,
) -> dict:
    """Update parser_index.json with ocd_division_id per entry.

    Adds the field next to existing `city` / `county` keys; sets to null
    when no OCD entry matches.
    """
    with parser_index_path.open() as f:
        idx = json.load(f)

    matched = 0
    unmatched: list[str] = []
    already_set = 0

    for city, meta in idx.items():
        # Idempotency — if it's already set + correct, leave it.
        existing = meta.get("ocd_division_id")
        ocd_id = lookup.get(slug(city))
        if ocd_id:
            if existing == ocd_id:
                already_set += 1
            meta["ocd_division_id"] = ocd_id
            matched += 1
        else:
            meta.setdefault("ocd_division_id", None)
            unmatched.append(city)

    if not dry_run:
        # Pretty-print + trailing newline (project convention).
        parser_index_path.write_text(
            json.dumps(idx, indent=2, ensure_ascii=False) + "\n"
        )

    return {
        "total": len(idx),
        "matched": matched,
        "already_set": already_set,
        "unmatched_count": len(unmatched),
        "unmatched": unmatched,
        "dry_run": dry_run,
    }


def backfill_city_intelligence(
    city_intel_dir: Path,
    lookup: dict[str, str],
    *,
    dry_run: bool = False,
) -> dict:
    """Update each city_intelligence/<slug>.json with a top-level ocd_division_id.

    Uses `canonical_name` for the lookup (RECIPE.md says it must match the
    parser_index entry character-for-character).
    """
    results = []
    matched = 0
    unmatched: list[str] = []
    already_set = 0

    for path in sorted(city_intel_dir.glob("*.json")):
        with path.open() as f:
            data = json.load(f)
        canonical = data.get("canonical_name") or path.stem
        existing = data.get("ocd_division_id")
        ocd_id = lookup.get(slug(canonical))
        if ocd_id:
            if existing == ocd_id:
                already_set += 1
            # Insert at top-level near canonical_name for readability.
            data["ocd_division_id"] = ocd_id
            matched += 1
            results.append({"file": path.name, "ocd_division_id": ocd_id})
        else:
            data.setdefault("ocd_division_id", None)
            unmatched.append(path.name)
            results.append({"file": path.name, "ocd_division_id": None})

        if not dry_run:
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    return {
        "total": len(results),
        "matched": matched,
        "already_set": already_set,
        "unmatched_count": len(unmatched),
        "unmatched": unmatched,
        "results": results,
        "dry_run": dry_run,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Backfill ocd_division_id into parser_index.json + "
            "city_intelligence/*.json (Recon-4)."
        ),
    )
    p.add_argument(
        "--state",
        default="AZ",
        help="USPS 2-letter state code (default: AZ). Picks the OCD CSV at data/ocd_division_ids/state-<lc>.csv.",
    )
    p.add_argument(
        "--ocd-csv",
        type=Path,
        default=None,
        help="Override path to the OCD CSV (default: data/ocd_division_ids/state-<lc>.csv).",
    )
    p.add_argument(
        "--parser-index",
        type=Path,
        default=DEFAULT_PARSER_INDEX,
        help=f"Path to parser_index.json (default: {DEFAULT_PARSER_INDEX}).",
    )
    p.add_argument(
        "--city-intelligence-dir",
        type=Path,
        default=DEFAULT_CITY_INTELLIGENCE_DIR,
        help=f"Path to city_intelligence/ (default: {DEFAULT_CITY_INTELLIGENCE_DIR}).",
    )
    p.add_argument(
        "--no-parser-index",
        action="store_true",
        help="Skip parser_index.json (only update city_intelligence/).",
    )
    p.add_argument(
        "--no-city-intelligence",
        action="store_true",
        help="Skip city_intelligence/ (only update parser_index.json).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write any files; report what WOULD change.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human-readable summary.",
    )
    args = p.parse_args(argv)

    ocd_csv = args.ocd_csv or (
        DEFAULT_OCD_DIR / f"state-{args.state.lower()}.csv"
    )
    if not ocd_csv.exists():
        sys.exit(
            f"OCD CSV missing: {ocd_csv}\n"
            "Fetch it via the recipe in parsers/scripts/data/README.md "
            "before running this backfill."
        )

    lookup = load_ocd_lookup(ocd_csv)

    pi_summary = None
    ci_summary = None

    if not args.no_parser_index:
        pi_summary = backfill_parser_index(
            args.parser_index, lookup, dry_run=args.dry_run
        )
    if not args.no_city_intelligence:
        ci_summary = backfill_city_intelligence(
            args.city_intelligence_dir, lookup, dry_run=args.dry_run
        )

    summary = {
        "state": args.state.upper(),
        "ocd_csv": str(ocd_csv),
        "lookup_entries": len(lookup),
        "dry_run": args.dry_run,
        "parser_index": pi_summary,
        "city_intelligence": ci_summary,
    }

    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(f"State: {summary['state']}")
        print(f"OCD CSV: {summary['ocd_csv']} ({summary['lookup_entries']} lookup keys)")
        print(f"Dry-run: {summary['dry_run']}")
        if pi_summary:
            print()
            print(
                f"parser_index.json: {pi_summary['matched']}/{pi_summary['total']} matched "
                f"({pi_summary['already_set']} already set), "
                f"{pi_summary['unmatched_count']} unmatched"
            )
            if pi_summary["unmatched"]:
                print("  Unmatched cities (need operator review):")
                for c in pi_summary["unmatched"]:
                    print(f"    - {c}")
        if ci_summary:
            print()
            print(
                f"city_intelligence/: {ci_summary['matched']}/{ci_summary['total']} matched "
                f"({ci_summary['already_set']} already set), "
                f"{ci_summary['unmatched_count']} unmatched"
            )
            if ci_summary["unmatched"]:
                print("  Unmatched files:")
                for c in ci_summary["unmatched"]:
                    print(f"    - {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
