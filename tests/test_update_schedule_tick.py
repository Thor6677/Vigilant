"""The scheduler's actual firing behaviour, against a real database.

test_update_schedule.py covers the arithmetic. This covers the part that writes:
that a fire is recorded in the same breath as the submit, that the recording
actually prevents a second fire, and that an automatic failure pauses the policy
instead of retrying the same bad release every week.
"""
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.db.models import AsyncSessionLocal, UpdatePolicy, UpdateSchedule, UpdateStatus
from app.ops import update_schedule as us

UTC = timezone.utc


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture
def control(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGILANT_CONTROL_DIR", str(tmp_path))

    async def reset():
        async with AsyncSessionLocal() as db:
            await db.execute(delete(UpdatePolicy))
            await db.execute(delete(UpdateSchedule))
            await db.execute(delete(UpdateStatus))
            await db.commit()
    _run(reset())
    return tmp_path


def _beat(control, current_tag="v1.2.0"):
    (control / "updater.json").write_text(json.dumps({
        "version": "v1.2.0", "current_tag": current_tag, "targets": [],
        "checks": {"socket": "ok"},
    }))


def _latest(tag):
    async def go():
        async with AsyncSessionLocal() as db:
            db.add(UpdateStatus(id=1, latest_tag=tag))
            await db.commit()
    _run(go())


def _set_policy(**kw):
    async def go():
        async with AsyncSessionLocal() as db:
            p = await us.get_policy(db)
            for k, v in kw.items():
                setattr(p, k, v)
            await db.commit()
    _run(go())


def _policy():
    async def go():
        async with AsyncSessionLocal() as db:
            return (await db.execute(select(UpdatePolicy))).scalars().first()
    return _run(go())


def _request(control):
    p = control / "request.json"
    return json.loads(p.read_text()) if p.exists() else None


# Sunday 04:30 UTC — half an hour into an 04:00 window, comfortably inside the
# 2h grace, so these tests exercise the decision rather than the grace boundary
# (which test_update_schedule.py covers directly).
SUNDAY_0430 = datetime(2026, 9, 13, 4, 30, tzinfo=UTC)


# ── Preconditions ────────────────────────────────────────────────────────────

def test_tick_does_nothing_without_an_updater(control):
    assert _run(us.tick(SUNDAY_0430)) == "no updater"


def test_tick_does_nothing_while_a_run_is_in_flight(control):
    _beat(control)
    started = (datetime.now(UTC) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    (control / "status.json").write_text(json.dumps({"state": "running", "started_at": started}))
    _set_policy(enabled=True, weekday=6, local_time="04:00", timezone="UTC", patch_only=False)
    _latest("v1.3.0")
    assert _run(us.tick(SUNDAY_0430)) == "busy"
    assert _request(control) is None


def test_tick_does_not_stack_on_an_unclaimed_request(control):
    _beat(control)
    (control / "request.json").write_text(json.dumps({"id": "x", "action": "update"}))
    _set_policy(enabled=True, weekday=6, local_time="04:00", timezone="UTC", patch_only=False)
    _latest("v1.3.0")
    assert _run(us.tick(SUNDAY_0430)) == "a request is already queued"


def test_tick_is_inert_with_everything_off(control):
    """The default install: updater running, nothing scheduled, policy off."""
    _beat(control)
    _latest("v1.3.0")
    assert _run(us.tick(SUNDAY_0430)) == "disabled"
    assert _request(control) is None


# ── The policy fires, once ───────────────────────────────────────────────────

def test_policy_fires_and_records_the_window(control):
    _beat(control, current_tag="v1.2.0")
    _set_policy(enabled=True, weekday=6, local_time="04:00", timezone="UTC", patch_only=False)
    _latest("v1.3.0")

    assert _run(us.tick(SUNDAY_0430)) == "fired policy"
    req = _request(control)
    assert req["action"] == "update" and req["tag"] == "v1.3.0"

    p = _policy()
    assert p.last_fired_window == "2026-09-13"
    assert p.last_fired_tag == "v1.3.0"
    assert p.awaiting_request_id == req["id"]


def test_the_restart_loop_is_guarded(control):
    """THE failure mode. The app fires, the update recreates the app, the app
    comes back inside the same window and ticks again.

    Simulated exactly: fire, then clear the request as the sidecar would on
    claim, then tick again with the same window still open.
    """
    _beat(control, current_tag="v1.2.0")
    _set_policy(enabled=True, weekday=6, local_time="04:00", timezone="UTC", patch_only=False)
    _latest("v1.3.0")

    assert _run(us.tick(SUNDAY_0430)) == "fired policy"
    (control / "request.json").unlink()                     # sidecar claimed it

    assert _run(us.tick(SUNDAY_0430 + timedelta(minutes=2))) == "already fired for this window"
    assert _request(control) is None, "fired a second time inside one window"


def test_a_later_window_fires_again(control):
    _beat(control, current_tag="v1.2.0")
    _set_policy(enabled=True, weekday=6, local_time="04:00", timezone="UTC",
                patch_only=False, last_fired_window="2026-09-06")
    _latest("v1.3.0")
    assert _run(us.tick(SUNDAY_0430)) == "fired policy"


def test_patch_only_blocks_a_minor_release(control):
    _beat(control, current_tag="v1.2.0")
    _set_policy(enabled=True, weekday=6, local_time="04:00", timezone="UTC", patch_only=True)
    _latest("v1.3.0")
    assert "not a patch release" in _run(us.tick(SUNDAY_0430))
    assert _request(control) is None


# ── One-shot schedules ───────────────────────────────────────────────────────

def _schedule(tag="v1.3.0", minutes_ago=5, base=None):
    """`base` defaults to real now for the ticks that use the real clock; the
    tests that pass a fixed `now` into tick() must anchor the row to that same
    clock or the row is simply not due yet."""
    anchor = base or datetime.now(UTC)

    async def go():
        async with AsyncSessionLocal() as db:
            db.add(UpdateSchedule(
                target_tag=tag,
                run_at=(anchor - timedelta(minutes=minutes_ago)).replace(tzinfo=None),
                timezone="UTC", state="pending"))
            await db.commit()
    _run(go())


def _schedule_states():
    async def go():
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(UpdateSchedule))).scalars().all()
            return [(r.target_tag, r.state) for r in rows]
    return _run(go())


