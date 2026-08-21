#!/usr/bin/env python3.11
"""S-036 V1 — on-demand Haiku-class HTML scraper wrapper.

Invokes the `haiku-html-scraper` agent (see `agents/haiku-html-scraper.md`)
via `claude -p` in a tightly scope-locked settings.json profile. Extracts
council meetings from a single calendar URL into canonical-schema JSON.

The agent's output IS the data — this wrapper parses Claude's stream-json
trace, pulls out the final assistant text, validates it as the canonical
schema, optionally normalizes via `normalize.py`, and prints the result.

Usage:
    python3.11 haiku_html_scrape.py --city "Lake Havasu City"
    python3.11 haiku_html_scrape.py --city "Lake Havasu City" --url "https://..."
    python3.11 haiku_html_scrape.py --city "Bisbee" --json-only > out.json
    python3.11 haiku_html_scrape.py --city "Casa Grande" --no-normalize

Defaults:
- URL resolves from `parser_index.json` if not supplied
- Logs trace to `agents/_haiku_html_scraper_logs/<timestamp>_<city>.jsonl`
- Normalizes via `normalize.py` so the output composes with `cache_meetings()`
- Exit code 0 on `scrape_success: true`, 1 on `scrape_success: false`,
  2 on infrastructure failure (claude CLI missing, subprocess crash, etc.),
  4 on rate-limit refusal (D-078 ceiling hit, cooldown active — caller
  should NOT re-attempt without waiting for the reason to clear)

Rule-of-thumb cost (per S-036 V0 evidence): ~30K tokens, ~30s wall-clock,
~$0.01-0.03/city at retail Haiku 4.5 pricing. Under Max subscription, no
retail exposure (D-078-compliant).

D-078 persisted invocation counter (haiku_rate_limit.py) checks BEFORE each
subprocess fire and records AFTER each return (success or failure). The
ceiling is 50 invocations/day with a 5s wall-clock cooldown; both constants
are code-edit only per D-078's structural-wall principle. Counter rows live
in `balance_ledger` (provider="claude_haiku_scraper", event_type="invocation").
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Intermittent claude-code startup crash signature (0xC0000005 access violation
# in decimal). The existing fleet heartbeats (content-scout-heartbeat.ps1,
# disputed-quotes-reviewer-heartbeat.ps1, orchestrator-heartbeat.ps1) all
# retry around this; we mirror the same shape.
CRASH_EXIT_CODE = 3221225477
CRASH_BYTE_THRESHOLD = 100  # if stdout < this AND non-zero exit, treat as crash
MAX_RETRY_ATTEMPTS = 5
RETRY_DELAYS_S = [10, 12, 13, 13]

# Exit codes for the wrapper's main(): 0 = success, 1 = scrape_success=false,
# 2 = infrastructure failure, 4 = rate-limit refusal (D-078 ceiling or cooldown).
EXIT_OK = 0
EXIT_SCRAPE_FAILED = 1
EXIT_INFRASTRUCTURE_ERROR = 2
EXIT_RATE_LIMITED = 4

_HERE = Path(__file__).resolve().parent
_PARSERS_DIR = _HERE.parent
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# F-4b uses the same threshold constant as the F-4a classifier so wrapper-
# side and classifier-side archive-only checks stay in sync. Co-located in
# scripts/, so the sys.path insert above is enough to import it.
from calendar_class_detector import ARCHIVE_AGE_THRESHOLD_YEARS

# Co-located rate-limit module. Lazy-imported in main() so test files that
# only exercise URL safety / tagging functions don't pull the database
# import path needlessly.
def _load_rate_limit_module():
    import importlib
    return importlib.import_module("haiku_rate_limit")


# Co-located field-sanity output gate. Lazy-imported in main() so tests
# that only exercise URL safety / tagging / prompt construction don't pull
# the requests import path needlessly.
def _load_field_sanity_module():
    import importlib
    return importlib.import_module("haiku_field_sanity")

# Repo root = parsers/.parent (= council_navigator) → ..
# but we need _Z-SPAN/ZSPAN root, which is two levels up from parsers/
_REPO_ROOT = _PARSERS_DIR.parent.parent.parent  # parsers → Navigator → 02_Core_Project → ZSPAN
_AGENTS_DIR = _REPO_ROOT / "agents"
_SETTINGS_PATH = _AGENTS_DIR / "haiku-html-scraper.settings.json"
_LOGS_DIR = _AGENTS_DIR / "_haiku_html_scraper_logs"
_MANUAL_PATH = _AGENTS_DIR / "haiku-html-scraper.md"

HAIKU_MODEL_ID = "claude-haiku-4-5-20251001"
CANONICAL_FIELDS = [
    "meeting_title", "meeting_date", "meeting_time", "meeting_location",
    "agenda_url", "minutes_url", "video_url", "meeting_status", "meeting_id",
]
PROMPT_TEMPLATE = """You are the haiku-html-scraper agent (see agents/haiku-html-scraper.md). Your job: extract civic meetings from ONE URL into canonical-schema JSON and return ONLY that JSON.

