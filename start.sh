#!/usr/bin/env bash
#
# Z-SPAN flagship container entrypoint.
#
# Starts Flask (parser API) on internal 127.0.0.1:5001 in the background,
# then exec's Express on $PORT (Railway-provided) in the foreground.
# Express proxies /api/* to Flask and serves /media/* static files.
#
# Persistent state (SQLite + media files) lives in /data when a Railway
# volume is mounted there — start.sh sets ZSPAN_DB_PATH + ZSPAN_MEDIA_ROOT
# so the apps write to the persistent volume instead of ephemeral container
# storage.

set -euo pipefail

# ─────────────────────────────────────────────────────────────────
# Persistent storage. Railway mounts a volume at /data when configured.
# ─────────────────────────────────────────────────────────────────
if [ -d "/data" ]; then
    mkdir -p /data/media
    export ZSPAN_DB_PATH="${ZSPAN_DB_PATH:-/data/meetings_cache.db}"
    export ZSPAN_MEDIA_ROOT="${ZSPAN_MEDIA_ROOT:-/data/media}"
    echo "[start.sh] /data is mounted; DB=$ZSPAN_DB_PATH MEDIA=$ZSPAN_MEDIA_ROOT"
else
    echo "[start.sh] WARNING: /data not mounted — DB + media will be ephemeral"
fi

# Express talks to Flask on the loopback. Override only if you have a
# reason (e.g., running Flask in a sibling container).
export PARSER_API_URL="${PARSER_API_URL:-http://127.0.0.1:5001}"

# Railway injects $PORT; default to 3000 for local docker run.
export PORT="${PORT:-3000}"

# Flask binds 127.0.0.1 by default (parsers/api_server.py). Keep it
# internal — only Express should reach it.
export PARSER_API_HOST="${PARSER_API_HOST:-127.0.0.1}"
export PARSER_API_PORT="${PARSER_API_PORT:-5001}"

# ─────────────────────────────────────────────────────────────────
# Start Flask in the background.
# ─────────────────────────────────────────────────────────────────
cd /app/02_Core_Project/council_navigator/parsers
python3 api_server.py "${PARSER_API_PORT}" &
FLASK_PID=$!
echo "[start.sh] Flask started (pid ${FLASK_PID}) on ${PARSER_API_HOST}:${PARSER_API_PORT}"

# Forward SIGTERM/SIGINT to Flask so the whole container shuts down cleanly.
cleanup() {
    echo "[start.sh] shutting down (signal received)"
    kill -TERM "${FLASK_PID}" 2>/dev/null || true
    wait "${FLASK_PID}" 2>/dev/null || true
    exit 0
}
trap cleanup SIGTERM SIGINT

# Wait briefly so Flask binds before Express tries its first proxy call.
# 2s is generous; Flask normally binds in <500ms.
sleep 2

# Sanity check — if Flask died at boot, fail loudly instead of running
# a broken proxy.
if ! kill -0 "${FLASK_PID}" 2>/dev/null; then
    echo "[start.sh] FATAL: Flask exited during startup"
    exit 1
fi

# ─────────────────────────────────────────────────────────────────
# Start Express in the foreground. exec replaces the shell so tini
# (PID 1) sees the node process directly for clean signal handling.
# ─────────────────────────────────────────────────────────────────
cd /app/02_Core_Project/council_navigator
echo "[start.sh] Express starting on port ${PORT}"
exec node dist/index.js
