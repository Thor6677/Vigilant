"""The panel's markup contract — chiefly the restart gap.

Every assertion here is about something that is invisible on a fast machine and
silent in production: a poll that stops, an error handler that eats the panel, a
confirmation that never fires, a handler name that resolves to nothing.
"""
import os
import re
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

from app.ops.updater import BUSY, IDLE, INTERRUPTED

PANEL = Path("app/templates/partials/updater_panel.html")
ACTIONS = Path("static/js/actions.js")
BASE = Path("app/templates/base.html")
OVERVIEW = Path("app/templates/partials/admin_overview.html")
ADMIN_TPL = Path("app/templates/admin.html")

_TEMPLATES = os.path.join(os.path.dirname(__file__), "..", "app", "templates")


@pytest.fixture(scope="module")
def panel():
    return PANEL.read_text()


@pytest.fixture(scope="module")
def actions():
    return ACTIONS.read_text()


def _render_finished(status):
    """Render the panel as it looks right after a run has reached a terminal
    state: not polling, not busy, a status dict with log_tail. Mirrors the
    context app/routes/admin.py:_updater_context builds, trimmed to what this
    template actually reads.
    """
    env = Environment(loader=FileSystemLoader(_TEMPLATES), autoescape=True)
    tmpl = env.get_template("partials/updater_panel.html")
    return tmpl.render(
        available=True,
        error=None,
        checks={"socket": "ok"},
        run_state=IDLE,
        awaiting_pickup=False,
        polling=False,
        status=status,
        current_tag=status.get("to_tag"),
        latest_tag=status.get("to_tag"),
        update_available=False,
        targets=[],
        IDLE=IDLE,
        BUSY=BUSY,
        INTERRUPTED=INTERRUPTED,
    )


def _finished_status(**overrides):
    status = {
        "id": "a1b2c3d4-0000-4000-8000-000000000001",
        "action": "update",
        "state": "success",
        "step": "done",
        "from_tag": "v1.2.0",
        "to_tag": "v1.2.1",
        "message": "",
        "error": None,
        "reverted_to": None,
        "log_tail": ["Pulling image...", "Recreating app...", "Healthy."],
        "started_at": "2026-09-21T10:00:00Z",
        "finished_at": "2026-09-21T10:01:00Z",
    }
    status.update(overrides)
    return status


# ── The restart gap ──────────────────────────────────────────────────────────

def test_poll_and_no_error_opt_out_are_on_the_same_element(panel):
    """base.html's ISS-007 handler reads evt.detail.elt — the element that
    TRIGGERED the request. If data-htmx-no-error sits anywhere other than the
    polling element, every failed poll during the app's own restart overwrites
    the panel with a "couldn't load" pill, destroying the hx-trigger and
    stranding the operator on an error.
    """
    opening = panel[panel.index('<div id="updater-panel"'):panel.index("<div class=\"b-card-head\">")]
    assert "hx-trigger" in opening
    assert 'data-htmx-no-error="1"' in opening


def test_the_polling_element_is_the_swap_target(panel):
    """hx-swap replaces the target, so the poll must live on the element that
    gets replaced — htmx re-initialises the replacement and the loop continues.
    On an inner element the first swap would silently end the loop."""
    opening = panel[:panel.index("<div class=\"b-card-head\">")]
    assert 'hx-target="#updater-panel"' in opening
    assert 'hx-swap="outerHTML"' in opening


def test_restart_banner_exists_and_starts_hidden(panel):
    assert 'id="updater-restarting"' in panel
    banner = panel[panel.index('id="updater-restarting"'):]
    assert "hidden" in banner[:200]


def test_restart_watcher_toggles_hidden_not_inner_html(actions):
    """Replacing the panel's markup on error would destroy the hx-trigger, so
    the page would never notice the app coming back — the exact failure this
    handling exists to prevent."""
    assert "updaterRestartBanner" in actions
    watcher = actions[actions.index("function updaterRestartBanner"):]
    body = watcher[:watcher.index("}")]
    assert "hidden" in body
    assert "innerHTML" not in body


def test_restart_watcher_listens_for_both_failure_modes(actions):
    """sendError is the container being gone; responseError is the edge proxy
    answering 502/503 while it restarts. Both are the same restart."""
    assert "htmx:sendError" in actions
    assert "htmx:responseError" in actions


def test_base_error_handler_still_honours_the_opt_out():
    """Pins the contract this panel depends on. If ISS-007's handler is ever
    changed to read the target instead of the triggering element, this fails
    here rather than silently in production."""
    base = BASE.read_text()
    assert "data-htmx-no-error" in base
    assert "evt.detail.elt" in base or "detail && evt.detail.elt" in base


# ── Confirmation ─────────────────────────────────────────────────────────────

def test_both_destructive_forms_confirm(panel):
    """The spec requires stating plainly that this restarts the app. Two forms,
    two confirmations."""
    assert panel.count("hx-confirm=") == 2


def test_confirmations_mention_the_outage(panel):
    for m in re.findall(r'hx-confirm="([^"]+)"', panel):
        assert "unavailable" in m.lower(), m


def test_confirm_uses_htmx_not_the_native_submit_dispatcher(panel):
    """The repo's data-confirm dispatcher listens for the native submit event.
    Racing htmx's own submit handling for the same event is a coin flip, so
    these forms use htmx's own hx-confirm, which it evaluates before issuing
    the request."""
    assert "data-confirm=" not in panel


