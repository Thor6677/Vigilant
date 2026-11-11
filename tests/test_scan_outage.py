"""Hub Scan during an EVE API outage: what a scan costs the API.

The real runner and the real `ensure_summary` on a temp SQLite database; only
the two upstream fetches (`_fetch_history_esi`, `_fetch_orders_esi`), the
clock, and EVE's price list (another upstream call, not under test) are fake.
Lists are real market categories, expanded from SDE rows in the temp database.

The bounds below allow for requests already in flight when the fifth failure
lands: the shared Pacer lets PACER_CONCURRENT requests be in flight at once,
so up to PACER_CONCURRENT - 1 others have already been sent and count too.
Once the job has stopped, no request is started. Names and figures invented."""
import asyncio
import inspect
import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, User
from app.db.sde_models import SDEMarketGroup, SDEType
from app.esi.market import APPRAISAL_HUBS
from app.esi.rate_limit import LegacyErrorState
from app.market import scan_fetch, scan_results, scan_runner

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
S = scan_results.Settings(5e8, 20, 3.37, 1.5)
USER = 91
STATION_OF = {h["region_id"]: h["station_id"] for h in APPRAISAL_HUBS.values()}
AMARR = APPRAISAL_HUBS["amarr"]["region_id"]
PACER_CONCURRENT = inspect.signature(scan_fetch.Pacer).parameters["concurrent"].default
STOP = scan_runner.STOP_AFTER_FAILED_REQUESTS
BOUND = STOP + PACER_CONCURRENT - 1        # 5 failed + 2 already in flight
# Categories: (market group id, how many items, first type id)
BIG, ONE, TWELVE = (71, 40, 5000), (72, 1, 6000), (73, 12, 7000)


def _ids(group):
    _gid, n, first = group
    return list(range(first, first + n))


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(asyncio.wait_for(coro, 30))
    finally:
        loop.close()
        asyncio.set_event_loop(None)


class Clock:
    def __init__(self):
        self.now, self.step = NOW, timedelta(0)

    def __call__(self):
        now = self.now
        self.now += self.step
        return now


class Api:
    """Both upstream fetches. Every call is recorded as (kind, region, type)."""

    def __init__(self):
        self.calls = []
        self.down = False
        self.fail_types = set()     # these types fail in every region
        self.fail_once = set()      # these (region, type) fail on their first request
        self.after_call = None      # called with the call count, before answering

    async def _call(self, kind, region_id, type_id):
        self.calls.append((kind, region_id, type_id))
        if self.after_call is not None:
            self.after_call(len(self.calls))
        await asyncio.sleep(0)      # in flight: other lookups run meanwhile
        pair = (region_id, type_id)
        if self.down or type_id in self.fail_types:
            raise RuntimeError("503 from the sample API")
        if pair in self.fail_once:
            self.fail_once.discard(pair)
            raise RuntimeError("one blip")
        if kind == "history":
            return [{"date": (NOW.date() - timedelta(days=d)).isoformat(), "average": 100.0,
                     "highest": 110.0, "lowest": 90.0, "volume": 10, "order_count": 4}
                    for d in range(1, 31)]
        return [{"is_buy_order": False, "price": 120.0, "volume_remain": 50,
                 "location_id": STATION_OF[region_id]}]

    async def history(self, region_id, type_id):
        return await self._call("history", region_id, type_id)

    async def orders(self, region_id, type_id):
        return await self._call("orders", region_id, type_id)

    def since(self, n):
        return self.calls[n:]


