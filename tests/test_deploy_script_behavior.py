"""deploy.sh / rollback.sh must ACTUALLY drive the stack named by the
environment, not merely mention the right variable names in their source.

Runs the real scripts with `git`/`docker`/`curl` replaced by stubs that record
their argv, so the assertions are about what the script WOULD do to Docker and
git rather than about which strings appear in its source. A source-text test
cannot tell `cd "$VIGILANT_ROOT"` from `cd /srv/vigilant`, cannot tell a real
`exec` re-exec from a no-op, and cannot prove a hostile `.health-env` fails to
retarget the run — a reviewer reproduced all three by sabotaging
tests/test_deploy_script_params.py's target file and watching every assertion
there keep passing. This file exercises behaviour instead.
"""
import os
import shutil
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STUB = """#!/usr/bin/env bash
echo "$(basename "$0") $*" >> "$STUB_LOG"
case "$(basename "$0")" in
  docker)
    # `docker exec <c> python3 -c <health probe>` -> pretend /healthz is green.
    [ "$1" = "exec" ] && [ "$4" = "-c" ] && echo 200
    [ "$1" = "exec" ] && [ "$3" = "printenv" ] && echo "stub-version"
    ;;
esac
exit 0
"""

COMPOSE = """services:
  app:
    image: x
networks:
  web:
    external: true
    name: web
"""


@pytest.fixture
def stack(tmp_path):
    root = tmp_path / "vigilant-elsewhere"
    (root / "scripts").mkdir(parents=True)
    for s in ("deploy.sh", "rollback.sh"):
        shutil.copy(os.path.join(REPO, "scripts", s), root / "scripts" / s)
    (root / "custom-compose.yml").write_text(COMPOSE)
    (root / ".deployed").write_text("2026-01-01T00:00:00Z v0.9.0\n")

    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name in ("git", "docker", "curl"):
        p = bin_ / name
        p.write_text(STUB)
        p.chmod(0o755)

    log = tmp_path / "calls.log"
    env = {
        **os.environ,
        "PATH": f"{bin_}:{os.environ['PATH']}",
        "STUB_LOG": str(log),
        "VIGILANT_ROOT": str(root),
        "VIGILANT_IMAGE": "127.0.0.1:5000/vigilant",
        "VIGILANT_COMPOSE_FILE": "custom-compose.yml",
        "APP_CONTAINER": "elsewhere-app-1",
    }
    return root, log, env


