"""The "All skills - Level N" presets floor the hull's own skills at 1 (ISS-158).

EVE Workbench's Level 0 still counts every skill the SHIP directly requires as
level 1 (the flat 1, not the required level; module, drone and charge skills
stay at 0), so a preset pilot can fly the hull. Characters and All V
(skill_levels=None) are untouched.

Built on the per-level-link slice plus invented skills, a drone and a hull
(9950s / 9960s). Route half: POST /tools/fitting/stats with `skill_preset`.
"""
from datetime import datetime

import pytest

from app.db.models import Character
from app.db.sde_models import SDEType, SDETypeSkillReq
from app.fitting.constants import ATTR_MAX_VELOCITY
from app.fitting.engine import calculate_fitting_stats, preset_skill_levels
import app.routes.fitting as fitting_mod
from tests.test_fitting_boosters_routes import _CSRF, _csrf_client, _seeded_db, USER_A
from tests.test_fitting_engine import AMMO, TURRET, _Slice, _high
from tests.test_fitting_per_level_link import (
    A_ODD_NAME, BASE_SPEED, HULL, SK_CLASS, SK_OTHER, SK_REQ, _hull, _rows,
)

SK_MODULE, SK_DRONE, SK_CHARGE = 9954, 9955, 9956     # category 16
SK_UNPUBLISHED = 9957
NOT_A_SKILL = 9958
HULL_REQ5 = 9962        # requires SK_CLASS at level 5
CHAR_ID = 9990001

BONUS = 5.0             # +5% max velocity per level of SK_CLASS


def _preset_rows():
    rows = _hull(HULL, "Test Hull", [(A_ODD_NAME, BONUS, ATTR_MAX_VELOCITY)])
    rows += _hull(HULL_REQ5, "Test Hull Five", [(A_ODD_NAME, BONUS, ATTR_MAX_VELOCITY)])
    rows += [SDEType(type_id=s, type_name=f"Test Skill {s}", group_id=9660,
                     category_id=16) for s in (SK_MODULE, SK_DRONE, SK_CHARGE)]
    rows += [SDEType(type_id=SK_UNPUBLISHED, type_name="Test Retired Skill",
                     group_id=9660, category_id=16, published=False),
             SDEType(type_id=NOT_A_SKILL, type_name="Test Hull Part", group_id=9670,
                     category_id=6)]
    rows += [
        # The hull's own requirements: a bonus skill at I, an unrelated one at V.
        SDETypeSkillReq(type_id=HULL, skill_type_id=SK_CLASS, required_level=1),
        SDETypeSkillReq(type_id=HULL, skill_type_id=SK_REQ, required_level=5),
        SDETypeSkillReq(type_id=HULL_REQ5, skill_type_id=SK_CLASS, required_level=5),
        # Module / drone / charge requirements: not the hull's, never floored.
        SDETypeSkillReq(type_id=TURRET, skill_type_id=SK_MODULE, required_level=4),
        SDETypeSkillReq(type_id=9959, skill_type_id=SK_DRONE, required_level=3),
        SDETypeSkillReq(type_id=AMMO, skill_type_id=SK_CHARGE, required_level=2),
    ]
    return _rows(extra=rows)


def _levels(hull, level):
    async def _go(db):
        return await preset_skill_levels(db, hull, level)
    return _Slice().run(_go, rows=_preset_rows())


def _speed(hull, skill_levels):
    async def _go(db):
        return await calculate_fitting_stats(db, hull, [], skill_levels=skill_levels)
    return _Slice().run(_go, rows=_preset_rows())["max_velocity"]


def test_a_hull_skill_with_a_per_level_bonus_counts_as_one_at_level_zero():
    levels = _levels(HULL, 0)
    assert levels[SK_CLASS] == 1
    assert _speed(HULL, levels) == pytest.approx(BASE_SPEED * (1 + BONUS / 100), abs=0.5)
    # What the preset replaces: every skill at 0 zeroes the bonus.
    assert _speed(HULL, {SK_CLASS: 0}) == pytest.approx(BASE_SPEED, abs=0.5)


def test_levels_above_zero_leave_the_hull_skill_untouched():
    levels = _levels(HULL, 3)
    assert levels[SK_CLASS] == 3
    assert _speed(HULL, levels) == pytest.approx(BASE_SPEED * (1 + 3 * BONUS / 100), abs=0.5)


