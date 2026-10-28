"""Module state caps (SDE classifier) and the pure state resolver (ISS-156).

The resolver cases live in tests/fixtures/module_state_cases.json so the JS
mirror on the fitting page can be checked against the very same table. Type
ids and names below are invented; effect ids, categories and the group-limit
attribute are the real dogma ones.
"""
import json
from pathlib import Path

import pytest

from app.db.sde_models import (
    SDEEffect, SDEType, SDETypeDogmaAttribute, SDETypeEffect,
)
from app.fitting.constants import (
    ATTR_MAX_GROUP_ACTIVE, DEFAULT_ONLINE_EFFECTS, EFFECT_MJFG_CAPITAL,
    STATE_ACTIVE, STATE_OFFLINE, STATE_ONLINE, STATE_OVERHEATED,
)
from app.fitting.module_state import get_module_caps, resolve_states

from tests.test_fitting_engine import _Slice

CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "module_state_cases.json").read_text()
)

# Real effects: 16 online (cat 4), 10 targetAttack (cat 2), 5231 hardener
# (cat 1), 3001 overloadRofBonus (cat 5), 2857 cynosuralGeneration (cat 1),
# 4921 microJumpDrive (cat 1), 12126 microJumpPortalDriveCapital.
EFFECTS = {16: 4, 10: 2, 5231: 1, 3001: 5, 2857: 1, 4921: 1, 7012: 1,
           EFFECT_MJFG_CAPITAL: 1, 6731: 1, 9: 3}

PASSIVE_PLATE = 9501      # online effect only
TURRET = 9502             # online + targetAttack + overload, group 55
HARDENER = 9503           # online + hardener effect + overload
CYNO = 9504               # online + cynosural generation
ASSAULT_DC = 9505         # online + assault damage control (group-limited)
SECOND_DC = 9506          # same group as ASSAULT_DC
MJFG_CAP = 9507           # capital portal generator: not a default-online effect
AREA = 9508               # category-3 effect only (plus online)
NO_ROWS = 9509            # in the SDE with no effects
GHOST = 9510              # not in the SDE at all
PROP = 9511               # group-limited, limit 1 via attribute 763
PROP_ZERO = 9512          # attribute 763 present but 0


def _rows():
    rows = [SDEEffect(effect_id=e, effect_name=f"effect{e}", effect_category=c)
            for e, c in EFFECTS.items()]

    def mod(type_id, group, effects, name):
        rows.append(SDEType(type_id=type_id, type_name=name, group_id=group,
                            category_id=7))
        rows.extend(SDETypeEffect(type_id=type_id, effect_id=e) for e in effects)

    mod(PASSIVE_PLATE, 9601, [16], "Test Plate")
    mod(TURRET, 55, [16, 10, 3001], "Test Turret")
    mod(HARDENER, 9602, [16, 5231, 3001], "Test Hardener")
    mod(CYNO, 9603, [16, 2857], "Test Cyno")
    mod(ASSAULT_DC, 9604, [16, 7012], "Test Assault DC")
    mod(SECOND_DC, 9604, [16, 7012], "Test Second DC")
    mod(MJFG_CAP, 9605, [16, EFFECT_MJFG_CAPITAL], "Test Capital Portal")
    mod(AREA, 9606, [16, 9], "Test Area Module")
    mod(NO_ROWS, 9607, [], "Test Bare Item")
    mod(PROP, 9608, [16, 6731], "Test Propulsion")
    mod(PROP_ZERO, 9609, [16, 6731], "Test Unlimited Propulsion")
    rows += [
        SDETypeDogmaAttribute(type_id=PROP, attribute_id=ATTR_MAX_GROUP_ACTIVE,
                              value=1.0),
        SDETypeDogmaAttribute(type_id=ASSAULT_DC,
                              attribute_id=ATTR_MAX_GROUP_ACTIVE, value=1.0),
        SDETypeDogmaAttribute(type_id=SECOND_DC,
                              attribute_id=ATTR_MAX_GROUP_ACTIVE, value=1.0),
        SDETypeDogmaAttribute(type_id=PROP_ZERO,
                              attribute_id=ATTR_MAX_GROUP_ACTIVE, value=0.0),
    ]
    return rows


