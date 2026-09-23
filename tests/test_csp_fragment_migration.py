"""Where a fragment's behaviour is allowed to live.

`tests/test_csp_inline_handlers.py::test_partials_carry_no_script_tags` says a
fragment may not ship a script element. That closes the obvious half of the
bug. These tests close the other half: the behaviour has to have landed
somewhere that actually executes, and be reachable the way the dispatcher
reaches things.

The failure this guards against is subtle. `test_every_data_binding_resolves`
in the sibling module accepts a handler defined ANYWHERE, partials included —
which is precisely how eighteen fragments passed their tests for months while
every control in them was dead: the definition was right there in the file,
in a script element the browser refused to run. A binding in a fragment must
resolve to a definition on the PAGE.
"""

import os
import re

# One source of truth for what is still waiting to be migrated: a fragment
# that may still ship a script element necessarily still defines its handlers
# there too, so both tests must skip the same files.
from tests.test_csp_inline_handlers import PARTIALS_WITH_SCRIPT_ALLOWED

ROOT = os.path.join(os.path.dirname(__file__), "..")
TEMPLATES = os.path.join(ROOT, "app", "templates")
PARTIALS = os.path.join(TEMPLATES, "partials")
STATIC_JS = os.path.join(ROOT, "static", "js")

_BINDING = re.compile(
    r'data-(?:click|change|input|submit|keydown|mousedown|focus|on-error)'
    r'\s*=\s*"([A-Za-z_$][\w$]*)"'
)
# data-on-error also accepts this literal, handled inside the dispatcher.
_BUILTIN = {"hide"}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _walk(root, suffix):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in sorted(files):
            if name.endswith(suffix):
                yield os.path.join(dirpath, name)


def _page_level_definitions():
    """Names defined somewhere that runs as part of the page.

    That means static/js/*.js (covered by `script-src 'self'`, nonce or no)
    and any template that is NOT a fragment — a page's own script block
    carries the page's own nonce, which is the whole point.
    """
    defined = set()
    sources = list(_walk(STATIC_JS, ".js"))
    sources += [p for p in _walk(TEMPLATES, ".html")
                if "partials" not in os.path.relpath(p, ROOT).split(os.sep)]
    for path in sources:
        body = _read(path)
        defined |= set(re.findall(r'window\.([A-Za-z_$][\w$]*)\s*=', body))
        defined |= set(re.findall(r'\bfunction\s+([A-Za-z_$][\w$]*)\s*\(', body))
    return defined


def test_partial_bindings_resolve_to_page_level_definitions():
    """A data-* binding in a fragment must point at a handler defined on the
    page, not one defined in the fragment beside it."""
    defined = _page_level_definitions()
    missing = {}
    for path in _walk(PARTIALS, ".html"):
        rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
        if rel in PARTIALS_WITH_SCRIPT_ALLOWED:
            continue
        for name in sorted(set(_BINDING.findall(_read(path)))):
            if name not in _BUILTIN and name not in defined:
                missing.setdefault(rel, []).append(name)
    assert not missing, (
        "fragment bindings with no page-level handler — a definition inside "
        "the fragment does NOT count, because the browser refuses to run it: "
        f"{missing}"
    )


def test_migrated_handlers_take_no_positional_arguments():
    """The dispatcher calls `fn.call(el, event)` — handlers read their
    arguments from `this.dataset`, never from a parameter list. A handler
    that still expects positional args would be called with the event in the
    first slot and fail in a way no test elsewhere would catch."""
    # Handlers that moved out of a fragment in this change, with the page
    # script each one landed in.
    moved = {
        "app/templates/admin.html": [
            "adminAuditFilter", "adminSetRole", "searchAllowlist",
            "selectAllowlistResult", "clearAllowlistSearch", "addAllowlistEntry",
        ],
        "app/templates/industry.html": [
            "toggleBuildAll", "toggleComponentFromEl", "subMeSlide",
            "recalcComponentFromEl", "toggleSubComponent", "copyParentSettings",
            "sendToCompressor",
        ],
        "app/templates/character_detail.html": ["toggleAssetLocation", "toggleMail"],
        "app/templates/planetary_chain.html": ["piLoadNode"],
        "static/js/actions.js": ["copyToClipboard", "sendToHauling"],
    }
    problems = []
    for rel, names in moved.items():
        body = _read(os.path.join(ROOT, rel))
        for name in names:
            m = re.search(
                r'window\.' + name + r'\s*=\s*(?:window\.' + name
                + r'\s*\|\|\s*)?function\s*\(([^)]*)\)', body)
            if not m:
                problems.append(f"{rel}: {name} not defined as a window.<name> function")
                continue
            params = [p.strip() for p in m.group(1).split(",") if p.strip()]
            # One parameter is allowed and is the event — anything more means
            # the caller was expected to pass data in.
            if len(params) > 1:
                problems.append(f"{rel}: {name} takes {params}, expected dataset reads")
    assert not problems, problems


