"""Plausibility window for user-submitted structure anchor dates
(app/intel/structure_age_check.py). Database tests run against the
straight-line synthetic table in tests/_structure_age.py. Ids are invented."""
from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import select

from app.db.models import StructureAgeCalibration, StructureAgeSubmission
from app.intel import structure_age_check as sac
from tests._structure_age import (NEWEST_ID, POINTS, RATE, make_db, point_date, point_id,
                                  run, today)

USER = 4301
MID_ID = point_id(100) + RATE // 2          # between points 100 and 101
TAIL_ID = NEWEST_ID + 30 * RATE             # expected: 30 days after the newest point
D0 = date(2026, 1, 1)


# ── parsing ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,want", [
    ("1055400000000", 1055400000000),
    (" 1055400000000 ", 1055400000000),
    ("9" * 19, int("9" * 19)),        # parses; check_submission range-checks it
    ("9" * 20, None),
    ("12a", None),
    ("", None),
    (None, None),
    ("١٠٥٥٤٠٠٠٠٠٠٠٠", None),          # Arabic-Indic digits: int() would accept them
])
def test_parse_structure_id(raw, want):
    assert sac.parse_structure_id(raw) == want


@pytest.mark.parametrize("raw,want", [
    ("2026-09-30", date(2026, 9, 30)),
    (" 2026-09-30 ", date(2026, 9, 30)),
    ("2026-9-30", None),
    ("2026-02-30", None),
    ("30/09/2026", None),
    ("２０２６-09-30", None),           # full-width digits
    ("", None),
])
def test_parse_anchor_date(raw, want):
    assert sac.parse_anchor_date(raw) == want


# ── pure window maths ─────────────────────────────────────────────────────

def _line(n=200, start=D0):
    return [(1_050_000_000_000 + i * RATE, datetime.combine(start + timedelta(days=i), time()))
            for i in range(n)]


def test_fit_rate_recovers_a_straight_line():
    assert sac.fit_rate(_line()) == pytest.approx(RATE, rel=0.01)


def test_fit_rate_shrugs_off_misdated_points():
    pts = _line()
    for i in range(0, len(pts), 10):            # every 10th point 100 days off
        sid, when = pts[i]
        pts[i] = (sid, when + timedelta(days=100 if i % 20 else -100))
    assert sac.fit_rate(pts) == pytest.approx(RATE, rel=0.02)


def test_fit_rate_falls_back_with_too_few_points():
    assert sac.fit_rate(_line(n=5)) == sac.DEFAULT_RATE


def test_fit_rate_falls_back_when_ids_repeat():
    """A zero rate would divide by zero in every extrapolation."""
    pts = [(1_050_000_000_000, datetime.combine(D0 + timedelta(days=i), time()))
           for i in range(40)]
    assert sac.fit_rate(pts) == sac.DEFAULT_RATE


def test_dense_neighbours_on_both_sides_give_their_own_median():
    line = _line()
    w = sac.window_for(line[100][0] + RATE // 2, line[76:126], RATE,
                       D0 + timedelta(days=400), tail=False)
    assert not w.tail and abs((w.expected - (D0 + timedelta(days=100))).days) <= 1
    # The allowance grows by a quarter of the neighbours' median distance:
    # ~3 days on this one-point-a-day line, under one on real recent data.
    assert sac.TOLERANCE_DAYS <= (w.expected - w.low).days <= sac.TOLERANCE_DAYS + 5


def test_one_wild_neighbour_doesnt_move_the_median():
    line = _line()
    neighbours = line[76:126]
    sid, when = neighbours[10]
    neighbours[10] = (sid, when - timedelta(days=4 * 365))
    w = sac.window_for(line[100][0] + RATE // 2, neighbours, RATE,
                       D0 + timedelta(days=400), tail=False)
    assert abs((w.expected - (D0 + timedelta(days=100))).days) <= 1


