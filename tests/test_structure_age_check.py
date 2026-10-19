"""Plausibility window for user-submitted structure anchor dates
(app/intel/structure_age_check.py). Database tests run against the
straight-line synthetic table in tests/_structure_age.py. Ids are invented."""
from datetime import date, datetime, time, timedelta

import pytest

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

def _line(n=200):
    return [(1_050_000_000_000 + i * RATE, datetime.combine(D0 + timedelta(days=i), time()))
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


def test_interior_window_uses_the_median_and_caps_at_today():
    dates = [D0 + timedelta(days=i) for i in range(50)]
    w = sac.interior_window(dates, today=D0 + timedelta(days=60))
    assert w.expected in (D0 + timedelta(days=24), D0 + timedelta(days=25))
    assert w.low == w.expected - timedelta(days=sac.TOLERANCE_DAYS)
    assert w.high == D0 + timedelta(days=60)
    assert not w.tail


def test_one_wild_neighbour_doesnt_move_the_median():
    dates = [D0 + timedelta(days=i) for i in range(50)]
    dates[30] = D0 - timedelta(days=4 * 365)
    w = sac.interior_window(dates, today=D0 + timedelta(days=400))
    assert abs((w.expected - (D0 + timedelta(days=24))).days) <= 1


def test_tail_window_widens_with_distance_and_stops_at_the_frontier():
    t = date(2026, 10, 18)
    base_id, base = 1_055_000_000_000, t - timedelta(days=100)
    near = sac.tail_window(base_id + 10 * RATE, base_id, base, RATE, t)
    far = sac.tail_window(base_id + 90 * RATE, base_id, base, RATE, t)
    assert near.tail and near.expected == base + timedelta(days=10)
    assert (far.expected - far.low) > (near.expected - near.low)
    # 161 days past the base is today + 61: past the 60-day slack.
    assert sac.tail_window(base_id + 161 * RATE, base_id, base, RATE, t) is None


def test_an_absurd_id_hits_the_frontier_instead_of_overflowing():
    t = date(2026, 10, 18)
    assert sac.tail_window(sac.INT64_MAX, 1_055_000_000_000, t, RATE, t) is None


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


def test_interior_expected_date_is_the_neighbours_median():
    async def fn(SL):
        async with SL() as db:
            return await sac.plausible_window(db, MID_ID)
    w = _with_db(fn)
    assert not w.tail and abs((w.expected - point_date(100)).days) <= 1


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
            # 40 approved user points claiming a tenth of the real rate.
            for i in range(1, 41):
                db.add(StructureAgeCalibration(**sac.calibration_values(
                    NEWEST_ID + i * RATE // 10, point_date(POINTS - 1) + timedelta(days=i),
                    "day", datetime(2026, 10, 1))))
            await db.commit()
            return before, await sac.tail_rate(db)
    before, after = _with_db(fn)
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
    assert not full.tail and baseline.tail


def _check(structure_id, anchor, setup=None):
    async def fn(SL):
        async with SL() as db:
            if setup:
                await setup(db)
            return await sac.check_submission(db, structure_id, anchor, USER)
    return _with_db(fn, users=[(USER, "user")])


def test_a_sensible_interior_date_passes():
    v = _check(MID_ID, point_date(100))
    assert v.ok and v.window is not None


def test_a_recent_id_claimed_four_years_ago_is_refused():
    v = _check(MID_ID, today() - timedelta(days=4 * 365))
    assert not v.ok and v.code == "outside" and "plausible anchor date is" in v.reason


def test_a_sensible_tail_date_passes_and_an_old_one_doesnt():
    assert _check(TAIL_ID, point_date(POINTS - 1) + timedelta(days=28)).ok
    assert _check(TAIL_ID, today() - timedelta(days=4 * 365)).code == "outside"


def test_impossible_dates_and_ids_are_refused():
    assert _check(MID_ID, today() + timedelta(days=1)).code == "invalid"
    assert _check(MID_ID, date(2016, 1, 1)).code == "invalid"
    assert _check(1_020_999_999_999, point_date(0)).code == "invalid"
    assert _check(sac.INT64_MAX + 1, point_date(0)).code == "frontier"
    assert _check(NEWEST_ID + 200 * RATE, today() - timedelta(days=1)).code == "frontier"


def test_ids_with_a_point_or_a_pending_claim_are_refused():
    assert _check(point_id(50), point_date(50)).code == "confirmed"

    async def pending(db):
        db.add(StructureAgeSubmission(structure_id=MID_ID, anchor_date=point_date(100),
                                      certainty="day", status="pending",
                                      submitted_at=datetime(2026, 10, 1)))
        await db.commit()
    assert _check(MID_ID, point_date(100), setup=pending).code == "pending"


def test_a_user_can_have_at_most_twenty_pending():
    async def twenty(db):
        for i in range(sac.MAX_PENDING_PER_USER):
            db.add(StructureAgeSubmission(structure_id=point_id(i) + 1, anchor_date=point_date(i),
                                          certainty="day", status="pending", submitted_by=USER,
                                          submitted_at=datetime(2026, 10, 1)))
        await db.commit()
    assert _check(MID_ID, point_date(100), setup=twenty).code == "limit"
