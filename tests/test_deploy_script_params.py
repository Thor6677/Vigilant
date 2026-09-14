"""deploy.sh / rollback.sh must be targetable at a non-production stack.

Both scripts drove exactly one stack until the in-app updater needed a way to
be exercised somewhere other than production. The parameterization is only
useful if the DEFAULTS are byte-identical to the hardcoded values they replace
— a deploy that silently retargets is far worse than one that refuses.

Parsed as text rather than executed: running them requires Docker, a git
checkout and a live host, none of which belong in the unit suite.
"""
import re
import subprocess

DEPLOY = "scripts/deploy.sh"
ROLLBACK = "scripts/rollback.sh"


def _src(path):
    with open(path) as fh:
        return fh.read()


def _default_for(src, var):
    """Extract `X` from a `[readonly ]VAR="${VAR:-X}"` assignment."""
    m = re.search(rf'^(?:readonly )?{var}="\$\{{{var}:-([^}}]*)\}}"', src, re.MULTILINE)
    assert m, f"{var} must be assigned with a :- default"
    return m.group(1)


def test_deploy_defaults_match_the_old_hardcoded_values():
    src = _src(DEPLOY)
    assert _default_for(src, "VIGILANT_ROOT") == "/opt/vigilant"
    assert _default_for(src, "VIGILANT_IMAGE") == "ghcr.io/thor6677/vigilant"
    assert _default_for(src, "VIGILANT_COMPOSE_FILE") == "docker-compose.yml"


def test_rollback_defaults_match_the_old_hardcoded_values():
    src = _src(ROLLBACK)
    assert _default_for(src, "VIGILANT_ROOT") == "/opt/vigilant"
    assert _default_for(src, "VIGILANT_IMAGE") == "ghcr.io/thor6677/vigilant"
    assert _default_for(src, "VIGILANT_COMPOSE_FILE") == "docker-compose.yml"


_ROOT_DEFAULT_LINE = 'VIGILANT_ROOT="${VIGILANT_ROOT:-/opt/vigilant}"'
_ALLOWED_ROOT_DEFAULT_LINES = {_ROOT_DEFAULT_LINE, f"readonly {_ROOT_DEFAULT_LINE}"}


def test_no_literal_opt_vigilant_paths_remain_in_executable_lines():
    """A stray literal would silently retarget one step back at production."""
    for path in (DEPLOY, ROLLBACK):
        for i, line in enumerate(_src(path).splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            if stripped in _ALLOWED_ROOT_DEFAULT_LINES:
                continue
            assert "/opt/vigilant" not in stripped, \
                f"{path}:{i} still hardcodes /opt/vigilant: {stripped}"


def _first_executable_offset(src, needle):
    """Byte offset of the first NON-COMMENT line containing `needle`.

    Comments matter here: both scripts open with a long comment explaining the
    `git checkout` hazard, so a naive src.index("git ") lands in the prose above
    the guard and the assertion inverts.
    """
    offset = 0
    for line in src.splitlines(keepends=True):
        if not line.strip().startswith("#") and needle in line:
            return offset
        offset += len(line)
    return None


def test_reexec_guard_still_precedes_any_git_use():
    """The guard must stay the first thing that runs: these scripts rewrite
    themselves via `git checkout`, and an unguarded script whose file shrinks
    mid-run silently stops early AND exits 0."""
    for path, marker in ((DEPLOY, "VIGILANT_DEPLOY_REEXEC"), (ROLLBACK, "VIGILANT_ROLLBACK_REEXEC")):
        src = _src(path)
        first_git = _first_executable_offset(src, "git ")
        assert first_git is not None, f"{path}: expected a git invocation"
        guard_at = _first_executable_offset(src, marker)
        assert guard_at is not None, f"{path}: no executable {marker} check — the re-exec guard is gone"
        assert guard_at < first_git, f"{path}: re-exec guard must appear before any git invocation"


def test_both_scripts_are_syntactically_valid():
    for path in (DEPLOY, ROLLBACK):
        subprocess.run(["bash", "-n", path], check=True)
