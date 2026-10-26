"""Everything imported under app/ must exist in the release image (ISS-140).

The image holds only what the final Dockerfile stage COPYs, plus the
distributions in requirements.txt. A checkout has far more on sys.path: CI and
a laptop run from the repo root, so `scripts/`, `tests/` and `updater/` import
fine there and fail only inside the container. The original case was a
function-level `from build_map_data import ...` behind a sys.path insert: it
passed every test, and in the image a caught ImportError turned it into one log
warning (T-085).

This check walks every import statement under app/, nested ones included, and
fails on a name that only a checkout provides, on a dev-only dependency, and on
dynamic imports that would hide the target from the walk. It runs on every PR.
scripts/image_smoke.py is the matching check against the built image at release
time; the packaged set here is derived from the same COPY lines that
test_sde_map_data.py pins.
"""
import ast
import importlib.metadata as md
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}
DYNAMIC = {"import_module", "__import__", "spec_from_file_location",
           "module_from_spec", "run_path", "run_module"}


def packaged_prefixes(root: Path) -> set[str]:
    """Repo paths the final Dockerfile stage copies to the same path under WORKDIR."""
    text = re.sub(r"\\\n", " ", (root / "Dockerfile").read_text())
    lines = [ln.strip() for ln in text.splitlines()]
    last_from = max(i for i, ln in enumerate(lines) if ln.upper().startswith("FROM "))
    prefixes = set()
    for ln in lines[last_from:]:
        if not ln.upper().startswith("COPY "):
            continue
        parts = ln.split()[1:]
        if any(p.startswith("--from") for p in parts):
            continue  # another stage's output, not a repo path
        args = [p for p in parts if not p.startswith("--")]
        *srcs, dest = args
        if dest.startswith("/"):
            continue  # outside WORKDIR (the entrypoint): never importable
        for src in srcs:
            if src.endswith("/") and dest == "./" + src:
                prefixes.add(src)  # COPY app/ ./app/
            elif not src.endswith("/") and dest == "./":
                prefixes.add(src)  # COPY README.md ./
            else:
                raise AssertionError(
                    f"unrecognised COPY layout {ln!r}; teach packaged_prefixes about it")
    ignored = {ln.strip().strip("/") for ln in (root / ".dockerignore").read_text().splitlines()
               if ln.strip() and not ln.startswith(("#", "!")) and not set("*?[") & set(ln)}
    return {p for p in prefixes
            if not any(p.rstrip("/") == i or p.startswith(i + "/") for i in ignored)}


def _python_files(root: Path) -> list[Path]:
    """Tracked .py files: exactly what CI checks out, so a laptop's worktrees,
    scratch directories and differently named venvs cannot change the answer."""
    try:
        out = subprocess.run(["git", "ls-files", "*.py"], cwd=root, capture_output=True,
                             text=True, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        out = []
    if out:
        return [Path(p) for p in out]
    return [p.relative_to(root) for p in root.rglob("*.py")
            if not SKIP_DIRS & set(p.relative_to(root).parts)]


def checkout_module_names(root: Path, prefixes: set[str]) -> dict[str, str]:
    """name -> path for every importable name outside the image."""
    names = {}
    for rel in _python_files(root):
        if any(str(rel).startswith(p) or str(rel) == p for p in prefixes):
            continue
        if len(rel.parts) > 1:
            names.setdefault(rel.parts[0], str(rel))
        if rel.stem != "__init__":
            names.setdefault(rel.stem, str(rel))  # reachable via a sys.path insert
    return names


def requirement_names(path: Path) -> set[str]:
    out = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "-")):
            out.add(_norm(re.split(r"[<>=!~\s\[;]", line, maxsplit=1)[0]))
    return out


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def violations(root: Path) -> list[str]:
    prefixes = packaged_prefixes(root)
    assert "app/" in prefixes, f"Dockerfile parser lost `COPY app/`: {prefixes}"
    packaged_roots = {p.split("/")[0] for p in prefixes}
    checkout = checkout_module_names(root, prefixes)
    dev_only = (requirement_names(root / "requirements-dev.txt")
                - requirement_names(root / "requirements.txt"))
    dists = md.packages_distributions()
    stdlib = set(sys.stdlib_module_names)
    bad = []
    for path in sorted((root / "app").rglob("*.py")):
        rel = path.relative_to(root)
        for node in ast.walk(ast.parse(path.read_text(), filename=str(rel))):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name in DYNAMIC:
                    bad.append(f"{rel}:{node.lineno} dynamic import ({name}) "
                               f"hides the target from this check")
                continue
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                mods = [node.module]
            else:
                continue
            for mod in mods:
                top = mod.split(".")[0]
                if top in stdlib or top in packaged_roots:
                    continue
                if top in checkout:
                    bad.append(f"{rel}:{node.lineno} imports {mod!r}, which exists only in a "
                               f"checkout ({checkout[top]}); the image does not contain it")
                    continue
                owners = {_norm(d) for d in dists.get(top, [])}
                if not owners:
                    bad.append(f"{rel}:{node.lineno} imports {mod!r}, which no installed "
                               f"distribution provides")
                elif owners <= dev_only:
                    bad.append(f"{rel}:{node.lineno} imports {mod!r} from {sorted(owners)}, "
                               f"which is only in requirements-dev.txt")
    return bad


