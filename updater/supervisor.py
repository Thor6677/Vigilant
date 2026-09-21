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

from app.ops.version import is_newer, parse_version

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


# ── The `remote` self-check's vocabulary ─────────────────────────────────────
#
# All pure, so the message an operator actually reads can be asserted in the
# main suite with no git, no network and no container anywhere near it. The
# subprocess half lives below the banner and does nothing but feed these.

# How often a FAILING `remote` check is retried. The other self-checks run once
# at startup and stay put, which is right for them — a wrong DOCKER_GID does not
# heal itself. This one is different: it depends on DNS and outbound network,
# and after a host reboot the sidecar can easily win the race against either.
# One-shot semantics there would leave the Update button disabled, with a
# network error as its reason, until a human SSHed in to restart the container —
# the exact situation this feature exists to avoid.
#
# Five minutes, not every tick: at POLL_SECONDS=1.0 a per-tick retry would mean
# ~17,000 ls-remote calls a day against github.com from every install that has
# this profile enabled. A passing check is never re-run at all.
_REMOTE_RECHECK_SECONDS = 5 * 60

# Userinfo in a URL, i.e. the `user:token@` in `https://user:token@host/path`.
# A self-hoster who cloned with a personal access token embedded in the origin
# URL has that token in git's own error output, and the whole point of this
# check is that its reason gets PUBLISHED — into /control/updater.json, rendered
# into an admin page, and pasted into bug reports. Redaction is applied to the
# URL and to git's stderr, because git echoes the URL back inside the stderr.
#
# An scp-style remote (`USER@HOST:owner/repo`) is deliberately NOT touched:
# there is no `://`, its "userinfo" is a well-known service account name rather
# than a secret, and hiding it would erase the single most important clue on the
# failure path this check exists for.
_URL_USERINFO_RE = re.compile(r"(?<=://)[^/@\s]*@")

# The one host whose SSH remotes the image rewrites to https:// (see the
# GIT_CONFIG_* block in updater/Dockerfile). Named here rather than spelled
# inline below so the classifier and the image cannot drift apart silently;
# tests/test_updater_packaging.py asserts this matches the Dockerfile's
# insteadOf key.
REWRITTEN_SSH_HOST = "github.com"

# Phrases git uses when the remote answered but refused. Distinguishing these
# from "could not resolve host" is what lets the reason say "this repo needs
# credentials the updater does not have" rather than a generic network error.
_AUTH_HINT_RE = re.compile(
    r"could not read Username|could not read Password|Authentication failed|"
    r"terminal prompts disabled|Repository not found|403 Forbidden|"
    r"Permission denied|access denied|Invalid username or password",
    re.IGNORECASE,
)


def redact_userinfo(text: str) -> str:
    """Replace `scheme://user:secret@` with `scheme://***@` everywhere."""
    if not isinstance(text, str):
        return ""
    return _URL_USERINFO_RE.sub("***@", text)


def looks_like_ssh_url(url: str) -> bool:
    """Whether git would try to reach this URL by exec'ing an ssh client.

    Two spellings count, and they parse completely differently:
      - explicit scheme — `ssh://USER@HOST/owner/repo`, `git+ssh://…`
      - scp-style — `USER@HOST:owner/repo`, which has no scheme at all and is
        recognised only by a colon appearing before the first slash.

    That second rule is why this cannot be `"ssh" in url` or a urlparse call:
    urlparse reads an scp-style remote as scheme-less with a path, and the
    string contains no "ssh" anywhere. Checking the first path segment for a
    colon is the same test git itself applies.
    """
    if not isinstance(url, str):
        return False
    url = url.strip()
    if not url:
        return False
    scheme = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*)://", url)
    if scheme:
        return scheme.group(1).lower() in ("ssh", "git+ssh")
    # No scheme: scp-style iff the colon comes before any slash. A bare local
    # path (`/opt/vigilant`, `../mirror.git`) therefore does not match, which
    # matters because a local-path origin is how this gets tested offline.
    return ":" in url.split("/", 1)[0]


def url_host(url: str) -> str:
    """The hostname from either URL spelling, lowercased, or "" if unclear."""
    if not isinstance(url, str):
        return ""
    url = url.strip()
    scheme = re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://(?:[^/@\s]*@)?([^/:\s]+)", url)
    if scheme:
        return scheme.group(1).lower()
    head = url.split("/", 1)[0]
    if ":" in head:
        return head.split(":", 1)[0].rsplit("@", 1)[-1].lower()
    return ""


def remote_failure_reason(effective_url: str | None, stderr: str | None) -> str:
    """The human-actionable half of a failed `remote` check.

    "Actionable" is the requirement, and it is not the same as "accurate". The
    accurate version of this failure is git's own
    `error: cannot run ssh: No such file or directory / fatal: unable to fork`,
    which tells a self-hoster nothing about what to change — and which the panel
    never showed at all, because the check that would have caught it did not
    exist. Every branch below names the thing to change.

    `effective_url` must be the POST-rewrite URL (`git ls-remote --get-url`),
    not the configured one (`git remote get-url`). The difference is the whole
    diagnosis: a REWRITTEN_SSH_HOST origin that the image's insteadOf rules have
    turned into https:// is fine, and an identical-looking one that is still
    SSH by the time git resolves it means those rules are not in effect.
    """
    detail = redact_userinfo(" ".join((stderr or "").split()))[:400]
    url = redact_userinfo((effective_url or "").strip()) or "origin"
    suffix = f" git said: {detail}" if detail else ""

    if looks_like_ssh_url(effective_url or ""):
        if url_host(effective_url or "") == REWRITTEN_SSH_HOST:
            # The rewrite should have caught this one, so the fault is in the
            # container's environment, not in the operator's choice of origin.
            return (
                f"origin still resolves to {url} after URL rewriting. The "
                f"updater image rewrites {REWRITTEN_SSH_HOST} SSH origins to "
                f"https:// via GIT_CONFIG_COUNT/GIT_CONFIG_KEY_*; "
                f"if that is not happening, "
                f"those variables are missing from the sidecar's environment. "
                f"Check `docker exec <updater> env | grep GIT_CONFIG` and that "
                f"nothing strips the environment before deploy.sh runs."
                f"{suffix}")
        return (
            f"origin resolves to {url}, which git can only reach over SSH. This "
            f"container ships no SSH client and holds no key, deliberately: it "
            f"also holds the Docker socket, so a credential stored here would "
            f"hand one compromise both root on the host and push access to the "
            f"source. The in-app updater needs origin to be an anonymously "
            f"readable https:// URL. {REWRITTEN_SSH_HOST} SSH origins are "
            f"rewritten automatically; private repositories and other SSH hosts are not "
            f"supported — deploy those with scripts/deploy.sh over SSH, which "
            f"is unaffected.{suffix}")

    if _AUTH_HINT_RE.search(stderr or ""):
        return (
            f"origin {url} requires credentials. The in-app updater fetches "
            f"anonymously and has none, by design — it holds the Docker socket, "
            f"so no token lives here. A private repository cannot be deployed "
            f"from the browser; use scripts/deploy.sh over SSH instead."
            f"{suffix}")

    return f"could not reach origin {url}.{suffix}"


