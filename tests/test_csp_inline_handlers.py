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
REMAINING = {}

# `javascript:` URLs still to convert. MUST only shrink.
REMAINING_JS_URLS = {}

_HANDLER = re.compile(r'\bon([a-z]+)\s*=\s*\\?["\']')
# `ondelete` / `onupdate` are SQLAlchemy ForeignKey keywords, not DOM events.
# Denylisting the two beats allowlisting event names: an inline handler for
# an event nobody thought to list would otherwise pass unnoticed, which is
# exactly the failure this test exists to prevent.
_NOT_EVENTS = {"delete", "update"}


def _handlers(body):
    return [name for name in _HANDLER.findall(body) if name not in _NOT_EVENTS]
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
            count = len(_handlers(fh.read()))
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


# ── ISS-020/021 positional-arg regression guard ─────────────────────────────
#
# test_every_data_binding_resolves (above) only proves a data-* binding's
# name resolves to SOME window function — it does not prove that function
# honours the calling convention it was bound under. selectShip / addModule /
# addDrone shipped in v1.1.0 still expecting real positional args
# (typeId, typeName, ...) even though the CSP dispatcher only ever calls a
# handler as `fn.call(el, event)` — one positional argument, the Event. Every
# other data-click handler in this file (toggleOnline, removeItem,
# addModuleFromBrowser, addImplant, ...) opens with a `this.dataset`
# prologue for exactly this reason; these three didn't, so clicking a search
# result resolved "typeId" to a PointerEvent instead of an SDE type id.

FITTING_TEMPLATES = [
    os.path.join(TEMPLATES, "fitting_tool.html"),
    os.path.join(TEMPLATES, "partials", "fitting_search_results.html"),
]

# A data-bound handler for the fitting tool is defined either inline in
# fitting_tool.html's own <script> block, or (for a handful of generic ones
# like closeModalOnBackdrop) in the shared dispatcher module.
_HANDLER_SOURCES = FITTING_TEMPLATES + [os.path.join(STATIC_JS, "actions.js")]

_FN_DEF = re.compile(
    r'(?:^|\n)[ \t]*function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*\{'
)
# A later top-level reassignment shadows the `function name(...)` declared
# above it, because a <script> block runs synchronously top-to-bottom on
# load — by the time a click can happen the reassignment has already taken
# effect (this is exactly how the addModule fit-restriction wrapper works:
# `var _origAddModule = addModule; addModule = function(...) {...}`).
# actions.js's `window.X = window.X || function (...) {...}` idempotent-init
# idiom is the same shape with an extra `window.X || ` in the middle.
_FN_REASSIGN = re.compile(
    r'(?:^|\n)[ \t]*(?:window\.)?([A-Za-z_$][\w$]*)\s*=\s*'
    r'(?:window\.\1\s*\|\|\s*)?function\s*\(([^)]*)\)\s*\{'
)


def _handler_definitions(body):
    """name -> (params, body_window) for the LAST definition of each name in
    `body` (see _FN_REASSIGN docstring for why "last" is the one that runs).

    body_window is the ~12 lines after the opening brace, not the true
    function body (finding the real matching `}` needs a parser, not a
    regex) — enough to hold the this.dataset prologue every compliant
    handler in this codebase puts right at the top.
    """
    defs = {}  # name -> (start_pos, params, window)
    for pattern in (_FN_DEF, _FN_REASSIGN):
        for m in pattern.finditer(body):
            name, params = m.group(1), m.group(2)
            window = "\n".join(body[m.end():].splitlines()[:12])
            if name not in defs or m.start() > defs[name][0]:
                defs[name] = (m.start(), params, window)
    return {name: (params, window) for name, (_, params, window) in defs.items()}


def test_fitting_data_handlers_read_dataset_not_positionals():
    """Every data-click/data-change/data-input handler reachable from the
    fitting templates must either take no positional parameters (reading
    `this`/`this.dataset` directly, e.g. searchShip's `this.value`) or read
    `this.dataset` near the top of its body as the documented fallback for
    programmatic callers (Browse panel, EFT import, saved-fit load).

    This can't verify a handler reads its dataset fields *correctly* — only
    that the fallback code path exists at all, so a handler with declared
    positional parameters and no this.dataset anywhere near its top is
    almost certainly still expecting the dispatcher to pass them, which it
    never will. Same tier of guarantee as test_every_data_binding_resolves,
    one level deeper.
    """
    binding_names = set()
    for path in FITTING_TEMPLATES:
        with open(path, encoding="utf-8") as fh:
            binding_names |= set(_BINDING.findall(fh.read()))
    binding_names.discard("hide")  # data-on-error's built-in shortcut, not a window fn

    definitions = {}
    for path in _HANDLER_SOURCES:
        with open(path, encoding="utf-8") as fh:
            definitions.update(_handler_definitions(fh.read()))

    violations = {}
    for name in sorted(binding_names):
        if name not in definitions:
            continue  # test_every_data_binding_resolves already reports this
        params, window = definitions[name]
        if params.strip() and "this.dataset" not in window:
            violations[name] = params.strip()

    assert not violations, (
        "data-click/data-change/data-input handlers with positional "
        "parameters but no this.dataset fallback — the dispatcher only ever "
        f"calls fn.call(el, event), so these resolve their args to the Event "
        f"object instead: {violations}"
    )


