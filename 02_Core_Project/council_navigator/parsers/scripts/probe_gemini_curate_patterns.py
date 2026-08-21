#!/usr/bin/env python3.11
"""S-021 Stage 1 feasibility probe — does the unofficial gemini-webapi module
reliably ground a meeting-pattern curation prompt against Google Search?

ONE-OFF, NOT a production script. Lives in scripts/ so it's discoverable from
TASKS.md / DECISIONS.md, but it never runs in the heartbeat. Result is logged
to stdout + a sidecar JSON file under `media/_probes/` (gitignored, like the
rest of media/) so future-me can compare across attempts.

What it does:
  1. Builds the Step 4 RECIPE prompt for ONE city (default: Kingman, AZ).
  2. Sends it via gemini-webapi (using the same operator-PRIMARY cookies as
     pipeline_operator_gemini_verify.py — Stage 0 of S-021, BEFORE the
     dedicated-Mac-Gemini-host migration).
  3. Parses the response, runs validate_meeting_patterns() on it.
  4. Logs: model used, response length, JSON validity, validation errors (if
     any), and a head-of-response excerpt so we can eyeball whether grounding
     fired (presence of city-website URLs in the response is the tell).

What it does NOT do:
  - File the response into the city's intelligence JSON. The browser
    paste-block path is ground-truth; this probe is for the feasibility
    signal, not for filing.
  - Touch any state outside `media/_probes/`. No DB writes, no city_intelligence
    edits, no commit. Operator inspects the sidecar manually.

Output sidecar shape (`media/_probes/curate_<city>_<timestamp>.json`):
  {
    "city": "Kingman", "state": "Arizona",
    "model": "<whatever the library used>",
    "response_chars": <int>,
    "looks_like_json": <bool>,
    "validates": <bool>,
    "validation_errors": [...],
    "search_grounded_hint": <bool>,   # heuristic: response contains an http URL
    "patterns_count": <int or null>,
    "response_excerpt": "<first 600 chars>"
  }
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Dict, Optional

_HERE = Path(__file__).resolve().parent
_PARSERS_DIR = _HERE.parent
sys.path.insert(0, str(_PARSERS_DIR))

from env_config import get_gemini_consumer_cookies  # noqa: E402
from meeting_patterns import validate_meeting_patterns  # noqa: E402


STEP4_TEMPLATE = """I need a verified JSON snapshot of {city}, {state}'s recurring public-meeting schedule for a civic data project. Use Google search to find authoritative sources — strongly prefer the official city government website ({domain}), specifically the city council / boards & commissions / calendar pages.

Return ONLY a JSON array of meeting_pattern objects. No surrounding prose, no markdown fences, no explanation. I will paste it directly into a file.

Include every body whose meetings are scheduled on a recurring cadence AND streamed publicly (typically City Council + Planning & Zoning / Planning Commission; sometimes also Board of Adjustment, Parks Board, Airport Commission, etc.). Skip closed-session-only bodies and one-off committees.

