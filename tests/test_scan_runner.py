"""Hub Scan runner: queueing, retry, cancel, failure handling.

`ensure_summary` is faked here and its errors are plain ones, not failed
requests (it never reports to the tally), so the request-level stop never
fires: these tests are about the runner's own bookkeeping. The outage rules
(request-level stop, sibling cancel, failure memory, budget guard) are tested
against the real `ensure_summary` in test_scan_outage.py."""
import asyncio
import gc
import tempfile
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, MarketWatchlist, User
from app.esi.market import APPRAISAL_HUBS
from app.market import hub_cost, hub_route, scan_fetch, scan_lists, scan_results, scan_runner

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
        self.fail_now_region = None      # one hub's call raises at once, ignoring `hold`
        self.fail_now_items = None       # ...for these items only (None: every item)
        self.per_item = Counter()
        self.pacers = []
        self.price_map = {34: 1.0}
        self.price_calls = 0
        self.bench = hub_cost.Benchmark(100.0, 10)
        self.bench_maps = []
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

    async def get_price_map(db):
        e.price_calls += 1
        return e.price_map

    async def benchmark(db, type_id, price_map=None):
        e.bench_maps.append(price_map)
        if type_id in e.bench_raises:
            raise RuntimeError("bad lookup")
        return e.bench

    async def ensure_summary(db, region_id, station_id, type_id, now, pacer, tally=None):
        e.calls += 1
        e.seen.add(type_id)
        e.per_item[type_id] += 1
        e.pacers.append(pacer)
        e.inflight += 1
        try:
            if region_id == e.fail_now_region and (e.fail_now_items is None
                                                   or type_id in e.fail_now_items):
                raise RuntimeError("fast failure")
            await e.hold.wait()
            if type_id in e.fail_always:
                raise RuntimeError("upstream down")
            if type_id in e.fail_once and type_id not in e._failed:
                e._failed.add(type_id)
                raise RuntimeError("blip")
            return 0
        finally:
            await asyncio.sleep(0)       # a suspension point, so cleanup ordering is real
            e.inflight -= 1

    monkeypatch.setattr(scan_runner, "AsyncSessionLocal", factory)
    monkeypatch.setattr(scan_runner.scan_lists, "expand", expand)
    monkeypatch.setattr(scan_runner.hub_cost, "benchmark", benchmark)
    monkeypatch.setattr(scan_runner.market_lp, "get_price_map", get_price_map)
    monkeypatch.setattr(scan_runner.scan_fetch, "ensure_summary", ensure_summary)
    monkeypatch.setattr(scan_runner, "_now", lambda: NOW)
    monkeypatch.setattr(scan_fetch, "_failed", {})      # no failure memory from other tests
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
        queued_same = scan_runner.start(91, "blueprints", 0, S)       # still queued
        await until(lambda: first.state == "running")
        running_same = scan_runner.start(91, "blueprints", 0, S)      # actually running
        env.hold.set()
        await wait(first)
        return first, queued_same, running_same
    first, queued_same, running_same = _run(go())
    assert first is queued_same and first is running_same


def test_start_returns_the_queued_job(env):
    async def go():
        scan_runner.reset()
        env.hold.clear()
        a = scan_runner.start(91, "category", 10, S)
        b = scan_runner.start(92, "category", 10, S)
        c = scan_runner.start(93, "category", 10, S)
        await until(lambda: (a.state, b.state, c.state) == ("running", "running", "queued"))
        again = scan_runner.start(93, "blueprints", 0, S)
        env.hold.set()
        for j in (a, b, c):
            await wait(j)
        return c, again
    c, again = _run(go())
    assert c is again


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
    assert env.calls == 3 * 5 + 5                   # the failed item's five hubs again
    assert env.per_item[2002] == 10 and env.per_item[2001] == 5


def test_item_that_keeps_failing_is_unavailable(env):
    env.fail_always = {2002}
    job, result = _run(_scan_and_load(env))
    assert [r["status"] for r in result["rows"]].count("unavailable") == 1
    assert result["status"] == "complete" and result["item_scanned"] == 2
    assert env.per_item[2002] == 2 * 5              # attempted exactly twice
    assert env.calls == 2 * 5 + 2 * 5


