"""The panel's markup contract — chiefly the restart gap.

Every assertion here is about something that is invisible on a fast machine and
silent in production: a poll that stops, an error handler that eats the panel, a
confirmation that never fires, a handler name that resolves to nothing.
"""
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader

from app.ops import update_schedule
from app.ops.updater import BUSY, IDLE, INTERRUPTED

PANEL = Path("app/templates/partials/updater_panel.html")
ACTIONS = Path("static/js/actions.js")
BASE = Path("app/templates/base.html")
OVERVIEW = Path("app/templates/partials/admin_overview.html")
UPDATES_TAB = Path("app/templates/partials/admin_updates.html")
# First element inside #updater-panel. Everything before it is the panel's own
# opening tag, which is where the poll and its opt-out must live.
FIRST_CHILD = '<div class="b-panel upd-status">'
ADMIN_TPL = Path("app/templates/admin.html")

_TEMPLATES = os.path.join(os.path.dirname(__file__), "..", "app", "templates")


@pytest.fixture(scope="module")
def panel():
    return PANEL.read_text()


@pytest.fixture(scope="module")
def actions():
    return ACTIONS.read_text()


def _render_finished(status, **overrides):
    """Render the panel as it looks right after a run has reached a terminal
    state: not polling, not busy, a status dict with log_tail. Mirrors the
    context app/routes/admin.py:_updater_context builds, trimmed to what this
    template actually reads.

    Note there is no StrictUndefined here, so a key this helper forgets renders
    as empty and falsy rather than raising. Assertions below therefore check
    rendered CONTENT, never the absence of an error.
    """
    env = Environment(loader=FileSystemLoader(_TEMPLATES), autoescape=True)
    tmpl = env.get_template("partials/updater_panel.html")
    context = dict(
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
        # Sidecar version skew. The quiet defaults are the normal state: the
        # sidecar is on the same release as the app and is not mid-handoff, so
        # neither the neutral "upgrading itself" line nor the lag warning
        # renders. Kept here for the same reason the other sections' keys are —
        # the template reads them unconditionally.
        updater_version=status.get("to_tag"),
        updater_lagging=False,
        self_update=None,
        self_update_in_flight=False,
        IDLE=IDLE,
        BUSY=BUSY,
        INTERRUPTED=INTERRUPTED,
        **_schedule_defaults(),
    )
    context.update(overrides)
    return tmpl.render(**context)


def _schedule_defaults():
    """The scheduling half of the context, in its shipped-off state: policy
    disabled, nothing pending. Mirrors the KEYS of app/routes/admin.py:
    _schedule_context, with UpdatePolicy's column defaults as the values.

    A plain namespace rather than an UpdatePolicy(): SQLAlchemy applies column
    defaults at flush, so an unflushed instance reads None for every field and
    would render a panel no real install can produce.

    It lives here because the panel is ONE template. The scheduling section
    dereferences `policy.enabled` unconditionally, so a standalone render that
    omits it raises UndefinedError before reaching the markup under test — which
    is how four log-preservation tests broke the moment scheduling landed on top
    of them, with nothing wrong in either change.
    """
    return dict(
        policy=SimpleNamespace(enabled=False, weekday=6, local_time="04:00",
                               timezone="UTC", patch_only=True,
                               paused_reason=None),
        pending_schedule=None,
        next_window=None,
        weekdays=list(enumerate(update_schedule.WEEKDAYS)),
        schedule_error=None,
        schedule_notice=None,
        grace_hours=update_schedule.GRACE_SECONDS // 3600,
        open_form=None,
        policy_form=dict(enabled=False, weekday=6, local_time="04:00",
                         timezone="UTC", patch_only=True),
        schedule_form=dict(run_at="", timezone="UTC"),
        notify=_notify_defaults(),
        push_configured=False,
        run_history=[],
        skipped_release=None,
    )


def _notify_defaults(**overrides):
    """app/ops/update_reports.py:notify_view() for a fresh install: no push
    channel of any kind, nothing delivered yet."""
    view = dict(discord_webhook_set=False, discord_on=False, discord_policy="all",
                discord_last=None, webhook_set=False, webhook_shown="",
                webhook_format="json", webhook_policy="all", webhook_last=None)
    view.update(overrides)
    return view


def test_schedule_defaults_cover_every_key_the_route_supplies():
    """The helper above is a hand-kept mirror, and a mirror drifts. If
    _schedule_context grows a key the template then reads, the standalone
    renders fail with an UndefinedError that names the variable but not the
    cause; this names the cause."""
    src = Path("app/routes/admin.py").read_text()
    body = src.split("async def _schedule_context", 1)[1].split("\nasync def ", 1)[0]
    returned = set(re.findall(r'^\s{8}"([a-z_]+)":', body, flags=re.M))
    assert returned, "could not find _schedule_context's returned keys"
    assert returned == set(_schedule_defaults())


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
    opening = panel[panel.index('<div id="updater-panel"'):panel.index(FIRST_CHILD)]
    assert "hx-trigger" in opening
    assert 'data-htmx-no-error="1"' in opening


