"""Refuse any authenticated ESI call the character has not granted a scope for.

Every authenticated request goes through ESIClient.get()/post(), and both call
``check()`` first — before the DB cache, before the ETag cache, before the
network. So a permission a user withdrew stops being served from Vigilant's
own caches the moment the new token is in place, not when the cache expires.

The token already limits what ESI will answer; this is the app-side half of the
same promise. It makes the rule testable ("nothing the user did not select is
read" is a property of one function, not of forty call sites), and it turns a
would-be 403 — which costs error-limit budget and reads as a sync failure —
into a typed exception callers treat as "not permitted".

Unknown operations are refused too. The table in app/esi/scope_map.py is
generated from CCP's spec, and tests/test_esi_scope_guard.py fails if app code
calls a path the table does not know; failing closed means a missed entry shows
up as a refused call in development, never as an unchecked one in production.
"""
from __future__ import annotations

import base64
import json
import re
from functools import lru_cache

from app.esi.scope_map import AUTH_OPS, PUBLIC_OPS


class ScopeNotGranted(Exception):
    """The character's token does not carry the scope this call needs."""

    def __init__(self, method: str, path: str, scope: str | None):
        self.method = method
        self.path = path
        self.scope = scope
        what = scope or "a known ESI operation"
        super().__init__(f"{method} {path} requires {what}, which this token was not granted")


def _compile(template: str) -> re.Pattern:
    parts = re.split(r"(\{[^}]+\})", template.rstrip("/"))
    rx = "".join("[^/]+" if part.startswith("{") else re.escape(part) for part in parts)
    return re.compile(f"^{rx}/?$")


# Literal segments beat placeholders: /characters/{id}/mail/labels must not be
# read as /characters/{id}/mail/{mail_id}. Sorting by the number of
# placeholders (fewest first) makes the most specific template win.
def _ordered(ops):
    return sorted(ops, key=lambda op: op[1].count("{"))


_AUTH = [(m, _compile(p), p, scope, roles) for m, p, scope, roles in _ordered(AUTH_OPS)]
_PUBLIC = [(m, _compile(p), p) for m, p in _ordered(PUBLIC_OPS)]


@lru_cache(maxsize=2048)
def lookup(method: str, path: str) -> tuple[bool, str | None, tuple[str, ...]]:
    """(known, required scope or None, in-game roles) for an ESI call.

    ``path`` is what callers pass to ESIClient — e.g. "/characters/1/wallet/" —
    with or without the trailing slash, never with the base URL or a query.
    """
    method = method.upper()
    path = path.split("?", 1)[0]
    for m, rx, _tpl, scope, roles in _AUTH:
        if m == method and rx.match(path):
            return True, scope, roles
    for m, rx, _tpl in _PUBLIC:
        if m == method and rx.match(path):
            return True, None, ()
    return False, None, ()


def granted_scopes(token: str) -> frozenset[str]:
    """Scopes an EVE SSO access token carries, from its ``scp`` claim.

    Not signature-verified: the token came from SSO over TLS and is our own,
    and this only ever NARROWS what the client will do. Anything unparseable
    (the empty token public clients use, a test double) grants nothing.
    """
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        scp = json.loads(base64.urlsafe_b64decode(payload_b64)).get("scp")
    except Exception:
        return frozenset()
    if isinstance(scp, str):
        return frozenset(scp.split())
    if isinstance(scp, list):
        return frozenset(s for s in scp if isinstance(s, str))
    return frozenset()


def scopes_in_token(token: str) -> frozenset[str] | None:
    """Like granted_scopes(), but None when the string is not a parseable EVE
    JWT at all — so a caller can tell "this token has no scopes" (EVE omits
    `scp` then) from "this is not a token I can read"."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return None
    if not isinstance(payload, dict) or "sub" not in payload:
        return None
    return granted_scopes(token)


def check(method: str, path: str, granted: frozenset[str]) -> None:
    """Raise ScopeNotGranted unless ``granted`` covers this call."""
    known, scope, _roles = lookup(method, path)
    if not known:
        raise ScopeNotGranted(method.upper(), path, None)
    if scope is not None and scope not in granted:
        raise ScopeNotGranted(method.upper(), path, scope)