def should_recheck_remote(checks: dict, last_checked: float | None,
                          now: float, interval: float = _REMOTE_RECHECK_SECONDS) -> bool:
    """Whether the failing `remote` check is due for another attempt.

    Deliberately narrow, and each condition is load-bearing:
      - a missing `remote` key means _self_checks() never ran; the caller that
        owns startup handles that, not the retry loop.
      - "ok" is never re-run. A check that passed has nothing to heal, and
        re-running it would put a network call on a timer for the life of the
        container.
      - the rate limit is measured from the LAST ATTEMPT, not the last failure,
        so a check that keeps timing out does not accumulate back-to-back runs.
    """
    if not isinstance(checks, dict):
        return False
    result = checks.get("remote")
    if result is None or result == "ok":
        return False
    if last_checked is None:
        return True
    return (now - last_checked) >= interval


# ── Self-update ──────────────────────────────────────────────────────────────
#
# scripts/deploy.sh and scripts/rollback.sh recreate ONLY the `app` service
# (`up -d --no-deps --force-recreate app`), so a sidecar keeps running the image
# from whichever release first enabled the profile. Fixes shipped in the sidecar
# image never reached an install through the panel, and nothing said so — the
# skew was silent by construction. That bit production twice: once with a
# sidecar fix that sat undelivered, and once with a manual recreate that
# silently never happened and was only found by inspecting the container.
#
# Adding `updater` to deploy.sh's recreate command CANNOT fix it. The supervisor
# is the process running deploy.sh, and compose is client-driven: recreating the
# sidecar from inside the sidecar kills the client mid-recreate. Worst case the
# old container is gone, the new one never starts, and the install is left with
# no updater at all — recoverable only over the SSH session this feature exists
# to avoid.
#
# So the recreate is performed by a short-lived HELPER container launched from
# the NEW image, which is not part of the compose project and therefore survives
# the recreate it is performing. The pure half below decides whether to do it
# and builds the argv; the impure half runs it.
#
# Nothing here changes deploy.sh or rollback.sh. The CLI path stays exactly as
# it was; a CLI deploy simply leaves the sidecar lagging, which the app's panel
# now says out loud instead of hiding.

SELF_UPDATE_PULLING = "pulling"
SELF_UPDATE_HANDED_OFF = "handed_off"
SELF_UPDATE_DONE = "done"
SELF_UPDATE_FAILED = "failed"

# Fixed, so a leftover from a previous attempt is found and removed rather than
# accumulating one container per try. The prefix is a constant and not derived
# from the live compose project name on purpose: this container must NOT carry
# compose project labels (see build_helper_command), so nothing about it needs
# to match the project, and reading the project name would be one more docker
# call on the path where the old sidecar is about to be killed.
HELPER_CONTAINER_NAME = "vigilant-updater-selfupdate"


def self_update_target(own_version, deployed_tag, run_state) -> str | None:
    """The release this sidecar should move ITSELF to, or None to stay put.

    FORWARD ONLY, and that is a deliberate asymmetry with the app rather than an
    oversight. After a rollback the app goes back and the sidecar stays where it
    is, because:
      - the sidecar is the privileged component (it holds the Docker socket), so
        it should sit on the most-fixed version available, not the one the
        operator happens to want the *app* pinned to;
      - a downgraded sidecar would lose this very feature and strand itself —
        the only way back would be the manual recreate this exists to remove;
      - newer-sidecar/older-app is an already-working, already-shipped
        combination. The app reads the IPC files generically (app/ops/updater.py
        classifies unknown states as idle rather than wedging), and
        MIN_ROLLBACK_TAG guarantees any app the operator can roll back to has a
        /control mount.

    `is_newer` is the same comparator the update checker uses, and it FAILS
    CLOSED on anything it cannot parse — which includes the "dev" version a
    source build reports. So a dev sidecar never self-updates, and neither does
    one whose .deployed tail is junk. No separate "is this dev?" flag is needed,
    exactly as in app/ops/update_check.py.

    `deployed_tag` must come from the last line of .deployed, which deploy.sh
    appends only AFTER the health gate has passed — not from git state, which
    changes at checkout time, before anything is known to work.

    The returned tag is a LABEL: it is published into JSON and rendered in the
    panel (escaped), and is never passed to git, to a shell, or to docker. The
    image actually launched is resolved from the compose file on disk, so a
    hostile .deployed line cannot steer what runs.
    """
    if run_state != "success":
        return None
    if not is_newer(deployed_tag, own_version):
        return None
    return deployed_tag