def test_a_scan_with_nothing_scanned_keeps_the_last_result(env):
    """Every item unavailable: nothing is saved over the last good result,
    and the job says why. (Replaces the old rule, which saved a page of
    "unavailable" rows as an incomplete result.)"""
    async def go():
        scan_runner.reset()
        first = scan_runner.start(91, "category", 10, S)
        await wait(first)
        async with env.factory() as db:
            before = await scan_results.load(db, 91, "category", 10)
        env.ids = list(range(3000, 3040))
        env.fail_always = set(env.ids)
        second = scan_runner.start(91, "category", 10, S)
        await wait(second)
        async with env.factory() as db:
            after = await scan_results.load(db, 91, "category", 10)
        return second, before, after
    job, before, after = _run(go())
    assert (job.state, job.message) == ("failed", scan_runner.OUTAGE)
    assert job.message == "EVE's market data is unavailable right now. Your last result is kept."
    assert before["status"] == "complete" and len(before["rows"]) == 3
    assert after == before


def test_a_scan_with_nothing_scanned_and_no_earlier_result_saves_nothing(env):
    env.fail_always = set(env.ids)
    job, result = _run(_scan_and_load(env))
    assert (job.state, job.message) == ("failed", scan_runner.OUTAGE)
    assert result is None


def test_an_empty_list_still_saves_an_empty_result(env):
    """No items is not an outage: the (empty) result is saved as before."""
    env.ids = []
    job, result = _run(_scan_and_load(env))
    assert job.state == "done" and result["rows"] == [] and result["status"] == "complete"


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


async def _add_watchlist(env, user_id, name="Sample List"):
    async with env.factory() as db:
        wl = MarketWatchlist(user_id=user_id, name=name, created_at=datetime(2026, 11, 1))
        db.add(wl)
        await db.commit()
        return wl.id


def test_a_watchlist_scan_saves_while_the_list_exists(env):
    """The control for the test below: the check before saving lets a list
    that is still there (and the user's) through."""
    async def go():
        wid = await _add_watchlist(env, 91)
        return await _scan_and_load(env, kind="watchlist", ref=wid)
    job, result = _run(go())
    assert job.state == "done" and result is not None and len(result["rows"]) == 3


def test_a_watchlist_deleted_mid_scan_gets_no_result(env):
    async def go():
        scan_runner.reset()
        wid = await _add_watchlist(env, 91)
        env.hold.clear()
        job = scan_runner.start(91, "watchlist", wid, S)
        await until(lambda: env.inflight > 0)          # expanded, now fetching
        async with env.factory() as db:
            await db.delete(await db.get(MarketWatchlist, wid))
            await db.commit()
        env.hold.set()
        await wait(job)
        async with env.factory() as db:
            return job, await scan_results.load(db, 91, "watchlist", wid)
    job, result = _run(go())
    assert (job.state, job.message) == ("failed", "That list no longer exists.")
    assert result is None


def test_a_watchlist_now_someone_elses_gets_no_result(env):
    """watchlists.get checks the owner as well as the id."""
    async def go():
        scan_runner.reset()
        wid = await _add_watchlist(env, 91)
        env.hold.clear()
        job = scan_runner.start(91, "watchlist", wid, S)
        await until(lambda: env.inflight > 0)
        async with env.factory() as db:
            (await db.get(MarketWatchlist, wid)).user_id = 92
            await db.commit()
        env.hold.set()
        await wait(job)
        async with env.factory() as db:
            return job, await scan_results.load(db, 91, "watchlist", wid)
    job, result = _run(go())
    assert job.state == "failed" and result is None


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


# ---- price list: once per scan ----

def test_the_price_list_is_fetched_once_per_scan(env):
    env.ids = [2001, 2002, 2003, 2004]
    job, result = _run(_scan_and_load(env))
    assert env.price_calls == 1
    assert len(env.bench_maps) == 4 and all(m is env.price_map for m in env.bench_maps)
    assert not any("price list" in n for n in result["notes"])


def test_an_empty_price_list_adds_the_note(env):
    env.price_map = {}
    job, result = _run(_scan_and_load(env))
    assert "EVE's price list was unavailable, so build costs are missing." in result["notes"]
    assert env.price_calls == 1


def test_items_without_blueprints_do_not_add_the_price_note(env):
    env.bench = hub_cost.Benchmark(None, 10, hub_cost.NO_BLUEPRINT)
    job, result = _run(_scan_and_load(env))
    assert not any("price list" in n for n in result["notes"])


