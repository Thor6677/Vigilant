"""Contract tests for the fitting builder's blank-slate open and the
module state (online/overheated/offline) toggle's discoverability + coverage.

Style follows tests/test_csp_inline_handlers.py and tests/test_update_banner.py:
string/regex assertions against the template source, plus one route-level
smoke test that renders the real page through the app (exercising the CSP
nonce machinery, not just the raw template text).

Why these specific assertions:

* The page used to call restoreState() unconditionally on load, so it always
  reopened on the last-edited fit. The fix is a product change (open EMPTY,
  autosave kept only as an opt-in recovery path) -- the regression this guards
  against is someone reintroducing an unconditional restoreState() call in
  the init() IIFE, which would silently undo the blank-slate behavior without
  touching anything a human would think to grep for.
* overheatCache (state toggle capability) is populated by a dedicated
  /tools/fitting/can-overheat fetch, keyed by type_id. It must be refreshed
  after every code path that can introduce a NEW type_id into state.items --
  miss one and a module that can be overheated silently shows no overheat
  step. addModule() (search/click) already called it; doImportEFT() and
  loadFitting() (saved-fit load) did not.
"""
import os
import re

TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "templates", "fitting_tool.html"
)


def _read():
    with open(TEMPLATE_PATH, encoding="utf-8") as fh:
        return fh.read()


def _function_body(html, name):
    """Grab the body of `function <name>(...) { ... }` by brace matching.

    Good enough for this file's flat, non-nested-string function bodies --
    the same trick would break on a body containing a `{` inside a string
    literal, none of which occur in the functions this test inspects.
    """
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", html)
    assert m, f"no `function {name}(...)` found in {TEMPLATE_PATH}"
    start = m.end()
    depth = 1
    i = start
    while depth:
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
        i += 1
    return html[start : i - 1]


def _iife_body(html, marker):
    """Grab the body of the `(function <marker>() { ... })();` IIFE."""
    m = re.search(r"\(function\s+" + re.escape(marker) + r"\s*\(\)\s*\{", html)
    assert m, f"no `(function {marker}() {{...}})()` IIFE found"
    start = m.end()
    depth = 1
    i = start
    while depth:
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
        i += 1
    return html[start : i - 1]


# ── Item 1: open on a blank slate ────────────────────────────────────────


def test_init_does_not_call_restore_state_unconditionally():
    """The regression this guards against: init() calling restoreState()
    outside of a user-initiated (click) path would silently bring back
    open-on-last-fit."""
    html = _read()
    init_body = _iife_body(html, "init")
    assert "restoreState()" not in init_body, (
        "init() must not call restoreState() directly -- the page opens "
        "blank and offers a 'Restore last fit' control instead "
        "(see restoreSavedFit())"
    )
    assert "showRestoreNotice()" in init_body, (
        "init() must offer the restore control when a saved fit exists, "
        "instead of auto-applying it"
    )
    # The deep-link path (?load=<id>) must still take precedence and must
    # be mutually exclusive with the restore-notice branch, or a saved-fits
    # link would show the notice AND load the requested fit.
    assert re.search(
        r"if\s*\(loadId\)\s*\{.*?\}\s*else if\s*\(localStorage\.getItem\(FIT_KEY\)\)\s*\{",
        init_body,
        re.DOTALL,
    ), "loadId (deep-link) branch must be an if/else-if with the restore-notice branch"


def test_restore_state_only_reachable_via_explicit_user_action():
    """restoreState() itself may still exist (autosave-as-recovery needs it)
    but the only bare call site left must be inside restoreSavedFit(), which
    is wired to a click (data-click), never to page load."""
    html = _read()
    call_sites = [
        m.start() for m in re.finditer(r"\brestoreState\(\)\s*;", html)
    ]
    # One definition-adjacent call: inside restoreSavedFit().
    assert len(call_sites) == 1, (
        f"expected exactly one `restoreState();` call site (inside "
        f"restoreSavedFit()), found {len(call_sites)}"
    )
    restore_fn_body = _function_body(html, "restoreSavedFit")
    assert "restoreState()" in restore_fn_body
    assert "refreshOverheatCache()" in restore_fn_body, (
        "restoring a saved fit can introduce type_ids overheatCache has "
        "never checked -- restoreSavedFit() must refresh it"
    )