def test_a_level_five_requirement_floors_to_one_not_five():
    levels = _levels(HULL_REQ5, 0)
    assert levels[SK_CLASS] == 1
    assert _speed(HULL_REQ5, levels) == pytest.approx(BASE_SPEED * (1 + BONUS / 100), abs=0.5)


def test_module_drone_and_charge_skills_are_not_floored():
    levels = _levels(HULL, 0)
    assert levels[SK_MODULE] == 0
    assert levels[SK_DRONE] == 0
    assert levels[SK_CHARGE] == 0


def test_the_hulls_other_skills_stay_at_the_preset_level():
    # SK_OTHER is not a requirement of HULL; SK_REQ is, so it is floored to 1.
    levels = _levels(HULL, 0)
    assert levels[SK_OTHER] == 0
    assert levels[SK_REQ] == 1


def test_every_published_skill_is_listed_and_nothing_else():
    levels = _levels(HULL, 2)
    assert {SK_CLASS, SK_OTHER, SK_REQ, SK_MODULE, SK_DRONE, SK_CHARGE} <= set(levels)
    assert SK_UNPUBLISHED not in levels
    assert NOT_A_SKILL not in levels


def test_the_floor_is_a_no_op_from_level_one_up():
    for n in (1, 2, 3, 4):
        levels = _levels(HULL, n)
        assert set(levels.values()) == {n}


# ── route ────────────────────────────────────────────────────────────────────

async def _seed_route(db):
    db.add_all(_preset_rows())
    db.add(Character(
        character_id=CHAR_ID, character_name="Test Pilot",
        access_token="dummy-access", refresh_token="dummy-refresh",
        token_expiry=datetime(2099, 1, 1),
        scopes=fitting_mod._SKILLS_SCOPE, user_id=USER_A,
    ))


def _post(monkeypatch, extra):
    """POST the stats route; return (skill_levels the engine got, stats, html)."""
    captured = {}
    orig = fitting_mod.calculate_fitting_stats

    async def spy(*args, **kwargs):
        captured["skill_levels"] = kwargs.get("skill_levels")
        captured["stats"] = await orig(*args, **kwargs)
        return captured["stats"]
    monkeypatch.setattr(fitting_mod, "calculate_fitting_stats", spy)

    async def fake_skills(db, char):
        return {SK_CLASS: 2}
    monkeypatch.setattr(fitting_mod, "_character_skills_map", fake_skills)

    teardown = _seeded_db(_seed_route)
    try:
        r = _csrf_client().post(
            "/tools/fitting/stats",
            json={"ship_type_id": HULL, "items": [], **extra},
            headers={"X-CSRF-Token": _CSRF})
        assert r.status_code == 200, r.text
    finally:
        teardown()
    return captured["skill_levels"], captured["stats"], r.text


def test_route_preset_zero_changes_the_result_and_labels_it(monkeypatch):
    base_levels, base_stats, base_html = _post(monkeypatch, {})
    levels, stats, html = _post(monkeypatch, {"skill_preset": 0})
    assert base_levels is None
    assert levels[SK_CLASS] == 1 and levels[SK_MODULE] == 0
    assert stats["max_velocity"] != base_stats["max_velocity"]
    assert "All skills - Level 0" in html
    assert "All skills - Level" not in base_html


@pytest.mark.parametrize("bad", [-1, 5, 9, "x", "3", 2.5, True, None, [1]])
def test_route_ignores_invalid_presets(monkeypatch, bad):
    levels, _stats, html = _post(monkeypatch, {"skill_preset": bad})
    assert levels is None
    assert "All skills - Level" not in html


def test_route_character_wins_over_the_preset(monkeypatch):
    levels, _stats, html = _post(
        monkeypatch, {"skill_preset": 0, "character_id": CHAR_ID})
    assert levels == {SK_CLASS: 2}
    assert "Test Pilot" in html
    assert "All skills - Level" not in html


# ── template ─────────────────────────────────────────────────────────────────

def test_the_selector_offers_every_preset_and_the_js_handles_them():
    src = open("app/templates/fitting_tool.html", encoding="utf-8").read()
    for n in range(5):
        assert f'<option value="lvl:{n}">All skills - Level {n}</option>' in src
    assert "getSelectedSkillPreset" in src
    assert "skill_preset: getSelectedSkillPreset()" in src
