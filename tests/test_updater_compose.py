"""Compose wiring for the updater profile.

These assertions are all about properties that are invisible until the worst
possible moment: a second app container fighting the first over one database, an
app that quietly gained the Docker socket, a volume the app cannot write to.
"""
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

COMPOSE = Path("docker-compose.yml")
ENTRYPOINT = Path("docker-entrypoint.sh")
ENV_EXAMPLE = Path(".env.example")


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(COMPOSE.read_text())


@pytest.fixture(scope="module")
def app_svc(compose):
    return compose["services"]["app"]


@pytest.fixture(scope="module")
def updater_svc(compose):
    return compose["services"]["updater"]


# ── Off by default ───────────────────────────────────────────────────────────

def test_updater_is_profile_gated(updater_svc):
    """No profile means no container, no heartbeat, no button. A default
    install must be byte-identical to one without this service."""
    assert updater_svc.get("profiles") == ["updater"]


def test_app_is_not_profile_gated(app_svc):
    assert "profiles" not in app_svc


# ── The app stays unprivileged ───────────────────────────────────────────────

def test_app_never_sees_the_docker_socket(app_svc):
    """The central claim of the design: an RCE in Vigilant does not hand over
    the daemon."""
    assert not any("docker.sock" in str(v) for v in app_svc["volumes"])


def test_app_keeps_its_hardening(app_svc):
    """The /control mount must not have been bought by relaxing anything."""
    assert app_svc["read_only"] is True
    assert app_svc["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in app_svc["security_opt"]


def test_app_gets_the_control_mount(app_svc):
    assert any(str(v).endswith(":/control") for v in app_svc["volumes"])


# ── The updater's identity ───────────────────────────────────────────────────

def test_updater_does_not_run_as_root(updater_svc):
    """A root updater leaves .env and .git objects root-owned, bricking the SSH
    path this feature keeps as its fallback."""
    user = updater_svc["user"]
    assert "VIGILANT_UID" in user
    assert not user.startswith("0:")


def test_updater_gets_socket_access_by_group_not_uid(updater_svc):
    groups = [str(g) for g in updater_svc["group_add"]]
    assert any("DOCKER_GID" in g for g in groups)


def test_updater_joins_the_apps_group_for_the_shared_volume(updater_svc):
    """Both sides need group write on /control, which the entrypoint makes
    0770 vigilant:vigilant."""
    assert "10001" in [str(g) for g in updater_svc["group_add"]]


# ── The path that must not move ──────────────────────────────────────────────

def test_repo_is_mounted_at_exactly_opt_vigilant(updater_svc):
    """compose derives its project identity from the working directory and
    deploy.sh hardcodes this path. Mounted anywhere else, compose treats it as a
    new project and CREATES A SECOND app container instead of recreating the
    running one — with both bound to the same data volume.
    """
    mounts = [str(v) for v in updater_svc["volumes"]]
    assert "/opt/vigilant:/opt/vigilant" in mounts


def test_updater_root_env_matches_the_mount(updater_svc):
    env = updater_svc["environment"]
    joined = " ".join(env) if isinstance(env, list) else str(env)
    assert "VIGILANT_ROOT=/opt/vigilant" in joined


# ── No network surface ───────────────────────────────────────────────────────

def test_updater_is_not_on_the_shared_edge_bridge(updater_svc):
    """It has no listener and publishes no ports; nothing on the web bridge
    should be able to reach it. Outbound for `git fetch` comes from the
    project's default network."""
    assert "web" not in (updater_svc.get("networks") or [])


def test_updater_publishes_no_ports(updater_svc):
    assert "ports" not in updater_svc
    assert "expose" not in updater_svc


# ── Volume bootstrap ─────────────────────────────────────────────────────────

def test_control_volume_is_declared(compose):
    assert "control" in compose["volumes"]


def test_entrypoint_prepares_the_control_volume():
    """A fresh named volume is root-owned 0755 — the app could read but not
    write, and the first click would fail on permissions with nothing in the
    logs to explain it."""
    src = ENTRYPOINT.read_text()
    assert "/control" in src
    assert "chown vigilant:vigilant /control" in src
    assert "0770" in src


def test_entrypoint_tolerates_a_missing_control_dir():
    """The default install mounts no /control at all and must still boot."""
    src = ENTRYPOINT.read_text()
    assert "if [ -d /control ]" in src


# ── Documented, because DOCKER_GID is host-specific ──────────────────────────

def test_env_example_documents_the_gids():
    src = ENV_EXAMPLE.read_text()
    for key in ("VIGILANT_UID", "VIGILANT_GID", "DOCKER_GID"):
        assert key in src
    assert "getent group docker" in src