# ---- the request tally (the stop rule's bookkeeping) ----

def test_five_failed_requests_in_a_row_stop_and_a_success_resets_the_count():
    async def go():
        t = scan_runner._Tally()
        for ok in (False, False, False, False, True, False, False, False, False):
            t.after(ok)
        before = (t.reason, t.stopped.is_set(), t.made, t.failed_in_a_row)
        t.after(False)
        return before, (t.reason, t.stopped.is_set(), t.made)
    before, after = _run(go())
    assert before == (None, False, 9, 4)
    assert after == (scan_runner.STOPPED_REQUESTS, True, 10)
    assert scan_runner.STOP_AFTER_FAILED_REQUESTS == 5


def test_a_stopped_tally_lets_no_request_through_and_keeps_its_first_reason():
    async def go():
        t = scan_runner._Tally()
        item = scan_runner._ItemTally(t)
        item.before()                               # nothing stopped yet: no raise
        t.halt(scan_runner.STOPPED_BUDGET)
        t.halt(scan_runner.STOPPED_REQUESTS)
        with pytest.raises(scan_runner._Stopped):
            item.before()
        return t.reason
    assert _run(go()) == scan_runner.STOPPED_BUDGET


def test_an_item_that_failed_makes_no_further_request():
    async def go():
        t = scan_runner._Tally()
        item, other = scan_runner._ItemTally(t), scan_runner._ItemTally(t)
        item.after(False)
        with pytest.raises(scan_runner._Stopped):
            item.before()
        other.before()                              # another item is not affected
        return t.made, t.failed_in_a_row, item.request_failed, other.request_failed
    assert _run(go()) == (1, 1, True, False)


def test_a_stopped_job_starts_no_item(env):
    async def go():
        scan_runner.reset()
        t = scan_runner._Tally()
        t.halt(scan_runner.STOPPED_REQUESTS)
        return await scan_runner._fill(None, [2001, 2002, 2003], t)
    assert _run(go()) == {2001, 2002, 2003}
    assert env.calls == 0


def test_items_that_fail_for_other_reasons_never_stop_the_scan(env):
    """Errors that are not failed requests do not count towards the stop."""
    env.ids = list(range(3000, 3030))
    env.fail_once = set(env.ids[:19]) | set(env.ids[20:25])
    job, result = _run(_scan_and_load(env))
    assert result["status"] == "complete"
    assert result["item_scanned"] == 30
    assert all(r["status"] != "unavailable" for r in result["rows"])


# ---- routes ----

def test_a_routes_failure_marks_every_hub_unavailable_with_a_start_system(env, monkeypatch):
    async def boom(db, settings):
        raise RuntimeError("planner down")
    monkeypatch.setattr(scan_runner, "_routes", boom)
    scan_runner.reset()
    with_origin = replace(S, origin_system_id=30000142, origin_name="Somewhere")
    async def go():
        job = scan_runner.start(91, "category", 10, with_origin)
        await wait(job)
        async with env.factory() as db:
            return await scan_results.load(db, 91, "category", 10)
    result = _run(go())
    assert result["routes"] == {k: hub_route.UNAVAILABLE for k in APPRAISAL_HUBS}


def test_a_routes_failure_with_no_start_system_stays_empty(env, monkeypatch):
    async def boom(db, settings):
        raise RuntimeError("planner down")
    monkeypatch.setattr(scan_runner, "_routes", boom)
    job, result = _run(_scan_and_load(env))
    assert result["routes"] == {}


# ---- cancel, errors, bookkeeping ----

def test_a_cancel_right_after_the_save_keeps_the_job_done(env, monkeypatch):
    real_save = scan_results.save

    async def save_then_cancel(*a, **kw):
        await real_save(*a, **kw)
        assert scan_runner.cancel(91) is True      # lands at the next suspension
    monkeypatch.setattr(scan_runner.scan_results, "save", save_then_cancel)
    job, result = _run(_scan_and_load(env))
    assert job.state == "done"
    assert result is not None and result["status"] == "complete"


