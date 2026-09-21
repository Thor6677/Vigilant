"""The sidecar image copies three files out of `app/`; keep them import-clean.

updater/Dockerfile copies app/__init__.py, app/ops/__init__.py and
app/ops/version.py and nothing else. Those two __init__ files are currently a
docstring and an empty file, so `from app.ops.version import parse_version`
resolves inside a container that has none of the app's dependencies installed.

Add one import to either — a config module, a model, anything — and the sidecar
raises ImportError at startup, IN PRODUCTION, after a release has shipped. The
Dockerfile cannot express "this file must stay trivial", so this test does.
"""
import ast
import os
import re
import shlex
import subprocess
import sys

import pytest

from updater.supervisor import REWRITTEN_SSH_HOST

# Files the sidecar image actually copies out of the app package.
COPIED = ["app/__init__.py", "app/ops/__init__.py", "app/ops/version.py"]

_STDLIB = set(sys.stdlib_module_names)

DOCKERFILE = "updater/Dockerfile"


def _imported_roots(path):
    with open(path) as fh:
        tree = ast.parse(fh.read(), filename=path)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                roots.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import — inside `app`, which the sidecar
            # only partially copies, so it is never safe here.
            if node.level:
                roots.add(f".{node.module or ''}")
            elif node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_files_copied_into_the_sidecar_import_only_stdlib():
    for path in COPIED:
        for root in _imported_roots(path):
            assert root in _STDLIB, (
                f"{path} imports {root!r}, which the sidecar image does not "
                f"install. updater/Dockerfile copies only {COPIED} out of app/, "
                f"so this becomes an ImportError at sidecar startup in production."
            )


def _copy_sources(line):
    """Source paths from one COPY line, skipping any --flags.

    `COPY --chown=1000:1000 app/foo.py /dst` would otherwise put the flag at
    index 1 and let app/foo.py slip past the guard below unnoticed.
    """
    parts = line.split()[1:]
    parts = [p for p in parts if not p.startswith("--")]
    return parts[:-1]          # everything but the destination


def test_dockerfile_copies_exactly_the_files_this_test_guards():
    """If someone adds a COPY of another app/ file, the guard above silently
    stops covering it."""
    with open("updater/Dockerfile") as fh:
        lines = [l.strip() for l in fh if l.strip().startswith("COPY ")]
    copied_app_files = sorted(
        src for line in lines for src in _copy_sources(line)
        if src.startswith("app/")
    )
    assert copied_app_files == sorted(COPIED), copied_app_files


def test_copy_source_parser_survives_a_chown_flag():
    """Pins the parser itself: the naive split()[1] version silently stopped
    guarding any file copied with a --chown or --from flag."""
    assert _copy_sources("COPY --chown=1000:1000 app/x.py /dst/x.py") == ["app/x.py"]
    assert _copy_sources("COPY app/x.py /dst/x.py") == ["app/x.py"]
    assert _copy_sources("COPY a.py b.py /dst/") == ["a.py", "b.py"]


def test_dockerfile_declares_no_user_directive():
    """The uid must match the host repo owner, which varies per install, so
    compose supplies it. A USER here would leave root-owned .env and .git
    objects and brick the scripts/deploy.sh fallback."""
    with open("updater/Dockerfile") as fh:
        for line in fh:
            assert not line.strip().upper().startswith("USER "), \
                "uid must come from compose, not the image"


def test_dockerfile_declares_no_entrypoint():
    """The self-update helper is this image run with a `docker compose …`
    command appended after the image reference. That overrides CMD; it does NOT
    override an ENTRYPOINT, which would instead receive the compose argv as
    arguments and try to run the supervisor with them.

    It matters across versions, which is the whole point of the helper: the
    image launched may be NEWER than the sidecar that launched it. An ENTRYPOINT
    added in some future release would break a self-update FROM every release
    before it, discovered only in production. Also note the helper needs nothing
    from this image but docker-cli-compose, which every updater image has.
    """
    with open(DOCKERFILE) as fh:
        for line in fh:
            assert not line.strip().upper().startswith("ENTRYPOINT"), \
                "the self-update helper overrides CMD; an ENTRYPOINT defeats it"