def test_window_is_capped_at_today_and_floored_at_the_citadel_release():
    line = _line()
    w = sac.window_for(line[100][0] + RATE // 2, line[76:126], RATE,
                       D0 + timedelta(days=110), tail=False)
    assert w.high == D0 + timedelta(days=110)
    early = _line(start=date(2016, 5, 1))        # expected 9 Aug 2016: 120 days back is pre-release
    w = sac.window_for(early[100][0] + RATE // 2, early[76:126], RATE, D0, tail=False)
    assert w.low == sac.CITADEL_RELEASE


def test_a_gap_above_the_stalled_data_is_bridged_not_pinned():
    """Triff stops at day 199; one user point sits 150 days of IDs above it.
    An ID in the gap must expect its own date. A plain median of the
    neighbours' dates expected ~day 187 everywhere in the gap and refused
    true dates once the gap passed ~132 days."""
    line = _line()
    user = (line[-1][0] + 150 * RATE, datetime.combine(D0 + timedelta(days=349), time()))
    on = D0 + timedelta(days=360)
    for k in (30, 100, 140, 145):
        true_date = D0 + timedelta(days=199 + k)
        w = sac.window_for(line[-1][0] + k * RATE, line[-25:] + [user], RATE, on, tail=False)
        assert abs((w.expected - true_date).days) <= 1, k
        assert w.contains(true_date), k


def test_one_point_doesnt_decide_the_allowance_alone():
    """A lone user point next to a tail ID is one vote on the date, and must
    not shrink the allowance to the bare tolerance either."""
    t = date(2026, 10, 18)
    top = _line(n=25, start=t - timedelta(days=184))     # newest point 160 days ago
    sid = top[-1][0] + 151 * RATE
    alone = sac.window_for(sid, top, RATE, t, tail=True)
    user = (top[-1][0] + 150 * RATE, datetime.combine(t - timedelta(days=10), time()))
    with_user = sac.window_for(sid, top + [user], RATE, t, tail=False)
    assert with_user.expected == alone.expected == t - timedelta(days=9)
    assert (with_user.expected - with_user.low).days >= (alone.expected - alone.low).days - 5


def test_tail_windows_widen_with_distance_and_stop_at_the_frontier():
    t = date(2026, 10, 18)
    top = _line(n=25, start=t - timedelta(days=124))     # newest point 100 days ago
    newest_id = top[-1][0]
    near = sac.window_for(newest_id + 10 * RATE, top, RATE, t, tail=True)
    far = sac.window_for(newest_id + 90 * RATE, top, RATE, t, tail=True)
    assert near.tail and near.expected == t - timedelta(days=90)
    assert (far.expected - far.low) > (near.expected - near.low)
    assert far.high == t
    # 161 days past the newest point is today + 61: past the 60-day slack.
    assert sac.window_for(newest_id + 161 * RATE, top, RATE, t, tail=True) is None


def test_an_absurd_id_hits_the_frontier_instead_of_overflowing():
    t = date(2026, 10, 18)
    assert sac.window_for(sac.INT64_MAX, _line(n=25), RATE, t, tail=True) is None


def test_the_tail_slack_stays_inside_the_tolerance():
    """Keeps every tail window non-empty: expected is at most today + slack,
    and the allowance is at least TOLERANCE_DAYS."""
    assert sac.FUTURE_SLACK_DAYS < sac.TOLERANCE_DAYS


@pytest.mark.parametrize("certainty,width", [("day", 2), ("week", 7), ("month", 30)])
def test_calibration_values_window_follows_certainty(certainty, width):
    now = datetime(2026, 10, 18, 12, 0)
    v = sac.calibration_values(1_055_400_000_000, date(2026, 9, 30), certainty, now)
    assert v["anchor_mid"] == datetime(2026, 9, 30)
    assert v["anchor_high"] - v["anchor_low"] == timedelta(days=width)
    assert (v["days_wide"], v["source"], v["fetched_at"]) == (float(width), "user", now)


# ── against the synthetic table ───────────────────────────────────────────

def _with_db(fn, users=()):
    """Run fn(SessionLocal) against a fresh synthetic table, in one loop."""
    async def go():
        engine, SessionLocal = await make_db(users=users)
        try:
            return await fn(SessionLocal)
        finally:
            await engine.dispose()
    return run(go())


def test_interior_expected_date_follows_the_line():
    async def fn(SL):
        async with SL() as db:
            return await sac.plausible_window(db, MID_ID)
    w = _with_db(fn)
    assert not w.tail and abs((w.expected - point_date(100)).days) <= 1


def test_readers_refuse_an_id_too_big_for_sqlite_themselves():
    async def fn(SL):
        async with SL() as db:
            return (await sac.plausible_window(db, sac.INT64_MAX + 1),
                    await sac.tail_estimate(db, sac.INT64_MAX + 1),
                    await sac.pending_for(db, sac.INT64_MAX + 1))
    assert _with_db(fn) == (None, None, False)


def test_a_passed_rate_is_used_instead_of_refitting():
    async def fn(SL):
        async with SL() as db:
            return (await sac.plausible_window(db, TAIL_ID),
                    await sac.plausible_window(db, TAIL_ID, rate=RATE / 2))
    fitted, halved = _with_db(fn)
    assert halved.expected > fitted.expected + timedelta(days=30)


def test_misdated_neighbour_doesnt_cause_a_false_refusal():
    async def fn(SL):
        async with SL() as db:
            row = await db.get(StructureAgeCalibration, point_id(101))
            row.anchor_mid = row.anchor_mid - timedelta(days=4 * 365)
            await db.commit()
            return await sac.check_submission(db, MID_ID, point_date(100), USER)
    assert _with_db(fn, users=[(USER, "user")]).ok


def test_tail_expected_date_comes_from_the_rate():
    async def fn(SL):
        async with SL() as db:
            return await sac.plausible_window(db, TAIL_ID)
    w = _with_db(fn)
    want = point_date(POINTS - 1) + timedelta(days=30)
    assert w.tail and abs((w.expected - want).days) <= 1


def test_user_points_cannot_move_the_rate():
    async def fn(SL):
        async with SL() as db:
            before = await sac.tail_rate(db)
            # 100 approved user points claiming a tenth of the real rate.
            for i in range(1, 101):
                db.add(StructureAgeCalibration(**sac.calibration_values(
                    NEWEST_ID + i * RATE // 10, point_date(POINTS - 1) + timedelta(days=i),
                    "day", datetime(2026, 10, 1))))
            await db.commit()
            rows = (await db.execute(select(StructureAgeCalibration.structure_id,
                                            StructureAgeCalibration.anchor_mid))).all()
            return before, await sac.tail_rate(db), rows
    before, after, rows = _with_db(fn)
    # Control: fitted without the triff-only filter, these points drag the
    # rate well away — so the equality below really tests the filter.
    cutoff = (datetime.combine(point_date(POINTS - 1), time())
              - timedelta(days=sac.RATE_WINDOW_DAYS))
    unfiltered = sac.fit_rate([(r[0], r[1]) for r in rows if r[1] >= cutoff])
    assert abs(unfiltered - RATE) / RATE > 0.1
    assert before == pytest.approx(RATE, rel=0.01) and after == before


def test_triff_baseline_ignores_user_points():
    async def fn(SL):
        async with SL() as db:
            # A user point above TAIL_ID makes it an interior ID for the full
            # table; the triff-only baseline still extrapolates.
            db.add(StructureAgeCalibration(**sac.calibration_values(
                TAIL_ID + RATE, point_date(POINTS - 1) + timedelta(days=31), "day",
                datetime(2026, 10, 1))))
            await db.commit()
            return (await sac.plausible_window(db, TAIL_ID),
                    await sac.plausible_window(db, TAIL_ID, sources=("triff",)))
    full, baseline = _with_db(fn)
    want = point_date(POINTS - 1) + timedelta(days=30)
    assert not full.tail and baseline.tail
    assert abs((full.expected - want).days) <= 1
    assert abs((baseline.expected - want).days) <= 1


def _check(structure_id, anchor, setup=None, *, on=None):
    """check_submission against a fresh table. `on` pins "today" so a run
    that crosses midnight UTC can't flip a date-boundary assertion."""
    async def fn(SL):
        async with SL() as db:
            if setup:
                await setup(db)
            return await sac.check_submission(db, structure_id, anchor, USER, today=on)
    return _with_db(fn, users=[(USER, "user")])


def _pending(n):
    async def setup(db):
        for i in range(n):
            db.add(StructureAgeSubmission(structure_id=point_id(i) + 1, anchor_date=point_date(i),
                                          certainty="day", status="pending", submitted_by=USER,
                                          submitted_at=datetime(2026, 10, 1)))
        await db.commit()
    return setup


def test_a_sensible_interior_date_passes():
    v = _check(MID_ID, point_date(100))
    assert v.ok and v.window is not None


def test_a_recent_id_claimed_four_years_ago_is_refused():
    t = today()
    v = _check(MID_ID, t - timedelta(days=4 * 365), on=t)
    assert not v.ok and v.code == "outside" and "plausible anchor date is" in v.reason


def test_a_sensible_tail_date_passes_and_an_old_one_doesnt():
    t = today()
    assert _check(TAIL_ID, point_date(POINTS - 1) + timedelta(days=28), on=t).ok
    assert _check(TAIL_ID, t - timedelta(days=4 * 365), on=t).code == "outside"


def test_impossible_dates_and_ids_are_refused():
    t = today()
    assert _check(MID_ID, t + timedelta(days=1), on=t).code == "invalid"
    assert _check(MID_ID, date(2016, 1, 1), on=t).code == "invalid"
    assert _check(999_999_999_999, point_date(0), on=t).code == "not_upwell"
    assert _check(sac.INT64_MAX + 1, point_date(0), on=t).code == "frontier"
    assert _check(NEWEST_ID + 200 * RATE, t - timedelta(days=1), on=t).code == "frontier"


async def _late_2016(db):
    """25 triff points from 1.021e12, one a day from 17 Nov 2016 — the real
    table's oldest corner."""
    for i in range(25):
        db.add(StructureAgeCalibration(**{**sac.calibration_values(
            1_021_010_094_060 + i * RATE, date(2016, 11, 17) + timedelta(days=i), "day",
            datetime(2026, 6, 6)), "source": "triff"}))
    await db.commit()


def test_an_id_that_extrapolates_to_before_the_citadel_release_isnt_upwell():
    """Below the oldest point the window is moved back at today's rate. Far
    enough below, even its latest date is before the Citadel release — the
    window would read backwards — so the ID isn't an Upwell structure."""
    v = _check(1_010_000_000_000, date(2016, 6, 1), setup=_late_2016)
    assert v.code == "not_upwell" and v.reason == sac.NOT_UPWELL_MSG
    assert _check(1_020_500_000_000, date(2016, 10, 1), setup=_late_2016).ok


def test_ids_with_a_point_or_a_pending_claim_are_refused():
    assert _check(point_id(50), point_date(50)).code == "confirmed"

    async def pending(db):
        db.add(StructureAgeSubmission(structure_id=MID_ID, anchor_date=point_date(100),
                                      certainty="day", status="pending",
                                      submitted_at=datetime(2026, 10, 1)))
        await db.commit()
    assert _check(MID_ID, point_date(100), setup=pending).code == "pending"


def test_a_user_can_have_twenty_pending_but_not_twenty_one():
    assert _check(MID_ID, point_date(100), setup=_pending(sac.MAX_PENDING_PER_USER - 1)).ok
    v = _check(MID_ID, point_date(100), setup=_pending(sac.MAX_PENDING_PER_USER))
    assert v.code == "limit" and v.reason == sac.LIMIT_MSG
