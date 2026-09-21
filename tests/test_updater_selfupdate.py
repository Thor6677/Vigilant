"""The sidecar's self-update: the decision, the helper argv, the reporting.

There is no Docker daemon in this suite and there must never be one. Every
subprocess boundary the self-update crosses is a small named function taking
argv and returning a CompletedProcess, so the fakes below assert on the ARGV
that was run rather than stubbing out a behaviour. That shape is deliberate: a
previous bug in this file hid behind a `*args, **kwargs` stub that accepted
anything and returned None, and the test kept passing for the wrong reason.

Everything in the first half is a pure function — no filesystem, no clock, no
subprocess — for the same reason tag validation is: the decision that moves a
privileged container onto a new image should be readable and exhaustively
covered without a container anywhere near it.
"""
import json
import subprocess

import pytest

from updater.supervisor import (
    SELF_UPDATE_DONE,
    SELF_UPDATE_FAILED,
    SELF_UPDATE_HANDED_OFF,
    SELF_UPDATE_PULLING,
    build_helper_command,
    helper_container_name,
    reconciled_self_update_state,
    self_update_target,
)


# ── The decision: should this sidecar move, and to what ──────────────────────
#
# Forward only. After a rollback the app goes back and the sidecar stays: it is
# the privileged component, so it sits on the most-fixed version, and a
# downgraded sidecar would lose this feature and strand itself.

@pytest.mark.parametrize("own,deployed,state,expected", [
    # The happy path: our own run succeeded and the app moved forward.
    ("v1.2.2", "v1.2.3", "success", "v1.2.3"),
    ("v1.2.2", "v2.0.0", "success", "v2.0.0"),
    ("v1.2.2", "v1.3.0", "success", "v1.3.0"),

    # Equal: nothing to do. This is the state every install settles into.
    ("v1.2.3", "v1.2.3", "success", None),

    # Older target — a rollback. NEVER follow it down.
    ("v1.2.3", "v1.2.2", "success", None),
    ("v2.0.0", "v1.0.0", "success", None),

    # The run did not succeed, so .deployed may describe a reverted state and
    # the sidecar has no business acting on it.
    ("v1.2.2", "v1.2.3", "failed", None),
    ("v1.2.2", "v1.2.3", "running", None),
    ("v1.2.2", "v1.2.3", None, None),

    # Unparseable versions fail CLOSED, in both directions. "dev" is the
    # sentinel a source build reports, so a dev sidecar never self-updates and
    # needs no flag of its own.
    ("dev", "v1.2.3", "success", None),
    ("v1.2.2", "dev", "success", None),
    ("dev", "dev", "success", None),
    ("v1.2.2", None, "success", None),
    (None, "v1.2.3", "success", None),
    ("v1.2.2", "not-a-version", "success", None),
    ("v1.2.2", "", "success", None),
])
def test_self_update_decision_table(own, deployed, state, expected):
    assert self_update_target(own, deployed, state) == expected


def test_a_prerelease_the_app_is_actually_running_is_followed():
    """Prereleases are not DEPLOYABLE targets (validate_tag rejects them), but
    a host running one got there through the documented escape hatch, and its
    sidecar image exists. Following the app onto it is right; the tag is a label
    here, never an argument to git or a shell."""
    assert self_update_target("v1.2.2", "v1.3.0-rc1", "success") == "v1.3.0-rc1"


def test_the_full_release_outranks_its_own_prerelease():
    assert self_update_target("v1.3.0-rc1", "v1.3.0", "success") == "v1.3.0"
    assert self_update_target("v1.3.0", "v1.3.0-rc1", "success") is None


# ── The helper's name is per stack, not per daemon ───────────────────────────
#
# Container names are unique per Docker DAEMON, and this repo runs a second
# stack on the same daemon by design. _remove_helper() removes by name before
# launching, so one hardcoded name would let one stack's sidecar `rm -f` another
# stack's helper mid-recreate — exactly the "old container gone, new one never
# started" outcome the helper exists to prevent — or fail to launch on a name
# conflict. A second stack silently acting on the first is a defect this project
# has already had once.

def test_the_default_install_keeps_the_obvious_name():
    assert helper_container_name("/opt/vigilant") == "vigilant-updater-selfupdate"