# Partials still allowed to ship a script element. This list may only ever
# SHRINK — a new entry means a new instance of a bug that has now bitten
# twice (every banner's dismiss button, then nineteen more fragments).
#
# fitting_stats.html: deferred, not exempt. It is held by concurrent work at
# the time of writing and migrating it here would collide; it comes out on
# its own change.
PARTIALS_WITH_SCRIPT_ALLOWED = {
    "app/templates/partials/fitting_stats.html",
}


def test_partials_carry_no_script_tags():
    """A fragment in app/templates/partials/ must carry markup only.

    These load via their OWN request, separate from the page that swaps them
    in. The CSP nonce is minted per request (app/middleware/csp_nonce.py), so
    a script element in a fragment carries a nonce that never matches the
    page's CSP header. htmx re-creates such an element on swap, as inline
    script, and the browser refuses it — silently, apart from a violation
    report. Whatever it implemented is dead code that still looks alive in
    the source.

    That is exactly how every banner's dismiss (x) button went dead while the
    banner itself stayed visible, shown by base.html's page-level
    applyDismissState(), which does run under the page's own nonce. The same
    thing had quietly happened to eighteen more fragments: chart panels that
    rendered an empty canvas, admin controls that did nothing, copy buttons
    that copied nothing.

    So the rule is the whole directory, not just the banners. Behaviour goes
    to the parent page's own nonced block, to static/js/actions.js, or to an
    after-swap hook scoped by target id — all of which execute as part of the
    page. Data the behaviour needs travels on data-* attributes.

    A fragment that needs a script element is a fragment whose behaviour has
    not been migrated yet, so this list is the migration's remaining work,
    not a set of exceptions.
    """
    offenders = []
    for path in _templates():
        rel = _rel(path)
        if "partials" not in rel.split("/"):
            continue
        if rel in PARTIALS_WITH_SCRIPT_ALLOWED:
            continue
        with open(path, encoding="utf-8") as fh:
            if "<script" in fh.read():
                offenders.append(rel)
    assert not offenders, (
        "partials must carry markup only — a script element here carries a "
        f"mismatched CSP nonce and is silently dead code: {sorted(offenders)}"
    )


def test_partial_script_allowlist_is_not_stale():
    """The allowlist has to keep shrinking. An entry that no longer ships a
    script element is one nobody remembered to delete, and a stale entry is
    how an allowlist quietly turns back into a loophole."""
    stale = []
    for rel in PARTIALS_WITH_SCRIPT_ALLOWED:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            stale.append(rel)
            continue
        with open(path, encoding="utf-8") as fh:
            if "<script" not in fh.read():
                stale.append(rel)
    assert not stale, (
        f"PARTIALS_WITH_SCRIPT_ALLOWED is out of date — remove: {sorted(stale)}"
    )


def test_base_disables_htmx_script_tag_execution():
    """base.html must set `htmx.config.allowScriptTags = false`.

    htmx re-creates any script element it finds in swapped-in content and
    appends it to the document, which makes it inline script. Under the
    enforcing policy such a script cannot carry the page's nonce — the
    fragment is a different request with a nonce of its own — so it was
    refused and did nothing. The refusal still costs a violation report
    each, and the admin Overview re-fetches itself every 10s: a stale
    session there swapped the whole landing page in on every tick and filed
    six reports a time, ~2.4k an hour from one idle tab.

    Left at htmx's default this silently comes back the moment anyone adds
    a script element to a fragment, so the setting is pinned here rather
    than trusted to stay.
    """
    with open(os.path.join(TEMPLATES, "base.html"), encoding="utf-8") as fh:
        body = fh.read()
    assert re.search(
        r"htmx\.config\.allowScriptTags\s*=\s*false", body
    ), "base.html no longer disables htmx's script-tag execution"


# ── the policy the conversion work was for ──────────────────────────────────

def test_policy_is_enforcing_and_script_src_has_no_unsafe_inline():
    """T-033's payload. `script-src` keeps the nonce and drops
    `'unsafe-inline'`, and the header enforces rather than reports.

    `style-src` deliberately keeps `'unsafe-inline'` — that is the T-032
    decision, not an oversight, and a nonce must never be added there (it
    would make browsers ignore `'unsafe-inline'` for styles and fire a report
    for every inline style attribute in the app; see the 2026-07-03 incident
    note in the middleware).
    """
    from starlette.testclient import TestClient

    import app.main as main

    with TestClient(main.app) as client:
        response = client.get("/", follow_redirects=False)
    header = response.headers.get("content-security-policy")
    assert header, "no enforcing CSP header"
    assert "content-security-policy-report-only" not in response.headers

    script_src = next(d for d in header.split(";") if d.strip().startswith("script-src"))
    assert "'unsafe-inline'" not in script_src, script_src
    assert "'nonce-" in script_src, script_src

    style_src = next(d for d in header.split(";") if d.strip().startswith("style-src"))
    assert "'unsafe-inline'" in style_src, style_src
    assert "'nonce-" not in style_src, style_src


def test_every_template_script_block_carries_the_nonce():
    """With `'unsafe-inline'` gone, a `<script>` without the nonce no longer
    runs. Catch a missing one here rather than in a browser."""
    bare = {}
    for path in _templates():
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        for tag in re.findall(r"<script\b[^>]*>", body):
            # Both inline blocks and src= tags need it: the nonce is what
            # script-src matches on now, for either shape.
            if "nonce=" not in tag:
                bare.setdefault(_rel(path), []).append(tag[:80])
    assert not bare, f"<script> tags with no nonce — these will not run: {bare}"
