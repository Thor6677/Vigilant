"""Per-request CSP nonce middleware.

Step 1 of T-012 (remove CSP unsafe-inline). The middleware generates a
fresh nonce per request, stashes it on `request.state.csp_nonce` so
templates can render `<script nonce="{{ request.state.csp_nonce }}">`,
and sets a `Content-Security-Policy-Report-Only` header whose
script-src/style-src include both the nonce and `'unsafe-inline'`.

Why both nonce + unsafe-inline in Report-Only:
- Per CSP Level 3, when a nonce is present in script-src, `'unsafe-inline'`
  is IGNORED by modern browsers. So nonced inline runs; un-nonced inline
  is *reported* (Report-Only = no enforcement, just CSP violation events).
- Legacy browsers without CSP3 nonce support fall back to `'unsafe-inline'`
  and execute everything as before.
- This is the foundation step: after Steps 2-4 land (514 inline handlers
  refactored to addEventListener, 100+ style attrs moved to utility
  classes, then header flipped from Report-Only to enforcing), we drop
  `'unsafe-inline'` entirely.

The edge nginx config at /opt/edge/nginx/conf.d/vigilant.conf:47 still
sends its own static Content-Security-Policy-Report-Only header. That's
harmless — browsers accept multiple Report-Only headers and fire reports
against each independently. The cleanup (removing the nginx-side header)
happens in Step 4 alongside the unsafe-inline drop.
"""
from __future__ import annotations

import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


# Format string for the header. The {nonce} placeholder is filled per
# request. Mirrors the existing nginx Report-Only policy at
# /opt/edge/nginx/conf.d/vigilant.conf:47 with the addition of a nonce
# source in script-src and style-src.
_CSP_TEMPLATE = (
    "default-src 'self'; "
    "script-src 'self' 'nonce-{nonce}' 'unsafe-inline'; "
    "style-src 'self' 'nonce-{nonce}' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data: blob: https:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


class CSPNonceMiddleware(BaseHTTPMiddleware):
    """Stamps a per-request nonce on request.state and emits a matching
    Content-Security-Policy-Report-Only header on the response."""

    async def dispatch(self, request: Request, call_next):
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = await call_next(request)
        # Don't override the header if a downstream handler already set
        # one — gives individual routes an escape hatch if they need a
        # tighter policy without unsafe-inline.
        if "content-security-policy-report-only" not in {
            k.lower() for k in response.headers.keys()
        }:
            response.headers["Content-Security-Policy-Report-Only"] = (
                _CSP_TEMPLATE.format(nonce=nonce)
            )
        return response