City: {city}
URL: {url}

Process:
1. WebFetch the URL.
2. Identify the meeting list in the rendered HTML (look for tables, calendars, lists of agendas/minutes/videos).
3. **Extract EVERY meeting visible on the page — every governing body, every commission, every committee, every public hearing.** This includes City Council, Planning & Zoning, Board of Adjustment, Parks Commission, Heritage Commission, Industrial Park Commission, etc. Do NOT filter to City Council only — the user wants the full civic-meeting picture, not just the headline body.
4. Return ONLY the JSON object as your final message. No markdown fence. No preamble. No commentary.

Schema (return ONE object with these EXACT top-level fields):
{{
  "scrape_success": true | false,
  "scrape_method": "static_html" | "js_required" | "error",
  "meetings_found": <int>,
  "meetings": [<meeting_object>, ...],
  "caveats": ["<string>", ...],
  "raw_observations": "<3-5 sentence description of what was on the page>"
}}

Each meeting_object has these EXACT fields:
{{
  "meeting_title": "<plain string>",
  "meeting_date": "YYYY-MM-DD",
  "meeting_time": "H:MM AM/PM" | "HH:MM" | "",
  "meeting_location": "<string or empty>",
  "agenda_url": "<absolute URL or empty>",
  "minutes_url": "<absolute URL or empty>",
  "video_url": "<absolute URL or empty>",
  "meeting_status": "<status string from the page, e.g. 'Cancelled', or empty>",
  "meeting_id": "<source-internal ID or empty>"
}}

Rules:
- ISO date format required (YYYY-MM-DD). Convert from M/D/YYYY or "Month D, YYYY" if needed.
- Empty fields = "" (NEVER null).
- Absolute URLs only — resolve relative URLs against the page's origin.
- NEVER hallucinate fields. If unsure, return "" and add a caveat.
- Status text stays verbatim from the page ("Cancelled" / "Final" / etc.).
- If the page requires JavaScript rendering and WebFetch returns no meeting list, report scrape_success=false + scrape_method="js_required" + describe what you saw.
- If you receive an HTTP error or the page is missing, report scrape_success=false + scrape_method="error".

Critical: your FINAL ASSISTANT MESSAGE must be the raw JSON object — nothing before, nothing after, no triple-backtick fence. The caller parses your final message as JSON directly.
"""


PROMPT_TEMPLATE_PRE_RENDERED = """You are the haiku-html-scraper agent (see agents/haiku-html-scraper.md). Your job: extract civic meetings from PRE-RENDERED HTML into canonical-schema JSON and return ONLY that JSON.

City: {city}
Source URL (for context + metadata only): {url}

The HTML below was rendered for you because the page requires JavaScript to populate the meeting list (a Class-B page per the deterministic calendar_class_detector). Do NOT WebFetch — the URL alone would return an empty default view. Extract directly from the HTML provided below.

Process:
1. Identify the meeting list in the HTML below. For Legistar pages, the meeting table is `<table class="rgMasterTable">` inside a Telerik RadGrid wrapper.
2. **Extract EVERY meeting visible — every governing body, every commission, every committee, every public hearing.** Do NOT filter to City Council only.
3. Return ONLY the JSON object as your final message. No markdown fence. No preamble. No commentary.

Schema (return ONE object with these EXACT top-level fields):
{{
  "scrape_success": true | false,
  "scrape_method": "static_html" | "js_required" | "error",
  "meetings_found": <int>,
  "meetings": [<meeting_object>, ...],
  "caveats": ["<string>", ...],
  "raw_observations": "<3-5 sentence description of what was on the page>"
}}

Note: because you're working from PRE-RENDERED HTML (not a live page), set `scrape_method` to `"static_html"` if extraction succeeds (the JS rendering already happened — your input is the rendered output). The `"js_required"` value applies only to live pages where WebFetch returned empty.

Each meeting_object has these EXACT fields:
{{
  "meeting_title": "<plain string>",
  "meeting_date": "YYYY-MM-DD",
  "meeting_time": "H:MM AM/PM" | "HH:MM" | "",
  "meeting_location": "<string or empty>",
  "agenda_url": "<absolute URL or empty>",
  "minutes_url": "<absolute URL or empty>",
  "video_url": "<absolute URL or empty>",
  "meeting_status": "<status string from the page, e.g. 'Cancelled', or empty>",
  "meeting_id": "<source-internal ID or empty>"
}}

Rules:
- ISO date format required (YYYY-MM-DD). Convert from M/D/YYYY or "Month D, YYYY" if needed.
- Empty fields = "" (NEVER null).
- Absolute URLs only — resolve relative URLs against the source URL's origin shown above.
- NEVER hallucinate fields. If unsure, return "" and add a caveat.
- Status text stays verbatim from the page ("Cancelled" / "Final" / etc.).

Critical: your FINAL ASSISTANT MESSAGE must be the raw JSON object — nothing before, nothing after, no triple-backtick fence. The caller parses your final message as JSON directly.

