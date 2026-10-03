"""ISS-080: a sync queued from the login / SSO callback frees its own
`_queued_sync` entry when it ends, so the background scheduler is not held off
for the 5-minute unstick, and a queued sync that found another one running
(and so did nothing) leaves the pilot to the scheduler's next pass.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import app.db.models  # noqa: F401
import app.routes.dashboard as dash
from app.auth import routes as auth_routes
from app.db.models import Character

CID = 616161


def _run(coro):
    return asyncio.run(coro)


def _drive_queue_sync(monkeypatch, sync_task, cid=CID):
    """Queue a sync the way the callback does, let the task finish, and return
    the entry state seen right after queueing and after the task ended."""
    monkeypatch.setattr(dash, "_sync_task", sync_task)
    dash._queued_sync.pop(cid, None)

    async def go():
        auth_routes._queue_sync(cid)
        queued = cid in dash._queued_sync
        await asyncio.gather(*[t for t in asyncio.all_tasks() if t is not asyncio.current_task()],
                             return_exceptions=True)
        return queued

    queued = _run(go())
    return queued, cid in dash._queued_sync


def test_a_queued_sync_frees_its_entry_when_it_finishes(monkeypatch):
    async def sync_task(cid):
        await asyncio.sleep(0)

    queued, after = _drive_queue_sync(monkeypatch, sync_task)
    assert queued is True
    assert after is False


def test_a_queued_sync_that_raises_still_frees_its_entry(monkeypatch):
    async def sync_task(cid):
        raise RuntimeError("sync blew up")

    queued, after = _drive_queue_sync(monkeypatch, sync_task)
    assert queued is True
    assert after is False


def test_a_queued_sync_that_found_the_lock_held_leaves_the_pilot_to_the_scheduler(monkeypatch):
    """The real _sync_task returns at once when the pilot's lock is held. The
    entry must be gone and the scheduler's own stale predicate must pick the
    pilot (a pilot with no cache row has never been synced)."""
    dash._char_sync_locks.pop(CID, None)
    char = Character(character_id=CID, character_name="Test Pilot", user_id=1,
                     access_token="x", refresh_token="y",
                     token_expiry=datetime(2099, 1, 1), scopes="")

    async def inner(cid):
        raise AssertionError("must not run while the lock is held")

    monkeypatch.setattr(dash, "_sync_task_inner", inner)
    real_sync_task = dash._sync_task

    async def go():
        async with dash._get_char_sync_lock(CID):
            monkeypatch.setattr(dash, "_sync_task", real_sync_task)
            dash._queued_sync.pop(CID, None)
            auth_routes._queue_sync(CID)
            await asyncio.gather(*[t for t in asyncio.all_tasks() if t is not asyncio.current_task()],
                                 return_exceptions=True)

    try:
        _run(go())
        assert CID not in dash._queued_sync
        assert dash._collect_stale([char], {}) == [CID]
    finally:
        dash._char_sync_locks.pop(CID, None)


def test_a_queued_sync_leaves_an_entry_the_scheduler_re_marked(monkeypatch):
    """The scheduler stamps its own entry; a queued sync that ends must not
    free that one while the scheduler's batch is still running."""
    async def sync_task(cid):
        dash._queued_sync[cid] = datetime.now(timezone.utc) + timedelta(seconds=1)

    _queued, after = _drive_queue_sync(monkeypatch, sync_task)
    assert after is True
    dash._queued_sync.pop(CID, None)


