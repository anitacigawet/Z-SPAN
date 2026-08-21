#!/usr/bin/env python3
"""seed_state_from_census.py — Recon-1: Census-seeded Phase-1 enumeration.

Replaces NATIONAL_SCALING.md Phase-1 LLM enumeration with a deterministic,
audit-traceable, OCD-compatible seed. Direct application of D-085 at the
recon layer, per the 2026-06-14 RECON_SWARM_AUDIT (Action 1).

Inputs (vendored at parsers/scripts/data/, see data/README.md):
  - Census GUS 2022 "Government Units" XLSX (one-time download)
  - OCD division-IDs subset per-state (CSV, regen recipe in data/README.md)

Output:
  state_scaffolding/<state-slug>/_city_list/<county-slug>.json
  Matches the existing utah/* schema PLUS Census FIPS + OCD ID columns.

Usage:
  python3.11 seed_state_from_census.py --state NV
  python3.11 seed_state_from_census.py --state NV --dry-run --json
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

try:
    import openpyxl
except ImportError:
    sys.exit("seed_state_from_census.py needs openpyxl. Install: pip install openpyxl")


_SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CENSUS_XLSX = _SCRIPT_DIR / "data" / "Govt_Units_2022_Final.xlsx"
DEFAULT_OCD_DIR = _SCRIPT_DIR / "data" / "ocd_division_ids"

CENSUS_GUS_URL = "https://www2.census.gov/programs-surveys/gus/datasets/2022/govt_units_2022.ZIP"
CENSUS_GUS_METHODOLOGY_URL = "https://www.census.gov/data/tables/2022/econ/gus/2022-governments.html"
OCD_REPO_URL = "https://github.com/opencivicdata/ocd-division-ids"

UNIT_TYPE_MUNICIPAL = "2 - MUNICIPAL"

# USPS abbreviation → (display name, repo slug). Extend as states queue.
STATE_NAMES: dict[str, tuple[str, str]] = {
    "AZ": ("Arizona", "arizona"),
    "CA": ("California", "california"),
    "CO": ("Colorado", "colorado"),
    "NV": ("Nevada", "nevada"),
    "TX": ("Texas", "texas"),
    "UT": ("Utah", "utah"),
    "VA": ("Virginia", "virginia"),
    "WA": ("Washington", "washington"),
}

# Census prefixes municipal UNIT_NAME with one of these; strip before display.
_MUNICIPAL_PREFIXES = ("CITY OF ", "TOWN OF ", "VILLAGE OF ", "TOWNSHIP OF ", "BOROUGH OF ")


def _smart_title(name: str) -> str:
    return " ".join(w.capitalize() for w in name.split())


def title_case_city(unit_name: str) -> str:
    n = unit_name.strip()
    for p in _MUNICIPAL_PREFIXES:
        if n.startswith(p):
            n = n[len(p):]
            break
    return _smart_title(n)


def title_case_county(name: str) -> str:
    return _smart_title(name.strip())


def slugify(name: str) -> str:
    return name.lower().replace(" ", "_").replace("-", "_").replace("'", "")


# OCD names use lowercase suffixes ("Henderson city") that mark unit type,
# while suffixes that are part of the proper name stay capitalized
# ("Carson City"). Case-sensitive matching distinguishes the two cleanly,
# including "Boulder City city" → "Boulder City" (one stripped suffix).
_OCD_NAME_SUFFIXES = (" city", " town", " village", " borough", " township")


def _strip_ocd_name_suffix(ocd_name: str) -> str:
    n = ocd_name.strip()
    for s in _OCD_NAME_SUFFIXES:
        if n.endswith(s):
            return n[: -len(s)].rstrip()
    return n


def make_ocd_lookup_by_geoid(ocd_csv_path: Path) -> dict[str, tuple[str, str]]:
    """Read per-state OCD CSV; return {census_geoid: (ocd_division_id, canonical_name)} for place: rows."""
    if not ocd_csv_path.exists():
        return {}
    lookup: dict[str, tuple[str, str]] = {}
    with ocd_csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) < 6:
                continue
            ocd_id = row[0]
            ocd_name = row[1]
            census_geoid = row[5]
            if census_geoid and census_geoid.startswith("place-"):
                lookup[census_geoid] = (ocd_id, _strip_ocd_name_suffix(ocd_name))
    return lookup


def resolve_ocd_entry(
    state_abbr: str,
    fips_state: str,
    fips_place: str,
    census_fallback_name: str,
    ocd_lookup: dict[str, tuple[str, str]],
) -> tuple[str, str, bool]:
    """Return (ocd_id, canonical_name, found_in_catalog).

    OCD catalog wins for name when present (Census's 'CITY OF BOULDER' loses
    the canonical 'Boulder City'). Falls back to Census-stripped name when absent.
    """
    geoid = f"place-{fips_state.zfill(2)}{fips_place.zfill(5)}"
    if geoid in ocd_lookup:
        ocd_id, ocd_name = ocd_lookup[geoid]
        return ocd_id, ocd_name, True
    slug = slugify(census_fallback_name)
    return (
        f"ocd-division/country:us/state:{state_abbr.lower()}/place:{slug}",
        census_fallback_name,
        False,
    )


def seed_state(
    state_abbr: str,
    census_xlsx: Path,
    ocd_csv: Path,
    output_dir: Path,
    dry_run: bool = False,
) -> dict:
    state_abbr = state_abbr.upper()
    if state_abbr not in STATE_NAMES:
        raise SystemExit(f"State {state_abbr!r} not in STATE_NAMES; add it to the script.")
    state_name, _ = STATE_NAMES[state_abbr]

    if not census_xlsx.exists():
        raise SystemExit(f"Census XLSX missing at {census_xlsx}; see data/README.md.")

    ocd_lookup = make_ocd_lookup_by_geoid(ocd_csv)
    if not ocd_lookup:
        print(
            f"WARNING: OCD CSV missing/empty at {ocd_csv}; all OCD IDs will be provisional.",
            file=sys.stderr,
        )

    print(f"Reading {census_xlsx} ...", file=sys.stderr)
    wb = openpyxl.load_workbook(census_xlsx, read_only=True)
    ws = wb["General Purpose"]
    rows = ws.iter_rows(values_only=True)
    header = next(rows)
    col = {name: idx for idx, name in enumerate(header)}

    cities_by_county: dict[str, list[dict]] = defaultdict(list)
    state_fips: Optional[str] = None
    county_fips_by_name: dict[str, str] = {}
    provisional_ocd_count = 0

    for row in rows:
        if row[col["STATE"]] != state_abbr:
            continue
        if row[col["UNIT_TYPE"]] != UNIT_TYPE_MUNICIPAL:
            continue
        if row[col["IS_ACTIVE"]] != "Y":
            continue

        unit_name = row[col["UNIT_NAME"]]
        county_area_raw = row[col["COUNTY_AREA_NAME"]] or ""
        county_area = title_case_county(county_area_raw)
        fips_state = str(row[col["FIPS_STATE"]] or "").zfill(2)
        fips_county = str(row[col["FIPS_COUNTY"]] or "").zfill(3)
        fips_place = str(row[col["FIPS_PLACE"]] or "").zfill(5)
        population = row[col["POPULATION"]]

        if state_fips is None:
            state_fips = fips_state
        county_fips_by_name[county_area] = fips_county

        census_fallback_name = title_case_city(unit_name)
        ocd_id, canonical_name, ocd_found = resolve_ocd_entry(
            state_abbr, fips_state, fips_place, census_fallback_name, ocd_lookup
        )
        if not ocd_found:
            provisional_ocd_count += 1

        city_entry: dict = {
            "name": canonical_name,
            "population": int(population) if isinstance(population, (int, float)) and population else None,
            "fips_place_code": fips_place,
            "ocd_division_id": ocd_id,
        }
        if not ocd_found:
            city_entry["ocd_provisional"] = True
        cities_by_county[county_area].append(city_entry)

    if state_fips is None:
        raise SystemExit(f"No municipalities found for state {state_abbr!r}.")

    summary: dict = {
        "state": state_name,
        "state_abbr": state_abbr,
        "state_fips": state_fips,
        "county_count": len(cities_by_county),
        "city_count": sum(len(v) for v in cities_by_county.values()),
        "provisional_ocd_count": provisional_ocd_count,
        "output_dir": str(output_dir),
        "dry_run": dry_run,
        "counties": {},
    }

    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    for county_name, cities in sorted(cities_by_county.items()):
        slug = slugify(county_name)
        cities_sorted = sorted(cities, key=lambda c: c["name"])
        doc = {
            "county": county_name,
            "state": state_name,
            "state_fips": state_fips,
            "county_fips": county_fips_by_name.get(county_name, ""),
            "cities": cities_sorted,
            "sources": [
                CENSUS_GUS_URL,
                CENSUS_GUS_METHODOLOGY_URL,
                OCD_REPO_URL,
            ],
            "_provenance": {
                "generator": "parsers/scripts/seed_state_from_census.py (Recon-1)",
                "census_dataset": "Census Government Units 2022 (gus/datasets/2022/govt_units_2022.ZIP)",
                "ocd_catalog": "github.com/opencivicdata/ocd-division-ids @ master",
            },
        }
        out_path = output_dir / f"{slug}.json"
        if dry_run:
            print(f"[dry-run] would write {out_path} ({len(cities_sorted)} cities)", file=sys.stderr)
        else:
            out_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        summary["counties"][county_name] = len(cities_sorted)

    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", required=True, help="USPS 2-letter state abbreviation (e.g., NV)")
    parser.add_argument(
        "--census-xlsx",
        type=Path,
        default=DEFAULT_CENSUS_XLSX,
        help=f"Census GUS XLSX path (default: {DEFAULT_CENSUS_XLSX})",
    )
    parser.add_argument(
        "--ocd-csv",
        type=Path,
        default=None,
        help="per-state OCD CSV (default: <data>/ocd_division_ids/state-<lower>.csv)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="output dir (default: state_scaffolding/<state-slug>/_city_list/)",
    )
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument("--json", action="store_true", help="print summary as JSON to stdout")
    args = parser.parse_args(argv)

    state_abbr = args.state.upper()
    if state_abbr not in STATE_NAMES:
        raise SystemExit(f"State {state_abbr!r} not in STATE_NAMES; edit the script to add it.")
    _, state_slug = STATE_NAMES[state_abbr]

    ocd_csv = args.ocd_csv or (DEFAULT_OCD_DIR / f"state-{state_abbr.lower()}.csv")

    if args.output_dir is None:
        navigator_root = _SCRIPT_DIR.parent.parent
        output_dir = navigator_root / "state_scaffolding" / state_slug / "_city_list"
    else:
        output_dir = args.output_dir

    summary = seed_state(state_abbr, args.census_xlsx, ocd_csv, output_dir, dry_run=args.dry_run)

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"State: {summary['state']} ({summary['state_abbr']}, FIPS {summary['state_fips']})")
        print(f"Counties: {summary['county_count']}")
        print(f"Cities total: {summary['city_count']}")
        if summary["provisional_ocd_count"]:
            print(f"  WARNING: {summary['provisional_ocd_count']} cities with provisional OCD IDs")
        print(f"Output: {summary['output_dir']}")
        if summary["dry_run"]:
            print("(dry-run; no files written)")
        for c, n in sorted(summary["counties"].items()):
            print(f"  {c}: {n} cities")

    return 0


if __name__ == "__main__":
    sys.exit(main())
