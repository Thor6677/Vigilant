"""T-033 guard rails: no inline event handlers, and no dead bindings.

`script-src` can only drop `'unsafe-inline'` once no template ships an
inline `on*=` attribute or a `javascript:` URL — a nonce cannot be attached
to either, so a CSP with a nonce and no `'unsafe-inline'` blocks them both.
The migration replaces them with delegated `data-<event>="fn"` attributes
dispatched by `static/js/actions.js`.

Two tests here, pulling in opposite directions:

* `test_no_inline_event_handlers_remain` is the finish line. It ratchets:
  the counts below may only ever go down. Converting a file means lowering
  its entry (or deleting it); nothing may be added.
* `test_every_data_binding_resolves` is the safety net for the conversion
  itself. A `data-click="foo"` whose `foo` is not defined anywhere fails
  silently in the browser (actions.js warns to the console and returns), so
  a typo would turn a working button into a dead one with no other signal.
"""

import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")
TEMPLATES = os.path.join(ROOT, "app", "templates")
APP = os.path.join(ROOT, "app")
STATIC_JS = os.path.join(ROOT, "static", "js")

# Inline handler attributes still to convert, by template. MUST only shrink.
# Empty dict = T-033 unblocked on the template side.
REMAINING = {
    "app/db/models.py": 5,
    "app/templates/blueprints.html": 1,
    "app/templates/character_detail.html": 1,
    "app/templates/compression.html": 1,
    "app/templates/corp_inventory.html": 1,
    "app/templates/fittings.html": 2,
    "app/templates/hauling.html": 3,
    "app/templates/industry.html": 2,
    "app/templates/intel.html": 1,
    "app/templates/intel_dscan.html": 2,
    "app/templates/intel_local.html": 2,
    "app/templates/mining.html": 1,
    "app/templates/partials/admin_audit.html": 1,
    "app/templates/partials/admin_users.html": 2,
    "app/templates/partials/calc_results.html": 1,
    "app/templates/partials/compression_results.html": 1,
    "app/templates/partials/contract_alert_banners.html": 1,
    "app/templates/partials/corp_inventory_scan.html": 1,
    "app/templates/partials/fitting_info.html": 1,
    "app/templates/partials/fitting_stats.html": 2,
    "app/templates/partials/gatecheck_finder.html": 1,
    "app/templates/partials/gatecheck_route.html": 1,
    "app/templates/partials/gatecheck_wartarget.html": 1,
    "app/templates/partials/inventory_alert_banners.html": 1,
    "app/templates/partials/mail_panel.html": 1,
    "app/templates/partials/mining_ledger_corp.html": 1,
    "app/templates/partials/mining_ledger_data.html": 1,
    "app/templates/partials/planetary_chain_node.html": 2,
    "app/templates/partials/shopping_list.html": 1,
    "app/templates/partials/structure_alert_banners.html": 1,
    "app/templates/partials/timer_alert_banners.html": 1,
    "app/templates/planetary_calculator.html": 2,
    "app/templates/planetary_chain.html": 1,
    "app/templates/planetary_lookup.html": 3,
    "app/templates/ship_mastery.html": 1,
    "app/templates/skills.html": 1,
    "app/templates/tools_image_view.html": 2,
    "app/templates/wormhole_system.html": 2,
}

# `javascript:` URLs still to convert. MUST only shrink.
REMAINING_JS_URLS = {}

_HANDLER = re.compile(r'\bon([a-z]+)\s*=\s*\\?["\']')
_JS_URL = re.compile(r'(?:href|src)\s*=\s*\\?["\']javascript:')
# data-<event>="name" bindings that actions.js dispatches by looking up
# window[name]. data-on-error also accepts the literal "hide".
_BINDING = re.compile(
    r'data-(?:click|change|input|submit|keydown|mousedown|focus|on-error)'
    r'\s*=\s*"([A-Za-z_$][\w$]*)"'
)


def _templates():
    for dirpath, _dirs, files in os.walk(TEMPLATES):
        for name in sorted(files):
            if name.endswith(".html"):
                yield os.path.join(dirpath, name)


def _html_sources():
    """Templates, plus the route modules that build HTML fragments in Python.

    Several search-dropdown endpoints return hand-assembled HTML strings
    carrying their own onclick/onmouseover attributes. They are invisible to
    a templates-only scan and would have survived the migration to break in
    production the moment the policy started enforcing — so they are scanned
    with the same eyes.
    """
    yield from _templates()
    for dirpath, dirs, files in os.walk(APP):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in sorted(files):
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _rel(path):
    return os.path.relpath(path, ROOT).replace(os.sep, "/")


def test_no_inline_event_handlers_remain():
    found = {}
    for path in _html_sources():
        with open(path, encoding="utf-8") as fh:
            count = len(_HANDLER.findall(fh.read()))
        if count:
            found[_rel(path)] = count

    regressions = {
        path: (count, REMAINING.get(path, 0))
        for path, count in found.items()
        if count > REMAINING.get(path, 0)
    }
    assert not regressions, (
        "inline on*= handlers added or un-converted (file: found vs allowed): "
        f"{regressions}. They block T-033 — use data-<event> and a handler on "
        "window instead (see static/js/actions.js)."
    )

    stale = {p: n for p, n in REMAINING.items() if p not in found or found[p] < n}
    assert not stale, (
        f"REMAINING is out of date — these are already converted: {stale}. "
        "Lower or delete the entries so the ratchet keeps its teeth."
    )


def test_no_javascript_urls_remain():
    """`javascript:` URLs are inline script too, and equally un-nonceable."""
    offenders = {}
    for path in _html_sources():
        with open(path, encoding="utf-8") as fh:
            count = len(_JS_URL.findall(fh.read()))
        if count > REMAINING_JS_URLS.get(_rel(path), 0):
            offenders[_rel(path)] = count
    assert not offenders, f"javascript: URLs block T-033: {offenders}"


def test_every_data_binding_resolves():
    """Every data-<event> name must have a `window.<name> =` definition."""
    defined = set()
    sources = []
    for dirpath, _dirs, files in os.walk(STATIC_JS):
        sources += [os.path.join(dirpath, f) for f in files if f.endswith(".js")]
    sources += list(_templates())
    for path in sources:
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        defined |= set(re.findall(r'window\.([A-Za-z_$][\w$]*)\s*=', body))
        # A `function name(...)` declaration inside a nonced block is also
        # reachable as window.name. Indentation says nothing here (template
        # blocks are indented arbitrarily) and telling a top-level
        # declaration from one nested in an IIFE would need a real parser —
        # so this matches any of them. It errs toward accepting: the point is
        # to catch a binding whose handler does not exist under that name at
        # all, which is the failure mode that leaves a dead control.
        defined |= set(re.findall(r'\bfunction\s+([A-Za-z_$][\w$]*)\s*\(', body))

    missing = {}
    for path in _html_sources():
        with open(path, encoding="utf-8") as fh:
            for name in set(_BINDING.findall(fh.read())):
                if name != "hide" and name not in defined:
                    missing.setdefault(_rel(path), set()).add(name)
    assert not missing, (
        "data-* bindings with no matching window.<name> — these are dead "
        f"controls in the browser: { {k: sorted(v) for k, v in missing.items()} }"
    )