def test_two_stacks_on_one_daemon_get_two_names():
    assert helper_container_name("/opt/vigilant") != \
        helper_container_name("/opt/vigilant-dev")
    assert helper_container_name("/opt/vigilant-dev") == \
        "vigilant-dev-updater-selfupdate"
    assert helper_container_name("/srv/apps/vigilant-b") == \
        "vigilant-b-updater-selfupdate"


def test_a_trailing_slash_does_not_change_the_name():
    """/opt/vigilant and /opt/vigilant/ are the same stack, and basename() on
    the second returns "" — which would produce a name starting with a dash."""
    assert helper_container_name("/opt/vigilant/") == \
        helper_container_name("/opt/vigilant")


@pytest.mark.parametrize("root", ["/", "", None, "///"])
def test_a_rootless_root_still_yields_a_valid_container_name(root):
    """Docker rejects a name starting with a dash outright, so the fallback is
    not cosmetic."""
    name = helper_container_name(root)
    assert not name.startswith("-")
    assert name == "vigilant-updater-selfupdate"


def test_the_name_matches_the_convention_used_beside_it():
    """compose derives its project from the working directory and
    scripts/deploy.sh derives the app container as
    `$(basename "$VIGILANT_ROOT")-app-1`. Same shape, so the helper sits next to
    containers an operator recognises."""
    for root in ("/opt/vigilant", "/opt/vigilant-dev"):
        base = root.rsplit("/", 1)[-1]
        assert helper_container_name(root).startswith(base + "-")


def test_every_naming_site_goes_through_one_function(sup, monkeypatch):
    """Launch, remove, inspect, logs and post-mortem must all agree. Deriving
    the name one way in one place and another way in another means removing —
    or failing to find — a container belonging to a different stack."""
    monkeypatch.setattr(sup, "ROOT", "/srv/apps/vigilant-b")
    expected = "vigilant-b-updater-selfupdate"
    docker = FakeDocker({**_happy_responses(),
                         ("docker", "inspect"): _ok("false 0\n"),
                         ("docker", "logs"): _ok("done")})
    monkeypatch.setattr(sup, "_docker", docker)
    monkeypatch.setattr(sup, "_await_handoff", lambda target, rec, publish: rec)
    monkeypatch.setattr(sup, "_current_tag", lambda: "v1.2.3")

    sup._maybe_self_update({"state": "success"}, None)
    sup._helper_postmortem()

    # Everything that addresses a CONTAINER. `docker compose`, `docker pull`
    # and `docker image inspect` address a compose project or an image and are
    # correctly nameless here.
    container_calls = [c for c in docker.calls
                       if c[1] in ("run", "rm", "logs")
                       or c[1:2] == ["inspect"]]
    assert len(container_calls) >= 4, container_calls
    for call in container_calls:
        assert expected in call, call
        assert "vigilant-updater-selfupdate" not in call


# ── The helper's argv ────────────────────────────────────────────────────────

ROOT = "/opt/vigilant"
IMAGE_ID = "sha256:ffeeddccbbaa99887766554433221100ffeeddccbbaa99887766554433221100"


def _argv(**overrides):
    kwargs = dict(image_ref=IMAGE_ID, root=ROOT, compose_file="docker-compose.yml",
                  uid=1000, gid=1000, groups=[983, 10001],
                  container_name=helper_container_name(ROOT))
    kwargs.update(overrides)
    return build_helper_command(**kwargs)


def _pairs(argv, flag):
    """Every value following `flag` in argv."""
    return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == flag]


def test_the_command_is_a_list_not_a_shell_string():
    argv = _argv()
    assert isinstance(argv, list)
    assert all(isinstance(a, str) for a in argv)


def test_it_runs_detached():
    argv = _argv()
    assert argv[:3] == ["docker", "run", "--detach"]


def test_it_uses_the_fixed_helper_name():
    assert _pairs(_argv(), "--name") == ["vigilant-updater-selfupdate"]


def test_it_carries_no_compose_project_labels():
    """If the helper looked like part of the project, the very `up -d` it runs
    could reap it mid-recreate — and `--remove-orphans` elsewhere would too on
    the Compose versions that remove profile-disabled services."""
    argv = _argv()
    assert "--label" not in argv
    assert not any("com.docker.compose" in a for a in argv)


def test_it_is_not_rm():
    """Its exit code and its logs ARE the post-mortem of a failed self-update.
    A helper that deleted itself would take the only evidence with it."""
    assert "--rm" not in _argv()


