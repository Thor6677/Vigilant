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

def _repo_mount(updater_svc):
    return [str(v) for v in updater_svc["volumes"] if "docker.sock" not in str(v)
            and not str(v).startswith("control:")][0]


def _split_mount(spec: str) -> tuple[str, str]:
    """Split host:container on the separator colon only.

    A naive partition(":") splits inside ${VIGILANT_ROOT:-/opt/vigilant} — the
    default-value syntax contains its own colon — and would compare two halves
    of the same variable reference. Track brace depth and cut at the first colon
    outside one.
    """
    depth = 0
    for i, ch in enumerate(spec):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == ":" and depth == 0:
            return spec[:i], spec[i + 1:]
    raise AssertionError(f"not a bind mount: {spec}")


def test_repo_mount_is_identical_on_both_sides(updater_svc):
    """The path INSIDE the container decides which stack gets recreated, because
    compose derives its project identity from the working directory. If the two
    sides ever diverge, compose treats it as a new project and creates a SECOND
    app container bound to the same data volume instead of recreating the
    running one.
    """
    host, container = _split_mount(_repo_mount(updater_svc))
    assert host == container, f"repo mount sides differ: {host} != {container}"


def test_updater_root_env_matches_the_mount(updater_svc):
    """The sidecar's idea of its root and the path it is mounted at must be the
    same string, or deploy.sh cd's somewhere the bind mount does not cover."""
    env = updater_svc["environment"]
    joined = " ".join(env) if isinstance(env, list) else str(env)
    host = _split_mount(_repo_mount(updater_svc))[0]
    assert f"VIGILANT_ROOT={host}" in joined


def test_repo_path_defaults_to_opt_vigilant(updater_svc):
    """Parameterised, but a stock install must be unchanged."""
    assert "${VIGILANT_ROOT:-/opt/vigilant}" in _repo_mount(updater_svc)


# ── Which images the file resolves to ────────────────────────────────────────
#
# Parameterised so a fork or a mirrored registry can be named — release.yml
# publishes a fork's images into the fork's own namespace, which a hardcoded
# line could never reach — and because the sidecar's self-update reads
# services.updater.image straight out of this file. The helper it launches runs
# compose with an explicit `-f`, which suppresses docker-compose.override.yml,
# so interpolation here is the ONLY route a non-default registry has into the
# sidecar recreate.
#
# The defaults must resolve byte-identically to the strings that were hardcoded
# before, or an existing install's compose config hash changes and the next
# `up -d` recreates containers for no reason.

_INTERPOLATE = __import__("re").compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _resolve(spec: str, env: dict) -> str:
    """Apply compose's `${VAR:-default}` interpolation to one value.

    Hand-rolled rather than shelled out to `docker compose config`: there is no
    Docker daemon in this suite, and the two forms used in this file are the
    whole of the syntax that needs covering.
    """
    return _INTERPOLATE.sub(
        lambda m: env.get(m.group(1)) or (m.group(2) or ""), spec)


def test_default_images_are_unchanged_from_the_hardcoded_ones(app_svc, updater_svc):
    """Byte-identical, not merely equivalent. A different string is a different
    compose config, and the release that introduces the variables would recreate
    every stock install's containers to run exactly the same image."""
    assert _resolve(app_svc["image"], {}) == "ghcr.io/thor6677/vigilant:latest"
    assert _resolve(updater_svc["image"], {}) == \
        "ghcr.io/thor6677/vigilant-updater:latest"


def test_the_pinned_tag_still_reaches_both_images(app_svc, updater_svc):
    env = {"VIGILANT_TAG": "v1.2.3"}
    assert _resolve(app_svc["image"], env) == "ghcr.io/thor6677/vigilant:v1.2.3"
    assert _resolve(updater_svc["image"], env) == \
        "ghcr.io/thor6677/vigilant-updater:v1.2.3"


