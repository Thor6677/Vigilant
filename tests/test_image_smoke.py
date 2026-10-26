"""scripts/image_smoke.py and its place in the release workflow (ISS-140).

The script runs inside the built image at release time. Running the same file
against the checkout on every PR means only image-specific failures can reach
a tag: a module that cannot import on its own, warns at import, or reaches the
network is caught here first.

The workflow tests pin the ordering that makes the check worth having: both
images are built and smoke-tested before the registry login, before any push,
and before the Release exists.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "image_smoke.py"


def _run(cwd: Path, tmp_path: Path):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "EVE_CLIENT_ID": "smoke", "EVE_CLIENT_SECRET": "smoke", "SECRET_KEY": "smoke",
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path}/never-opened.db",
        "BACKGROUND_JOBS_ENABLED": "false", "SDE_AUTOLOAD_ENABLED": "false",
    }
    with SCRIPT.open() as fh:
        return subprocess.run([sys.executable, "-"], stdin=fh, cwd=cwd, env=env,
                              capture_output=True, text=True, timeout=120)


def _tree(tmp_path: Path, **modules: str) -> Path:
    app = tmp_path / "tree" / "app"
    app.mkdir(parents=True)
    (app / "__init__.py").write_text("")
    (app / "main.py").write_text("")
    for name, body in modules.items():
        (app / f"{name}.py").write_text(body)
    return tmp_path / "tree"


def test_smoke_script_passes_against_the_checkout(tmp_path):
    result = _run(ROOT, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "pass 2: imported" in result.stdout
    assert re.search(r"^image-smoke: OK \d+ modules$", result.stdout, re.M)


def test_failures_and_empty_input_never_print_the_sentinel(tmp_path):
    failed = _run(_tree(tmp_path, eager="import scripts_only_mod\n"), tmp_path)
    assert failed.returncode == 1 and "image-smoke: OK" not in failed.stdout
    # An empty stdin makes `python -` exit 0 having checked nothing; the
    # workflow's grep for the sentinel is what turns that into a failure.
    empty = subprocess.run([sys.executable, "-"], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=60)
    assert empty.returncode == 0 and "image-smoke: OK" not in empty.stdout


def test_clean_tree_passes(tmp_path):
    assert _run(_tree(tmp_path, ok="import json\n"), tmp_path).returncode == 0


def test_function_level_import_of_a_missing_module_fails(tmp_path):
    tree = _tree(tmp_path, lazy="def f():\n    from build_map_data import x\n    return x\n")
    result = _run(tree, tmp_path)
    assert result.returncode == 1
    assert "build_map_data" in result.stderr and "lazy.py:2" in result.stderr


def test_module_level_import_of_a_missing_module_fails(tmp_path):
    result = _run(_tree(tmp_path, eager="import scripts_only_mod\n"), tmp_path)
    assert result.returncode == 1
    assert "scripts_only_mod" in result.stderr


def test_warning_logged_by_app_code_at_import_fails(tmp_path):
    body = "import logging\nlogging.getLogger(__name__).warning('data file missing')\n"
    result = _run(_tree(tmp_path, noisy=body), tmp_path)
    assert result.returncode == 1
    assert "data file missing" in result.stderr


def test_network_attempt_at_import_fails_even_when_swallowed(tmp_path):
    body = ("import socket\n"
            "try:\n"
            "    socket.create_connection(('example.invalid', 80))\n"
            "except OSError:\n"
            "    pass\n")
    result = _run(_tree(tmp_path, net=body), tmp_path)
    assert result.returncode == 1
    assert "network attempt" in result.stderr


# ---- release.yml -------------------------------------------------------------

def _build_steps():
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text())
    return wf["jobs"]["build"], wf["jobs"]["build"]["steps"]


def _is(step, action):
    return step.get("uses", "").startswith(action)


def _index(steps, pred):
    return [i for i, s in enumerate(steps) if pred(s)]


def _truthy(value):
    return str(value).lower() == "true"


def test_smoke_steps_run_before_login_push_and_release():
    _, steps = _build_steps()
    smoke = _index(steps, lambda s: "image_smoke.py" in s.get("run", "")
                   or "import updater.supervisor" in s.get("run", ""))
    assert len(smoke) == 2, "expected one smoke step per image"
    gated = _index(steps, lambda s: _is(s, "docker/login-action")
                   or _truthy(s.get("with", {}).get("push"))
                   or _is(s, "softprops/action-gh-release"))
    assert gated, "login, push and release steps went missing"
    assert max(smoke) < min(gated)


# Keys that are meant to differ between the tested build and the pushed one.
BUILD_DIFFERS = {"load", "push", "tags", "labels"}


def test_local_builds_load_and_never_push_and_match_their_push_step():
    _, steps = _build_steps()
    builds = [s for s in steps if _is(s, "docker/build-push-action")]
    local = [s for s in builds if _truthy(s["with"].get("load"))]
    pushed = [s for s in builds if _truthy(s["with"].get("push"))]
    assert len(local) == 2 and len(pushed) == 2
    assert not [s for s in local if _truthy(s["with"].get("push"))]
    # The tested build must be the published build: every input matches except
    # the allow-listed keys, so no-cache, pull, target or build-contexts added
    # to one side only fails here.
    for loc in local:
        twin = [p for p in pushed if p["with"].get("file") == loc["with"].get("file")]
        assert len(twin) == 1, loc["name"]
        strip = lambda w: {k: v for k, v in w.items() if k not in BUILD_DIFFERS}
        assert strip(loc["with"]) == strip(twin[0]["with"]), loc["name"]


def _smoke_run():
    _, steps = _build_steps()
    return next(s["run"] for s in steps if "image_smoke.py" in s.get("run", ""))


def test_app_smoke_run_matches_production_hardening_and_takes_no_secrets():
    run = _smoke_run()
    assert re.search(r"docker run\b[^\n]*\s(-i|--interactive)\s", run), \
        "stdin must reach the container or the script never runs"
    for flag in ("--network none", "--read-only", "--cap-drop ALL",
                 "no-new-privileges", "python3 - < scripts/image_smoke.py"):
        assert flag in run, flag
    for banned in (" -u ", "--user", "--entrypoint", " -v ", "--mount",
                   "secrets.", "GITHUB_TOKEN"):
        assert banned not in run, banned
    assert not re.search(r"(^|\s)pytest\b", run), \
        "test_workflow_timeouts treats any pytest step as a test job"


def test_app_smoke_fails_closed_on_the_sentinel():
    run = _smoke_run()
    assert "set -o pipefail" in run, "the default shell has no pipefail"
    assert re.search(r"\|\s*grep -q '\^image-smoke: OK'\s*$", run.strip()), \
        "the step must require the script's final sentinel line"


def test_nothing_between_the_local_build_and_the_release_can_be_skipped_or_ignored():
    job, steps = _build_steps()
    assert "if" not in job and "continue-on-error" not in job
    first = _index(steps, lambda s: _is(s, "docker/build-push-action"))[0]
    last = _index(steps, lambda s: _is(s, "softprops/action-gh-release"))[0]
    assert first < last
    # Pinned to what exists today: none of these steps carries either key.
    loose = [s.get("name") or s.get("uses") for s in steps[first:last + 1]
             if "if" in s or "continue-on-error" in s]
    assert not loose, loose


def test_updater_smoke_has_no_network_and_no_mounts():
    _, steps = _build_steps()
    run = next(s["run"] for s in steps if "import updater.supervisor" in s.get("run", ""))
    assert "--network none" in run and "--read-only" in run
    assert " -v " not in run and "--mount" not in run and "docker.sock" not in run


def test_build_job_has_a_timeout():
    job, _ = _build_steps()
    minutes = job.get("timeout-minutes")
    assert isinstance(minutes, int) and 0 < minutes <= 60


def test_smoke_script_stays_out_of_the_image():
    ignored = {ln.strip() for ln in (ROOT / ".dockerignore").read_text().splitlines()}
    assert SCRIPT.is_file() and "scripts/" in ignored