def test_it_gets_no_network():
    """The image was pulled while the old sidecar was still alive, so it is
    already local and compose needs no network to recreate from it."""
    assert _pairs(_argv(), "--network") == ["none"]


def test_it_keeps_no_new_privileges():
    assert _pairs(_argv(), "--security-opt") == ["no-new-privileges"]


def test_it_runs_as_the_same_user_and_every_supplementary_group():
    """The docker-socket gid and the repo owner's uid are the entire reason
    this can work: drop either and it is "permission denied" on the socket, or
    root-owned objects left in the host repo."""
    argv = _argv(uid=1000, gid=1000, groups=[983, 10001, 27])
    assert _pairs(argv, "--user") == ["1000:1000"]
    assert _pairs(argv, "--group-add") == ["983", "10001", "27"]


def test_group_ids_are_stringified():
    """os.getgroups() returns ints; argv must be strings or Popen raises."""
    argv = _argv(groups=[983])
    assert "983" in argv and 983 not in argv


def test_no_groups_still_produces_a_valid_command():
    argv = _argv(groups=[])
    assert "--group-add" not in argv
    assert argv[-1] == "updater"


def test_it_mounts_the_docker_socket():
    assert "/var/run/docker.sock:/var/run/docker.sock" in _pairs(_argv(), "-v")


def test_the_repo_is_mounted_at_an_identical_path_on_both_sides():
    """compose derives project identity from the working directory. Mount the
    repo anywhere else and compose treats it as a NEW project, creating a second
    set of containers bound to the same volumes instead of recreating the
    running ones — the failure this repo's compose file already documents."""
    for root in ("/opt/vigilant", "/srv/apps/vigilant", "/opt/vigilant-dev"):
        argv = _argv(root=root)
        assert f"{root}:{root}" in _pairs(argv, "-v")
        host, _, container = f"{root}:{root}".partition(":")
        assert host == container
        assert _pairs(argv, "-w") == [root]
        assert f"VIGILANT_ROOT={root}" in _pairs(argv, "-e")


def test_it_never_passes_vigilant_tag():
    """compose must read the pin from .env, which deploy.sh's _pin_tag_in_env()
    has just rewritten. A VIGILANT_TAG in the helper's environment would let a
    stale in-process value beat the file that records what was deployed."""
    argv = _argv()
    assert not any(a.startswith("VIGILANT_TAG") for a in argv)
    assert "VIGILANT_TAG" not in " ".join(argv)


def test_the_image_is_referenced_by_id_not_a_floating_tag():
    argv = _argv()
    assert IMAGE_ID in argv
    assert "latest" not in " ".join(argv)
    # It sits between the flags and the command override.
    assert argv[argv.index(IMAGE_ID) + 1] == "docker"


def test_the_command_override_recreates_only_the_updater_service():
    argv = _argv(compose_file="docker-compose.yml")
    tail = argv[argv.index(IMAGE_ID) + 1:]
    assert tail == ["docker", "compose", "-f", "docker-compose.yml",
                    "--profile", "updater", "up", "-d", "--no-deps", "updater"]


def test_a_custom_compose_file_is_honoured():
    argv = _argv(compose_file="compose.prod.yml")
    assert _pairs(argv, "-f") == ["compose.prod.yml"]


def test_the_profile_is_passed_or_compose_does_not_know_the_service_exists():
    argv = _argv()
    assert "--profile" in argv
    assert argv[argv.index("--profile") + 1] == "updater"


# ── Startup reconciliation of the persisted record ───────────────────────────

