#!/usr/bin/env bash
# ops/emergency_reprivatize.sh — one-command kill switch to re-privatize zspan.org.
#
# What this does: detaches the reusable "Public Bypass" policy from the
# zspan.org Cloudflare Access application by partially updating the app's
# policies list. Because Access is default-deny, removing the Bypass policy
# immediately restores the Access wall — anonymous visitors get redirected
# to the SSO login page again. Every other attached policy is preserved.
#
# Rollback of the rollback: run this script with --reattach, or manually
# re-attach the policy in the Cloudflare dashboard under Zero Trust →
# Access → Applications → zspan.org → Access policies →
# "Add existing policy" → Public Bypass → Save.
#
# THE INGRESS MANIFEST — every known public door, checked every run.
#
# This script WRITES to one thing: the zspan.org Cloudflare Access
# application (which also fronts z-span.pages.dev — both are in that app's
# self_hosted_domains). Its API token is Access-scoped and deliberately
# cannot mutate Workers or Railway.
#
# But it VERIFIES everything. Checking a public door needs no credentials
# at all — an anonymous GET is exactly what a stranger does. So after any
# action, and on every --check, this script probes each door in
# INGRESS_MANIFEST below and FAILS LOUDLY if any of them answers with
# content. A door this script cannot close is still a door it will not let
# you believe is shut.
#
# This exists because on 2026-07-25 the api-zspan-org Worker was serving
# the full jurisdiction catalog and meeting index anonymously on its
# workers.dev hostnames while zspan.org was correctly walled — and nothing
# would have caught it, because that door was on nobody's list. A kill
# switch that reports success while a door stands open is not a kill
# switch. If a new ingress is ever added, it goes in the manifest in the
# same change.
#
# Usage:
#   ops/emergency_reprivatize.sh                         # detach Public Bypass
#   CF_API_TOKEN=... ops/emergency_reprivatize.sh        # env override
#   ops/emergency_reprivatize.sh --dry-run               # show detach PUT only
#   ops/emergency_reprivatize.sh --reattach              # re-attach Public Bypass
#   ops/emergency_reprivatize.sh --reattach --dry-run    # show reattach PUT only
#   ops/emergency_reprivatize.sh --check                 # verify token + report state
#
# Token AND resource-ID resolution (env first, then the gitignored settings
# file). The settings-file fallback exists because an emergency environment
# frequently does NOT have the operator's exported shell vars — an earlier
# version required them and simply refused to run, which stacked a second
# failure on top of the payload bug at exactly the wrong moment.
#   token:  CF_API_TOKEN            -> cf_emergency_reprivatize_token
#   account:ZSPAN_CF_ACCOUNT_ID     -> cf_account_id
#   app:    ZSPAN_CF_ACCESS_APP_ID  -> cf_access_app_id
#   policy: ZSPAN_CF_ACCESS_POLICY_ID -> cf_access_policy_id
# The token needs the "Access: Apps and Policies: Edit" permission scoped
# to the account. Create at https://dash.cloudflare.com/profile/api-tokens.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SETTINGS_PATH="${REPO_ROOT}/02_Core_Project/council_navigator/parsers/user_settings.json"

# Read one key out of the gitignored settings file; empty string if absent.
settings_value() {
    [[ -f "$SETTINGS_PATH" ]] || { echo ""; return; }
    python3 -c 'import json,sys
try:
    s = json.load(open(sys.argv[1]))
except Exception:
    print(""); raise SystemExit
print(s.get(sys.argv[2]) or "")' "$SETTINGS_PATH" "$1" 2>/dev/null || echo ""
}

ACCOUNT_ID="${ZSPAN_CF_ACCOUNT_ID:-$(settings_value cf_account_id)}"
APP_ID="${ZSPAN_CF_ACCESS_APP_ID:-$(settings_value cf_access_app_id)}"
POLICY_ID="${ZSPAN_CF_ACCESS_POLICY_ID:-$(settings_value cf_access_policy_id)}"