=== PRE-RENDERED HTML BEGINS ===
{html_content}
=== PRE-RENDERED HTML ENDS ===
"""

# Sanity cap on the pre-rendered HTML file size. Typical rendered Legistar
# pages are 50-200 KB; 1 MB is generous headroom. Files beyond this are
# almost certainly the wrong artifact (full HAR dump, archived site export,
# etc.) and embedding them would explode token cost without value.
MAX_HTML_FILE_BYTES = 1_048_576


def load_parser_index_url(city: str) -> Optional[str]:
    """Resolve a city name to its calendar_url via parser_index.json."""
    idx_path = _PARSERS_DIR / "parser_index.json"
    try:
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    entry = idx.get(city)
    if not isinstance(entry, dict):
        return None
    return entry.get("calendar_url")


def build_prompt(city: str, url: str) -> str:
    return PROMPT_TEMPLATE.format(city=city, url=url)


def build_prompt_pre_rendered(city: str, url: str, html_content: str) -> str:
    """Build the Class-B (pre-rendered HTML) prompt.

    The HTML is embedded directly in the prompt because the agent's
    settings.json grants only WebFetch (no Read of arbitrary files).
    Caller is responsible for honoring `MAX_HTML_FILE_BYTES` before reading
    the file — this function trusts the input it receives.
    """
    return PROMPT_TEMPLATE_PRE_RENDERED.format(
        city=city, url=url, html_content=html_content,
    )


def load_pre_rendered_html(path: Path) -> str:
    """Read a pre-rendered HTML file with sanity gates. Raises ValueError on
    missing / empty / oversized files so the wrapper can return a clean
    infrastructure-error exit code without invoking the agent."""
    if not path.is_file():
        raise ValueError(f"--html-file path not found or not a regular file: {path}")
    size = path.stat().st_size
    if size == 0:
        raise ValueError(f"--html-file is empty: {path}")
    if size > MAX_HTML_FILE_BYTES:
        raise ValueError(
            f"--html-file size {size} bytes exceeds {MAX_HTML_FILE_BYTES}-byte cap "
            f"(MAX_HTML_FILE_BYTES). Almost certainly the wrong artifact."
        )
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ValueError(f"could not read --html-file {path}: {exc}") from exc


# F-2 (2026-06-14 Maricopa validation): rendered Legistar pages embed a
# large amount of Telerik UI markup, scripts, and styles around the actual
# meeting table. Embedding the full page exceeded both Haiku's 200K-token
# context window AND the Mac relay's prompt cap. Pre-extracting just the
# meeting table preserves all the meeting data while dropping size 3-4x.
#
# Per-vendor selectors: Legistar's calendar lives in `<table class="rgMasterTable">`
# (Telerik RadGrid master table). Other vendors will get their own selectors
# as they're discovered (Granicus, CivicPlus, generic HTML, etc.).
_MEETING_TABLE_SELECTORS = (
    # Vendor: Legistar (Telerik RadGrid)
    {"name": "table", "attrs": {"class": "rgMasterTable"}},
)


def extract_meeting_table_subtree(html: str) -> Tuple[str, Optional[str]]:
    """Try to extract just the meeting-table subtree from rendered HTML.

    Returns `(html_to_use, vendor_label)`. When extraction succeeds, the
    returned HTML is a minimal document wrapping the matched table; the
    vendor label identifies which selector matched (for the audit trail).
    When no known selector matches, returns `(original_html, None)` so the
    wrapper transparently falls back to embedding the full HTML — the
    behavior pre-F-2 — rather than refusing.

    BeautifulSoup is a project dep already (used by `calendar_class_detector`).
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return (html, None)

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return (html, None)

    for selector in _MEETING_TABLE_SELECTORS:
        match = soup.find(selector["name"], selector["attrs"])
        if match is not None:
            cls = selector["attrs"].get("class", "")
            vendor_label = f"{selector['name']}.{cls}" if cls else selector["name"]
            wrapped = (
                "<!DOCTYPE html><html><head><meta charset=utf-8>"
                f"<title>Pre-extracted meeting-table subtree ({vendor_label})</title>"
                "</head><body>"
                + str(match)
                + "</body></html>"
            )
            return (wrapped, vendor_label)
    return (html, None)


# ── S-008 V0 / surface S-4 URL safety ─────────────────────────────────


class HaikuUrlSafetyError(ValueError):
    """Raised when a URL passed to the Haiku scraper fails the safety
    pre-flight (non-https scheme, fence marker, bidi controls, etc.).

    Per `01_Project_Overview/THREAT_MODEL_INPUT_SECURITY.md` surface S-4
    + `01_Project_Overview/S008_INPUT_SECURITY_SPEC.md` chunk 2.7.
    """


_HAIKU_DENY_SCHEMES = ("javascript:", "data:", "file:", "ftp:", "vbscript:")
_HAIKU_FENCE_MARKERS = ("<zspan-content-begin", "<zspan-content-end")
_HAIKU_BIDI_CHARS = frozenset(
    chr(cp) for cp in (
        0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
        0x2066, 0x2067, 0x2068, 0x2069,
    )
)


