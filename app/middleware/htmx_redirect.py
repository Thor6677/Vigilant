"""Turn a redirect answered to an htmx request into a page-level navigation.

There is no auth middleware in this app — every route gates itself, and the
gate's answer to a missing session is a redirect to the login page
(`RedirectResponse("/")` in ~80 handlers, `HTTPException(303)` in
`require_admin`). That is the right answer for a browser navigation. It is
the wrong answer for an htmx request, and the difference is invisible from
the handler's side:

XHR follows a redirect on its own and hands htmx a **200 with the login
page as its body**. htmx then does what it was asked to do and swaps that
body into the request's target. For a one-off click that shows a login
form nested inside whatever panel the user was looking at. For a polling
slot it does that on every tick, forever — the admin Overview re-fetches
itself every 10 s, so one tab left open past its session's expiry swapped
the 20 kB landing page into `#admin-content` six times a minute until
someone closed it (ISS-038). Each of those swaps also re-created the
landing page's six inline script blocks under a nonce the page's CSP never
matched, which is where the ~2,400 violation reports an hour came from
(ISS-037).

The htmx-native way to say "go to this page" is the `HX-Redirect` header:
htmx sets `location.href` to it and does nothing else — no swap, no
`htmx:responseError` (it checks the header before it looks at the status;
htmx 1.9.12 `handleAjaxResponse`). So this middleware rewrites every
redirect that would have been answered to an `HX-Request` into

    401  HX-Redirect: <the redirect's Location>

with no body. The route's own answer is untouched for ordinary navigation.
It is a middleware rather than a change to each gate because the gates are
inline by design (see tests/test_route_auth_gating.py) and a per-gate fix
is exactly the kind that gets forgotten on the next route.

Why 401 and not the redirect's own status: to htmx the status is
irrelevant once `HX-Redirect` is present, and to anything watching the
network tab a 401 says what actually happened. In this app an htmx request
being redirected only ever means the session is gone — surveyed 2026-09-23:
every `hx-post` endpoint answers its success path with a fragment and its
no-session path with an empty 401, never a redirect. If a handler ever
wants htmx to navigate somewhere on success, it should set `HX-Redirect` /
`HX-Location` itself; the middleware leaves a response that already
carries either header alone.
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def is_htmx_request(request: Request) -> bool:
    return request.headers.get("hx-request", "").lower() == "true"


class HTMXRedirectMiddleware(BaseHTTPMiddleware):
    """Answer htmx with `401 + HX-Redirect` wherever a handler redirected."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if not is_htmx_request(request):
            return response
        if response.status_code not in _REDIRECT_STATUSES:
            return response
        location = response.headers.get("location")
        if not location:
            return response
        if "hx-redirect" in response.headers or "hx-location" in response.headers:
            return response
        # Keep everything else the handler said (cookies, HX-Trigger, ...);
        # only the browser-level redirect and the body it came with go.
        # A 303 raised as an HTTPException carries a JSON `detail` body, and
        # a body is precisely what htmx must not be given here.
        out = Response(status_code=401)
        out.raw_headers = [
            (k, v) for k, v in response.headers.raw
            if k not in (b"location", b"content-length", b"content-type")
        ]
        out.headers["Content-Length"] = "0"
        out.headers["HX-Redirect"] = location
        # A redirect is a moment-in-time answer; make sure no cache in front
        # of the app hands the 401 back to a later, signed-in poll.
        out.headers["Cache-Control"] = "no-store"
        out.headers["Vary"] = "HX-Request"
        return out
