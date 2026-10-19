"""Structure Age estimates past the newest calibration point, the
user-submitted tag, the window label and the live point count. Runs against
the synthetic table in tests/_structure_age.py. Ids and names are invented."""
import re
import time as _time
from datetime import datetime, time, timedelta, timezone

import pytest

from app.db.models import StructureAgeCalibration
from app.intel import structure_age_check as sac
from app.routes import structure_age as sa_mod
from tests._mobile import render_page
from tests._structure_age import (NEWEST_ID, POINTS, RATE, client, make_db, override_db,
                                  point_date, point_id, run, showinfo, today)


def _estimate(structure_id, setup=None):
    async def go():
        engine, SL = await make_db()
        try:
            async with SL() as db:
                if setup:
                    await setup(db)
                return await sa_mod._estimate(db, structure_id)
        finally:
            await engine.dispose()
    return run(go())


def test_a_new_id_is_extrapolated_not_pinned_to_the_newest_point():
    est = _estimate(NEWEST_ID + 30 * RATE)
    assert est["method"] == "extrapolate"
    want = point_date(POINTS - 1) + timedelta(days=30)
    assert abs((est["mid"].date() - want).days) <= 1
    assert est["high"].date() <= today()


def test_an_extrapolated_estimate_never_passes_today():
    est = _estimate(NEWEST_ID + 140 * RATE)          # expected ~39 days from now
    assert est["mid"].date() == today() and est["high"].date() == today()
    assert not sa_mod._age_str(est["mid"]).startswith("-")


def test_an_id_past_the_frontier_gets_a_message_not_a_date():
    assert _estimate(NEWEST_ID + 200 * RATE) == {"error": sac.FRONTIER_MSG}


def test_an_exact_hit_carries_its_source():
    async def user_point(db):
        db.add(StructureAgeCalibration(**sac.calibration_values(
            point_id(50) + 1, point_date(50), "day", datetime(2026, 10, 1))))
        await db.commit()
    est = _estimate(point_id(50) + 1, setup=user_point)
    assert est["method"] == "exact" and est["source"] == "user"
    assert _estimate(point_id(50))["source"] == "triff"


@pytest.fixture
def far_from_utc(monkeypatch):
    """Naive UTC read as local time only shows on a non-UTC host; CI is UTC."""
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    _time.tzset()
    yield
    monkeypatch.undo()
    _time.tzset()


