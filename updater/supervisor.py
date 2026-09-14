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

Everything above the `# ── Impure half` banner is pure: no filesystem, no
subprocess, no clock. That is what lets the security-critical part — tag
validation — be tested in the main pytest suite with no Docker daemon anywhere
near it. Below the banner is the run loop, which is none of those things.
"""
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

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
#
# It was v1.1.0 until 2026-09-14. That was not a bump: v1.1.0 shipped as a
# CSP/nav release WITHOUT the updater, so the constant named a release with no
# /control mount — precisely the one-way door it exists to prevent. v1.2.0 is
# the release that actually introduces /control. Correcting a value whose
# premise turned out to be false is not the same as moving it; from here it
# does not move.
MIN_ROLLBACK_TAG = "v1.2.0"

_LOCK_MAX_AGE_SECONDS = 30 * 60

_LOG_MAX_LINES = 100
_LOG_MAX_BYTES = 8192

# Markers scripts/deploy.sh and scripts/rollback.sh actually print, mapped to a
# deliberately coarse step vocabulary.
#
# Two different reasons for the coarseness, and they are not the same reason:
#   - deploy.sh genuinely bundles checkout, pull and recreate into one [1/5]
#     block, so finer steps do not exist to be read. Inventing them would mean
#     forking the script, which defeats the point of reusing it unmodified.
#   - rollback.sh DOES emit [1/4] Roll back, [2/4] Pull and [3/4] Recreate as
#     three distinct markers. Those are collapsed to "deploying" ON PURPOSE:
#     three UI steps for what the operator experiences as one recreate is noise,
#     and it keeps both scripts presenting the same vocabulary. Do not "fix"
#     this by expanding them.
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


# ── Impure half: filesystem, subprocess, clock ───────────────────────────────

log = logging.getLogger("updater")

CONTROL = Path(os.environ.get("VIGILANT_CONTROL_DIR", "/control"))
ROOT = os.environ.get("VIGILANT_ROOT", "/opt/vigilant")
POLL_SECONDS = 1.0
HEARTBEAT_SECONDS = 10.0
_SEEN_LIMIT = 50

# Tracks the most recent _tick() failure message so run() can log a persistent
# failure (e.g. an unmounted /control) once with a traceback instead of once a
# second forever — at POLL_SECONDS=1.0 an unlogged rate limit would mean
# ~86,400 identical tracebacks a day.
_last_tick_error: str | None = None

# deploy.sh's two revert outcomes, verified against scripts/deploy.sh:207 and
# :209 rather than assumed. Anchored, and capturing the tag explicitly — the
# tag is NOT the last whitespace-separated token on either line.
_REVERT_OK_RE = re.compile(r"^✓ Reverted to (v\S+?);")
_REVERT_FAILED_RE = re.compile(r"^✗ Revert to (v\S+?) ALSO failed")

# Wall-clock ceiling on one deploy. Slightly under the lock's 30-minute
# staleness so a timed-out run always releases its own lock before another
# process would judge it abandoned.
_RUN_TIMEOUT_SECONDS = 25 * 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_json(path: Path):
    """Parse a JSON file, or None if it is missing, unreadable or malformed.

    Never raises: this runs in a loop that must outlive any single bad file,
    including one half-written by a crash.
    """
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return None


def write_json_atomic(path: Path, payload: dict) -> None:
    """Write via a sibling .tmp then rename, so a reader never sees a partial
    file. The app (Task 4) polls status.json roughly every 2s; without this it
    would eventually read one mid-write and render a parse error as a failure.
    That polling interval is Task 4's design, not yet built — verify it there
    rather than assuming this number stays accurate."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


def claim_request(control: Path) -> tuple[bool, dict | None]:
    """Atomically take ownership of a pending request.

    Returns (claimed, payload):
      (False, None)  nothing was there
      (True,  None)  a request WAS consumed but is unusable — malformed JSON, or
                     valid JSON that is not an object
      (True,  {...}) a usable request

    The two-value return exists because collapsing the middle case into None
    lost it: the rename had already happened, so the operator's click was
    consumed while the app went on showing the PREVIOUS run's status — which may
    read "success". A non-dict payload was worse still, reaching request.get()
    and killing the poll loop outright.

    rename() is atomic within a filesystem. Read-then-unlink is not: two
    claimants could both read before either unlinked. Here the loser's rename
    fails and it simply has nothing to claim.
    """
    src = control / "request.json"
    # request.json.claimed is deliberately never deleted — it is the only
    # post-mortem record of the last request's raw payload if something later
    # goes wrong. Do not "clean it up"; it is a leak of exactly one file.
    dst = control / "request.json.claimed"
    try:
        os.replace(src, dst)
    except OSError:
        return (False, None)
    data = read_json(dst)
    if not isinstance(data, dict):
        return (True, None)
    return (True, data)


