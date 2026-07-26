"""Guard against literal routes being shadowed by parameterised ones.

Starlette matches routes in *registration order* and stops at the first hit. So
a literal path like `/intel/tracker` registered AFTER a catch-all like
`/intel/{scan_id}` is unreachable — every request to it resolves as a scan id
instead, and the page renders whatever the catch-all does for an unknown id.

This is a silent failure. `tests/test_nav_registry.py` cannot catch it, because
the shadowed route *is* registered and its URL *does* resolve — just to the
wrong handler. It only surfaces as "why does this page say not found?".

This exact bug shipped once: `/intel/tracker` (WH Tracker) was swallowed by
`/intel/{scan_id}` because `wh_tracker_router` was included after `dscan_router`
in `app/main.py`. The fix is registration order, so this test locks it in.
"""
from fastapi.routing import APIRoute

import app.main as main

# Methods that don't carry their own handler semantics here.
_IGNORED_METHODS = {"HEAD", "OPTIONS"}


def _api_routes() -> list[APIRoute]:
    return [r for r in main.app.routes if isinstance(r, APIRoute)]


def test_no_literal_route_is_shadowed_by_a_parameterised_one():
    routes = _api_routes()
    shadowed: list[str] = []

    for route in routes:
        if "{" in route.path:
            continue  # only literal paths can be shadowed this way

        for method in sorted((route.methods or set()) - _IGNORED_METHODS):
            # Walk in registration order and find who actually answers first.
            for candidate in routes:
                if method not in (candidate.methods or set()):
                    continue
                if not candidate.path_regex.match(route.path):
                    continue
                if candidate.path != route.path:
                    shadowed.append(
                        f"{method} {route.path} is shadowed by {candidate.path} "
                        f"— register the literal route before the parameterised one"
                    )
                break

    assert not shadowed, "Unreachable route(s):\n  " + "\n  ".join(shadowed)


def test_intel_tracker_resolves_to_the_wh_tracker_page():
    """Regression: the specific route that shipped broken."""
    for target in ("/intel/tracker", "/intel/tracker/poll"):
        winner = next(
            r for r in _api_routes()
            if "GET" in (r.methods or set()) and r.path_regex.match(target)
        )
        assert winner.path == target, (
            f"GET {target} resolves to {winner.path}, not itself"
        )


def test_dscan_scan_ids_still_resolve_to_the_catch_all():
    """The ordering fix must not stop real scan ids reaching the dscan view."""
    winner = next(
        r for r in _api_routes()
        if "GET" in (r.methods or set()) and r.path_regex.match("/intel/abc123")
    )
    assert winner.path == "/intel/{scan_id}"
