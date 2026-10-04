"""Every CI job that runs pytest has a job timeout and a per-test timeout.

The v1.8.0 release run hung inside pytest for five hours, printing nothing, until
it was cancelled by hand (ISS-112). `timeout-minutes` caps the job, so a hang
fails in minutes instead of blocking a release for GitHub's six-hour default.
`--timeout` (pytest-timeout) fails the stuck test itself and, with the thread
method, dumps every thread's stack, which names the hanging test.

The per-test timeout lives on the CI command line, not in pytest config, so a
local run doesn't need the plugin installed.
"""
import glob
import os
import re

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pytest_jobs():
    for path in sorted(glob.glob(os.path.join(REPO, ".github", "workflows", "*.yml"))):
        with open(path) as fh:
            wf = yaml.safe_load(fh)
        for name, job in (wf.get("jobs") or {}).items():
            runs = [s.get("run", "") for s in job.get("steps", [])]
            pytest_runs = [r for r in runs if re.search(r"(^|\s)(python -m )?pytest\b", r)]
            if pytest_runs:
                yield os.path.basename(path), name, job, pytest_runs


def test_pytest_jobs_are_found_in_both_workflows():
    files = {wf for wf, *_ in _pytest_jobs()}
    assert files == {"tests.yml", "release.yml"}


def test_every_pytest_job_has_a_job_timeout():
    missing = []
    for wf, name, job, _ in _pytest_jobs():
        minutes = job.get("timeout-minutes")
        if not isinstance(minutes, int) or not 0 < minutes <= 30:
            missing.append(f"{wf}:{name} timeout-minutes={minutes!r}")
    assert not missing, "set timeout-minutes (1-30) on:\n  " + "\n  ".join(missing)


def test_every_pytest_command_has_a_per_test_timeout_that_dumps_threads():
    loose = []
    for wf, name, _, runs in _pytest_jobs():
        for run in runs:
            if not re.search(r"--timeout=\d+\b", run) or "--timeout-method=thread" not in run:
                loose.append(f"{wf}:{name}: {run.strip()}")
    assert not loose, "add --timeout=N --timeout-method=thread to:\n  " + "\n  ".join(loose)


def test_pytest_timeout_is_a_dev_requirement():
    with open(os.path.join(REPO, "requirements-dev.txt")) as fh:
        names = {re.split(r"[<>=!~\s\[]", line.strip(), maxsplit=1)[0].lower()
                 for line in fh if line.strip() and not line.startswith("#")}
    assert "pytest-timeout" in names
