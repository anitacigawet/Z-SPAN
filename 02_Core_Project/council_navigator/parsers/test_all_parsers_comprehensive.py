#!/usr/bin/env python3.11
"""
Comprehensive parser testing script
Tests all parsers and categorizes failures by error type
"""
import json
import requests
import time
from datetime import datetime

import os
API_URL = os.getenv('PARSER_API_URL', "http://127.0.0.1:5001") + "/scrape"

# Get list of all cities from parser_index.json
with open('parser_index.json', 'r') as f:
    parser_index = json.load(f)

cities = list(parser_index.keys())
print(f"Testing {len(cities)} city parsers...\n")

results = {}
working_count = 0
broken_count = 0
timeout_count = 0

for i, city in enumerate(cities, 1):
    print(f"[{i}/{len(cities)}] Testing {city}...", end=" ")
    
    try:
        response = requests.get(f"{API_URL}/{city}", timeout=60)
        data = response.json()
        
        if data.get('success') and data.get('count', 0) > 0:
            print(f"✓ {data['count']} meetings")
            results[city] = {
                'status': 'working',
                'meetingCount': data['count'],
                'lastTested': datetime.now().isoformat()
            }
            working_count += 1
        else:
            error = data.get('error', 'No meetings found')
            error_type = data.get('error_type', 'unknown')
            print(f"✗ {error}")
            results[city] = {
                'status': 'broken',
                'meetingCount': 0,
                'error': error,
                'errorType': error_type,
                'lastTested': datetime.now().isoformat()
            }
            broken_count += 1
            
    except requests.exceptions.Timeout:
        print("✗ TIMEOUT")
        results[city] = {
            'status': 'broken',
            'meetingCount': 0,
            'error': 'Request timeout',
            'errorType': 'http',
            'lastTested': datetime.now().isoformat()
        }
        timeout_count += 1
        broken_count += 1
        
    except Exception as e:
        print(f"✗ ERROR: {str(e)}")
        results[city] = {
            'status': 'broken',
            'meetingCount': 0,
            'error': str(e),
            'errorType': 'unknown',
            'lastTested': datetime.now().isoformat()
        }
        broken_count += 1
    
    time.sleep(0.5)  # Small delay between requests

# Save results
with open('../parser_test_results.json', 'w') as f:
    json.dump(results, f, indent=2)

# Print summary
print("\n" + "="*60)
print("PARSER TEST SUMMARY")
print("="*60)
print(f"Total Parsers: {len(cities)}")
print(f"Working: {working_count} ({working_count/len(cities)*100:.1f}%)")
print(f"Broken: {broken_count} ({broken_count/len(cities)*100:.1f}%)")
print(f"  - Timeouts: {timeout_count}")
print(f"\nResults saved to parser_test_results.json")
