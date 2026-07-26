"""Guard against route handlers that forget to check for a session (T-047).

There is no auth middleware in this app. `app/main.py` installs request-timing,
CSP-nonce, CSRF and session middleware only — **every route gates itself**, with
~220 hand-written `request.session.get("user_id")` checks. That design makes a
per-route omission invisible: nothing fails, the page just answers strangers.

That is exactly how `/structure-timers/search/owners` shipped ungated. The
parent page correctly redirected without a session, but the HTMX partial behind
it did not, and it borrows a stored character's token to run an *authenticated*
ESI search — so any anonymous visitor could loop it and drain the instance-wide
ESI error/rate budget, degrading every sync on the deployment.

Why a test and not a `Depends(require_user)` dependency: a dependency only
protects the routes someone remembers to decorate, which is the same failure
mode one level up. Retrofitting all ~220 existing call sites is a far larger
change than this ticket, and mixing styles would leave the codebase less
legible, not more. So the gates stay inline in the style of their module, and
*this file* is the enforcement: a new ungated route fails the suite until its
author either gates it or consciously adds it to the allowlist below.

Scope note: only literal (non-parameterised) GET routes are swept. Routes with
path params would need placeholder substitution and mostly answer 404/422 for a
made-up id, which produces false passes; POST/DELETE hit CSRF before the auth
gate, so their status codes say nothing about authorization. Every hole found
in the T-047 audit was a literal GET, which is the shape this class of bug
takes: a partial that outlived the page it was written for.
"""
import base64
import inspect
import json
import os
import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import app.main as main


def _make_user(role: str) -> int:
    """Insert a user with `role` into the hermetic test DB and return its id."""
    import asyncio

    from app.db.models import AsyncSessionLocal, User, engine

    async def _insert() -> int:
        async with AsyncSessionLocal() as db:
            user = User(is_admin=role in ("admin", "manager"), role=role)
            db.add(user)
            await db.commit()
            await db.refresh(user)
            uid = user.id
        # Same reason as conftest.pytest_configure: every test drives its own
        # event loop, so a pooled connection pinned to this one is unusable
        # from the TestClient's loop.
        await engine.dispose()
        return uid

    return asyncio.run(_insert())


@pytest.fixture(scope="module")
def nonadmin_user_id() -> int:
    return _make_user("user")


@pytest.fixture(scope="module")
def admin_user_id() -> int:
    return _make_user("admin")


@pytest.fixture(scope="module")
def manager_user_id() -> int:
    return _make_user("manager")

# Idioms that count as "this handler makes an authorization decision". Loose on
# purpose: the test's job is to catch handlers with NO check at all, not to
# audit the quality of the check. `_is_admin` / `require_admin` are the two
# role-level helpers; the rest is the inline session pattern.
_GATE_IDIOM = re.compile(
    r"session\.get\(|request\.session|_require|require_|is_admin|role\s*=="
)

# Any reference to an ESI client. An ungated handler that reaches ESI spends
# the deployment's shared error/rate budget on behalf of an anonymous caller.
_ESI_CALL = re.compile(r"get_client|ESIClient|get_client_safe")

