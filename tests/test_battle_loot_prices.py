"""T-082: the Jita pricing job. ESI is stubbed; sleeps are recorded, not slept."""
import asyncio
from datetime import datetime, timezone

import pytest

from app.esi import market
from app.esi.rate_limit import rate_limit_tracker
from app.intel import battle_loot_prices as bp

TYPE_BASE = 91000


@pytest.fixture(autouse=True)
def clean_state():
    bp._cache.clear()
    bp._budget_calls.clear()
    bp._jobs.clear()
    bp._job_lock = None
    rate_limit_tracker.recent_rejections.clear()
    yield
    bp._cache.clear()
    bp._budget_calls.clear()
    bp._jobs.clear()
    rate_limit_tracker.recent_rejections.clear()


def _items(types, dropped=1):
    return {1: [(t, 0, dropped, False, 5) for t in types]}


# ── candidates ────────────────────────────────────────────────────────────

def test_candidates_are_dropped_non_blueprint_types_ranked_by_value():
    items = {1: [(11, 5, 0, False, 5),          # destroyed only: never Jita-priced
                 (12, 0, 1, False, 5), (13, 0, 10, False, 5), (14, 0, 1, False, 5),
                 (15, 0, 1, False, 5)],         # a blueprint
            2: [(13, 0, 5, False, 5)]}          # the same type in another wreck
    avg = {11: 1e9, 12: 100.0, 13: 20.0, 14: 400.0, 15: 1e9}
    c = bp.rank_candidates(items, avg, {15})
    assert c.types == (14, 13, 12)               # 400, 15x20=300, 100
    assert c.total == 3 and c.coverage == 1.0


def test_types_without_an_average_go_last():
    c = bp.rank_candidates(_items([21, 22]), {22: 5.0}, set())
    assert c.types == (22, 21)


def test_equal_values_rank_by_quantity_not_type_id():
    """No averages at all (the price list failed): the cap must not be a type-id lottery."""
    items = {1: [(11, 0, 1, False, 5), (12, 0, 50, False, 5), (13, 0, 7, False, 5)]}
    assert bp.rank_candidates(items, {}, set()).types == (12, 13, 11)


def test_the_cap_and_its_coverage():
    types = list(range(TYPE_BASE, TYPE_BASE + 200))
    avg = {t: float(t - TYPE_BASE + 1) for t in types}
    c = bp.rank_candidates(_items(types), avg, set())
    assert len(c.types) == bp.JITA_TYPE_CAP == 150 and c.total == 200
    assert c.types[0] == TYPE_BASE + 199
    assert 0.9 < c.coverage < 1.0


# ── quote helper ──────────────────────────────────────────────────────────

class FakeClient:
    def __init__(self, orders=None, exc=None):
        self.orders, self.exc, self.calls = orders, exc, []

    async def get_public(self, path, params=None, bypass_cache=False):
        self.calls.append((path, params))
        if self.exc:
            raise self.exc
        return self.orders


def _quote(client, tid=34):
    return asyncio.run(market.get_hub_sell_quote(client, 10000002, 60003760, tid))


def test_quote_keeps_only_the_station_and_takes_the_lowest():
    c = FakeClient([{"location_id": 60003760, "price": 7.0}, {"location_id": 60003760, "price": 5.0},
                    {"location_id": 1, "price": 1.0}])
    assert _quote(c) == ("ok", 5.0)
    assert c.calls == [("/markets/10000002/orders/", {"type_id": 34, "order_type": "sell"})]


def test_quote_tells_no_order_from_a_failure_and_never_raises():
    assert _quote(FakeClient([{"location_id": 1, "price": 1.0}])) == ("none", None)
    assert _quote(FakeClient([])) == ("none", None)
    assert _quote(FakeClient(exc=RuntimeError("boom"))) == ("error", None)
    assert _quote(FakeClient({"error": "x"})) == ("error", None)


# ── the job ───────────────────────────────────────────────────────────────