# ── CSP ──────────────────────────────────────────────────────────────────────

def test_no_inline_event_handlers(panel):
    assert not re.search(r"\son(click|change|submit|input|load|error)=", panel)


def test_every_data_handler_resolves_to_a_real_function(panel, actions):
    """A data-click naming a function that does not exist fails silently — the
    dispatcher warns to the console and returns, so the button simply does
    nothing. Caught here rather than by a human clicking it in production.
    """
    for attr in ("data-click", "data-change", "data-submit", "data-input"):
        for name in re.findall(rf'{attr}="([^"]+)"', panel):
            assert f"window.{name}" in actions or f"function {name}" in actions, name


# ── Log collapse (hx-preserve) ──────────────────────────────────────────────
#
# Both refresh paths that hit this element — the 10s #admin-content innerHTML
# swap and the panel's own outerHTML swap — re-render the template from
# scratch. Without hx-preserve keyed by run id, an operator who opens "Output"
# on a finished run watches it collapse and loses their scroll position a few
# seconds later. See htmx's handlePreservedElements(), which matches by `id`
# against the live DOM before either swap clears its target.

def test_finished_run_renders_keyed_id_and_hx_preserve():
    html = _render_finished(_finished_status(id="deadbeef-1111-4000-8000-000000000001"))
    assert 'id="updater-log-deadbeef-1111-4000-8000-000000000001"' in html
    assert 'hx-preserve="true"' in html
    # Both attributes belong on the same <details> tag, not scattered elsewhere.
    details = html[html.index("<details"):html.index(">", html.index("<details")) + 1]
    assert 'id="updater-log-deadbeef-1111-4000-8000-000000000001"' in details
    assert 'hx-preserve="true"' in details


def test_missing_status_id_skips_preserve_instead_of_emitting_a_broken_id():
    """Older status.json files predate the `id` field, and the supervisor's own
    schema default is None. Either way this must fall back to a plain
    <details> rather than id="updater-log-" or id="updater-log-None"."""
    for missing_id in (None, ""):
        html = _render_finished(_finished_status(id=missing_id))
        assert "updater-log-" not in html
        assert "hx-preserve" not in html
        assert "<details>" in html


def test_status_without_an_id_key_at_all_also_skips_preserve():
    """Simulates a status.json written before the `id` field existed, where the
    key is absent rather than present-and-None."""
    status = _finished_status()
    del status["id"]
    html = _render_finished(status)
    assert "updater-log-" not in html
    assert "hx-preserve" not in html
    assert "<details>" in html


def test_different_run_ids_give_different_element_ids():
    html_a = _render_finished(_finished_status(id="run-aaaa"))
    html_b = _render_finished(_finished_status(id="run-bbbb"))
    assert 'id="updater-log-run-aaaa"' in html_a
    assert 'id="updater-log-run-bbbb"' in html_b
    assert "updater-log-run-bbbb" not in html_a
    assert "updater-log-run-aaaa" not in html_b


# ── Wiring ───────────────────────────────────────────────────────────────────

def test_panel_is_rendered_inline_not_lazily_fetched():
    """A placeholder with hx-trigger="load" flashes, because this section
    re-fetches itself every 10s.

    Each refresh swaps in an EMPTY placeholder and the panel only reappears a
    round-trip later, so the operator sees it pop in and out every few seconds.
    Reported from the browser 2026-09-14. Rendering the include inline means the
    panel arrives already built, with the section.
    """
    src = OVERVIEW.read_text()
    assert 'include "partials/updater_panel.html"' in src
    assert 'hx-get="/admin/update/status"' not in src, \
        "lazily fetching the panel here makes it flash on every section refresh"


def test_overview_section_still_auto_refreshes():
    """The inline rendering above is only necessary because this section
    re-fetches itself. If that ever stops, revisit the reasoning rather than
    assuming it still holds."""
    assert "setInterval" in ADMIN_TPL.read_text()


def test_rollback_targets_are_a_closed_list(panel):
    """A free-text tag field would invite typing any ref; the select can only
    offer releases this host has actually run."""
    assert "<select" in panel
    assert 'type="text"' not in panel


# ── The ancestor that can defeat all of the above ────────────────────────────

ADMIN = Path("app/templates/admin.html")


def test_admin_content_container_is_also_opted_out():
    """The panel's own opt-out is not sufficient.

    #admin-content re-fetches itself every 10s and the panel is swapped INSIDE
    it. During an update the app is down for 30-60s by design, so that refresh
    fails too — and without this opt-out ISS-007 replaces the container's
    innerHTML with a "couldn't load" pill, destroying #updater-panel, its
    hx-trigger and its restart banner. The page then never notices the app
    coming back: the exact failure Task 6 exists to prevent, defeated one DOM
    level up.

    The panel tests above read the panel file in isolation and structurally
    cannot see this, which is why it gets its own assertion here.
    """
    src = ADMIN.read_text()
    container = src[src.index('<div id="admin-content"'):]
    container = container[:container.index(">") + 1]
    assert 'data-htmx-no-error="1"' in container


def test_admin_content_still_auto_refreshes():
    """The opt-out is only justified because the container retries on its own —
    if the refresh loop is ever removed, the pill should come back."""
    src = ADMIN.read_text()
    assert "refreshSections" in src
    assert "setInterval" in src
