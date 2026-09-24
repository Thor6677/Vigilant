"""Tests for the ISS-032 stale-while-revalidate cache around the ESI
/status/ call (`app.routes.dashboard.fetch_server_status`).

Exercises the real coroutine against an `httpx.MockTransport` fake — no
mocking of `fetch_server_status` itself, no real network. Time is supplied
by an injectable fake clock, and background refreshes are awaited directly
(by capturing the task `asyncio.create_task` hands back) rather than by
sleeping, so nothing here is time- or timing-dependent.

Sync-style (no pytest-asyncio): a single manually-managed event loop per
test, per tests/test_sync_field_sessions.py. Each test's whole scenario runs
inside one coroutine passed to `_run` — a background-refresh task created by
`asyncio.create_task` is bound to the loop that created it, so awaiting it
later has to happen on that same loop rather than a fresh one.
"""
import asyncio

import httpx

import app.routes.dashboard as dash


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


class _FakeClock:
    """Controllable monotonic-style clock. Starts away from 0 so a bug that
    treats "no fetched_at yet" as 0 would show up as a bogus huge age."""

    def __init__(self, start: float = 1_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _reset_cache(monkeypatch):
    monkeypatch.setattr(dash, "_server_status_cache", None)
    monkeypatch.setattr(dash, "_server_status_revalidating", False)
    monkeypatch.setattr(dash, "_server_status_refresh_tasks", set())


def _capture_tasks(monkeypatch):
    """Let create_task run for real, but keep a handle to every task this
    module schedules so a test can `await` it deterministically instead of
    sleeping to let the background refresh land."""
    created = []
    real_create_task = asyncio.create_task

    def _capturing(coro):
        task = real_create_task(coro)
        created.append(task)
        return task

    monkeypatch.setattr(dash.asyncio, "create_task", _capturing)
    return created


def _transport(responses):
    """responses: list popped one-per-call; an Exception instance is raised
    instead of returned. The last entry repeats once exhausted."""
    calls = []

    async def handler(request):
        calls.append(request)
        item = responses.pop(0) if len(responses) > 1 else responses[0]
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.MockTransport(handler), calls


def _ok(players):
    return httpx.Response(200, json={"players": players})


def test_first_call_fetches_and_second_call_within_ttl_reuses_it(monkeypatch):
    _reset_cache(monkeypatch)
    clock = _FakeClock()
    transport, calls = _transport([_ok(1000)])

    async def _body():
        first = await dash.fetch_server_status(clock=clock, transport=transport)
        assert first == {"online": True, "players": 1000, "status": "online"}
        assert len(calls) == 1

        clock.advance(dash._SERVER_STATUS_TTL - 1)
        second = await dash.fetch_server_status(clock=clock, transport=transport)
        assert second == {"online": True, "players": 1000, "status": "online"}
        assert len(calls) == 1  # no second ESI call

    _run(_body())


def test_call_after_ttl_serves_stale_value_and_fires_one_refresh(monkeypatch):
    _reset_cache(monkeypatch)
    clock = _FakeClock()
    tasks = _capture_tasks(monkeypatch)
    transport, calls = _transport([_ok(1000), _ok(2000)])

    async def _body():
        await dash.fetch_server_status(clock=clock, transport=transport)
        clock.advance(dash._SERVER_STATUS_TTL + 1)

        stale = await dash.fetch_server_status(clock=clock, transport=transport)
        assert stale == {"online": True, "players": 1000, "status": "online"}  # stale value, served immediately
        assert len(tasks) == 1
        assert len(calls) == 1  # background refresh hasn't run yet

        # A second call while the refresh is still in flight must not
        # schedule a second one.
        again = await dash.fetch_server_status(clock=clock, transport=transport)
        assert again == {"online": True, "players": 1000, "status": "online"}
        assert len(tasks) == 1

        await asyncio.gather(*tasks)
        assert len(calls) == 2
        assert dash._server_status_revalidating is False

        refreshed = await dash.fetch_server_status(clock=clock, transport=transport)
        assert refreshed == {"online": True, "players": 2000, "status": "online"}
        assert len(tasks) == 1  # value is fresh again, no new refresh needed

    _run(_body())


def test_background_refresh_task_is_pinned_by_production_code(monkeypatch):
    """Guards the module's own strong reference to the refresh task (not the
    harness's `_capture_tasks` list, which is itself a strong reference and
    so can't catch this). asyncio.create_task() only holds a *weak*
    reference; if production code dropped the return value, the task could
    be garbage-collected before it ever starts, its `finally` would never
    run, and _server_status_revalidating would stay True forever — wedging
    the cache permanently onto the offline/unknown shape. This checks
    dash._server_status_refresh_tasks directly: non-empty while the refresh
    is in flight, drained by its own done-callback once it completes."""
    _reset_cache(monkeypatch)
    clock = _FakeClock()
    transport, calls = _transport([_ok(1000), _ok(2000)])

    async def _body():
        await dash.fetch_server_status(clock=clock, transport=transport)
        clock.advance(dash._SERVER_STATUS_TTL + 1)

        await dash.fetch_server_status(clock=clock, transport=transport)
        assert len(dash._server_status_refresh_tasks) == 1
        task = next(iter(dash._server_status_refresh_tasks))

        await task
        assert dash._server_status_refresh_tasks == set()
        assert dash._server_status_revalidating is False

    _run(_body())


def test_value_older_than_staleness_cap_returns_unknown_shape(monkeypatch):
    _reset_cache(monkeypatch)
    clock = _FakeClock()
    tasks = _capture_tasks(monkeypatch)
    # Every refresh attempt fails, so the cached value just keeps aging.
    transport, calls = _transport([_ok(1000), httpx.ConnectError("down")])

    async def _body():
        await dash.fetch_server_status(clock=clock, transport=transport)
        clock.advance(dash._SERVER_STATUS_MAX_STALE + 1)

        result = await dash.fetch_server_status(clock=clock, transport=transport)
        assert result == {"online": False, "players": None, "status": "unknown"}
        await asyncio.gather(*tasks)  # drain the refresh this call kicked off

    _run(_body())


def test_esi_failure_on_refresh_keeps_serving_last_good_value(monkeypatch):
    _reset_cache(monkeypatch)
    clock = _FakeClock()
    tasks = _capture_tasks(monkeypatch)
    transport, calls = _transport([_ok(1000), httpx.ConnectError("down")])

    async def _body():
        await dash.fetch_server_status(clock=clock, transport=transport)
        clock.advance(dash._SERVER_STATUS_TTL + 1)  # stale, nowhere near the staleness cap

        stale = await dash.fetch_server_status(clock=clock, transport=transport)
        assert stale == {"online": True, "players": 1000, "status": "online"}
        assert len(tasks) == 1

        await asyncio.gather(*tasks)  # refresh runs and fails
        assert dash._server_status_cache["data"] == {"online": True, "players": 1000, "status": "online"}
        assert dash._server_status_revalidating is False

        still_good = await dash.fetch_server_status(clock=clock, transport=transport)
        assert still_good == {"online": True, "players": 1000, "status": "online"}

        # That last call is itself stale, so it kicked off another retry —
        # drain it so nothing is left pending when the loop closes.
        await asyncio.gather(*tasks)

    _run(_body())


# ── unknown is not offline ──────────────────────────────────────────────────

def test_esi_timeout_on_cold_start_is_unknown_not_offline(monkeypatch):
    """The first poll after a deploy races app boot and can time out. That
    is "we don't know", and must not be painted as Tranquility being down."""
    _reset_cache(monkeypatch)
    transport, _ = _transport([httpx.ReadTimeout("slow")])

    async def _body():
        result = await dash.fetch_server_status(clock=_FakeClock(), transport=transport)
        assert result["status"] == "unknown"
        assert result["online"] is False
        assert dash._server_status_cache is None, "a failure must not be cached"

    _run(_body())


def test_esi_503_is_a_genuine_offline(monkeypatch):
    """ESI answers 503 for the status endpoint during downtime — the one
    answer that means Tranquility itself is unavailable."""
    _reset_cache(monkeypatch)
    transport, _ = _transport([httpx.Response(503, json={"error": "datasource unavailable"})])

    async def _body():
        result = await dash.fetch_server_status(clock=_FakeClock(), transport=transport)
        assert result == {"online": False, "players": None, "status": "offline"}

    _run(_body())


def test_other_esi_errors_are_unknown(monkeypatch):
    _reset_cache(monkeypatch)
    transport, _ = _transport([httpx.Response(502, text="bad gateway")])

    async def _body():
        result = await dash.fetch_server_status(clock=_FakeClock(), transport=transport)
        assert result["status"] == "unknown"

    _run(_body())


def test_dashboard_renders_unknown_neutrally_and_retries_soon():
    """The pill has three states and only ESI's own "offline" is red. An
    unknown answer schedules a 30 s retry instead of waiting out the
    fifteen-minute poll."""
    import os, re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "app/templates/dashboard.html"), encoding="utf-8") as fh:
        body = fh.read()
    fn = re.search(r"function updateServerStatus\(\) \{(.*?)\n\}", body, re.S).group(1)
    assert "data.status === 'offline'" in fn, "OFFLINE must be gated on the explicit status"
    assert "TRANQUILITY — CHECKING…" in fn
    assert fn.count("scheduleServerStatusRetry()") == 2, "retry on unknown AND on fetch failure"
    assert re.search(r"scheduleServerStatusRetry[\s\S]*?30 \* 1000", body)