@pytest.fixture
def world(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'outage.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as db:
            db.add(User(id=USER))
            for gid, n, first in (BIG, ONE, TWELVE):
                db.add(SDEMarketGroup(market_group_id=gid, parent_group_id=None,
                                      market_group_name=f"Sample Goods {gid}"))
                for i in range(n):
                    db.add(SDEType(type_id=first + i, type_name=f"Sample Item {gid}-{i:03d}",
                                   published=True, market_group_id=gid, group_id=1))
            await db.commit()
    _run(setup())

    api, clock = Api(), Clock()

    async def no_price_list(db):
        return {}

    monkeypatch.setattr(scan_runner, "AsyncSessionLocal", factory)
    monkeypatch.setattr(scan_runner, "_now", clock)
    monkeypatch.setattr(scan_runner.market_lp, "get_price_map", no_price_list)
    monkeypatch.setattr(scan_fetch.market_history, "_fetch_history_esi", api.history)
    monkeypatch.setattr(scan_fetch.market_orders, "_fetch_orders_esi", api.orders)
    monkeypatch.setattr(scan_fetch, "_failed", {})
    monkeypatch.setattr(scan_fetch.rate_limit_tracker, "legacy", None)
    scan_runner._jobs.clear()
    scan_runner._slots = scan_runner._pacer = None
    yield api, clock, factory
    scan_runner._jobs.clear()
    scan_runner._slots = scan_runner._pacer = None
    _run(engine.dispose())


async def _fresh_limits():
    scan_runner.reset()
    scan_runner._pacer = scan_fetch.Pacer(pause=0)     # no one-second batch pauses in tests


async def _scan(group):
    job = scan_runner.start(USER, "category", group[0], S)
    try:
        await asyncio.wait_for(asyncio.shield(job.task), 20)
    except asyncio.CancelledError:
        pass
    return job


async def _load(factory, group):
    async with factory() as db:
        return await scan_results.load(db, USER, "category", group[0])


def _stops(caplog):
    return [r for r in caplog.records
            if r.name == scan_runner.__name__ and r.levelno == logging.WARNING
            and "stopped early" in r.getMessage()]


def test_an_outage_costs_a_few_requests_not_one_per_lookup(world, caplog):
    api, _clock, factory = world
    api.down = True
    caplog.set_level(logging.INFO, logger=scan_runner.__name__)

    async def go():
        await _fresh_limits()
        return await _scan(BIG), await _load(factory, BIG)
    job, result = _run(go())
    # 40 items x 5 hubs = 200 lookups (400 requests once both parts are
    # counted); the scan stops after the fifth failed request in a row.
    assert STOP <= len(api.calls) <= BOUND
    assert (job.state, job.message) == ("failed", scan_runner.OUTAGE)
    assert result is None
    (stop,) = _stops(caplog)
    text = stop.getMessage()
    assert "user 91" in text and "category 71" in text
    assert f"{len(api.calls)} requests" in text and scan_runner.STOPPED_REQUESTS in text
    infos = [r.getMessage() for r in caplog.records
             if r.name == scan_runner.__name__ and r.levelno == logging.INFO]
    assert any(m.startswith("hub scan started: user 91, category 71, 40 items") for m in infos)
    (end,) = [m for m in infos if m.startswith("hub scan ended")]
    assert f"40 items, {len(api.calls)} requests" in end and end.endswith("failed")


def test_a_rescan_during_the_outage_costs_no_more_and_skips_remembered_lookups(world):
    api, clock, factory = world
    api.down = True

    async def go():
        await _fresh_limits()
        first = await _scan(BIG)
        made_first = len(api.calls)
        remembered = set(scan_fetch._failed)
        clock.now += timedelta(seconds=5)           # pressed Rescan straight away
        second = await _scan(BIG)
        return first, second, made_first, remembered
    first, second, made_first, remembered = _run(go())
    again = api.since(made_first)
    assert made_first <= BOUND and len(again) <= BOUND
    assert remembered and len(remembered) == made_first     # one pair per failed request
    # No remembered lookup is requested again inside the window.
    assert not any((region, type_id) in remembered for _kind, region, type_id in again)
    assert second.state == "failed" and second.message == scan_runner.OUTAGE


def test_after_an_item_fails_its_other_lookups_are_not_started(world, monkeypatch):
    api, _clock, factory = world
    events = []
    gate = asyncio.Event()

    async def history(region_id, type_id):
        events.append(("start", region_id))
        if len(events) == 1:
            # Fail only once the pacer's other slots are in flight too.
            for _ in range(400):
                if len(events) >= PACER_CONCURRENT:
                    break
                await asyncio.sleep(0.005)
            events.append(("fail", region_id))
            raise RuntimeError("503 from the sample API")
        try:
            await gate.wait()                       # held until cancelled
        except asyncio.CancelledError:
            events.append(("cancelled", region_id))
            raise
        return []
    monkeypatch.setattr(scan_fetch.market_history, "_fetch_history_esi", history)

    async def go():
        await _fresh_limits()
        return await _scan(ONE)
    job = _run(go())
    fail_at = events.index(next(e for e in events if e[0] == "fail"))
    started = [region for kind, region in events if kind == "start"]
    assert all(kind != "start" for kind, _region in events[fail_at:])
    # Three in flight (the pacer's limit), two of the five never started.
    assert len(started) == PACER_CONCURRENT < len(APPRAISAL_HUBS)
    # The two in flight beside the failure were cancelled, none finished; the
    # retry pass made no request (the failed lookup is remembered).
    held = started[1:]
    assert len(held) == 2
    assert sorted(r for kind, r in events if kind == "cancelled") == sorted(held)
    assert set(scan_fetch._failed) == {(started[0], _ids(ONE)[0])}
    assert (job.state, job.message) == ("failed", scan_runner.OUTAGE)


def test_a_transient_failure_does_not_stop_the_scan_and_a_rescan_after_the_window_gets_it(
        world, caplog):
    """The retry pass never waits out the failure memory: inside the window the
    item is reported unavailable, and "retry these" (a rescan) after it
    fetches only what is missing."""
    api, clock, factory = world
    ids = _ids(TWELVE)
    flaky = [ids[0], ids[4], ids[8]]                # one per chunk of four items
    api.fail_once = {(AMARR, t) for t in flaky}

    async def go():
        await _fresh_limits()
        first_job = await _scan(TWELVE)
        first = await _load(factory, TWELVE)
        made_first = len(api.calls)
        clock.now += scan_fetch.FAILURE_TTL
        second_job = await _scan(TWELVE)
        return first_job, first, made_first, second_job, await _load(factory, TWELVE)
    first_job, first, made_first, second_job, second = _run(go())
    assert first_job.state == "done" and first["status"] == "complete"
    assert first["item_scanned"] == 9
    assert sorted(r["type_id"] for r in first["rows"] if r["status"] == "unavailable") == flaky
    assert _stops(caplog) == []
    again = api.since(made_first)
    assert again and {type_id for _kind, _region, type_id in again} == set(flaky)
    assert second_job.state == "done" and second["status"] == "complete"
    assert second["item_scanned"] == 12
    assert all(r["status"] != "unavailable" for r in second["rows"])


def test_the_retry_pass_retries_an_item_whose_failure_has_expired(world):
    """A long scan: by the retry pass the item's failure is over 90 s old, so
    it is fetched again there and the scan completes."""
    api, clock, factory = world
    ids = _ids(TWELVE)
    api.fail_once = {(AMARR, ids[0])}
    clock.step = timedelta(seconds=2)               # each look at the clock is 2 s later

    async def go():
        await _fresh_limits()
        job = await _scan(TWELVE)
        return job, await _load(factory, TWELVE)
    job, result = _run(go())
    assert job.state == "done" and result["status"] == "complete"
    assert result["item_scanned"] == 12
    assert sum(1 for kind, region, t in api.calls
               if (kind, region, t) == ("history", AMARR, ids[0])) == 2


def test_successes_then_an_outage_save_an_incomplete_result(world, caplog):
    api, _clock, factory = world
    ids = _ids(BIG)
    api.fail_types = set(ids[8:])                   # 8 items fine, then the API goes down

    async def go():
        await _fresh_limits()
        return await _scan(BIG), await _load(factory, BIG)
    job, result = _run(go())
    assert job.state == "done" and result["status"] == "incomplete"
    assert result["item_total"] == 40 and result["item_scanned"] == 8
    by_id = {r["type_id"]: r["status"] for r in result["rows"]}
    assert all(by_id[t] != "unavailable" for t in ids[:8])
    assert all(by_id[t] == "unavailable" for t in ids[8:])
    good = 8 * len(APPRAISAL_HUBS) * 2                  # history and orders per hub
    assert good + STOP <= len(api.calls) <= good + BOUND   # no retry pass after a stop
    assert len(_stops(caplog)) == 1


def _low_budget():
    return LegacyErrorState(remaining=scan_fetch.ERROR_BUDGET_FLOOR - 1, reset_in_seconds=60,
                            last_updated=datetime.now(timezone.utc))


def test_a_low_error_budget_stops_the_scan_without_a_request(world, monkeypatch, caplog):
    api, _clock, factory = world
    monkeypatch.setattr(scan_fetch.rate_limit_tracker, "legacy", _low_budget())

    async def go():
        await _fresh_limits()
        return await _scan(BIG), await _load(factory, BIG)
    job, result = _run(go())
    assert api.calls == []
    assert (job.state, job.message) == ("failed", scan_runner.OUTAGE) and result is None
    (stop,) = _stops(caplog)
    assert scan_runner.STOPPED_BUDGET in stop.getMessage()
    assert scan_fetch._failed == {}                 # nothing failed: nothing remembered


def test_a_budget_that_runs_low_mid_scan_stops_it_there(world, monkeypatch):
    api, _clock, factory = world
    ids = _ids(BIG)
    first_chunk = scan_runner.ITEMS_AT_ONCE * len(APPRAISAL_HUBS) * 2

    def run_low(n):
        if n == first_chunk:                        # the first four items' last request
            monkeypatch.setattr(scan_fetch.rate_limit_tracker, "legacy", _low_budget())
    api.after_call = run_low

    async def go():
        await _fresh_limits()
        return await _scan(BIG), await _load(factory, BIG)
    job, result = _run(go())
    assert len(api.calls) == first_chunk
    assert result["status"] == "incomplete" and result["item_scanned"] == 4
    assert {r["type_id"] for r in result["rows"] if r["status"] != "unavailable"} == set(ids[:4])