def test_restore_notice_markup_starts_hidden_and_wires_to_dispatcher():
    html = _read()
    m = re.search(
        r'<div id="restore-fit-notice"[^>]*style="([^"]*)"', html
    )
    assert m, "no #restore-fit-notice element in the template"
    assert "display:none" in m.group(1).replace(" ", ""), (
        "the notice must start hidden -- it is only shown by "
        "showRestoreNotice() when a saved fit is actually found"
    )
    # Must be shown/hidden via id, and wired through the data-click
    # dispatcher (no inline onclick=), per the CSP nonce policy.
    notice_block = html[m.start() : m.start() + 400]
    assert 'data-click="restoreSavedFit"' in notice_block, (
        "the restore control must use the data-click dispatcher, not an "
        "inline handler"
    )


def test_clear_fitting_still_clears_both_page_and_saved_state():
    """Item 1 requires the explicit New-fit/Clear control to still wipe both
    the in-memory state and localStorage -- this was already true, this
    test just pins it so a future edit to clearFitting() can't drop it."""
    html = _read()
    clear_body = _function_body(html, "clearFitting")
    assert "localStorage.removeItem(FIT_KEY)" in clear_body


# ── Item 2a: discoverability (tooltips, aria-label, legend) ─────────────


def test_state_dot_tooltips_cover_all_four_cases():
    html = _read()
    render_body = _function_body(html, "renderSlots")
    for expected in [
        "Online — click to overheat",
        "Overheated — click to offline",
        "Online — click to offline",
        "Offline — click to online",
    ]:
        assert expected in render_body, (
            f"missing state-dot tooltip text: {expected!r}"
        )


def test_state_dot_carries_aria_label():
    html = _read()
    render_body = _function_body(html, "renderSlots")
    # The toggleOnline button is built as one JS string-concat expression;
    # assert it carries an aria-label bound to the same stateTitle variable
    # used for its title= tooltip, without over-fitting to exact spacing.
    m = re.search(r"data-click=\\?\"toggleOnline\\?\".*?;\n", render_body)
    assert m, "no toggleOnline button build line found in renderSlots()"
    onlinebtn_line = m.group(0)
    assert "aria-label" in onlinebtn_line, (
        "the toggleOnline button must carry an aria-label"
    )
    assert onlinebtn_line.count("stateTitle") >= 2, (
        "aria-label and title must both read from the same stateTitle "
        "variable (one text, not two to keep in sync)"
    )


def test_slot_layout_has_a_state_legend():
    html = _read()
    # Scoped to the slot-layout container so this doesn't just match
    # unrelated text elsewhere in the page.
    idx = html.index('id="slot-layout"')
    legend_region = html[idx : idx + 1000]
    assert "online" in legend_region
    assert "overheated" in legend_region
    assert "offline" in legend_region


# ── Item 2b: overheatCache coverage for every path that adds items ──────


def test_overheat_cache_refreshed_after_module_search_add():
    html = _read()
    assert "refreshOverheatCache()" in _function_body(html, "addModule")


def test_overheat_cache_refreshed_after_eft_import():
    html = _read()
    assert "refreshOverheatCache()" in _function_body(html, "doImportEFT"), (
        "EFT-imported items were never checked against can-overheat -- a "
        "hardener imported via EFT must still get its overheat step"
    )


def test_overheat_cache_refreshed_after_loading_a_saved_fit():
    html = _read()
    assert "refreshOverheatCache()" in _function_body(html, "loadFitting"), (
        "loading a saved fit (Saved Fits page -> ?load=<id>) must refresh "
        "overheatCache once state.items is actually populated -- init()'s "
        "own call races the fetch and runs while state.items is still empty"
    )


def test_overheat_cache_refreshed_after_restoring_from_localstorage():
    html = _read()
    assert "refreshOverheatCache()" in _function_body(html, "restoreSavedFit")


# ── Route smoke test: the real page renders with both features present ──


def test_fitting_tool_page_renders_with_notice_and_legend():
    from fastapi.testclient import TestClient

    import app.main as main

    with TestClient(main.app) as client:
        r = client.get("/tools/fitting")
    assert r.status_code == 200
    body = r.text
    assert 'id="restore-fit-notice"' in body
    assert "Module state:" in body
    # The template's own <script nonce="..."> tags must carry a real nonce
    # (test_csp_inline_handlers.py covers this globally; this just confirms
    # the page that ships the new functions is one of the covered ones).
    assert re.search(r'<script nonce="[^"]+">', body)
