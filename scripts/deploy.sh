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
# Refuse to overwrite local edits silently — surface them so the operator
# can decide what to do (commit, stash, or discard).
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "ERROR: working tree at /opt/vigilant has uncommitted changes."
    git status --short
    echo "Either commit/stash these or run 'git checkout .' before deploying."
    exit 1
fi
prev_head=$(git rev-parse HEAD)
git pull --ff-only
new_head=$(git rev-parse HEAD)

# nginx/*.conf are bind-mounted as individual files. git pull replaces the
# inode, but the container's mount still points at the old one — `nginx -s
# reload` then reloads stale content. Detect nginx config changes here so we
# can force-recreate the nginx container later in step 5.
nginx_recreate=0
if [[ "$prev_head" != "$new_head" ]]; then
    if git diff --name-only "$prev_head" "$new_head" | grep -qE '^nginx/'; then
        nginx_recreate=1
        echo "     nginx config changed — will recreate nginx container in step 5"
    fi
fi

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
if [[ $nginx_recreate -eq 1 ]]; then
    # File-bind-mount inode swap from git pull means nginx -s reload reads
    # stale content. Recreate the container so the bind picks up the new
    # inode. Brief blip on mapper.thunderborn.dev (~2s).
    docker compose up -d --no-deps --force-recreate nginx
    sleep 2
    docker exec vigilant-nginx-1 nginx -t
    echo "     nginx recreated (config changed)"
else
    # Graceful reload: existing connections keep using the old worker until
    # they finish; new ones use the new config + re-resolved app upstream.
    docker exec vigilant-nginx-1 nginx -s reload
    echo "     nginx reloaded"
fi

echo "[6/6] Post-deploy checks"
# Wait for the app to bind and respond. /healthz is a stub that returns
# 200 OK with no DB/ESI work — the cheapest possible liveness probe.
ok=0
for i in 1 2 3 4 5 6 7 8 9 10; do
    sleep 2
    code=$(docker exec vigilant-app-1 python3 -c "import urllib.request,sys
try:
    sys.stdout.write(str(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status))
except Exception:
    sys.stdout.write('000')" 2>/dev/null || echo "000")
    if [[ "$code" == "200" ]]; then
        ok=1
        echo "     /healthz responded 200 after ${i} attempt(s)"
        break
    fi
done

echo "--- last 30s of app logs ---"
docker logs --since 30s vigilant-app-1 2>&1 | tail -20

if [[ $ok -ne 1 ]]; then
    echo ""
    echo "⚠ /healthz never returned 200 — the new image is not serving."
    echo "  Roll back with: /opt/vigilant/scripts/rollback.sh"
    exit 1
fi

# Defense-in-depth: also flag obvious startup errors even when /healthz is
# green (a partial-init bug might serve health but still log tracebacks).
errors=$(docker logs --since 30s vigilant-app-1 2>&1 | grep -iE 'error|traceback' | head -5 || true)
if [[ -n "$errors" ]]; then
    echo ""
    echo "⚠ Errors detected in startup logs (app is serving but worth investigating):"
    echo "$errors"
fi

echo ""
echo "✓ Deploy complete. Running commit: $(git rev-parse --short HEAD)"
echo "  To roll back: /opt/vigilant/scripts/rollback.sh"