def seen_request(request_id: str, seen: list) -> bool:
    """Whether this id has already been handled. Appends it if not.

    Bounded to the last 50 so a long-lived updater cannot grow this without
    limit; replays that old are not a threat model anyone has.
    """
    if request_id in seen:
        return True
    seen.append(request_id)
    del seen[:-_SEEN_LIMIT]
    return False


def build_command(action: str, tag: str, root: str | None = None) -> list[str]:
    """The argv for one action. A LIST, never a shell string — the tag has
    already passed validate_tag(), but defence in depth is free here.

    `root` defaults to None, not the ROOT constant, so it is read at CALL time
    rather than bound once at import: `root: str = ROOT` would freeze whatever
    ROOT was when the module first loaded, and a test monkeypatching ROOT later
    would silently have no effect on this function's default.
    """
    root = root or ROOT
    if action == "update":
        return [f"{root}/scripts/deploy.sh", "--tag", tag]
    if action == "rollback":
        return [f"{root}/scripts/rollback.sh", "--to", tag]
    raise ValueError(f"unknown action: {action!r}")


def _pid_alive(pid: int) -> bool:
    """Does this process EXIST — not "can I signal it".

    `except OSError: return False` is the obvious version and it is wrong:
    os.kill raises PermissionError for a process that exists but is owned by
    another uid, so a live lock holder would be reported dead and its lock
    reclaimed out from under it. Only ProcessLookupError means gone.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_group(proc) -> None:
    """SIGTERM then SIGKILL the process GROUP, so docker/git children die too."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except OSError:
            return
        try:
            proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            continue
    log.error("process group for pid %s survived SIGTERM and SIGKILL", proc.pid)


def _current_tag() -> str | None:
    """The running release, from the last line of .deployed."""
    try:
        with open(f"{ROOT}/.deployed") as fh:
            tags = parse_deployed_tags(fh.read())
        return tags[-1] if tags else None
    except Exception as e:
        # Logged, not just swallowed: a silent failure here makes
        # eligible_rollback_targets() return [], and the operator sees "not an
        # eligible rollback target" — a message that blames THEIR choice for
        # what is actually a mis-mounted /opt/vigilant.
        log.warning("could not read %s/.deployed: %s", ROOT, e)
        return None


def _deployed_text() -> str:
    try:
        with open(f"{ROOT}/.deployed") as fh:
            return fh.read()
    except Exception as e:
        log.warning("could not read %s/.deployed: %s", ROOT, e)
        return ""


def _self_checks() -> dict:
    """One-time environment checks, published so self-hoster environment
    failures show up as a disabled button with a readable reason instead of a
    mid-deploy explosion.

    The socket gid varies by distro and compose in this image is older than the
    host's, so a file the host parses might not parse here.
    """
    checks = {}
    try:
        subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                       check=True, capture_output=True, timeout=15)
        checks["socket"] = "ok"
    except Exception as e:
        checks["socket"] = f"FAIL: {e}"
    try:
        subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"],
                       check=True, capture_output=True, timeout=15)
        checks["git"] = "ok"
    except Exception as e:
        checks["git"] = f"FAIL: {e}"
    try:
        subprocess.run(["docker", "compose", "-f",
                        os.environ.get("VIGILANT_COMPOSE_FILE", "docker-compose.yml"),
                        "config", "--quiet"],
                       cwd=ROOT, check=True, capture_output=True, timeout=30)
        checks["compose"] = "ok"
    except Exception as e:
        checks["compose"] = f"FAIL: {e}"
    try:
        # .deployed is the file the entire rollback list depends on
        # (eligible_rollback_targets reads it fresh on every pickup) — the one
        # thing the checks above did not cover.
        with open(f"{ROOT}/.deployed") as fh:
            fh.read()
        checks["deployed"] = "ok"
    except Exception as e:
        checks["deployed"] = f"FAIL: {e}"
    return checks