def build_helper_command(image_ref: str, root: str, compose_file: str,
                         uid: int, gid: int, groups,
                         container_name: str = HELPER_CONTAINER_NAME) -> list[str]:
    """argv for the throwaway container that recreates the `updater` service.

    A LIST, never a shell string. Every flag below is load-bearing:

    --name <fixed>      so the next attempt can find and remove a leftover, and
                        so its exit code and logs are findable afterwards.
    (no compose labels) the helper must NOT look like part of the compose
                        project. If it did, the very `up -d` it is running could
                        reap it mid-recreate — and on Compose versions that do
                        remove profile-disabled services, `--remove-orphans`
                        elsewhere would too.
    (no --rm)           its exit code and its logs ARE the post-mortem. A helper
                        that deleted itself would leave a failed self-update
                        with nothing to read.
    --network none      the image is already local, because step 2 pulled it
                        while the old sidecar was still alive. compose needs no
                        network to recreate a service from a local image, so the
                        most privileged container in the stack gets none.
    --security-opt no-new-privileges
                        same posture as the sidecar it replaces.
    --user / --group-add
                        identical uid:gid and EVERY supplementary group of the
                        running sidecar. The docker-socket gid and the repo
                        owner's uid are the entire reason this can work at all;
                        dropping either gives "permission denied" on the socket
                        or leaves root-owned files in the host repo.
    -v docker.sock      the capability itself, held for a few seconds.
    -v ROOT:ROOT        IDENTICAL on both sides. compose derives project
                        identity from the working directory: mount the repo
                        anywhere else and compose treats it as a NEW project and
                        creates a second set of containers bound to the same
                        volumes instead of recreating the running ones. This is
                        the same constraint the sidecar service itself documents.
    -w ROOT             so `docker compose` picks up that project identity and
                        reads .env from the right place.
    -e VIGILANT_ROOT    the only variable it needs.

    VIGILANT_TAG is deliberately NOT passed. compose must read the pin from
    .env, where deploy.sh's _pin_tag_in_env() just wrote it; passing it here
    would let a stale in-process value win over the file that is the actual
    record of what was deployed.

    `image_ref` is an image ID or digest, never a floating tag: `:latest` could
    resolve to something other than the image step 2 just pulled and verified.

    The command is appended after the image ref, overriding the image's CMD.
    updater/Dockerfile declares CMD and no ENTRYPOINT, which is what makes this
    work against an OLDER-format or NEWER updater image alike — all the helper
    needs from the image is docker-cli-compose, which every updater image has.
    """
    argv = [
        "docker", "run", "--detach",
        "--name", container_name,
        "--network", "none",
        "--security-opt", "no-new-privileges",
        "--user", f"{uid}:{gid}",
    ]
    for group in groups:
        argv += ["--group-add", str(group)]
    argv += [
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "-v", f"{root}:{root}",
        "-w", root,
        "-e", f"VIGILANT_ROOT={root}",
        image_ref,
        "docker", "compose", "-f", compose_file,
        "--profile", "updater", "up", "-d", "--no-deps", "updater",
    ]
    return argv


def reconciled_self_update_state(record, own_version) -> str | None:
    """What a freshly started sidecar should record about a handoff it inherited.

    Returns "done", "failed", or None when there is nothing to reconcile.

    The old sidecar cannot publish the outcome of its own replacement — by the
    time the answer exists it has been SIGTERMed. So it persists `handed_off`
    to /control/selfupdate.json and the NEW process closes the loop by comparing
    the target against its own version:

      - own version is not older than the target  -> the handoff worked: "done".
      - own version is STILL older than the target -> the replacement never
        happened (or brought back the same image): "failed".

    `failed` is reconciled as well as `handed_off`, which is not redundant. The
    old sidecar records `failed` when its bounded wait expires with the helper
    still running — a slow recreate, not a broken one — and it is then killed a
    moment later anyway. Re-deciding from the version that actually came back
    turns that pessimistic guess into the truth. A genuinely failed attempt
    (the pull failed, say) leaves own_version BELOW target, so it stays failed.

    A "dev" own_version cannot satisfy a release target, so it reads as failed
    rather than silently as done — parse_version is what tells those apart, and
    is_newer alone would not: it fails closed on "dev" in BOTH directions.
    """
    if not isinstance(record, dict):
        return None
    if record.get("state") not in (SELF_UPDATE_HANDED_OFF, SELF_UPDATE_FAILED):
        return None
    target = record.get("target")
    if not isinstance(target, str) or not target:
        # Malformed: there is no target to compare against, so there is no
        # honest verdict to publish. Leaving it alone is better than inventing
        # one; the record is cosmetic and the sidecar still works.
        return None
    if is_newer(target, own_version) or parse_version(own_version) is None:
        return SELF_UPDATE_FAILED
    return SELF_UPDATE_DONE


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

# The self-update record, persisted so it survives the restart it describes.
# It is a file on /control and NOT a new IPC channel: the app already reads that
# directory, and adding a second transport for one dict would mean a second
# thing to get wrong.
_SELF_UPDATE_FILE = "selfupdate.json"

# How long the old sidecar waits for its own SIGTERM after launching the helper.
# The helper's work is a local-image recreate of one service, which takes a few
# seconds; 90s is generous enough that a loaded host does not read as a failure,
# and short enough that a helper which will never finish does not hold the
# updater hostage. The wait heartbeats throughout, so the app never flips to
# "no updater" during it.
_HANDOFF_WAIT_SECONDS = 90

# How long to keep waiting after the helper has exited 0 without our SIGTERM
# having arrived. Normally the signal lands first and we never get here; this is
# purely the race window.
_HANDOFF_SIGTERM_GRACE_SECONDS = 15

# Poll interval during the wait. Doubles as the heartbeat interval there, which
# is why it is well under HEARTBEAT_MAX_AGE_SECONDS on the app side.
_HANDOFF_POLL_SECONDS = 5.0

