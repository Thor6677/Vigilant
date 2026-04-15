#!/usr/bin/env bash
# Safe deploy with rollback tag.
#
# Tags the currently-running image as :prev before rebuilding, so a bad
# deploy can be swapped back instantly via rollback.sh without rebuilding.
#
# Usage (on the VPS):
#   /opt/vigilant/scripts/deploy.sh
set -euo pipefail

cd /opt/vigilant

echo "[1/5] git pull"
git pull --ff-only

echo "[2/5] Tag current image as :prev (for fast rollback)"
if docker image inspect vigilant-app:latest >/dev/null 2>&1; then
    docker tag vigilant-app:latest vigilant-app:prev
    echo "     tagged vigilant-app:prev"
else
    echo "     no existing :latest image (first deploy?)"
fi

echo "[3/5] docker compose down"
docker compose down

echo "[4/5] docker compose up -d --build"
docker compose up -d --build

echo "[5/5] Post-deploy checks"
sleep 3
docker exec vigilant-nginx-1 nginx -t
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
