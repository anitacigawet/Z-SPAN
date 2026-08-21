#!/usr/bin/env python3.11
"""
Populate the database cache by scraping all cities.
Can be run as a cron job for periodic refresh.
"""
import json
import os
import sys
import time
from datetime import datetime

# Add parsers dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import init_db, populate_cities_from_index, cache_meetings, get_stats
from parser_loader import scrape_city_calendar, load_parser_index
from normalize import normalize_meeting_fields

def populate_all(timeout_per_city=30):
    """Scrape all cities and cache results."""
    print(f"\n{'='*60}")
    print(f"Cache Population Started: {datetime.now().isoformat()}")
    print(f"{'='*60}\n")
    
    # Initialize DB and populate cities
    init_db()
    populate_cities_from_index()
    
    index = load_parser_index()
    total = len(index)
    success = 0
    failed = 0
    total_meetings = 0
    
    for i, (city_name, info) in enumerate(index.items(), 1):
        county = info.get('county', 'Unknown')
        print(f"[{i}/{total}] {city_name} ({county})...", end=" ", flush=True)
        
        start = time.time()
        try:
            meetings = scrape_city_calendar(city_name)
            if meetings:
                # Normalize field names
                normalized = [normalize_meeting_fields(m) for m in meetings]
                cached = cache_meetings(city_name, county, normalized)
                print(f"✓ {cached} meetings ({time.time()-start:.1f}s)")
                success += 1
                total_meetings += cached
            else:
                print(f"○ 0 meetings ({time.time()-start:.1f}s)")
                # Cache empty result so we don't re-scrape constantly
                cache_meetings(city_name, county, [])
        except Exception as e:
            elapsed = time.time() - start
            print(f"✗ Error: {str(e)[:60]} ({elapsed:.1f}s)")
            failed += 1
    
    print(f"\n{'='*60}")
    print(f"Cache Population Complete: {datetime.now().isoformat()}")
    print(f"  Success: {success}/{total}")
    print(f"  Failed:  {failed}/{total}")
    print(f"  Total meetings cached: {total_meetings}")
    print(f"{'='*60}\n")
    
    # Print stats
    stats = get_stats()
    print(f"Database Stats:")
    print(f"  Cities: {stats['total_cities']}")
    print(f"  Active: {stats['active_cities']}")
    print(f"  Meetings: {stats['total_meetings']}")
    
    return {'success': success, 'failed': failed, 'total_meetings': total_meetings}


if __name__ == '__main__':
    populate_all()
