"""Check, inside the release image, that app/ can import everything it uses.

Run from the directory that
contains app/ (WORKDIR /app in the image), fed on stdin so sys.path[0] is the
cwd, exactly as uvicorn sees it:

    docker run --rm -i ... IMAGE python3 - < scripts/image_smoke.py

Two passes, both against the image's own interpreter and site-packages:

1. Resolve every import statement under app/, INCLUDING ones inside function
   bodies, with importlib.util.find_spec. Importing modules never executes a
   function body, so this pass is the one that catches a lazy import of
   something that exists only in a checkout (the T-085 shape).
2. Import every module under app/ (pkgutil walk). Catches what static
   resolution cannot: native-library and wheel failures, module-level reads of
   files the image lacks, import-time self-checks, read-only/uid effects.

Fails (exit 1) on any unresolvable import, any import error, any network
attempt during import, or any WARNING+ log record emitted by code under app/
(the T-085 failure was a caught error logged as one warning). Warnings from
third-party code are printed but do not fail.

Lives in scripts/ so .dockerignore keeps it out of the image. Never mount
anything under /app when running it: a mount would add files and mask exactly
the failure this exists to find. release.yml runs it against the built image;
tests/test_image_smoke.py runs it against the checkout on every PR.
"""
import ast
import importlib
import importlib.util
import logging
import os
import pkgutil
import socket
import sys
import traceback
from pathlib import Path

APP_DIR = Path("app").resolve()
problems = []
net_attempts = []


def _deny(*args, **kwargs):
    net_attempts.append(repr(args)[:200])
    raise OSError("network is disabled during the image smoke import")


socket.socket.connect = _deny
socket.socket.connect_ex = _deny
socket.create_connection = _deny
socket.getaddrinfo = _deny


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(record)


collector = _Collect()
logging.getLogger().addHandler(collector)

print(f"python {sys.version.split()[0]}  cwd={os.getcwd()}  sys.path[0]={sys.path[0]!r}")
if not APP_DIR.is_dir():
    sys.exit(f"no app/ under {os.getcwd()}; run this from the image WORKDIR")

# ---- pass 1: resolve every import statement, nested ones included ----------
stdlib = set(sys.stdlib_module_names)
checked = {}
for path in sorted(APP_DIR.rglob("*.py")):
    rel = path.relative_to(APP_DIR.parent)
    for node in ast.walk(ast.parse(path.read_text(), filename=str(rel))):
        if isinstance(node, ast.Import):
            targets = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level:
            targets = [node.module]
        else:
            continue
        for target in targets:
            top = target.split(".")[0]
            if top in stdlib:
                continue
            # app.* by full dotted name; third-party by top-level name only,
            # which find_spec resolves without importing anything.
            name = target if top == "app" else top
            if name not in checked:
                try:
                    checked[name] = importlib.util.find_spec(name) is not None
                except Exception as exc:  # parent package failed to import
                    checked[name] = False
                    problems.append(f"{rel}:{node.lineno} {name}: {exc!r}")
            if not checked[name]:
                problems.append(f"{rel}:{node.lineno} imports {target!r}: "
                                f"not resolvable in this image")
print(f"pass 1: {len(checked)} distinct non-stdlib import targets resolved")

# ---- pass 2: import every module ------------------------------------------
import app  # noqa: E402

names = ["app.main"] + [m.name for m in pkgutil.walk_packages(
    app.__path__, "app.",
    onerror=lambda n: problems.append(f"{n}: {traceback.format_exc()}"))]
imported = 0
for name in dict.fromkeys(names):
    try:
        importlib.import_module(name)
        imported += 1
    except BaseException:  # SystemExit too: an import must never exit
        problems.append(f"import {name} failed:\n{traceback.format_exc()}")
print(f"pass 2: imported {imported}/{len(dict.fromkeys(names))} modules under app/")

for rec in collector.records:
    ours = Path(rec.pathname).resolve().is_relative_to(APP_DIR)
    line = f"{rec.name}: {rec.levelname}: {rec.getMessage()}"
    if ours:
        problems.append(f"logged during import: {line}")
    else:
        print(f"note (third-party, not failing): {line}")
problems += [f"network attempt during import: {a}" for a in net_attempts]

for p in problems:
    print(f"FAIL {p}", file=sys.stderr)
if not problems:
    # Callers require this line, so an empty stdin or any early exit can
    # never pass vacuously.
    print(f"image-smoke: OK {imported} modules")
sys.exit(1 if problems else 0)
