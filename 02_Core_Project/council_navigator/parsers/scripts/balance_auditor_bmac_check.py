#!/usr/bin/env python3.11
"""balance_auditor_bmac_check -- ingest Buy Me a Coffee supporter payments
into the balance_ledger as API-sourced deposits.

The donations leg of the Balance Auditor (agents/balance-auditor.md). The
original V1 ledger had exactly one money-in path: the operator manually
records deposits via balance_auditor_record_deposit.py, because OpenAI
exposes no deposit history. Buy Me a Coffee DOES expose one -- its public
API lists every supporter payment -- so donation inflows can be
API-transcribed the same way OpenAI spend is: the agent never *invents*
money-in, it transcribes what the provider's API attests.

GUARDED UNTIL ACTIVATED: this script is inert until `bmac_api_token`
exists in user_settings.json. Pasting the token (operator action, from
the BMAC dashboard's developer/API page) is the activation switch --
same opt-in-by-config pattern as ops/backup_to_proton.sh. Until then
every invocation exits 2 with an honest `skipped_no_token` status.

Ledger semantics:
  - provider="bmac", event_type="deposit_observed", source="bmac_api"
  - amount_cents = GROSS support amount (coffees x coffee_price) -- the
    ledger reflects 1:1 what the supporter put in. BMAC's ~5% platform
    fee + card processing deduct at payout time; record those at payout
    as a manual_correction with notes, so gross stays honest and net is
    reconcilable. (Operator-confirmed direction 2026-07-08.)
  - external_ref = "bmac:support:<support_id>" -- the idempotency key.
    Deposit rows carry NULL bucket times, so the table's UNIQUE
    constraint does not dedupe them; this script dedupes in code by
    external_ref before every insert.
  - Privacy floor: supporter names / notes / emails are NOT written to
    the ledger. Names live on the BMAC dashboard; the ledger stores
    support_id, amount, currency, coffee count, and the BMAC-side
    creation date only.

Subscriptions (recurring members): V0 reports the active-member count in
the summary JSON but does NOT ledger per-cycle payments -- BMAC's public
API exposes subscription records, not a clean per-cycle payment history.
Validate the real response shapes against a live token before extending
(run --dry-run --verbose once the token exists; it prints raw shapes).

API notes (verify at first live run -- written against the documented
public API at developers.buymeacoffee.com, unverifiable without a token):
  GET https://developers.buymeacoffee.com/api/v1/supporters
  GET https://developers.buymeacoffee.com/api/v1/subscriptions?status=active
  Authorization: Bearer <token>; Laravel-style pagination via next_page_url.

Usage:
    python3.11 scripts/balance_auditor_bmac_check.py [--dry-run] [--verbose]
    (run from the parsers/ dir, or via the balance-auditor heartbeat)

Exit codes (family-consistent with balance_auditor_balance_check.py):
    0  ok (including honest-empty: token works, zero supporters yet)
    2  bmac_api_token not configured (inert -- the designed pre-activation state)
    3  BMAC API call failed (configured token, unreachable/rejected API)
    4  response parsed but shape was unrecognized (schema drift -- see --verbose)
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Make parsers/ importable when invoked from cwd=parsers/.
_PARSERS_DIR = Path(__file__).resolve().parent.parent
if str(_PARSERS_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSERS_DIR))

from database import append_ledger_event, get_connection, get_current_balance  # noqa: E402

USER_SETTINGS_PATH = _PARSERS_DIR / "user_settings.json"
TOKEN_FIELD = "bmac_api_token"
BMAC_API_BASE = "https://developers.buymeacoffee.com/api/v1"
HTTP_TIMEOUT_SECONDS = 30
MAX_PAGES = 20  # paginate defensively; ~20 pages covers thousands of supporters


def _load_token() -> Optional[str]:
    """Read bmac_api_token from user_settings.json. Absent/empty -> None."""
    try:
        settings = json.loads(USER_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    token = settings.get(TOKEN_FIELD)
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def _http_get(url: str, token: str) -> dict[str, Any]:
    """GET a BMAC API URL with the Bearer token. Raises on transport errors."""
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "zspan-balance-auditor/1.0",
    })
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _cents(value: Any) -> Optional[int]:
    """Parse a BMAC money value (string or number) into integer cents."""
    if value is None:
        return None
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return None


def _existing_bmac_refs() -> set[str]:
    """All bmac external_refs already in the ledger (the code-level dedupe --
    deposit rows have NULL bucket times, so the UNIQUE constraint can't)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT external_ref FROM balance_ledger "
            "WHERE provider='bmac' AND external_ref IS NOT NULL"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def _walk_supporters(token: str, verbose: bool) -> list[dict[str, Any]]:
    """Fetch all supporter records across pages. Raises urllib errors upward."""
    supporters: list[dict[str, Any]] = []
    url = f"{BMAC_API_BASE}/supporters"
    for _ in range(MAX_PAGES):
        payload = _http_get(url, token)
        if verbose:
            print(f"[verbose] page keys: {sorted(payload.keys())}", file=sys.stderr)
        data = payload.get("data")
        if not isinstance(data, list):
            # Unrecognized shape -- surface for schema-drift diagnosis.
            raise ValueError(f"unrecognized supporters payload shape: keys={sorted(payload.keys())}")
        supporters.extend(d for d in data if isinstance(d, dict))
        next_url = payload.get("next_page_url")
        if not next_url:
            break
        url = next_url
    return supporters


def _count_active_subscriptions(token: str, verbose: bool) -> Optional[int]:
    """Best-effort active-member count. Returns None if the endpoint errors --
    subscriptions are report-only at V0, never worth failing the run over."""
    try:
        payload = _http_get(f"{BMAC_API_BASE}/subscriptions?status=active", token)
        data = payload.get("data")
        if isinstance(data, list):
            return len(data)
        total = payload.get("total")
        return int(total) if isinstance(total, (int, float)) else None
    except Exception as exc:  # noqa: BLE001 -- report-only leg, log + move on
        if verbose:
            print(f"[verbose] subscriptions probe failed (non-fatal): {exc}", file=sys.stderr)
        return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Ingest Buy Me a Coffee supporter payments into balance_ledger "
                    "as API-sourced deposits (guarded until bmac_api_token exists).")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch + report, but write NO ledger rows.")
    ap.add_argument("--verbose", action="store_true",
                    help="print raw response shapes to stderr (first-token validation aid).")
    args = ap.parse_args()

    token = _load_token()
    if not token:
        print(json.dumps({
            "status": "skipped_no_token",
            "detail": f"'{TOKEN_FIELD}' not set in user_settings.json -- the BMAC leg is "
                      "inert until the operator creates the BMAC account and pastes its "
                      "API token (BMAC dashboard -> developer/API). Pasting the token IS "
                      "the activation switch.",
        }, indent=2))
        return 2

    try:
        supporters = _walk_supporters(token, args.verbose)
    except ValueError as exc:
        print(json.dumps({"status": "error", "error_class": "shape", "detail": str(exc)}, indent=2))
        return 4
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
        print(json.dumps({"status": "error", "error_class": "transport", "detail": str(exc)}, indent=2))
        return 3

    known_refs = _existing_bmac_refs()
    appended = 0
    skipped_dup = 0
    skipped_unparseable = 0
    gross_new_cents = 0

    for s in supporters:
        support_id = s.get("support_id")
        if support_id is None:
            skipped_unparseable += 1
            continue
        ref = f"bmac:support:{support_id}"
        if ref in known_refs:
            skipped_dup += 1
            continue

        coffee_price_cents = _cents(s.get("support_coffee_price"))
        coffees = s.get("support_coffees")
        try:
            coffees = int(coffees)
        except (TypeError, ValueError):
            coffees = 1
        if coffee_price_cents is None:
            skipped_unparseable += 1
            continue
        amount_cents = coffee_price_cents * max(coffees, 1)
        currency = str(s.get("support_currency") or "usd").lower()
        created_on = str(s.get("support_created_on") or "")

        # Privacy floor: no payer names, notes, or emails in the ledger.
        note_text = (f"{coffees} coffee(s) via Buy Me a Coffee on {created_on or 'unknown date'} "
                     f"(GROSS -- platform + processing fees deduct at payout)")

        if args.dry_run:
            appended += 1
            gross_new_cents += amount_cents
            continue

        row_id = append_ledger_event(
            provider="bmac",
            event_type="deposit_observed",
            amount_cents=amount_cents,
            currency=currency,
            running_balance_cents=None,
            source="bmac_api",
            notes=note_text,
            external_ref=ref,
        )
        if row_id is not None:
            appended += 1
            gross_new_cents += amount_cents
            known_refs.add(ref)
        else:
            skipped_dup += 1

    active_members = _count_active_subscriptions(token, args.verbose)
    donation_pool_cents = get_current_balance("bmac")

    status = "ok" if supporters else "empty"
    print(json.dumps({
        "status": status,  # F8 discipline: ok / empty are distinct from error paths above
        "dry_run": args.dry_run,
        "supporters_fetched": len(supporters),
        "deposits_appended": appended,
        "skipped_duplicates": skipped_dup,
        "skipped_unparseable": skipped_unparseable,
        "gross_new_cents": gross_new_cents,
        "gross_new_pretty": f"${gross_new_cents / 100:.2f}",
        "donation_pool_cents": donation_pool_cents,
        "donation_pool_pretty": f"${donation_pool_cents / 100:.2f}",
        "active_members_reported": active_members,
        "checked_at": datetime.now(tz=timezone.utc).isoformat(),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