@pytest.fixture
def esi(monkeypatch):
    """Stub the quote helper and asyncio.sleep; record the pacing."""
    rec = {"sleeps": [], "active": 0, "peak": 0, "calls": [], "batches": [], "result": lambda t: ("ok", float(t))}

    async def fake_quote(client, region, station, type_id):
        rec["active"] += 1
        rec["peak"] = max(rec["peak"], rec["active"])
        await asyncio.sleep(0)
        rec["calls"].append(type_id)
        rec["active"] -= 1
        return rec["result"](type_id)

    real_sleep = asyncio.sleep

    async def fake_sleep(s, *a):
        if s:
            rec["sleeps"].append(s)
        await real_sleep(0)

    monkeypatch.setattr(bp, "get_hub_sell_quote", fake_quote)
    monkeypatch.setattr(bp.asyncio, "sleep", fake_sleep)
    return rec


async def _wait(job):
    for _ in range(2000):
        if job.state == "done":
            return
        await asyncio.sleep(0)
    raise AssertionError("job never finished")


def _run_job(types):
    async def go():
        job = bp.ensure_job("k", types)
        await _wait(job)
        return job
    return asyncio.run(go())


def test_pacing_batches_of_ten_three_at_a_time_one_second_apart(esi):
    types = list(range(TYPE_BASE, TYPE_BASE + 25))
    job = _run_job(types)
    assert sorted(esi["calls"]) == types
    assert esi["peak"] <= bp.CONCURRENCY == 3
    assert esi["sleeps"] == [1.0, 1.0]            # three batches (10, 10, 5): two pauses
    assert (job.done, job.total, job.failed) == (25, 25, 0)


def test_prices_are_cached_so_a_second_job_makes_no_calls(esi):
    types = list(range(TYPE_BASE, TYPE_BASE + 5))
    _run_job(types)
    assert bp.ensure_job("k2", types) is None
    assert len(esi["calls"]) == 5
    assert bp.cached_prices(types)[TYPE_BASE] == ("ok", float(TYPE_BASE))


def test_errors_expire_after_a_minute_but_prices_do_not(esi, monkeypatch):
    esi["result"] = lambda t: ("error", None) if t == TYPE_BASE else ("ok", 1.0)
    types = [TYPE_BASE, TYPE_BASE + 1]
    job = _run_job(types)
    assert job.failed == 1
    assert set(bp.cached_prices(types)) == set(types)
    real = bp._now
    monkeypatch.setattr(bp, "_now", lambda: real() + bp.TTL_ERROR_SECONDS + 1)
    assert set(bp.cached_prices(types)) == {TYPE_BASE + 1}
    monkeypatch.setattr(bp, "_now", lambda: real() + bp.TTL_OK_SECONDS + 1)
    assert bp.cached_prices(types) == {}


def test_the_budget_stops_the_job_and_marks_the_rest(esi, monkeypatch):
    monkeypatch.setattr(bp, "BUDGET_CALLS", 12)
    types = list(range(TYPE_BASE, TYPE_BASE + 30))
    job = _run_job(types)
    assert len(esi["calls"]) == 12
    assert job.budget_hit and job.budget_resume_at is not None
    cached = bp.cached_prices(types)
    assert sum(1 for s, _ in cached.values() if s == "budget") == 18


def test_a_rise_in_rejections_between_batches_ends_the_job(esi):
    """get_public absorbs a first 429, so the quote looks fine. The job has to
    watch the tracker itself."""
    from app.esi.client import rate_limit_tracker as tracker
    assert tracker is rate_limit_tracker
    calls_before_429 = []

    def result(t):
        calls_before_429.append(t)
        if len(calls_before_429) == 10:           # the first batch completes...
            rate_limit_tracker.update_from_response("/markets/10000002/orders/", 429, {"retry-after": "1"})
        return ("ok", 1.0)
    esi["result"] = result
    types = list(range(TYPE_BASE, TYPE_BASE + 40))
    job = _run_job(types)
    assert rate_limit_tracker.recent_rejection_count() == 1
    assert job.stopped_early and len(esi["calls"]) == 10
    cached = bp.cached_prices(types)
    assert sum(1 for s, _ in cached.values() if s == "error") == 30
    assert job.failed == 30


