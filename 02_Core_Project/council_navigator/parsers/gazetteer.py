"""
US Census Places Gazetteer lookup — resolves (state_abbr, city_name) →
(lat, lng) without per-city hand-curation.

Substrate: `data/2024_Gaz_place_national.txt` (~6.5 MB, ~33K rows). One row
per US incorporated place / CDP. Fields used: USPS (state abbr), NAME
(place name with LSAD suffix appended — e.g. "Kingman city",
"Lake Havasu City city", "Colorado City town"), INTPTLAT / INTPTLONG
(internal-point coordinates — guaranteed inside the place polygon,
better than geometric centroid for irregular shapes).

Refresh cadence: Census publishes annually each August. To update, drop
the new `YYYY_Gaz_place_national.txt` into `data/` and update
`GAZETTEER_FILENAME` below.

Z-SPAN uses this as the DEFAULT coordinate substrate for every city
in `parser_index.json`. Hand-curated overrides for showcase cities live
client-side in `client/src/data/zspanCities.ts`, which wins when present.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

GAZETTEER_FILENAME = "2024_Gaz_place_national.txt"
_GAZETTEER_PATH = Path(__file__).parent / "data" / GAZETTEER_FILENAME

# LSAD-suffix tokens that Census appends to NAME. Stripping these
# leaves the base place name we want to match on. Case-insensitive.
# Source: live inspection of unique tail-tokens in the 2024 file.
_LSAD_SUFFIXES = {
    "city",
    "town",
    "village",
    "borough",
    "township",
    "cdp",
    "comunidad",
    "corporation",
    "government",
    "municipality",
    "urbana",
    "(balance)",
}

# Loaded lazily on first lookup. Key: (state_abbr_upper, normalized_name).
# Value: (lat, lng) as floats.
_INDEX: Optional[dict[tuple[str, str], tuple[float, float]]] = None


def _normalize_query(raw: str) -> str:
    """Lowercase + collapse whitespace. Does NOT strip LSAD-shaped tail
    tokens: a query name like "Bullhead City" or "Colorado City" has the
    word "City" as part of the name, not as a Census-style suffix.
    """
    s = re.sub(r"\s+", " ", raw.strip()).lower()
    return s


def _normalize_census_name(raw: str) -> str:
    """Census places carry an LSAD suffix appended to NAME — strip it
    before indexing so the key matches a clean query.

    "Bullhead City city"   -> "bullhead city"
    "Lake Havasu City city" -> "lake havasu city"
    "Colorado City town"    -> "colorado city"
    "Kingman city"          -> "kingman"
    """
    s = raw.strip()
    parts = s.rsplit(" ", 1)
    if len(parts) == 2 and parts[1].lower() in _LSAD_SUFFIXES:
        s = parts[0]
    return _normalize_query(s)


def _load_index() -> dict[tuple[str, str], tuple[float, float]]:
    """Parse the gazetteer TSV into an in-memory index.

    Tab-separated; header row first. Columns:
    USPS  GEOID  ANSICODE  NAME  LSAD  FUNCSTAT  ALAND  AWATER
    ALAND_SQMI  AWATER_SQMI  INTPTLAT  INTPTLONG
    """
    if not _GAZETTEER_PATH.exists():
        logger.warning(
            "gazetteer file missing at %s — coordinate lookups will return None. "
            "Re-download from "
            "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/",
            _GAZETTEER_PATH,
        )
        return {}

    # Census LSAD code for a Census Designated Place (unincorporated). On a
    # same-name collision within a state, the incorporated municipality
    # (city/town/village — any non-CDP LSAD) is the one that has a council
    # + meetings, so it MUST win over the CDP. First-write-wins alone gets
    # this wrong whenever the CDP's GEOID sorts before the city's (e.g.
    # Cottonwood AZ: the tiny CDP near the Navajo border would otherwise
    # mask the real Verde Valley city ~150mi away). (S-067 audit F2.)
    _LSAD_CDP = "57"

    idx: dict[tuple[str, str], tuple[float, float]] = {}
    idx_lsad: dict[tuple[str, str], str] = {}
    collisions: list[tuple[tuple[str, str], str]] = []
    with _GAZETTEER_PATH.open("r", encoding="utf-8") as fh:
        header = fh.readline()  # discard header row
        if not header.startswith("USPS"):
            logger.error(
                "gazetteer file %s missing expected USPS header — got %r",
                _GAZETTEER_PATH,
                header[:80],
            )
            return {}
        for line in fh:
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 12:
                continue
            usps = cols[0].strip().upper()
            name_raw = cols[3].strip()
            try:
                lat = float(cols[10].strip())
                lng = float(cols[11].strip())
            except ValueError:
                continue
            key = (usps, _normalize_census_name(name_raw))
            lsad = cols[4].strip() if len(cols) > 4 else ""
            if key in idx:
                existing_lsad = idx_lsad.get(key, "")
                new_is_cdp = lsad == _LSAD_CDP
                existing_is_cdp = existing_lsad == _LSAD_CDP
                if existing_is_cdp and not new_is_cdp:
                    # Incorporated place beats the CDP we stored first.
                    idx[key] = (lat, lng)
                    idx_lsad[key] = lsad
                    collisions.append((key, f"CDP→{lsad} (corrected to incorporated)"))
                elif (not existing_is_cdp) and new_is_cdp:
                    # Keep the incorporated place; the new CDP is correctly skipped.
                    collisions.append((key, f"{lsad}=CDP dropped (kept incorporated)"))
                else:
                    # Genuine same-class ambiguity (two CDPs, or two incorporated
                    # places of the same name in one state — rare; e.g. Red Rock
                    # AZ has two CDPs). First-write-wins; disambiguation needs the
                    # county via lookup_city_coords county_hint (deferred until a
                    # parser-registered city actually hits this). Loud per the F8
                    # succeeded-empty-vs-failed-silent discipline. (S-067 audit F2.)
                    collisions.append((key, f"{lsad} dropped (same-class, first-write-wins)"))
                continue
            idx[key] = (lat, lng)
            idx_lsad[key] = lsad
    if collisions:
        logger.warning(
            "gazetteer same-name collisions: %d total (CDP-vs-incorporated resolved "
            "in favor of incorporated; same-class first-write-wins). First 10: %s",
            len(collisions),
            [f"{k[0]}|{k[1]} [{note}]" for k, note in collisions[:10]],
        )
    logger.info("gazetteer loaded: %d places indexed (%d collisions handled)",
                len(idx), len(collisions))
    return idx


def lookup_city_coords(
    city_name: Optional[str],
    state_abbr: Optional[str],
    county_hint: Optional[str] = None,  # disambiguation hook for same-name same-state collisions (see _load_index collision warning); unused until national scale needs it
) -> Optional[tuple[float, float]]:
    """Resolve (state_abbr, city_name) to (lat, lng) via the Census gazetteer.

    Returns None when:
      - either argument is falsy
      - the gazetteer file is missing
      - no matching place exists in the gazetteer

    Caller is expected to fall back (county centroid, hand-curated table,
    honest-empty pin) when None is returned.
    """
    global _INDEX
    if _INDEX is None:
        _INDEX = _load_index()
    if not city_name or not state_abbr:
        return None
    key = (state_abbr.strip().upper(), _normalize_query(city_name))
    return _INDEX.get(key)


def gazetteer_size() -> int:
    """Diagnostic — number of places indexed. 0 if file missing."""
    global _INDEX
    if _INDEX is None:
        _INDEX = _load_index()
    return len(_INDEX)


if __name__ == "__main__":
    # Smoke check: hit a few canonical AZ cities + Chicago.
    logging.basicConfig(level=logging.INFO)
    samples = [
        ("Kingman", "AZ"),
        ("Bullhead City", "AZ"),
        ("Lake Havasu City", "AZ"),
        ("Colorado City", "AZ"),
        ("Phoenix", "AZ"),
        ("Prescott", "AZ"),
        ("Prescott Valley", "AZ"),
        ("Chicago", "IL"),
        ("San Luis", "AZ"),
        ("St. Johns", "AZ"),
        ("Made Up Place", "ZZ"),
    ]
    print(f"gazetteer size: {gazetteer_size()} places")
    for city, state in samples:
        result = lookup_city_coords(city, state)
        print(f"  {state} / {city:30s} -> {result}")