def assert_haiku_url_safe(url: str) -> None:
    """Reject URLs that don't match the canonical-civic-scraping shape.

    The Haiku agent's settings.json allows `WebFetch`; constraining WHICH
    URL is fetched lives here at the wrapper layer (the SCH's wrapper is
    the operator-controlled gate before the agent ever sees the URL).

    Raises HaikuUrlSafetyError on:
      - empty url
      - non-https scheme (http allowed only when explicitly opted in)
      - blocked scheme prefix (javascript:, data:, file:, ftp:, vbscript:)
      - structural fence markers in the URL
      - bidi-control characters in the URL
      - URL longer than 4 KB (almost certainly malicious or buggy)
    """
    if not isinstance(url, str) or not url.strip():
        raise HaikuUrlSafetyError("URL is empty")
    if len(url) > 4_096:
        raise HaikuUrlSafetyError(
            f"URL length {len(url)} exceeds 4096-byte safety cap"
        )
    lowered = url.lower().strip()
    for deny in _HAIKU_DENY_SCHEMES:
        if lowered.startswith(deny):
            raise HaikuUrlSafetyError(
                f"URL scheme {deny!r} is not permitted for Haiku scraping"
            )
    # Allow http:// but warn that civic sites should be https. We do not
    # raise on http to support legacy municipal sites without TLS.
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        raise HaikuUrlSafetyError(
            "URL must start with http:// or https://"
        )
    for marker in _HAIKU_FENCE_MARKERS:
        if marker in lowered:
            raise HaikuUrlSafetyError(
                f"URL contains structural fence marker {marker!r}"
            )
    if any(ch in _HAIKU_BIDI_CHARS for ch in url):
        raise HaikuUrlSafetyError("URL contains bidi control characters")


def tag_meetings_haiku_fallback(meetings: List[Dict]) -> List[Dict]:
    """Stamp scraper_source='haiku_fallback' on each meeting dict.

    Per S-008 V0 / surface S-4: every Haiku-class fallback record is tagged
    so the parser-custodian board surfaces it for operator spot-check.
    The downstream cache_meetings layer preserves the column (added in
    C2.0 SQLite migration).
    """
    tagged: List[Dict] = []
    for m in meetings:
        if isinstance(m, dict):
            cp = dict(m)
            cp.setdefault("scraper_source", "haiku_fallback")
            tagged.append(cp)
        else:
            tagged.append(m)
    return tagged


def _ensure_logs_dir() -> Path:
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return _LOGS_DIR


def _check_claude_cli() -> Optional[str]:
    """Return the resolved `claude` CLI path or None if not on PATH."""
    return shutil.which("claude")


def _single_invoke(claude_path: str, prompt: str, timeout_sec: int) -> Tuple[int, str, str]:
    """One subprocess call. Returns (exit_code, stdout, stderr)."""
    cmd = [
        claude_path, "-p",
        "--model", HAIKU_MODEL_ID,
        "--settings", str(_SETTINGS_PATH),
        "--output-format", "stream-json",
        "--verbose",
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_sec,
            cwd=str(_PARSERS_DIR),
        )
        return (proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired:
        return (2, "", f"claude -p subprocess timed out after {timeout_sec}s")
    except FileNotFoundError:
        return (2, "", "claude CLI not found on PATH")
    except Exception as e:
        return (2, "", f"subprocess error: {type(e).__name__}: {e}")


def invoke_claude(prompt: str, log_path: Path, timeout_sec: int = 180,
                  err: Optional[callable] = None) -> Tuple[int, str, str]:
    """Invoke `claude -p` with auto-retry on the known intermittent startup crash.

    Mirrors the retry shape from `ops/content-scout-heartbeat.ps1`:
    up to 5 attempts, 10-13s delays between, classify exit as crash when
    exit code is the known signature OR (non-zero exit AND <100 stdout bytes).
    On success, writes stream-json output to `log_path`.

    Returns (exit_code, raw_stream_json_stdout, stderr_text) from the last
    attempt (successful or final failure).
    """
    if err is None:
        err = lambda _: None
    if not _SETTINGS_PATH.is_file():
        return (2, "", f"settings file not found: {_SETTINGS_PATH}")
    claude_path = shutil.which("claude")
    if not claude_path:
        return (2, "", "claude CLI not found on PATH")

    last_code, last_out, last_err = 2, "", "no attempts run"
    for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
        if attempt > 1:
            delay = RETRY_DELAYS_S[min(attempt - 2, len(RETRY_DELAYS_S) - 1)]
            err(f"  retrying after {delay}s (attempt {attempt}/{MAX_RETRY_ATTEMPTS}) — last exit {last_code}, {len(last_out)} stdout bytes")
            time.sleep(delay)
        last_code, last_out, last_err = _single_invoke(claude_path, prompt, timeout_sec)
        is_crash = (
            last_code == CRASH_EXIT_CODE
            or (last_code != 0 and len(last_out) < CRASH_BYTE_THRESHOLD)
        )
        if not is_crash:
            break

    try:
        log_path.write_text(last_out, encoding="utf-8")
    except OSError:
        pass
    return (last_code, last_out, last_err)


_FINAL_ASSISTANT_TEXT_RE = re.compile(
    r'"type"\s*:\s*"text"\s*,\s*"text"\s*:\s*"((?:[^"\\]|\\.)*)"',
)


def extract_final_assistant_text(stream_json_stdout: str) -> Optional[str]:
    """Pull the final assistant text from claude -p stream-json output.

    The stream-json format emits one JSON object per line. We want the
    final assistant message's text content. Parse line-by-line + collect
    the most recent assistant text.
    """
    if not stream_json_stdout:
        return None
    last_text: Optional[str] = None
    for line in stream_json_stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Multiple possible shapes — handle the most common ones
        if obj.get("type") == "assistant":
            msg = obj.get("message")
            if isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, list):
                    parts: List[str] = []
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            t = c.get("text")
                            if isinstance(t, str):
                                parts.append(t)
                    if parts:
                        last_text = "".join(parts)
                elif isinstance(content, str):
                    last_text = content
        elif obj.get("type") == "result":
            r = obj.get("result")
            if isinstance(r, str):
                last_text = r
    return last_text


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)


