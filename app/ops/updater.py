"""App-side client for the update sidecar. File I/O against /control, nothing else.

This module is the unprivileged half of the updater. It writes one JSON file and
reads two others; it imports no Docker client, spawns no subprocess, and holds no
credential. That is the whole point — a remote-code-execution bug in Vigilant
must not hand over the daemon, only the ability to write a request whose contents
the privileged side validates before acting. `tests/test_updater_client.py`
asserts the absence of those imports so the property cannot rot silently.

Deliberately NOT imported from `updater.supervisor`, despite the overlap in tag
validation and timeouts. The app image has no reason to ship `updater/`, and such
an import would pass in the test suite — where both packages are on the path —
then fail at runtime in the container. The duplication is the cheaper mistake.

Feature detection is the heartbeat, not a config flag: profile off means no
updater container, which means no `updater.json`, which means the button hides
and the routes 404. Nothing to keep in sync.
"""
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# The supervisor writes its heartbeat every 10s. 60s tolerates five missed
# writes before the feature disappears from the UI — slack for a loaded host,
# without leaving a dead updater advertised for minutes.
HEARTBEAT_MAX_AGE_SECONDS = 60.0

# Must stay >= the supervisor's own _RUN_TIMEOUT_SECONDS (25 min), plus grace for
# its watchdog to tear the run down and publish a terminal status. Declaring
# "interrupted" while the privileged side is still cleaning up would show the
# operator a host-level problem that is about to resolve itself.
_RUN_TIMEOUT_SECONDS = 25 * 60
_RUN_TIMEOUT_GRACE_SECONDS = 2 * 60