def test_interpolation_between_points_is_unchanged(far_from_utc):
    est = _estimate(point_id(100) + RATE // 2)
    assert est["method"] == "interpolate"
    assert est["mid"] == datetime.combine(point_date(100), time()) + timedelta(hours=12)
    assert est["days_wide"] == 6.0


def _user_points(points):
    async def setup(db):
        for sid, when in points:
            db.add(StructureAgeCalibration(**sac.calibration_values(
                sid, when, "day", datetime(2026, 10, 1))))
        await db.commit()
    return setup


def _line(k):
    """The synthetic line's date k days of IDs past the newest point."""
    return point_date(POINTS - 1) + timedelta(days=k)


def test_one_user_point_is_one_vote_not_a_straight_line():
    """An approved point 30 days off the trend — an honest outlier ID — must
    not drag every estimate between it and the triff data, nor leave a jump
    at its ID (spec §5, decided 2026-10-19). A straight line to it put
    NEWEST+50 15 days off and jumped 31 days across two days of IDs."""
    outlier = _user_points([(NEWEST_ID + 101 * RATE, today() - timedelta(days=30))])
    est = _estimate(NEWEST_ID + 50 * RATE, setup=outlier)
    assert est["method"] == "interpolate" and abs((est["mid"].date() - _line(50)).days) <= 1
    assert "user-submitted" in est["note"]
    below = _estimate(NEWEST_ID + 100 * RATE, setup=outlier)["mid"].date()
    above = _estimate(NEWEST_ID + 102 * RATE, setup=outlier)["mid"].date()
    assert abs((above - below).days) <= 3


def test_as_user_points_accumulate_they_take_over():
    """Sixty approved points, all 30 days earlier than the line: between
    them, the estimate follows them, not the triff data."""
    early = _user_points([(NEWEST_ID + j * RATE + RATE // 2, _line(j - 30))
                          for j in range(1, 61)])
    est = _estimate(NEWEST_ID + 30 * RATE, setup=early)
    assert est["method"] == "interpolate"
    assert abs((est["mid"].date() - _line(0)).days) <= 1


def test_an_exact_user_point_never_shows_a_latest_date_after_today():
    async def recent_month_point(db):
        db.add(StructureAgeCalibration(**sac.calibration_values(
            NEWEST_ID + RATE, today() - timedelta(days=3), "month", datetime(2026, 10, 1))))
        await db.commit()
    est = _estimate(NEWEST_ID + RATE, setup=recent_month_point)
    assert est["method"] == "exact" and est["high"].date() <= today()
    assert est["days_wide"] < 30                 # the span actually shown
    # An ID interpolated just below that point is capped the same way.
    est = _estimate(NEWEST_ID + RATE - 1, setup=recent_month_point)
    assert est["method"] == "interpolate" and est["high"].date() <= today()


def test_age_reads_naturally_for_recent_dates():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert sa_mod._age_str(now) == "today"
    assert sa_mod._age_str(now - timedelta(days=1, hours=1)) == "1 day"
    assert sa_mod._age_str(now - timedelta(days=5, hours=1)) == "5 days"


def _card(**estimate):
    base = {"method": "exact", "age_str": "≈ 2 months", "days_wide": 12.5,
            "mid_str": "04 Jul 2026", "low_str": "28 Jun 2026", "high_str": "10 Jul 2026"}
    return render_page(sa_mod, "structure_age_result.html", "/tools/structure-age/partial",
                       session=None, paste="", parsed=None, error=None,
                       estimate={**base, **estimate})


def test_window_label_says_width_not_plus_minus():
    html = _card()
    assert "12.5-day window" in html and "±" not in html


def test_user_points_are_tagged_without_naming_anyone():
    assert "user-submitted" in _card(source="user")
    assert "user-submitted" not in _card(source="triff")


@pytest.fixture
def sa_db():
    engine, SL = run(make_db())
    undo = override_db(SL)
    yield SL
    undo()
    run(engine.dispose())


def test_page_shows_the_live_point_count(sa_db):
    r = client().get("/tools/structure-age")
    assert r.status_code == 200
    assert f"Uses {POINTS:,} local calibration points" in r.text and "36,403" not in r.text


@pytest.mark.parametrize("digits", [25, 5000])
def test_a_huge_pasted_number_gets_a_message_not_a_500(sa_db, digits):
    """25 digits overflow SQLite's INTEGER; past 4300, int() itself refuses."""
    huge = "9" * digits
    for paste in (huge, f"<url=showinfo:35833//{huge}>J123456 - Sample Citadel (Sample Corp)</url>"):
        for path in ("/tools/structure-age", "/tools/structure-age/partial"):
            r = client().get(path, params={"paste": paste})
            assert r.status_code == 200 and sac.FRONTIER_MSG in r.text, (digits, path)


def test_a_character_id_is_not_an_upwell_structure(sa_db):
    r = client().get("/tools/structure-age/partial", params={"paste": "2112345678"})
    assert r.status_code == 200 and "an Upwell structure ID" in r.text
    assert "sa-age" not in r.text


def test_a_new_structure_pasted_into_an_embed_is_extrapolated(sa_db):
    r = client().get("/tools/structure-age/partial",
                     params={"paste": showinfo(NEWEST_ID + 30 * RATE)})
    assert r.status_code == 200 and "is-extrapolate" in r.text
    assert "extrapolated from the recent ID rate" in r.text
    age = re.search(r'class="sa-age">([^<]*)<', r.text).group(1).strip()
    assert age and not age.startswith("-"), age