def test_a_failed_child_is_retrieved_when_the_job_is_cancelled(env):
    async def go():
        loop = asyncio.get_running_loop()
        contexts = []
        loop.set_exception_handler(lambda lp, ctx: contexts.append(ctx))
        scan_runner.reset()
        env.hold.clear()
        env.fail_now_region = next(iter(APPRAISAL_HUBS.values()))["region_id"]
        env.fail_now_items = {2001}
        job = scan_runner.start(91, "category", 10, S)
        # 2001 failed at once and its four held lookups were cancelled; the
        # other two items' ten lookups are held.
        await until(lambda: env.calls == 15 and env.inflight == 10)
        assert scan_runner.cancel(91) is True
        await wait(job)
        state = job.state
        scan_runner._jobs.clear()       # the job's task keeps its traceback, so the children
        job.task = None                 # stay reachable until the job is let go
        del job
        gc.collect()
        return state, contexts
    state, contexts = _run(go())
    assert state == "cancelled"
    assert contexts == []
    assert env.inflight == 0


def test_an_unexpected_error_fails_the_job_and_frees_the_slot(env, monkeypatch):
    real_save = scan_results.save
    calls = {"n": 0}

    async def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] in (2, 3):
            raise RuntimeError("disk full")
        return await real_save(*a, **kw)
    monkeypatch.setattr(scan_runner.scan_results, "save", flaky)

    async def go():
        scan_runner.reset()
        first = scan_runner.start(91, "category", 10, S)
        await wait(first)                           # save #1: a good result
        async with env.factory() as db:
            before = await scan_results.load(db, 91, "category", 10)
        bad_a = scan_runner.start(91, "category", 10, S)
        await wait(bad_a)                           # save #2 raises
        bad_b = scan_runner.start(92, "category", 10, S)
        await wait(bad_b)                           # save #3 raises
        good = scan_runner.start(93, "category", 10, S)
        await wait(good)                            # both slots were freed
        async with env.factory() as db:
            after = await scan_results.load(db, 91, "category", 10)
        return bad_a, bad_b, good, before, after
    bad_a, bad_b, good, before, after = _run(go())
    for bad in (bad_a, bad_b):
        assert (bad.state, bad.message) == ("failed", "The scan failed. Scan again.")
    assert good.state == "done"
    assert after == before                          # the previous result is untouched


def test_cancelling_a_queued_job_lets_a_later_one_run(env):
    async def go():
        scan_runner.reset()
        env.hold.clear()
        a = scan_runner.start(91, "category", 10, S)
        b = scan_runner.start(92, "category", 10, S)
        c = scan_runner.start(93, "category", 10, S)
        await until(lambda: (a.state, b.state, c.state) == ("running", "running", "queued"))
        assert scan_runner.cancel(93) is True
        await wait(c)
        cancelled = c.state
        env.hold.set()
        await wait(a)
        await wait(b)
        d = scan_runner.start(93, "category", 10, S)
        await wait(d)
        return cancelled, a.state, b.state, d.state
    assert _run(go()) == ("cancelled", "done", "done", "done")


def test_cancelling_before_the_task_starts_marks_it_cancelled(env):
    async def go():
        scan_runner.reset()
        job = scan_runner.start(91, "category", 10, S)
        assert scan_runner.cancel(91) is True       # the task has not taken a step
        await wait(job)
        return job.state
    assert _run(go()) == "cancelled"


def test_cancel_on_a_finished_job_returns_false(env):
    async def go():
        scan_runner.reset()
        job = scan_runner.start(91, "category", 10, S)
        await wait(job)
        return job.state, scan_runner.cancel(91), scan_runner.cancel(12345)
    assert _run(go()) == ("done", False, False)


def test_forget_cancels_a_running_job_and_drops_it(env):
    """Account removal: the job stops, saves nothing, and its entry goes; the
    other user's job runs on."""
    async def go():
        scan_runner.reset()
        env.hold.clear()
        mine = scan_runner.start(91, "category", 10, S)
        theirs = scan_runner.start(92, "category", 10, S)
        await until(lambda: env.inflight > 0 and mine.state == "running")
        scan_runner.forget(91)
        gone = scan_runner.get(91)
        await wait(mine)
        env.hold.set()
        await wait(theirs)
        async with env.factory() as db:
            saved = await scan_results.load(db, 91, "category", 10)
            other = await scan_results.load(db, 92, "category", 10)
        return gone, mine.state, saved, scan_runner.get(92), other
    gone, state, saved, kept, other = _run(go())
    assert gone is None and state == "cancelled" and saved is None
    assert kept is not None and kept.state == "done" and other is not None


