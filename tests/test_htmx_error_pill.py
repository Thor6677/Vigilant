"""base.html's ISS-007 handlers: where a failed request's "couldn't load" pill goes.

ISS-007 replaces a failed panel's contents with a small pill, so a lazy-loaded
panel never sits on its loading filler forever. The pill used to go into
evt.detail.elt, always. A request made with htmx.ajax() has no element of its
own and htmx fires its events on <body>, so every failed htmx.ajax() call
replaced the whole page (ISS-125). On /admin that is the section refresh: during
an in-app update's restart gap it destroyed #updater-panel and its poll, and a
403 blanked the page instead of showing ISS-038's "no longer has admin access".

The handlers now put the pill in the element that made the request, or, for a
request fired on <body> or <html>, in its swap target; honour
data-htmx-no-error="1" on that element and on a target the pill would
overwrite; and never write into <body> or <html>.

Each case runs the script cut from a rendered page under node (tests/_htmx_error.py).
"""
import pytest

from tests._htmx_error import admin_page, attrs_of, iss007_script, run_error_handlers

OPT_OUT = {"data-htmx-no-error": "1"}

# A page in miniature. "html" and "body" are added by the harness.
DOM = {
    # The admin page's section container and the updater panel inside it.
    "admin-content": {"attrs": {"hx-swap": "innerHTML", **OPT_OUT}, "parent": "body"},
    "updater-panel": {"attrs": {"hx-target": "#updater-panel", **OPT_OUT}, "parent": "admin-content"},
    "update-form": {"attrs": {"hx-post": "/admin/update", "hx-target": "#updater-panel"},
                    "parent": "updater-panel"},
    # An ordinary lazy-loaded panel, and one that opted out.
    "panel": {"attrs": {"hx-get": "/panel", "hx-trigger": "load"}, "parent": "body"},
    "quiet-panel": {"attrs": {"hx-get": "/quiet", "hx-trigger": "load", **OPT_OUT}, "parent": "body"},
    # An element whose request swaps into an opted-out element inside it.
    "wrapper": {"attrs": {"hx-get": "/w", "hx-target": "#guarded"}, "parent": "body"},
    "guarded": {"attrs": OPT_OUT, "parent": "wrapper"},
}

# (name, elt, target, element expected to get the pill or None)
CASES = [
    # (a) htmx.ajax() into an opted-out container: the admin section refresh.
    ("ajax_into_opted_out_container", "body", "admin-content", None),
    # (b) htmx.ajax() into an ordinary container: the pill goes there, not the page.
    ("ajax_into_plain_container", "body", "panel", "panel"),
    # (c) an ordinary hx-* element: unchanged, it gets the pill.
    ("plain_element", "panel", "panel", "panel"),
    # (d) an hx-* element that opted out: unchanged, untouched.
    ("opted_out_element", "quiet-panel", "quiet-panel", None),
    # The same as (a) and (b) when the event comes from <html>.
    ("document_element_into_plain_container", "html", "panel", "panel"),
    ("document_element_into_opted_out_container", "html", "admin-content", None),
    # htmx.ajax() straight into the updater panel.
    ("ajax_into_updater_panel", "body", "updater-panel", None),
    # Nothing to put a pill in: the page itself is never replaced.
    ("ajax_with_no_target", "body", None, None),
    ("ajax_into_body", "body", "body", None),
    # A form inside an opted-out target: unchanged, the form gets the pill and
    # the panel around it is left alone.
    ("form_inside_opted_out_target", "update-form", "updater-panel", "update-form"),
    # An element whose pill would overwrite its opted-out target.
    ("element_containing_opted_out_target", "wrapper", "guarded", None),
]

PILL_TEXT = {"responseError": "couldn't load (HTTP 502) — refresh to retry",
             "sendError": "network error — refresh to retry"}


@pytest.fixture(scope="module")
def page():
    return admin_page()


@pytest.fixture(scope="module")
def results(page):
    events = [{"type": kind, "elt": elt, "target": target, "status": 502}
              for kind in ("responseError", "sendError")
              for _, elt, target, _ in CASES]
    out = run_error_handlers(iss007_script(page), DOM, events)
    names = [(kind, name) for kind in ("responseError", "sendError") for name, *_ in CASES]
    return dict(zip(names, out))


@pytest.mark.parametrize("kind", ["responseError", "sendError"])
@pytest.mark.parametrize("name,elt,target,expect", CASES, ids=[c[0] for c in CASES])
def test_where_the_pill_goes(results, kind, name, elt, target, expect):
    writes = results[(kind, name)]
    assert "body" not in writes and "html" not in writes, "the page was replaced"
    if expect is None:
        assert writes == {}
    else:
        assert list(writes) == [expect]
        assert PILL_TEXT[kind] in writes[expect]


def test_the_pill_markup_is_unchanged(results):
    """The pill itself is ISS-007's, byte for byte: only where it goes changed."""
    assert results[("responseError", "plain_element")]["panel"] == (
        '<div style="padding:0.3rem 0.6rem;font-size:10px;color:var(--muted);'
        'border:1px solid var(--border);background:var(--surface);display:inline-block;">'
        "couldn't load (HTTP 502) — refresh to retry</div>")
    assert results[("sendError", "plain_element")]["panel"] == (
        '<div style="padding:0.3rem 0.6rem;font-size:10px;color:var(--muted);'
        'border:1px solid var(--border);background:var(--surface);display:inline-block;">'
        "network error — refresh to retry</div>")


@pytest.mark.parametrize("status", [403, 401])
def test_a_refused_admin_section_refresh_is_left_to_iss_038(page, status):
    """The admin page as rendered: #admin-content is refreshed with
    htmx.ajax(), so a refusal arrives with elt = <body>. It must leave the
    page, and #admin-content, to admin.html's htmx:afterRequest handler
    (ISS-038), which runs after this one and says the session has expired or
    the account is no longer an admin. Before ISS-125 the pill replaced the
    whole page first. The restart gap (502, no response) is pinned in
    tests/test_updater_panel.py."""
    assert "htmx.ajax('GET', '/admin/section/' + currentSection, '#admin-content')" in page
    dom = {"admin-content": {"attrs": attrs_of(page, "admin-content"), "parent": "body"}}
    events = [{"type": "responseError", "elt": "body", "target": "admin-content", "status": status}]
    assert run_error_handlers(iss007_script(page), dom, events) == [{}]
