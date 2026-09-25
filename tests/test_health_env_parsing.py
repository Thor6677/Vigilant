""".health-env is read as KEY=value data by deploy.sh, rollback.sh and
health-check.sh, never executed.

It is configuration, so it is parsed as data and nothing in it may run a
command: a line the parser does not recognise refuses the whole file. The function is copied into each script (each runs
alone or re-execs from a temp copy, so none can source a shared file), and the
first test keeps the three copies identical.
"""
import os
import re
import shutil
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = ("deploy.sh", "rollback.sh", "health-check.sh")


def _function(script: str) -> str:
    src = open(os.path.join(REPO, "scripts", script)).read()
    m = re.search(r"^load_health_env\(\) \{\n.*?^\}\n", src, re.MULTILINE | re.DOTALL)
    assert m, f"load_health_env not found in {script}"
    return m.group(0)


def test_every_script_carries_the_same_parser():
    copies = {s: _function(s) for s in SCRIPTS}
    assert len(set(copies.values())) == 1, "load_health_env copies have drifted apart"


def test_no_script_sources_the_file():
    for s in SCRIPTS:
        src = open(os.path.join(REPO, "scripts", s)).read()
        assert not re.search(r"^\s*(source|\.)\s+\S*health-env", src, re.MULTILINE), s


def _load(tmp_path, content: str, *names: str, prelude: str = ""):
    env_file = tmp_path / ".health-env"
    env_file.write_text(content)
    show = "; ".join(f'echo "{n}=${{{n}-<unset>}}"' for n in names)
    script = (
        "set -euo pipefail\n"
        f"{prelude}\n"
        f"{_function('deploy.sh')}\n"
        f'load_health_env "{env_file}" 7\n'
        f"{show}\n"
        'env | grep "^EXPORTED_" || true\n'
    )
    return subprocess.run(["bash", "-c", script], cwd=tmp_path,
                          capture_output=True, text=True, timeout=30)


def test_plain_quoted_and_exported_values_are_set(tmp_path):
    r = _load(tmp_path, (
        "# comment\n"
        "\n"
        "PROBES=/opt/ops-toolkit/scripts/probes.sh\n"
        'HEALTHZ_URL="https://app.example.org/healthz"\n'
        "APP_CONTAINER='vigilant-app-1'\n"
        "  export EXPORTED_NAME=edge-nginx-1\n"
        "EMPTY=\n"
        "LAST=no-trailing-newline"
    ), "PROBES", "HEALTHZ_URL", "APP_CONTAINER", "EXPORTED_NAME", "EMPTY", "LAST")
    assert r.returncode == 0, r.stderr
    out = r.stdout.splitlines()
    assert "PROBES=/opt/ops-toolkit/scripts/probes.sh" in out
    assert "HEALTHZ_URL=https://app.example.org/healthz" in out
    assert "APP_CONTAINER=vigilant-app-1" in out
    assert "EXPORTED_NAME=edge-nginx-1" in out
    assert "EMPTY=" in out
    assert "LAST=no-trailing-newline" in out
    # `export` reaches child processes, same as when the file was sourced.
    assert out.count("EXPORTED_NAME=edge-nginx-1") == 2


def test_a_missing_file_is_fine(tmp_path):
    script = f"set -euo pipefail\n{_function('deploy.sh')}\nload_health_env {tmp_path}/nope 7\necho ok\n"
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr


@pytest.mark.parametrize("line", [
    "PROBES=$(touch MARKER)",
    'PROBES="$(touch MARKER)"',
    "PROBES=`touch MARKER`",
    'PROBES="`touch MARKER`"',
    "PROBES=/x; touch MARKER",
    "PROBES=/x && touch MARKER",
    "PROBES=/x touch MARKER",                        # would run `touch` with PROBES in its env
    "PROBES=/x|touch MARKER",
    "touch MARKER",
    "PROBES='/x'; touch MARKER",
    'PROBES="a\\"b"',
    "PROBES=~/x",
    "1BAD=/x",
])
def test_anything_but_plain_assignments_refuses_the_file(tmp_path, line):
    r = _load(tmp_path, f"GOOD=1\n{line}\n", "GOOD")
    assert r.returncode == 7, (r.stdout, r.stderr)
    assert "refusing to read the file" in r.stderr
    assert not (tmp_path / "MARKER").exists()


def test_a_readonly_variable_still_cannot_be_overridden(tmp_path):
    r = _load(tmp_path, "VIGILANT_ROOT=/elsewhere\n", "VIGILANT_ROOT",
              prelude='readonly VIGILANT_ROOT=/opt/stack')
    assert r.returncode != 0
    assert "readonly" in r.stderr.lower()
    assert "VIGILANT_ROOT=/elsewhere" not in r.stdout


# ── The real scripts ────────────────────────────────────────────────────────

NOT_DATA = 'PROBES="$(touch MARKER)"\n'


def _stub_bin(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name in ("git", "docker", "curl"):
        p = bin_ / name
        p.write_text("#!/usr/bin/env bash\nexit 0\n")
        p.chmod(0o755)
    return bin_


@pytest.mark.parametrize("script,flag", [("deploy.sh", "--tag"), ("rollback.sh", "--to")])
def test_deploy_and_rollback_refuse_a_file_that_is_not_data(tmp_path, script, flag):
    root = tmp_path / "stack"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(os.path.join(REPO, "scripts", script), root / "scripts" / script)
    (root / ".health-env").write_text(NOT_DATA)
    bin_ = _stub_bin(tmp_path)
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "VIGILANT_ROOT": str(root)}
    r = subprocess.run(["bash", str(root / "scripts" / script), flag, "v1.2.3"],
                       env=env, cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "refusing to read the file" in r.stderr
    assert not (tmp_path / "MARKER").exists()
    assert not (root / "MARKER").exists()


def test_health_check_reports_could_not_assess(tmp_path):
    root = tmp_path / "stack"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(os.path.join(REPO, "scripts", "health-check.sh"), root / "scripts" / "health-check.sh")
    (root / ".health-env").write_text(NOT_DATA)
    r = subprocess.run(["bash", str(root / "scripts" / "health-check.sh")],
                       cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert r.returncode == 78, (r.stdout, r.stderr)
    assert not (tmp_path / "MARKER").exists()
