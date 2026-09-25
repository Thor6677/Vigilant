"""Per-request CSP nonce middleware.

Step 1 of T-012 (remove CSP unsafe-inline). The middleware generates a
fresh nonce per request, stashes it on `request.state.csp_nonce` so
templates can render `<script nonce="{{ request.state.csp_nonce }}">`,
and sets a `Content-Security-Policy-Report-Only` header whose
script-src includes both the nonce and `'unsafe-inline'`.

Why the nonce, and why style-src is shaped differently:
- Per CSP Level 3, when a nonce is present in script-src, `'unsafe-inline'`
  is IGNORED by modern browsers. Until T-033 the directive carried both: the
  nonce did the real work, and `'unsafe-inline'` was a fallback for browsers
  too old to understand nonces. It is gone as of T-033, so un-nonced inline
  script is blocked everywhere rather than reported.
- style-src carries NO nonce for exactly the same spec rule: a nonce there
  would neuter `'unsafe-inline'` and make every un-nonceable `style=""`
  attribute fire a report — the 2026-07-03 report-flood incident (see the
  _CSP_TEMPLATE comment). Inline styles are permanently allowed per T-032.

## T-012 roadmap status (updated 2026-05-19 per T-032 decision)

- **Step 1 (T-012):** ✅ nonce middleware + nonced inline blocks (commit 6d1d9de).
- **Step 2 (T-031):** ✅ done — inline event handlers (`onclick=` etc.)
  migrated to delegated `data-<event>="fn"` attributes via
  `static/js/actions.js`. The tail of arg-bearing handlers (ISS-020/021/022)
  went with the ISS-021 dataset convention, and the last round also took the
  handlers that five route modules were emitting inside hand-built HTML
  strings — invisible to a templates-only search, and the reason to keep
  `tests/test_csp_inline_handlers.py` pointed at `app/**/*.py` as well. That
  test now holds the line at zero.
- **Step 3 (T-032):** ✅ DECISION — `style-src` keeps `'unsafe-inline'`
  permanently. Refactoring 3,296 inline `style="..."` declarations across
  101 templates to JS-driven `setProperty` calls would be a months-long
  undertaking with real first-paint regressions, for marginal hardening of
  a threat (style-based XSS) that is dramatically less exploitable than
  script injection. Industry precedent (GitHub, Linear, Notion, Stripe
  Dashboard) keeps `style-src 'unsafe-inline'` while tightening
  `script-src`. The OWASP CSP cheat-sheet and Google's web.dev "Strict
  CSP" guide both treat this stance as acceptable. For a single-tenant
  authenticated EVE companion app with no untrusted user-generated HTML
  surface, the residual style-based XSS risk is effectively zero. Future
  contributors: do NOT remove `'unsafe-inline'` from `style-src` thinking
  it's the natural next step — that scope was explicitly killed in T-032.
  Design-system hygiene for the most-repeated inline patterns is a
  separate concern, tracked under ISS-028.
- **Step 4 (T-033):** ✅ done — `'unsafe-inline'` dropped from `script-src`
  (style-src untouched, per T-032) and the header flipped from `Report-Only`
  to enforcing.

  What this now blocks, rather than reports: any `<script>` without the
  request's nonce, any `on*=` attribute, and any `javascript:` URL. Nonced
  blocks, `/static/js/*`, and the nonced htmx tag are unaffected. Note the
  loss of the legacy fallback described above: a browser too old for CSP3
  nonces now blocks every inline block rather than running it, because
  `'unsafe-inline'` is no longer there to catch it. That is the point of the
  step, and those browsers are far outside what this app targets.

  If something inline has to run again, it needs the nonce
  (`<script nonce="{{ request.state.csp_nonce }}">`), never a policy
  loosening.

The edge nginx config at /opt/edge/nginx/conf.d/vigilant.conf:47 still
sends its own static Content-Security-Policy-Report-Only header. That was
harmless while this header was Report-Only too, and it still is — a
Report-Only header cannot relax an enforcing one, and the two are evaluated
independently. It is now pure noise in the violation sink, though, and
removing it is the one piece of T-033 that lives outside this repo.
"""
from __future__ import annotations

import secrets
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.config import get_settings


def _sso_origin() -> str:
    """Origin of the EVE SSO authorize endpoint (https://login.eveonline.com).

    The permission picker POSTs to /auth/authorize, which answers with a 303 to
    EVE SSO. Browsers enforce form-action on every hop of a form submission's
    redirect chain, so with 'self' alone the navigation to EVE is blocked and
    the picker's button appears to do nothing (found on the dev instance,
    2026-09-25). Only this one origin is added.
    """
    parts = urlsplit(get_settings().eve_sso_auth_url)
    return f"{parts.scheme}://{parts.netloc}"


# Format string for the header. The {nonce} placeholder is filled per
# request. Mirrors the existing nginx Report-Only policy at
# /opt/edge/nginx/conf.d/vigilant.conf:47 with the addition of a nonce
# source in script-src (and ONLY script-src — see below).
#
# IMPORTANT (T-032 decision, 2026-05-19): `style-src 'unsafe-inline'` is
# permanent — vigilant intentionally keeps inline `style="..."` attrs.
# See module docstring for rationale. Removing it is not part of T-033 or
# any planned future step; doing so requires a separate ticket with
# explicit reconsideration of the threat model.
#
# NO nonce in style-src (2026-07-03 incident): per CSP3, a nonce in the
# directive makes browsers IGNORE 'unsafe-inline' for it, and style=""
# attributes can't carry nonces — so every inline style attr fired a
# style-src-attr report (23k+ rows, ~97% of the sink log). The report
# POSTs flooded the shared edge nginx rate bucket (30r/s) and 429'd real
# requests. A nonce here defeats the T-032 decision above; do not re-add.
_CSP_TEMPLATE = (
    "default-src 'self'; "
    "script-src 'self' 'nonce-{nonce}'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data: blob: https:; "
    # esi.evetech.net: ambient module fetches public sovereignty data client-side (login page)
    "connect-src 'self' https://esi.evetech.net; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self' {sso_origin}; "
    # ISS-023: server-side violation sink. Sink lives at app/routes/csp.py,
    # appends to /data/logs/csp-violations.jsonl with size-capped rotation.
    "report-uri /csp-report"
)


_SSO_ORIGIN = _sso_origin()


class CSPNonceMiddleware(BaseHTTPMiddleware):
    """Stamps a per-request nonce on request.state and emits a matching
    Content-Security-Policy header on the response."""

    async def dispatch(self, request: Request, call_next):
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = await call_next(request)
        # Don't override the header if a downstream handler already set
        # one — gives individual routes an escape hatch if they need a
        # tighter policy of their own.
        if "content-security-policy" not in {
            k.lower() for k in response.headers.keys()
        }:
            response.headers["Content-Security-Policy"] = (
                _CSP_TEMPLATE.format(nonce=nonce, sso_origin=_SSO_ORIGIN)
            )
        return response