def strip_markdown_fence(text: str) -> str:
    """Strip a ```json ... ``` markdown fence if present (Claude sometimes
    wraps despite instructions)."""
    if not text:
        return text
    m = _JSON_FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


def parse_haiku_response(final_text: str) -> Tuple[Optional[Dict], Optional[str]]:
    """Return (parsed_dict, error_string). On success, error_string is None."""
    if not final_text:
        return (None, "haiku returned no final text")
    cleaned = strip_markdown_fence(final_text)
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError as e:
        return (None, f"haiku response not valid JSON: {e}")
    if not isinstance(obj, dict):
        return (None, f"haiku response not a JSON object (got {type(obj).__name__})")
    required = {"scrape_success", "scrape_method", "meetings_found", "meetings", "caveats", "raw_observations"}
    missing = required - set(obj.keys())
    if missing:
        return (None, f"haiku response missing required fields: {sorted(missing)}")
    return (obj, None)


# F-4b (2026-06-14): post-extraction archive-only signal. F-4a's classifier
# sees only the static default page; for Class-B sites whose static view is
# empty (the Glendale-class shape), it can't read the year. F-4b runs the
# same threshold check against Haiku's extracted `meeting_date` list, where
# the rendered archive content actually surfaces.
_MEETING_DATE_YEAR_RE = re.compile(r"^(\d{4})-\d{2}-\d{2}$")


def compute_archive_only_candidate(
    meetings: List[Dict],
    *,
    threshold_year: Optional[int] = None,
) -> Tuple[Optional[int], bool]:
    """Return ``(latest_meeting_year, archive_only_candidate)``.

    Scans the ISO ``meeting_date`` values in Haiku's extracted meeting list,
    finds the max year, and flags the run as archive-only when that year is
    below ``threshold_year``. Mirrors the F-4a classifier check, applied at
    the wrapper layer so Class-B pages (empty default view) also get the
    signal — they need it most because their dormant-platform shape is what
    motivated F-4 in the first place (see MARICOPA_VALIDATION_2026-06-14.md
    Finding F-4).

    Years outside 1990-2099 are ignored so date-shaped junk (typos, parser
    artifacts) can't pull the latest-year up or down. Returns ``(None, False)``
    when no parseable years are present — caller should interpret that as
    "no signal," not "archive-only."

    When ``threshold_year`` is None, the function still returns the latest
    year but never flags. This matches F-4a's "caller owns the operational
    threshold" pattern and keeps the helper purely a signal-extractor for
    tests + downstream V3 dataset use.
    """
    years: List[int] = []
    for m in meetings:
        if not isinstance(m, dict):
            continue
        date = m.get("meeting_date")
        if not isinstance(date, str):
            continue
        match = _MEETING_DATE_YEAR_RE.match(date.strip())
        if not match:
            continue
        year = int(match.group(1))
        if 1990 <= year <= 2099:
            years.append(year)

    if not years:
        return (None, False)

    latest = max(years)
    if threshold_year is None:
        return (latest, False)
    return (latest, latest < threshold_year)


def normalize_meetings(meetings: List[Dict]) -> List[Dict]:
    """Pass each meeting through normalize.py so downstream code (cache_meetings,
    work-order pipeline) sees the same canonical fields it does from
    deterministic parsers."""
    try:
        from normalize import normalize_meeting_fields
    except ImportError:
        return meetings  # fall back to raw if normalize unavailable

    # Haiku output uses canonical snake_case already; normalize.py expects
    # parser output which sometimes uses "Meeting Title/Name" etc. Pass through
    # via a small adapter so normalize gets what it expects.
    out: List[Dict] = []
    field_map = {
        "meeting_title": "Meeting Title/Name",
        "meeting_date": "Meeting Date",
        "meeting_time": "Meeting Time",
        "meeting_location": "Meeting Location",
        "agenda_url": "Agenda URL",
        "minutes_url": "Minutes URL",
        "video_url": "Video URL",
        "meeting_status": "Meeting Status",
        "meeting_id": "Meeting ID",
    }
    for m in meetings:
        parser_shape = {field_map.get(k, k): v for k, v in m.items()}
        normalized = normalize_meeting_fields(parser_shape)
        out.append(normalized)
    return out