def test_dockerfile_sets_home_and_unbuffered_output():
    with open("updater/Dockerfile") as fh:
        src = fh.read()
    assert "HOME=" in src, "the container uid is not in /etc/passwd; git needs HOME"
    assert "PYTHONUNBUFFERED=1" in src, \
        "without this the deploy log appears only when the process exits"


def test_dockerfile_disables_bytecode_writes():
    """/opt/updater is root-owned (no USER directive), so a .pyc write there
    already fails silently under a writable rootfs — and now that the rootfs
    is genuinely read-only (compose's `updater:` service and the self-update
    helper's `docker run` both set it), that silent failure is the only thing
    standing in for an explicit one. Parsed via _dockerfile_env(), not a
    substring check, for the same reason this file's own docstring gives for
    parsing the GIT_CONFIG block: the interesting part is the VALUE, and a
    substring match would pass just as happily for a typo'd key."""
    assert _dockerfile_env().get("PYTHONDONTWRITEBYTECODE") == "1"


# ── The HTTPS rewrite that lets an SSH-cloned host repo fetch from here ──────
#
# The sidecar has no ssh client on purpose (it holds the Docker socket; giving
# it the host's GitHub key too would make one compromise yield both root on the
# box and push access to the source). The production clone's origin is an
# scp-style SSH URL, so without a rewrite `git fetch` inside deploy.sh dies with
# "cannot run ssh: No such file or directory" — which is exactly what the first
# real in-app update did.


def _dockerfile_env(path=DOCKERFILE):
    """Every ENV key/value the image declares, continuations joined.

    Parsed rather than substring-matched because the interesting part is the
    VALUES: an assertion that the string "insteadOf" appears somewhere would
    pass just as happily for a typo'd key or a rewrite pointing the wrong way.
    shlex.split does the unquoting, so `KEY="a b"` and `KEY=a` both land as the
    bare value git would actually see.
    """
    with open(path) as fh:
        src = fh.read()
    src = re.sub(r"\\\n\s*", " ", src)          # join line continuations
    env = {}
    for line in src.splitlines():
        line = line.strip()
        if not line.upper().startswith("ENV "):
            continue
        for token in shlex.split(line[4:]):
            if "=" in token:
                key, value = token.split("=", 1)
                env[key] = value
    return env


def _image_git_config_env():
    return {k: v for k, v in _dockerfile_env().items() if k.startswith("GIT_CONFIG")}