def test_a_due_schedule_fires(control):
    _beat(control, current_tag="v1.2.0")
    _schedule("v1.3.0", minutes_ago=5)
    assert _run(us.tick()) == "fired schedule"
    assert _request(control)["tag"] == "v1.3.0"
    assert ("v1.3.0", "fired") in _schedule_states()


def test_a_future_schedule_does_not_fire(control):
    _beat(control, current_tag="v1.2.0")
    _schedule("v1.3.0", minutes_ago=-120)          # two hours from now
    _run(us.tick())
    assert _request(control) is None
    assert ("v1.3.0", "pending") in _schedule_states()


def test_a_badly_missed_schedule_is_abandoned_not_applied(control):
    """Firing hours late is the surprise a schedule exists to prevent."""
    _beat(control, current_tag="v1.2.0")
    _schedule("v1.3.0", minutes_ago=60 * 6)
    assert _run(us.tick()) == "schedule missed its window"
    assert _request(control) is None
    assert ("v1.3.0", "superseded") in _schedule_states()


def test_a_schedule_for_the_running_release_is_dropped(control):
    _beat(control, current_tag="v1.3.0")
    _schedule("v1.3.0", minutes_ago=5)
    assert _run(us.tick()) == "scheduled tag already running"
    assert _request(control) is None


def test_a_schedule_outranks_the_policy(control):
    """The operator named this tag explicitly; a standing policy would apply
    something else."""
    _beat(control, current_tag="v1.2.0")
    _set_policy(enabled=True, weekday=6, local_time="04:00", timezone="UTC", patch_only=False)
    _latest("v1.9.9")
    _schedule("v1.3.0", minutes_ago=5, base=SUNDAY_0430)
    assert _run(us.tick(SUNDAY_0430)) == "fired schedule"
    assert _request(control)["tag"] == "v1.3.0"


# ── Outcome reconciliation ───────────────────────────────────────────────────

def _finish(control, request_id, state, reverted_to=None):
    (control / "status.json").write_text(json.dumps({
        "id": request_id, "state": state, "step": "done" if state == "success" else "failed",
        "action": "update", "to_tag": "v1.3.0", "reverted_to": reverted_to,
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }))


def test_a_failed_automatic_run_pauses_the_policy(control, monkeypatch):
    """Without this the same bad release is retried every week, unattended."""
    sent = []
    monkeypatch.setattr("app.notify.discord.send_discord_alert",
                        lambda *a, **k: sent.append(a) or asyncio.sleep(0))
    _beat(control, current_tag="v1.2.0")
    _set_policy(enabled=True, weekday=6, local_time="04:00", timezone="UTC", patch_only=False)
    _latest("v1.3.0")
    _run(us.tick(SUNDAY_0430))
    rid = _request(control)["id"]
    (control / "request.json").unlink()

    _finish(control, rid, "failed", reverted_to="v1.2.0")
    _run(us.tick(SUNDAY_0430 + timedelta(minutes=5)))

    p = _policy()
    assert p.enabled is False
    assert "failed" in p.paused_reason
    assert p.awaiting_request_id is None


def test_a_successful_automatic_run_leaves_the_policy_enabled(control, monkeypatch):
    monkeypatch.setattr("app.notify.discord.send_discord_alert",
                        lambda *a, **k: asyncio.sleep(0))
    _beat(control, current_tag="v1.2.0")
    _set_policy(enabled=True, weekday=6, local_time="04:00", timezone="UTC", patch_only=False)
    _latest("v1.3.0")
    _run(us.tick(SUNDAY_0430))
    rid = _request(control)["id"]
    (control / "request.json").unlink()

    _finish(control, rid, "success")
    _run(us.tick(SUNDAY_0430 + timedelta(minutes=5)))

    p = _policy()
    assert p.enabled is True
    assert p.paused_reason is None
    assert p.awaiting_request_id is None


def test_a_notification_failure_does_not_break_the_loop(control, monkeypatch):
    """An unattended change with no trail is the hazard, so this is logged
    loudly — but it must not take the scheduler down with it."""
    async def boom(*a, **k):
        raise RuntimeError("discord is down")
    monkeypatch.setattr("app.notify.discord.send_discord_alert", boom)
    _beat(control, current_tag="v1.2.0")
    _set_policy(enabled=True, weekday=6, local_time="04:00", timezone="UTC", patch_only=False)
    _latest("v1.3.0")
    _run(us.tick(SUNDAY_0430))
    rid = _request(control)["id"]
    (control / "request.json").unlink()

    _finish(control, rid, "success")
    _run(us.tick(SUNDAY_0430 + timedelta(minutes=5)))     # must not raise
    assert _policy().awaiting_request_id is None