def test_app_imports_only_what_the_image_contains():
    bad = violations(ROOT)
    assert not bad, "\n".join(bad)


def test_packaged_set_matches_the_dockerfile():
    assert packaged_prefixes(ROOT) >= {
        "app/", "static/", "design-system/css/", "design-system/ambient/", "README.md"}


# ---- self-tests: a flag case and a negative control per rule ----------------

DOCKERFILE = """\
FROM node:22-alpine AS frontend
COPY frontend/ ./
FROM python:3.12-slim
WORKDIR /app
COPY app/ ./app/
COPY README.md ./
COPY --from=frontend /build/dist ./frontend/dist
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
"""


def _repo(tmp_path, body: str, *, dev="pytest\nPyYAML\n"):
    (tmp_path / "Dockerfile").write_text(DOCKERFILE)
    (tmp_path / ".dockerignore").write_text("scripts/\n*.md\n!README.md\n")
    (tmp_path / "requirements.txt").write_text("fastapi==1\n")
    (tmp_path / "requirements-dev.txt").write_text(dev)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "mod.py").write_text(body)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "build_map_data.py").write_text("def build_from_zip(): ...\n")
    return tmp_path


def test_flags_a_module_level_import_from_scripts(tmp_path):
    bad = violations(_repo(tmp_path, "from scripts.build_map_data import build_from_zip\n"))
    assert len(bad) == 1 and "exists only in a checkout" in bad[0]


def test_flags_a_function_level_sys_path_import(tmp_path):
    body = ("def run():\n"
            "    from build_map_data import build_from_zip\n"
            "    return build_from_zip\n")
    bad = violations(_repo(tmp_path, body))
    assert len(bad) == 1 and "build_map_data" in bad[0] and "mod.py:2" in bad[0]


def test_flags_a_dev_only_dependency(tmp_path):
    bad = violations(_repo(tmp_path, "import yaml\n"))
    assert len(bad) == 1 and "requirements-dev.txt" in bad[0]


def test_flags_a_name_nothing_provides(tmp_path):
    bad = violations(_repo(tmp_path, "import not_a_real_module_xyz\n"))
    assert len(bad) == 1 and "no installed distribution" in bad[0]


def test_flags_dynamic_imports(tmp_path):
    bad = violations(_repo(tmp_path, "import importlib\nimportlib.import_module('x')\n"))
    assert len(bad) == 1 and "dynamic import" in bad[0]


def test_accepts_stdlib_app_and_runtime_dependencies(tmp_path):
    body = "import json\nfrom app import mod\nimport fastapi\nfrom . import sibling\n"
    assert violations(_repo(tmp_path, body)) == []


def test_parser_ignores_other_stage_copies_and_the_entrypoint(tmp_path):
    prefixes = packaged_prefixes(_repo(tmp_path, ""))
    assert prefixes == {"app/", "README.md"}


def test_parser_refuses_a_copy_layout_it_does_not_understand(tmp_path):
    repo = _repo(tmp_path, "")
    (repo / "Dockerfile").write_text(DOCKERFILE + "COPY extra/ ./vendored/\n")
    try:
        packaged_prefixes(repo)
    except AssertionError as exc:
        assert "unrecognised COPY layout" in str(exc)
    else:
        raise AssertionError("an unknown COPY layout must fail loudly")
