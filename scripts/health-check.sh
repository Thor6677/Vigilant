#!/bin/bash
# Health check: exit 0 = healthy, exit 1 = unhealthy.
# Pass --wait to allow up to 90s for containers to start (used post-reboot).
#
# Single-shot pass/fail gate. Used by:
#   - scripts/deploy.sh (via VPS path /opt/vigilant/scripts/health-check.sh)
#   - thunderborn-ops/scripts/post-reboot-check.sh
#
# Sources the shared probe library from the thunderborn-ops install. If
# thunderborn-ops isn't installed on this host, this script can't run.

set -euo pipefail

PROBES=/opt/thunderborn-ops/scripts/probes.sh
if [ ! -r "$PROBES" ]; then
    echo "health-check: missing $PROBES — install thunderborn-ops first" >&2
    exit 1
fi
# shellcheck disable=SC1090
source "$PROBES"

WAIT_FOR_START=0
[ "${1:-}" = "--wait" ] && WAIT_FOR_START=1

APP_CONTAINER="vigilant-app-1"
NGINX_CONTAINER="edge-nginx-1"   # was vigilant-nginx-1 pre-2026-05-14
HEALTHZ_URL="https://vigilant.thunderborn.dev/healthz"

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
