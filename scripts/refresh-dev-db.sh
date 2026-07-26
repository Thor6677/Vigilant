#!/usr/bin/env bash
# Rebuild the dev instance's database from production.
#
# PRODUCTION STAYS UP. See the header of seed-dev-db.py for why that is safe:
# every query is index-driven and the seeder reopens its read transaction each
# chunk, so no single snapshot is held long enough to block WAL checkpointing.
#
# The WAL watchdog below is the measurable proxy for "am I about to repeat the
# 2026-07-10 outage" -- if the write-ahead log grows past the threshold, the
# seeder is holding checkpoints off and we back off. Worst case is a slow run,
# not a stalled site.
#
# Usage (on the host):
#   /opt/vigilant-dev/scripts/refresh-dev-db.sh [--force] [--days N]
set -euo pipefail

PROD_VOL=vigilant_app_data
DEV_VOL=vigilant-dev_dev_data
DEV_IMAGE=vigilant-dev:local
PROD_CONTAINER=vigilant-app-1
WAL_LIMIT_BYTES=$((2 * 1024 * 1024 * 1024))
MIN_FREE_GB=20

FORCE=0
DAYS=30
while [[ $# -gt 0 ]]; do
    case "$1" in
        --force) FORCE=1; shift ;;
        --days)  DAYS="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

free_gb=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
if (( free_gb < MIN_FREE_GB )); then
    echo "ERROR: only ${free_gb}G free, need ${MIN_FREE_GB}G." >&2
    exit 1
fi
echo "[1/4] disk check: ${free_gb}G free"

echo "[2/4] dev DB pre-check"
# --entrypoint, NOT -u 10001. Two different situations, two different answers:
# `docker exec` into the RUNNING prod container needs -u 10001 because that
# container drops CAP_DAC_OVERRIDE. But `docker run` of the IMAGE must not pass
# -u: the entrypoint runs as root, chowns /data and exec's `gosu vigilant`, so
# forced to uid 10001 the chown fails under set -e and gosu cannot switch users
# -- the container dies before doing anything. See seed-dev-db.py's header.
if docker run --rm --entrypoint test -v "${DEV_VOL}:/devdata" "$DEV_IMAGE" \
        -f /devdata/vigilant.db; then
    if (( FORCE != 1 )); then
        echo "ERROR: dev DB already exists. Re-run with --force to replace it." >&2
        exit 1
    fi
    docker run --rm --entrypoint rm -v "${DEV_VOL}:/devdata" "$DEV_IMAGE" \
        -f /devdata/vigilant.db
    echo "     removed existing dev DB (--force)"
fi

echo "[3/4] WAL watchdog + seed"
# -u 10001 IS correct here: this is `docker exec` into the running prod
# container, where uid 0 cannot read vigilant-owned files.
(
    while sleep 30; do
        wal=$(docker exec -u 10001 "$PROD_CONTAINER" \
              stat -c %s /data/vigilant.db-wal 2>/dev/null || echo 0)
        if (( wal > WAL_LIMIT_BYTES )); then
            echo "     WARNING: WAL at $((wal / 1024 / 1024))MB — pausing seeder 60s"
            pkill -STOP -f seed-dev-db.py || true
            sleep 60
            pkill -CONT -f seed-dev-db.py || true
        fi
    done
) &
watchdog=$!
# Always reap the watchdog, and make sure a paused seeder is never left stopped.
trap 'kill "$watchdog" 2>/dev/null || true; pkill -CONT -f seed-dev-db.py 2>/dev/null || true' EXIT

docker run --rm --entrypoint python3 \
    -v "${PROD_VOL}:/prod" \
    -v "${DEV_VOL}:/devdata" \
    -v "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd):/scripts:ro" \
    "$DEV_IMAGE" \
    /scripts/seed-dev-db.py \
        --prod /prod/vigilant.db --out /devdata/vigilant.db --days "$DAYS"

echo "[4/4] production health after seed"
docker exec -u 10001 "$PROD_CONTAINER" python3 -c \
  "import urllib.request;print('healthz:', urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3).status)"

echo ""
echo "✓ Dev database rebuilt. Start dev with:"
echo "    cd /opt/vigilant-dev && docker compose -f docker-compose.dev.yml up -d"
echo "  Then tunnel in:  ssh -L 8001:127.0.0.1:8001 <your-host>"
