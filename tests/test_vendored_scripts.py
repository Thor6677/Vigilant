"""ISS-066: no page loads a script from a third-party host.

htmx, Chart.js, its date-fns adapter and SortableJS are vendored under
static/vendor/ (see the README there for provenance). CSP already limits
script-src to 'self' plus the nonce; this pins that the templates keep to it,
that the vendored bytes are the verified ones, and that they are served.
"""
import hashlib
import re
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "static" / "vendor"

# file -> sha256 of the file taken from the npm tarball (integrity verified)
MANIFEST = {
    "htmx-1.9.12.min.js": "449317ade7881e949510db614991e195c3a099c4c791c24dacec55f9f4a2a452",
    "chart-4.4.0.umd.js": "321e3a3fa98da4aaa957d10be57cbb514de0989eed8f9d726b5d05902cd01904",
    "chartjs-adapter-date-fns-3.0.0.bundle.min.js":
        "ea7ab30d26c38dcf1f2d26bb43e73a94537b58f1906f55e1a546dd09321b5615",
    "sortablejs-1.15.3.min.js": "72aa2c4f9f7cb2b8b3268052d6d2daa9d952f209b7fc5cc247ff9e1153db1f16",
}

_SCRIPT_SRC = re.compile(r"<script\b[^>]*\bsrc\s*=\s*[\"']?\s*((?:https?:)?//[^\"'\s>]+)", re.I)


def test_no_template_loads_an_external_script():
    offenders = []
    for path in sorted((ROOT / "app" / "templates").rglob("*.html")):
        for m in _SCRIPT_SRC.finditer(path.read_text()):
            offenders.append(f"{path.relative_to(ROOT)}: {m.group(1)}")
    assert not offenders, offenders


def test_vendored_files_match_the_manifest():
    assert sorted(p.name for p in VENDOR.glob("*.js")) == sorted(MANIFEST)
    for name, digest in MANIFEST.items():
        assert hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() == digest, name


def test_every_vendored_reference_points_at_a_vendored_file():
    seen = set()
    for path in (ROOT / "app" / "templates").rglob("*.html"):
        seen.update(re.findall(r"/static/vendor/([^\"'\s>]+)", path.read_text()))
    assert seen and seen <= set(MANIFEST), seen - set(MANIFEST)
    assert seen == set(MANIFEST)      # nothing vendored that no page uses


def test_vendored_files_are_served():
    import app.main as main
    client = TestClient(main.app)
    for name in MANIFEST:
        r = client.get(f"/static/vendor/{name}")
        assert r.status_code == 200, name
        assert hashlib.sha256(r.content).hexdigest() == MANIFEST[name]


def test_dockerfile_copies_static_wholesale():
    assert re.search(r"^COPY static/ \./static/$", (ROOT / "Dockerfile").read_text(), re.M)