# Ceilings on the docker calls the self-update makes. The pull gets the generous
# one — it is a real network transfer — and it runs while the old sidecar is
# still alive and serving, so a slow registry costs nothing but time.
_SELF_UPDATE_PULL_TIMEOUT_SECONDS = 10 * 60
_SELF_UPDATE_DOCKER_TIMEOUT_SECONDS = 60
# Deliberately short: these run at STARTUP, between heartbeats, so their sum is
# time the app spends seeing a stale heartbeat. Three calls x 10s is the worst
# case, comfortably inside the app's 60s staleness window.
_HELPER_INSPECT_TIMEOUT_SECONDS = 10

# Bound on the helper's log tail as republished into selfupdate.json and the
# admin panel. Same reasoning as clamp_log_tail: this file lives on a shared
# volume and is rendered into a page.
_HELPER_LOG_MAX_BYTES = 1024

# Published in the heartbeat while _self_checks() is still running. NOT an empty
# dict: the panel computes "can the updater run" by rejecting non-"ok" check
# values, so an empty set of checks reads as "everything passed" and would
# render the Update and Rollback buttons ENABLED before a single check had run.
# That is the same fail-open class as the `remote` check that did not exist.
_STARTUP_CHECKS = {
    "startup": ("FAIL: the updater is still starting — its environment checks "
                "have not finished yet. This clears by itself within a few "
                "seconds."),
}


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


# Wall-clock ceiling on the `remote` check. This runs inside _self_checks(),
# which runs before the first heartbeat, so it is also how long a sidecar with
# no outbound network takes to publish anything at all — the panel stays hidden
# for that long after a restart. Twenty seconds is the trade: long enough for a
# slow TLS handshake over a congested link, short enough that a hard network
# failure does not look like a sidecar that failed to start.
_REMOTE_CHECK_TIMEOUT_SECONDS = 20


def _git_env() -> dict:
    """Environment for a read-only git call that must never block on input.

    `{**os.environ, ...}` and NOT a fresh dict. The image sets GIT_CONFIG_COUNT
    and GIT_CONFIG_KEY_*/VALUE_* to rewrite github.com SSH origins to https://
    (see updater/Dockerfile); building a clean environment here would drop them
    and make this check report a failure on a host where deploys actually
    succeed — a false alarm that disables the button.

    GIT_TERMINAL_PROMPT=0 turns "please enter a username" into an immediate
    error. Without it a private-repo origin makes git block on a terminal that
    will never answer, and the check would hit its timeout instead of returning
    the one message that names the real problem.

    GIT_ASKPASS is pinned for the same reason one level up: GIT_TERMINAL_PROMPT
    governs git's OWN prompt, not a configured askpass helper or credential
    manager, which on a developer machine is a GUI keychain that blocks. Pointing
    it at a binary that prints an empty line and exits keeps failure fast.
    """
    return {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/echo",
    }


def _effective_origin_url(env: dict) -> str:
    """origin's URL as git will actually use it — AFTER insteadOf rewriting.

    `ls-remote --get-url`, not `remote get-url`: the latter reports the
    configured string, so on a host whose clone was made over SSH (and whose
    origin the image's environment rewrites to https) it would report an SSH URL for a remote
    that works perfectly, and every failure here would be misdiagnosed as "your
    origin is SSH". This form resolves the name locally and contacts nothing.
    """
    try:
        proc = subprocess.run(["git", "-C", ROOT, "ls-remote", "--get-url", "origin"],
                              capture_output=True, text=True, timeout=15, env=env)
    except Exception:
        return ""
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""


def _remote_check() -> str:
    """Can this container actually FETCH from origin, read-only.

    The check that would have caught the cannot-run-ssh failure. Its absence is
    why the panel showed four green checks and an enabled button on a host where
    the first click could not possibly work: the old `git` check ran
    `git rev-parse HEAD`, which is purely local and passes happily in a
    container with no network, no ssh client and no credentials.

    `--exit-code … HEAD` rather than a bare `ls-remote`: a remote that answers
    but has no refs at all should not read as success, and `HEAD` is the one ref
    every non-empty repository has.

    check=False and stderr read directly, NOT check=True: the other checks in
    _self_checks() format the raised CalledProcessError, whose str() is
    "Command '[...]' returned non-zero exit status 128." — the exit code, the
    argv, and none of the message git actually printed. On this check that
    message is the entire diagnostic value.
    """
    try:
        env = _git_env()
        proc = subprocess.run(
            ["git", "-C", ROOT, "ls-remote", "--exit-code", "origin", "HEAD"],
            capture_output=True, text=True, env=env,
            timeout=_REMOTE_CHECK_TIMEOUT_SECONDS,
        )
        if proc.returncode == 0:
            return "ok"
        return "FAIL: " + remote_failure_reason(_effective_origin_url(env), proc.stderr)
    except subprocess.TimeoutExpired:
        return (f"FAIL: `git ls-remote origin` did not answer within "
                f"{_REMOTE_CHECK_TIMEOUT_SECONDS}s. The sidecar may have no "
                f"outbound network, or origin may be unreachable from this host.")
    except Exception as e:
        # Never raises: this is called from the poll loop as well as at startup,
        # and a failed check must disable the button, not kill the supervisor.
        return "FAIL: " + redact_userinfo(f"could not run git ls-remote: {e}")[:512]


def refresh_remote_check(checks: dict, last_checked: float, now: float) -> float:
    """Re-run a failing `remote` check, in place. Returns the new attempt time.

    Mutates the caller's dict rather than returning a fresh one so that run()'s
    single `checks` object — the same one handed to write_heartbeat() every
    tick — picks the new value up with no further plumbing. Rebuilding it by
    calling _self_checks() again would be the obvious alternative and is wrong:
    that re-runs socket, compose and deployed too, putting a `docker version`
    and a `docker compose config` on a timer forever to fix a network blip.

    This BLOCKS the poll loop for up to _REMOTE_CHECK_TIMEOUT_SECONDS when it
    does run, which delays that iteration's heartbeat: a 10s beat interval can
    stretch to ~30s. The app treats a heartbeat older than 60s as no updater at
    all, so today it fits — but that headroom is now spoken for, and anyone
    tightening the staleness window or lengthening the timeout has to count
    this call against it.
    """
    if not should_recheck_remote(checks, last_checked, now):
        return last_checked
    previous = checks.get("remote")
    checks["remote"] = _remote_check()
    if checks["remote"] == "ok":
        log.info("remote check recovered (was: %s)", previous)
    return now


