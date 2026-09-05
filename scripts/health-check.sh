#!/bin/bash
# Health check: exit 0 = healthy, exit 1 = unhealthy.
# Pass --wait to wait for containers to report READY before probing (post-reboot).
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

# Step 1: Wait for the containers to become READY (post-reboot mode)
#
# "Running" is not "ready". The gate here used to count running containers
# host-wide and break at >=2 — an accurate proxy on the single-purpose droplet
# this script was written for, but vacuous on a host that also runs a
# monitoring/media stack: ~30 unrelated containers satisfy it within a second of
# boot. On 2026-09-05 that let the "90s" wait return in ~1s, step 2 passed on a
# container whose health was still `starting`, and the /healthz retries expired
# 7s before uvicorn bound :8000 — a false CRIT, a postmortem bundle and a revert
# prompt for a perfectly healthy box (ISS-032).
#
# Gate on the app container's OWN healthcheck instead. The budget has to clear
# its StartPeriod + Interval (20s + 30s) on top of app startup, so ~90s is the
# floor; 150s leaves headroom for a cold page cache after a kernel reboot.
READY_TIMEOUT="${READY_TIMEOUT:-150}"

# Echoes a status word. Returns 0 only when the container can plausibly serve:
# running AND (reporting healthy, or declaring no healthcheck at all).
container_ready() {
    local name="$1" status health
    status=$(docker inspect --format '{{.State.Status}}' "$name" 2>/dev/null) || {
        echo "absent"; return 1
    }
    if [ "$status" != "running" ]; then
        echo "$status"; return 1
    fi
    health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
        "$name" 2>/dev/null) || health=""
    case "$health" in
        healthy) echo "healthy"; return 0 ;;
        "")      echo "running (no healthcheck)"; return 0 ;;
        *)       echo "$health"; return 1 ;;
    esac
}

if [ "$WAIT_FOR_START" = "1" ]; then
    # ONE deadline shared across every container, not one budget each: the edge
    # proxy declares no healthcheck and so reports ready instantly, and a
    # per-container budget would only inflate the worst case for no gain. The
    # unit is Type=oneshot with TimeoutStartUSec=infinity, so a long wait here
    # cannot be killed mid-flight — which matters, because the cleanup tail of
    # post-reboot-check.sh (marker removal, `maintenance.sh off`) runs after it.
    log "Waiting up to ${READY_TIMEOUT}s for containers to become ready..."
    elapsed=0
    for c in "$APP_CONTAINER" "$NGINX_CONTAINER"; do
        while :; do
            # set -e: container_ready returns non-zero by design, so it must be
            # tested directly in `if`, never `out=$(...); rc=$?` (ISS-010).
            if out=$(container_ready "$c"); then
                log "OK: $c ready at ${elapsed}s -> $out"
                break
            fi
            if [ "$elapsed" -ge "$READY_TIMEOUT" ]; then
                # Deliberately not a failure: steps 2 and 3 own the verdict and
                # the exit code. This is a wait, not an assessment.
                log "WARN: $c still '$out' at the ${elapsed}s deadline — probing anyway."
                break
            fi
            sleep 5
            elapsed=$((elapsed + 5))
        done
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

# Step 3: /healthz HTTP check (retry, 5s apart)
# This budget is insurance BEHIND step 1's readiness gate, never a substitute
# for it — a flat 5 attempts only ever covered ~20s, less than a cold app takes
# to bind (ISS-032). Post-reboot gets a wider one: DNS, the edge proxy and the
# app are all warming at the same time.
# NOTE: must use `if out=$(cmd); then rc=0; else rc=$?; fi` — a plain
# `out=$(cmd); rc=$?` causes set -e to exit the script on the first failed
# attempt before rc=$? runs, silently defeating the retry loop (ISS-010).
HTTP_ATTEMPTS=5
[ "$WAIT_FOR_START" = "1" ] && HTTP_ATTEMPTS=10
log "Checking $HEALTHZ_URL..."
for attempt in $(seq 1 "$HTTP_ATTEMPTS"); do
    if out=$(probe_http "$HEALTHZ_URL" 200 10); then rc=0; else rc=$?; fi
    if [ "$rc" -eq 0 ]; then
        log "OK: $HEALTHZ_URL → $out (attempt $attempt)"
        break
    fi
    if [ "$attempt" -eq "$HTTP_ATTEMPTS" ]; then
        log "FAIL: /healthz never returned 200 after $HTTP_ATTEMPTS attempts → $out"
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