# Advisory only. The authoritative gate is validate_tag() in the supervisor,
# which runs on the privileged side against input it does not trust.
# Do NOT "deduplicate" these into one shared function living here: that moves a
# security check into the component this design assumes can be compromised.
# Two validators is the correct shape, and this one exists purely so a typo is
# rejected with a useful message instead of travelling to the sidecar and coming
# back as a refusal 1-2s later.
_TAG_RE = re.compile(r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")

_ACTIONS = ("update", "rollback")

# Run states the UI has to tell apart. "interrupted" is not a variant of "busy":
# a busy updater will finish on its own, an interrupted one never will, and
# treating the second as the first wedges the feature behind a status that will
# never change — recoverable only over the SSH session this feature exists to
# avoid.
IDLE = "idle"
BUSY = "busy"
INTERRUPTED = "interrupted"


class UpdaterUnavailable(RuntimeError):
    """No updater is running — the compose profile is off, or it has died."""


class UpdaterBusy(RuntimeError):
    """A run is already in flight. Carries the state so callers can distinguish
    a genuine run from an interrupted one."""

    def __init__(self, state: str):
        self.state = state
        super().__init__(f"an update is already {state}")


class InvalidRequest(ValueError):
    """The action or tag is not something worth sending to the sidecar."""


def control_dir() -> Path:
    """Where the shared volume is mounted.

    Read at call time, not bound at import: the test suite points this at a
    tmpdir per test, and a module-level constant would freeze whatever the
    environment held when the app first imported this module.
    """
    return Path(os.environ.get("VIGILANT_CONTROL_DIR", "/control"))


# ── Pure half ────────────────────────────────────────────────────────────────
# No filesystem, no clock. Everything below takes what it needs as an argument
# so the decisions can be tested without a /control directory anywhere.


def validate_tag_advisory(tag) -> bool:
    """Whether `tag` looks like an exact release tag. See _TAG_RE's note: this
    is a UX gate, never the security one."""
    if not isinstance(tag, str):
        return False
    return bool(_TAG_RE.match(tag))


def heartbeat_is_fresh(mtime: float | None, now: float) -> bool:
    """Whether a heartbeat written at `mtime` still counts as live.

    Freshness comes from the file's mtime rather than a timestamp inside it.
    The two containers do not share a clock, and comparing a parsed field
    against `time.time()` would make the feature appear and disappear with any
    drift between them. mtime is set by the kernel doing the rename, on the
    filesystem both sides share.
    """
    if mtime is None:
        return False
    return (now - mtime) <= HEARTBEAT_MAX_AGE_SECONDS


def parse_iso(raw) -> datetime | None:
    """Parse the supervisor's timestamps, or None.

    It writes `...isoformat(timespec="seconds").replace("+00:00", "Z")`, which
    `fromisoformat` could not read before Python 3.11. Normalising the Z keeps
    this working regardless of interpreter, and costs one replace.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A naive timestamp would raise on comparison with an aware `now`. Assume
    # UTC, which is what the supervisor writes.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def run_state(status: dict | None, now: datetime) -> str:
    """Classify the last published status as idle, busy, or interrupted.

    Anything that is not an in-flight run is idle, including a status this
    version does not recognise: the supervisor owns the vocabulary, and a status
    written by a NEWER updater than the app (which happens by construction —
    see the skew note in the design addendum) must not wedge the button.
    """
    if not isinstance(status, dict):
        return IDLE
    if status.get("state") != "running":
        return IDLE

    started = parse_iso(status.get("started_at"))
    if started is None:
        # Running, but we cannot establish since when. Not idle — something is
        # genuinely in an unknown state and the operator should look — and not
        # busy, because a status that can never age out would wedge forever.
        return INTERRUPTED

    age = (now - started).total_seconds()
    if age > (_RUN_TIMEOUT_SECONDS + _RUN_TIMEOUT_GRACE_SECONDS):
        return INTERRUPTED
    return BUSY


def build_request(action: str, tag: str, requested_by: int | None) -> dict:
    """The request.json payload. Raises InvalidRequest rather than writing
    something the sidecar will only refuse.

    `requested_by` is carried through to `request.json.claimed`, which the
    supervisor never deletes — so the record of who asked for a deploy survives
    even when the run itself fails.
    """
    if action not in _ACTIONS:
        raise InvalidRequest(f"unknown action: {action!r}")
    if not validate_tag_advisory(tag):
        raise InvalidRequest(f"not a release tag: {tag!r}")
    return {
        "id": str(uuid.uuid4()),
        "action": action,
        "tag": tag,
        "requested_by": requested_by,
        "requested_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }


# ── Impure half ──────────────────────────────────────────────────────────────


def _read_json(path: Path):
    """Parse a JSON file, or None if missing, unreadable or malformed.

    Never raises. Both files this reads are rewritten by another container via
    rename while this one may be mid-read, and a missing /control is the normal
    state when the updater profile is off — none of which is an error worth
    propagating into a request handler.
    """
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return None


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def read_heartbeat() -> dict | None:
    """The updater's last heartbeat, or None if it is not running."""
    if not is_available():
        return None
    return _read_json(control_dir() / "updater.json")


def read_status() -> dict | None:
    """The current or last run, or None if nothing has ever run."""
    return _read_json(control_dir() / "status.json")


def is_available() -> bool:
    """Whether an updater is alive right now.

    This is the feature flag. Routes 404 and the panel hides when it is False.
    """
    import time

    return heartbeat_is_fresh(_mtime(control_dir() / "updater.json"), time.time())


def has_pending_request() -> bool:
    """Whether a request is written but not yet claimed by the sidecar.

    The window is short — the supervisor polls about once a second — but it is
    exactly the window right after the operator clicks, and status.json still
    holds the PREVIOUS run's outcome throughout it. Without this the panel
    answers a click by re-displaying the last run, which may well read
    "succeeded": the supervisor's own _publish_refusal docstring flags the same
    trap on its side.

    Stateless on purpose. claim_request() renames request.json away as its first
    act, so the file's existence IS the "queued but not yet picked up" signal,
    and every poll can ask independently without the app having to remember
    anything across its own restart.
    """
    return (control_dir() / "request.json").exists()


def current_run_state() -> str:
    return run_state(read_status(), datetime.now(timezone.utc))


def submit(action: str, tag: str, requested_by: int | None) -> str:
    """Queue one request for the sidecar. Returns the new request id.

    Raises UpdaterUnavailable, UpdaterBusy or InvalidRequest — all three are
    things a route needs to render differently, which a bool return could not
    express.

    The busy check is a courtesy, not a mutex: the real exclusion is the
    supervisor's update.lock plus the atomic claim. Two requests racing here
    means one file overwrites the other before pickup, and the loser simply
    never runs — no corruption, and the status poll shows whichever won.
    """
    control = control_dir()
    if not is_available():
        raise UpdaterUnavailable("no updater is running")

    state = current_run_state()
    if state != IDLE:
        raise UpdaterBusy(state)

    payload = build_request(action, tag, requested_by)

    # The tmp file MUST be a sibling in /control: os.replace is atomic only
    # within a filesystem, and /control is in any case the one writable mount an
    # otherwise read_only rootfs has. The pid+uuid suffix keeps two concurrent
    # submits from overwriting each other's tmp before either rename lands —
    # a fixed "request.json.tmp" would let the loser's partial write become the
    # winner's file.
    dst = control / "request.json"
    tmp = control / f"request.json.{os.getpid()}.{payload['id']}.tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, dst)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        logger.error("updater: could not write request: %s", e)
        raise UpdaterUnavailable(f"could not write to {control}: {e}") from e

    logger.info(
        "updater: queued %s to %s (id=%s, by=%s)",
        payload["action"], payload["tag"], payload["id"], requested_by,
    )
    return payload["id"]