def _self_checks(on_progress: Callable[[dict], None] | None = None) -> dict:
    """Startup environment checks, published so self-hoster environment
    failures show up as a disabled button with a readable reason instead of a
    mid-deploy explosion.

    The socket gid varies by distro and compose in this image is older than the
    host's, so a file the host parses might not parse here.

    All but one are one-shot: they describe a mounting or permissions mistake,
    which does not fix itself while the container keeps running. `remote` is the
    exception — it depends on the network — and run() re-runs THAT ONE on a
    timer while it is failing. See refresh_remote_check().

    `on_progress` is called with the checks gathered so far after each one, and
    exists because of the restart gap this function sits in the middle of. Add
    up the timeouts below and startup can take 15 + 15 + 20 + 30 = 80 seconds
    before the first heartbeat is written — longer than the app's
    HEARTBEAT_MAX_AGE_SECONDS of 60, so a sidecar restarting into a bad network
    would make the panel vanish entirely while it came up. Beating after each
    check bounds the gap to the single longest timeout (30s, `compose`) instead
    of their sum. run() also beats once BEFORE calling this at all, so the app
    sees a live updater from the first moment.

    Typical numbers are nowhere near those ceilings — every check but `remote`
    is local, and a healthy `remote` answers in well under a second — so the
    real handoff gap is a few seconds. The beats are for the pathological case,
    which is precisely the one where the operator most needs the panel.
    """
    checks = {}

    def _progress():
        if on_progress is not None:
            # A copy: the caller publishes this and we keep mutating ours.
            on_progress(dict(checks))

    try:
        subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                       check=True, capture_output=True, timeout=15)
        checks["socket"] = "ok"
    except Exception as e:
        checks["socket"] = f"FAIL: {e}"
    _progress()
    try:
        subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"],
                       check=True, capture_output=True, timeout=15)
        checks["git"] = "ok"
    except Exception as e:
        checks["git"] = f"FAIL: {e}"
    _progress()
    # Deliberately adjacent to the `git` check above, because it is the half of
    # it that was missing: `rev-parse HEAD` proves the bind mount and the repo
    # ownership, and proves nothing at all about whether this container can
    # reach the remote it is about to fetch a release tag from.
    checks["remote"] = _remote_check()
    _progress()
    try:
        subprocess.run(["docker", "compose", "-f",
                        os.environ.get("VIGILANT_COMPOSE_FILE", "docker-compose.yml"),
                        "config", "--quiet"],
                       cwd=ROOT, check=True, capture_output=True, timeout=30)
        checks["compose"] = "ok"
    except Exception as e:
        checks["compose"] = f"FAIL: {e}"
    _progress()
    try:
        # .deployed is the file the entire rollback list depends on
        # (eligible_rollback_targets reads it fresh on every pickup) — the one
        # thing the checks above did not cover.
        with open(f"{ROOT}/.deployed") as fh:
            fh.read()
        checks["deployed"] = "ok"
    except Exception as e:
        checks["deployed"] = f"FAIL: {e}"
    _progress()
    return checks


def _own_version() -> str:
    """This sidecar's image version, or "dev" for a source build.

    Read at call time, like everything else here, so a test can set it per case.
    """
    return os.environ.get("VIGILANT_UPDATER_VERSION", "dev")