def write_heartbeat(checks: dict) -> None:
    """Publish liveness plus the rollback targets the app cannot see for itself.

    The app mounts only /control, so it cannot read /opt/vigilant/.deployed.
    This list is UX only — every target is re-validated on pickup.
    """
    current = _current_tag()
    payload = {
        "version": os.environ.get("VIGILANT_UPDATER_VERSION", "dev"),
        "written_at": _now_iso(),
        "current_tag": current,
        "targets": eligible_rollback_targets(_deployed_text(), current),
        "min_rollback_tag": MIN_ROLLBACK_TAG,
        "checks": checks,
    }
    try:
        write_json_atomic(CONTROL / "updater.json", payload)
    except OSError as e:
        # A fresh named volume is root-owned 0755 until the app's entrypoint
        # chowns it. Retrying is the actual fix; depends_on only orders START,
        # not the entrypoint's chown.
        log.warning("heartbeat write failed (retrying next tick): %s", e)


def _publish(status: dict) -> None:
    try:
        write_json_atomic(CONTROL / "status.json", status)
    except OSError as e:
        log.warning("status write failed: %s", e)


def _new_status(**overrides) -> dict:
    """The status.json schema, defined ONCE.

    This is a contract between two containers — the app (Task 4) reads it —
    and it was previously spelled out verbatim in two places (here and
    run_action's initial publish), so the next field added would have landed
    in only one of them.
    """
    status = {
        "id": None, "action": None, "state": "running", "step": "validating",
        "from_tag": None, "to_tag": None, "message": "", "error": None,
        "reverted_to": None, "log_tail": [],
        "started_at": _now_iso(), "finished_at": None,
    }
    status.update(overrides)
    return status


def _publish_refusal(request: dict, error: str) -> None:
    """Record a request the loop consumed but never ran.

    claim_request() renames request.json away, so a bare `continue` here would
    make the operator's click vanish with only a log line — and the app would go
    on showing the PREVIOUS run's status, which may well read "success".
    """
    _publish(_new_status(
        id=request.get("id"),
        action=request.get("action"),
        state="failed",
        step="failed",
        from_tag=_current_tag(),
        to_tag=request.get("tag"),
        error=error,
        finished_at=_now_iso(),
    ))


