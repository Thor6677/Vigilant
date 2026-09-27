"""T-078 / ISS-065: the "+ Add Account" (add-group-btn) handler.

Before the fix, this handler built the new group's header with a single
`section.innerHTML = '...' + name + '...'` call, interpolating the raw
`prompt()` return value straight into HTML with no escaping (ISS-065), and
the markup it produced was the pre-T-076 header shape (no explicit
`flex:none` on the chevron, count not pinned right with `margin-left:auto`).

This is a source-level check, in the spirit of tests/test_csp_inline_handlers.py
— there is no browser here to actually run the click handler and inspect the
resulting DOM, so the guarantee this test can make is: nowhere in the handler
does an interpolated string reach `.innerHTML`, and the DOM is instead built
with `document.createElement` + `.textContent`, mirroring the current
group-header partial's markup (chevron immediately before the name, both
`flex:none`; count pinned right via `margin-left:auto`).
"""
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")
DASHBOARD_HTML = os.path.join(ROOT, "app", "templates", "dashboard.html")


def _add_group_handler() -> str:
    with open(DASHBOARD_HTML) as f:
        source = f.read()
    start = source.index("if (addBtn) addBtn.addEventListener('click', function() {")
    # Balance on the function's own braces (starting at the `{` opened by
    # `function() {`) to pull out exactly this handler, not the rest of the
    # DOMContentLoaded listener around it.
    brace_start = source.index("{", start)
    depth = 0
    for i in range(brace_start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
    raise AssertionError("unbalanced add-group-btn handler")


def test_handler_never_writes_the_prompted_name_into_innerhtml():
    handler = _add_group_handler()
    assert ".innerHTML" not in handler, (
        "the add-group handler must never build markup via innerHTML — "
        "the prompt() name has to go through textContent/dataset instead"
    )
    # innerHTML's usual escapes — building markup with these still lets the
    # prompt() value reach the DOM as HTML instead of text.
    assert ".outerHTML" not in handler
    assert "insertAdjacentHTML" not in handler


def test_handler_builds_the_header_with_createelement_and_textcontent():
    handler = _add_group_handler()
    assert "document.createElement" in handler
    # The name must reach the DOM only via textContent/value/dataset
    # assignment, never string concatenation into markup.
    assert re.search(r"label\.textContent\s*=\s*name\b", handler)


def test_handler_mirrors_current_group_header_markup_shape():
    handler = _add_group_handler()
    # Chevron (dash-group-toggle) comes before the name, both explicitly
    # flex:none (T-076's fix for the header-layout regression), and the
    # count is pinned right with margin-left:auto — the current partial's
    # shape, not the pre-T-076 layout the old innerHTML string produced.
    toggle_idx = handler.index("dash-group-toggle")
    label_idx = handler.index("group-label")
    count_idx = handler.index("b-muted-sm")
    assert toggle_idx < label_idx < count_idx
    assert handler.count("flex:none") >= 3  # up, down and chevron buttons
    # Anchored to the count element's own style assignment, not just present
    # anywhere in the handler (a comment mentioning "margin-left:auto" would
    # satisfy a bare substring check without the element actually having it).
    assert re.search(r"count\.style\.cssText\s*=\s*'[^']*margin-left:auto[^']*'", handler)


def test_handler_carries_the_rename_title_and_collapsed_summary_span():
    """Every other `.group-label` gets `title="Click to rename"` and
    `cursor:pointer` from toggleEditMode()'s querySelectorAll pass when edit
    mode was entered — this group is created afterwards, so the handler has
    to set both directly. The (empty, hidden) `.dash-group-summary` span
    matches the partial's markup shape even though a brand-new group has
    nothing to summarize yet."""
    handler = _add_group_handler()
    assert re.search(r"label\.title\s*=\s*'Click to rename'", handler)
    assert "dash-group-summary" in handler


def test_handler_still_wires_drag_drop_and_group_order_save():
    handler = _add_group_handler()
    assert "Sortable.create" in handler
    assert "sortables.push(ns)" in handler
    assert "scheduleSave" in handler