def write_heartbeat(checks: dict, self_update: dict | None = None) -> None:
    """Publish liveness plus the rollback targets the app cannot see for itself.

    The app mounts only /control, so it cannot read /opt/vigilant/.deployed.
    This list is UX only — every target is re-validated on pickup.

    `self_update` is always present in the payload, null when there is nothing
    to report. A key that only appeared sometimes would make the app's "is this
    an older heartbeat that predates the field?" check indistinguishable from
    "nothing is happening", and those render differently.
    """
    current = _current_tag()
    payload = {
        "version": _own_version(),
        "written_at": _now_iso(),
        "current_tag": current,
        "targets": eligible_rollback_targets(_deployed_text(), current),
        "min_rollback_tag": MIN_ROLLBACK_TAG,
        "checks": checks,
        "self_update": self_update,
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


def run_action(request: dict) -> dict:
    """Execute one claimed request, streaming progress into status.json.

    Returns the terminal status it published. The caller needs the outcome to
    decide whether to self-update afterwards, and reading status.json back off
    the volume to learn something this function already knows would be a
    needless round trip through a file another container also writes to.
    """
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
        return status
    if action not in ("update", "rollback"):
        status.update(
            state="failed", step="failed", finished_at=_now_iso(),
            error=(f"unknown action: {action!r}. This is a bug in Vigilant, "
                   f"not something you can fix from here."))
        _publish(status)
        return status
    if action == "rollback" and tag not in eligible_rollback_targets(
            _deployed_text(), _current_tag()):
        status.update(
            state="failed", step="failed", finished_at=_now_iso(),
            error=(f"{tag} is not an eligible rollback target — it must be a "
                   f"release this host has run, at or above {MIN_ROLLBACK_TAG}"))
        _publish(status)
        return status

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
        return status

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
    return status


# ── Self-update: the I/O half ────────────────────────────────────────────────
#
# Every docker call below is its own small function taking argv and returning a
# CompletedProcess, so the tests can fake the subprocess boundary at argv level
# rather than mocking whole behaviours. A past bug in this file hid behind a
# `*args, **kwargs` stub, which is why the fakes here assert on what was run.


def _docker(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    """Run a docker CLI command, never raising. Failure is a returncode."""
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, cwd=ROOT,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            argv, 124, stdout="", stderr=f"timed out after {timeout}s")
    except Exception as e:
        return subprocess.CompletedProcess(argv, 125, stdout="", stderr=str(e))


def _short(text: str | None, limit: int = _HELPER_LOG_MAX_BYTES) -> str:
    """Redact and bound something that is about to be published.

    Goes through redact_userinfo for the same reason the `remote` check's
    reason does: this text lands in /control/selfupdate.json, is rendered into
    an admin page, and gets pasted into bug reports. Bounded in BYTES, sliced
    from the END, and decoded with errors="ignore" so a cut cannot land
    mid-codepoint and produce JSON the app then refuses to parse — the same
    reasoning as clamp_log_tail.
    """
    cleaned = " ".join(redact_userinfo(text or "").split())
    raw = cleaned.encode()
    if len(raw) <= limit:
        return cleaned
    return "..." + raw[-(limit - 3):].decode(errors="ignore")


def _compose_file() -> str:
    return os.environ.get("VIGILANT_COMPOSE_FILE", "docker-compose.yml")


def _compose_updater_image() -> tuple[str | None, str]:
    """The image compose WILL use for the `updater` service. Returns (image, error).

    Read out of `docker compose config` against the file ON DISK rather than
    rebuilt by string-building `f"{registry}:{tag}"`. Two reasons, both real:
    the registry is overridable per install (VIGILANT_IMAGE exists precisely so
    a mirrored stack works), and the tag comes from `${VIGILANT_TAG:-latest}`
    resolved against .env — which deploy.sh's _pin_tag_in_env() has just
    rewritten. Asking compose is asking the only component whose answer is
    authoritative, and it means the sidecar never has to know the naming scheme.

    --profile updater is required: without it compose omits the service
    entirely and the lookup below finds nothing.

    Note that VIGILANT_TAG is NOT in this process's environment (the compose
    file gives the sidecar VIGILANT_ROOT, VIGILANT_CONTROL_DIR and
    VIGILANT_IMAGE, and no env_file), so .env wins the interpolation, which is
    what we want: .env is the record of what was actually deployed.
    """
    proc = _docker(["docker", "compose", "-f", _compose_file(),
                    "--profile", "updater", "config", "--format", "json"],
                   _SELF_UPDATE_DOCKER_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        return None, f"could not read the compose file: {_short(proc.stderr)}"
    try:
        config = json.loads(proc.stdout or "")
        image = config["services"]["updater"]["image"]
    except Exception as e:
        return None, f"could not find services.updater.image in the compose file: {e}"
    if not isinstance(image, str) or not image:
        return None, "the compose file gives the updater service no image"
    return image, ""


def _pull_image(image: str) -> str:
    """Pull `image`, returning "" on success or a readable reason.

    Deliberately done BEFORE anything is touched and while the old sidecar is
    still alive and serving. A missing or unpublished sidecar image then fails
    HERE, with the registry's own words, and the install carries on exactly as
    it was — rather than failing inside a helper that has already killed the
    only updater on the host.
    """
    proc = _docker(["docker", "pull", image], _SELF_UPDATE_PULL_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        return f"could not pull {image}: {_short(proc.stderr or proc.stdout)}"
    return ""


def _image_id(image: str) -> tuple[str | None, str]:
    """The local image ID for `image`. Returns (id, error).

    The helper is launched by ID, never by the tag: `:latest` (the compose
    default when no VIGILANT_TAG is pinned) can resolve to a different image
    than the one just pulled and proved to exist, and the whole point of the
    pull step is that what runs next is a known quantity.
    """
    proc = _docker(["docker", "image", "inspect", image, "--format", "{{.Id}}"],
                   _SELF_UPDATE_DOCKER_TIMEOUT_SECONDS)
    image_id = (proc.stdout or "").strip().splitlines()
    if proc.returncode != 0 or not image_id or not image_id[0]:
        return None, f"could not resolve an image id for {image}: {_short(proc.stderr)}"
    return image_id[0], ""


def _remove_helper() -> None:
    """Delete a helper container left behind by a previous attempt.

    Not --rm on the helper itself, so this is where the cleanup happens. Absent
    is the normal case and is not an error — `docker rm` says "No such
    container" and returns non-zero, which is fine.
    """
    _docker(["docker", "rm", "-f", HELPER_CONTAINER_NAME],
            _HELPER_INSPECT_TIMEOUT_SECONDS)


def _helper_state() -> tuple[bool, int | None]:
    """(running, exit_code) for the helper, or (False, None) if it is gone."""
    proc = _docker(["docker", "inspect", HELPER_CONTAINER_NAME, "--format",
                    "{{.State.Running}} {{.State.ExitCode}}"],
                   _HELPER_INSPECT_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        return False, None
    parts = (proc.stdout or "").strip().split()
    if len(parts) != 2:
        return False, None
    try:
        return parts[0] == "true", int(parts[1])
    except ValueError:
        return False, None


def _helper_log_tail() -> str:
    proc = _docker(["docker", "logs", "--tail", "20", HELPER_CONTAINER_NAME],
                   _HELPER_INSPECT_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        return ""
    return _short((proc.stdout or "") + " " + (proc.stderr or ""))


def _helper_postmortem() -> str:
    """Exit code plus a short log tail, then remove the container.

    Captured BEFORE the removal, which is the whole reason the helper is not
    `--rm`: a container that deleted itself takes the only evidence with it.
    Tolerates the container being absent — an operator may well have cleaned it
    up themselves, and that must not turn a successful self-update into a
    failed one.
    """
    running, code = _helper_state()
    tail = _helper_log_tail()
    _remove_helper()
    if code is None and not tail:
        return ""
    where = "still running" if running else f"exit {code}"
    return _short(f"helper container {where}: {tail}".strip())


def _persist_self_update(record: dict | None) -> dict | None:
    """Write the record where a restarted sidecar will find it.

    The heartbeat alone cannot carry this: it is rewritten from scratch by the
    NEW process, which knows nothing about what the old one was doing. A file
    the new process reads at startup is the only thing that crosses the restart.
    """
    try:
        write_json_atomic(CONTROL / _SELF_UPDATE_FILE, record or {})
    except OSError as e:
        # Cosmetic state, not the feature. A self-update whose record could not
        # be written still works; losing the record only costs the panel one
        # line of explanation.
        log.warning("could not persist the self-update record: %s", e)
    return record


def _read_self_update() -> dict | None:
    record = read_json(CONTROL / _SELF_UPDATE_FILE)
    return record if isinstance(record, dict) and record.get("state") else None


def _reconcile_self_update(record: dict | None) -> dict | None:
    """Close out a handoff the previous process could not report on itself.

    See reconciled_self_update_state() for the decision; this half does the
    docker calls (post-mortem, then removal) and the write.
    """
    state = reconciled_self_update_state(record, _own_version())
    if state is None or state == (record or {}).get("state"):
        return record
    detail = _helper_postmortem()
    out = dict(record or {})
    out["state"] = state
    out["at"] = _now_iso()
    if state == SELF_UPDATE_DONE:
        out["error"] = None
        log.info("self-update to %s completed (%s)", out.get("target"),
                 detail or "no helper container left to inspect")
    else:
        out["error"] = _short(
            f"the updater is still running {_own_version()} after handing off to "
            f"{out.get('target')}, so the replacement never started. Recreate it "
            f"on the host with `docker compose --profile updater up -d updater` "
            f"from the install directory. {detail}")
        log.error("self-update to %s did not take effect: %s",
                  out.get("target"), out["error"])
    return _persist_self_update(out)


def _maybe_self_update(status: dict | None,
                       publish: Callable[[dict | None], None] | None) -> dict | None:
    """Move this sidecar onto the release the app just deployed, if any.

    Called ONLY after a successful run of our own, with the update lock already
    released. Not on a timer and not at startup, both deliberately: a timer
    could fire while an operator's CLI deploy was mid-flight and run two compose
    operations against one project at once. The cost of that choice is that a
    CLI deploy leaves the sidecar lagging — which is exactly what the app's new
    lag warning exists to say out loud.

    A self-update problem must NEVER turn a successful deploy into a failed one.
    status.json is finished and published before this runs and is not touched
    here; everything this function has to say goes into the heartbeat and
    selfupdate.json instead.

    Requests that arrive during the handoff are not lost. The app writes
    /control/request.json and claim_request() takes it with an os.replace()
    rename — so a request written while this container is being replaced simply
    sits there until the NEW supervisor's first _tick() renames it. The rename
    is also what makes double-running impossible: if both processes were
    somehow alive, only one rename can succeed.

    Returns the record to publish, or None when nothing was attempted.
    """
    target = self_update_target(_own_version(), _current_tag(),
                                (status or {}).get("state"))
    if target is None:
        return None

    def record(state: str, error: str | None = None) -> dict:
        rec = {"state": state, "target": target, "error": error, "at": _now_iso()}
        _persist_self_update(rec)
        if publish is not None:
            publish(rec)
        return rec

    log.info("self-update: %s -> %s", _own_version(), target)
    record(SELF_UPDATE_PULLING)

    image, error = _compose_updater_image()
    if image is None:
        return record(SELF_UPDATE_FAILED, error)
    if error := _pull_image(image):
        return record(SELF_UPDATE_FAILED, error)
    image_id, error = _image_id(image)
    if image_id is None:
        return record(SELF_UPDATE_FAILED, error)

    # Any leftover from a previous attempt must go before the new one can take
    # the fixed name.
    _remove_helper()
    argv = build_helper_command(
        image_id, ROOT, _compose_file(),
        os.getuid(), os.getgid(), os.getgroups(),
    )
    proc = _docker(argv, _SELF_UPDATE_DOCKER_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        return record(SELF_UPDATE_FAILED,
                      f"could not start the self-update helper: "
                      f"{_short(proc.stderr or proc.stdout)}")

    rec = record(SELF_UPDATE_HANDED_OFF)
    return _await_handoff(target, rec, publish)


def _await_handoff(target: str, handed_off: dict,
                   publish: Callable[[dict | None], None] | None) -> dict | None:
    """Wait for the helper to replace us, heartbeating throughout.

    Three ways out:
      - SIGTERM. This is the NORMAL path: the helper's `compose up -d updater`
        stops this container, __main__'s handler calls sys.exit(0), and the
        SystemExit raised in this thread propagates straight out of run(). It
        is deliberately NOT caught below — `except Exception` does not catch
        SystemExit, and widening it to BaseException would turn a textbook
        successful handoff into a recorded failure.
      - the helper exits non-zero while we are still alive. The recreate did not
        happen; record it with the helper's own output and carry on serving. The
        deploy that triggered this remains a success.
      - the bounded wait expires. Recorded as failed, pessimistically: the
        replacement may still be seconds away, and if it is, the NEW sidecar's
        startup reconciliation turns this back into "done" by comparing its own
        version against the target. Guessing wrong in the direction of "say
        something" is better than leaving the panel on "upgrading itself…".
    """
    deadline = time.time() + _HANDOFF_WAIT_SECONDS

    def finish(error: str) -> dict:
        rec = {"state": SELF_UPDATE_FAILED, "target": target,
               "error": _short(error), "at": _now_iso()}
        _persist_self_update(rec)
        if publish is not None:
            publish(rec)
        log.error("self-update to %s failed: %s", target, rec["error"])
        return rec

    while time.time() < deadline:
        time.sleep(_HANDOFF_POLL_SECONDS)
        # Keep beating. The app calls an updater with a heartbeat older than
        # HEARTBEAT_MAX_AGE_SECONDS "not running" and hides the whole panel, so
        # a silent wait would make the feature disappear in the middle of the
        # one operation that is hardest to explain afterwards.
        if publish is not None:
            publish(handed_off)
        running, code = _helper_state()
        if running or code is None:
            # Still working, or already gone — `docker inspect` also fails when
            # something has removed the container, which is not a reason to
            # declare failure while we are plainly still waiting to be replaced.
            continue
        if code != 0:
            return finish(f"the self-update helper exited {code} without "
                          f"replacing this container. {_helper_postmortem()}")
        # Exit 0 and we are still here: almost always just the race between the
        # helper finishing and our SIGTERM arriving. Give the signal a moment.
        deadline = min(deadline, time.time() + _HANDOFF_SIGTERM_GRACE_SECONDS)

    return finish(f"the self-update helper did not replace this container "
                  f"within {_HANDOFF_WAIT_SECONDS}s. {_helper_postmortem()}")


def _tick(seen: list, lock_path: Path,
          self_update: dict | None = None,
          publish_beat: Callable[[dict | None], None] | None = None) -> dict | None:
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

    Returns the self-update record to keep publishing, which is `self_update`
    unchanged on every path but the one where a successful run has just
    triggered a handoff. `publish_beat` writes a heartbeat carrying a given
    record; the handoff needs it because it blocks for up to a minute and a half
    and the app must not conclude the updater has died meanwhile.
    """
    held = read_json(lock_path)
    if held is not None and not is_stale_lock(held, time.time(), _pid_alive):
        return self_update
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
        return self_update
    if request is None:
        log.warning("consumed an unusable request (malformed or not an object)")
        _publish_refusal(
            {}, "The update request was unreadable and has been discarded. "
                "Please try again.")
        return self_update

    rid = str(request.get("id") or "")
    if not rid:
        log.warning("request has no id — refusing")
        _publish_refusal(
            request, "Request had no id and was refused. This is a bug in "
                     "Vigilant, not something you can fix from here.")
        return self_update
    if seen_request(rid, seen):
        # A genuine replay: the original run already published its outcome, so
        # leave status.json alone rather than overwriting it.
        log.info("ignoring replayed request: %s", rid)
        return self_update

    try:
        write_json_atomic(lock_path, {"pid": os.getpid(),
                                      "started_at": time.time()})
    except OSError as e:
        log.error("could not take the update lock: %s", e)
        _publish_refusal(
            request, f"Could not take the update lock: {e} The /control "
                     f"volume may be root-owned; see the updater "
                     f"troubleshooting section.")
        return self_update

    status = None
    try:
        status = run_action(request)
    except Exception as e:
        log.exception("run_action crashed: %s", e)
        _publish_refusal(request, f"The updater crashed mid-run: {e}")
    finally:
        try:
            os.unlink(lock_path)
        except OSError:
            pass

    # Deliberately AFTER the finally block above, so the update lock is already
    # released and the run's terminal status is already published. A handoff
    # started while still holding the lock would leave a lock file owned by a
    # pid that no longer exists — recoverable, but only via the staleness
    # timeout, which is 30 minutes of a disabled feature for no reason.
    return _maybe_self_update(status, publish_beat) or self_update


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

    # A heartbeat BEFORE the self-checks, and again after each one.
    #
    # The arithmetic that makes this necessary: the app treats a heartbeat older
    # than HEARTBEAT_MAX_AGE_SECONDS (60s) as no updater at all and hides the
    # panel entirely. A self-updating sidecar's worst-case gap is the old
    # container's last beat (up to HEARTBEAT_SECONDS = 10s before it is
    # stopped), plus the helper's recreate (the image is already local, so a few
    # seconds), plus this process's startup. Startup used to mean the whole of
    # _self_checks(), whose timeouts sum to 15 + 15 + 20 + 30 = 80s — over the
    # limit on its own, before anything else is counted. Typical is a few
    # seconds, but "typical" is not what a restart gap needs to be sized for,
    # and refresh_remote_check() already spends the headroom that used to exist.
    #
    # So: beat immediately, then after every check. The largest remaining gap is
    # one check's own timeout, 30s for `compose`, which leaves real margin.
    #
    # _STARTUP_CHECKS, not {}: an empty checks dict renders in the panel as "no
    # failed checks", which ENABLES the Update and Rollback buttons before a
    # single check has run.
    self_update = _read_self_update()
    write_heartbeat(_STARTUP_CHECKS, self_update)

    # Close out a handoff the previous process could not report on itself. This
    # can make docker calls, so it happens after the first beat is safely out.
    self_update = _reconcile_self_update(self_update)
    write_heartbeat(_STARTUP_CHECKS, self_update)

    checks = _self_checks(
        lambda partial: write_heartbeat({**_STARTUP_CHECKS, **partial}, self_update))
    log.info("updater starting: checks=%s root=%s control=%s", checks, ROOT, CONTROL)
    _reconcile_orphaned_status()

    seen: list[str] = []
    last_beat = 0.0
    # Seeded with the time _self_checks() ran, not with 0.0 or None: those would
    # both make the first loop iteration re-run the network check immediately,
    # milliseconds after the startup one, and then again every interval — which
    # is the opposite of rate limiting. A boot-time failure waits the full
    # interval before its first retry.
    last_remote_check = time.time()
    lock_path = CONTROL / "update.lock"

    global _last_tick_error
    while True:
        # Mutates `checks` in place while `remote` is failing, so the next
        # heartbeat publishes the new value and the panel's button re-enables
        # itself without anyone restarting the container. A no-op once it is ok.
        last_remote_check = refresh_remote_check(checks, last_remote_check, time.time())
        if time.time() - last_beat >= HEARTBEAT_SECONDS:
            write_heartbeat(checks, self_update)
            last_beat = time.time()
        try:
            # The closure is how the handoff keeps beating while it blocks: it
            # carries THIS loop's live `checks` dict, so a heartbeat written
            # from inside _tick is indistinguishable from one written here.
            self_update = _tick(
                seen, lock_path, self_update,
                lambda record: write_heartbeat(checks, record))
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
