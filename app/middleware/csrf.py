"""
CSRF middleware — synchronizer-token pattern bound to the user's session.

Strategy:
- The token lives in ``request.session["csrf_token"]`` so it is signed by
  Starlette's SessionMiddleware (no separate signing infra needed).
- On safe-method requests (GET/HEAD/OPTIONS) we lazily mint a token if the
  session has one but no token yet, so the very first page-load after deploy
  populates it for already-logged-in users.
- On state-mutating requests we require either an ``X-CSRF-Token`` header
  (used by htmx, fetch and the React frontend via a global hook) or a
  ``csrf_token`` field in an ``application/x-www-form-urlencoded`` body
  (used by native HTML form submits with the auto-injected hidden input).
- Multipart bodies are intentionally not parsed here. Forms that need to
  POST multipart must use a JS-driven fetch and send the header.

Wiring: this middleware must sit *inside* SessionMiddleware so that
``scope["session"]`` is populated when we read or write the token. In
Starlette, that means it must be ``add_middleware``-ed BEFORE
SessionMiddleware (later registrations wrap earlier ones).
"""
from __future__ import annotations

import json
import logging
import secrets
from urllib.parse import parse_qsl
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Paths that are state-mutating but cannot use a CSRF token. None today —
# OAuth callback is GET (state cookie covers it) and /healthz is GET.
EXEMPT_PATHS: frozenset[str] = frozenset()

# urlencoded bodies above this size are rejected before parsing — guards
# against a memory-amplification attack via the CSRF middleware itself.
MAX_URLENCODED_BODY = 1 * 1024 * 1024  # 1 MB


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _ensure_session_token(session: dict) -> None:
    """Mint a CSRF token if the session doesn't have one yet."""
    if not session.get("csrf_token"):
        session["csrf_token"] = _new_token()


async def _send_403(send: Send, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": 403,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
        ],
    })
    await send({"type": "http.response.body", "body": body, "more_body": False})


def _header_value(scope: Scope, name: bytes) -> str | None:
    for k, v in scope.get("headers", []):
        if k == name:
            try:
                return v.decode("latin-1")
            except UnicodeDecodeError:
                return None
    return None


class CSRFMiddleware:
    """Pure ASGI CSRF middleware.

    Reads ``scope["session"]`` (set up by Starlette's SessionMiddleware) so it
    must be installed *inside* SessionMiddleware in the middleware stack.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")

        session = scope.get("session")
        # When SessionMiddleware is configured, ``scope["session"]`` is always
        # a dict (possibly empty). If it's missing, something is wired wrong —
        # don't pretend we provided protection.
        if session is None:
            log.warning("CSRFMiddleware: scope has no 'session' for %s %s", method, path)
            await self.app(scope, receive, send)
            return

        # Safe methods: just lazily seed the token and pass through.
        if method in SAFE_METHODS or path in EXEMPT_PATHS:
            _ensure_session_token(session)
            await self.app(scope, receive, send)
            return

        session_token = session.get("csrf_token")
        if not session_token:
            await _send_403(send, "CSRF token missing — refresh the page and retry.")
            return

        # 1. Header check (htmx, fetch, React frontend).
        sent = _header_value(scope, b"x-csrf-token")

        # 2. Fallback: urlencoded form field. Read body, parse, replay.
        replay_receive: Receive = receive
        if not sent:
            ct = (_header_value(scope, b"content-type") or "").lower()
            if "application/x-www-form-urlencoded" in ct:
                # Buffer the body so we can both parse it and pass it on.
                body_chunks: list[bytes] = []
                total = 0
                while True:
                    msg = await receive()
                    if msg["type"] == "http.disconnect":
                        return
                    if msg["type"] != "http.request":
                        # Unknown ASGI message — be safe and drop.
                        return
                    chunk = msg.get("body", b"") or b""
                    total += len(chunk)
                    if total > MAX_URLENCODED_BODY:
                        await _send_403(send, "CSRF check: form body too large.")
                        return
                    body_chunks.append(chunk)
                    if not msg.get("more_body", False):
                        break
                body = b"".join(body_chunks)
                try:
                    decoded = body.decode("utf-8")
                except UnicodeDecodeError:
                    decoded = body.decode("latin-1", errors="replace")
                for k, v in parse_qsl(decoded, keep_blank_values=True):
                    if k == "csrf_token":
                        sent = v
                        break

                # Build a one-shot replay receive so the route handler can read
                # the same body. After the buffered chunk is delivered, fall
                # back to the original receive (which yields http.disconnect
                # etc.).
                _delivered = False
                async def _replay() -> Message:
                    nonlocal _delivered
                    if not _delivered:
                        _delivered = True
                        return {
                            "type": "http.request",
                            "body": body,
                            "more_body": False,
                        }
                    return await receive()
                replay_receive = _replay

        if not sent or not secrets.compare_digest(str(sent), str(session_token)):
            await _send_403(send, "CSRF token invalid or missing — refresh the page and retry.")
            return

        await self.app(scope, replay_receive, send)
