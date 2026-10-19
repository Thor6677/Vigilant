"""Structure Age estimates past the newest calibration point, the
user-submitted tag, the window label and the live point count. Runs against
the synthetic table in tests/_structure_age.py. Ids and names are invented."""
from datetime import datetime, timedelta

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


def test_interpolation_between_points_is_unchanged():
    est = _estimate(point_id(100) + RATE // 2)
    assert est["method"] == "interpolate"
    assert abs((est["mid"].date() - point_date(100)).days) <= 1


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
    assert f"Uses {POINTS} local calibration points" in r.text and "36,403" not in r.text


def test_a_huge_pasted_number_gets_a_message_not_a_500(sa_db):
    r = client().get("/tools/structure-age", params={"paste": showinfo(int("9" * 25))})
    assert r.status_code == 200 and sac.FRONTIER_MSG in r.text
