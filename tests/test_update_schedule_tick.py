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


def _beat(control, current_tag="v1.2.0", self_update=None, checks=None):
    (control / "updater.json").write_text(json.dumps({
        "version": "v1.2.0", "current_tag": current_tag, "targets": [],
        "checks": {"socket": "ok"} if checks is None else checks,
        "self_update": self_update,
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


# ── The sidecar replacing itself ─────────────────────────────────────────────
#
# After a successful in-app update the sidecar pulls its own new image and hands
# off to a helper that recreates it. Its poll loop is blocked throughout, so a
# request written then waits for whichever sidecar polls next — and if the
# handoff leaves none running, it waits for whoever starts one, however late
# that is. The scheduler must hold off rather than queue into that.

def _handoff(state, target="v1.2.0"):
    return {"state": state, "target": target, "error": None,
            "at": "2026-09-13T04:29:00Z"}


def _pending_schedule_ids():
    async def go():
        async with AsyncSessionLocal() as db:
            return [r.id for r in (await db.execute(
                select(UpdateSchedule).where(UpdateSchedule.state == "pending"))).scalars()]
    return _run(go())


@pytest.mark.parametrize("state", ["pulling", "handed_off"])
def test_a_handoff_in_flight_is_waited_out_not_queued_into(control, state):
    _beat(control, current_tag="v1.2.0", self_update=_handoff(state))
    _set_policy(enabled=True, weekday=6, local_time="04:00", timezone="UTC", patch_only=False)
    _latest("v1.3.0")
    _schedule("v1.3.0", minutes_ago=5, base=SUNDAY_0430)

    assert _run(us.tick(SUNDAY_0430)) == "updater is upgrading itself"
    assert _request(control) is None, "queued a request into a sidecar handoff"
    # Nothing recorded, so the next tick is free to try again.
    assert _policy().last_fired_window is None
    assert len(_pending_schedule_ids()) == 1


def test_the_window_fires_once_the_replacement_is_ready(control):
    _set_policy(enabled=True, weekday=6, local_time="04:00", timezone="UTC", patch_only=False)
    _latest("v1.3.0")

    _beat(control, current_tag="v1.2.0", self_update=_handoff("handed_off"))
    assert _run(us.tick(SUNDAY_0430)) == "updater is upgrading itself"

    # The replacement is up but has not finished its own checks yet.
    _beat(control, current_tag="v1.2.0", self_update=_handoff("done"),
          checks={"startup": "FAIL: the updater is still starting"})
    assert _run(us.tick(SUNDAY_0430 + timedelta(minutes=1))).startswith(
        "updater checks not passing")
    assert _request(control) is None

    _beat(control, current_tag="v1.2.0", self_update=_handoff("done"))
    assert _run(us.tick(SUNDAY_0430 + timedelta(minutes=2))) == "fired policy"
    assert _request(control)["tag"] == "v1.3.0"
    (control / "request.json").unlink()                     # sidecar claimed it

    assert _run(us.tick(SUNDAY_0430 + timedelta(minutes=3))) == "already fired for this window"
    assert _request(control) is None, "fired a second time inside one window"


def test_a_finished_run_is_still_reported_during_the_handoff(control, monkeypatch):
    """The handoff follows every successful in-app update, so it follows every
    successful AUTOMATIC one. Reporting the outcome needs nothing from the
    sidecar and must not wait for it."""
    sent = []

    async def spy(**kwargs):
        sent.append(kwargs)
    monkeypatch.setattr("app.notify.discord.send_discord_alert", spy)

    _beat(control, current_tag="v1.2.0")
    _set_policy(enabled=True, weekday=6, local_time="04:00", timezone="UTC", patch_only=False)
    _latest("v1.3.0")
    _run(us.tick(SUNDAY_0430))
    rid = _request(control)["id"]
    (control / "request.json").unlink()

    _finish(control, rid, "success")
    _beat(control, current_tag="v1.3.0", self_update=_handoff("pulling", "v1.3.0"))
    assert _run(us.tick(SUNDAY_0430 + timedelta(minutes=5))) == "updater is upgrading itself"
    assert _policy().awaiting_request_id is None
    assert sent and "succeeded" in sent[0]["title"]


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


# ── The compensating controls for unattended deploys ─────────────────────────

def test_the_discord_call_matches_the_real_signature(control, monkeypatch):
    """Binds the actual call against the real function's signature.

    Every other test here stubs Discord with `*a, **k`, which accepts anything —
    so an argument-order mistake is invisible. It nearly shipped: the signature
    is (title, body, alert_type, key), and passing "type first" positionally
    made alert_type the message body. That matches nothing in
    DISCORD_ALERT_TYPES, so it is dropped with no error at all.
    """
    import inspect
    from app.notify import discord as real

    # Snapshot the signature BEFORE patching. Reading it inside the spy looks up
    # the module attribute, which by then IS the spy — so it would bind against
    # (*args, **kwargs), accept literally anything, and prove nothing.
    real_sig = inspect.signature(real.send_discord_alert)
    captured = {}

    async def spy(*args, **kwargs):
        bound = real_sig.bind(*args, **kwargs)   # raises TypeError on bad arity
        bound.apply_defaults()
        captured.update(bound.arguments)

    monkeypatch.setattr("app.notify.discord.send_discord_alert", spy)

    _beat(control, current_tag="v1.2.0")
    _set_policy(enabled=True, weekday=6, local_time="04:00", timezone="UTC", patch_only=False)
    _latest("v1.3.0")
    _run(us.tick(SUNDAY_0430))
    rid = _request(control)["id"]
    (control / "request.json").unlink()
    _finish(control, rid, "failed", reverted_to="v1.2.0")
    _run(us.tick(SUNDAY_0430 + timedelta(minutes=5)))

    assert captured, "no notification was attempted"
    # The literal, not us.ALERT_TYPE: comparing the constant with itself would
    # pass whatever it held. And not the release-notice type — sharing it meant
    # opting out of "a release is out" also silenced these failure reports.
    from app.ops import update_check
    assert captured["alert_type"] == "auto_update"
    assert captured["alert_type"] != update_check.ALERT_TYPE
    assert "v1.3.0" in captured["body"]
    assert "FAILED" in captured["title"]


def _audit_rows():
    from app.db.models import AdminAuditLog

    async def go():
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(AdminAuditLog))).scalars().all()
            return [(r.event_type, r.user_id, r.detail) for r in rows]
    return _run(go())


def test_an_automatic_fire_is_audit_logged(control):
    """Discord is gated on an opt-in type and may not be configured at all, so
    the audit log is the only guaranteed record that an unattended deploy
    happened. Null user id because nobody asked — that is the fact recorded."""
    _beat(control, current_tag="v1.2.0")
    _set_policy(enabled=True, weekday=6, local_time="04:00", timezone="UTC", patch_only=False)
    _latest("v1.3.0")
    _run(us.tick(SUNDAY_0430))

    rows = [r for r in _audit_rows() if r[0] == "auto_update_requested"]
    assert rows, "an unattended deploy left no audit trail"
    assert rows[0][1] is None
    assert "v1.3.0" in rows[0][2] and "2026-09-13" in rows[0][2]


def test_a_scheduled_fire_is_audit_logged_distinctly(control):
    _beat(control, current_tag="v1.2.0")
    _schedule("v1.3.0", minutes_ago=5)
    _run(us.tick())
    assert any(r[0] == "scheduled_update_requested" for r in _audit_rows())
