#!/bin/bash
# Health check: exit 0 = healthy, exit 1 = unhealthy.
# Pass --wait to allow up to 90s for containers to start (used post-reboot).
#
# Single-shot pass/fail gate. Maintainer tooling — it assumes a host-level ops
# toolkit that provides a shared probe library (probe_container, probe_http,
# probe_log_errors) and a publicly reachable /healthz URL.
#
# Deployment-specific values (probe library path, public URL, container names)
# are NOT baked into this file. Put them in an untracked `.health-env` beside
# the repo root, e.g.:
#
#   PROBES=/opt/<your-ops-toolkit>/scripts/probes.sh
#   HEALTHZ_URL=https://<your-host>/healthz
#   NGINX_CONTAINER=edge-nginx-1
#
# or export the same names in the calling environment.
#
# EXIT CODES — callers must distinguish these:
#   0  healthy
#   1  UNHEALTHY (a probe genuinely failed)
#  78  MISCONFIGURED (EX_CONFIG) — could not assess. Required settings are
#      missing, so nothing was actually checked. Callers that restore/restart
#      on failure MUST NOT treat 78 as "down": the app may be perfectly fine.

set -euo pipefail

EX_CONFIG=78

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
[ -r "$REPO_ROOT/.health-env" ] && source "$REPO_ROOT/.health-env"

PROBES="${PROBES:-}"
if [ -z "$PROBES" ] || [ ! -r "$PROBES" ]; then
    echo "health-check: probe library not found (PROBES='${PROBES:-unset}')." >&2
    echo "  Set PROBES in $REPO_ROOT/.health-env or in the environment." >&2
    exit "$EX_CONFIG"
fi
# shellcheck disable=SC1090
source "$PROBES"

WAIT_FOR_START=0
[ "${1:-}" = "--wait" ] && WAIT_FOR_START=1

APP_CONTAINER="${APP_CONTAINER:-vigilant-app-1}"
NGINX_CONTAINER="${NGINX_CONTAINER:-edge-nginx-1}"   # reverse proxy fronting the app
HEALTHZ_URL="${HEALTHZ_URL:-}"
if [ -z "$HEALTHZ_URL" ]; then
    echo "health-check: HEALTHZ_URL is not set." >&2
    echo "  Set it in $REPO_ROOT/.health-env or in the environment." >&2
    exit "$EX_CONFIG"
fi

log() { echo "[health-check] $*"; }

# Step 1: Wait for containers to start (post-reboot mode)
if [ "$WAIT_FOR_START" = "1" ]; then
    log "Waiting for Docker containers to start (up to 90s)..."
    elapsed=0
    while [ $elapsed -lt 90 ]; do
        running=$(docker ps --filter status=running --format '{{.Names}}' 2>/dev/null | wc -l)
        [ "$running" -ge 2 ] && break
        sleep 5
        elapsed=$((elapsed + 5))
    done
fi

# Step 2: Both containers must be running (via shared probe)
log "Checking containers are running..."
for c in "$APP_CONTAINER" "$NGINX_CONTAINER"; do
    if out=$(probe_container "$c"); then rc=0; else rc=$?; fi
    if [ "$rc" -ne 0 ]; then
        log "FAIL: $c → $out"
        exit 1
    fi
    log "OK: $c → $out"
done

# Step 3: /healthz HTTP check (retry 5x, 5s apart)
# NOTE: must use `if out=$(cmd); then rc=0; else rc=$?; fi` — a plain
# `out=$(cmd); rc=$?` causes set -e to exit the script on the first failed
# attempt before rc=$? runs, silently defeating the retry loop (ISS-010).
log "Checking $HEALTHZ_URL..."
for attempt in 1 2 3 4 5; do
    if out=$(probe_http "$HEALTHZ_URL" 200 10); then rc=0; else rc=$?; fi
    if [ "$rc" -eq 0 ]; then
        log "OK: $HEALTHZ_URL → $out (attempt $attempt)"
        break
    fi
    if [ "$attempt" -eq 5 ]; then
        log "FAIL: /healthz never returned 200 → $out"
        exit 1
    fi
    log "Attempt $attempt failed ($out), retrying in 5s..."
    sleep 5
done

# Step 4: Log scan — only real Python-level failures
# Same set -e pattern fix applied here.
log "Scanning recent app logs for critical errors..."
if out=$(probe_log_errors "$APP_CONTAINER" 90s 1 1); then rc=0; else rc=$?; fi
if [ "$rc" -ne 0 ]; then
    log "FAIL: $out"
    docker logs "$APP_CONTAINER" --since 90s 2>&1 | \
        grep -E '^(ERROR|CRITICAL):|^Traceback \(most recent call last\)|^[A-Za-z]+Error:|^[A-Za-z]+Exception:' | tail -20
    exit 1
fi
log "OK: $out"
log "Health check PASSED."
exit 0
