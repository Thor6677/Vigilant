#!/usr/bin/env bash
# Deploy a published release by pulling its image. Nothing is built on the host.
#
# Release CI builds and pushes ghcr.io/thor6677/vigilant:<tag> from a version
# tag (.github/workflows/release.yml), so what runs here is byte-identical to
# what was tested on the dev instance. A host rebuild could resolve different
# base layers — the Dockerfile deliberately tracks floating python/node tags.
#
# The shared reverse proxy lives in a separate stack (/opt/edge/); this script
# only touches the `app` service. Edge nginx re-resolves vigilant's container IP
# at request time, so a fresh container is picked up with no nginx reload.
#
# Usage (on the host):
#   /opt/vigilant/scripts/deploy.sh                 # latest published Release
#   /opt/vigilant/scripts/deploy.sh --tag v1.2.3    # a specific release
#
# Rolling back: scripts/rollback.sh (reads /opt/vigilant/.deployed).
set -euo pipefail

# ── Self-modification guard ──────────────────────────────────────────────────
# Bash reads a script incrementally, by byte offset. This script checks out a
# git tag, which REWRITES THIS FILE ON DISK while bash is still reading it —
# and unlike the old `git pull` on main, that now happens on EVERY deploy, not
# just when this script itself changes. A size change mid-run can make bash
# resume at an offset inside a different statement and execute garbage.
#
# So: copy ourselves out of the working tree and re-exec from there before
# touching git. The copy is what runs; the checkout can then do as it likes.
if [ -z "${VIGILANT_DEPLOY_REEXEC:-}" ]; then
    _tmp="$(mktemp -d)"
    cp "${BASH_SOURCE[0]}" "$_tmp/deploy.sh"
    export VIGILANT_DEPLOY_REEXEC=1
    trap 'rm -rf "$_tmp"' EXIT
    exec bash "$_tmp/deploy.sh" "$@"
fi

cd /opt/vigilant

# Optional: set MAINTENANCE in an untracked `.health-env` beside the repo root
# to point at your ops toolkit's maintenance script, silencing the host service
# monitor for the duration of the deploy. Unset, or not executable, and the step
# is skipped cleanly.
# shellcheck disable=SC1091
[ -r /opt/vigilant/.health-env ] && source /opt/vigilant/.health-env
MAINTENANCE="${MAINTENANCE:-}"
APP_CONTAINER="${APP_CONTAINER:-vigilant-app-1}"

IMAGE=ghcr.io/thor6677/vigilant
DEPLOY_LOG=/opt/vigilant/.deployed
REPO_SLUG="${VIGILANT_REPO:-Thor6677/Vigilant}"

TARGET_TAG=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --tag) TARGET_TAG="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

# Resolve the latest PUBLISHED RELEASE, not the latest tag: a tag with no
# release is deliberately invisible (see release.yml). python3 rather than jq so
# the host needs no extra package.
if [ -z "$TARGET_TAG" ]; then
    TARGET_TAG=$(curl -fsSL --max-time 10 \
        "https://api.github.com/repos/${REPO_SLUG}/releases/latest" \
        | python3 -c "import json,sys; print(json.load(sys.stdin)['tag_name'])") || {
        echo "ERROR: could not resolve the latest release from the GitHub API." >&2
        echo "       Pass an explicit --tag vX.Y.Z to deploy without it." >&2
        exit 1
    }
    echo "     resolved latest release: $TARGET_TAG"
fi

# The tag currently deployed, for the auto-revert path below. Read BEFORE we
# append this deploy's entry.
PREV_TAG=$(tail -1 "$DEPLOY_LOG" 2>/dev/null | awk '{print $2}' || true)

if [ -n "$MAINTENANCE" ] && [ -x "$MAINTENANCE" ]; then
    "$MAINTENANCE" on >/dev/null
    trap '"$MAINTENANCE" off >/dev/null 2>&1 || true' EXIT
fi

echo "[0/5] preflight: external 'web' network + compose declaration"
# The app attaches to an external Docker bridge named `web` shared with the edge
# nginx stack. If it doesn't exist, compose fails mid-deploy with a confusing
# error. Fail fast here instead, before touching any state.
if ! docker network inspect web >/dev/null 2>&1; then
    echo "ERROR: docker network 'web' is missing."
    echo "Create it once with: docker network create web"
    exit 1