# Literal GET routes that are deliberately reachable without a session.
# Adding a path here is a decision, not a formality — it means "an anonymous
# stranger on the public internet may have this".
PUBLIC_LITERAL_GET = {
    # Infrastructure / auth entry points.
    "/healthz",                                 # uptime probe, must stay open
    "/auth/login",                              # the SSO entry point itself
    "/characters",                              # bare RedirectResponse to /dashboard
    "/dscan",                                   # bare redirect to /intel/dscan
    # Public EVE data, no user component, no authenticated ESI call.
    "/api/server-status",                       # Tranquility player count
    "/api/ambient/kills",                       # login-page ambient backdrop
    "/api/ambient/systems.json",                # login-page ambient backdrop
    "/dashboard/big-battle-banner",             # global battle feed, not per-user
    "/dashboard/recent-battles",                # global battle feed, not per-user
    "/intel/gatecheck/finder",                  # public gatecamp intel partial
    "/intel/gatecheck/systems",                 # ^ its SDE autocomplete
    # Local SDE lookups — static game data shipped with the app, no user data.
    "/industry/planetary/lookup/search",
    "/skill-plans/search/ships",
    "/skill-plans/search/skills",
    # The fitting tool is a public calculator: it renders and computes for
    # anonymous visitors by design. Its per-user surfaces (saved fits, character
    # import) gate separately and are not in this set. The `/tools/fitting` and
    # `/intel/gatecheck` pages themselves are absent here on purpose: they read
    # the session to personalise, so they carry a gate idiom and this allowlist
    # — which is only about routes with *no* check — does not apply to them.
    "/tools/fitting/browse/groups",
    "/tools/fitting/can-overheat",
    "/tools/fitting/check-fit",
    "/tools/fitting/search/charges",
    "/tools/fitting/search/drones",
    "/tools/fitting/search/implants",
    "/tools/fitting/search/modules",
    "/tools/fitting/search/ships",
    # Deliberately public utilities (T-047 asked for an explicit decision on
    # these rather than a default). Neither exposes user data nor makes an
    # authenticated outbound call. structure-age reads the operator's own
    # calibration table, which is derived from public EVERef data — if that is
    # ever reconsidered, this is the line to change.
    "/tools/discordtime",
    "/tools/structure-age",
    "/tools/structure-age/partial",
}


def _literal_get_routes() -> list[APIRoute]:
    return [
        r for r in main.app.routes
        if isinstance(r, APIRoute) and "{" not in r.path and "GET" in (r.methods or set())
    ]


def _source(route: APIRoute) -> str:
    try:
        return inspect.getsource(route.endpoint)
    except (OSError, TypeError):  # pragma: no cover — C-implemented endpoint
        return ""


def _ungated() -> list[APIRoute]:
    return [r for r in _literal_get_routes() if not _GATE_IDIOM.search(_source(r))]


def _session_cookie(**data) -> str:
    """Forge the signed session cookie Starlette's SessionMiddleware reads."""
    from itsdangerous import TimestampSigner

    signer = TimestampSigner(os.environ["SECRET_KEY"])
    payload = base64.b64encode(json.dumps(data).encode())
    return signer.sign(payload).decode()


def _client(**session) -> TestClient:
    client = TestClient(main.app, follow_redirects=False)
    if session:
        client.cookies.set("vigilant_session", _session_cookie(**session))
    return client


def test_no_ungated_route_makes_an_esi_call():
    """The strongest invariant here, and the one T-047 actually turned on.

    A route that skips the session check AND reaches ESI lets a stranger spend
    the deployment's shared ESI budget. At the time of writing exactly one
    route did this (`/structure-timers/search/owners`) and it is now gated.
    """
    offenders = [
        f"{r.path} ({r.endpoint.__module__}.{r.endpoint.__name__})"
        for r in _ungated() if _ESI_CALL.search(_source(r))
    ]
    assert not offenders, (
        "Route(s) with no session check that call ESI — an anonymous caller can "
        "burn the instance-wide ESI error/rate budget:\n  " + "\n  ".join(offenders)
    )


def test_every_ungated_literal_get_route_is_declared_public():
    """New ungated routes must be a conscious choice, not an oversight."""
    undeclared = sorted(
        f"{r.path} ({r.endpoint.__module__}.{r.endpoint.__name__})"
        for r in _ungated() if r.path not in PUBLIC_LITERAL_GET
    )
    assert not undeclared, (
        "Route(s) with no authorization check that are not declared public.\n"
        "Either add a session gate to the handler, or — if anonymous access is "
        "intended — add the path to PUBLIC_LITERAL_GET with a reason:\n  "
        + "\n  ".join(undeclared)
    )


def test_public_allowlist_has_no_stale_entries():
    """A path that got gated (or deleted) must not linger in the allowlist."""
    ungated_paths = {r.path for r in _ungated()}
    stale = sorted(PUBLIC_LITERAL_GET - ungated_paths)
    assert not stale, (
        "PUBLIC_LITERAL_GET names route(s) that are gated or no longer exist — "
        "remove them so the allowlist keeps meaning what it says:\n  "
        + "\n  ".join(stale)
    )


