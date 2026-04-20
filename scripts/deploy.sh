#!/usr/bin/env bash
# Zero-downtime app deploy with rollback tag.
#
# Tags the currently-running image as :prev before rebuilding, so a bad
# deploy can be swapped back instantly via rollback.sh without rebuilding.
#
# nginx is NEVER torn down by this script. The Wanderer mapper
# (mapper.thunderborn.dev) shares this nginx as its reverse proxy via
# the external `web` bridge, so stopping nginx would disconnect every
# LiveView socket on the mapper. Instead we rebuild + recreate only the
# `app` service, then graceful-reload nginx so it picks up the new app
# container's IP (and any nginx/*.conf changes in this commit).
#
# Usage (on the VPS):
#   /opt/vigilant/scripts/deploy.sh
set -euo pipefail

cd /opt/vigilant

echo "[1/6] git pull"
git pull --ff-only

echo "[2/6] Tag current image as :prev (for fast rollback)"
if docker image inspect vigilant-app:latest >/dev/null 2>&1; then
    docker tag vigilant-app:latest vigilant-app:prev
    echo "     tagged vigilant-app:prev"
else
    echo "     no existing :latest image (first deploy?)"
fi

echo "[3/6] Rebuild + recreate app (nginx left running)"
# --no-deps prevents compose from touching nginx or any other dependent
# service. --force-recreate ensures the new image is actually swapped in.
docker compose up -d --build --no-deps --force-recreate app

echo "[4/6] Ensure nginx is up (no-op if already running)"
# If nginx somehow died (or this is a first-time deploy), bring it up
# without recreating — idempotent.
docker compose up -d --no-deps nginx

echo "[5/6] Validate + reload nginx"
# Validate first so a bad config doesn't take nginx down on reload.
docker exec vigilant-nginx-1 nginx -t
# Graceful reload: existing connections keep using the old worker until
# they finish; new ones use the new config + re-resolved app upstream.
docker exec vigilant-nginx-1 nginx -s reload
echo "     nginx reloaded"

echo "[6/6] Post-deploy checks"
sleep 3
echo "--- last 30s of app logs ---"
docker logs --since 30s vigilant-app-1 2>&1 | tail -20
errors=$(docker logs --since 30s vigilant-app-1 2>&1 | grep -iE 'error|traceback' | head -5 || true)
if [[ -n "$errors" ]]; then
    echo ""
    echo "⚠ Errors detected in startup logs:"
    echo "$errors"
    echo ""
    echo "If broken, roll back with: /opt/vigilant/scripts/rollback.sh"
    exit 1
fi

echo ""
echo "✓ Deploy complete. Running commit: $(git rev-parse --short HEAD)"
echo "  To roll back: /opt/vigilant/scripts/rollback.sh"