def invoke_via_mac_relay(prompt: str, log_path: Path, timeout_sec: int,
                          err: Optional[callable] = None) -> Tuple[int, str, str]:
    """Dispatch the prompt via the Mac Claude Relay (D-099 migration path).

    The Mac runs claude -p in a stable environment without the PC-side
    0xC0000005 intermittent startup crash. Returns the same shape as
    `invoke_claude` so callers don't need to branch on transport.

    Note: the Mac relay's `/invoke` endpoint doesn't expose `--settings`
    or `--model` to PC callers. It accepts `prompt` + optional
    `allowed_tools`. We pass `["WebFetch"]` since that's the only tool
    the haiku-html-scraper role needs.
    """
    if err is None:
        err = lambda _: None
    try:
        # Lazy import so the script still works on machines without the client
        from mac_claude_relay_client import invoke_mac_claude
    except ImportError as e:
        return (2, "", f"mac_claude_relay_client unavailable: {e}")

    try:
        # D-099 Phase 1: relay now accepts --model and --settings. Pin Haiku
        # 4.5 + apply the scope-locked settings file so the Mac invocation
        # gets the same lane discipline a local PC invocation would.
        result = invoke_mac_claude(
            prompt,
            allowed_tools=["WebFetch"],
            model=HAIKU_MODEL_ID,
            settings="agents/haiku-html-scraper.settings.json",
            # Repo root (this file sits at parsers/scripts/, four levels
            # below it); the relay target is this same machine post-D-111.
            working_dir=str(Path(__file__).resolve().parents[4]),
            timeout_seconds=timeout_sec,
        )
    except Exception as e:
        return (2, "", f"mac relay invocation failed: {type(e).__name__}: {e}")

    exit_code = int(result.get("exit_code", 1))
    text = result.get("text", "") or ""
    stderr = result.get("stderr", "") or ""
    duration = result.get("duration_s", "?")
    err(f"  mac relay duration: {duration}s, stdout {len(text)} chars, exit {exit_code}")
    # Persist the response to the same log_path so the audit trail shape
    # matches local invocations. This is JSON, not stream-json, since the
    # relay returns claude's text output directly.
    try:
        log_path.write_text(json.dumps({
            "transport": "mac_relay",
            "duration_s": duration,
            "exit_code": exit_code,
            "stdout": text,
            "stderr": stderr,
        }, indent=2), encoding="utf-8")
    except OSError:
        pass
    return (exit_code, text, stderr)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="On-demand Haiku-class HTML scraper for a single city.",
    )
    parser.add_argument("--city", required=True, help="City name (must match parser_index.json key).")
    parser.add_argument("--url", default=None, help="Override calendar URL (default: from parser_index.json).")
    parser.add_argument(
        "--html-file",
        default=None,
        type=Path,
        help=(
            "Path to a pre-rendered HTML file (Class-B path per S-036 V1-complete). "
            "When provided, skip WebFetch and embed the HTML in the prompt so "
            "the Haiku agent extracts from the rendered DOM directly. Use for "
            "postback-gated Legistar pages (Glendale canary). Operator obtains "
            "the rendered HTML by driving Chrome MCP / a headless browser to "
            "the page, waiting for the meeting table to populate, then dumping "
            "the rendered HTML. URL safety + rate-limit gates still apply. "
            "By default the wrapper pre-extracts just the meeting-table subtree "
            "(rgMasterTable for Legistar) before embedding — dramatically smaller "
            "prompt without losing meeting data. Pass --no-extract-subtree to "
            "send the raw HTML instead."
        ),
    )
    parser.add_argument(
        "--no-extract-subtree",
        action="store_true",
        help=(
            "Skip pre-extraction of the meeting-table subtree when --html-file "
            "is provided. Default is to extract just <table class='rgMasterTable'> "
            "(or vendor-equivalent) so the prompt fits inside Haiku's context "
            "window and the Mac relay's prompt cap. Use this flag only when "
            "debugging or when the operator knows the raw HTML is needed."
        ),
    )
    parser.add_argument(
        "--via-mac",
        action="store_true",
        help=(
            "Dispatch claude -p via the Mac Claude Relay instead of local "
            "subprocess. Stable substrate (no 0xC0000005 crashes); slightly "
            "slower per network hop. Per D-099, this is the production-target "
            "dispatch path; the local subprocess remains for PC-side testing."
        ),
    )
    parser.add_argument("--no-normalize", action="store_true",
                        help="Skip normalize.py pass — return Haiku's raw schema.")
    parser.add_argument(
        "--no-head-checks",
        action="store_true",
        help=(
            "Skip HEAD-check verification of extracted URLs in the field-"
            "sanity gate. Use for speed-over-verification scenarios or when "
            "the operator has independently confirmed URL validity. Date + "
            "time format sanity still apply regardless."
        ),
    )
    parser.add_argument("--json-only", action="store_true",
                        help="Suppress stderr status lines; emit only the final JSON to stdout.")
    parser.add_argument("--timeout", type=int, default=180, help="claude -p timeout in seconds (default 180).")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    log_to_stderr = not args.json_only

    def err(msg: str) -> None:
        if log_to_stderr:
            print(msg, file=sys.stderr)

    cli_path = _check_claude_cli()
    if not cli_path:
        err("ERROR: 'claude' CLI not on PATH. Install Claude Code CLI.")
        return EXIT_INFRASTRUCTURE_ERROR

    url = args.url or load_parser_index_url(args.city)
    if not url:
        err(f"ERROR: no URL supplied + city {args.city!r} not in parser_index.json")
        return EXIT_INFRASTRUCTURE_ERROR

    # S-008 V0 / surface S-4: URL safety pre-flight before the agent runs.
    try:
        assert_haiku_url_safe(url)
    except HaikuUrlSafetyError as exc:
        err(f"ERROR: URL safety pre-flight rejected: {exc}")
        return EXIT_INFRASTRUCTURE_ERROR

    # D-078 persisted invocation counter (S-036 V1-complete). Refuses if
    # today's count would exceed the daily ceiling, or if the wall-clock
    # cooldown hasn't elapsed since the last invocation. Both ceilings are
    # hardcoded in haiku_rate_limit.py (code-edit only).
    hrl = _load_rate_limit_module()
    try:
        reservation = hrl.check_and_reserve_invocation(city=args.city, url=url)
    except hrl.HaikuRateLimitError as exc:
        err(f"REFUSED ({exc.reason_code}): {exc}")
        return EXIT_RATE_LIMITED

    # Class-B (pre-rendered HTML) path — load + sanity-gate the HTML before
    # building the prompt. Failures here are infrastructure errors; bail
    # before invoking the agent so we don't waste a quota slot.
    pre_rendered_html: Optional[str] = None
    extracted_vendor: Optional[str] = None
    if args.html_file is not None:
        try:
            pre_rendered_html = load_pre_rendered_html(args.html_file)
        except ValueError as exc:
            err(f"ERROR: {exc}")
            return EXIT_INFRASTRUCTURE_ERROR
        # F-2 pre-extraction: drop Telerik UI markup / scripts / styles
        # around the actual meeting table so the prompt fits Haiku's
        # context window AND the Mac relay's prompt cap. Falls back
        # transparently to the raw HTML when no known vendor selector
        # matches (operator gets pre-F-2 behavior unchanged).
        if not args.no_extract_subtree:
            orig_size = len(pre_rendered_html)
            pre_rendered_html, extracted_vendor = extract_meeting_table_subtree(
                pre_rendered_html
            )
            new_size = len(pre_rendered_html)
            if extracted_vendor:
                err(
                    f"  html pre-extract: matched {extracted_vendor}; "
                    f"{orig_size} -> {new_size} bytes "
                    f"({100 * (1 - new_size / orig_size):.0f}% smaller)"
                )
            else:
                err(
                    f"  html pre-extract: no known meeting-table selector "
                    f"matched; embedding raw HTML ({orig_size} bytes)"
                )

    _ensure_logs_dir()
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    safe_city = re.sub(r"[^A-Za-z0-9_.-]+", "_", args.city)
    log_path = _LOGS_DIR / f"{stamp}_{safe_city}.jsonl"

    if pre_rendered_html is not None:
        prompt = build_prompt_pre_rendered(args.city, url, pre_rendered_html)
        mode = "class_b_pre_rendered"
    else:
        prompt = build_prompt(args.city, url)
        mode = "class_a_webfetch"
    transport = "mac_relay" if args.via_mac else "local_subprocess"
    err(f"haiku-html-scraper: invoking on {args.city!r} via {transport} mode={mode} (url={url[:80]})")
    if pre_rendered_html is not None:
        err(f"  html-file: {args.html_file} ({len(pre_rendered_html)} chars)")
    if not args.via_mac:
        err(f"  settings: {_SETTINGS_PATH}")
    err(f"  log: {log_path}")
    err(f"  rate-limit: invocation {reservation.today_count_before_this_call + 1}/{hrl.MAX_INVOCATIONS_PER_DAY} today")

    if args.via_mac:
        exit_code, stdout, stderr_text = invoke_via_mac_relay(
            prompt, log_path, timeout_sec=args.timeout, err=err,
        )
    else:
        exit_code, stdout, stderr_text = invoke_claude(
            prompt, log_path, timeout_sec=args.timeout, err=err,
        )

    # Record the completed invocation regardless of success/failure (both
    # consumed quota). A ledger-write failure here is logged but doesn't
    # block returning the agent's actual extraction result.
    try:
        hrl.record_invocation_complete(
            reservation,
            exit_code=exit_code,
            log_path=log_path,
            city=args.city,
            url=url,
        )
    except Exception as exc:
        err(f"WARNING: failed to record invocation in balance_ledger: {exc}")
    if exit_code != 0:
        err(f"WARNING: claude -p non-zero exit ({exit_code})")
    if stderr_text:
        err(f"  stderr: {stderr_text[:500]}")
    err(f"  stdout bytes: {len(stdout)}, lines: {stdout.count(chr(10))}")
    if not stdout:
        err(f"ERROR: claude -p produced no stdout")
        return EXIT_INFRASTRUCTURE_ERROR

    if args.via_mac:
        # Mac relay returns claude's text output directly (no stream-json).
        final_text = stdout
    else:
        final_text = extract_final_assistant_text(stdout)
    if not final_text:
        err("ERROR: could not extract final assistant text from response")
        return EXIT_INFRASTRUCTURE_ERROR

    obj, parse_err = parse_haiku_response(final_text)
    if obj is None:
        err(f"ERROR: {parse_err}")
        err(f"  raw final text (first 500 chars): {final_text[:500]}")
        return EXIT_INFRASTRUCTURE_ERROR

    # S-036 V1-complete field-sanity output gate: HEAD-check extracted URLs,
    # validate ISO date format, validate plausible time format. Clears
    # fabricated/unverifiable fields BEFORE tagging + normalize so the
    # cache never sees hallucinated values. The sanity report is recorded
    # in `_invocation.sanity_report` for downstream V3 dataset use.
    sanity = _load_field_sanity_module()
    try:
        obj, sanity_report = sanity.apply_field_sanity(
            obj, skip_head_checks=args.no_head_checks,
        )
        sanity_report_dict = sanity_report.to_dict()
        if sanity_report.urls_cleared:
            err(f"  field-sanity: cleared {len(sanity_report.urls_cleared)} URL(s) on HTTP-error verdict")
        if sanity_report.urls_unverified:
            err(f"  field-sanity: {len(sanity_report.urls_unverified)} URL(s) unverified (server didn't speak); kept")
        if sanity_report.dates_cleared:
            err(f"  field-sanity: cleared {len(sanity_report.dates_cleared)} non-ISO date(s)")
        if sanity_report.times_cleared:
            err(f"  field-sanity: cleared {len(sanity_report.times_cleared)} implausible time(s)")
    except Exception as exc:
        err(f"WARNING: field-sanity gate failed: {exc}; emitting raw Haiku output")
        sanity_report_dict = {"error": f"gate raised: {type(exc).__name__}: {exc}"}

    # F-4b post-extraction archive-only signal. Same threshold constant as
    # the F-4a classifier (`ARCHIVE_AGE_THRESHOLD_YEARS`). Runs against
    # Haiku's extracted dates so Class-B pages (whose static view is empty)
    # also get the signal — see compute_archive_only_candidate's docstring.
    archive_threshold = datetime.now().year - ARCHIVE_AGE_THRESHOLD_YEARS
    meetings_for_check = (
        obj["meetings"] if isinstance(obj.get("meetings"), list) else []
    )
    latest_meeting_year, archive_only_candidate = compute_archive_only_candidate(
        meetings_for_check, threshold_year=archive_threshold,
    )
    if archive_only_candidate:
        err(
            f"  archive-only candidate: newest extracted meeting year is "
            f"{latest_meeting_year} (threshold {archive_threshold}); "
            f"likely a migrated platform — operator should review before re-scraping"
        )

    # S-008 V0 / surface S-4: tag every Haiku-extracted record with
    # scraper_source='haiku_fallback' BEFORE normalize so the column
    # survives the normalization pass + lands at cache.db.
    if isinstance(obj.get("meetings"), list):
        obj["meetings"] = tag_meetings_haiku_fallback(obj["meetings"])

    if not args.no_normalize and isinstance(obj.get("meetings"), list):
        try:
            obj["meetings"] = normalize_meetings(obj["meetings"])
            obj["_normalized"] = True
        except Exception as e:
            err(f"WARNING: normalize pass failed: {e}; emitting raw schema")
            obj["_normalized"] = False
    else:
        obj["_normalized"] = False

    obj["_invocation"] = {
        "city": args.city,
        "url": url,
        "mode": mode,
        "transport": transport,
        "html_file": str(args.html_file) if args.html_file else None,
        "html_pre_extracted_vendor": extracted_vendor,
        "log_path": str(log_path),
        "claude_exit_code": exit_code,
        "stamp": stamp,
        "sanity_report": sanity_report_dict,
        "latest_meeting_year": latest_meeting_year,
        "archive_only_candidate": archive_only_candidate,
    }

    print(json.dumps(obj, indent=2))
    return EXIT_OK if obj.get("scrape_success") else EXIT_SCRAPE_FAILED


if __name__ == "__main__":
    sys.exit(main())
