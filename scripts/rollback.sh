#!/usr/bin/env bash
# Roll production back to a previously deployed release.
#
# Images live in GHCR, so this can go back an arbitrary number of releases — the
# old one-level `vigilant-app:prev` limitation is gone. And because the code and
# image are both pinned to the same tag, there is no "image rolled back but the
# repo still has the bad commit" mismatch to clean up afterwards: the checkout
# and the pull move together. Once a fix is released, deploy the new tag
# normally.
#
# Usage (on the host):
#   /opt/vigilant/scripts/rollback.sh              # the release before the current one
#   /opt/vigilant/scripts/rollback.sh --to v1.2.3  # a specific release
#
# See what is available:  gh release list   /   cat /opt/vigilant/.deployed
set -euo pipefail

# Same self-modification guard as deploy.sh: this script checks out a tag, which
# rewrites this file while bash is still reading it. Demonstrated 2026-07-26 —
# an unguarded script whose on-disk file shrinks mid-run silently stops early
# AND EXITS 0, so the truncation looks like success. See deploy.sh for the full
# explanation.
if [ -z "${VIGILANT_ROLLBACK_REEXEC:-}" ]; then
    _tmp="$(mktemp -d)"
    cp "${BASH_SOURCE[0]}" "$_tmp/rollback.sh"
    export VIGILANT_ROLLBACK_REEXEC=1
    trap 'rm -rf "$_tmp"' EXIT
    exec bash "$_tmp/rollback.sh" "$@"
fi

cd /opt/vigilant

# shellcheck disable=SC1091
[ -r /opt/vigilant/.health-env ] && source /opt/vigilant/.health-env
APP_CONTAINER="${APP_CONTAINER:-vigilant-app-1}"

IMAGE=ghcr.io/thor6677/vigilant
DEPLOY_LOG=/opt/vigilant/.deployed

TARGET=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --to) TARGET="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

current=$(tail -1 "$DEPLOY_LOG" 2>/dev/null | awk '{print $2}' || true)

if [ -z "$TARGET" ]; then
    # The newest previously-deployed release that is strictly OLDER than what is
    # running, by VERSION order — not by line position.
    #
    # Line position is wrong because rollbacks are themselves logged, so the
    # history is not monotonic. After deploy 1.0.0 → deploy 1.1.0 → rollback,
    # the log reads 1.0.0, 1.1.0, 1.0.0; "second-most-recent line" is 1.1.0 —
    # the broken release we just escaped — and a second bare rollback would
    # redeploy it. Sorting by version instead makes repeated bare rollbacks walk
    # steadily backwards and never forwards.
    #
    # `sort -u -V` orders tags naturally (v1.9.0 < v1.10.0); the awk stops at
    # the running tag and prints the entry just before it, or nothing when the
    # running tag is already the oldest one deployed.
    TARGET=$(awk '{print $2}' "$DEPLOY_LOG" 2>/dev/null \
             | sort -u -V \
             | awk -v c="$current" '$0 == c {exit} {last=$0} END {print last}' || true)
    if [ -z "$TARGET" ]; then
        echo "ERROR: no earlier release than ${current:-<none>} in $DEPLOY_LOG." >&2
        echo "       Either fewer than two releases have been deployed, or you" >&2
        echo "       have already rolled back to the oldest one on record." >&2
        echo "Pass one explicitly:  rollback.sh --to v1.2.3" >&2
        echo "Available releases:   gh release list" >&2
        exit 1
    fi
fi

echo "[1/4] Roll back to $TARGET"
git fetch --tags --prune
git checkout --detach "$TARGET"
[ -f .env ] && chmod 600 .env

echo "[2/4] Pull $IMAGE:$TARGET"
docker pull "$IMAGE:$TARGET"

echo "[3/4] Recreate app"
# Persist the tag into .env (compose reads it for interpolation) for the same
# reason deploy.sh does: otherwise a later bare `docker compose up -d` would
# resolve the image to :latest and silently undo this rollback. See
# _pin_tag_in_env in deploy.sh for the full explanation.
touch .env
grep -v '^VIGILANT_TAG=' .env > .env.tmp 2>/dev/null || true
echo "VIGILANT_TAG=$TARGET" >> .env.tmp
chmod 600 .env.tmp
mv .env.tmp .env

# --no-deps: only the app container cycles. TLS termination and routing live in
# a separate edge stack that this compose file does not own, so a rollback must
# leave it alone.
VIGILANT_TAG="$TARGET" docker compose up -d --no-deps --force-recreate app

echo "[4/4] Health check"
ok=0
for i in 1 2 3 4 5 6 7 8 9 10; do
    sleep 2
    code=$(docker exec "$APP_CONTAINER" python3 -c "import urllib.request,sys
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
docker logs --since 30s "$APP_CONTAINER" 2>&1 | tail -20

if [[ $ok -ne 1 ]]; then
    echo ""
    echo "✗ $TARGET is not serving either. Try an older release:"
    echo "    /opt/vigilant/scripts/rollback.sh --to <tag>"
    exit 1
fi

# Log the rollback so a subsequent bare rollback steps back another release
# rather than bouncing between the same two.
date -u +"%Y-%m-%dT%H:%M:%SZ $TARGET" >> "$DEPLOY_LOG"

echo ""
echo "✓ Rolled back to $TARGET."
echo "  Code and image are both pinned to this tag — nothing further to re-sync."