For each pattern, find the cadence (weekly / biweekly / 2nd-and-4th-X / 1st-of-month / etc.), the meeting time in the city's local zone, the typical location, and the YouTube channel URL where the body's meetings appear (if different from the city's default council channel).

If a body meets ad-hoc (no recurring cadence), use frequency: "adhoc" and skip the cadence sub-fields.

If you cannot find authoritative info for a specific field, use null rather than guessing. Every pattern MUST include a source_url pointing to the page where you verified the cadence.

JSON schema to fill (one object per pattern, wrapped in an array):

[
  {{
    "pattern_id": "<lowercase_snake_case, e.g. 'city_council'>",
    "meeting_type": "<human label, e.g. 'City Council'>",
    "cadence": {{
      "frequency": "<weekly | biweekly | monthly_weeks | monthly_date | twice_monthly | adhoc>",
      "day_of_week": "<Monday..Sunday, when frequency uses it>",
      "weeks_of_month": [<1-5>, ...],
      "anchor_date": "<YYYY-MM-DD, for biweekly>",
      "date_of_month": <1-31, for monthly_date>,
      "days_of_month": [<1-31>, <1-31>]
    }},
    "time_local": "<HH:MM or H:MM AM/PM>",
    "location": "<address or chamber name, or null>",
    "youtube_channel_url": "<URL or null>",
    "exceptions": [],
    "source_url": "<URL>",
    "verified_on": "<YYYY-MM-DD>",
    "notes": "<caveats: holiday skips, seasonal-only bodies, etc.>"
  }}
]
"""


def _extract_json_array(text: str) -> Optional[list]:
    """Try to find a JSON array in the response — tolerant of common Gemini
    cruft (markdown fences, leading/trailing prose despite the instruction)."""
    s = text.strip()
    # strip markdown fence if present
    fence = re.match(r"^```(?:json)?\s*(.+?)\s*```$", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    # find first [ and last matching ]
    start = s.find("[")
    end = s.rfind("]")
    if start < 0 or end < 0 or end <= start:
        return None
    try:
        return json.loads(s[start : end + 1])
    except json.JSONDecodeError:
        return None


async def run(city: str, state: str, domain: str, today_iso: str) -> Dict:
    psid, psidts = get_gemini_consumer_cookies()
    if not psid or not psidts:
        raise SystemExit(
            "Gemini consumer cookies not configured "
            "(gemini_secure_1psid / gemini_secure_1psidts in user_settings.json)."
        )

    from gemini_webapi import GeminiClient, set_log_level

    set_log_level("WARNING")
    client = GeminiClient(secure_1psid=psid, secure_1psidts=psidts)
    await asyncio.wait_for(client.init(timeout=30), timeout=45)

    # Resolve the strongest Pro-class model the library knows about.
    # Library exposes a Model enum; we pick by name-substring match so the
    # probe survives lib updates that rename internal enum constants.
    from gemini_webapi.constants import Model
    available = [m for m in Model]
    # Prefer Pro-tier; rank by name descending so the newest tier wins.
    pro_candidates = sorted(
        (m for m in available if "PRO" in m.name.upper()),
        key=lambda m: m.name,
        reverse=True,
    )
    chosen_model = pro_candidates[0] if pro_candidates else None
    model_label = chosen_model.name if chosen_model else "library-default"

    prompt = STEP4_TEMPLATE.format(
        city=city, state=state, domain=domain,
    ).replace("<YYYY-MM-DD>", today_iso)  # nudge verified_on default

    print(f"[probe] city={city} state={state} model={model_label}")
    print(f"[probe] prompt {len(prompt)} chars; sending...")

    t0 = time.time()
    kwargs = {"model": chosen_model} if chosen_model else {}
    response = await asyncio.wait_for(
        client.generate_content(prompt, **kwargs),
        timeout=300,
    )
    elapsed = time.time() - t0

    text = getattr(response, "text", "") or ""
    print(f"[probe] response {len(text)} chars in {elapsed:.1f}s")

    parsed = _extract_json_array(text)
    looks_like_json = parsed is not None
    if looks_like_json:
        ok, errs = validate_meeting_patterns(parsed)
    else:
        ok, errs = False, ["response did not contain a parseable JSON array"]

    # Heuristic: did grounding fire? If the model used Google Search, the
    # response (or its citations) typically reference http(s) URLs from the
    # city's domain. We treat ANY http URL in the response text as a signal,
    # acknowledging this is imperfect.
    search_grounded_hint = bool(re.search(r"https?://", text))

    result = {
        "city": city, "state": state, "domain": domain,
        "model": model_label, "elapsed_seconds": round(elapsed, 1),
        "response_chars": len(text),
        "looks_like_json": looks_like_json,
        "validates": ok,
        "validation_errors": errs[:30],
        "search_grounded_hint": search_grounded_hint,
        "patterns_count": len(parsed) if isinstance(parsed, list) else None,
        "response_excerpt": text[:600],
    }

    return result


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="S-021 Stage 1 feasibility probe")
    p.add_argument("--city", default="Kingman")
    p.add_argument("--state", default="Arizona")
    p.add_argument("--domain", default="cityofkingman.gov")
    p.add_argument("--today", default=time.strftime("%Y-%m-%d"))
    args = p.parse_args(argv)

    result = asyncio.run(run(args.city, args.state, args.domain, args.today))

    out_dir = _PARSERS_DIR.parent / "media" / "_probes"
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"curate_{args.city.lower().replace(' ', '_')}_{int(time.time())}.json"
    out_path = out_dir / fname
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    print()
    print("=== probe result ===")
    print(json.dumps(
        {k: v for k, v in result.items() if k != "response_excerpt"},
        indent=2,
    ))
    print()
    print(f"[probe] sidecar written to {out_path}")
    print(f"[probe] response excerpt (first 600 chars):")
    print(result["response_excerpt"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
