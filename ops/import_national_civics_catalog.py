#!/usr/bin/env python3
"""Build Z-SPAN's public navigation roster from National Civics Catalog.

This is an explicit, offline import. It projects public identity and
contribution-location fields only; endpoint URLs and parser routing stay in
their respective source/application layers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "zspan.national-catalog-roster.v1"
CATALOG_REPOSITORY = "https://github.com/anitacigawet/national-civics-catalog"
STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
    "AS": "American Samoa", "GU": "Guam", "MP": "Northern Mariana Islands",
    "PR": "Puerto Rico", "VI": "U.S. Virgin Islands",
}
SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
ALLOWED_STATUSES = {
    "needs_source", "working", "empty", "blocked", "broken", "moved",
    "retired", "unverified",
}


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _load_route_names(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("route map must be one JSON object")
    result: dict[str, str] = {}
    for route_name, raw in payload.items():
        catalog = raw.get("catalog") if isinstance(raw, dict) else None
        endpoint = catalog.get("endpoint") if isinstance(catalog, dict) else None
        source_id = endpoint.get("endpoint_id") if isinstance(endpoint, dict) else None
        if not isinstance(route_name, str) or not isinstance(source_id, str):
            continue
        if source_id in result:
            raise ValueError(f"duplicate routed source_id {source_id!r}")
        result[source_id] = route_name
    return result


def build_roster(
    catalog_root: Path,
    *,
    catalog_commit: str,
    imported_on: str,
    route_map: Path | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{7,40}", catalog_commit):
        raise ValueError("catalog_commit must be a lowercase Git commit id")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", imported_on):
        raise ValueError("imported_on must use YYYY-MM-DD")
    routes = _load_route_names(route_map)
    projected: dict[str, list[dict[str, Any]]] = {code: [] for code in STATE_NAMES}
    source_hashes: dict[str, str] = {}
    seen_sources: set[str] = set()

    for file_code in STATE_NAMES:
        source_file = catalog_root / "data" / "states" / file_code.casefold() / "sources.jsonl"
        raw_bytes = source_file.read_bytes()
        source_hashes[file_code] = _sha256(raw_bytes)
        for line_number, raw_line in enumerate(raw_bytes.decode("utf-8").splitlines(), 1):
            if not raw_line.strip():
                continue
            value = json.loads(raw_line)
            if not isinstance(value, dict):
                raise ValueError(f"{source_file}:{line_number} must be one object")
            source_id = value.get("source_id")
            status = value.get("status")
            covers = value.get("covers")
            if not isinstance(source_id, str) or not SOURCE_ID_RE.fullmatch(source_id):
                raise ValueError(f"{source_file}:{line_number} has invalid source_id")
            if source_id in seen_sources:
                raise ValueError(f"duplicate source_id {source_id!r}")
            if status not in ALLOWED_STATUSES:
                raise ValueError(f"{source_file}:{line_number} has invalid status")
            if not isinstance(covers, list) or not covers:
                raise ValueError(f"{source_file}:{line_number} has no covered place")
            seen_sources.add(source_id)

            for cover in covers:
                if not isinstance(cover, dict):
                    raise ValueError(f"{source_file}:{line_number} has invalid coverage")
                name = cover.get("name")
                place_type = cover.get("type")
                state_codes = cover.get("state_codes")
                county_names = cover.get("county_names")
                if not isinstance(name, str) or not name.strip():
                    raise ValueError(f"{source_file}:{line_number} has invalid place name")
                if not isinstance(place_type, str) or not place_type:
                    raise ValueError(f"{source_file}:{line_number} has invalid place type")
                if not isinstance(state_codes, list) or not state_codes:
                    raise ValueError(f"{source_file}:{line_number} has no state coverage")
                if not isinstance(county_names, list):
                    raise ValueError(f"{source_file}:{line_number} has invalid counties")
                shelves = county_names or ["Statewide and regional"]
                for state_code in state_codes:
                    if state_code not in STATE_NAMES:
                        raise ValueError(f"{source_file}:{line_number} has unknown state code")
                    for county_name in shelves:
                        if not isinstance(county_name, str) or not county_name.strip():
                            raise ValueError(f"{source_file}:{line_number} has invalid county name")
                        projected[state_code].append({
                            "source_id": source_id,
                            "name": name.strip(),
                            "place_type": place_type,
                            "county_name": county_name.strip(),
                            "status": status,
                            "file_state_code": file_code,
                            "line_number": line_number,
                            "route_name": routes.get(source_id),
                        })

    states: list[dict[str, Any]] = []
    total_projections = 0
    for code, name in STATE_NAMES.items():
        places = sorted(
            projected[code],
            key=lambda row: (
                row["county_name"].casefold(), row["name"].casefold(),
                row["source_id"], row["line_number"],
            ),
        )
        identities = {
            (row["county_name"].casefold(), row["name"].casefold(), row["source_id"])
            for row in places
        }
        if len(identities) != len(places):
            raise ValueError(f"duplicate navigation projection in {code}")
        total_projections += len(places)
        states.append({
            "code": code,
            "name": name,
            "source_file_sha256": source_hashes[code],
            "places": places,
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "catalog_repository": CATALOG_REPOSITORY,
        "catalog_commit": catalog_commit,
        "imported_on": imported_on,
        "source_count": len(seen_sources),
        "projection_count": total_projections,
        "states": states,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog-root", type=Path, required=True)
    parser.add_argument("--catalog-commit", required=True)
    parser.add_argument("--imported-on", required=True)
    parser.add_argument("--route-map", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    payload = build_roster(
        args.catalog_root.resolve(),
        catalog_commit=args.catalog_commit,
        imported_on=args.imported_on,
        route_map=args.route_map.resolve() if args.route_map else None,
    )
    encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    print(
        f"validated {payload['source_count']} sources / "
        f"{payload['projection_count']} navigation projections / sha256={_sha256(encoded)}"
    )
    if not args.apply:
        print("dry run: no output written")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise FileExistsError(f"refusing to overwrite {args.output}")
    args.output.write_bytes(encoded)
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