def test_the_polling_element_is_the_swap_target(panel):
    """hx-swap replaces the target, so the poll must live on the element that
    gets replaced — htmx re-initialises the replacement and the loop continues.
    On an inner element the first swap would silently end the loop."""
    opening = panel[panel.index('<div id="updater-panel"'):panel.index(FIRST_CHILD)]
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

def _form_for(panel: str, action: str) -> str:
    """The markup of the form posting to `action`."""
    i = panel.index(f'hx-post="{action}"')
    start = panel.rindex("<form", 0, i)
    return panel[start:panel.index("</form>", i)]


DESTRUCTIVE = ["/admin/update", "/admin/rollback", "/admin/update/policy"]


@pytest.mark.parametrize("action", DESTRUCTIVE)
def test_every_destructive_form_confirms(action, panel):
    """Anything that can restart the app — now or on a schedule — asks first."""
    assert "hx-confirm=" in _form_for(panel, action)


@pytest.mark.parametrize("action", ["/admin/update", "/admin/rollback"])
def test_immediate_actions_mention_the_outage(action, panel):
    """The spec requires stating plainly that this restarts the app."""
    msg = re.search(r'hx-confirm="([^"]+)"', _form_for(panel, action)).group(1)
    assert "unavailable" in msg.lower(), msg


def test_the_policy_confirmation_names_what_is_actually_being_agreed(panel):
    """Enabling a policy is not an outage now — it is consenting to unattended
    ones later, including the case the health check cannot catch. Saying "the
    site will be unavailable for 30-60 seconds" here would be wrong."""
    msg = re.search(r'hx-confirm="([^"]+)"', _form_for(panel, "/admin/update/policy")).group(1)
    assert "automatically" in msg.lower()
    assert "health check" in msg.lower()


def test_cancelling_a_schedule_needs_no_confirmation(panel):
    """Cancelling is the safe direction. A confirmation there is just friction."""
    assert "hx-confirm=" not in _form_for(panel, "/admin/update/schedule/cancel")


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


# ── Sidecar version skew ─────────────────────────────────────────────────────
#
# The sidecar upgrades itself after a successful in-app update, but never on a
# timer and never at startup, so a CLI deploy still leaves it behind. Skew that
# nobody is told about is how a sidecar fix sat undelivered in production; this
# section is the telling.

MANUAL_RECREATE = "docker compose --profile updater up -d updater"


def _self_update(state, target="v1.2.3", error=None):
    return {"state": state, "target": target, "error": error,
            "at": "2026-09-21T10:00:00Z"}


def test_a_lagging_sidecar_names_both_versions_and_the_command():
    html = _render_finished(_finished_status(),
                            updater_lagging=True,
                            updater_version="v1.2.2",
                            current_tag="v1.2.3")
    assert "v1.2.2" in html and "v1.2.3" in html
    assert MANUAL_RECREATE in html
    assert "install directory" in html


def test_a_lagging_sidecar_does_not_disable_the_buttons():
    """Updating is how a lagging sidecar heals itself. Disabling the controls
    would wedge the one path out of the skew."""
    html = _render_finished(_finished_status(),
                            updater_lagging=True,
                            updater_version="v1.2.2",
                            current_tag="v1.2.3",
                            update_available=True,
                            latest_tag="v1.2.4",
                            targets=["v1.2.1"])
    assert "Update to v1.2.4" in html
    assert "Roll back" in html
    assert "disabled" not in html


def test_a_lagging_sidecar_adds_no_new_form_or_input():
    """Every form in the panel is pinned by the tests above, and CSP forbids
    inline handlers — so the remedy has to be text the operator copies, not
    another control.

    Measured against the same render without the skew rather than as absolute
    zeroes: the scheduling section's policy form renders in every idle view,
    so the question is only whether the notice itself adds anything."""
    quiet = _render_finished(_finished_status(),
                             updater_version="v1.2.3",
                             current_tag="v1.2.3")
    html = _render_finished(_finished_status(),
                            updater_lagging=True,
                            updater_version="v1.2.2",
                            current_tag="v1.2.3")
    assert "install directory" in html       # the notice did render
    for needle in ("<form", "<input", "<select", "<button", "hx-post"):
        assert html.count(needle) == quiet.count(needle), needle


