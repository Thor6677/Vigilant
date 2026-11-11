"""Hub Scan runner: queueing, retry, early stop, cancel, failure handling."""
import asyncio
import tempfile
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, User
from app.market import hub_cost, scan_lists, scan_results, scan_runner

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
S = scan_results.Settings(5e8, 20, 3.37, 1.5)
USERS = (91, 92, 93)


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(asyncio.wait_for(coro, 20))
    finally:
        loop.close()
        asyncio.set_event_loop(None)


async def wait(job):
    try:
        await asyncio.wait_for(asyncio.shield(job.task), 5)
    except asyncio.CancelledError:
        pass


async def until(predicate):
    for _ in range(400):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached")


class Env:
    def __init__(self, factory):
        self.factory = factory
        self.ids = [2001, 2002, 2003]
        self.total = None
        self.calls = 0
        self.inflight = 0
        self.seen = set()
        self.fail_once = set()
        self.fail_always = set()
        self.bench_raises = set()
        self._failed = set()
        self._hold = None

    @property
    def hold(self):
        if self._hold is None:
            self._hold = asyncio.Event()
            self._hold.set()
        return self._hold

    @property
    def items_attempted(self):
        return len(self.seen)


@pytest.fixture
def env(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as db:
            db.add_all([User(id=u) for u in USERS])
            await db.commit()
    _run(setup())

    e = Env(factory)

    async def expand(db, user_id, kind, ref):
        ids = list(e.ids)
        return scan_lists.Expanded("Sample", ids, {i: f"Item {i}" for i in ids},
                                   e.total if e.total is not None else len(ids))

    async def benchmark(db, type_id):
        if type_id in e.bench_raises:
            raise RuntimeError("bad lookup")
        return hub_cost.Benchmark(100.0, 10)

    async def ensure_summary(db, region_id, station_id, type_id, now, pacer):
        e.calls += 1
        e.seen.add(type_id)
        e.inflight += 1
        try:
            await e.hold.wait()
            if type_id in e.fail_always:
                raise RuntimeError("upstream down")
            if type_id in e.fail_once and type_id not in e._failed:
                e._failed.add(type_id)
                raise RuntimeError("blip")
            return 0
        finally:
            e.inflight -= 1

    monkeypatch.setattr(scan_runner, "AsyncSessionLocal", factory)
    monkeypatch.setattr(scan_runner.scan_lists, "expand", expand)
    monkeypatch.setattr(scan_runner.hub_cost, "benchmark", benchmark)
    monkeypatch.setattr(scan_runner.scan_fetch, "ensure_summary", ensure_summary)
    monkeypatch.setattr(scan_runner, "_now", lambda: NOW)
    # No limits from an earlier loop; start() rebuilds them on this one.
    scan_runner._jobs.clear()
    scan_runner._slots = scan_runner._pacer = None
    yield e
    scan_runner._jobs.clear()
    scan_runner._slots = scan_runner._pacer = None
    _run(engine.dispose())


async def _scan_and_load(env, user=91, kind="category", ref=10):
    scan_runner.reset()
    job = scan_runner.start(user, kind, ref, S)
    await wait(job)
    async with env.factory() as db:
        return job, await scan_results.load(db, user, kind, ref)


def test_a_scan_saves_a_complete_result(env):
    job, result = _run(_scan_and_load(env))
    assert (job.state, job.done, job.total) == ("done", 3, 3)
    assert result["status"] == "complete" and len(result["rows"]) == 3
    assert env.calls == 3 * 5                      # five regions per item


def test_start_returns_the_running_job(env):
    async def go():
        scan_runner.reset()
        env.hold.clear()
        first = scan_runner.start(91, "category", 10, S)
        second = scan_runner.start(91, "blueprints", 0, S)
        env.hold.set()
        await wait(first)
        return first, second
    first, second = _run(go())
    assert first is second


def test_third_job_queues_until_a_slot_frees(env):
    async def go():
        scan_runner.reset()
        env.hold.clear()
        a = scan_runner.start(91, "category", 10, S)
        b = scan_runner.start(92, "category", 10, S)
        c = scan_runner.start(93, "category", 10, S)
        await asyncio.sleep(0.05)
        states = (a.state, b.state, c.state)
        env.hold.set()
        for j in (a, b, c):
            await wait(j)
        return states, c.state
    assert _run(go()) == (("running", "running", "queued"), "done")


def test_failed_item_is_retried_once(env):
    env.fail_once = {2002}
    job, result = _run(_scan_and_load(env))
    assert all(r["status"] != "unavailable" for r in result["rows"])
    assert result["item_scanned"] == 3
    assert job.done == 3                            # the retry pass adds no progress


def test_item_that_keeps_failing_is_unavailable(env):
    env.fail_always = {2002}
    job, result = _run(_scan_and_load(env))
    assert [r["status"] for r in result["rows"]].count("unavailable") == 1
    assert result["status"] == "complete" and result["item_scanned"] == 2


def test_twenty_failures_in_a_row_stop_the_scan(env):
    env.ids = list(range(3000, 3040))
    env.fail_always = set(env.ids)
    job, result = _run(_scan_and_load(env))
    assert result["status"] == "incomplete"
    assert all(r["status"] == "unavailable" for r in result["rows"])
    assert env.items_attempted == 20                # ITEMS_AT_ONCE is 4: stops after 5 chunks


def test_cancel_keeps_the_previous_result(env):
    async def go():
        scan_runner.reset()
        job = scan_runner.start(91, "category", 10, S)
        await wait(job)                             # a first, complete result
        env.hold.clear()
        second = scan_runner.start(91, "category", 10, S)
        await until(lambda: env.inflight > 0)
        assert scan_runner.cancel(91) is True
        await wait(second)
        async with env.factory() as db:
            return second.state, await scan_results.load(db, 91, "category", 10)
    state, result = _run(go())
    assert state == "cancelled" and result["status"] == "complete"
    assert env.inflight == 0                        # no child left running


def test_cancel_frees_the_slot_for_a_queued_job(env):
    async def go():
        scan_runner.reset()
        env.hold.clear()
        a = scan_runner.start(91, "category", 10, S)
        b = scan_runner.start(92, "category", 10, S)
        c = scan_runner.start(93, "category", 10, S)
        await until(lambda: (a.state, b.state, c.state) == ("running", "running", "queued"))
        assert scan_runner.cancel(91) is True
        await wait(a)
        await until(lambda: c.state == "running")
        env.hold.set()
        await wait(b)
        await wait(c)
        return a.state, b.state, c.state
    assert _run(go()) == ("cancelled", "done", "done")


def test_missing_list_fails_without_saving(env, monkeypatch):
    async def gone(db, user_id, kind, ref):
        raise scan_lists.ListNotFound()
    monkeypatch.setattr(scan_runner.scan_lists, "expand", gone)
    job, result = _run(_scan_and_load(env))
    assert (job.state, job.message) == ("failed", "That list no longer exists.")
    assert result is None


def test_cap_note_when_the_list_was_cut(env):
    env.total = 412
    job, result = _run(_scan_and_load(env))
    assert any("3 of 412 scanned" in n for n in result["notes"])


def test_a_benchmark_error_costs_only_that_item(env):
    env.bench_raises = {2002}
    job, result = _run(_scan_and_load(env))
    assert job.state == "done" and result["status"] == "complete"
    assert len(result["rows"]) == 3
    assert result["item_scanned"] == 3