if [[ -z "$ACCOUNT_ID" || -z "$APP_ID" || -z "$POLICY_ID" ]]; then
    echo "ERROR: Cloudflare resource IDs are not fully configured." >&2
    echo "Set ZSPAN_CF_ACCOUNT_ID / ZSPAN_CF_ACCESS_APP_ID / ZSPAN_CF_ACCESS_POLICY_ID," >&2
    echo "or add cf_account_id / cf_access_app_id / cf_access_policy_id to" >&2
    echo "  $SETTINGS_PATH" >&2
    exit 1
fi

APP_URL="https://api.cloudflare.com/client/v4/accounts/${ACCOUNT_ID}/access/apps/${APP_ID}"
POLICY_URL="https://api.cloudflare.com/client/v4/accounts/${ACCOUNT_ID}/access/policies/${POLICY_ID}"

# Cloudflare's Access-application PUT is a whole-object update. Keep the
# fields that may be written back to a self-hosted app in one Bash array so
# payload construction and pre/post verification cannot drift apart.
APP_SETTABLE_FIELDS=(
    name
    domain
    type
    session_duration
    session_start_time
    cors_headers
    custom_deny_message
    custom_deny_url
    custom_non_identity_deny_url
    logo_url
    header_bg_color
    skip_interstitial
    app_launcher_visible
    app_launcher_logo_url
    service_auth_401_redirect
    path_cookie_attribute
    same_site_cookie_attribute
    allowed_idps
    auto_redirect_to_identity
    allow_authenticate_via_warp
    enable_binding_cookie
    http_only_cookie_attribute
    options_preflight_bypass
    read_service_tokens_from_header
    tags
    saas_app
    self_hosted_domains
    destinations
    footer_links
    landing_page_design
)

ACTION="detach"
DRY_RUN="false"
CHECK_ONLY="false"

for argument in "$@"; do
    case "$argument" in
        --reattach) ACTION="attach" ;;
        --dry-run)  DRY_RUN="true" ;;
        --check)    CHECK_ONLY="true" ;;
        *)
            echo "unknown flag: ${argument}. Try --dry-run, --reattach, or --check." >&2
            exit 64
            ;;
    esac
done

if [[ "$CHECK_ONLY" == "true" && ( "$ACTION" != "detach" || "$DRY_RUN" == "true" ) ]]; then
    echo "--check cannot be combined with --reattach or --dry-run." >&2
    exit 64
fi

CF_API_TOKEN="${CF_API_TOKEN:-}"
if [[ -z "$CF_API_TOKEN" && -f "$SETTINGS_PATH" ]]; then
    CF_API_TOKEN="$(python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); print(s.get("cf_emergency_reprivatize_token") or "")' "$SETTINGS_PATH" 2>/dev/null || true)"
fi

if [[ -z "$CF_API_TOKEN" ]]; then
    echo "ERROR: CF_API_TOKEN unset and cf_emergency_reprivatize_token missing from" >&2
    echo "       $SETTINGS_PATH" >&2
    echo "Create a token with 'Access: Apps and Policies: Edit' scope at" >&2
    echo "https://dash.cloudflare.com/profile/api-tokens." >&2
    exit 1
fi

RESPONSE_FILE="$(mktemp)"
APP_SNAPSHOT_FILE="$(mktemp)"
trap 'rm -f -- "$RESPONSE_FILE" "$APP_SNAPSHOT_FILE"' EXIT
HTTP_STATUS=""

# ── The ingress manifest ──────────────────────────────────────────────
# One line per public door: LABEL|URL|EXPECTATION
#   walled  — must redirect to Cloudflare Access (a stranger cannot read it)
#   closed  — must return no application content (404 / gate error / refusal)
# Add a row here the moment a new public door is created. A door absent
# from this list is a door nobody is watching.
INGRESS_MANIFEST=(
    "zspan.org (public site)|https://zspan.org/|walled"
    "zspan.org catalog API|https://zspan.org/v1/catalog/jurisdictions|walled"
    "z-span.pages.dev (Pages alias)|https://z-span.pages.dev/|walled"
    "operator.zspan.org|https://operator.zspan.org/|walled"
    "Worker workers.dev (production)|https://api-zspan-org.jjworkaz.workers.dev/v1/catalog/jurisdictions|closed"
    "Worker custom domain|https://api.zspan.org/v1/catalog/jurisdictions|closed"
    "Railway origin API|https://z-span-production.up.railway.app/api/channels/tree|closed"
    "Railway origin media|https://z-span-production.up.railway.app/media/1/audio_overview.mp4|closed"
)

