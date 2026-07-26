#!/bin/sh
# Drop privileges before launching the app.
#
# Why an entrypoint instead of `USER vigilant` in the Dockerfile:
# the /data Docker volume was root-owned for months. A naive USER
# directive (commit 7216dd1, 2026-04-28) made vigilant unable to
# write WAL files; the cap_drop=ALL container also blocked root in
# the rolled-back :prev image from chowning /data back. Required
# manual host-side sudo recovery and caused a 22 min outage.
#
# This script runs as root (only inside the container — see compose
# security_opt and cap_drop:ALL + cap_add:[CHOWN,SETUID,SETGID]),
# fixes ownership of the volume idempotently, then drops to the
# vigilant uid (10001) via gosu before exec'ing the real CMD.
#
# Idempotent: subsequent boots find /data already owned by vigilant
# and chown is a no-op for matching files.
set -e

# Only chown if any file under /data is NOT already vigilant-owned.
# Avoids gratuitous mtime changes on every container restart.
if [ -d /data ]; then
    if find /data ! -uid "$(id -u vigilant)" -print -quit | grep -q .; then
        chown -R vigilant:vigilant /data
    fi
fi

# Hand off to the real process as the vigilant user.
exec gosu vigilant "$@"
