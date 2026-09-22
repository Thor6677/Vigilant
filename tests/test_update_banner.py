"""The update banner must follow the site's established banner discipline.

Per the alert-banner pattern: markup starts display:none, and base.html's
page-level applyDismissState() (running under the PAGE's CSP nonce) reveals it
unless dismissed, re-applying that state after every htmx swap. The dismiss
button dispatches to dismissUpdateBanner in static/js/actions.js rather than
carrying its own inline script — a fragment loaded via its own htmx request
gets a CSP nonce that never matches the page's, so an inline script here would
be silently blocked (see tests/test_csp_inline_handlers.py). Inverting the
hidden-by-default markup would make a dismissed banner flash back on every
navigation.
"""
import app.routes.status as status_mod


def test_banner_route_lives_on_the_status_router():
    """base.html loads this for anonymous visitors too, so it must NOT be an
    /admin/* route — that would look like an auth hole to anyone reading
    tests/test_route_auth_gating.py. /status/banner is the existing precedent."""
    assert hasattr(status_mod, "update_banner")


def test_banner_partial_starts_hidden():
    html = open("app/templates/partials/update_banner.html").read()
    assert "display:none" in html.replace(" ", ""), \
        "banner must start hidden and be revealed by applyDismissState()"


def test_banner_is_keyed_by_tag():
    html = open("app/templates/partials/update_banner.html").read()
    assert "data-update-tag" in html, \
        "dismiss state must key on the release tag so a newer release un-dismisses"


def test_banner_dismiss_uses_the_dispatcher_pattern():
    """No inline handler: tests/test_csp_inline_handlers.py forbids both a
    <script> in this fragment and an inline on*= attribute anywhere."""
    html = open("app/templates/partials/update_banner.html").read()
    assert 'data-click="dismissUpdateBanner"' in html, \
        "dismiss must go through static/js/actions.js, like every other banner"
    assert "<script" not in html, \
        "a fragment <script> carries a mismatched CSP nonce and never runs"


def test_banner_reuses_the_shared_alert_banner_markup():
    """Re-rendered as .structure-alert-banner (with an is-accent tone) so this
    banner lines up with the alert banners stacked above it in base.html,
    instead of the old one-off inline-styled monospace block."""
    html = open("app/templates/partials/update_banner.html").read()
    assert "structure-alert-banner" in html
    assert "is-accent" in html


def test_base_html_reapplies_dismiss_state_after_swap():
    base = open("app/templates/base.html").read()
    assert "update-banner" in base, "base.html must include the banner slot"
    assert "vigilant_dismissed_update" in base, \
        "the global htmx:afterSwap handler must re-apply update-banner dismiss state"