def run_action(request: dict) -> None:
    """Execute one claimed request, streaming progress into status.json."""
    action = request.get("action")
    tag = request.get("tag")
    status = _new_status(
        id=request.get("id"),
        action=action,
        from_tag=_current_tag(),
        to_tag=tag,
    )
    _publish(status)

    # Re-validate on pickup. The heartbeat's target list is UX; this is the
    # authority, and it must not trust anything the app wrote.
    if not validate_tag(tag):
        status.update(
            state="failed", step="failed", finished_at=_now_iso(),
            error=(f"{tag!r} is not a deployable release tag — only exact "
                   f"versions like v1.2.3 (prereleases are excluded)."))
        _publish(status)
        return
    if action not in ("update", "rollback"):
        status.update(
            state="failed", step="failed", finished_at=_now_iso(),
            error=(f"unknown action: {action!r}. This is a bug in Vigilant, "
                   f"not something you can fix from here."))
        _publish(status)
        return
    if action == "rollback" and tag not in eligible_rollback_targets(
            _deployed_text(), _current_tag()):
        status.update(
            state="failed", step="failed", finished_at=_now_iso(),
            error=(f"{tag} is not an eligible rollback target — it must be a "
                   f"release this host has run, at or above {MIN_ROLLBACK_TAG}"))
        _publish(status)
        return

    status["step"] = "preflight"
    _publish(status)

    # Bounded, not a plain list: clamp_log_tail() keeps only the last 100
    # entries regardless, but list(lines)[-100:] on an unbounded list still
    # copies the WHOLE list on every call — one per output line — making the
    # accumulate-then-clamp pattern O(N^2) in total output over a long-running
    # deploy. maxlen=200 (double clamp_log_tail's own cap, for headroom) bounds
    # that copy to a constant size.
    lines: deque = deque(maxlen=200)
    timed_out = threading.Event()
    proc = None
    try:
        proc = subprocess.Popen(
            build_command(action, tag),
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            env={**os.environ, "VIGILANT_ROOT": ROOT},
            # Own process group, so the watchdog can kill deploy.sh AND the
            # docker and git children it spawned. Killing only the shell leaves
            # an orphaned `docker pull` holding the daemon.
            start_new_session=True,
            # Pin the codec instead of inheriting the container's locale.
            # deploy.sh emits ✓ → ✗ ⚠ (3-byte UTF-8), and text=True otherwise
            # decodes with locale.getpreferredencoding(). Alpine happens to give
            # UTF-8 today (verified in the real image), but a base-image change
            # would turn that into a UnicodeDecodeError MID-DEPLOY. errors=
            # "replace" means a stray byte from docker's output degrades one
            # character rather than killing a running deploy.
            encoding="utf-8", errors="replace",
        )

        # A watchdog THREAD, not a timeout on wait(). `for line in proc.stdout`
        # blocks with no deadline of its own, and wait(timeout=...) is only
        # reached after EOF — so a hang that keeps printing (a stalled docker
        # pull progress bar) never closes the pipe and the ceiling is never
        # evaluated. Manually reproduced before this was fixed: a script
        # echoing in a loop ran indefinitely past its ceiling. Killing the
        # group here closes the pipe, which ends the read loop, which lets
        # wait() return.
        def _on_timeout():
            # The Timer thread and the main thread race: by the time this
            # fires, proc may already have exited on its own (rc published,
            # lock released) a moment earlier. poll() returning non-None means
            # it is gone — killing an already-reaped pid is a no-op, but
            # setting timed_out here would still overwrite a real success with
            # a false "exceeded N minutes" a moment after the true outcome was
            # already published.
            if proc.poll() is not None:
                return
            timed_out.set()
            _kill_group(proc)

        watchdog = threading.Timer(_RUN_TIMEOUT_SECONDS, _on_timeout)
        watchdog.daemon = True
        watchdog.start()
        try:
            for line in proc.stdout:
                # RAW line — step_for_line matches with startswith(), so
                # prepending a timestamp or prefix here would silently stop
                # progress advancing.
                line = line.rstrip("\n")
                lines.append(line)
                step = step_for_line(line)
                if step:
                    status["step"] = step
                # Parse the tag out with a regex, NOT line.split()[-1]:
                # deploy.sh prints "✓ Reverted to v1.0.0; the site is serving."
                # and the naive version extracts "serving." — rendering
                # "Reverted to serving." in the UI on the failure path, which is
                # the one that matters.
                m = _REVERT_OK_RE.match(line)
                if m:
                    status["reverted_to"] = m.group(1)
                m = _REVERT_FAILED_RE.match(line)
                if m:
                    # Both the deploy AND its revert failed. Nothing automatic
                    # is left; say so as loudly as the status schema allows.
                    status["reverted_to"] = None
                    status["message"] = (
                        f"Revert to {m.group(1)} ALSO failed — the site may be "
                        f"down. Manual intervention required on the host."
                    )
                status["log_tail"] = clamp_log_tail(list(lines))
                _publish(status)
            rc = proc.wait()
        finally:
            watchdog.cancel()
    except Exception as e:
        if proc is not None:
            _kill_group(proc)
        status.update(state="failed", step="failed", finished_at=_now_iso(),
                      error=str(e)[:512], log_tail=clamp_log_tail(list(lines)))
        _publish(status)
        return

    if timed_out.is_set():
        status.update(
            state="failed", step="failed", finished_at=_now_iso(),
            error=(f"{action} exceeded {_RUN_TIMEOUT_SECONDS // 60} minutes and "
                   f"was killed. The host may be mid-deploy — check `docker ps` "
                   f"before retrying."))
    elif rc == 0:
        status.update(state="success", step="done", finished_at=_now_iso(),
                      message=f"{action} to {tag} completed")
    else:
        # Popen.returncode is negative for a signal kill (e.g. -9 for SIGKILL),
        # so a bare f"exited {rc}" would render as "exited -9" — technically
        # true and useless to an operator who does not know POSIX exit codes.
        error = (f"{action} was killed by signal {-rc} — see the log below."
                 if rc < 0 else
                 f"{action} failed (exit {rc}) — see the log below.")
        status.update(state="failed", step="failed", finished_at=_now_iso(),
                      error=error)
    status["log_tail"] = clamp_log_tail(list(lines))
    _publish(status)


