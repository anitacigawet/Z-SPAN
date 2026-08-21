#!/usr/bin/env python3.11
"""
Standalone script to scrape a city's calendar
Usage: python3.11 scrape_city.py <city_name>
"""
import sys
import json
from parser_loader import scrape_city_calendar

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"error": "City name required"}), file=sys.stderr)
        sys.exit(1)
    
    city_name = sys.argv[1]
    
    try:
        meetings = scrape_city_calendar(city_name)
        print(json.dumps(meetings))
    except Exception as e:
        import traceback
        error_info = {"error": str(e), "traceback": traceback.format_exc()}
        print(json.dumps(error_info), file=sys.stderr)
        sys.exit(1)
