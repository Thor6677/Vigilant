"""Vigilant's update sidecar: claim a request, run the real deploy script,
publish progress.

This process holds the Docker socket, which makes it root-equivalent on the
host by construction — anything that can create containers can create a
privileged one. The design does not pretend otherwise. What it does instead is
narrow the capability to exactly two operations, remove the network attack
surface entirely (there is no listener, so there is nothing to authenticate and
nothing reachable from the bridge), and keep Vigilant itself unprivileged.

It deliberately does NOT reimplement deployment. It shells out to the same
scripts/deploy.sh and scripts/rollback.sh that an SSH session runs, so the
preflights, the dirty-tree refusal, .env pinning, the health loop, the
auto-revert and the .deployed append all come along for free — and a CLI deploy
and an in-app deploy share one history, so either can roll back the other.

Everything above `run()` is pure: no filesystem, no subprocess, no clock. That
is what lets the security-critical part — tag validation — be tested in the
main pytest suite with no Docker daemon anywhere near it.
"""
import re

from app.ops.version import parse_version

# Only exact release tags. This single rule rejects git argument injection
# (`--upload-pack=…`), shell metacharacters, path traversal and prereleases.
# Tags are attacker-influenced input the moment anyone can open a PR against a
# fork, so this runs before anything else and the value is passed as argv —
# never through a shell — even after it passes.
#
# \Z, not $: in Python `$` ALSO matches just before a trailing newline, so
# `^v\d+\.\d+\.\d+$` accepts "v1.0.0\n". \Z anchors to the true end of string.
#
# [0-9] and not \d: Python's \d is Unicode-aware, so \d+ accepts Arabic-Indic
# digits — "v١.٠.٠" passed validation AND parsed to the same (1, 0, 0) tuple as
# "v1.0.0" while being an entirely different git ref. A validator whose whole
# job is "exactly a release tag" must not admit homoglyphs.
_TAG_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+\Z")

# The first release that ships the updater. Rolling back below this checks out
# a docker-compose.yml with no /control mount on the app service and then
# force-recreates it: the app comes back unable to read status OR submit a
# request, and the only way forward is an SSH session. That is a one-way door,
# so it is refused rather than warned about.
#
# THIS CONSTANT NEVER MOVES. It names the earliest release that is safe to roll
# back to, not the release currently being shipped. Bumping it to today's
# version would make eligible_rollback_targets() return nothing, permanently.
MIN_ROLLBACK_TAG = "v1.1.0"

_LOCK_MAX_AGE_SECONDS = 30 * 60

_LOG_MAX_LINES = 100
_LOG_MAX_BYTES = 8192

# Markers scripts/deploy.sh and scripts/rollback.sh actually print. Deliberately
# coarser than the design's draft vocabulary: those scripts do not emit separate
# fetch/pull/recreate markers — all three happen inside their [1/N] block — and
# inventing finer ones would mean forking the scripts, which defeats the point
# of reusing them.
_STEP_MARKERS = (
    ("[0/5]", "preflight"),
    ("[1/5]", "deploying"),
    ("[2/5]", "awaiting-health"),
    ("[3/5]", "finalizing"),
    ("[4/5]", "finalizing"),
    ("[5/5]", "finalizing"),
    ("[1/4]", "deploying"),      # rollback.sh
    ("[2/4]", "deploying"),
    ("[3/4]", "deploying"),
    ("[4/4]", "awaiting-health"),
    ("Automatically reverting", "reverting"),
)


def validate_tag(tag) -> bool:
    """Whether `tag` is an exact release tag safe to hand to git and docker."""
    if not isinstance(tag, str):
        return False
    return bool(_TAG_RE.match(tag))


def parse_deployed(text: str) -> list[str]:
    """Tags from a `.deployed` file, in file order, junk lines dropped.

    Format is `<iso8601> <tag>` per line, appended by deploy.sh and rollback.sh.
    """
    tags = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) == 2:
            tags.append(parts[1])
    return tags


def eligible_rollback_targets(deployed_text: str, current_tag: str | None,
                              min_tag: str = MIN_ROLLBACK_TAG) -> list[str]:
    """Releases this host has run that it is safe to roll back to.

    Newest first, deduped. Excludes anything malformed, anything below `min_tag`
    (see MIN_ROLLBACK_TAG), and anything at or newer than the running release —
    a control labelled "Roll back" must not offer to move forward, which is what
    the tag field is for. The supervisor re-checks every one of these on pickup;
    the published list is UX, not authority.
    """
    floor = parse_version(min_tag)
    current = parse_version(current_tag)
    out = {}
    for tag in parse_deployed(deployed_text):
        if not validate_tag(tag):
            continue
        parsed = parse_version(tag)
        if parsed is None:
            continue
        if floor is not None and parsed[:3] < floor[:3]:
            continue
        if current is not None and parsed[:3] >= current[:3]:
            continue
        out[tag] = parsed
    return [t for t, _ in sorted(out.items(), key=lambda kv: kv[1], reverse=True)]


def is_stale_lock(lock: dict | None, now: float, pid_alive) -> bool:
    """Whether a held `update.lock` may be reclaimed.

    Stale means the holder is gone or has been running implausibly long. Without
    reclamation a killed updater wedges the feature permanently and the only
    recovery is the SSH session this feature exists to avoid — so an
    unparseable lock counts as stale too.
    """
    if not isinstance(lock, dict):
        return True
    pid = lock.get("pid")
    started = lock.get("started_at")
    if not isinstance(pid, int) or not isinstance(started, (int, float)):
        return True
    if now - started > _LOCK_MAX_AGE_SECONDS:
        return True
    return not pid_alive(pid)


def step_for_line(line: str) -> str | None:
    """Map one line of deploy.sh output to a progress step, or None to keep the
    current one. Unrecognised chatter must never reset progress."""
    for marker, step in _STEP_MARKERS:
        if marker in line:
            return step
    return None


def clamp_log_tail(lines: list[str]) -> list[str]:
    """Most recent lines, under both the line and byte caps.

    Both caps matter: 100 lines of ordinary output is small, but 100 lines of a
    docker pull progress dump is not, and this file lives on a shared volume.

    Never returns [] for a non-empty input. Popping until the total fits would
    do exactly that for a single oversized line — routine here, since a docker
    pull progress dump or a traceback arrives as one long line — and an empty
    log is the worst possible rendering of a failed deploy, which is precisely
    when this is the only thing the operator has.
    """
    out = list(lines)[-_LOG_MAX_LINES:]
    while len(out) > 1 and sum(len(l) for l in out) > _LOG_MAX_BYTES:
        out.pop(0)
    if out and len(out[0]) > _LOG_MAX_BYTES:
        out[0] = out[0][:_LOG_MAX_BYTES - 3] + "..."
    return out