def test_after_swap_hooks_are_scoped_to_a_target():
    """Per-swap initialisation (charts, panel wiring) has to re-run after
    every swap, and only for the panel that swapped.

    An unscoped htmx:afterSwap re-runs on every lazy panel on the page — the
    dashboard alone has a dozen — and a hook that runs once at page load
    never fires at all for content that arrives later. Both failure modes
    look like "the chart is sometimes blank", so pin the scoping.
    """
    expectations = [
        # (file, hook target it must name, the initialiser it must call)
        ("app/templates/admin.html", "admin-content", "adminRenderCharts"),
        ("app/templates/character_detail.html", "kill-stats-panel", "renderCombatCharts"),
        ("static/js/actions.js", "history-panel", "initActivityHistory"),
    ]
    problems = []
    for rel, target, initialiser in expectations:
        body = _read(os.path.join(ROOT, rel))
        if "htmx:afterSwap" not in body:
            problems.append(f"{rel}: no htmx:afterSwap hook")
        if f"'{target}'" not in body and f'"{target}"' not in body:
            problems.append(f"{rel}: hook does not name the {target} swap target")
        if initialiser not in body:
            problems.append(f"{rel}: does not call {initialiser}")

    # The dashboard's activity panel swaps with outerHTML, so it is scoped by
    # subtree rather than by target id — detail.target is the detached old
    # node. Check the guard that makes that safe rather than a target name.
    dash = _read(os.path.join(ROOT, "app/templates/dashboard.html"))
    if "htmx:afterSwap" not in dash or "isConnected" not in dash:
        problems.append(
            "app/templates/dashboard.html: activity/combat panels need an "
            "htmx:afterSwap hook with the isConnected fallback (outerHTML swap)")
    assert not problems, problems


def test_fragment_charts_carry_their_series_in_the_markup():
    """A chart fragment has to hand its series over as data, not as script.

    A `type=\"application/json\"` data island is still a script element and
    still gets re-created on swap, so the general no-script rule covers it —
    this pins the shape that replaced it, per canvas, so a future chart copies
    the right pattern.
    """
    expected = {
        "partials/admin_esi.html": ["data-chart="],
        "partials/character_kill_stats.html": ["data-chart-kind=", "data-chart="],
        "partials/dashboard_combat_profile.html": ["data-chart-kind=", "data-chart="],
        "partials/dashboard_activity.html": ["data-activity-chart="],
        "partials/mining_ledger_data.html": ["data-chart="],
    }
    problems = []
    for rel, needles in expected.items():
        body = _read(os.path.join(TEMPLATES, rel))
        for needle in needles:
            if needle not in body:
                problems.append(f"{rel}: missing {needle}")
    assert not problems, problems


def test_admin_loads_chart_js_at_page_level():
    """The ESI section's chart library used to ship inside the fragment,
    where its tag carried the fragment's nonce and was refused. A library tag
    is subject to exactly the same rule as an inline block, so it has to be
    requested by the page."""
    body = _read(os.path.join(TEMPLATES, "admin.html"))
    tag = re.search(r'<script[^>]*src="[^"]*chart\.js[^"]*"[^>]*>', body)
    assert tag, "admin.html does not load Chart.js at page level"
    assert "nonce=" in tag.group(0), tag.group(0)
