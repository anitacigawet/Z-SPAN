"""The zspan command loop: init / pick / pull / process / open, plus
`providers` (print the matrix).

Any command that lands name-first before it lands functionally stays an
honest stub — it says so and exits non-zero, never silently no-ops.

The terminal output here is deliberately plain: the hologram
choreography lives on the local page the CLI serves at `open`;
the terminal process itself stays an honest log.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from typing import Any, Dict, List, Optional

from zspan_cli import __version__
from zspan_cli import auth as auth_mod
from zspan_cli.config import (
    DEFAULT_FLAGSHIP_URL,
    PROCESSING_ACK_TEXT,
    ConfigError,
    config_path,
    flagship_url,
    has_processing_ack,
    home_jurisdiction,
    key_fingerprint,
    load_config,
    media_dir,
    record_processing_ack,
    save_config,
    save_home_jurisdiction,
    transcripts_dir,
)
from zspan_cli.flagship import (
    FlagshipError,
    fetch_coverage,
    fetch_jurisdictions,
    fetch_meetings,
)
from zspan_cli.providers import (
    DEFAULT_PROVIDER,
    PROVIDERS,
    matrix_lines,
    provider_ids,
)
from zspan_cli.validate import validate_key
from zspan_cli import protocol, resolver, workspace

# Empty — every shipped command is real. The stub machinery stays for any
# future command that lands name-first: a stub says so and exits
# non-zero, never silently no-ops.
_NOT_BUILT_YET: Dict[str, tuple] = {}

# Coverage statuses that read as a working data path today. Everything
# else stays selectable (the flagship serves whatever cache it holds, and
# `pull` reports honestly) but renders dimmed with its status word — the
# operator's "grayed out by what's available" direction, as expectation-
# setting rather than a hard block.
_HEALTHY_STATUSES = {"covered", "monitored"}


def _say(msg: str = "") -> None:
    print(msg)


def _fail(msg: str) -> int:
    print(f"zspan: {msg}", file=sys.stderr)
    return 1


def _dim(text: str) -> str:
    """ANSI-dim when the terminal can take it; plain text otherwise."""
    if sys.stdout.isatty() and (os.name != "nt" or os.environ.get("WT_SESSION") or os.environ.get("TERM")):
        return f"\x1b[2m{text}\x1b[0m"
    return text


def _flagship_url(config: Optional[Dict[str, Any]]) -> str:
    """Env override wins at use time (the local-Flask dev loop), then the
    stored config, then the public default."""
    return flagship_url(config)


def _year_arg(value: str):
    """--year accepts a year number or the literal 'all' (the flagship's
    own ?year=all form for the full catalog)."""
    v = value.strip().lower()
    if v == "all":
        return "all"
    try:
        return int(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a year like 2026, or 'all' — got {value!r}")


# ---------------------------------------------------------------- init


def _read_key_from_flags(args: argparse.Namespace) -> Optional[str]:
    """Key from --key-env or --key-stdin (the automation/test path).
    Never from a bare CLI argument — that would land in shell history."""
    if args.key_env:
        value = os.environ.get(args.key_env, "").strip()
        if not value:
            return None
        return value
    if args.key_stdin:
        line = sys.stdin.readline()
        return line.strip() or None
    return None


def _prompt_key(provider_id: str) -> Optional[str]:
    p = PROVIDERS[provider_id]
    _say(f"Paste your {p['label']} API key (get one: {p['key_url']}).")
    try:
        key = getpass.getpass("API key (input is hidden — paste, then press Enter): ")
    except (EOFError, KeyboardInterrupt):
        return None
    key = (key or "").strip()
    if not key:
        return None
    hint = p.get("key_prefix_hint")
    if hint and not key.startswith(hint):
        _say(f"  note: {p['label']} keys usually start with '{hint}' — continuing anyway.")
    return key


def _validate_interactively(provider_id: str, key: str, assume_yes: bool):
    """Validate with a retry/save-anyway/quit loop.

    Returns (status, final_key, model_ids) where status is "valid",
    "save_anyway", or "abort". The key comes back because the retry path
    can replace it; model_ids is the key's own list-models response —
    the default-strongest-reachable resolution ranks against it.
    Non-interactive callers (assume_yes / no tty) get one attempt."""
    while True:
        _say("Checking the key with a free list-models ping (no tokens consumed)...")
        result = validate_key(provider_id, key)
        if result["valid"]:
            _say(f"  ✓ key {result['fingerprint']} is valid ({result.get('model_count', '?')} models visible).")
            return "valid", key, result.get("model_ids") or []
        _say(f"  ✗ the provider rejected it: {result.get('error', 'unknown error')}")
        if assume_yes or not sys.stdin.isatty():
            return "abort", None, []
        try:
            choice = input("  (r)etry with a new key, (s)ave anyway, or (q)uit? [r/s/q] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "abort", None, []
        if choice == "s":
            return "save_anyway", key, []
        if choice == "q":
            return "abort", None, []
        new_key = _prompt_key(provider_id)
        if new_key is None:
            return "abort", None, []
        key = new_key


def cmd_init(args: argparse.Namespace) -> int:
    try:
        existing = load_config()
    except ConfigError as e:
        return _fail(str(e))

    _say(f"zspan {__version__} — first-run setup")
    _say("")

    if existing:
        prov = existing.get("synthesis_provider", "?")
        keys = existing.get("api_keys") or {}
        fps = ", ".join(f"{p}: {key_fingerprint(k)}" for p, k in keys.items()) or "none"
        _say(f"A config already exists at {config_path()}")
        _say(f"  provider: {prov} · stored keys: {fps}")
        _say("Re-running init updates it; other stored keys are kept.")
        _say("")

    if not args.yes and sys.stdin.isatty() and home_jurisdiction(existing) is None:
        try:
            picked_home = _jurisdiction_drill(existing)
        except FlagshipError as e:
            _say(f"Home city not set: {e} Continuing to provider setup.")
        else:
            if picked_home is None:
                _say("Home city not set — continuing to provider setup.")
            else:
                existing = save_home_jurisdiction(
                    existing,
                    picked_home["state"],
                    picked_home["county"],
                    picked_home["city"],
                )
                _say(f"Home city: {picked_home['city']}, {picked_home['state']}")
                _say("")

    # -- provider choice
    provider_id = (args.provider or "").strip().lower()
    if not provider_id:
        if not sys.stdin.isatty():
            return _fail(
                "no provider chosen and no terminal to ask on. "
                "Run interactively, or pass --provider and --key-env."
            )
        for line in matrix_lines():
            _say(line)
        _say("")
        raw = input(f"Which provider? [{'/'.join(provider_ids())}] (Enter = {DEFAULT_PROVIDER}) ").strip().lower()
        provider_id = raw or DEFAULT_PROVIDER
    if provider_id not in PROVIDERS:
        return _fail(f"unknown provider '{provider_id}'. Supported: {', '.join(provider_ids())}.")

    # -- key
    key = _read_key_from_flags(args)
    if key is None and (args.key_env or args.key_stdin):
        return _fail(
            f"no key found via {'--key-env ' + args.key_env if args.key_env else '--key-stdin'}."
        )
    if key is None:
        if not sys.stdin.isatty():
            return _fail("no key provided. Run interactively, or pass --key-env VAR / --key-stdin.")
        key = _prompt_key(provider_id)
        if key is None:
            return _fail("no key entered — nothing written.")

    # -- validation
    model_ids: List[str] = []
    if not args.skip_validate:
        status, accepted_key, model_ids = _validate_interactively(
            provider_id, key, assume_yes=args.yes)
        if status == "abort":
            return _fail("key did not validate — nothing written. (--skip-validate to store it anyway.)")
        key = accepted_key
        if status == "save_anyway":
            _say("  storing the key unvalidated at your request.")
    else:
        _say("Skipping the validation ping (--skip-validate).")

    # -- assemble + write config (preserve unknown fields + other keys)
    config: Dict[str, Any] = dict(existing) if existing else {}
    api_keys: Dict[str, str] = dict(config.get("api_keys") or {})
    api_keys[provider_id] = key
    config["api_keys"] = api_keys
    config["synthesis_provider"] = provider_id
    if model_ids:
        # The key's own model list — synthesis defaults to the strongest
        # model it actually reaches; `zspan process --model` / config
        # synthesis_model opt down.
        available = dict(config.get("available_models") or {})
        available[provider_id] = model_ids
        config["available_models"] = available
    config["flagship_url"] = (
        args.flagship_url
        or os.environ.get("ZSPAN_FLAGSHIP_URL", "").strip()
        or config.get("flagship_url")
        or DEFAULT_FLAGSHIP_URL
    )

    # Transcription is local-by-default (free), so one key of ANY provider
    # runs the pipeline end-to-end — no second-key pressure here. Adding a
    # key for another provider later is just `zspan init --provider X`.
    path = save_config(config)

    _say("")
    _say("Setup complete.")
    _say(f"  config:   {path}")
    _say(f"  provider: {provider_id}")
    for p, k in api_keys.items():
        _say(f"  key:      {p} {key_fingerprint(k)}")
    _say(f"  endpoint: {config['flagship_url']}")
    if model_ids:
        from zspan_cli.providers import strongest_reachable
        _say(f"  synthesis defaults to {strongest_reachable(provider_id, model_ids)} — "
             "the strongest model this key reaches")
        _say("  (re-run init later to refresh; `zspan process --model X` opts down)")
    _say("")
    _say("Next: `zspan pick` to choose your city.")
    return 0


# ---------------------------------------------------------------- providers


def cmd_providers(_args: argparse.Namespace) -> int:
    for line in matrix_lines():
        _say(line)
    return 0


# ---------------------------------------------------------------- account


def cmd_login(_args: argparse.Namespace) -> int:
    try:
        current_config = load_config()
    except ConfigError as e:
        return _fail(str(e))
    return 0 if auth_mod.login(current_config or {}) else 1


def cmd_logout(_args: argparse.Namespace) -> int:
    try:
        current_config = load_config()
    except ConfigError as e:
        return _fail(str(e))
    auth_mod.logout(current_config)
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    try:
        current_config = load_config()
    except ConfigError as e:
        return _fail(str(e))
    auth_mod.whoami(current_config, verify=args.verify)
    return 0


# ---------------------------------------------------------------- pick


def _city_line(row: Dict[str, Any]) -> str:
    """One coverage row as a sentence fragment — healthy bright, everything
    else dimmed with the registry's own plain status words."""
    status = (row.get("status") or "").strip()
    name = row.get("city") or "?"
    if status in _HEALTHY_STATUSES:
        extra = ""
        if row.get("published_count"):
            extra = f" — {row['published_count']} broadcasts on zspan.org"
        return f"{name} ({status}{extra})"
    return _dim(f"{name} ({status})")


