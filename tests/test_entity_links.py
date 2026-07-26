"""Tests for the `entity_links` cross-link macro (partials/_entity_links.html).

Two disciplines here:

1. **Render correctness** — the macro is rendered standalone through a Jinja
   Environment with a FileSystemLoader rooted at `app/templates` and autoescape
   on (matching the app's template setup). We assert the right chips appear,
   `current` suppresses self-links, `jspace` gates the WH Info chip, dotlan gets
   underscores for spaces, and item names are urlencoded.

2. **Dead-link guard** (same idea as tests/test_nav_registry.py) — every
   *internal* href the macro can emit must resolve to a real route on the app.
   Because the macro's internal links carry embedded ids/names, we match against
   each route's compiled `path_regex` (stronger than the registry test's exact
   string match) after stripping the query string.
"""

import os
import re

from jinja2 import Environment, FileSystemLoader

import app.main as main

_TEMPLATES = os.path.join(
    os.path.dirname(__file__), "..", "app", "templates"
)


def _macro():
    env = Environment(loader=FileSystemLoader(_TEMPLATES), autoescape=True)
    tmpl = env.get_template("partials/_entity_links.html")
    return tmpl.module.entity_links


def _hrefs(html):
    return re.findall(r'href="([^"]+)"', str(html))


# ── character ────────────────────────────────────────────────────────────────

def test_character_overview_current_suppresses_self_link():
    html = _macro()("character", 90000001, "Bob Pilot", current="overview")
    hrefs = _hrefs(html)
    assert "/character/90000001" not in hrefs                 # suppressed
    assert "/character/90000001/skills" in hrefs
    assert "/character/90000001/journal" in hrefs
    assert "/character/90000001/blueprints" in hrefs
    assert "/character/90000001/fittings" in hrefs
    assert "/character/90000001/mining" in hrefs
    assert "https://zkillboard.com/character/90000001/" in hrefs


def test_character_no_current_includes_overview():
    hrefs = _hrefs(_macro()("character", 42, "X"))
    assert "/character/42" in hrefs


def test_character_current_zkb_suppresses_external():
    hrefs = _hrefs(_macro()("character", 42, "X", current="zkb"))
    assert "https://zkillboard.com/character/42/" not in hrefs
    assert "/character/42/skills" in hrefs


def test_external_chips_have_noopener_and_blank_target():
    html = str(_macro()("character", 42, "X"))
    # the external chip carries both attrs
    ext = [line for line in html.splitlines() if "zkillboard.com" in line][0]
    assert 'target="_blank"' in ext
    assert 'rel="noopener"' in ext


# ── system ───────────────────────────────────────────────────────────────────

def test_system_kspace_omits_wh_info():
    hrefs = _hrefs(_macro()("system", 30000142, "Jita"))  # jspace defaults False
    assert not any("/wormholes/system/" in h for h in hrefs)
    assert "/map?focus=30000142" in hrefs
    assert "https://zkillboard.com/system/30000142/" in hrefs
    assert "https://evemaps.dotlan.net/system/Jita" in hrefs


def test_system_jspace_includes_wh_info():
    hrefs = _hrefs(_macro()("system", 31000005, "J123456", jspace=True))
    assert "/wormholes/system/J123456" in hrefs


def test_system_current_wh_suppresses_wh_info_even_with_jspace():
    hrefs = _hrefs(
        _macro()("system", 31000005, "J123456", jspace=True, current="wh")
    )
    assert "/wormholes/system/J123456" not in hrefs
    assert "/map?focus=31000005" in hrefs


def test_system_dotlan_underscores_spaces():
    hrefs = _hrefs(_macro()("system", 30002187, "Amarr Prime"))
    assert "https://evemaps.dotlan.net/system/Amarr_Prime" in hrefs
    assert "https://evemaps.dotlan.net/system/Amarr Prime" not in hrefs


# ── item ─────────────────────────────────────────────────────────────────────

def test_item_build_and_zkb():
    hrefs = _hrefs(_macro()("item", 34, "Tritanium"))
    assert "/industry/manufacturing?search=Tritanium" in hrefs
    assert "https://zkillboard.com/item/34/" in hrefs


def test_item_build_urlencodes_spaces():
    hrefs = _hrefs(_macro()("item", 11987, "Heavy Assault Missile"))
    # Jinja's urlencode emits %20 for spaces (not '+')
    assert "/industry/manufacturing?search=Heavy%20Assault%20Missile" in hrefs


def test_item_current_build_suppresses_build():
    hrefs = _hrefs(_macro()("item", 34, "Tritanium", current="build"))
    assert not any("/industry/manufacturing" in h for h in hrefs)
    assert "https://zkillboard.com/item/34/" in hrefs


# ── dead-link guard: every internal href resolves to a real route ────────────

def _internal_hrefs():
    m = _macro()
    renders = [
        m("character", 90000001, "Bob Pilot"),
        m("system", 30000142, "Jita"),
        m("system", 31000005, "J123456", jspace=True),
        m("item", 34, "Tritanium With Spaces"),
    ]
    for html in renders:
        for h in _hrefs(html):
            if not h.startswith(("http://", "https://", "#")):
                yield h


def _routes():
    return [r for r in main.app.routes if hasattr(r, "path_regex")]


def test_every_internal_macro_href_matches_a_registered_route():
    routes = _routes()
    dead = []
    for href in _internal_hrefs():
        path = href.split("?", 1)[0]              # strip query string
        if not any(r.path_regex.match(path) for r in routes):
            dead.append(href)
    assert not dead, f"Macro internal hrefs with no registered route: {dead}"