def test_structure_timer_owner_search_requires_a_session():
    """Regression for the specific hole T-047 was filed about."""
    r = _client().get("/structure-timers/search/owners?q=hard")
    assert r.status_code == 401, (
        f"owner search answered {r.status_code} to an anonymous caller — this "
        "endpoint runs an authenticated ESI search on a stored token"
    )
    assert r.text == ""


def test_structure_timer_sde_searches_require_a_session():
    for path in ("/structure-timers/search/systems", "/structure-timers/search/regions"):
        r = _client().get(f"{path}?q=jita")
        assert r.status_code == 401, f"{path} answered {r.status_code} anonymously"


def test_status_telemetry_is_not_readable_without_a_session():
    for path in ("/status/data", "/status/chart.json"):
        r = _client().get(path)
        assert r.status_code in (401, 403), f"{path} answered {r.status_code} anonymously"


def test_status_telemetry_is_not_readable_by_a_plain_user(nonadmin_user_id):
    """Session alone was the old gate; instance-wide ESI telemetry needs a role.

    `_build_context` returns per-group ESI token budgets, the request log with
    paths, and recent rate-limit events — instance-wide state that an ordinary
    logged-in member has no business reading.
    """
    client = _client(user_id=nonadmin_user_id, role="user")
    for path in ("/status/data", "/status/chart.json"):
        r = client.get(path)
        assert r.status_code == 403, (
            f"{path} answered {r.status_code} to a non-admin session — expected 403"
        )


def test_status_chart_is_readable_by_an_admin(admin_user_id):
    """The gate must not lock out the console these partials exist to serve."""
    client = _client(user_id=admin_user_id, role="admin")
    r = client.get("/status/chart.json")
    assert r.status_code == 200, (
        f"admin got {r.status_code} from /status/chart.json — the role gate is "
        "too tight and the admin console's ESI chart is broken"
    )


def test_status_chart_is_readable_by_a_manager(manager_user_id):
    """Pins a deliberate divergence from `/status/banner`.

    `_is_admin` admits admin *and* manager, matching `require_admin` (the gate
    on the admin console these partials belong to). `/status/banner` in the
    same module admits only a strict `role == "admin"` from the session. So a
    manager sees the telemetry partials but an empty ESI banner. That is the
    intended reading — manager is an admin-console role — but it is subtle
    enough that it deserves a test rather than a comment, so changing it is a
    conscious act.
    """
    client = _client(user_id=manager_user_id, role="manager")
    assert client.get("/status/chart.json").status_code == 200


def test_the_gate_detection_actually_discriminates():
    """Prove the lint can tell a hole from a fix — otherwise it guards nothing.

    A test that only ever sees compliant code cannot show it would catch the
    non-compliant case. Rather than remove a real gate to watch this file go
    red, feed the two regexes synthetic handlers with known properties.
    """
    leaky = (
        "async def search_owners(request: Request, db=Depends(get_db)):\n"
        "    q = request.query_params.get('q', '')\n"
        "    client = await get_client(char, db)\n"
        "    return await client.get('/characters/1/search/')\n"
    )
    gated = (
        "async def search_owners(request: Request, db=Depends(get_db)):\n"
        "    if not request.session.get('user_id'):\n"
        "        return HTMLResponse('', status_code=401)\n"
        "    client = await get_client(char, db)\n"
        "    return await client.get('/characters/1/search/')\n"
    )
    # The shape this ticket was filed about: no gate + reaches ESI.
    assert not _GATE_IDIOM.search(leaky), "gate regex false-positives on ungated code"
    assert _ESI_CALL.search(leaky), "ESI regex misses a get_client call"
    # Adding the gate is what clears it — the ESI call itself is still there.
    assert _GATE_IDIOM.search(gated), "gate regex misses the standard session check"

    # And the real handler is on the right side of that line now.
    owners = next(
        r for r in _literal_get_routes() if r.path == "/structure-timers/search/owners"
    )
    assert _GATE_IDIOM.search(_source(owners))