# Probe one door anonymously. Returns 0 if it is shut, 1 if it is open.
# No credentials are used or sent — this is deliberately the stranger's view.
#
# The ONLY thing that counts as "open" is: HTTP 2xx carrying real
# application content. Everything else — a redirect to Access, any 4xx/5xx,
# a connection failure — means a stranger cannot read the data.
#
# This distinction is load-bearing and was got wrong once already. A bare
# 403 is AMBIGUOUS: Cloudflare's bot-fight rules answer curl with 403 on a
# site that serves a real browser perfectly, and the same 403 is also what
# a genuine refusal looks like. Treating 403 as "not walled" made this
# sweep report three false OPEN doors on a site that was verifiably behind
# the Access wall in a browser. A refusal is a refusal; only content is
# exposure. (The mirror-image of this same trap once made a curl 403 get
# read as proof a file was unreachable when a browser fetched it instantly.)
probe_ingress() {
    local url="$1" expectation="$2"
    local status body_head

    status="$(curl -sS --connect-timeout 5 --max-time 20 \
        -o "$RESPONSE_FILE" -w '%{http_code}' -L "$url" 2>/dev/null)" || {
        # Nothing answered at all — that is shut.
        echo "unreachable"
        return 0
    }

    body_head="$(head -c 600 "$RESPONSE_FILE" 2>/dev/null || true)"

    # Landed on the Access login host: walled, exactly as intended.
    if grep -qiE 'cloudflareaccess\.com|<title>Sign in' <<<"$body_head"; then
        echo "Access sign-in"
        return 0
    fi

    # Any non-2xx is a refusal. Ambiguous as to WHY (bot-fight vs gate vs
    # 404), but unambiguous as to effect: no data reached the caller.
    if [[ ! "$status" =~ ^2[0-9][0-9]$ ]]; then
        echo "refused (HTTP ${status})"
        return 0
    fi

    # 2xx, but the body is a known refusal payload rather than content.
    if grep -qiE 'origin gate|there is nothing here|page not found|access denied' <<<"$body_head"; then
        echo "refusal body (HTTP ${status})"
        return 0
    fi

    # 2xx with real content — a stranger can read this.
    echo "HTTP ${status} — SERVING CONTENT"
    return 1
}

# Walk the whole manifest. Non-zero exit if ANY door is open.
sweep_ingress_manifest() {
    local open=0 label url expectation detail
    echo
    echo "── ingress sweep (anonymous — no credentials sent) ──"
    for row in "${INGRESS_MANIFEST[@]}"; do
        IFS='|' read -r label url expectation <<<"$row"
        if detail="$(probe_ingress "$url" "$expectation")"; then
            printf '  [shut] %-34s %s\n' "$label" "$detail"
        else
            printf '  [OPEN] %-34s %s\n' "$label" "$detail"
            open=$((open + 1))
        fi
    done
    echo
    if (( open > 0 )); then
        echo "FAILED — ${open} public door(s) still answering." >&2
        echo "The site is NOT fully offline. Close each [OPEN] door above." >&2
        echo "Worker hostnames: Cloudflare dash -> Workers & Pages ->" >&2
        echo "  api-zspan-org -> Domains -> Worker URL (toggle off), and make" >&2
        echo "  it durable with workers_dev/preview_urls = false in" >&2
        echo "  workers/api-zspan-org/wrangler.toml." >&2
        return 1
    fi
    echo "All doors in the manifest are shut."
    return 0
}

