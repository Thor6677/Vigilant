"""Window arithmetic and the fire/don't-fire decision.

All pure — no clock, no database, no /control. The traps being tested here are
the ones that only show up months later and unattended: a window that drifts an
hour at a DST boundary, and a policy that fires twice because the update it
triggered restarted the app that was tracking it.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.ops import update_schedule as us

UTC = timezone.utc


def _policy(**kw):
    base = dict(enabled=True, weekday=6, local_time="04:00", timezone="UTC",
                patch_only=False, last_fired_window=None, paused_reason=None)
    base.update(kw)
    return SimpleNamespace(**base)


# ── Input validation ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("04:00", (4, 0)), ("00:00", (0, 0)), ("23:59", (23, 59)), (" 9:05 ", (9, 5)),
])
def test_parse_local_time_accepts(raw, expected):
    assert us.parse_local_time(raw) == expected


@pytest.mark.parametrize("raw", [
    "24:00", "12:60", "-1:00", "4", "4:00:00", "aa:bb", "", None, 400,
])
def test_parse_local_time_rejects(raw):
    """A silently misparsed schedule is an update at the wrong hour, unattended."""
    assert us.parse_local_time(raw) is None


@pytest.mark.parametrize("tz", ["UTC", "America/New_York", "Europe/London"])
def test_valid_timezone_accepts_iana(tz):
    assert us.valid_timezone(tz) is True


@pytest.mark.parametrize("tz", ["EST5EDT-nonsense", "Mars/Olympus", "", None, 5])
def test_valid_timezone_rejects(tz):
    assert us.valid_timezone(tz) is False


# ── Window resolution ────────────────────────────────────────────────────────

def test_most_recent_window_finds_todays_occurrence_once_passed():
    now = datetime(2026, 9, 13, 6, 0, tzinfo=UTC)      # a Sunday, 06:00
    w = us.most_recent_window(6, "04:00", "UTC", now)
    assert w.date() == datetime(2026, 9, 13).date()
    assert (w.hour, w.minute) == (4, 0)


def test_most_recent_window_walks_back_when_time_has_not_arrived():
    now = datetime(2026, 9, 13, 3, 0, tzinfo=UTC)      # Sunday, before 04:00
    w = us.most_recent_window(6, "04:00", "UTC", now)
    assert w.date() == datetime(2026, 9, 6).date()      # the previous Sunday


def test_next_window_is_upcoming_not_past():
    now = datetime(2026, 9, 13, 6, 0, tzinfo=UTC)
    assert us.next_window(6, "04:00", "UTC", now).date() == datetime(2026, 9, 20).date()


def test_window_is_resolved_in_the_policys_zone_not_utc():
    """04:00 in New York is 08:00 UTC — a policy evaluated in UTC would fire at
    the wrong local hour, which is the whole reason the zone is stored."""
    now = datetime(2026, 9, 13, 8, 30, tzinfo=UTC)
    w = us.most_recent_window(6, "04:00", "America/New_York", now)
    assert (w.hour, w.minute) == (4, 0)
    assert w.utcoffset() == timedelta(hours=-4)          # EDT


def test_window_survives_the_dst_boundary_at_the_same_local_hour():
    """The bug a stored UTC hour produces: 04:00 local silently becoming 03:00
    or 05:00 local twice a year. Resolve on both sides of a US transition and
    require the LOCAL time to be identical while the UTC offset changes.
    """
    tz = "America/New_York"
    before = us.most_recent_window(6, "04:00", tz, datetime(2026, 10, 25, 12, 0, tzinfo=UTC))
    after = us.most_recent_window(6, "04:00", tz, datetime(2026, 11, 8, 12, 0, tzinfo=UTC))
    assert (before.hour, before.minute) == (4, 0)
    assert (after.hour, after.minute) == (4, 0)
    assert before.utcoffset() != after.utcoffset(), "test dates did not straddle a DST change"


@pytest.mark.parametrize("kw", [
    {"local_time": "nope"}, {"tz_name": "Mars/Olympus"}, {"weekday": 9}, {"weekday": -1},
])
def test_window_is_none_for_unresolvable_policies(kw):
    args = dict(weekday=6, local_time="04:00", tz_name="UTC",
                now_utc=datetime(2026, 9, 13, 6, 0, tzinfo=UTC))
    args.update(kw)
    assert us.most_recent_window(**args) is None


def test_window_key_is_the_local_date():
    """Local rather than UTC so one window is one key regardless of which side
    of midnight UTC it falls on."""
    w = us.most_recent_window(6, "22:00", "America/New_York",
                              datetime(2026, 9, 14, 3, 0, tzinfo=UTC))
    assert us.window_key(w) == "2026-09-13"


# ── One-shot schedules ───────────────────────────────────────────────────────

def test_schedule_not_due_before_its_time():
    now = datetime(2026, 9, 13, 6, 0, tzinfo=UTC)
    due, reason = us.schedule_is_due(now + timedelta(hours=1), now)
    assert due is False and reason == "not yet"


def test_schedule_due_at_its_time():
    now = datetime(2026, 9, 13, 6, 0, tzinfo=UTC)
    assert us.schedule_is_due(now, now)[0] is True


def test_schedule_fires_late_within_the_grace():
    """The app is both the scheduler and the thing being recreated, so it will
    sometimes be down when a window passes."""
    now = datetime(2026, 9, 13, 6, 0, tzinfo=UTC)
    assert us.schedule_is_due(now - timedelta(minutes=90), now)[0] is True


def test_schedule_is_abandoned_past_the_grace():
    """Firing unboundedly late is worse than skipping: a 4am Sunday update going
    off at 9am Monday is the surprise a schedule exists to prevent."""
    now = datetime(2026, 9, 13, 6, 0, tzinfo=UTC)
    due, reason = us.schedule_is_due(now - timedelta(hours=5), now)
    assert due is False and reason == "missed the window"


def test_schedule_tolerates_a_naive_stored_datetime():
    """SQLite hands back naive datetimes; comparing one to an aware now raises."""
    now = datetime(2026, 9, 13, 6, 0, tzinfo=UTC)
    assert us.schedule_is_due(datetime(2026, 9, 13, 5, 30), now)[0] is True


# ── Patch-only ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cur,new", [("v1.2.0", "v1.2.1"), ("v1.2.1", "v1.2.9")])
def test_patch_upgrade_accepted(cur, new):
    assert us.is_patch_upgrade(cur, new) is True


@pytest.mark.parametrize("cur,new", [
    ("v1.2.0", "v1.3.0"),   # minor
    ("v1.2.0", "v2.0.0"),   # major
    ("v1.2.1", "v1.2.0"),   # backwards
    ("v1.2.0", "v1.2.0"),   # same
    ("dev", "v1.2.1"),      # a source build cannot be compared
    (None, "v1.2.1"),
])
def test_patch_upgrade_rejected(cur, new):
    assert us.is_patch_upgrade(cur, new) is False


# ── The policy decision ──────────────────────────────────────────────────────

SUNDAY_0600 = datetime(2026, 9, 13, 6, 0, tzinfo=UTC)


def test_policy_fires_in_its_window():
    fire, key, reason = us.policy_decision(_policy(), SUNDAY_0600, "v1.3.0", "v1.2.0")
    assert fire is True and key == "2026-09-13" and reason == "due"


def test_disabled_policy_never_fires():
    fire, _, reason = us.policy_decision(_policy(enabled=False), SUNDAY_0600,
                                         "v1.3.0", "v1.2.0")
    assert fire is False and reason == "disabled"


def test_missing_policy_never_fires():
    assert us.policy_decision(None, SUNDAY_0600, "v1.3.0", "v1.2.0")[0] is False


def test_paused_policy_never_fires():
    """An automatic run that auto-reverted must not retry the same bad release
    every week, unattended."""
    p = _policy(paused_reason="automatic update to v1.3.0 failed")
    fire, _, reason = us.policy_decision(p, SUNDAY_0600, "v1.3.0", "v1.2.0")
    assert fire is False and "paused" in reason


def test_policy_does_not_fire_outside_the_window():
    later = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)   # 8h after a 04:00 window
    fire, _, reason = us.policy_decision(_policy(), later, "v1.3.0", "v1.2.0")
    assert fire is False and reason == "outside the window"


def test_policy_fires_late_within_the_grace():
    late = datetime(2026, 9, 13, 5, 30, tzinfo=UTC)
    assert us.policy_decision(_policy(), late, "v1.3.0", "v1.2.0")[0] is True


def test_policy_will_not_fire_twice_for_one_window():
    """THE restart loop. The app fires an update, the update recreates the app,
    the app comes back inside the same window and re-evaluates. Without the
    idempotency key it fires again, and again."""
    p = _policy(last_fired_window="2026-09-13")
    fire, _, reason = us.policy_decision(p, SUNDAY_0600, "v1.3.0", "v1.2.0")
    assert fire is False and reason == "already fired for this window"


def test_a_new_window_clears_the_idempotency_key():
    p = _policy(last_fired_window="2026-09-06")       # last week
    assert us.policy_decision(p, SUNDAY_0600, "v1.3.0", "v1.2.0")[0] is True


def test_policy_does_not_fire_when_already_current():
    fire, _, reason = us.policy_decision(_policy(), SUNDAY_0600, "v1.2.0", "v1.2.0")
    assert fire is False and reason == "already up to date"


def test_policy_does_not_fire_without_release_information():
    fire, _, reason = us.policy_decision(_policy(), SUNDAY_0600, None, "v1.2.0")
    assert fire is False and "no release information" in reason


def test_policy_refuses_a_prerelease():
    """validate_tag's regex admits no hyphen, so this is the shape of the
    validator rather than a policy that can be misconfigured."""
    fire, _, reason = us.policy_decision(_policy(), SUNDAY_0600, "v1.3.0-rc1", "v1.2.0")
    assert fire is False and "not an exact release tag" in reason


def test_patch_only_blocks_a_minor_release():
    p = _policy(patch_only=True)
    fire, _, reason = us.policy_decision(p, SUNDAY_0600, "v1.3.0", "v1.2.0")
    assert fire is False and "not a patch release" in reason


def test_patch_only_allows_a_patch_release():
    p = _policy(patch_only=True)
    assert us.policy_decision(p, SUNDAY_0600, "v1.2.1", "v1.2.0")[0] is True


def test_unresolvable_policy_is_refused_not_crashed():
    p = _policy(timezone="Mars/Olympus")
    fire, key, reason = us.policy_decision(p, SUNDAY_0600, "v1.3.0", "v1.2.0")
    assert fire is False and key is None and "not resolvable" in reason


def test_every_refusal_carries_a_reason():
    """"It did nothing" is the question anyone asks of a scheduler. Each path
    must be able to answer it."""
    cases = [
        (_policy(enabled=False), "v1.3.0", "v1.2.0"),
        (_policy(patch_only=True), "v1.3.0", "v1.2.0"),
        (_policy(last_fired_window="2026-09-13"), "v1.3.0", "v1.2.0"),
        (_policy(), None, "v1.2.0"),
        (_policy(), "v1.2.0", "v1.2.0"),
    ]
    for policy, latest, current in cases:
        fire, _, reason = us.policy_decision(policy, SUNDAY_0600, latest, current)
        assert fire is False
        assert reason and reason != "due"
