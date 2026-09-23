"""A redirect answered to htmx must become `401 + HX-Redirect` (ISS-038).

Every route gates itself by redirecting to the login page. XHR follows that
redirect on its own, so htmx receives a 200 whose body is the login page and
swaps it into the request's target — on the admin Overview's 10 s poll, every
tick until the tab is closed. `app/middleware/htmx_redirect.py` rewrites the
redirect into the htmx-native form, which navigates the whole page instead.

The sweep at the bottom is the part that matters long-term: it walks every
literal GET route the app has and checks the rewrite against whatever the
route answers anonymously today, so a new gated route is covered the moment
it exists, with no allowlist to keep.
"""
import os
import re

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

import app.main as main
from app.middleware.htmx_redirect import HTMXRedirectMiddleware
from tests.test_route_auth_gating import (
    _client, _literal_get_routes, _session_cookie, admin_user_id, nonadmin_user_id,
)

HX = {"HX-Request": "true"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── the middleware on its own ───────────────────────────────────────────────

def _mini_app() -> TestClient:
    async def login_redirect(request):
        return RedirectResponse("/", status_code=303)

    async def permanent(request):
        return RedirectResponse("/elsewhere", status_code=301)

    async def own_hx_redirect(request):
        # A handler that has already decided where htmx should go.
        return Response(status_code=303, headers={"Location": "/a", "HX-Redirect": "/b"})

    async def own_hx_location(request):
        return Response(status_code=303, headers={"Location": "/a", "HX-Location": "/b"})

    async def no_location(request):
        return Response(status_code=302)

    async def redirect_with_baggage(request):
        r = RedirectResponse("/", status_code=303)
        r.set_cookie("crumb", "kept")
        r.headers["HX-Trigger"] = "signed-out"
        return r

    async def fragment(request):
        return PlainTextResponse("<div>fragment</div>")

    async def forbidden(request):
        return PlainTextResponse("no", status_code=403)

    app = Starlette(routes=[
        Route("/login-redirect", login_redirect),
        Route("/permanent", permanent),
        Route("/own-hx-redirect", own_hx_redirect),
        Route("/own-hx-location", own_hx_location),
        Route("/no-location", no_location),
        Route("/baggage", redirect_with_baggage),
        Route("/fragment", fragment),
        Route("/forbidden", forbidden),
    ])
    app.add_middleware(HTMXRedirectMiddleware)
    return TestClient(app, follow_redirects=False)


def test_htmx_redirect_is_rewritten_to_401_with_hx_redirect():
    r = _mini_app().get("/login-redirect", headers=HX)
    assert r.status_code == 401
    assert r.headers["HX-Redirect"] == "/"
    assert "location" not in r.headers, "the browser-level redirect must not survive"
    assert r.content == b"", "nothing for htmx to swap"
    assert r.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_every_redirect_status_is_rewritten(status):
    async def handler(request):
        return RedirectResponse("/x", status_code=status)

    app = Starlette(routes=[Route("/r", handler)])
    app.add_middleware(HTMXRedirectMiddleware)
    r = TestClient(app, follow_redirects=False).get("/r", headers=HX)
    assert (r.status_code, r.headers.get("HX-Redirect")) == (401, "/x")


def test_plain_navigation_is_untouched():
    """The route's own answer stands for a browser navigation."""
    r = _mini_app().get("/login-redirect")
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert "hx-redirect" not in r.headers


@pytest.mark.parametrize("value", ["false", "", "yes"])
def test_only_a_true_hx_request_header_counts(value):
    r = _mini_app().get("/login-redirect", headers={"HX-Request": value})
    assert r.status_code == 303


def test_a_handler_that_set_its_own_htmx_header_is_left_alone():
    c = _mini_app()
    r = c.get("/own-hx-redirect", headers=HX)
    assert (r.status_code, r.headers["HX-Redirect"]) == (303, "/b")
    r = c.get("/own-hx-location", headers=HX)
    assert (r.status_code, r.headers["HX-Location"]) == (303, "/b")


def test_the_rest_of_the_handlers_answer_survives():
    """Only the browser-level redirect and its body are replaced. A cookie the
    handler set, or an HX-Trigger it wanted fired, still reach the client —
    and an HTTPException's JSON `detail` body does not."""
    r = _mini_app().get("/baggage", headers=HX)
    assert r.status_code == 401
    assert r.cookies.get("crumb") == "kept"
    assert r.headers["HX-Trigger"] == "signed-out"
    assert r.content == b""
    assert r.headers["Content-Length"] == "0"
    assert "content-type" not in r.headers


def test_non_redirects_are_untouched():
    c = _mini_app()
    r = c.get("/fragment", headers=HX)
    assert (r.status_code, r.text) == (200, "<div>fragment</div>")
    r = c.get("/forbidden", headers=HX)
    assert r.status_code == 403
    assert "hx-redirect" not in r.headers
    r = c.get("/no-location", headers=HX)
    assert r.status_code == 302, "a redirect with nowhere to go is not ours to rewrite"


# ── the real app ────────────────────────────────────────────────────────────

def test_admin_section_poll_without_a_session():
    """The exact request the ISS-038 loop was making, ten times a minute."""
    r = _client().get("/admin/section/overview", headers=HX)
    assert r.status_code == 401
    assert r.headers["HX-Redirect"] == "/"
    assert r.content == b""
    # And the answer the browser gets on a plain navigation is what it was.
    r = _client().get("/admin/section/overview")
    assert (r.status_code, r.headers["location"]) == (303, "/")


def test_admin_section_poll_by_a_plain_user_is_a_403_either_way(nonadmin_user_id):
    """A role failure is not a redirect, so the middleware has no say in it —
    and the page's timer handler treats 403 the same as 401 (below)."""
    c = _client(user_id=nonadmin_user_id, role="user")
    assert c.get("/admin/section/overview", headers=HX).status_code == 403
    assert c.get("/admin/section/overview").status_code == 403


def test_admin_section_poll_by_an_admin_is_the_fragment(admin_user_id):
    c = _client(user_id=admin_user_id, role="admin")
    r = c.get("/admin/section/overview", headers=HX)
    assert r.status_code == 200
    assert "hx-redirect" not in r.headers
    assert "<html" not in r.text.lower(), "a fragment, not a page"


def test_every_gated_literal_get_route_answers_htmx_with_hx_redirect():
    """For every literal GET route: if an anonymous browser navigation is
    redirected, an anonymous htmx request to the same path must be told to
    navigate instead of being handed the redirect's destination to swap.

    Derived from the live route table rather than a list, so a route added
    tomorrow is covered the day it lands. Routes that answer anonymously with
    anything other than a redirect are outside this test's concern — the
    public allowlist in test_route_auth_gating.py is the place that argues
    about those.
    """
    client = TestClient(main.app, follow_redirects=False, raise_server_exceptions=False)
    wrong = []
    checked = 0
    for route in _literal_get_routes():
        plain = client.get(route.path)
        if plain.status_code not in (301, 302, 303, 307, 308):
            continue
        location = plain.headers.get("location")
        if not location:
            continue
        checked += 1
        hx = client.get(route.path, headers=HX)
        # Two separate requests: /auth/login's Location carries a fresh SSO
        # `state` each time, so compare where they point, not the query.
        ok = (
            hx.status_code == 401
            and hx.headers.get("HX-Redirect", "").split("?")[0] == location.split("?")[0]
            and "location" not in hx.headers
            and hx.content == b""
        )
        if not ok:
            wrong.append(f"{route.path}: {hx.status_code} {dict(hx.headers)}")
    assert checked > 50, f"only {checked} redirecting routes found — is the sweep still finding gates?"
    assert not wrong, (
        "Route(s) that redirect a browser but hand htmx something to swap:\n  "
        + "\n  ".join(wrong)
    )


# ── the page-level half ─────────────────────────────────────────────────────

def test_admin_page_stops_its_timer_on_401_or_403():
    """admin.html's poll must clear its interval on the first 401/403 rather
    than keep asking. The 401 normally never gets this far — HX-Redirect has
    already navigated the page — but a 403 (role revoked mid-session) has
    nothing else to stop it."""
    with open(os.path.join(ROOT, "app/templates/admin.html"), encoding="utf-8") as fh:
        body = fh.read()
    handler = re.search(
        r"addEventListener\('htmx:afterRequest',(.*?)\n\}\);", body, re.S
    )
    assert handler, "admin.html no longer listens for htmx:afterRequest"
    block = handler.group(1)
    assert "target.id !== 'admin-content'" in block, "must ignore the updater panel's own poll"
    assert re.search(r"status !== 401 && xhr\.status !== 403", block)
    assert "clearInterval(refreshTimer)" in block
    assert "sign in again" in block