cf_request() {
    local method="$1"
    local url="$2"
    local payload="${3:-}"

    # Timeouts are load-bearing for a break-glass tool: without them a
    # hung connection leaves the operator staring at a blank terminal
    # during the exact minutes they need an answer. No --retry on a
    # mutating PUT — a silently repeated write is worse than a clean error.
    local -a timeouts=(--connect-timeout 5 --max-time 20)

    if [[ -n "$payload" ]]; then
        if ! HTTP_STATUS="$(curl -sS "${timeouts[@]}" -o "$RESPONSE_FILE" -w '%{http_code}' \
            -X "$method" "$url" \
            -H "Authorization: Bearer ${CF_API_TOKEN}" \
            -H "Content-Type: application/json" \
            --data-binary "$payload")"; then
            echo "Cloudflare API request failed before an HTTP response was received: ${method} ${url}" >&2
            exit 2
        fi
    else
        if ! HTTP_STATUS="$(curl -sS "${timeouts[@]}" -o "$RESPONSE_FILE" -w '%{http_code}' \
            -X "$method" "$url" \
            -H "Authorization: Bearer ${CF_API_TOKEN}" \
            -H "Content-Type: application/json")"; then
            echo "Cloudflare API request failed before an HTTP response was received: ${method} ${url}" >&2
            exit 2
        fi
    fi
}

cf_error_detail() {
    python3 - "$RESPONSE_FILE" <<'PY'
import json
import sys

try:
    envelope = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    print('code "unknown", message "response was not a valid Cloudflare JSON envelope"')
    raise SystemExit

errors = envelope.get("errors")
if isinstance(errors, list) and errors and isinstance(errors[0], dict):
    first = errors[0]
    print(
        f"code {json.dumps(first.get('code', 'unknown'))}, "
        f"message {json.dumps(first.get('message', 'unknown'))}"
    )
else:
    print('code "unknown", message "Cloudflare response contained no error detail"')
PY
}

require_cf_success() {
    local context="$1"
    local envelope_ok="false"

    if python3 - "$RESPONSE_FILE" <<'PY'
import json
import sys

try:
    envelope = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)
raise SystemExit(0 if envelope.get("success") is True else 1)
PY
    then
        envelope_ok="true"
    fi

    if [[ ! "$HTTP_STATUS" =~ ^2[0-9][0-9]$ || "$envelope_ok" != "true" ]]; then
        echo "${context} failed (HTTP ${HTTP_STATUS}): $(cf_error_detail)" >&2
        exit 2
    fi
}

app_policy_state() {
    python3 - "$RESPONSE_FILE" "$POLICY_ID" <<'PY'
import json
import sys

envelope = json.load(open(sys.argv[1], encoding="utf-8"))
policies = envelope.get("result", {}).get("policies")
if not isinstance(policies, list):
    print("Cloudflare app response has no result.policies array", file=sys.stderr)
    raise SystemExit(2)

target_id = sys.argv[2]
matches = [
    policy
    for policy in policies
    if isinstance(policy, dict) and policy.get("id") == target_id
]
if len(matches) > 1:
    print(f"Cloudflare app response contains duplicate policy ID {target_id}", file=sys.stderr)
    raise SystemExit(2)
print("attached" if matches else "detached")
PY
}

validate_target_policy() {
    python3 - "$RESPONSE_FILE" "$POLICY_ID" <<'PY'
import json
import sys

envelope = json.load(open(sys.argv[1], encoding="utf-8"))
result = envelope.get("result")
target_id = sys.argv[2]
if not isinstance(result, dict):
    print("Reusable-policy response has no result object", file=sys.stderr)
    raise SystemExit(2)
if result.get("id") != target_id:
    print(
        f"Reusable-policy GET returned ID {result.get('id')!r}, "
        f"expected {target_id!r}",
        file=sys.stderr,
    )
    raise SystemExit(2)
if result.get("decision") != "bypass":
    print(
        f"Reusable policy {target_id} has decision "
        f"{result.get('decision')!r}, expected 'bypass'",
        file=sys.stderr,
    )
    raise SystemExit(2)

include = result.get("include")
if not isinstance(include, list):
    print(
        f"Reusable bypass policy {target_id} has no include array",
        file=sys.stderr,
    )
    raise SystemExit(2)

# Access represents the Everyone selector as {"everyone": {}}. Some API
# responses normalize that selector to an empty object, so accept that
# documented equivalent too and reject every other shape.
def is_everyone_rule(rule):
    if not isinstance(rule, dict):
        return False
    if not rule:
        return True
    if len(rule) != 1:
        return False
    selector, value = next(iter(rule.items()))
    return (
        isinstance(selector, str)
        and selector.lower() == "everyone"
        and (value == {} or value is True)
    )


has_everyone = any(is_everyone_rule(rule) for rule in include)
if not has_everyone:
    print(
        f"Reusable bypass policy {target_id} does not include an Everyone rule",
        file=sys.stderr,
    )
    raise SystemExit(2)
PY
}

