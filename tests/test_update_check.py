"""Tests for the in-app update checker.

The load-bearing property is NOTIFY EXACTLY ONCE PER RELEASE, surviving
restarts — hence `notified_tag` in the database rather than an in-memory flag.
The second property is that a "dev" build is silent, which falls out of
is_newer() failing closed rather than from a dedicated flag.

Sync-style (no pytest-asyncio): a single manually-managed event loop, per
tests/test_discord_alert_relay.py.
"""
import asyncio

import app.ops.update_check as uc


class _FakeSettings:
    def __init__(self, version="v1.0.0"):
        self.version = version
        self.update_check_enabled = True
        self.update_check_repo = "owner/repo"
        self.contact_email = "a@example.com"


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _State:
    """Stand-in for the persisted UpdateStatus row."""

    def __init__(self):
        self.latest_tag = None
        self.latest_url = None
        self.latest_body = None
        self.latest_published_at = None
        self.checked_at = None
        self.notified_tag = None
        self.last_error = None


def _release(tag="v1.1.0"):
    return {"tag_name": tag, "html_url": f"https://example.com/{tag}",
            "body": "notes", "published_at": "2026-07-26T00:00:00Z"}


def _decide(state, release, version="v1.0.0"):
    return uc.decide(state, release, _FakeSettings(version=version))


def test_new_release_is_announced():
    st = _State()
    action = _decide(st, _release("v1.1.0"))
    assert action.notify is True
    assert action.tag == "v1.1.0"


def test_same_release_is_not_reannounced():
    st = _State()
    st.notified_tag = "v1.1.0"
    assert _decide(st, _release("v1.1.0")).notify is False


def test_newer_release_after_a_notification_announces_again():
    st = _State()
    st.notified_tag = "v1.1.0"
    assert _decide(st, _release("v1.2.0")).notify is True


def test_dev_build_never_announces():
    st = _State()
    assert _decide(st, _release("v9.9.9"), version="dev").notify is False


def test_older_or_equal_release_does_not_announce():
    st = _State()
    assert _decide(st, _release("v1.0.0")).notify is False
    assert _decide(st, _release("v0.9.0")).notify is False


def test_poll_swallows_transport_errors(monkeypatch):
    """A dead GitHub must never propagate out of the checker.

    monkeypatch, not a bare assignment: an unrestored module global would leak
    the broken fetch into every test that runs after this one.
    """
    async def _boom(*a, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(uc, "_fetch_latest_release", _boom)
    # Must not raise.
    _run(uc.check_once())


def test_two_polls_of_the_same_release_send_exactly_once(monkeypatch):
    """The whole point of the notified_tag COLUMN, exercised end to end.

    Every other test here drives decide(), which is pure and never touches the
    database — so none of them would notice if _load_state, the row-creation
    path, or the commit ordering were broken. This one runs check_once() twice
    against the real (hermetic, per tests/conftest.py) database and asserts a
    single send. Without it, "notify exactly once, surviving restarts" is a
    claim with no coverage.
    """
    sends = []

    async def _fake_release(*a, **kw):
        return _release("v1.1.0")

    async def _fake_send(**kwargs):
        sends.append(kwargs)

    monkeypatch.setattr(uc, "_fetch_latest_release", _fake_release)
    monkeypatch.setattr(uc, "send_discord_alert", _fake_send)
    monkeypatch.setattr(uc, "get_settings", lambda: _FakeSettings(version="v1.0.0"))

    async def _both():
        # Each check_once() opens and closes its own session, so the second
        # call sees the notified_tag the first one committed.
        await uc.check_once()
        await uc.check_once()
        from app.db.models import engine
        # Same reason as conftest.pytest_configure: every test drives its own
        # event loop, so a pooled connection pinned to this one is unusable
        # from the next.
        await engine.dispose()

    _run(_both())

    assert len(sends) == 1, f"expected one announcement, got {len(sends)}"
    assert sends[0]["alert_type"] == uc.ALERT_TYPE
    assert "v1.1.0" in sends[0]["title"]