def _caps(type_ids):
    async def _go(db):
        return await get_module_caps(db, type_ids)
    return _Slice().run(_go, rows=_rows())


ALL = [PASSIVE_PLATE, TURRET, HARDENER, CYNO, ASSAULT_DC, SECOND_DC, MJFG_CAP,
       AREA, NO_ROWS, GHOST, PROP, PROP_ZERO]


@pytest.fixture(scope="module")
def caps():
    return _caps(ALL)


def test_keys_cover_every_requested_type(caps):
    assert set(caps) == set(ALL)
    for entry in caps.values():
        assert set(entry) == {"can_activate", "can_overheat", "default_active",
                              "group_id", "max_group_active"}


def test_empty_request_returns_empty():
    assert _caps([]) == {}


def test_online_effect_alone_cannot_activate(caps):
    c = caps[PASSIVE_PLATE]
    assert (c["can_activate"], c["can_overheat"], c["default_active"]) == (
        False, False, False)
    assert c["group_id"] == 9601


def test_target_effect_activates_and_heats(caps):
    c = caps[TURRET]
    assert c["can_activate"] and c["can_overheat"] and c["default_active"]
    assert c["group_id"] == 55 and c["max_group_active"] == 0


def test_active_effect_category_one(caps):
    assert caps[HARDENER]["can_activate"] and caps[HARDENER]["can_overheat"]


def test_area_effect_category_three_activates(caps):
    assert caps[AREA]["can_activate"] and caps[AREA]["default_active"]


def test_default_online_effect_rests_at_online(caps):
    for tid in (CYNO, ASSAULT_DC):
        assert caps[tid]["can_activate"] is True
        assert caps[tid]["default_active"] is False


def test_capital_portal_generator_defaults_active(caps):
    assert EFFECT_MJFG_CAPITAL not in DEFAULT_ONLINE_EFFECTS
    assert caps[MJFG_CAP]["default_active"] is True


def test_max_group_active_read_raw(caps):
    assert caps[PROP]["max_group_active"] == 1
    assert caps[PROP_ZERO]["max_group_active"] == 0     # zero means no limit
    assert caps[PASSIVE_PLATE]["max_group_active"] == 0  # absent too


def test_types_without_rows_get_inert_caps(caps):
    for tid in (NO_ROWS, GHOST):
        assert caps[tid]["can_activate"] is False
        assert caps[tid]["can_overheat"] is False
        assert caps[tid]["default_active"] is False
    assert caps[NO_ROWS]["group_id"] == 9607
    assert caps[GHOST]["group_id"] is None


def test_string_ids_are_accepted():
    assert set(_caps(["9502", 9503])) == {TURRET, HARDENER}


# ── resolve_states over the shared table ─────────────────────────────────

@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_resolve_states_case_table(case):
    caps = {int(k): v for k, v in case["caps"].items()}
    states, warnings = resolve_states(case["items"], caps)
    assert states == case["states"]
    assert warnings == case["warnings"]


def test_case_table_is_not_trivial():
    seen = {s for c in CASES for s in c["states"]}
    assert {None, STATE_OFFLINE, STATE_ONLINE, STATE_ACTIVE,
            STATE_OVERHEATED} <= seen
    assert any(c["warnings"] for c in CASES)


def test_resolve_does_not_mutate_inputs():
    case = next(c for c in CASES if c["name"].startswith("explicit choice"))
    items = json.loads(json.dumps(case["items"]))
    caps = {int(k): v for k, v in case["caps"].items()}
    resolve_states(items, caps)
    assert items == case["items"]


def test_resolve_with_real_caps():
    """Caps straight from the classifier feed the resolver."""
    caps = _caps([TURRET, CYNO, PROP, PASSIVE_PLATE])
    items = [
        {"type_id": TURRET, "slot": "high"},
        {"type_id": CYNO, "slot": "high"},
        {"type_id": PROP, "slot": "mid"},
        {"type_id": PROP, "slot": "mid"},
        {"type_id": PASSIVE_PLATE, "slot": "low"},
    ]
    states, warnings = resolve_states(items, caps)
    assert states == [STATE_ACTIVE, STATE_ONLINE, STATE_ACTIVE, STATE_ONLINE,
                      STATE_ONLINE]
    assert len(warnings) == 1
