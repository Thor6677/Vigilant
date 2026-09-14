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

# The updater IPC volume, when the profile is enabled. A fresh named volume is
# root-owned 0755, so the app — uid 10001 with a read_only rootfs — could read
# it but not write, and the operator's first click would fail on permissions
# with nothing in the logs to explain it.
#
# 0770 rather than 0755: the updater joins gid 10001 (see compose group_add) so
# both sides need group write, and nothing else on the host has any business
# reading a channel that names releases and requesters.
#
# Guarded on the directory existing, so the default install — where no /control
# is mounted at all — skips this silently rather than failing to boot.
if [ -d /control ]; then
    # ORDER MATTERS, and it is not the obvious one.
    #
    # chmod on a file you do not own requires CAP_FOWNER, which this container
    # deliberately does not have (cap_drop: ALL, cap_add: CHOWN/SETUID/SETGID).
    # A fresh named volume is root-owned, so root may chmod it *before* the
    # chown and may not after. Doing it the natural way round — chown then
    # chmod — fails with "Operation not permitted", and set -e turns that into
    # an app that CRASH-LOOPS the moment the updater profile is first enabled.
    # Found on the throwaway stack, 2026-09-14.
    #
    # Both steps are guarded so the second boot, where the directory is already
    # vigilant-owned and root can no longer chmod it, skips rather than dies.
    if [ "$(stat -c %a /control)" != "770" ]; then
        chmod 0770 /control || echo "entrypoint: could not set /control mode; the updater may be unable to write its heartbeat" >&2
    fi
    if [ "$(stat -c %u /control)" != "$(id -u vigilant)" ]; then
        chown vigilant:vigilant /control
    fi
fi

# Hand off to the real process as the vigilant user.
exec gosu vigilant "$@"