@pytest.mark.parametrize("record,own,expected", [
    # The handoff worked: the version that came back IS the target.
    ({"state": SELF_UPDATE_HANDED_OFF, "target": "v1.2.3"}, "v1.2.3", SELF_UPDATE_DONE),
    # It worked and then some — another deploy landed first. Still done.
    ({"state": SELF_UPDATE_HANDED_OFF, "target": "v1.2.3"}, "v1.3.0", SELF_UPDATE_DONE),
    # Still the old image: the replacement never started.
    ({"state": SELF_UPDATE_HANDED_OFF, "target": "v1.2.3"}, "v1.2.2", SELF_UPDATE_FAILED),
    # A dev build cannot be the release that was handed off to.
    ({"state": SELF_UPDATE_HANDED_OFF, "target": "v1.2.3"}, "dev", SELF_UPDATE_FAILED),

    # A pessimistic `failed` from the old sidecar's expired wait is re-decided
    # from the version that actually came back.
    ({"state": SELF_UPDATE_FAILED, "target": "v1.2.3"}, "v1.2.3", SELF_UPDATE_DONE),
    # A genuinely failed attempt stays failed.
    ({"state": SELF_UPDATE_FAILED, "target": "v1.2.3"}, "v1.2.2", SELF_UPDATE_FAILED),

    # Terminal or absent: nothing to reconcile.
    ({"state": SELF_UPDATE_DONE, "target": "v1.2.3"}, "v1.2.3", None),
    ({"state": SELF_UPDATE_PULLING, "target": "v1.2.3"}, "v1.2.3", None),
    (None, "v1.2.3", None),

    # Malformed: no target to compare against, so no honest verdict.
    ({"state": SELF_UPDATE_HANDED_OFF}, "v1.2.3", None),
    ({"state": SELF_UPDATE_HANDED_OFF, "target": None}, "v1.2.3", None),
    ({"state": SELF_UPDATE_HANDED_OFF, "target": ""}, "v1.2.3", None),
    ({"state": SELF_UPDATE_HANDED_OFF, "target": 123}, "v1.2.3", None),
    ("not a dict", "v1.2.3", None),
    ({}, "v1.2.3", None),
])
def test_reconciliation_decision_table(record, own, expected):
    assert reconciled_self_update_state(record, own) == expected


# ── The I/O half, with the subprocess boundary faked at argv level ───────────


