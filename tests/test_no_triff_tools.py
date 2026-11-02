"""T-090 PR 2: nothing under app/ talks to triff.tools any more. The word
"triff" survives only as the label of the frozen calibration rows."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
SUFFIXES = {".py", ".html", ".js", ".json", ".css", ".md", ".txt"}
PATTERN = re.compile(r"triff\W{0,3}tools|triff%2etools|https?://[^\s\"'<>]*triff", re.IGNORECASE)


def test_the_pattern_matches_every_host_form_and_spares_the_row_label():
    for bad in ("https://triff.tools/api/structure-intel/age", "TRIFF.TOOLS",
                "triff[.]tools", "//api.triff.tools", "triff%2eTools", "http://x.triff.example/"):
        assert PATTERN.search(bad), bad
    for ok in ("source == 'triff'", "frozen triff rows", 'sources=("triff",)'):
        assert not PATTERN.search(ok), ok


def test_triff_tools_appears_nowhere_under_app():
    hits = []
    for path in (ROOT / "app").rglob("*"):
        if not path.is_file() or path.suffix not in SUFFIXES or "__pycache__" in path.parts:
            continue
        for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if PATTERN.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{n}")
    assert hits == []


def test_the_scraper_and_its_admin_routes_are_gone():
    assert not (ROOT / "app/intel/structure_age_scraper.py").exists()
    for path in (ROOT / "app").rglob("*.py"):
        text = path.read_text()
        assert "structure_age_scraper" not in text, path
        assert "/admin/structure-age/scrape" not in text, path
        assert "/admin/structure-age/status" not in text, path
