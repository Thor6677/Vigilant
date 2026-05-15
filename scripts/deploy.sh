#!/usr/bin/env bash
# Zero-downtime app deploy with rollback tag.
#
# Tags the currently-running image as :prev before rebuilding, so a bad
# deploy can be swapped back instantly via rollback.sh without rebuilding.
#
# As of 2026-05-14, vigilant no longer ships its own nginx — the shared
# reverse proxy lives in /opt/edge/. This script only touches the `app`
# service. Edge nginx re-resolves vigilant's container IP at request
# time (resolver 127.0.0.11 + variable upstream) so a fresh app
# container is picked up automatically with no nginx reload needed.
#
# Usage (on the VPS):
#   /opt/vigilant/scripts/deploy.sh
set -euo pipefail

cd /opt/vigilant

echo "[1/4] git pull"
# Refuse to overwrite local edits silently — surface them so the operator
# can decide what to do (commit, stash, or discard).
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "ERROR: working tree at /opt/vigilant has uncommitted changes."
    git status --short
    echo "Either commit/stash these or run 'git checkout .' before deploying."
    exit 1
fi
git pull --ff-only

echo "[2/4] Tag current image as :prev (for fast rollback)"
if docker image inspect vigilant-app:latest >/dev/null 2>&1; then
    docker tag vigilant-app:latest vigilant-app:prev
    echo "     tagged vigilant-app:prev"
else
    echo "     no existing :latest image (first deploy?)"
fi

echo "[3/4] Rebuild + recreate app"
# --no-deps keeps compose from touching anything else (vigilant doesn't
# own any sibling services any more, but the flag is cheap insurance).
# --force-recreate ensures the new image is actually swapped in.
docker compose up -d --build --no-deps --force-recreate app

echo "[4/4] Post-deploy checks"
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
