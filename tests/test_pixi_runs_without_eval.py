"""Every PixiJS entry point imports the CSP-safe build (ISS-151).

The CSP forbids eval (script-src has no 'unsafe-eval'), and PixiJS checks for
eval when its renderer starts and otherwise refuses to run, leaving the star map
blank. Importing 'pixi.js/unsafe-eval' swaps in versions of the generated code
that do not use eval. There is no browser in CI, so this pins the source.
"""
import pathlib
import re

_SRC = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "src"
_IMPORT = re.compile(r"""^\s*import\s+['"]pixi\.js/unsafe-eval['"]\s*;?\s*$""", re.MULTILINE)
_STARTS_RENDERER = re.compile(r"new\s+Application\s*\(|autoDetectRenderer\s*\(")


def _files_that_start_a_renderer():
    return [
        p for p in _SRC.rglob("*")
        if p.suffix in {".ts", ".tsx"} and _STARTS_RENDERER.search(p.read_text())
    ]


def test_the_star_map_imports_the_csp_safe_build():
    star_map = _SRC / "components" / "StarMap" / "StarMap.tsx"
    assert _IMPORT.search(star_map.read_text())


def test_every_file_that_starts_a_renderer_imports_it():
    files = _files_that_start_a_renderer()
    assert files, "no Pixi renderer found; this pin is stale"
    missing = [str(p.relative_to(_SRC)) for p in files if not _IMPORT.search(p.read_text())]
    assert not missing, f"start a Pixi renderer without importing pixi.js/unsafe-eval: {missing}"