def test_forget_cancels_a_queued_job(env):
    async def go():
        scan_runner.reset()
        env.hold.clear()
        a = scan_runner.start(91, "category", 10, S)
        b = scan_runner.start(92, "category", 10, S)
        c = scan_runner.start(93, "category", 10, S)
        await until(lambda: c.state == "queued" and a.state == "running")
        scan_runner.forget(93)
        await wait(c)
        env.hold.set()
        await wait(a)
        await wait(b)
        return c.state, scan_runner.get(93)
    assert _run(go()) == ("cancelled", None)


def test_forget_drops_a_finished_job_and_ignores_an_unknown_user(env):
    async def go():
        scan_runner.reset()
        job = scan_runner.start(91, "category", 10, S)
        await wait(job)
        scan_runner.forget(91)
        scan_runner.forget(12345)
        return job.state, scan_runner.get(91)
    assert _run(go()) == ("done", None)


def test_two_jobs_share_one_pacer(env):
    async def go():
        scan_runner.reset()
        a = scan_runner.start(91, "category", 10, S)
        b = scan_runner.start(92, "category", 10, S)
        await wait(a)
        await wait(b)
    _run(go())
    assert env.pacers and all(p is env.pacers[0] for p in env.pacers)


def test_on_done_is_a_fallback_for_a_job_left_unfinished(env):
    async def go():
        ok = asyncio.ensure_future(asyncio.sleep(0))
        await ok
        stuck = scan_runner.Job(91, "category", 10, S, state="running")
        scan_runner._on_done(stuck, ok)
        gone = asyncio.ensure_future(asyncio.sleep(10))
        gone.cancel()
        await asyncio.gather(gone, return_exceptions=True)
        queued = scan_runner.Job(92, "category", 10, S)
        scan_runner._on_done(queued, gone)
        finished = scan_runner.Job(93, "category", 10, S, state="done")
        scan_runner._on_done(finished, gone)
        return stuck, queued, finished
    stuck, queued, finished = _run(go())
    assert (stuck.state, stuck.message) == ("failed", "The scan failed. Scan again.")
    assert queued.state == "cancelled"
    assert finished.state == "done"


def test_a_task_that_cannot_be_created_leaves_no_job(env):
    async def go():
        scan_runner.reset()
        with pytest.MonkeyPatch.context() as mp:
            def boom(coro):
                raise RuntimeError("no task")
            mp.setattr(scan_runner.asyncio, "create_task", boom)
            with pytest.raises(RuntimeError):
                scan_runner.start(91, "category", 10, S)
        return scan_runner.get(91)
    assert _run(go()) is None



def test_rows_are_computed_in_chunks_with_the_loop_free_between(env, monkeypatch):
    env.ids = list(range(3000, 3060))               # 60 items: 25, 25 and 10
    real = scan_results.compute_rows
    sizes, ticks_seen, ticks = [], [], {"n": 0}

    def spy(summaries, type_ids, *rest):
        sizes.append(len(type_ids))
        ticks_seen.append(ticks["n"])
        return real(summaries, type_ids, *rest)
    monkeypatch.setattr(scan_runner.scan_results, "compute_rows", spy)

    async def go():
        async def ticker():
            while True:
                ticks["n"] += 1
                await asyncio.sleep(0)
        t = asyncio.ensure_future(ticker())
        try:
            return await _scan_and_load(env)
        finally:
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)
    job, result = _run(go())
    assert scan_runner.ROWS_AT_ONCE == 25 and sizes == [25, 25, 10]
    assert ticks_seen[0] < ticks_seen[1] < ticks_seen[2]     # other tasks ran between
    assert [r["type_id"] for r in result["rows"]] == env.ids


def test_running_count_counts_only_running_jobs(env):
    async def go():
        scan_runner.reset()
        env.hold.clear()
        a = scan_runner.start(91, "category", 10, S)
        b = scan_runner.start(92, "category", 10, S)
        c = scan_runner.start(93, "category", 10, S)
        right_away = scan_runner.running_count()   # every job starts out queued
        await until(lambda: (a.state, b.state, c.state) == ("running", "running", "queued"))
        during = scan_runner.running_count()
        env.hold.set()
        for j in (a, b, c):
            await wait(j)
        return right_away, during, scan_runner.running_count()
    assert _run(go()) == (0, 2, 0)
