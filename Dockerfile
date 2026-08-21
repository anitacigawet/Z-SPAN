# Z-SPAN flagship backend image.
#
# Runs Flask (parser API) + Express (gateway + /media static serving)
# together in one container. The browser never talks to this container
# directly — Cloudflare Pages Functions reverse-proxy /api/* and
# /media/* paths from zspan.org to here.
#
# The image is not Railway-specific. It is the flagship's reference build and
# may be built or adapted for purposes allowed by the repository license.
#
# ─────────────────────────────────────────────────────────────────
# Sol pen-test Finding #12 remediation:
#
#   1. Base images pinned to explicit **manifest-list digests**
#      (session-94 discuss round-1, sol's PARTIAL→CLOSED refinement).
#      Session-93 PR #150 landed patch-pinning to
#      `python:3.11.15-slim-bookworm` + `node:20-bookworm-slim` and
#      documented the digest-refresh recipe; this pass pins the digests
#      themselves. Multi-arch friendly — the digests point at the OCI
#      image indexes, not single-platform manifests, so Railway's
#      builder still resolves the platform-appropriate layer.
#
#      Refresh cadence per sol's round-1: CVE-driven, not annual. To
#      refresh:
#        # Fetch the current manifest-list digest for a tag.
#        curl -sSI \
#          -H "Authorization: Bearer $(curl -sS \
#            'https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/python:pull' \
#            | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")" \
#          -H "Accept: application/vnd.oci.image.index.v1+json" \
#          -H "Accept: application/vnd.docker.distribution.manifest.list.v2+json" \
#          "https://registry-1.docker.io/v2/library/python/manifests/3.11.15-slim-bookworm" \
#          | grep -i docker-content-digest
#      Same recipe for `library/node`.
#
#   2. `curl | bash` NodeSource install replaced by multi-stage COPY
#      from the official pinned Node image. No shell script from the
#      network runs at build time.
#
#   3. Follow-up: hash-locked `requirements.lock` for the Python deps
#      (`pip-compile --generate-hashes` from pip-tools) generated inside
#      the pinned Docker image so transitives resolve against the exact
#      runtime, not macOS. Deferred to its own focused session per
#      session-94 discuss round-1.
# ─────────────────────────────────────────────────────────────────

# Node runtime source. Only the `/usr/local/bin/node` binary + the
# `/usr/local/lib/node_modules/npm/` package tree get copied into the
# final image; the rest of the node-src layer is discarded.
FROM node:20-bookworm-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0 AS node-src

# Python base image — the runtime the final image is built on.
FROM python:3.11.15-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NODE_VERSION=20

# Base system deps: bash for start.sh, ca-certs for TLS (needed by
# yt-dlp + Flask outbound calls), tini for proper PID-1 signal
# handling so SIGTERM kills both processes.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        tini \
 && rm -rf /var/lib/apt/lists/*

# Copy the Node runtime from the pinned node-src stage — this is the
# Finding #12 replacement for the previous `curl | bash` NodeSource
# install. `npm` lives inside node_modules/npm/, so we symlink its
# CLI entrypoints into /usr/local/bin/ so `pnpm` install works.
COPY --from=node-src /usr/local/bin/node /usr/local/bin/node
COPY --from=node-src /usr/local/include/node /usr/local/include/node
COPY --from=node-src /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && npm install -g pnpm@10.18.1

WORKDIR /app

# ─────────────────────────────────────────────────────────────────
# Python deps. Copy only requirements.txt first so docker layer cache
# hits when only application code changes.
# ─────────────────────────────────────────────────────────────────
COPY 02_Core_Project/council_navigator/parsers/requirements.txt \
     /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# ─────────────────────────────────────────────────────────────────
# Node deps. Same cache-friendly layering.
# ─────────────────────────────────────────────────────────────────
COPY 02_Core_Project/council_navigator/package.json \
     02_Core_Project/council_navigator/pnpm-lock.yaml \
     /app/02_Core_Project/council_navigator/
WORKDIR /app/02_Core_Project/council_navigator
RUN pnpm install --frozen-lockfile

# ─────────────────────────────────────────────────────────────────
# Now copy the actual application code.
# ─────────────────────────────────────────────────────────────────
WORKDIR /app
COPY . /app

# Build the Express server bundle (esbuild) so dist/index.js exists at
# runtime. We deliberately do NOT run `vite build` here — Cloudflare
# Pages builds the React static bundle on its own pipeline; this image
# only serves the API + /media static files.
WORKDIR /app/02_Core_Project/council_navigator
RUN pnpm exec esbuild server/index.ts \
        --platform=node \
        --packages=external \
        --bundle \
        --format=esm \
        --outdir=dist

WORKDIR /app
RUN chmod +x /app/start.sh

# Default port; Railway injects $PORT at runtime which start.sh respects.
EXPOSE 3000

# tini gives us proper SIGTERM propagation so both python + node shut
# down cleanly when Railway redeploys.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/start.sh"]
