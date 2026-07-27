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
import sys

# Files the sidecar image actually copies out of the app package.
COPIED = ["app/__init__.py", "app/ops/__init__.py", "app/ops/version.py"]

_STDLIB = set(sys.stdlib_module_names)


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


def test_dockerfile_copies_exactly_the_files_this_test_guards():
    """If someone adds a COPY of another app/ file, the guard above silently
    stops covering it."""
    with open("updater/Dockerfile") as fh:
        lines = [l.strip() for l in fh if l.strip().startswith("COPY ")]
    copied_app_files = sorted(
        l.split()[1] for l in lines if l.split()[1].startswith("app/")
    )
    assert copied_app_files == sorted(p for p in COPIED), copied_app_files


def test_dockerfile_declares_no_user_directive():
    """The uid must match the host repo owner, which varies per install, so
    compose supplies it. A USER here would leave root-owned .env and .git
    objects and brick the scripts/deploy.sh fallback."""
    with open("updater/Dockerfile") as fh:
        for line in fh:
            assert not line.strip().upper().startswith("USER "), \
                "uid must come from compose, not the image"


def test_dockerfile_sets_home_and_unbuffered_output():
    with open("updater/Dockerfile") as fh:
        src = fh.read()
    assert "HOME=" in src, "the container uid is not in /etc/passwd; git needs HOME"
    assert "PYTHONUNBUFFERED=1" in src, \
        "without this the deploy log appears only when the process exits"


def test_release_workflow_publishes_the_updater_image():
    with open(".github/workflows/release.yml") as fh:
        src = fh.read()
    assert "file: updater/Dockerfile" in src, "CI must build the sidecar image"
    assert "-updater:${{ github.ref_name }}" in src
    assert "-updater:latest" in src
    assert "steps.img.outputs.name" in src, \
        "reuse the lowercased repo name; do not recompute or hardcode it"