def test_a_second_trigger_for_the_same_report_joins_the_running_job(esi):
    async def go():
        types = list(range(TYPE_BASE, TYPE_BASE + 5))
        a = bp.ensure_job("same", types)
        b = bp.ensure_job("same", types)
        await _wait(a)
        return a, b
    a, b = asyncio.run(go())
    assert a is b and len(esi["calls"]) == 5


def test_a_second_report_waits_for_the_first(esi):
    order = []
    original = esi["result"]

    def result(t):
        order.append(t)
        return original(t)
    esi["result"] = result

    async def go():
        a = bp.ensure_job("one", list(range(TYPE_BASE, TYPE_BASE + 12)))
        b = bp.ensure_job("two", list(range(TYPE_BASE + 100, TYPE_BASE + 105)))
        await asyncio.sleep(0)
        assert b.state == "queued"
        await _wait(a)
        await _wait(b)
    asyncio.run(go())
    assert max(order[:12]) < TYPE_BASE + 100 <= min(order[12:])


def test_no_job_when_there_is_nothing_to_price():
    async def go():
        return bp.ensure_job("none", [])
    assert asyncio.run(go()) is None


def test_priced_at_reports_the_oldest_fresh_price(esi):
    assert bp.priced_at([TYPE_BASE]) is None
    _run_job([TYPE_BASE])
    stamp = bp.priced_at([TYPE_BASE])
    assert abs(stamp - datetime.now(timezone.utc).timestamp()) < 5


def test_a_rejection_is_noticed_even_when_the_count_cannot_rise(esi):
    """The tracker prunes old entries and caps its deque, so a count can stay
    level while ESI is rejecting. The job compares timestamps."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    rate_limit_tracker.recent_rejections.extend(now - timedelta(seconds=1) for _ in range(500))
    assert rate_limit_tracker.recent_rejections.maxlen == 500
    calls = []

    def result(t):
        calls.append(t)
        if len(calls) == 10:
            rate_limit_tracker.update_from_response("/markets/10000002/orders/", 429, {"retry-after": "1"})
        return ("ok", 1.0)
    esi["result"] = result
    count_before = rate_limit_tracker.recent_rejection_count()
    job = _run_job(list(range(TYPE_BASE, TYPE_BASE + 40)))
    assert rate_limit_tracker.recent_rejection_count() == count_before == 500     # the count never moved
    assert job.stopped_early and len(esi["calls"]) == 10


def test_old_rejections_before_the_job_do_not_stop_it(esi):
    from datetime import timedelta
    rate_limit_tracker.recent_rejections.append(datetime.now(timezone.utc) - timedelta(seconds=30))
    job = _run_job(list(range(TYPE_BASE, TYPE_BASE + 25)))
    assert not job.stopped_early and len(esi["calls"]) == 25


def test_a_crashed_job_is_not_reported_as_rate_limited_and_backs_off(esi, monkeypatch):
    def boom(t):
        raise RuntimeError("bug")
    esi["result"] = boom
    types = list(range(TYPE_BASE, TYPE_BASE + 5))

    async def go():
        job = bp.ensure_job("crash", types)
        await _wait(job)
        again = bp.ensure_job("crash", types)       # types are 'error' for 60 s, but the backoff is the guard
        return job, again
    job, again = asyncio.run(go())
    assert job.crashed and not job.stopped_early
    assert again is None
    real = bp._now
    esi["result"] = lambda t: ("ok", 1.0)
    monkeypatch.setattr(bp, "_now", lambda: real() + bp.CRASH_BACKOFF_SECONDS + 1)

    async def restart():
        j = bp.ensure_job("crash", types)
        await _wait(j)
        return j
    assert not asyncio.run(restart()).crashed
