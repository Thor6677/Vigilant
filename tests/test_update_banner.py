"""The update banner must follow the site's established banner discipline.

Per the alert-banner pattern: markup starts display:none and an inline script
reveals it unless dismissed, with the GLOBAL htmx:afterSwap handler in base.html
re-applying dismiss state after every swap. Inverting this to visible-by-default
makes a dismissed banner flash back on every navigation.
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
        "banner must start hidden and be revealed by the dismiss-aware script"


def test_banner_is_keyed_by_tag():
    html = open("app/templates/partials/update_banner.html").read()
    assert "data-update-tag" in html, \
        "dismiss state must key on the release tag so a newer release un-dismisses"


def test_base_html_reapplies_dismiss_state_after_swap():
    base = open("app/templates/base.html").read()
    assert "update-banner" in base, "base.html must include the banner slot"
    assert "vigilant_dismissed_update" in base, \
        "the global htmx:afterSwap handler must re-apply update-banner dismiss state"
