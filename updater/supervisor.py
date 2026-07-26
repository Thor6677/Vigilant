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
from collections.abc import Callable

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
#
# No leading zeros: "v01.0.0" is the same failure mode one level down — it
# validated and parsed to the identical (1, 0, 0) tuple as "v1.0.0" while
# being a distinct git ref. Semver forbids leading zeros anyway, so
# (?:0|[1-9][0-9]*) — zero itself, or a nonzero digit followed by anything —
# admits "0" and "10" but not "01" or "00".
_TAG_RE = re.compile(
    r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z"
)

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
#
# Every entry here is matched with startswith(), not substring `in` — deploy.sh's
# "[4/5] Startup log scan" step echoes raw `docker logs` output into the same
# stream, so an application log line that happens to mention "[1/5]" or
# "Automatically reverting" must not be mistaken for the real marker. Every
# real marker is printed at column 0 (verified against scripts/deploy.sh and
# scripts/rollback.sh directly, not assumed), so Task 2 MUST hand step_for_line
# the raw script output line with no added timestamp or prefix — prepending
# anything breaks this silently.
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
    ("→ Automatically reverting", "reverting"),
)


def validate_tag(tag) -> bool:
    """Whether `tag` is an exact release tag safe to hand to git and docker."""
    if not isinstance(tag, str):
        return False
    return bool(_TAG_RE.match(tag))


def parse_deployed_tags(text: str) -> list[str]:
    """Tags from a `.deployed` file, in file order, junk lines dropped.

    Format is `<iso8601> <tag>` per line, appended by deploy.sh and rollback.sh.
    """
    tags = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) == 2:
            tags.append(parts[1])
    return tags


def eligible_rollback_targets(deployed_text: str, current_tag: str | None) -> list[str]:
    """Releases this host has run that it is safe to roll back to.

    Newest first, deduped. Excludes anything malformed, anything below
    MIN_ROLLBACK_TAG, and anything at or newer than the running release — a
    control labelled "Roll back" must not offer to move forward, which is what
    the tag field is for. The supervisor re-checks every one of these on pickup;
    the published list is UX, not authority.

    The floor is read from the MIN_ROLLBACK_TAG module constant, not accepted
    as a parameter: a caller-supplied floor is dead surface area (nothing calls
    this with anything but the default) and, worse, a malformed one would
    silently disable the one-way-door guard — the exact fail-open failure mode
    the current_tag handling below is careful to avoid. A test needing a
    different floor can monkeypatch the module constant instead.
    """
    floor = parse_version(MIN_ROLLBACK_TAG)
    current = parse_version(current_tag)
    # Fail CLOSED on an unparseable running version. Skipping the at-or-newer
    # guard instead would offer releases NEWER than the running one, and this
    # same function is the pickup-time re-validation, so a mislabelled "roll
    # back" could actually execute. No current version also means nothing to
    # roll back FROM, so [] is the honest answer either way.
    if current is None:
        return []
    out = {}
    for tag in parse_deployed_tags(deployed_text):
        # validate_tag() MUST run before parse_version() below: parse_version
        # is the laxer regex (app/ops/version.py, shared with the update
        # checker) and happily accepts what validate_tag rejects — prereleases
        # ("v1.3.0-rc1"), Unicode-digit homoglyphs ("v١.٠.٠"), and unanchored
        # forms. A prerelease landing in .deployed is normal operation, not
        # hypothetical: shipping an image under a tag you don't want the
        # update checker to advertise is a documented, supported workflow, and
        # the "[3/5] Record the deploy" step appends whatever it deployed
        # unconditionally.
        if not validate_tag(tag):
            continue
        parsed = parse_version(tag)
        if parsed is None:
            continue
        if floor is not None and parsed[:3] < floor[:3]:
            continue
        if parsed[:3] >= current[:3]:
            continue
        out[tag] = parsed
    return [t for t, _ in sorted(out.items(), key=lambda kv: kv[1], reverse=True)]


