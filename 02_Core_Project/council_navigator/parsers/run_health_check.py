#!/usr/bin/env python3
"""Final comprehensive health check for all parsers via Flask API"""
import json, requests
from datetime import datetime

with open('parser_index.json', 'r') as f:
    parser_index = json.load(f)

results = {}
healthy = []
unhealthy = []
total_meetings = 0

print("Testing all parsers via GET /scrape/<city>...")
print("=" * 80)

for city in sorted(parser_index.keys()):
    print(f"  {city}...", end=" ", flush=True)
    try:
        r = requests.get(f'http://127.0.0.1:5001/scrape/{city}', timeout=30)
        if r.status_code == 200:
            data = r.json()
            count = data.get('count', 0)
            if data.get('success') and count > 0:
                print(f"OK ({count})")
                healthy.append(city)
                total_meetings += count
                results[city] = {'status': 'healthy', 'meetings': count}
            elif data.get('success') and count == 0:
                print(f"OK (0 meetings)")
                healthy.append(city)
                results[city] = {'status': 'healthy', 'meetings': 0}
            else:
                err = data.get('error', 'unknown')[:60]
                print(f"FAIL: {err}")
                unhealthy.append(city)
                results[city] = {'status': 'error', 'error': err}
        else:
            print(f"HTTP {r.status_code}")
            unhealthy.append(city)
            results[city] = {'status': 'http_error', 'code': r.status_code}
    except requests.exceptions.Timeout:
        print("TIMEOUT")
        unhealthy.append(city)
        results[city] = {'status': 'timeout'}
    except Exception as e:
        print(f"ERR: {str(e)[:40]}")
        unhealthy.append(city)
        results[city] = {'status': 'exception', 'error': str(e)[:100]}

print("\n" + "=" * 80)
print(f"HEALTHY:   {len(healthy)}/{len(parser_index)} ({len(healthy)/len(parser_index)*100:.1f}%)")
print(f"UNHEALTHY: {len(unhealthy)}/{len(parser_index)} ({len(unhealthy)/len(parser_index)*100:.1f}%)")
print(f"TOTAL MEETINGS: {total_meetings:,}")
print("\nHealthy cities:", ", ".join(healthy))
print("\nUnhealthy cities:", ", ".join(unhealthy))

report = {
    'timestamp': datetime.now().isoformat(),
    'healthy_count': len(healthy),
    'unhealthy_count': len(unhealthy),
    'total': len(parser_index),
    'total_meetings': total_meetings,
    'healthy_cities': healthy,
    'unhealthy_cities': unhealthy,
    'details': results
}
with open('FINAL_HEALTH_REPORT.json', 'w') as f:
    json.dump(report, f, indent=2)
print("\nReport saved to FINAL_HEALTH_REPORT.json")
