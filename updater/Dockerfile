# Vigilant's update sidecar.
#
# Deliberately NOT built from the app image: the app must never carry a Docker
# client, and this must never carry the app's dependencies or its code. The one
# thing they share is app/ops/version.py, copied in below — two implementations
# of "is this release newer" would eventually disagree.
#
# Build from the REPO ROOT, not from updater/:
#   docker build -f updater/Dockerfile -t vigilant-updater:local .
FROM alpine:3.20

# docker-cli-compose here is v2.27.0 while the host's is newer. The supervisor's
# startup self-check runs `docker compose config` against the real file so that
# skew surfaces as a readable reason, not a mid-deploy failure.
#
# curl is UNREACHABLE on the supervisor's own path and is here deliberately:
# scripts/deploy.sh curls the GitHub releases API to resolve "latest" when no
# --tag is given, and build_command() always passes one. It stays so that a
# human running `docker exec ... deploy.sh` inside this container — the natural
# thing to try when debugging a stuck update — does not hit a bare
# "curl: not found" on the fallback branch. Roughly 200 KB for a debugging
# affordance on the day the UI is already broken.
#
# THERE IS NO openssh HERE, AND THERE MUST NOT BE. This container holds the
# Docker socket, which makes it root-equivalent on the host; handing it the
# host's GitHub key as well would mean a bug in the least-defended part of the
# stack yields both root on the box and push access to the source it deploys.
# The absence of an ssh client is what forces the HTTPS rewrite below, and
# tests/test_updater_packaging.py fails if anyone adds one back.
RUN apk add --no-cache \
        bash \
        curl \
        docker-cli \
        docker-cli-compose \
        git \
        python3

# No USER directive on purpose. The uid must match the OWNER OF THE HOST REPO
# or the updater leaves root-owned .env and .git objects behind and bricks the
# SSH fallback this feature keeps. That uid varies per install, so compose
# supplies it — see the `user:` key on the updater service.
#
# HOME because that uid is not in /etc/passwd; git tolerates HOME=/ but the
# rootfs is read-only in compose, so point it somewhere writable.
ENV HOME=/tmp \
    PYTHONPATH=/opt/updater \
    PYTHONUNBUFFERED=1 \
    VIGILANT_CONTROL_DIR=/control \
    VIGILANT_ROOT=/opt/vigilant

# Fetch github.com over HTTPS even when the host clone's origin is an SSH URL.
#
# A deployment whose clone was made over SSH has an scp-style origin.
# scripts/deploy.sh — which this image runs UNMODIFIED, on purpose — starts with
# `git fetch --tags --prune`, and git resolves such an origin by exec'ing an ssh
# client. There is none here (see the apk block above), so an in-app update on
# such a host dies on
# "error: cannot run ssh: No such file or directory / fatal: unable to fork"
# before it has changed anything.
#
# The fix is a rewrite, not an ssh client. The repository is public and this
# side is FETCH-ONLY — it checks out a published release tag and never pushes —
# so an anonymous HTTPS fetch is strictly more capability than is needed, and it
# requires no key, no known_hosts and no ssh-agent in a container that already holds
# the Docker socket.
#
# Via GIT_CONFIG_COUNT rather than a config file for three reasons, all of them
# load-bearing:
#   - The uid this runs as is not in /etc/passwd and HOME is a tmpfs, so
#     ~/.gitconfig is neither guaranteed to exist nor guaranteed to survive.
#     Environment needs no writable filesystem at all.
#   - Environment is INHERITED. run_action() spawns deploy.sh with
#     env={**os.environ, ...}, so deploy.sh's own git children pick this up
#     without deploy.sh knowing the sidecar exists — which is the whole point of
#     reusing the script unmodified. tests/test_updater_supervisor.py pins that
#     passthrough so a later "scrub the environment" change cannot break it
#     silently.
#   - It also applies to a human running `docker exec … deploy.sh` in here,
#     which is the first thing anyone tries when an update is stuck. A config
#     file written at build time into a HOME that compose may remap would not.
#
# Two entries, not one: git's `insteadOf` matches a literal prefix, and the two
# spellings of an SSH remote — scp-style (`USER@HOST:owner/repo`) and the
# `ssh://` scheme — share no common prefix. A clone made with either must end up
# resolving to the same `https://` URL. The two VALUE_ lines below are those two
# prefixes verbatim; they are the only place in this repo that has to spell one
# out, because they ARE the configuration git matches against.
#
# Requires git >= 2.31 for GIT_CONFIG_COUNT; alpine 3.20 ships 2.45. The test
# suite proves the rewrite against real git using the values parsed out of THIS
# FILE, so a base-image downgrade fails the build rather than the deploy.
#
# Deliberately github.com only. Rewriting every host would silently redirect a
# self-hoster's private GitLab or Gitea origin to an https:// URL that may not
# exist; a narrow rule that fails visibly in the `remote` self-check is better
# than a broad one that fails mysteriously at fetch time.
ENV GIT_CONFIG_COUNT=2 \
    GIT_CONFIG_KEY_0="url.https://github.com/.insteadOf" \
    GIT_CONFIG_VALUE_0="git@github.com:" \
    GIT_CONFIG_KEY_1="url.https://github.com/.insteadOf" \
    GIT_CONFIG_VALUE_1="ssh://git@github.com/"

ARG VIGILANT_VERSION=dev
ENV VIGILANT_UPDATER_VERSION=${VIGILANT_VERSION}

WORKDIR /opt/updater
COPY app/__init__.py       /opt/updater/app/__init__.py
COPY app/ops/__init__.py   /opt/updater/app/ops/__init__.py
COPY app/ops/version.py    /opt/updater/app/ops/version.py
COPY updater/__init__.py   /opt/updater/updater/__init__.py
COPY updater/supervisor.py /opt/updater/updater/supervisor.py

LABEL org.opencontainers.image.title="Vigilant Updater" \
      org.opencontainers.image.version="${VIGILANT_VERSION}"

CMD ["python3", "-m", "updater.supervisor"]