def is_stale_lock(lock: dict | None, now: float, pid_alive: Callable[[int], bool]) -> bool:
    """Whether a held `update.lock` may be reclaimed.

    Stale means the holder is gone or has been running implausibly long. Without
    reclamation a killed updater wedges the feature permanently and the only
    recovery is the SSH session this feature exists to avoid — so an
    unparseable lock counts as stale too.

    `pid_alive(pid)` must answer "does this process EXIST", not "can I signal
    it". The obvious `os.kill(pid, 0)` implementation conflates the two:
    `os.kill` raises `PermissionError` for a process that exists but is owned
    by another user, which means alive, not dead. A caller must catch
    `ProcessLookupError` specifically for dead and treat `PermissionError` (and
    success) as alive — get this backwards and a live lock holder owned by a
    different uid gets reclaimed out from under it.

    The 30-minute age cap is not redundant with the pid check: it bounds the
    damage from PID reuse, where the OS recycles a dead updater's pid onto an
    unrelated live process and a pid-only check would report the long-dead
    holder as alive forever.
    """
    if not isinstance(lock, dict):
        return True
    pid = lock.get("pid")
    started = lock.get("started_at")
    # isinstance(True, int) is True — bool subclasses int — so a bare isinstance
    # check alone would accept True/False as pids. Reject those and anything
    # non-positive: os.kill(0, sig) signals the entire process group and
    # os.kill(-1, sig) signals every process the caller's uid can reach, so
    # either would report as "alive" via a real (and catastrophic) side effect
    # rather than a lookup.
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return True
    if not isinstance(started, (int, float)):
        return True
    if now - started > _LOCK_MAX_AGE_SECONDS:
        return True
    return not pid_alive(pid)


def step_for_line(line: str) -> str | None:
    """Map one line of deploy.sh output to a progress step, or None to keep the
    current one. Unrecognised chatter must never reset progress.

    Matches with startswith(), not substring containment: `[4/5] Startup log
    scan` echoes raw `docker logs` output into the same stream, so an
    application log line mentioning a marker mid-line must not walk progress
    backwards. Callers must pass the raw script line — no timestamp or prefix.
    """
    for marker, step in _STEP_MARKERS:
        if line.startswith(marker):
            return step
    return None


def clamp_log_tail(lines: list[str]) -> list[str]:
    """Most recent lines, under both the line and byte caps.

    Both caps matter: 100 lines of ordinary output is small, but 100 lines of a
    docker pull progress dump is not, and this file lives on a shared volume.

    Measured in BYTES, not characters. deploy.sh emits multibyte output on the
    path that matters most — `→ Automatically reverting`, `✓`, `✗`, `⚠` are all
    3-byte UTF-8 — so a character-counted cap silently allowed 2x, and 4x for
    emoji. The truncation below encodes, slices, and decodes with errors="ignore"
    so a cut can never land mid-codepoint and produce invalid UTF-8 that the
    app's json.load would then reject.

    A single oversized line is truncated from the FRONT, keeping the TAIL — the
    ellipsis goes at the start so it is obvious content was dropped. A
    traceback's exception type and message terminate it, and a docker pull
    progress dump's final state terminates it, so in both of the motivating
    cases the informative content is at the end, not the beginning. Keeping the
    head would discard exactly the line that says what went wrong.

    Never returns [] for a non-empty input. Popping until the total fits would
    do exactly that for a single oversized line — routine here, since a docker
    pull progress dump or a traceback arrives as one long line — and an empty
    log is the worst possible rendering of a failed deploy, which is precisely
    when this is the only thing the operator has.
    """
    out = list(lines)[-_LOG_MAX_LINES:]
    while len(out) > 1 and sum(len(l.encode()) for l in out) > _LOG_MAX_BYTES:
        out.pop(0)
    if out and len(out[0].encode()) > _LOG_MAX_BYTES:
        kept = out[0].encode()[-(_LOG_MAX_BYTES - 3):].decode(errors="ignore")
        out[0] = "..." + kept
    return out
