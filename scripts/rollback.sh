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

# readonly, resolved BEFORE .health-env is read: see deploy.sh for the full
# explanation of the split-brain hazard a stale/copied .health-env would
# otherwise open. Here it's worse than in deploy.sh — DEPLOY_LOG drives which
# TAG gets selected as the rollback target, so a retargeted ledger would pick a
# tag from the wrong stack's history, not just write state to the wrong place.
readonly VIGILANT_ROOT="${VIGILANT_ROOT:-/opt/vigilant}"
readonly VIGILANT_IMAGE="${VIGILANT_IMAGE:-ghcr.io/thor6677/vigilant}"
readonly VIGILANT_COMPOSE_FILE="${VIGILANT_COMPOSE_FILE:-docker-compose.yml}"

cd "$VIGILANT_ROOT"

# .health-env is read as data, never executed. Accepted lines: blank, `# comment`,
# and `[export ]KEY=value` where value is unquoted [A-Za-z0-9_./:@%+,=-]*, or
# "double" / 'single' quoted text with no $ ` \ " '. Anything else refuses the
# whole file, so nothing in it can run a command. Setting a readonly variable
# still aborts (see below). Kept identical in deploy.sh, rollback.sh and
# health-check.sh — each re-execs or runs alone, so none can source a shared
# copy; tests/test_health_env_parsing.py fails if they drift apart.
load_health_env() {
    local _he_file="$1" _he_code="$2" _he_line _he_key _he_val _he_export _he_n=0
    [ -r "$_he_file" ] || return 0
    while IFS= read -r _he_line || [ -n "$_he_line" ]; do
        _he_n=$((_he_n + 1))
        if [[ "$_he_line" =~ ^[[:space:]]*(#.*)?$ ]]; then
            continue
        fi
        if ! [[ "$_he_line" =~ ^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
            echo "ERROR: $_he_file line $_he_n is not KEY=value; refusing to read the file." >&2
            exit "$_he_code"
        fi
        _he_export="${BASH_REMATCH[1]}"
        _he_key="${BASH_REMATCH[2]}"
        _he_val="${BASH_REMATCH[3]}"
        case "$_he_val" in
            \"*\"|\'*\')
                _he_val="${_he_val:1:${#_he_val}-2}"
                if [[ "$_he_val" == *[\$\`\\\"\']* ]]; then
                    echo "ERROR: $_he_file line $_he_n ($_he_key) holds a character the shell would expand; refusing to read the file." >&2
                    exit "$_he_code"
                fi ;;
            *)
                if ! [[ "$_he_val" =~ ^[A-Za-z0-9_./:@%+,=-]*$ ]]; then
                    echo "ERROR: $_he_file line $_he_n ($_he_key) must be quoted or plain text; refusing to read the file." >&2
                    exit "$_he_code"
                fi ;;
        esac
        printf -v "$_he_key" '%s' "$_he_val" || exit "$_he_code"
        if [ -n "$_he_export" ]; then
            export "${_he_key?}"
        fi
    done < "$_he_file"
    return 0
}

load_health_env "$VIGILANT_ROOT/.health-env" 1
# Tracks compose's own basename(cwd) project-name derivation; see deploy.sh for
# the verification against production. .health-env may still override this
# because that file legitimately describes the stack it lives in.
APP_CONTAINER="${APP_CONTAINER:-$(basename "$VIGILANT_ROOT")-app-1}"

DEPLOY_LOG="$VIGILANT_ROOT/.deployed"

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

echo "[2/4] Pull $VIGILANT_IMAGE:$TARGET"
docker pull "$VIGILANT_IMAGE:$TARGET"

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
#
# Explicit -f suppresses compose's automatic merge of docker-compose.override.yml.
# Intentional: the app recreate must use the checked-out compose file verbatim.
# Real behaviour change from the previous bare `docker compose up` — don't
# "simplify" it away.
VIGILANT_TAG="$TARGET" docker compose -f "$VIGILANT_COMPOSE_FILE" up -d --no-deps --force-recreate app

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
    echo "    $VIGILANT_ROOT/scripts/rollback.sh --to <tag>"
    exit 1
fi

# Log the rollback so a subsequent bare rollback steps back another release
# rather than bouncing between the same two.
date -u +"%Y-%m-%dT%H:%M:%SZ $TARGET" >> "$DEPLOY_LOG"

echo ""
echo "✓ Rolled back to $TARGET."
echo "  Code and image are both pinned to this tag — nothing further to re-sync."