def test_mid_self_update_is_neutral_and_says_nothing_about_lag():
    """The skew is about to fix itself. Telling the operator to run a command
    by hand at that moment would be actively wrong."""
    for state in ("pulling", "handed_off"):
        html = _render_finished(_finished_status(),
                                updater_lagging=True,
                                updater_version="v1.2.2",
                                current_tag="v1.2.3",
                                self_update=_self_update(state),
                                self_update_in_flight=True)
        assert "upgrading itself" in html
        assert "v1.2.3" in html
        assert MANUAL_RECREATE not in html


def test_a_failed_self_update_shows_its_reason_with_the_warning():
    html = _render_finished(_finished_status(),
                            updater_lagging=True,
                            updater_version="v1.2.2",
                            current_tag="v1.2.3",
                            self_update=_self_update(
                                "failed", error="manifest unknown"))
    assert MANUAL_RECREATE in html
    assert "manifest unknown" in html


def test_a_newer_sidecar_than_the_app_warns_about_nothing():
    """The post-rollback state. Forward-only self-update means the sidecar
    deliberately stayed put while the app went back, and newer-sidecar with
    older-app is a supported combination."""
    html = _render_finished(_finished_status(),
                            updater_lagging=False,
                            updater_version="v1.2.3",
                            current_tag="v1.2.2",
                            self_update=_self_update("done"))
    assert MANUAL_RECREATE not in html
    assert "upgrading itself" not in html
    assert "still running" not in html


def test_a_legacy_heartbeat_renders_exactly_as_it_did_before():
    """A sidecar too old to publish `self_update` at all. Nothing about the
    skew section may appear, and the rest of the panel is untouched."""
    html = _render_finished(_finished_status())
    assert MANUAL_RECREATE not in html
    assert "upgrading itself" not in html
    assert "succeeded" in html


def test_the_skew_notice_survives_a_run_in_progress():
    """It is true regardless of run state, and the BUSY branch is exactly where
    an operator watching a CLI-triggered restart would be looking."""
    html = _render_finished(_finished_status(),
                            run_state=BUSY,
                            polling=True,
                            updater_lagging=True,
                            updater_version="v1.2.2",
                            current_tag="v1.2.3")
    assert MANUAL_RECREATE in html
    assert "in progress" in html


def test_the_skew_notice_has_no_inline_handlers():
    html = _render_finished(_finished_status(),
                            updater_lagging=True,
                            updater_version="v1.2.2",
                            current_tag="v1.2.3")
    assert not re.search(r"\son(click|change|submit|input|load|error)=", html)


# ── Wiring ───────────────────────────────────────────────────────────────────

def test_panel_is_rendered_inline_not_lazily_fetched():
    """A placeholder with hx-trigger="load" flashes, because this section
    re-fetches itself every 10s.

    Each refresh swaps in an EMPTY placeholder and the panel only reappears a
    round-trip later, so the operator sees it pop in and out every few seconds.
    Reported from the browser 2026-09-14. Rendering the include inline means the
    panel arrives already built, with the section.
    """
    src = UPDATES_TAB.read_text()
    assert 'include "partials/updater_panel.html"' in src
    assert 'hx-get="/admin/update/status"' not in src, \
        "lazily fetching the panel here makes it flash on every section refresh"


def test_overview_no_longer_carries_update_ui():
    """T-061: the panel moved to its own tab, and Overview's second "Updates"
    panel (Running / Latest / Last checked) was folded into it. Overview keeps
    only the version tile, which links to the tab."""
    src = OVERVIEW.read_text()
    assert "updater_panel.html" not in src
    assert "Last Checked" not in src
    assert 'href="/admin?tab=updates"' in src


def test_updates_tab_auto_refreshes():
    """The panel only polls while a run is in flight; the section refresh is
    what notices a new heartbeat, release or report while it is idle."""
    src = ADMIN_TPL.read_text()
    refresh = src[src.index("var refreshSections"):]
    refresh = refresh[:refresh.index(";")]
    assert "'updates'" in refresh


def test_overview_section_still_auto_refreshes():
    """The inline rendering above is only necessary because this section
    re-fetches itself. If that ever stops, revisit the reasoning rather than
    assuming it still holds."""
    assert "setInterval" in ADMIN_TPL.read_text()


def test_rollback_targets_are_a_closed_list(panel):
    """A free-text tag field would invite typing any ref; the select can only
    offer releases this host has actually run. Scoped to the rollback form —
    the schedule form legitimately takes typed text for a time and a zone."""
    form = _form_for(panel, "/admin/rollback")
    assert "<select" in form
    assert 'type="text"' not in form


def test_no_form_lets_a_tag_be_typed(panel):
    """Applies to the schedule form too: it carries the tag as a hidden field
    taken from the release the operator was shown, never as free text."""
    for action in ["/admin/update", "/admin/rollback", "/admin/update/schedule"]:
        form = _form_for(panel, action)
        assert not re.search(r'<input[^>]*name="tag"[^>]*type="text"', form), action
        assert not re.search(r'type="text"[^>]*name="tag"', form), action


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


