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
