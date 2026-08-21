#!/usr/bin/env python3.11
"""
One-off admin helper: register a city's YouTube channel URL in the DB.

Usage:
    cd 02_Core_Project
    python3.11 -m zspan_pipeline.scripts.set_city_channel \\
        --city "Kingman" \\
        --channel-url "https://www.youtube.com/@CityofKingman/videos"

Optional:
    --channel-id "UC_xxxxxxxxxxxxxxxxxxxxxx"   # only if you know it; not required

This bypasses Flask. It's meant for the manual workflow where you've just
verified a city's official YouTube channel in your browser and want to
register it without spinning up the API.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `parsers/` importable
_PARSERS_DIR = Path(__file__).resolve().parent.parent.parent / "council_navigator" / "parsers"
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

from database import (  # noqa: E402
    set_city_youtube_channel,
    get_city_youtube_channel,
    populate_cities_from_index,
    get_connection,
)


def _cities_table_empty() -> bool:
    conn = get_connection()
    try:
        n = conn.execute("SELECT COUNT(*) FROM cities").fetchone()[0]
        return n == 0
    finally:
        conn.close()


def main() -> int:
    p = argparse.ArgumentParser(description="Register a city's YouTube channel.")
    p.add_argument("--city", required=True, help='City name, e.g. "Kingman"')
    p.add_argument("--channel-url", required=True,
                   help='YouTube channel URL, e.g. "https://www.youtube.com/@CityofKingman/videos"')
    p.add_argument("--channel-id", default=None,
                   help="Optional YouTube channel ID (UCxxx). Not required.")
    p.add_argument("--state", default=None,
                   help='State name for multi-state disambiguation, e.g. "California". '
                        'Required if the city name exists in multiple states.')
    p.add_argument("--county", default=None,
                   help='County name for disambiguation, e.g. "Sacramento County". '
                        'Use with --state when even the (name, state) pair is ambiguous.')
    args = p.parse_args()

    # Defensive: if Flask has never run, the cities table is empty. Populate
    # it from parser_index.json so the city row exists for the UPDATE.
    if _cities_table_empty():
        print("Cities table empty — populating from parser_index.json...")
        populate_cities_from_index()

    try:
        updated = set_city_youtube_channel(
            city_name=args.city,
            channel_url=args.channel_url,
            channel_id=args.channel_id,
            state=args.state,
            county=args.county,
        )
    except ValueError as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        print(f"   Re-run with --state '<State Name>' (and --county '<County>' if needed).", file=sys.stderr)
        return 2

    if not updated:
        print(f"[FAIL] City not found in DB: {args.city}", file=sys.stderr)
        if args.state:
            print(f"   Searched with state='{args.state}'"
                  + (f", county='{args.county}'" if args.county else ""), file=sys.stderr)
        print(f"   Check spelling against parser_index.json. Common AZ names:", file=sys.stderr)
        print(f"   'Kingman', 'Bullhead City', 'Lake Havasu City', 'Colorado City'", file=sys.stderr)
        return 1

    info = get_city_youtube_channel(args.city, state=args.state, county=args.county)
    print(f"[OK] Registered YouTube channel for {args.city}"
          + (f" ({args.state})" if args.state else ""))
    if info:
        print(f"   Channel URL: {info['channel_url']}")
        if info.get("channel_id"):
            print(f"   Channel ID:  {info['channel_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
