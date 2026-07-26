"""Regression test for BUG-2: per-coroutine AsyncSessionLocal in structure resolution.

`_resolve_structures` fans `_fetch_structure` coroutines out through
`asyncio.gather`. Each coroutine passes its session to `get_structure` (which
does a StructureNameCache read and, on ESI success, a `cache_structure_name`
commit) and to the `get_cached_structure` except-fallback. If they all share
one request-scoped `db` AsyncSession, concurrent execute/commit on a single
session raises hard greenlet / InvalidRequestError failures that abort the
character's asset resolution (the except-fallback reuses the same broken
session, so it cannot recover either).

The fix gives each gathered coroutine its own `AsyncSessionLocal()` session.
The first test stubs `esi_universe.get_structure` to capture the `db` kwarg it
receives for 3 fake structure IDs, and asserts the 3 captured sessions are
pairwise distinct. Under the old shared-session code all three would be the
one outer `db` (identical), so the distinctness assertion fails — that's the
discriminator.

The second test covers the poisoned-session fallback: if get_structure fails
AND the fallback get_cached_structure read raises too (e.g. the session's
transaction was deactivated by a "database is locked" commit failure),
`_resolve_structures` must still return the "Unknown Structure" sentinel for
that ID instead of propagating through the plain `asyncio.gather`.

Sync-style (no pytest-asyncio): a single manually-managed event loop, per
tests/test_sync_field_sessions.py.
"""
import asyncio

import app.routes.dashboard as dash
from app.esi import universe as esi_universe


class _FakeSession:
    """Stand-in for an AsyncSessionLocal() context manager instance."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_each_structure_coroutine_gets_its_own_session(monkeypatch):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    captured_sessions: list = []

    def _session_factory():
        # Each `async with AsyncSessionLocal()` builds a brand-new instance.
        return _FakeSession()

    async def _stub_get_structure(client, structure_id, db=None):
        captured_sessions.append(db)
        return {"name": f"Struct {structure_id}", "solar_system_id": 30000142}

    monkeypatch.setattr(dash, "AsyncSessionLocal", _session_factory)
    monkeypatch.setattr(esi_universe, "get_structure", _stub_get_structure)

    struct_ids = [1000000000001, 1000000000002, 1000000000003]

    try:
        results = loop.run_until_complete(
            dash._resolve_structures(object(), struct_ids)
        )
    finally:
        loop.close()

    # All three structures resolved.
    assert len(results) == 3, results
    assert {sid for sid, _ in results} == set(struct_ids)
    for _sid, info in results:
        assert info["structure_name"].startswith("Struct ")
        assert info["system_id"] == 30000142

    # Each coroutine received its OWN session passed through to get_structure.
    assert len(captured_sessions) == 3, captured_sessions
    assert all(s is not None for s in captured_sessions)
    # Pairwise distinct — the fix's core guarantee. Old shared-session code
    # would pass the one outer `db` to all three (identical), failing here.
    assert len({id(s) for s in captured_sessions}) == 3, captured_sessions


def test_poisoned_session_fallback_returns_sentinel(monkeypatch):
    """get_structure raises AND the fallback cache read raises → sentinel,
    not an exception propagating through the plain asyncio.gather."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _stub_get_structure(client, structure_id, db=None):
        raise RuntimeError("ESI down")

    async def _stub_get_cached_structure(db, structure_id):
        # Simulates PendingRollbackError on a session whose transaction was
        # deactivated by a failed commit inside cache_structure_name.
        raise RuntimeError("PendingRollbackError: transaction deactivated")

    monkeypatch.setattr(dash, "AsyncSessionLocal", _FakeSession)
    monkeypatch.setattr(esi_universe, "get_structure", _stub_get_structure)
    monkeypatch.setattr(esi_universe, "get_cached_structure", _stub_get_cached_structure)

    struct_id = 1000000000009

    try:
        results = loop.run_until_complete(
            dash._resolve_structures(object(), [struct_id])
        )
    finally:
        loop.close()

    assert results == [
        (struct_id, {"system_id": None, "structure_name": "Unknown Structure"})
    ], results