# ── The refresh vs the scheduling forms ──────────────────────────────────────
#
# The scheduling forms live inside #admin-content, which re-renders every 10s.
# Nothing in a Python test can watch a browser lose typed text, so what is
# pinned here is the wiring that prevents it.

@pytest.mark.parametrize("cls", ["updater-schedule-form", "updater-policy", "updater-notify"])
def test_scheduling_details_record_the_operators_touch(cls, panel):
    tag = panel[panel.index(f'<details class="{cls}"'):]
    tag = tag[:tag.index(">") + 1]
    for attr in ("data-click", "data-input", "data-change"):
        assert f'{attr}="updaterHoldRefresh"' in tag, (cls, attr)


def test_every_refresh_timer_asks_before_re_rendering():
    """Both setInterval sites — the initial Overview one and switchTab's — must
    go through the hold check. One that bypassed it would wipe the form on
    exactly the tab where it lives."""
    src = ADMIN.read_text()
    assert src.count("setInterval(") == 2
    assert src.count("setInterval(refreshAdminSection,") == 2
    fn = src[src.index("function refreshAdminSection"):]
    fn = fn[:fn.index("\n}")]
    assert fn.index("updaterRefreshHeld") < fn.index("htmx.ajax")


def test_only_a_form_field_holds_the_refresh(actions):
    """Clicking a <summary> to look is not editing; freezing the whole
    Overview for it was too heavy."""
    fn = actions[actions.index("window.updaterHoldRefresh"):]
    fn = fn[:fn.index("\n    };")]
    assert "INPUT|SELECT|TEXTAREA" in fn
    assert fn.index("tagName") < fn.index("setAttribute")


def test_the_report_banner_slot_survives_a_failed_poll():
    """It polls through the app's own restart, when a failure is expected."""
    base = BASE.read_text()
    slot = base[base.index('<div id="update-report-slot"'):]
    slot = slot[:slot.index(">") + 1]
    assert 'data-htmx-no-error="1"' in slot


def test_the_hold_lapses_on_its_own(actions):
    """An edit abandoned in a background tab must not freeze the section, and
    the update panel inside it, for good. Focus is not a reason to hold: it
    stays where it was in a forgotten tab."""
    fn = actions[actions.index("window.updaterRefreshHeld"):]
    fn = fn[:fn.index("\n    };")]
    assert "UPDATER_HOLD_MS" in fn and "Date.now()" in fn
    assert "activeElement" not in fn
    assert "UPDATER_HOLD_MS = 2 * 60 * 1000" in actions


def test_panel_refusals_are_swapped_in_not_turned_into_a_pill(actions):
    """htmx 1.x does not swap a 4xx, and base.html's ISS-007 handler then
    replaces the submitting form with "couldn't load". The panel answers bad
    input and a busy updater with a re-render that says why, so those two
    statuses must reach the page — and only for the panel."""
    i = actions.index("htmx:beforeSwap")
    hook = actions[i:actions.index("});", i)]
    assert "updater-panel" in hook
    assert "400" in hook and "409" in hook
    assert "shouldSwap = true" in hook
    assert "isError = false" in hook


# ── The bell ─────────────────────────────────────────────────────────────────

NOTIFICATIONS = Path("static/js/notifications.js")


def test_the_bell_knows_the_update_report_type():
    """Without a default pref the bell treats an unknown type as enabled but
    unlabelled; without a settings box a muted type can never be unmuted."""
    js = NOTIFICATIONS.read_text()
    prefs = js[js.index("var DEFAULT_PREFS"):js.index("};", js.index("var DEFAULT_PREFS"))]
    labels = js[js.index("var TYPE_LABELS"):js.index("};", js.index("var TYPE_LABELS"))]
    assert "auto_update: true" in prefs
    assert "auto_update:" in labels
    assert 'data-notif-type="auto_update"' in BASE.read_text()


def test_the_report_banner_slot_is_not_part_of_the_local_dismiss_state():
    """Acknowledgement is server-side. applyDismissState() hides elements by
    localStorage, so the report banner must match none of its selectors."""
    env = Environment(loader=FileSystemLoader(_TEMPLATES), autoescape=True)
    banner = env.get_template("partials/update_report_banner.html").render(reports=[
        dict(id=1, kind="automatic", outcome=o, problem=o != "succeeded",
             from_tag="v1.2.0", to_tag="v1.3.0", detail="d", headline="h",
             at="2026-09-13 04:30", acknowledged=False, deliveries={})
        for o in ("succeeded", "failed", "reverted", "skipped")])
    assert banner.count("update-report-banner") == 4
    assert "data-alert-id" not in banner
    assert 'id="update-banner"' not in banner
    assert "<script" not in banner
    base = BASE.read_text()
    assert 'hx-get="/status/update-reports"' in base