def _run(script, root, env, *args):
    r = subprocess.run(
        ["bash", str(root / "scripts" / script), *args],
        env=env, cwd="/", capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, f"{script} failed:\n{r.stdout}\n{r.stderr}"
    return r


def test_deploy_drives_the_stack_named_by_the_environment(stack):
    root, log, env = stack
    _run("deploy.sh", root, env, "--tag", "v1.2.3")
    calls = log.read_text()

    # The image pulled and the compose file used both come from the env.
    assert "docker pull 127.0.0.1:5000/vigilant:v1.2.3" in calls
    assert "docker compose -f custom-compose.yml up -d" in calls
    assert "ghcr.io/thor6677" not in calls        # never production's image
    assert "elsewhere-app-1" in calls             # never production's container

    # State was written under VIGILANT_ROOT, not /opt/vigilant.
    assert (root / ".deployed").read_text().rstrip().endswith("v1.2.3")
    assert "VIGILANT_TAG=v1.2.3" in (root / ".env").read_text()


def test_rollback_drives_the_stack_named_by_the_environment(stack):
    root, log, env = stack
    (root / ".deployed").write_text(
        "2026-01-01T00:00:00Z v0.9.0\n2026-01-02T00:00:00Z v1.0.0\n"
    )
    _run("rollback.sh", root, env)
    calls = log.read_text()
    assert "docker pull 127.0.0.1:5000/vigilant:v0.9.0" in calls
    assert "docker compose -f custom-compose.yml up -d" in calls
    assert "ghcr.io/thor6677" not in calls
    assert (root / ".deployed").read_text().rstrip().endswith("v0.9.0")


# ── Self-modification guard, exercised rather than merely positioned ────────

_STUB_DOCKER_HEALTHY = """#!/usr/bin/env bash
[ "$1" = "exec" ] && [ "$4" = "-c" ] && echo 200
exit 0
"""

# A checkout that makes the script SHORTER, which is the dangerous direction.
_STUB_GIT_TRUNCATE = """#!/usr/bin/env bash
if [ "$1" = "checkout" ]; then : > "$VIGILANT_ROOT/scripts/deploy.sh"; fi
exit 0
"""

_MINIMAL_COMPOSE = "networks:\n  web:\n    external: true\n    name: web\n"


def test_deploy_survives_its_own_file_being_rewritten_mid_run(tmp_path):
    """Bash reads a script incrementally by byte offset. deploy.sh checks out a
    git tag, which rewrites deploy.sh on disk while bash is still reading it; a
    script whose file shrinks mid-run silently stops early AND EXITS 0, so the
    truncation looks like a successful deploy.

    The stub `git checkout` here truncates the on-disk script, exactly as a real
    checkout of a shorter revision would. If the guard is intact the running
    copy lives in a tmpdir and the deploy still reaches its last line. Position
    tests cannot catch a guard whose `exec` was replaced by a no-op; this can.
    """
    root = tmp_path / "stack"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(os.path.join(REPO, "scripts", "deploy.sh"), root / "scripts" / "deploy.sh")
    (root / "docker-compose.yml").write_text(_MINIMAL_COMPOSE)

    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "docker").write_text(_STUB_DOCKER_HEALTHY)
    (bin_ / "docker").chmod(0o755)
    (bin_ / "git").write_text(_STUB_GIT_TRUNCATE)
    (bin_ / "git").chmod(0o755)
    (bin_ / "curl").write_text("#!/usr/bin/env bash\nexit 0\n")
    (bin_ / "curl").chmod(0o755)

    env = {
        **os.environ, "PATH": f"{bin_}:{os.environ['PATH']}",
        "VIGILANT_ROOT": str(root), "APP_CONTAINER": "stub-app-1",
    }
    r = subprocess.run(
        ["bash", str(root / "scripts" / "deploy.sh"), "--tag", "v1.2.3"],
        env=env, cwd="/", capture_output=True, text=True, timeout=120,
    )

    assert (root / "scripts" / "deploy.sh").read_text() == "", \
        "the stub checkout should have truncated the on-disk script"
    # Reaching the LAST line is the whole assertion: a truncated read exits 0
    # partway through, so returncode alone proves nothing.
    assert "[5/5] Done" in r.stdout, (
        "deploy.sh stopped early after its file was rewritten — the re-exec "
        f"guard is not doing its job.\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )


# ── .health-env must not be able to retarget the run ─────────────────────────

def test_health_env_cannot_retarget_the_stack(tmp_path):
    """.health-env lives INSIDE the root it describes, so a stale or copied one
    naming a different stack is exactly the scenario the readonly VIGILANT_ROOT
    (etc.) guards against. Reproduced 2026-07-26: without `readonly`, a
    .health-env setting VIGILANT_ROOT produced a split-brain deploy — the
    checkout and .env pin landed in the caller's root while .deployed was
    appended to the OTHER stack's ledger, which is exactly the file
    rollback.sh reads to choose a target.

    This test writes a hostile .health-env and asserts the script refuses
    outright (non-zero exit, "readonly" in stderr) rather than silently
    running against two different roots at once.
    """
    root = tmp_path / "caller-root"
    other = tmp_path / "other-stack"
    (root / "scripts").mkdir(parents=True)
    other.mkdir()
    shutil.copy(os.path.join(REPO, "scripts", "deploy.sh"), root / "scripts" / "deploy.sh")
    (root / "docker-compose.yml").write_text(_MINIMAL_COMPOSE)
    # Hostile: a .health-env that tries to repoint VIGILANT_ROOT elsewhere.
    (root / ".health-env").write_text(f'VIGILANT_ROOT="{other}"\n')

    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name in ("git", "docker", "curl"):
        p = bin_ / name
        p.write_text("#!/usr/bin/env bash\nexit 0\n")
        p.chmod(0o755)

    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "VIGILANT_ROOT": str(root)}
    r = subprocess.run(
        ["bash", str(root / "scripts" / "deploy.sh"), "--tag", "v1.2.3"],
        env=env, cwd="/", capture_output=True, text=True, timeout=120,
    )

    assert r.returncode != 0, (
        "deploy.sh should refuse when .health-env tries to reassign a readonly "
        f"VIGILANT_ROOT, not proceed with a split-brain root.\nSTDOUT:\n{r.stdout}"
    )
    assert "readonly" in r.stderr.lower(), (
        f"expected bash's readonly-variable error in stderr, got:\n{r.stderr}"
    )
