"""The app's side of the /control protocol, tested without an updater.

Everything here runs against a tmpdir standing in for the shared volume. That is
the whole reason the module takes its control directory from the environment at
call time rather than binding it at import.
"""
import ast
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.ops import updater as uc


@pytest.fixture
def control(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGILANT_CONTROL_DIR", str(tmp_path))
    return tmp_path


def _beat(control: Path, age_seconds: float = 0.0):
    """Write a heartbeat aged `age_seconds` into the past."""
    p = control / "updater.json"
    p.write_text(json.dumps({"version": "v1.2.0"}))
    if age_seconds:
        past = time.time() - age_seconds
        os.utime(p, (past, past))
    return p


def _now():
    return datetime.now(timezone.utc)


# ── Advisory tag validation ──────────────────────────────────────────────────

@pytest.mark.parametrize("tag", ["v1.0.0", "v0.0.0", "v10.20.30", "v1.2.3"])
def test_valid_tags_accepted(tag):
    assert uc.validate_tag_advisory(tag) is True


@pytest.mark.parametrize("tag", [
    "--upload-pack=/bin/sh",     # git argument injection
    "v1.2.3; rm -rf /",          # shell metacharacters
    "$(whoami)",
    "../../etc/passwd",          # path traversal
    "v1.2.3-rc1",                # prereleases are deliberately not deployable
    "V1.2.3",                    # wrong case
    "1.2.3",                     # missing the v
    "v1.2",                      # not three components
    "v01.0.0",                   # leading zeros are a distinct git ref
    "v1.0.0\n",                  # $ would accept this; \Z must not
    "latest",
    "",
    None,
    123,
])
def test_invalid_tags_rejected(tag):
    assert uc.validate_tag_advisory(tag) is False


# ── Heartbeat freshness ──────────────────────────────────────────────────────

def test_heartbeat_absent_is_not_fresh():
    assert uc.heartbeat_is_fresh(None, time.time()) is False


def test_heartbeat_recent_is_fresh():
    now = 1_000_000.0
    assert uc.heartbeat_is_fresh(now - 5, now) is True


def test_heartbeat_at_the_boundary_is_still_fresh():
    now = 1_000_000.0
    assert uc.heartbeat_is_fresh(now - uc.HEARTBEAT_MAX_AGE_SECONDS, now) is True


def test_heartbeat_beyond_the_boundary_is_stale():
    now = 1_000_000.0
    assert uc.heartbeat_is_fresh(now - uc.HEARTBEAT_MAX_AGE_SECONDS - 1, now) is False


def test_availability_uses_mtime_not_the_file_contents(control):
    """A heartbeat whose *contents* claim it is current is still stale if the
    file has not been rewritten — the two containers share a filesystem, not a
    clock, so mtime is the only trustworthy signal."""
    p = control / "updater.json"
    p.write_text(json.dumps({"written_at": _now().isoformat()}))
    past = time.time() - 3600
    os.utime(p, (past, past))
    assert uc.is_available() is False


def test_available_when_beating(control):
    _beat(control)
    assert uc.is_available() is True


def test_read_heartbeat_is_none_when_stale(control):
    _beat(control, age_seconds=3600)
    assert uc.read_heartbeat() is None


# ── Timestamp parsing ────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["2026-09-13T04:00:00Z", "2026-09-13T04:00:00+00:00"])
def test_parse_iso_accepts_both_spellings(raw):
    assert uc.parse_iso(raw) == datetime(2026, 9, 13, 4, 0, tzinfo=timezone.utc)


def test_parse_iso_assumes_utc_for_naive_input():
    """A naive datetime would raise on comparison with an aware `now`, turning a
    cosmetic format change in the supervisor into a 500."""
    assert uc.parse_iso("2026-09-13T04:00:00").tzinfo is not None


@pytest.mark.parametrize("raw", [None, "", "not-a-date", 42, {}])
def test_parse_iso_rejects_junk(raw):
    assert uc.parse_iso(raw) is None


# ── Run state ────────────────────────────────────────────────────────────────

def test_no_status_is_idle():
    assert uc.run_state(None, _now()) == uc.IDLE


@pytest.mark.parametrize("state", ["success", "failed"])
def test_terminal_status_is_idle(state):
    assert uc.run_state({"state": state}, _now()) == uc.IDLE


def test_unknown_state_is_idle():
    """A status written by a newer updater than the app must not wedge the
    button — skew is structural here, since deploy.sh never updates the sidecar."""
    assert uc.run_state({"state": "quiescing"}, _now()) == uc.IDLE


def test_recent_running_is_busy():
    now = _now()
    started = (now - timedelta(minutes=2)).isoformat().replace("+00:00", "Z")
    assert uc.run_state({"state": "running", "started_at": started}, now) == uc.BUSY


def test_long_running_is_interrupted():
    """The supervisor's own ceiling is 25 minutes. Past that plus grace, nothing
    is going to publish a terminal status, and reporting 'busy' forever would
    wedge the feature behind a status that can never change."""
    now = _now()
    started = (now - timedelta(minutes=40)).isoformat().replace("+00:00", "Z")
    assert uc.run_state({"state": "running", "started_at": started}, now) == uc.INTERRUPTED


def test_running_within_grace_is_still_busy():
    now = _now()
    started = (now - timedelta(seconds=uc._RUN_TIMEOUT_SECONDS + 30)).isoformat()
    assert uc.run_state({"state": "running", "started_at": started}, now) == uc.BUSY


def test_running_without_a_start_time_is_interrupted():
    assert uc.run_state({"state": "running"}, _now()) == uc.INTERRUPTED


def test_running_with_unparseable_start_time_is_interrupted():
    status = {"state": "running", "started_at": "yesterday"}
    assert uc.run_state(status, _now()) == uc.INTERRUPTED


# ── Request building ─────────────────────────────────────────────────────────

def test_build_request_shape():
    req = uc.build_request("update", "v1.2.0", 42)
    assert set(req) == {"id", "action", "tag", "requested_by", "requested_at"}
    assert req["action"] == "update"
    assert req["tag"] == "v1.2.0"
    assert req["requested_by"] == 42
    assert req["requested_at"].endswith("Z")
    assert uc.parse_iso(req["requested_at"]) is not None


def test_build_request_ids_are_unique():
    a = uc.build_request("update", "v1.2.0", 1)["id"]
    b = uc.build_request("update", "v1.2.0", 1)["id"]
    assert a != b


def test_build_request_carries_an_anonymous_requester():
    assert uc.build_request("update", "v1.2.0", None)["requested_by"] is None


@pytest.mark.parametrize("action", ["deploy", "restart", "", None])
def test_build_request_rejects_unknown_actions(action):
    with pytest.raises(uc.InvalidRequest):
        uc.build_request(action, "v1.2.0", 1)


def test_build_request_rejects_bad_tags():
    with pytest.raises(uc.InvalidRequest):
        uc.build_request("update", "--upload-pack=/bin/sh", 1)


# ── Submit ───────────────────────────────────────────────────────────────────

def test_submit_refuses_without_an_updater(control):
    with pytest.raises(uc.UpdaterUnavailable):
        uc.submit("update", "v1.2.0", 1)


def test_submit_writes_the_request(control):
    _beat(control)
    request_id = uc.submit("update", "v1.2.0", 42)
    written = json.loads((control / "request.json").read_text())
    assert written["id"] == request_id
    assert written["action"] == "update"
    assert written["tag"] == "v1.2.0"
    assert written["requested_by"] == 42


def test_submit_leaves_no_temp_files(control):
    """The sidecar claims by renaming request.json; a stray .tmp sibling would
    accumulate in the volume forever."""
    _beat(control)
    uc.submit("update", "v1.2.0", 1)
    assert [p.name for p in control.iterdir() if p.name.endswith(".tmp")] == []


def test_submit_temp_file_is_a_sibling_not_in_tmpdir(control, monkeypatch):
    """os.replace is atomic only within a filesystem, and /control is the one
    writable mount a read_only rootfs has. Catch any regression that writes the
    scratch file somewhere else."""
    _beat(control)
    seen = []
    real_open = open

    def spy(path, *a, **kw):
        seen.append(str(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", spy)
    uc.submit("update", "v1.2.0", 1)
    tmps = [p for p in seen if p.endswith(".tmp")]
    assert tmps and all(p.startswith(str(control)) for p in tmps)


def test_submit_refuses_while_busy(control):
    _beat(control)
    started = (_now() - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    (control / "status.json").write_text(
        json.dumps({"state": "running", "started_at": started})
    )
    with pytest.raises(uc.UpdaterBusy) as exc:
        uc.submit("update", "v1.2.0", 1)
    assert exc.value.state == uc.BUSY
    assert not (control / "request.json").exists()


def test_submit_refuses_when_interrupted(control):
    """An interrupted run needs a human, not a second request piled on top."""
    _beat(control)
    started = (_now() - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    (control / "status.json").write_text(
        json.dumps({"state": "running", "started_at": started})
    )
    with pytest.raises(uc.UpdaterBusy) as exc:
        uc.submit("update", "v1.2.0", 1)
    assert exc.value.state == uc.INTERRUPTED


def test_submit_allowed_after_a_finished_run(control):
    _beat(control)
    (control / "status.json").write_text(json.dumps({"state": "success"}))
    assert uc.submit("update", "v1.2.0", 1)


def test_submit_rejects_a_bad_tag_before_touching_the_volume(control):
    _beat(control)
    with pytest.raises(uc.InvalidRequest):
        uc.submit("update", "v1.2.3; rm -rf /", 1)
    assert not (control / "request.json").exists()


# ── Corrupt input ────────────────────────────────────────────────────────────

def test_malformed_status_does_not_raise(control):
    """status.json is rewritten by another container via rename while this one
    may be mid-read."""
    (control / "status.json").write_text("{not json")
    assert uc.read_status() is None
    assert uc.current_run_state() == uc.IDLE


def test_missing_control_dir_is_not_an_error(monkeypatch, tmp_path):
    """The normal state when the updater profile is off."""
    monkeypatch.setenv("VIGILANT_CONTROL_DIR", str(tmp_path / "nope"))
    assert uc.is_available() is False
    assert uc.read_status() is None
    assert uc.read_heartbeat() is None


# ── The privilege property ───────────────────────────────────────────────────

_FORBIDDEN_IMPORTS = {"subprocess", "docker", "shutil", "socket", "httpx", "requests"}
_FORBIDDEN_CALLS = {"system", "popen", "exec", "eval", "spawn", "fork", "execv"}


def _module_ast():
    return ast.parse(Path(uc.__file__).read_text())


def test_module_imports_nothing_that_can_execute():
    """The app half must stay incapable of executing anything. If this ever
    fails, the design's central claim — that an RCE in Vigilant does not hand
    over the Docker daemon — has quietly stopped being true.

    Parsed rather than grepped: the module's own docstring has to be free to
    *describe* the privilege it does not hold, and a substring search cannot
    tell prose from an import.
    """
    imported = set()
    for node in ast.walk(_module_ast()):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported & _FORBIDDEN_IMPORTS), f"privileged import: {imported & _FORBIDDEN_IMPORTS}"


def test_module_calls_nothing_that_can_execute():
    """Catches the `os` escape hatches the import check cannot: `os` itself is
    legitimately needed for replace/unlink/getpid."""
    called = set()
    for node in ast.walk(_module_ast()):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute):
            called.add(fn.attr)
        elif isinstance(fn, ast.Name):
            called.add(fn.id)
    assert not (called & _FORBIDDEN_CALLS), f"privileged call: {called & _FORBIDDEN_CALLS}"