def test_a_fork_can_point_both_services_at_its_own_registry(app_svc, updater_svc):
    env = {"VIGILANT_IMAGE": "ghcr.io/someone/vigilant",
           "VIGILANT_UPDATER_IMAGE": "ghcr.io/someone/vigilant-updater",
           "VIGILANT_TAG": "v1.2.3"}
    assert _resolve(app_svc["image"], env) == "ghcr.io/someone/vigilant:v1.2.3"
    assert _resolve(updater_svc["image"], env) == \
        "ghcr.io/someone/vigilant-updater:v1.2.3"


def test_the_two_images_have_separate_variables(app_svc, updater_svc):
    """They are built from separate Dockerfiles and a mirror may well hold them
    in different places, so one variable for both would be wrong."""
    assert "${VIGILANT_IMAGE:-" in app_svc["image"]
    assert "${VIGILANT_UPDATER_IMAGE:-" in updater_svc["image"]
    assert "VIGILANT_UPDATER_IMAGE" not in app_svc["image"]


def test_env_example_documents_the_registry_overrides():
    src = ENV_EXAMPLE.read_text()
    assert "VIGILANT_IMAGE" in src
    assert "VIGILANT_UPDATER_IMAGE" in src


def test_the_supervisor_never_string_builds_an_image_name():
    """It asks `docker compose config` for services.updater.image instead, which
    is how the overrides above reach the self-update at all. A reconstructed
    name would hardcode the default registry and quietly ignore them."""
    src = Path("updater/supervisor.py").read_text()
    # No registry host, and no `name:tag` assembly. HELPER_CONTAINER_NAME is a
    # CONTAINER name and is allowed to be a constant — it names nothing that
    # gets pulled.
    assert "ghcr.io" not in src
    assert '["services"]["updater"]["image"]' in src


def test_updater_image_registry_is_passed_through(updater_svc):
    """deploy.sh resolves its own VIGILANT_IMAGE default independently of
    compose. If it is not passed in, the sidecar pulls from ghcr.io while
    compose runs whatever the file says — the two disagree about what a tag
    means, and it surfaces as a "not found" at pull time. Caught on the
    throwaway stack, 2026-09-14."""
    env = updater_svc["environment"]
    joined = " ".join(env) if isinstance(env, list) else str(env)
    assert "VIGILANT_IMAGE=" in joined


def test_repo_path_is_not_hardcoded(updater_svc):
    """Hardcoding it is a foot-gun, not a safety measure: a second stack
    enabling this profile would silently bind PRODUCTION's repo and deploy the
    wrong site. Observed on the throwaway stack, 2026-09-14."""
    assert _repo_mount(updater_svc) != "/opt/vigilant:/opt/vigilant"


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


def test_entrypoint_chmods_before_it_chowns():
    """Order is load-bearing and is not the obvious one.

    chmod on a file you do not own needs CAP_FOWNER, which this container
    deliberately drops. A fresh named volume is root-owned, so root may chmod it
    BEFORE the chown and may not after. Written the natural way round the
    entrypoint fails with "Operation not permitted" and set -e turns that into
    an app that crash-loops the moment the updater profile is first enabled —
    observed on the throwaway stack before this ordering was adopted.
    """
    src = ENTRYPOINT.read_text()
    block = src[src.index("if [ -d /control ]"):]
    assert block.index("chmod 0770 /control") < block.index("chown vigilant:vigilant /control")


def test_entrypoint_does_not_require_cap_fowner(compose):
    """If FOWNER is ever added to buy the naive ordering, this fails — the
    ordering above is the cheaper fix and keeps the capability set minimal."""
    assert "FOWNER" not in [c.upper() for c in compose["services"]["app"]["cap_add"]]


def test_control_chmod_failure_is_not_fatal():
    """Degrade visibly — a hidden panel and a logged warning — rather than
    taking the whole app down over a directory mode."""
    src = ENTRYPOINT.read_text()
    block = src[src.index("if [ -d /control ]"):]
    line = [l for l in block.splitlines() if "chmod 0770 /control" in l][0]
    assert "||" in line


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
