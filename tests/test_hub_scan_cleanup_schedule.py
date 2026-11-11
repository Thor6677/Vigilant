"""The scheduler's daily Hub Scan cleanup (app/routes/dashboard.py)."""
import asyncio
import inspect
from datetime import timezone

from app.market import scan_results
from app.routes import dashboard


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_scheduler_runs_the_cleanup_in_its_daily_block():
    src = inspect.getsource(dashboard._background_scheduler)
    assert "await _cleanup_hub_scan()" in src


def test_cleanup_gets_an_aware_utc_now(monkeypatch):
    seen = []

    async def fake(db, now):
        seen.append(now)
        return 3, 1

    monkeypatch.setattr(dashboard, "AsyncSessionLocal", _Session)
    monkeypatch.setattr(scan_results, "cleanup", fake)
    _run(dashboard._cleanup_hub_scan())
    assert len(seen) == 1 and seen[0].tzinfo is not None
    assert seen[0].utcoffset() == timezone.utc.utcoffset(None)


def test_a_failing_cleanup_is_logged_and_swallowed(monkeypatch, caplog):
    async def boom(db, now):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(dashboard, "AsyncSessionLocal", _Session)
    monkeypatch.setattr(scan_results, "cleanup", boom)
    with caplog.at_level("WARNING"):
        _run(dashboard._cleanup_hub_scan())          # does not raise
    assert "Hub Scan cleanup failed" in caplog.text