snapshot_app_invariants() {
    python3 - "$RESPONSE_FILE" "$APP_SNAPSHOT_FILE" "$POLICY_ID" \
        "${APP_SETTABLE_FIELDS[@]}" <<'PY'
import json
import sys

response_path, snapshot_path, target_id = sys.argv[1:4]
settable_fields = sys.argv[4:]
envelope = json.load(open(response_path, encoding="utf-8"))
result = envelope.get("result")
if not isinstance(result, dict):
    print("Cloudflare app response has no result object", file=sys.stderr)
    raise SystemExit(2)

policies = result.get("policies")
if not isinstance(policies, list):
    print("Cloudflare app response has no result.policies array", file=sys.stderr)
    raise SystemExit(2)

non_target_policies = []
seen_ids = set()
for index, policy in enumerate(policies):
    if not isinstance(policy, dict):
        print(f"Policy entry {index} is not an object", file=sys.stderr)
        raise SystemExit(2)
    policy_id = policy.get("id")
    precedence = policy.get("precedence")
    if not isinstance(policy_id, str) or not policy_id:
        print(f"Policy entry {index} has no string id", file=sys.stderr)
        raise SystemExit(2)
    if not isinstance(precedence, int) or isinstance(precedence, bool):
        print(f"Policy {policy_id} has no integer precedence", file=sys.stderr)
        raise SystemExit(2)
    if policy_id in seen_ids:
        print(
            f"Cloudflare app response contains duplicate policy ID {policy_id}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    seen_ids.add(policy_id)
    if policy_id != target_id:
        non_target_policies.append(
            {"id": policy_id, "precedence": precedence}
        )

snapshot = {
    "settable_fields": {
        key: result[key] for key in settable_fields if key in result
    },
    "non_target_policies": sorted(
        non_target_policies,
        key=lambda policy: (policy["id"], policy["precedence"]),
    ),
}
with open(snapshot_path, "w", encoding="utf-8") as snapshot_file:
    json.dump(snapshot, snapshot_file, sort_keys=True, separators=(",", ":"))
PY
}

build_policy_payload() {
    python3 - "$RESPONSE_FILE" "$POLICY_ID" "$ACTION" \
        "${APP_SETTABLE_FIELDS[@]}" <<'PY'
import json
import sys

# Cloudflare's Access-application PUT is a WHOLE-OBJECT update, not a patch.
# A payload of {"policies": [...]} alone is rejected with HTTP 400 /
# code 12130 "app type is missing or invalid" — empirically reproduced
# against the live API on 2026-07-25. So the payload has to carry the
# application's own identity fields back alongside the modified policy
# list. Anything settable that is omitted risks being reset to a default,
# which is why this echoes the shared full allowlist rather than a minimal
# set. The field names arrive from APP_SETTABLE_FIELDS in the shell.

envelope = json.load(open(sys.argv[1], encoding="utf-8"))
result = envelope.get("result")
if not isinstance(result, dict):
    print("Cloudflare app response has no result object", file=sys.stderr)
    raise SystemExit(2)

current = result.get("policies")
if not isinstance(current, list):
    print("Cloudflare app response has no result.policies array", file=sys.stderr)
    raise SystemExit(2)

if not result.get("type"):
    print(
        "Cloudflare app response has no 'type' — refusing to build a payload "
        "that would be rejected with code 12130",
        file=sys.stderr,
    )
    raise SystemExit(2)

links = []
seen_ids = set()
for index, policy in enumerate(current):
    if not isinstance(policy, dict):
        print(f"Policy entry {index} is not an object", file=sys.stderr)
        raise SystemExit(2)

    policy_id = policy.get("id")
    precedence = policy.get("precedence")
    if not isinstance(policy_id, str) or not policy_id:
        print(f"Policy entry {index} has no string id", file=sys.stderr)
        raise SystemExit(2)
    if not isinstance(precedence, int) or isinstance(precedence, bool):
        print(f"Policy {policy_id} has no integer precedence", file=sys.stderr)
        raise SystemExit(2)
    if policy_id in seen_ids:
        print(f"Cloudflare app response contains duplicate policy ID {policy_id}", file=sys.stderr)
        raise SystemExit(2)

    seen_ids.add(policy_id)
    links.append({"id": policy_id, "precedence": precedence})

target_id = sys.argv[2]
action = sys.argv[3]
settable_fields = sys.argv[4:]
if action == "detach":
    links = [policy for policy in links if policy["id"] != target_id]
elif action == "attach":
    next_precedence = max((policy["precedence"] for policy in links), default=0) + 1
    links.append({"id": target_id, "precedence": next_precedence})
else:
    print(f"Unknown action: {action}", file=sys.stderr)
    raise SystemExit(2)

payload = {key: result[key] for key in settable_fields if key in result}
payload["policies"] = links
print(json.dumps(payload, separators=(",", ":")))
PY
}

verify_updated_app() {
    local expected_state="$1"

    python3 - "$RESPONSE_FILE" "$APP_SNAPSHOT_FILE" "$POLICY_ID" \
        "$expected_state" "${APP_SETTABLE_FIELDS[@]}" <<'PY'
import json
import sys

response_path, snapshot_path, target_id, expected_state = sys.argv[1:5]
settable_fields = sys.argv[5:]
envelope = json.load(open(response_path, encoding="utf-8"))
result = envelope.get("result")
if not isinstance(result, dict):
    print("Fresh app read-back has no result object", file=sys.stderr)
    raise SystemExit(2)

policies = result.get("policies")
if not isinstance(policies, list):
    print(
        "Fresh app read-back has no result.policies array",
        file=sys.stderr,
    )
    raise SystemExit(2)

non_target_policies = []
seen_ids = set()
target_count = 0
for index, policy in enumerate(policies):
    if not isinstance(policy, dict):
        print(f"Fresh policy entry {index} is not an object", file=sys.stderr)
        raise SystemExit(2)
    policy_id = policy.get("id")
    precedence = policy.get("precedence")
    if not isinstance(policy_id, str) or not policy_id:
        print(
            f"Fresh policy entry {index} has no string id",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if not isinstance(precedence, int) or isinstance(precedence, bool):
        print(
            f"Fresh policy {policy_id} has no integer precedence",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if policy_id in seen_ids:
        print(
            f"Fresh app response contains duplicate policy ID {policy_id}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    seen_ids.add(policy_id)
    if policy_id == target_id:
        target_count += 1
    else:
        non_target_policies.append(
            {"id": policy_id, "precedence": precedence}
        )

expected_count = 1 if expected_state == "attached" else 0
if target_count != expected_count:
    print(
        f"Fresh read-back sanity check failed: Public Bypass expected "
        f"{expected_state}, found {target_count} matching policy entries",
        file=sys.stderr,
    )
    raise SystemExit(2)

with open(snapshot_path, encoding="utf-8") as snapshot_file:
    before = json.load(snapshot_file)

after_fields = {
    key: result[key] for key in settable_fields if key in result
}
before_fields = before.get("settable_fields")
if before_fields != after_fields:
    before_keys = set(before_fields or {})
    after_keys = set(after_fields)
    for key in sorted(before_keys | after_keys):
        before_value = (
            before_fields[key] if key in before_keys else "<absent>"
        )
        after_value = after_fields[key] if key in after_keys else "<absent>"
        if before_value != after_value:
            print(
                f"Settable app field drift after PUT: {key}: "
                f"before={before_value!r}, after={after_value!r}",
                file=sys.stderr,
            )
    raise SystemExit(2)

after_non_target = sorted(
    non_target_policies,
    key=lambda policy: (policy["id"], policy["precedence"]),
)
before_non_target = before.get("non_target_policies")
if before_non_target != after_non_target:
    print(
        "Non-target policy links drifted after PUT: "
        f"before={before_non_target!r}, after={after_non_target!r}",
        file=sys.stderr,
    )
    raise SystemExit(2)
PY
}

echo "=== emergency re-privatize zspan.org ==="
echo "target app: zspan.org (${APP_ID})"

cf_request "GET" "$POLICY_URL"
require_cf_success "Reusable-policy validation"
if ! validate_target_policy; then
    echo "Configured Public Bypass policy failed closed validation." >&2
    exit 2
fi
echo "policy OK — reusable policy is bypass + Everyone."

if [[ "$CHECK_ONLY" == "true" ]]; then
    echo "CHECK — verifying app attachment state and every ingress"

    cf_request "GET" "$APP_URL"
    require_cf_success "Application state check"
    if ! POLICY_STATE="$(app_policy_state)"; then
        echo "Could not determine Public Bypass attachment state." >&2
        exit 2
    fi
    echo "Public Bypass is currently ${POLICY_STATE}."

    # --check is also a full-perimeter audit, not just a token test. This is
    # the daily-canary use: run it and find out if any door has drifted open.
    sweep_ingress_manifest || exit 1
    exit 0
fi

echo "${ACTION} policy: Public Bypass (${POLICY_ID})"

cf_request "GET" "$APP_URL"
require_cf_success "Application state fetch"
if ! POLICY_STATE="$(app_policy_state)"; then
    echo "Could not determine Public Bypass attachment state." >&2
    exit 2
fi

if [[ "$ACTION" == "detach" && "$POLICY_STATE" == "detached" ]]; then
    echo "Public Bypass is already detached; no change needed."
    if ! sweep_ingress_manifest; then
        echo "KILL INCOMPLETE — see the open door(s) above." >&2
        exit 1
    fi
    echo
    echo "Kill remains complete: every door in the ingress manifest is shut."
    exit 0
fi
if [[ "$ACTION" == "attach" && "$POLICY_STATE" == "attached" ]]; then
    echo "Public Bypass is already attached; no change needed."
    exit 0
fi

if ! PAYLOAD="$(build_policy_payload)"; then
    echo "Could not build the app policies update payload." >&2
    exit 2
fi

if [[ "$DRY_RUN" == "true" ]]; then
    echo "DRY RUN — would PUT ${APP_URL}"
    echo "Request body:"
    printf '%s\n' "$PAYLOAD" | python3 -m json.tool
    exit 0
fi

if ! snapshot_app_invariants; then
    echo "Could not snapshot pre-update app invariants." >&2
    exit 2
fi

cf_request "PUT" "$APP_URL" "$PAYLOAD"
require_cf_success "Application policy update"

# Re-read from Cloudflare rather than trusting the PUT's own echo. A write
# API reporting its own success is not evidence the state actually landed;
# this is the same class of mistake as verifying a force-push against the
# local mirror instead of re-fetching from the remote.
cf_request "GET" "$APP_URL"
require_cf_success "Post-update state re-read"

if [[ "$ACTION" == "detach" ]]; then
    verify_updated_app "detached"
    echo
    echo "Public Bypass policy detached (confirmed by a fresh read-back)."
    echo "Access wall restored on zspan.org."

    # The kill is not reported as successful until every door in the
    # manifest is confirmed shut — including the ones this script cannot
    # itself close. A non-zero exit here means the site is still reachable
    # somewhere, and the operator needs to know that immediately.
    if ! sweep_ingress_manifest; then
        echo "KILL INCOMPLETE — see the open door(s) above." >&2
        exit 1
    fi

    echo
    echo "Kill complete: every door in the ingress manifest is shut."
    echo
    echo "Final human acceptance — a signed-out browser, private window:"
    echo "  open https://zspan.org/  -> must land on the Cloudflare Access sign-in."
    echo "A curl status line alone is NOT acceptance: bot-fight rules answer"
    echo "curl differently than a real browser and have produced a false"
    echo "'blocked' reading before."
else
    verify_updated_app "attached"
    echo
    echo "Public Bypass policy re-attached (confirmed by a fresh read-back)."
    echo "Public access restored on zspan.org."
    echo
    echo "ACCEPTANCE — use a browser, in a private/incognito window, signed out:"
    echo "  open https://zspan.org/  -> must render the site without a sign-in prompt."
fi