def _choose(prompt: str, options: List[str]) -> Optional[int]:
    """Numbered-list selection. Returns the chosen index, or None on
    quit/EOF. Plain input() — no curses, works everywhere."""
    for i, opt in enumerate(options, 1):
        _say(f"  {i:>3}. {opt}")
    while True:
        try:
            raw = input(f"{prompt} (number, or q to quit) ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if raw in ("q", "quit", ""):
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        _say(f"  Please enter a number between 1 and {len(options)}.")


def _save_pick(config: Optional[Dict[str, Any]], row: Dict[str, Any]) -> None:
    save_home_jurisdiction(
        config,
        row.get("state") or "",
        row.get("county") or "",
        row.get("city") or "",
    )
    _say("")
    _say(f"Picked {row.get('city')}, {row.get('county')}, {row.get('state')} ({row.get('status')}).")
    _say(f"Next: `zspan pull` fetches its meeting catalog into your workspace.")


def _jurisdiction_rows(states: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for state_row in states:
        if not isinstance(state_row, dict):
            continue
        state = state_row.get("state") or ""
        counties = state_row.get("counties")
        if not isinstance(counties, list):
            continue
        for county_row in counties:
            if not isinstance(county_row, dict):
                continue
            county = county_row.get("county") or ""
            cities = county_row.get("cities")
            if not isinstance(cities, list):
                continue
            for city_row in cities:
                if not isinstance(city_row, dict):
                    continue
                rows.append({
                    "state": state,
                    "county": county,
                    "city": city_row.get("city") or "",
                    "meeting_count": city_row.get("meeting_count") or 0,
                    "covered": bool(city_row.get("covered")),
                })
    return rows


def _jurisdiction_city_line(row: Dict[str, Any]) -> str:
    city = row.get("city") or "?"
    if row.get("covered"):
        count = int(row.get("meeting_count") or 0)
        noun = "meeting" if count == 1 else "meetings"
        return f"{city} — {count} {noun}"
    return _dim(f"{city} — not covered yet")


def _jurisdiction_drill(config: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    base_url = _flagship_url(config)
    _say(f"Fetching jurisdictions from {base_url} ...")
    states_payload = fetch_jurisdictions(base_url)
    rows = _jurisdiction_rows(states_payload)
    if not rows:
        raise FlagshipError(
            "the endpoint server answered, but its jurisdiction list is empty."
        )

    states = sorted({row["state"] for row in rows})
    idx = _choose(
        "Which state?",
        [f"{state} — {sum(r['state'] == state for r in rows)} cities" for state in states],
    )
    if idx is None:
        return None
    state = states[idx]

    state_rows = [row for row in rows if row["state"] == state]
    counties = sorted({row["county"] for row in state_rows})
    idx = _choose(
        f"Which county in {state}?",
        [f"{county} — {sum(r['county'] == county for r in state_rows)} cities"
         for county in counties],
    )
    if idx is None:
        return None
    county = counties[idx]

    city_rows = sorted(
        (row for row in state_rows if row["county"] == county),
        key=lambda row: row["city"],
    )
    idx = _choose(
        f"Which city in {county}?",
        [_jurisdiction_city_line(row) for row in city_rows],
    )
    return None if idx is None else city_rows[idx]


def cmd_home(args: argparse.Namespace) -> int:
    try:
        current_config = load_config()
    except ConfigError as e:
        return _fail(str(e))

    current = home_jurisdiction(current_config)
    if not args.change and not args.city:
        if current is None:
            _say("none set — `zspan home --change` picks one")
        else:
            _say(f"{current.get('city')}, {current.get('county')}, {current.get('state')}")
        return 0

    try:
        if args.city:
            states = fetch_jurisdictions(_flagship_url(current_config))
            wanted = args.city.strip().lower()
            matches = [
                row for row in _jurisdiction_rows(states)
                if row["city"].strip().lower() == wanted
            ]
            if not matches:
                return _fail(f"'{args.city}' isn't in the public jurisdiction list.")
            if len(matches) > 1:
                places = ", ".join(
                    f"{row['city']}, {row['county']}, {row['state']}" for row in matches
                )
                return _fail(f"'{args.city}' is ambiguous — matches: {places}.")
            picked = matches[0]
        else:
            if not sys.stdin.isatty():
                return _fail("no terminal to choose on. Use `zspan home --city <name>`.")
            picked = _jurisdiction_drill(current_config)
            if picked is None:
                return _fail("home city unchanged.")
    except FlagshipError as e:
        return _fail(str(e))

    save_home_jurisdiction(
        current_config, picked["state"], picked["county"], picked["city"]
    )
    _say(f"Home city: {picked['city']}, {picked['county']}, {picked['state']}")
    return 0


def cmd_pick(args: argparse.Namespace) -> int:
    try:
        config = load_config()
    except ConfigError as e:
        return _fail(str(e))

    base_url = _flagship_url(config)
    _say(f"Fetching live coverage from {base_url} ...")
    try:
        coverage = fetch_coverage(base_url)
    except FlagshipError as e:
        return _fail(str(e))
    if not coverage:
        return _fail(
            "the endpoint server answered, but its coverage list is empty — "
            "nothing to pick from right now."
        )
    _say(f"  {len(coverage)} cities listed.")
    _say("")

    # -- direct form: zspan pick --city Kingman
    if args.city:
        wanted = args.city.strip().lower()
        matches = [r for r in coverage if (r.get("city") or "").strip().lower() == wanted]
        if not matches:
            return _fail(
                f"'{args.city}' is not in the live coverage list. "
                f"Run `zspan pick --list` to see what's available."
            )
        _save_pick(config, matches[0])
        return 0

    # -- group state → county → city
    by_state: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for row in coverage:
        state = row.get("state") or "?"
        county = row.get("county") or "?"
        by_state.setdefault(state, {}).setdefault(county, []).append(row)

    # -- list-only form
    if args.list:
        for state in sorted(by_state):
            n = sum(len(v) for v in by_state[state].values())
            _say(f"{state} — {n} cities")
            for county in sorted(by_state[state]):
                _say(f"  {county}")
                for row in sorted(by_state[state][county], key=lambda r: r.get("city") or ""):
                    _say(f"    {_city_line(row)}")
        return 0

    if not sys.stdin.isatty():
        return _fail("no terminal to choose on. Use `zspan pick --city <name>` or `zspan pick --list`.")

    # -- interactive drill
    states = sorted(by_state)
    idx = _choose(
        "Which state?",
        [f"{s} — {sum(len(v) for v in by_state[s].values())} cities" for s in states],
    )
    if idx is None:
        return _fail("nothing picked.")
    state = states[idx]

    counties = sorted(by_state[state])
    idx = _choose(
        f"Which county in {state}?",
        [f"{c} — {len(by_state[state][c])} cities" for c in counties],
    )
    if idx is None:
        return _fail("nothing picked.")
    county = counties[idx]

    rows = sorted(by_state[state][county], key=lambda r: r.get("city") or "")
    idx = _choose(f"Which city in {county}?", [_city_line(r) for r in rows])
    if idx is None:
        return _fail("nothing picked.")

    _save_pick(config, rows[idx])
    return 0


# ---------------------------------------------------------------- pull


def _pull_state(args: argparse.Namespace, config) -> int:
    """`zspan pull --state az` — mirror EVERY covered city of a state
    into the workspace, one catalog fetch per city, per-city honesty
    kept compact (a whole-state sweep can't print eight lines each).
    One flaky city never kills the sweep; failures are counted and
    named at the end."""
    state = (args.state or "").strip().lower()
    base_url = _flagship_url(config)
    _say(f"Fetching the coverage list from {base_url} ...")
    try:
        coverage = fetch_coverage(base_url)
    except FlagshipError as e:
        return _fail(str(e))

    state_rows = [
        r for r in coverage
        if (r.get("state") or "").strip().lower() == state
        and (r.get("city") or "").strip()
    ]
    if not state_rows:
        states = sorted({
            (r.get("state") or "").strip().lower()
            for r in coverage if (r.get("state") or "").strip()
        })
        return _fail(
            f"no covered cities carry the state code '{state}' — "
            f"the coverage list knows: {', '.join(states) or '(none)'}."
        )

    # The public catalog serves PUBLISHED meetings only, so cities with no
    # published broadcasts answer empty by design — skip them upfront
    # rather than sweeping 80+ fetches that can only return nothing.
    cities = sorted(
        (r.get("city") or "").strip()
        for r in state_rows
        if (r.get("published_count") or 0)
    )
    unpublished = len(state_rows) - len(cities)
    if not cities:
        return _fail(
            f"none of the {len(state_rows)} covered {state.upper()} cities "
            "carry published meetings yet — the public catalog serves "
            "published meetings only, and it grows as Z-SPAN publishes more."
        )

    year_note = f" ({args.year})" if args.year else ""
    _say(f"Pulling {len(cities)} {state.upper()} city catalogs{year_note} — "
         "this is your machine mirroring the public catalog, one city at a time.")
    if unpublished:
        _say(f"  ({unpublished} covered cities have no published meetings yet — "
             "skipped; the public catalog serves published meetings only.)")
    _say("")

    total_events = total_new = 0
    empty_cities: list[str] = []
    failed: list[str] = []
    conn = workspace.connect()
    try:
        for i, city in enumerate(cities):
            if i:
                # Anti-bulk pacing: the sweep talks to OUR server,
                # but 88 back-to-back fetches is still a burst shape —
                # a small jittered gap keeps any one client's sweep
                # polite at the flagship's scale. Rate limiting on the
                # server is the actual wall; this is the client doing its part.
                import random
                import time
                time.sleep(0.3 + random.random() * 0.4)
            try:
                data = fetch_meetings(base_url, city, year=args.year)
            except FlagshipError:
                failed.append(city)
                _say(f"  {city:<24} ✗ fetch failed — skipped")
                continue
            events = data.get("events") or []
            new = 0
            for row in events:
                if workspace.upsert_meeting(conn, row) == "new":
                    new += 1
            conn.commit()
            total_events += len(events)
            total_new += new
            if not events:
                empty_cities.append(city)
            else:
                _say(f"  {city:<24} {len(events):>4} meetings ({new} new)")
    finally:
        conn.close()

    _say("")
    _say(f"Done: {total_events} meetings across {len(cities) - len(empty_cities) - len(failed)} "
         f"cities ({total_new} new) → {workspace.workspace_path()}")
    if empty_cities:
        # F8: succeeded-empty said plainly — these published cities
        # answered empty, usually the default current-year filter
        # excluding older published meetings.
        _say(f"  {len(empty_cities)} cities answered empty for this year's filter "
             f"(`--year all` pulls their full published catalog): "
             f"{', '.join(empty_cities[:8])}"
             + (" …" if len(empty_cities) > 8 else ""))
    if failed:
        _say(f"  {len(failed)} cities failed to fetch: {', '.join(failed)} — re-run to retry.")
    _say("")
    _say("`zspan open` now shows the whole state in Channels; any meeting "
         "with a video source takes Process.")
    return 1 if failed and not total_events else 0


def cmd_pull(args: argparse.Namespace) -> int:
    try:
        config = load_config()
    except ConfigError as e:
        return _fail(str(e))

    if getattr(args, "state", None):
        if (args.city or "").strip():
            return _fail("--state pulls every covered city in the state — "
                         "drop the city argument, or drop --state.")
        return _pull_state(args, config)

    city = (args.city or "").strip()
    if not city:
        picked = home_jurisdiction(config) or {}
        city = (picked.get("city") or "").strip()
    if not city:
        return _fail("no city named and none picked yet — run `zspan pick` first, or `zspan pull <city>`.")

    base_url = _flagship_url(config)
    _say(f"Pulling the {city} meeting catalog from {base_url} ...")
    try:
        data = fetch_meetings(base_url, city, year=args.year)
    except FlagshipError as e:
        return _fail(str(e))

    events = data.get("events") or []
    conn = workspace.connect()
    try:
        new = updated = 0
        for row in events:
            verdict = workspace.upsert_meeting(conn, row)
            if verdict == "new":
                new += 1
            else:
                updated += 1
        conn.commit()
        total, latest = workspace.pull_stats(conn, city)
    finally:
        conn.close()

    # F8 honesty: succeeded-empty is said plainly, never dressed as success —
    # and the three empty cases get their own true sentences: a city that
    # isn't in coverage at all (typo / not served) vs a covered city with
    # no PUBLISHED meetings (the public catalog serves published-only)
    # vs a published city whose published meetings fall outside this
    # year's filter.
    year_note = f" for {args.year}" if args.year else ""
    if not events:
        cov_row = None
        cov_known = None
        try:
            wanted = city.strip().lower()
            cov_row = next(
                (r for r in fetch_coverage(base_url)
                 if (r.get("city") or "").strip().lower() == wanted),
                None,
            )
            cov_known = cov_row is not None
        except FlagshipError:
            pass  # coverage unreachable — fall through to the generic empty message
        if cov_known is False:
            _say(f"'{city}' isn't in the live coverage list — check the spelling,")
            _say("or run `zspan pick --list` to see the cities Z-SPAN serves.")
            return 1
        if cov_row is not None and not (cov_row.get("published_count") or 0):
            _say(f"The public catalog serves meetings Z-SPAN has published, and {city} has none yet.")
            _say("`zspan pick --list` shows which cities carry published broadcasts today —")
            _say("coverage grows as the flagship publishes more of Arizona.")
            return 0
        _say(f"The endpoint server answered, but serves no published {city} meetings{year_note}.")
        _say("Meetings enter the public catalog when their broadcast publishes —")
        _say("`--year all` pulls the full published catalog if this year's filter is the cause.")
        return 0

    _say(f"  {len(events)} meetings received — {new} new, {updated} refreshed.")
    if data.get("last_scraped"):
        staleness = " (the flagship marks this cache as stale — a fresher scrape may add more)" if data.get("is_stale") else ""
        _say(f"  catalog last scraped by the flagship: {data['last_scraped']}{staleness}")
    _say(f"  workspace now holds {total} {city} meetings · most recent {latest}")
    _say(f"  workspace file: {workspace.workspace_path()}")
    _say("")

    recent = sorted(events, key=lambda r: r.get("meeting_date") or "", reverse=True)[:8]
    for row in recent:
        video = "▸ video" if row.get("video_url") else "  no video yet"
        _say(f"  {row.get('meeting_date', '????-??-??')}  {video}   {row.get('meeting_title', '(untitled)')}")
    if len(events) > len(recent):
        _say(f"  ... and {len(events) - len(recent)} more in the workspace.")
    _say("")
    _say("Next: `zspan process` transcribes + synthesizes one of these locally.")
    return 0


# ---------------------------------------------------------------- process


def _parse_meeting_target(value: str):
    raw = (value or "").strip()
    try:
        scheme_public_id = protocol.parse_scheme_url(raw)
    except protocol.ProtocolError as e:
        raise ValueError(str(e)) from e
    if scheme_public_id is not None:
        return "public", scheme_public_id
    if raw.isdigit():
        return "local", int(raw)
    if resolver.looks_like_public_id(raw):
        return "public", raw
    raise ValueError(
        "a meeting target must be a numeric local id or a public id like "
        "m_QKQR6sGF6WP5koWphY4zBs copied from zspan.org, including its "
        "zspan://meeting/… link form."
    )


def cmd_register_protocol(args: argparse.Namespace) -> int:
    try:
        summary = protocol.unregister() if args.remove else protocol.register()
    except protocol.ProtocolError as e:
        return _fail(str(e))
    _say(summary)
    if not args.remove:
        _say("Uninstall with `zspan register-protocol --remove`.")
    return 0


def _prompt_processing_ack(config: Dict[str, Any]) -> tuple[Dict[str, Any], bool]:
    _say(PROCESSING_ACK_TEXT)
    try:
        answer = input(
            "Acknowledge to enable processing on this machine? [y/N] "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return config, False
    if answer not in ("y", "yes"):
        return config, False
    updated = record_processing_ack(config)
    return updated, True


def _pick_process_target(
    conn, config: Dict[str, Any], meeting_id: Optional[int],
):
    """The meeting row to process, or (None, honest-reason). The empty
    cases each get their own true sentence (F8)."""
    if meeting_id is not None:
        row = workspace.get_meeting(conn, meeting_id)
        if row is None:
            return None, (
                f"meeting {meeting_id} isn't in your workspace — `zspan pull` "
                "fetches your home city's catalog, or copy a `zspan open m_…` "
                "command from zspan.org."
            )
        return row, None

    picked = home_jurisdiction(config) or {}
    city = (picked.get("city") or "").strip()
    if not city:
        return None, "no city picked yet — run `zspan pick` first."

    row = workspace.pick_processable(conn, city)
    if row is not None:
        return row, None

    total, _latest = workspace.pull_stats(conn, city)
    if total == 0:
        return None, f"your workspace holds no {city} meetings — run `zspan pull` first."
    with_video = conn.execute(
        "SELECT COUNT(*) AS n FROM meetings WHERE city = ? "
        "AND video_url IS NOT NULL AND video_url != ''",
        (city,),
    ).fetchone()["n"]
    if with_video == 0:
        return None, (
            f"none of the {total} {city} meetings in your workspace carry a "
            "video source — there's nothing this build can transcribe. "
            "(The flagship resolves more sources over time; re-pull later.)"
        )
    return None, (
        f"every {city} meeting with a video source is already processed — "
        "`zspan process <meeting-id> --force` re-synthesizes one, or pull a "
        "different year."
    )


def cmd_process(args: argparse.Namespace) -> int:
    try:
        config = load_config()
    except ConfigError as e:
        return _fail(str(e))
    # No hard init gate: the pipeline's resolve_synthesis_setup falls back to
    # the keyless Codex CLI when there's no stored key (local whisper + codex =
    # a fully keyless run, so the demo works with zero `zspan init`). A genuine
    # "no engine at all" case surfaces from the pipeline below with an
    # actionable message, caught by the except below.
    config = config or {}

    row = None
    local_meeting_id: Optional[int] = None
    if args.meeting is not None:
        try:
            target_kind, target = _parse_meeting_target(args.meeting)
        except ValueError as e:
            return _fail(str(e))
        if target_kind == "public":
            try:
                row = resolver.resolve_and_import(target, config, say=_say)
            except (FlagshipError, resolver.ResolveError) as e:
                return _fail(str(e))
        else:
            local_meeting_id = target

    if row is None:
        conn = workspace.connect()
        try:
            row, reason = _pick_process_target(conn, config, local_meeting_id)
        finally:
            conn.close()
        if row is None:
            return _fail(reason)

    if not has_processing_ack(config):
        if not sys.stdin.isatty():
            return _fail(
                PROCESSING_ACK_TEXT
                + " Run `zspan process` interactively once to acknowledge."
            )
        config, accepted = _prompt_processing_ack(config)
        if not accepted:
            return _fail("local processing was not acknowledged — nothing was processed.")

    from zspan_cli import processing

    try:
        result = processing.run_pipeline(
            int(row["id"]),
            config=config,
            progress=lambda m: _say(f"  {m}"),
            model_override=args.model or "",
            whisper_model=args.whisper_model,
            cloud_transcribe=args.cloud_transcribe,
            keep_media=args.keep_media,
            force=args.force,
            yes_to_all=args.yes_to_all,
        )
    except processing.pipeline_error_types() as e:
        return _fail(str(e))

    _say("")
    _say(f"  workspace: {workspace.workspace_path()}")
    _say("  `zspan open` serves it as the site.")
    return 0 if result["ok"] else 1


# ---------------------------------------------------------------- open


def cmd_open(args: argparse.Namespace) -> int:
    try:
        current_config = load_config()
    except ConfigError as e:
        return _fail(str(e))
    current_config = current_config or {}

    imported = False
    meeting_id: Optional[int] = None
    imported_row = None
    if args.meeting is not None:
        try:
            target_kind, target = _parse_meeting_target(args.meeting)
        except ValueError as e:
            return _fail(str(e))
        if target_kind == "public":
            try:
                imported_row = resolver.resolve_and_import(
                    target, current_config, say=_say
                )
            except (FlagshipError, resolver.ResolveError) as e:
                return _fail(str(e))
            imported = True
            meeting_id = int(imported_row["id"])
            state = "processed" if imported_row["processed_at"] else "not processed"
            _say(
                f"Imported {imported_row['title'] or '(untitled)'} — "
                f"{imported_row['city']} · {imported_row['meeting_date']} ({state})."
            )
            if not imported_row["processed_at"]:
                source = json.loads(imported_row["source_row_json"])
                local = source.get("local_processing") or {}
                if not imported_row["video_url"]:
                    _say(
                        "Its record page will open, but the catalog supplied no "
                        "safe video source, so this build cannot process it."
                    )
                elif local.get("status") != "ready":
                    kind = local.get("source_kind") or "unknown"
                    _say(
                        f"Its record page will open, but its {kind} source is not "
                        "processable by this build."
                    )
                else:
                    _say(
                        "Its record page will open; it is not processed yet, and "
                        "Process remains your choice."
                    )
        else:
            meeting_id = target

    conn = workspace.connect()
    try:
        rows = workspace.all_meetings(conn)
        if meeting_id is not None and workspace.get_meeting(conn, meeting_id) is None:
            return _fail(
                f"meeting {meeting_id} isn't in your workspace — `zspan pull` "
                "fetches your home city, or copy a `zspan open m_…` command "
                "from zspan.org."
            )
    finally:
        conn.close()
    if not rows:
        return _fail(
            "your workspace holds no meetings — run `zspan pull`, or copy a "
            "`zspan open m_…` command from a meeting card on zspan.org."
        )

    if imported and home_jurisdiction(current_config) is None and sys.stdin.isatty():
        try:
            answer = input(
                f"Make {imported_row['city']}, {imported_row['state']} your home city? [Y/n] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer in ("", "y", "yes"):
            current_config = save_home_jurisdiction(
                current_config,
                imported_row["state"] or "",
                imported_row["county"] or "",
                imported_row["city"] or "",
            )

    if not has_processing_ack(current_config) and sys.stdin.isatty():
        current_config, accepted = _prompt_processing_ack(current_config)
        if not accepted:
            _say("Processing remains disabled; viewing the workspace needs no acknowledgment.")

    from zspan_cli import serve

    # The pip form has no repo clone, so no dist/public — offer the
    # release bundle (consent-first, size stated) so `open` serves the
    # real site instead of the lean fallback.
    if serve.resolve_webapp_dir() is None:
        from zspan_cli import bundle as bundle_mod

        wants = bool(getattr(args, "fetch_bundle", False))
        if not wants and sys.stdin.isatty():
            _say("The full Z-SPAN site bundle isn't on this machine — without it,")
            _say("`open` serves a lean fallback view instead of the real site.")
            answer = input(
                f"Download it now (~{bundle_mod.BUNDLE_SIZE_MB} MB, one time)? [Y/n] "
            ).strip().lower()
            wants = answer in ("", "y", "yes")
        if wants:
            try:
                bundle_mod.fetch_bundle(say=_say)
            except bundle_mod.BundleError as e:
                _say(f"Bundle download skipped: {e}")
                _say("Continuing with the lean fallback view.")

    # The hologram boot — the operator's materialization, in the terminal
    # itself (boot.py). Columns render, the ocean waves hold the home
    # lines' positions while the real server starts, the waves splash
    # into the resolved home, THEN the browser opens onto a ready site.
    # Non-TTY/dumb terminals get the same facts as plain lines.
    from zspan_cli import boot as boot_mod

    tboot = boot_mod.TerminalBoot()
    try:
        server, url = tboot.step(
            "your local server",
            lambda: serve.open_workspace(
                meeting_id, port=args.port, open_browser=False,
                say=tboot.say,
            ),
        )
    except KeyboardInterrupt:
        _say("")
        _say("Stopped. Your workspace is untouched — `zspan open` brings it back.")
        return 0
    except OSError as e:
        return _fail(f"could not start the local server: {e}")

    lean = serve.resolve_webapp_dir() is None
    # The resolved home, per final.png's intentional order + colors: the
    # disclaimer FIRST (white frame, red core — the gate's sentence,
    # verbatim), then the URL in teal, the GitHub link in indigo, quiet
    # grey status under them.
    processed_count = sum(
        bool(r["processed_at"]) or int(r["output_count"] or 0) > 0 for r in rows
    )
    if processed_count == len(rows):
        status_line = (
            f"{len(rows)} processed meeting{'s' if len(rows) != 1 else ''} "
            "ready · private intake complete"
        )
    else:
        status_line = (
            f"{len(rows)} meetings · {processed_count} processed · "
            "required contributions sent"
        )
    home_lines = [
        ("spans", boot_mod.DISCLAIMER_SPANS),
        ("teal", f"Your workspace → {url}"),
        ("teal", "Z-SPAN source → github.com/anitacigawet/Z-SPAN"),
        # Support link belongs on the home screen, alongside the
        # workspace link — visible, not buried.
        ("indigo", "Support the work → ko-fi.com/zspan"),
        ("grey", status_line),
    ]
    if lean:
        home_lines.append(
            ("grey", "lean fallback view — `zspan open --fetch-bundle` "
                     "gets the full site"))
    home_lines.append(("grey", "Ctrl-C stops the server"))
    try:
        tboot.finish("Z-SPAN: connected to local workspace", home_lines)
    except KeyboardInterrupt:
        _say("")
        _say("Stopped. Your workspace is untouched — `zspan open` brings it back.")
        server.shutdown()
        return 0

    if not args.no_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        import threading
        threading.Event().wait()  # serve until Ctrl-C
    except KeyboardInterrupt:
        _say("")
        _say("Stopped. Your workspace is untouched — `zspan open` brings it back.")
        server.shutdown()
    return 0


# ---------------------------------------------------------------- stubs


def _make_stub(name: str):
    chunk, what = _NOT_BUILT_YET[name]

    def _stub(_args: argparse.Namespace) -> int:
        return _fail(f"`zspan {name}` is not in this build yet — it arrives with {chunk} ({what}).")

    return _stub


# ---------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zspan",
        description=(
            "Process your city's public meetings on your own computer, "
            "with your own AI key. Your key and raw media stay local; completed "
            "work goes to Z-SPAN's private intake for review."
        ),
    )
    parser.add_argument("--version", action="version", version=f"zspan-cli {__version__}")
    sub = parser.add_subparsers(dest="command")

    init_p = sub.add_parser("init", help="paste + validate your API key; writes ~/.zspan/config.json")
    init_p.add_argument("--provider", choices=provider_ids(), help="synthesis provider (skips the interactive pick)")
    init_p.add_argument("--key-env", metavar="VAR", help="read the API key from this environment variable")
    init_p.add_argument("--key-stdin", action="store_true", help="read the API key from the first line of stdin")
    init_p.add_argument("--flagship-url", help=f"Z-SPAN endpoint server (default {DEFAULT_FLAGSHIP_URL})")
    init_p.add_argument("--skip-validate", action="store_true", help="store the key without the validation ping")
    init_p.add_argument("-y", "--yes", action="store_true", help="non-interactive: no prompts, fail instead of asking")
    init_p.set_defaults(func=cmd_init)

    prov_p = sub.add_parser("providers", help="show the provider matrix (what each key unlocks)")
    prov_p.set_defaults(func=cmd_providers)

    login_p = sub.add_parser(
        "login", help="sign in with Google so generated outputs can be registered"
    )
    login_p.set_defaults(func=cmd_login)

    logout_p = sub.add_parser(
        "logout", help="revoke this CLI sign-in and remove it from this computer"
    )
    logout_p.set_defaults(func=cmd_logout)

    whoami_p = sub.add_parser(
        "whoami", help="show the Google account tied to generation registration"
    )
    whoami_p.add_argument(
        "--verify", action="store_true", help="check the stored sign-in with zspan.org"
    )
    whoami_p.set_defaults(func=cmd_whoami)

    pick_p = sub.add_parser("pick", help="choose your city from the live coverage list")
    pick_p.add_argument("--city", help="set the city directly, skipping the interactive drill")
    pick_p.add_argument("--list", action="store_true", help="print the full coverage tree and exit")
    pick_p.set_defaults(func=cmd_pick)

    home_p = sub.add_parser("home", help="show or change your home jurisdiction")
    home_p.add_argument(
        "--change", action="store_true", help="choose state, county, and city"
    )
    home_p.add_argument(
        "--city", help="set a uniquely named city directly from the public catalog"
    )
    home_p.set_defaults(func=cmd_home)

    pull_p = sub.add_parser("pull", help="fetch a city's meeting catalog into your local workspace")
    pull_p.add_argument("city", nargs="?", help="city name (default: the one you picked)")
    pull_p.add_argument(
        "--year",
        type=_year_arg,
        help="catalog year (default: the current year), or 'all' for the full catalog",
    )
    pull_p.add_argument(
        "--state",
        help="pull EVERY covered city in a state instead (two-letter code, "
             "e.g. az) — the whole-state channels tree in one sweep",
    )
    pull_p.set_defaults(func=cmd_pull)

    proc_p = sub.add_parser(
        "process",
        help="process one meeting locally: fetch video → transcribe (free, local) "
             "→ index → synthesize with your key → private intake",
    )
    proc_p.add_argument(
        "meeting", nargs="?",
        help="numeric local id, public m_… id, or zspan://meeting/… link "
             "(default: newest unprocessed with video)",
    )
    proc_p.add_argument(
        "--model",
        help="synthesis model override (default: your provider's standard model)",
    )
    proc_p.add_argument(
        "--whisper-model", default="small.en", metavar="SIZE",
        help="local Whisper size: tiny.en/base.en/small.en/medium (default small.en; "
             "smaller = faster, rougher)",
    )
    proc_p.add_argument(
        "--cloud-transcribe", action="store_true",
        help="transcribe via OpenAI whisper-1 instead of locally (~$0.36/hour of "
             "audio on your key; needs ffmpeg). Speed opt-in — never required",
    )
    proc_p.add_argument(
        "--force", action="store_true",
        help="re-synthesize outputs that already exist (the transcript is always "
             "reused; delete its file under ~/.zspan/transcripts to redo it)",
    )
    proc_p.add_argument(
        "--yes-to-all", "-y",
        action="store_true",
        dest="yes_to_all",
        help=(
            "skip the per-chunk approval prompt and synthesize all chunks "
            "without asking. Equivalent to setting ZSPAN_SKIP_APPROVALS=1. "
            "Use in CI or when you already trust the current prompt set."
        ),
    )
    proc_p.add_argument(
        "--keep-media", action="store_true",
        help="keep the downloaded audio/video file after transcription",
    )
    proc_p.set_defaults(func=cmd_process)

    open_p = sub.add_parser(
        "open",
        help="view your private workspace in the browser, rendered the way "
             "zspan.org presents broadcasts (served from your computer)",
    )
    open_p.add_argument(
        "meeting", nargs="?",
        help="numeric local id, public m_… id, or zspan://meeting/… link "
             "(default: workspace index)",
    )
    open_p.add_argument(
        "--port", type=int, default=0,
        help="local port (default: an OS-assigned free port)",
    )
    open_p.add_argument(
        "--no-browser", action="store_true",
        help="serve without auto-opening the browser (prints the URL)",
    )
    open_p.add_argument(
        "--fetch-bundle", action="store_true",
        help="download the full zspan.org site bundle (~176 MB, one time, "
             "SHA256-verified) when it isn't on this machine, instead of "
             "being asked",
    )
    open_p.set_defaults(func=cmd_open)

    protocol_p = sub.add_parser(
        "register-protocol",
        help="opt in to opening zspan:// links with this CLI",
        description=(
            "Opt in to zspan:// link handling for the current user. Writes a "
            "handler app under ~/.zspan on macOS, an HKCU registry key on "
            "Windows, or a user desktop entry on Linux."
        ),
    )
    protocol_p.add_argument(
        "--remove",
        action="store_true",
        help="remove this build's per-user protocol handler artifact",
    )
    protocol_p.set_defaults(func=cmd_register_protocol)

    for name in _NOT_BUILT_YET:
        chunk, what = _NOT_BUILT_YET[name]
        stub_p = sub.add_parser(name, help=f"{what} (arrives with {chunk})")
        stub_p.add_argument("args", nargs="*", help=argparse.SUPPRESS)
        stub_p.set_defaults(func=_make_stub(name))

    return parser


def main(argv: Optional[list] = None) -> int:
    # Windows' default console codec (cp1252) can't encode the Unicode the
    # CLI's help + output uses (→, —, ✓, …), so `zspan --help` crashed there
    # with UnicodeEncodeError — surfaced by RR-CI's windows-latest cells.
    # Force UTF-8 on both streams so the CLI behaves identically on
    # Windows / Linux / macOS. Best-effort: a redirected/wrapped stream may
    # not be reconfigurable, in which case we leave it as-is.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