def _hermetic_git_env(home, extra=None):
    """A git environment that cannot see the developer's own config.

    Without GIT_CONFIG_GLOBAL/SYSTEM pointed at /dev/null, a contributor with a
    personal `url.*.insteadOf` rule (common on machines that push over SSH) or a
    credential helper like osxkeychain gets a different result here than CI
    does — the test passes locally and fails, or worse silently stops proving
    anything, in the pipeline. PATH is carried over because git needs to find
    its own helper binaries.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        # Belt and braces for a test that must never touch the network: no
        # terminal prompt, and an askpass that cannot block on a GUI.
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/echo",
    }
    env.update(extra or {})
    return env


def _repo_with_origin(tmp_path, url, env):
    repo = tmp_path / "clone"
    subprocess.run(["git", "init", "-q", str(repo)],
                   check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", url],
                   check=True, capture_output=True, env=env)
    return repo


def _rewrite_rules():
    """The image's insteadOf rules as (https_prefix, ssh_prefix) pairs.

    Everything below is driven off these rather than off copies of the URLs, so
    the tests prove the values the image ACTUALLY ships. A hardcoded expectation
    would keep passing after someone edited the Dockerfile — which is the one
    failure these tests exist to prevent.
    """
    env = _image_git_config_env()
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    rules = []
    for i in range(count):
        key = env.get(f"GIT_CONFIG_KEY_{i}", "")
        value = env.get(f"GIT_CONFIG_VALUE_{i}", "")
        prefix = re.fullmatch(r"url\.(.+)\.insteadOf", key)
        rules.append((prefix.group(1) if prefix else None, value))
    return rules


def test_dockerfile_declares_two_insteadof_rules_for_one_https_prefix():
    """The two spellings of an SSH remote share no common prefix, and
    `insteadOf` matches a literal prefix — so one entry cannot cover both.

    Also pins the rewrite target against updater/supervisor.py's
    REWRITTEN_SSH_HOST: the `remote` self-check uses that constant to decide
    whether a still-SSH origin means "your origin is unsupported" or "this
    image's environment is broken", and the two drifting apart would make it
    give the wrong advice.
    """
    rules = _rewrite_rules()
    assert len(rules) == 2, rules
    https_prefixes = {https for https, _ in rules}
    assert https_prefixes == {f"https://{REWRITTEN_SSH_HOST}/"}, https_prefixes
    ssh_prefixes = {ssh for _, ssh in rules}
    # One scp-style prefix and one ssh:// prefix, and they must differ.
    assert len(ssh_prefixes) == 2, ssh_prefixes
    assert any(s.startswith("ssh://") for s in ssh_prefixes), ssh_prefixes
    assert any(not s.startswith("ssh://") and s.endswith(":")
               for s in ssh_prefixes), ssh_prefixes
    for ssh in ssh_prefixes:
        assert REWRITTEN_SSH_HOST in ssh, ssh


@pytest.mark.parametrize("index", [0, 1])
def test_image_git_config_env_really_rewrites_ssh_origins(tmp_path, index):
    """Proves the mechanism against real git, using the values parsed out of the
    Dockerfile rather than a copy of them.

    `ls-remote --get-url` is the primitive on purpose: unlike `remote get-url`
    it reports the url AFTER insteadOf expansion, and unlike a real fetch it
    resolves the name locally and touches no network. If GIT_CONFIG_COUNT were
    unsupported (git < 2.31) or a key were misspelled, this returns the original
    SSH url and the assertion fails here — in CI, rather than on the one click
    that matters.
    """
    https_prefix, ssh_prefix = _rewrite_rules()[index]
    env = _hermetic_git_env(tmp_path, _image_git_config_env())
    repo = _repo_with_origin(tmp_path, ssh_prefix + "OWNER/REPO.git", env)
    out = subprocess.run(["git", "-C", str(repo), "ls-remote", "--get-url", "origin"],
                         check=True, capture_output=True, text=True, env=env)
    assert out.stdout.strip() == https_prefix + "OWNER/REPO.git"


def test_image_git_config_env_leaves_other_hosts_alone(tmp_path):
    """The rule covers one host only. Rewriting every host would silently point
    a self-hoster's private GitLab or Gitea origin at an https:// URL that may
    not exist; the `remote` self-check reporting it plainly is the better
    failure."""
    other = "git@example.org:team/repo.git"
    env = _hermetic_git_env(tmp_path, _image_git_config_env())
    repo = _repo_with_origin(tmp_path, other, env)
    out = subprocess.run(["git", "-C", str(repo), "ls-remote", "--get-url", "origin"],
                         check=True, capture_output=True, text=True, env=env)
    assert out.stdout.strip() == other


def test_dockerfile_installs_no_ssh_client():
    """A GUARD, not an omission.

    The obvious "fix" for the cannot-run-ssh failure is `apk add openssh` plus a
    deploy key. That is rejected: this container holds the Docker socket, so it
    is root-equivalent on the host, and adding the host's GitHub credentials
    would mean one bug there yields both root on the box AND push access to the
    source it deploys. The HTTPS rewrite above exists precisely so no credential
    has to live here. If this assertion starts failing, the rewrite is no longer
    the thing keeping keys out of the sidecar.
    """
    with open(DOCKERFILE) as fh:
        # Comments are stripped first: the reason this rule exists is written
        # out in the Dockerfile in prose, and matching that prose would make the
        # guard fail on the very explanation of itself.
        instructions = "\n".join(
            l for l in fh if not l.lstrip().startswith("#")
        )
    assert "openssh" not in instructions, \
        "the sidecar must stay credential-free; fetch anonymously over HTTPS instead"


def test_release_workflow_publishes_the_updater_image():
    with open(".github/workflows/release.yml") as fh:
        src = fh.read()
    assert "file: updater/Dockerfile" in src, "CI must build the sidecar image"
    assert "-updater:${{ github.ref_name }}" in src
    assert "-updater:latest" in src
    assert "steps.img.outputs.name" in src, \
        "reuse the lowercased repo name; do not recompute or hardcode it"