fi
# Defensive: confirm the compose file still declares the network as external
# with the right name. A silent edit removing `external: true` would cause
# compose to create a NEW per-project network and detach the app from edge nginx.
if ! grep -qE '^[[:space:]]+external:[[:space:]]+true' docker-compose.yml \
    || ! grep -qE '^[[:space:]]+name:[[:space:]]+web' docker-compose.yml; then
    echo "ERROR: docker-compose.yml no longer declares the 'web' network"
    echo "       as 'external: true, name: web'. Refusing to deploy."
    exit 1
fi
echo "     'web' network present; compose declaration intact"

# Refuse to overwrite local edits silently — surface them so the operator can
# decide what to do (commit, stash, or discard).
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "ERROR: working tree at /opt/vigilant has uncommitted changes."
    git status --short
    echo "Either commit/stash these or run 'git checkout .' before deploying."
    exit 1
fi

_healthy() {
    local i code
    for i in 1 2 3 4 5 6 7 8 9 10; do
        sleep 2
        code=$(docker exec "$APP_CONTAINER" python3 -c "import urllib.request,sys
try:
    sys.stdout.write(str(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status))
except Exception:
    sys.stdout.write('000')" 2>/dev/null || echo "000")
        if [[ "$code" == "200" ]]; then
            echo "     /healthz responded 200 after ${i} attempt(s)"
            return 0
        fi
    done
    return 1
}

_deploy_tag() {
    local tag="$1"
    # Detached HEAD at the release tag, so the compose file, entrypoint and
    # scripts on disk always match the image being run.
    git fetch --tags --prune
    git checkout --detach "$tag"
    # Secrets live in .env — keep it owner-only so a co-located, less-privileged
    # account cannot read SECRET_KEY or the EVE client secret. Idempotent, so it
    # also retro-tightens an install whose .env was created world-readable.
    [ -f .env ] && chmod 600 .env
    docker pull "$IMAGE:$tag"
    VIGILANT_TAG="$tag" docker compose up -d --no-deps --force-recreate app
}

echo "[1/5] Deploy $TARGET_TAG (checkout + pull + recreate)"
_deploy_tag "$TARGET_TAG"

echo "[2/5] Health check"
if ! _healthy; then
    echo ""
    echo "⚠ /healthz never returned 200 for $TARGET_TAG."
    docker logs --since 60s "$APP_CONTAINER" 2>&1 | tail -30
    # PREV_TAG must DIFFER from the tag we just failed on. Right after the
    # manual cutover .deployed holds exactly one entry, so PREV_TAG ==
    # TARGET_TAG and a naive revert would redeploy the same broken image and
    # report it as a recovery.
    if [ -n "$PREV_TAG" ] && [ "$PREV_TAG" != "$TARGET_TAG" ]; then
        echo "→ Automatically reverting to $PREV_TAG"
        # Call the function, NOT rollback.sh: shelling out to an on-disk script
        # after a checkout re-opens the self-modification hazard the guard at
        # the top of this file exists to close.
        _deploy_tag "$PREV_TAG"
        if _healthy; then
            echo "✓ Reverted to $PREV_TAG; the site is serving."
        else
            echo "✗ Revert to $PREV_TAG ALSO failed — manual intervention needed."
        fi
    else
        echo "  No DIFFERENT previous tag in $DEPLOY_LOG — cannot auto-revert."
        echo "  Pick one manually: rollback.sh --to <tag>   (gh release list)"
    fi
    exit 1
fi

echo "[3/5] Record the deploy"
date -u +"%Y-%m-%dT%H:%M:%SZ $TARGET_TAG" >> "$DEPLOY_LOG"

echo "[4/5] Startup log scan"
echo "--- last 30s of app logs ---"
docker logs --since 30s "$APP_CONTAINER" 2>&1 | tail -20
# Defense-in-depth: flag obvious startup errors even when /healthz is green (a
# partial-init bug might serve health but still log tracebacks).
errors=$(docker logs --since 30s "$APP_CONTAINER" 2>&1 | grep -iE 'error|traceback' | head -5 || true)
if [[ -n "$errors" ]]; then
    echo ""
    echo "⚠ Errors detected in startup logs (app is serving but worth investigating):"
    echo "$errors"
fi

echo "[5/5] Done"
running=$(docker exec "$APP_CONTAINER" printenv VIGILANT_VERSION 2>/dev/null || echo '?')
echo ""
echo "✓ Deployed $TARGET_TAG (container reports: $running)"
echo "  To roll back: /opt/vigilant/scripts/rollback.sh"
