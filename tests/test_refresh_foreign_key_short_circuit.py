"""Tests the `_do_refresh` short-circuit added alongside the token migration
fix (see test_token_encryption_migration.py for the migration itself).

EncryptedText.process_result_value returns undecryptable ciphertext as a
plain string instead of raising, so a character whose refresh_token was
encrypted under a SECRET_KEY the app no longer has looks, to the ORM, like
it just has a weird refresh_token — not like an error. Without the
short-circuit, `_do_refresh` would send that ciphertext to EVE SSO as a
refresh_token grant, get invalid_grant, and only THEN raise TokenRevoked.
These tests pin that it now raises immediately, without the network call,
and that a normal (non-Fernet-shaped) refresh_token still goes through the
real request path.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

import app.esi.client as esi_client
from app.db.models import Character
from app.esi.client import TokenRevoked, _do_refresh


def _character(refresh_token: str) -> Character:
    return Character(
        character_id=1,
        character_name="Test Pilot",
        access_token="access-token-value",
        refresh_token=refresh_token,
        token_expiry=datetime.now(timezone.utc) - timedelta(minutes=10),
        scopes="",
        declined_scopes="",
    )


def _run(coro):
    # Python 3.14 removed the implicit auto-created event loop that
    # asyncio.get_event_loop() used to lazily vivify (see test_ambient_kills.py).
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _NetworkReached(Exception):
    """Raised by the fake HTTP client so a test can prove the network path
    was (or wasn't) reached, without depending on httpx internals."""


def _fail_if_called():
    def _get_http_client():
        raise _NetworkReached("get_http_client() was called")
    return _get_http_client


def test_foreign_key_ciphertext_short_circuits_without_network_call(monkeypatch):
    # A real Fernet token, but encrypted under a key this test never installs
    # as the app's current key — exactly what a rotated-away SECRET_KEY
    # leaves behind in the refresh_token column.
    foreign_ciphertext = Fernet(Fernet.generate_key()).encrypt(b"whatever").decode()
    monkeypatch.setattr(esi_client, "get_http_client", _fail_if_called())

    char = _character(foreign_ciphertext)
    with pytest.raises(TokenRevoked):
        _run(_do_refresh(char, db=None))


def test_opaque_refresh_token_still_reaches_the_network(monkeypatch):
    # Negative control: a short, non-Fernet-shaped refresh token must NOT be
    # short-circuited — it has to reach the real SSO request path.
    class _FakeClient:
        async def post(self, *args, **kwargs):
            raise _NetworkReached("post() was called")

    monkeypatch.setattr(esi_client, "get_http_client", lambda: _FakeClient())

    char = _character("short-opaque-refresh-token-1234")
    with pytest.raises(_NetworkReached):
        _run(_do_refresh(char, db=None))
