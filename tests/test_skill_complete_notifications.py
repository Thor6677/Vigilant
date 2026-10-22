"""Skill Complete must fire when a skill really finishes, and only then (ISS-107).

ESI drops start/finish dates from a paused queue and keeps finished skills
listed for offline pilots. The old check announced a finish whenever the
active skill was missing from the new queue, so pausing fired a false
"Skill Complete" and the real one was swallowed later. The shapes below are
the ones seen in the production cache: fully dated, fully undated (paused),
and finished entries still listed.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.routes import dashboard

NOW = datetime.now(timezone.utc)
CHAR = SimpleNamespace(user_id=1, character_name="Pilot", character_id=4242)


def _iso(delta):
    return (NOW + delta).isoformat().replace("+00:00", "Z")


def _dated(skill_id, level, start, finish):
    return {"skill_id": skill_id, "finished_level": level, "queue_position": 0,
            "start_date": _iso(start), "finish_date": _iso(finish)}


def _paused(skill_id, level):
    return {"skill_id": skill_id, "finished_level": level, "queue_position": 0}


@pytest.fixture
def emitted(monkeypatch):
    out = []
    monkeypatch.setattr(dashboard, "_emit_notification", lambda uid, ev: out.append(ev))

    async def names(db, ids):
        return {i: f"Skill {i}" for i in ids}

    monkeypatch.setattr(dashboard.sde, "type_ids_to_names", names)
    dashboard._notified_skills.clear()
    yield out
    dashboard._notified_skills.clear()


def _run(old, new, synced_at=NOW - timedelta(hours=1)):
    asyncio.run(dashboard._detect_notifications(
        CHAR, {"skillqueue": old}, {"skillqueue": new}, None, skillqueue_synced_at=synced_at))


H = timedelta(hours=1)


def test_pausing_a_queue_mid_skill_announces_nothing(emitted):
    old = [_dated(3300, 4, -2 * H, 2 * H), _dated(3301, 3, 2 * H, 5 * H)]
    new = [_paused(3300, 4), _paused(3301, 3)]
    _run(old, new)
    assert emitted == []
    assert dashboard._notified_skills.get(CHAR.character_id, set()) == set()


def test_pause_resume_finish_announces_exactly_once(emitted):
    skill = (3300, 4)
    # 1. training, then paused: nothing.
    _run([_dated(*skill, -2 * H, 2 * H)], [_paused(*skill)])
    # 2. paused, then resumed with a fresh schedule: nothing.
    _run([_paused(*skill)], [_dated(*skill, -5 * timedelta(minutes=1), 3 * H)])
    assert emitted == []
    # 3. it finishes 10 minutes ago and ESI has dropped it: one announcement.
    _run([_dated(*skill, -3 * H, -10 * timedelta(minutes=1))], [])
    assert [e["title"] for e in emitted] == ["Skill Complete"]
    assert emitted[0]["body"] == "Pilot — Skill 3300 4 finished"
    # 4. the next sync starts from the time step 3 synced, which is after the
    # finish, so the same entry cannot announce a second time.
    _run([_dated(*skill, -3 * H, -10 * timedelta(minutes=1))], [], synced_at=NOW - 5 * timedelta(minutes=1))
    assert len(emitted) == 1


def test_normal_completion_with_the_next_skill_now_training(emitted):
    old = [_dated(3300, 4, -3 * H, -10 * timedelta(minutes=1)), _dated(3301, 3, -10 * timedelta(minutes=1), 4 * H)]
    new = [_dated(3301, 3, -10 * timedelta(minutes=1), 4 * H)]
    _run(old, new)
    assert [e["body"] for e in emitted] == ["Pilot — Skill 3300 4 finished"]


def test_finished_skill_still_listed_by_esi_is_announced_once(emitted):
    finished = _dated(3300, 4, -3 * H, -10 * timedelta(minutes=1))
    _run([finished], [finished])
    _run([finished], [finished])
    assert len(emitted) == 1


def test_skill_that_finished_before_the_previous_sync_is_not_reannounced(emitted):
    # Listed-but-finished entries survive an app restart (the dedupe set is
    # memory only); the previous-sync bound is what keeps them quiet.
    stale = _dated(3300, 4, -9 * H, -3 * H)
    _run([stale], [stale], synced_at=NOW - H)
    assert emitted == []


def test_last_skill_finishing_empties_the_queue_and_is_announced(emitted):
    _run([_dated(3300, 5, -9 * H, -10 * timedelta(minutes=1))], [])
    assert len(emitted) == 1


def test_schedule_gone_stale_because_the_queue_was_paused_between_syncs(emitted):
    # The old schedule says it finished 10 minutes ago, but the new queue
    # lists it without dates: it was paused, not finished.
    _run([_dated(3300, 4, -3 * H, -10 * timedelta(minutes=1))], [_paused(3300, 4)])
    assert emitted == []


def test_removing_a_skill_before_it_finishes_announces_nothing(emitted):
    _run([_dated(3300, 4, -1 * H, 2 * H)], [])
    assert emitted == []


def test_unknown_previous_sync_time_stays_silent(emitted):
    _run([_dated(3300, 4, -3 * H, -10 * timedelta(minutes=1))], [], synced_at=None)
    assert emitted == []