class FakeDocker:
    """Answers docker calls by matching the start of the argv.

    Records every call so a test can assert on what actually ran. Anything
    unmatched returns a non-zero result rather than silently succeeding, so a
    test cannot pass because a command it forgot to model did nothing.
    """

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        for prefix, result in self.responses.items():
            if argv[:len(prefix)] == list(prefix):
                return result
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="unmatched")

    def ran(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


def _ok(stdout=""):
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def _fail(stderr="boom", code=1):
    return subprocess.CompletedProcess([], code, stdout="", stderr=stderr)


@pytest.fixture
def sup(tmp_path, monkeypatch):
    """The supervisor module pointed at a tmpdir for both /control and the repo."""
    import updater.supervisor as module
    monkeypatch.setattr(module, "CONTROL", tmp_path)
    monkeypatch.setattr(module, "ROOT", str(tmp_path))
    monkeypatch.setenv("VIGILANT_UPDATER_VERSION", "v1.2.2")
    (tmp_path / ".deployed").write_text(
        "2026-09-01T00:00:00Z v1.2.2\n2026-09-20T00:00:00Z v1.2.3\n")
    return module


def _record(sup):
    """The persisted record, or None when nothing has been written yet."""
    path = sup.CONTROL / "selfupdate.json"
    return json.loads(path.read_text()) if path.exists() else None


COMPOSE_CONFIG = json.dumps(
    {"services": {"updater": {"image": "ghcr.io/example/vigilant-updater:v1.2.3"}}})


def _happy_responses(image="ghcr.io/example/vigilant-updater:v1.2.3"):
    return {
        ("docker", "compose"): _ok(COMPOSE_CONFIG),
        ("docker", "pull"): _ok(),
        ("docker", "image", "inspect"): _ok(IMAGE_ID + "\n"),
        ("docker", "rm"): _ok(),
        ("docker", "run"): _ok("c0ffee\n"),
        ("docker", "inspect"): _fail("No such object"),
        ("docker", "logs"): _fail(),
    }


def test_a_failed_run_never_triggers_a_self_update(sup, monkeypatch):
    docker = FakeDocker(_happy_responses())
    monkeypatch.setattr(sup, "_docker", docker)
    assert sup._maybe_self_update({"state": "failed"}, None) is None
    assert docker.calls == []


def test_a_rollback_leaves_the_sidecar_alone(sup, monkeypatch):
    """.deployed ends on an OLDER tag than the sidecar's own version."""
    docker = FakeDocker(_happy_responses())
    monkeypatch.setattr(sup, "_docker", docker)
    monkeypatch.setenv("VIGILANT_UPDATER_VERSION", "v1.2.3")
    (sup.CONTROL / ".deployed").write_text(
        "2026-09-01T00:00:00Z v1.2.3\n2026-09-20T00:00:00Z v1.2.2\n")
    assert sup._maybe_self_update({"state": "success"}, None) is None
    assert docker.calls == []


def test_the_image_is_pulled_before_anything_is_touched(sup, monkeypatch):
    """A missing sidecar image must fail HERE, with the registry's own words,
    while the old container is still alive and serving — not inside a helper
    that has already stopped the only updater on the host."""
    docker = FakeDocker({**_happy_responses(),
                         ("docker", "pull"): _fail("manifest unknown")})
    monkeypatch.setattr(sup, "_docker", docker)

    record = sup._maybe_self_update({"state": "success"}, None)

    assert record["state"] == SELF_UPDATE_FAILED
    assert "manifest unknown" in record["error"]
    assert docker.ran("docker", "pull")
    assert not docker.ran("docker", "run")


def test_the_image_comes_from_the_compose_file_not_string_building(sup, monkeypatch):
    docker = FakeDocker(_happy_responses())
    monkeypatch.setattr(sup, "_docker", docker)
    monkeypatch.setattr(sup, "_await_handoff", lambda target, rec, publish: rec)
    sup._maybe_self_update({"state": "success"}, None)

    config = docker.ran("docker", "compose")[0]
    assert "--profile" in config and "updater" in config
    assert config[-2:] == ["--format", "json"]
    pull = docker.ran("docker", "pull")[0]
    assert pull[-1] == "ghcr.io/example/vigilant-updater:v1.2.3"


def test_an_unreadable_compose_file_fails_without_launching_anything(sup, monkeypatch):
    docker = FakeDocker({**_happy_responses(),
                         ("docker", "compose"): _fail("yaml: line 3")})
    monkeypatch.setattr(sup, "_docker", docker)
    record = sup._maybe_self_update({"state": "success"}, None)
    assert record["state"] == SELF_UPDATE_FAILED
    assert "yaml: line 3" in record["error"]
    assert not docker.ran("docker", "pull")
    assert not docker.ran("docker", "run")


def test_a_compose_file_without_an_updater_service_fails_readably(sup, monkeypatch):
    docker = FakeDocker({**_happy_responses(),
                         ("docker", "compose"): _ok(json.dumps({"services": {}}))})
    monkeypatch.setattr(sup, "_docker", docker)
    record = sup._maybe_self_update({"state": "success"}, None)
    assert record["state"] == SELF_UPDATE_FAILED
    assert "services.updater.image" in record["error"]
    assert not docker.ran("docker", "run")


def test_a_leftover_helper_is_removed_before_the_new_one_is_launched(sup, monkeypatch):
    docker = FakeDocker(_happy_responses())
    monkeypatch.setattr(sup, "_docker", docker)
    monkeypatch.setattr(sup, "_await_handoff",
                        lambda target, rec, publish: rec)

    sup._maybe_self_update({"state": "success"}, None)

    order = [c[:2] for c in docker.calls]
    assert order.index(["docker", "rm"]) < order.index(["docker", "run"])
    assert docker.ran("docker", "rm")[0][-1] == helper_container_name(sup.ROOT)


def test_the_helper_is_launched_from_the_pulled_image_id(sup, monkeypatch):
    docker = FakeDocker(_happy_responses())
    monkeypatch.setattr(sup, "_docker", docker)
    monkeypatch.setattr(sup, "_await_handoff", lambda target, rec, publish: rec)
    monkeypatch.setattr(sup.os, "getuid", lambda: 1000)
    monkeypatch.setattr(sup.os, "getgid", lambda: 1000)
    monkeypatch.setattr(sup.os, "getgroups", lambda: [983, 10001])

    sup._maybe_self_update({"state": "success"}, None)

    run = docker.ran("docker", "run")[0]
    assert IMAGE_ID in run
    assert "ghcr.io/example/vigilant-updater:v1.2.3" not in run
    assert f"{sup.ROOT}:{sup.ROOT}" in run
    assert "--group-add" in run


def test_the_states_are_published_in_order(sup, monkeypatch):
    published = []
    docker = FakeDocker(_happy_responses())
    monkeypatch.setattr(sup, "_docker", docker)
    monkeypatch.setattr(sup, "_await_handoff", lambda target, rec, publish: rec)

    sup._maybe_self_update({"state": "success"}, published.append)

    assert [p["state"] for p in published] == [SELF_UPDATE_PULLING,
                                               SELF_UPDATE_HANDED_OFF]
    assert {p["target"] for p in published} == {"v1.2.3"}
    assert _record(sup)["state"] == SELF_UPDATE_HANDED_OFF


def test_the_record_survives_on_disk_for_the_next_process(sup, monkeypatch):
    docker = FakeDocker(_happy_responses())
    monkeypatch.setattr(sup, "_docker", docker)
    monkeypatch.setattr(sup, "_await_handoff", lambda target, rec, publish: rec)
    sup._maybe_self_update({"state": "success"}, None)
    assert set(_record(sup)) == {"state", "target", "error", "at"}


def test_the_run_status_is_never_touched_by_a_self_update_failure(sup, monkeypatch):
    """A self-update problem must not turn a successful deploy into a failed
    one — the deploy DID succeed, and the operator's site is up."""
    status = {"id": "r1", "state": "success", "action": "update",
              "to_tag": "v1.2.3"}
    (sup.CONTROL / "status.json").write_text(json.dumps(status))
    docker = FakeDocker({**_happy_responses(), ("docker", "pull"): _fail()})
    monkeypatch.setattr(sup, "_docker", docker)

    sup._maybe_self_update(status, None)

    assert json.loads((sup.CONTROL / "status.json").read_text())["state"] == "success"
    assert status["state"] == "success"


# ── The wait for our own SIGTERM ─────────────────────────────────────────────

def test_the_wait_heartbeats_so_the_app_does_not_lose_the_panel(sup, monkeypatch):
    """A silent wait would age the heartbeat past HEARTBEAT_MAX_AGE_SECONDS and
    make the whole panel vanish during the one operation hardest to explain."""
    beats = []
    monkeypatch.setattr(sup, "_docker", FakeDocker(_happy_responses()))
    monkeypatch.setattr(sup.time, "sleep", lambda s: None)
    monkeypatch.setattr(sup, "_HANDOFF_POLL_SECONDS", 0)

    clock = iter([0.0] + [float(i) for i in range(1, 400)])
    monkeypatch.setattr(sup.time, "time", lambda: next(clock))

    sup._await_handoff("v1.2.3", {"state": SELF_UPDATE_HANDED_OFF,
                                  "target": "v1.2.3"}, beats.append)

    handed_off = [b for b in beats if b["state"] == SELF_UPDATE_HANDED_OFF]
    assert len(handed_off) > 1


def test_a_helper_that_exits_non_zero_is_recorded_with_its_output(sup, monkeypatch):
    docker = FakeDocker({
        **_happy_responses(),
        ("docker", "inspect"): _ok("false 3\n"),
        ("docker", "logs"): _ok("no configuration file provided"),
    })
    monkeypatch.setattr(sup, "_docker", docker)
    monkeypatch.setattr(sup.time, "sleep", lambda s: None)

    record = sup._await_handoff("v1.2.3", {"state": SELF_UPDATE_HANDED_OFF,
                                           "target": "v1.2.3"}, None)

    assert record["state"] == SELF_UPDATE_FAILED
    assert "exited 3" in record["error"]
    assert "no configuration file provided" in record["error"]
    # Removed only AFTER its logs were read.
    calls = [c[:2] for c in docker.calls]
    assert calls.index(["docker", "logs"]) < calls.index(["docker", "rm"])


def test_our_own_sigterm_is_the_normal_exit_and_is_not_caught(sup, monkeypatch):
    """__main__ installs a SIGTERM handler calling sys.exit(0), so the success
    path of the wait is a SystemExit raised in this thread. Catching it — which
    a bare `except:` or `except BaseException` would — turns a textbook
    successful handoff into a recorded failure."""
    monkeypatch.setattr(sup, "_docker", FakeDocker(_happy_responses()))

    def _sigterm(_seconds):
        raise SystemExit(0)

    monkeypatch.setattr(sup.time, "sleep", _sigterm)

    with pytest.raises(SystemExit):
        sup._await_handoff("v1.2.3", {"state": SELF_UPDATE_HANDED_OFF,
                                      "target": "v1.2.3"}, None)
    # Nothing terminal was written: the NEW process closes this loop out by
    # comparing its own version against the target.
    assert _record(sup) is None


def test_an_expired_wait_is_recorded_as_failed(sup, monkeypatch):
    monkeypatch.setattr(sup, "_docker", FakeDocker(_happy_responses()))
    monkeypatch.setattr(sup.time, "sleep", lambda s: None)
    clock = iter([0.0, 10_000.0, 10_001.0, 10_002.0, 10_003.0])
    monkeypatch.setattr(sup.time, "time", lambda: next(clock))

    record = sup._await_handoff("v1.2.3", {"state": SELF_UPDATE_HANDED_OFF,
                                           "target": "v1.2.3"}, None)
    assert record["state"] == SELF_UPDATE_FAILED
    assert "did not replace this container" in record["error"]


# ── Startup: closing out the inherited record ────────────────────────────────

def test_startup_records_done_when_the_new_version_is_the_target(sup, monkeypatch):
    monkeypatch.setenv("VIGILANT_UPDATER_VERSION", "v1.2.3")
    docker = FakeDocker({("docker", "inspect"): _ok("false 0\n"),
                         ("docker", "logs"): _ok("Container updater Started"),
                         ("docker", "rm"): _ok()})
    monkeypatch.setattr(sup, "_docker", docker)
    (sup.CONTROL / "selfupdate.json").write_text(json.dumps(
        {"state": SELF_UPDATE_HANDED_OFF, "target": "v1.2.3",
         "error": None, "at": "2026-09-21T10:00:00Z"}))

    record = sup._reconcile_self_update(sup._read_self_update())

    assert record["state"] == SELF_UPDATE_DONE
    assert record["error"] is None
    assert _record(sup)["state"] == SELF_UPDATE_DONE
    # The finished helper is inspected, then removed.
    assert docker.ran("docker", "inspect")
    assert docker.ran("docker", "rm")[0][-1] == helper_container_name(sup.ROOT)


def test_startup_records_failed_when_the_old_version_came_back(sup, monkeypatch):
    monkeypatch.setenv("VIGILANT_UPDATER_VERSION", "v1.2.2")
    monkeypatch.setattr(sup, "_docker", FakeDocker({("docker", "rm"): _ok()}))
    (sup.CONTROL / "selfupdate.json").write_text(json.dumps(
        {"state": SELF_UPDATE_HANDED_OFF, "target": "v1.2.3",
         "error": None, "at": "2026-09-21T10:00:00Z"}))

    record = sup._reconcile_self_update(sup._read_self_update())

    assert record["state"] == SELF_UPDATE_FAILED
    # The message has to name the manual fallback, or it reads as "you are
    # stuck" — the same requirement the `remote` check's reasons carry.
    assert "docker compose --profile updater up -d updater" in record["error"]


def test_startup_tolerates_the_helper_container_being_gone(sup, monkeypatch):
    """An operator may well have cleaned it up. That must not turn a successful
    self-update into a failed one."""
    monkeypatch.setenv("VIGILANT_UPDATER_VERSION", "v1.2.3")
    monkeypatch.setattr(sup, "_docker", FakeDocker(
        {("docker", "inspect"): _fail("No such object"),
         ("docker", "logs"): _fail("No such container"),
         ("docker", "rm"): _fail("No such container")}))
    (sup.CONTROL / "selfupdate.json").write_text(json.dumps(
        {"state": SELF_UPDATE_HANDED_OFF, "target": "v1.2.3",
         "error": None, "at": "2026-09-21T10:00:00Z"}))

    record = sup._reconcile_self_update(sup._read_self_update())
    assert record["state"] == SELF_UPDATE_DONE


@pytest.mark.parametrize("contents", ["", "not json", "[]", "null", '{"state": null}'])
def test_startup_survives_a_malformed_record(sup, monkeypatch, contents):
    monkeypatch.setattr(sup, "_docker", FakeDocker({}))
    (sup.CONTROL / "selfupdate.json").write_text(contents)
    assert sup._read_self_update() is None
    assert sup._reconcile_self_update(sup._read_self_update()) is None


def test_startup_with_no_record_at_all_does_nothing(sup, monkeypatch):
    docker = FakeDocker({})
    monkeypatch.setattr(sup, "_docker", docker)
    assert sup._reconcile_self_update(sup._read_self_update()) is None
    assert docker.calls == []


def test_a_terminal_record_is_left_alone_on_the_next_restart(sup, monkeypatch):
    """Reconciliation must be idempotent: the record survives every restart, so
    a rule that rewrote it each time would republish "done" forever."""
    monkeypatch.setenv("VIGILANT_UPDATER_VERSION", "v1.2.3")
    docker = FakeDocker({})
    monkeypatch.setattr(sup, "_docker", docker)
    done = {"state": SELF_UPDATE_DONE, "target": "v1.2.3",
            "error": None, "at": "2026-09-21T10:00:00Z"}
    (sup.CONTROL / "selfupdate.json").write_text(json.dumps(done))
    assert sup._reconcile_self_update(sup._read_self_update()) == done
    assert docker.calls == []


# ── The heartbeat carries the record ─────────────────────────────────────────

def test_the_heartbeat_always_has_a_self_update_key(sup):
    sup.write_heartbeat({"socket": "ok"})
    beat = json.loads((sup.CONTROL / "updater.json").read_text())
    assert "self_update" in beat
    assert beat["self_update"] is None


def test_the_heartbeat_publishes_the_record(sup):
    record = {"state": SELF_UPDATE_PULLING, "target": "v1.2.3",
              "error": None, "at": "2026-09-21T10:00:00Z"}
    sup.write_heartbeat({"socket": "ok"}, record)
    beat = json.loads((sup.CONTROL / "updater.json").read_text())
    assert beat["self_update"] == record


# ── The restart gap ──────────────────────────────────────────────────────────

def test_self_checks_beat_after_every_check(sup, monkeypatch):
    """_self_checks' own timeouts sum to 15 + 15 + 20 + 30 = 80 seconds, which
    is beyond the app's 60s staleness window on its own. Beating after each
    check bounds the gap to the single longest timeout instead of their sum."""
    monkeypatch.setattr(sup.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
    seen = []
    checks = sup._self_checks(seen.append)
    assert len(seen) == len(checks) == 5
    # Each call sees strictly more than the last, and the last sees them all.
    assert [len(s) for s in seen] == [1, 2, 3, 4, 5]
    assert seen[-1] == checks
    # A copy, not the live dict — the caller publishes what it was handed.
    assert seen[0] is not checks


def test_the_startup_placeholder_checks_do_not_read_as_passing():
    """The panel decides "can the updater run" by rejecting non-"ok" values, so
    an empty checks dict renders as "everything passed" and ENABLES the Update
    and Rollback buttons before a single check has run."""
    import updater.supervisor as module
    assert module._STARTUP_CHECKS
    assert all(v != "ok" for v in module._STARTUP_CHECKS.values())
    assert all(v.startswith("FAIL: ") for v in module._STARTUP_CHECKS.values())


# ── Requests that arrive during the handoff ──────────────────────────────────

def test_a_request_written_during_the_handoff_is_claimed_by_the_next_process(sup):
    """Nothing is lost while the sidecar is being replaced. The app writes
    /control/request.json and claim_request() takes it with an os.replace()
    rename, so a request written mid-handoff simply sits there until the NEW
    supervisor's first _tick() renames it. The rename is also what makes
    double-running impossible — only one rename can ever succeed."""
    (sup.CONTROL / "request.json").write_text(json.dumps(
        {"id": "queued-mid-handoff", "action": "update", "tag": "v1.2.3"}))

    # The old process is gone without having looked at it.
    claimed, request = sup.claim_request(sup.CONTROL)
    assert claimed is True
    assert request["id"] == "queued-mid-handoff"

    # A second claimant — the losing half of any double-run — gets nothing.
    assert sup.claim_request(sup.CONTROL) == (False, None)


def test_log_tails_published_by_the_self_update_are_redacted_and_bounded(sup):
    """Everything here lands in /control/selfupdate.json, is rendered into an
    admin page, and gets pasted into bug reports — the same exposure the
    `remote` check's reason has, so it goes through the same redaction."""
    assert "s3cret" not in sup._short("https://someone:s3cret@example.org/o/r.git")
    assert "***@example.org" in sup._short("https://someone:s3cret@example.org/o/r.git")
    assert len(sup._short("x" * 50_000).encode()) <= sup._HELPER_LOG_MAX_BYTES
    # A cut must never land mid-codepoint and produce JSON the app refuses.
    assert sup._short("→" * 50_000).encode().decode() is not None