def _tick(seen: list, lock_path: Path) -> None:
    """One pass of the poll loop.

    Extracted from run() purely so it can be tested: run() never returns, so
    nothing could exercise its body, and a named design constraint (pid_alive
    semantics) sat with zero coverage as a result.

    There is a narrow window between claim_request() succeeding and the lock
    write below where a SECOND supervisor process could claim a new request and
    run concurrently. Not closed: os.replace() on request.json is the mutex
    that actually matters — it guarantees the same request can never double-run
    — and the deployment model is one sidecar container, so a second supervisor
    is not a scenario this needs to defend against. The lock file's job is
    staleness/reclamation across restarts, not mutual exclusion within a single
    poll; writing it on every idle tick to close a window that cannot occur in
    the real deployment would just be needless I/O on a shared volume.
    """
    held = read_json(lock_path)
    if held is not None and not is_stale_lock(held, time.time(), _pid_alive):
        return
    if held is not None:
        # Unlink it here, not just log it: at POLL_SECONDS=1.0 leaving the file
        # in place made this fire on EVERY tick — ~86,400 identical warnings a
        # day — and "reclaiming" was false at the point it was logged, since
        # nothing is actually reclaimed unless a request happens to follow.
        log.warning("removing a stale update lock held by pid %s", held.get("pid"))
        try:
            os.unlink(lock_path)
        except OSError:
            pass

    claimed, request = claim_request(CONTROL)
    if not claimed:
        return
    if request is None:
        log.warning("consumed an unusable request (malformed or not an object)")
        _publish_refusal(
            {}, "The update request was unreadable and has been discarded. "
                "Please try again.")
        return

    rid = str(request.get("id") or "")
    if not rid:
        log.warning("request has no id — refusing")
        _publish_refusal(
            request, "Request had no id and was refused. This is a bug in "
                     "Vigilant, not something you can fix from here.")
        return
    if seen_request(rid, seen):
        # A genuine replay: the original run already published its outcome, so
        # leave status.json alone rather than overwriting it.
        log.info("ignoring replayed request: %s", rid)
        return

    try:
        write_json_atomic(lock_path, {"pid": os.getpid(),
                                      "started_at": time.time()})
    except OSError as e:
        log.error("could not take the update lock: %s", e)
        _publish_refusal(
            request, f"Could not take the update lock: {e} The /control "
                     f"volume may be root-owned; see the updater "
                     f"troubleshooting section.")
        return

    try:
        run_action(request)
    except Exception as e:
        log.exception("run_action crashed: %s", e)
        _publish_refusal(request, f"The updater crashed mid-run: {e}")
    finally:
        try:
            os.unlink(lock_path)
        except OSError:
            pass


def _reconcile_orphaned_status() -> None:
    """Close out a run that was interrupted by our own death.

    status.json is written by this process and read by the app. If we are
    SIGKILLed, OOM-killed or the host reboots mid-deploy, it is left saying
    "running" with nobody left to finish it — and nothing else ever corrects
    it, so the panel shows a spinner forever. This repo's own notes flag OOM
    (ExitCode 137) as a live hazard on this box, so it is not exotic. We
    cannot know the outcome (the deploy may well have completed), so say
    exactly that rather than guessing.
    """
    status = read_json(CONTROL / "status.json")
    if not isinstance(status, dict) or status.get("state") != "running":
        return
    log.warning("found an orphaned 'running' status from a previous process; "
                "marking it interrupted")
    status.update(
        state="failed", step="failed", finished_at=_now_iso(),
        error=("The updater restarted while this deploy was running, so its "
               "outcome is unknown. Check the version shown above and "
               "`docker ps` on the host before retrying."))
    _publish(status)


def run() -> None:
    """Poll for requests forever. Never exits on a per-request failure."""
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")
    checks = _self_checks()
    log.info("updater starting: checks=%s root=%s control=%s", checks, ROOT, CONTROL)
    _reconcile_orphaned_status()

    seen: list[str] = []
    last_beat = 0.0
    lock_path = CONTROL / "update.lock"

    global _last_tick_error
    while True:
        if time.time() - last_beat >= HEARTBEAT_SECONDS:
            write_heartbeat(checks)
            last_beat = time.time()
        try:
            _tick(seen, lock_path)
        except Exception as e:
            # Belt and braces. _tick guards its own known failure modes; this
            # exists so an UNKNOWN one cannot kill the loop and take the whole
            # feature down until someone SSHes in to restart the container —
            # exactly what a non-dict payload used to do.
            #
            # Rate-limited: a PERSISTENT failure (e.g. /control unmounted)
            # would otherwise log a full traceback once a second forever.
            # Log the first occurrence loudly, then repeats at debug level
            # until the message changes.
            msg = str(e)
            if msg != _last_tick_error:
                log.exception("tick failed: %s", e)
                _last_tick_error = msg
            else:
                log.debug("tick failed (repeat): %s", e)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    run()
