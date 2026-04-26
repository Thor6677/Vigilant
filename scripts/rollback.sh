#!/usr/bin/env bash
# Instant rollback: swap the :prev image back in as :latest without rebuilding.
#
# Use this when a deploy succeeded but the new image has a runtime bug and
# you need service restored NOW. After running, ALWAYS follow up with
# `git revert <bad-commit> && git push && deploy.sh` so the code and image
# are back in sync — otherwise the next deploy will re-introduce the bug.
#
# Usage (on the VPS):
#   /opt/vigilant/scripts/rollback.sh
set -euo pipefail

cd /opt/vigilant

if ! docker image inspect vigilant-app:prev >/dev/null 2>&1; then
    echo "ERROR: no vigilant-app:prev image exists — can't instant-rollback."
    echo ""
    echo "Use the git-revert path instead:"
    echo "  (on laptop)  git revert <bad-commit> && git push origin main"
    echo "  (on VPS)     /opt/vigilant/scripts/deploy.sh"
    exit 1
fi

echo "[1/4] Tag the broken :latest with a timestamp for later inspection"
broken_tag="vigilant-app:broken-$(date +%Y%m%d-%H%M%S)"
docker tag vigilant-app:latest "$broken_tag"
echo "     tagged ${broken_tag}"

echo "[2/4] Swap :prev -> :latest"
docker tag vigilant-app:prev vigilant-app:latest

echo "[3/4] Recreate app with the restored image (nginx left running)"
# Same reason deploy.sh uses --no-deps: keep the Wanderer mapper's
# reverse proxy up. Only the app container cycles.
docker compose up -d --no-deps --force-recreate app
docker compose up -d --no-deps nginx  # idempotent; brings nginx back if missing
docker exec vigilant-nginx-1 nginx -s reload

echo "[4/4] Post-rollback checks"
sleep 3
docker exec vigilant-nginx-1 nginx -t
echo "--- last 30s of app logs ---"
docker logs --since 30s vigilant-app-1 2>&1 | tail -20

echo ""
echo "✓ Rolled back to the previous image."
echo "  The broken image is tagged ${broken_tag} for debugging."
echo ""
echo "⚠ REMEMBER: this restored the IMAGE only. The repo still contains the"
echo "  bad commit. Follow up with:"
echo "    (on laptop)  git revert <bad-commit> && git push origin main"
echo "    (on VPS)     /opt/vigilant/scripts/deploy.sh"
echo "  Otherwise the next rebuild will re-introduce the bug."
