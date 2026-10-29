"""GET /tools/fitting/module-caps (ISS-156): login-gated, capped, and backed by
get_module_caps. Type ids are invented; effect ids and categories are real."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.db.sde_models import (
    SDEEffect, SDEModuleSlot, SDEType, SDETypeDogmaAttribute, SDETypeEffect,
)
from app.routes import fitting as fitting_mod
from tests.test_fitting_engine import _Slice

PLAIN, RUNNER, ROCKET = 9701, 9702, 9703


def _rows():
    return [
        SDEEffect(effect_id=16, effect_name="online", effect_category=4),
        SDEEffect(effect_id=10, effect_name="targetAttack", effect_category=2),
        SDEEffect(effect_id=3001, effect_name="overloadRofBonus", effect_category=5),
        SDEType(type_id=PLAIN, type_name="Test Plate", group_id=9801, category_id=7),
        SDEType(type_id=RUNNER, type_name="Test Turret", group_id=55, category_id=7),
        SDETypeEffect(type_id=PLAIN, effect_id=16),
        SDETypeEffect(type_id=RUNNER, effect_id=16),
        SDETypeEffect(type_id=RUNNER, effect_id=10),
        SDETypeEffect(type_id=RUNNER, effect_id=3001),
        SDEType(type_id=ROCKET, type_name="Test Launcher", group_id=507, category_id=7),
        SDETypeEffect(type_id=ROCKET, effect_id=16),
        # T-086: hardpoint kind and fitting limits. 1544 = maxGroupFitted,
        # 2431 = maxTypeFitted.
        SDEModuleSlot(type_id=RUNNER, slot_type="high", is_turret=True, is_launcher=False),
        SDEModuleSlot(type_id=ROCKET, slot_type="high", is_turret=False, is_launcher=True),
        SDEModuleSlot(type_id=PLAIN, slot_type="low", is_turret=False, is_launcher=False),
        SDETypeDogmaAttribute(type_id=RUNNER, attribute_id=1544, value=2.0),
        SDETypeDogmaAttribute(type_id=ROCKET, attribute_id=2431, value=3.0),
        SDETypeDogmaAttribute(type_id=ROCKET, attribute_id=1544, value=0.0),
    ]


def _call(type_ids, user_id=1):
    request = SimpleNamespace(session={"user_id": user_id} if user_id else {})

    async def _go(db):
        return await fitting_mod.module_caps(request, type_ids=type_ids, db=db)
    return _Slice().run(_go, rows=_rows())


def test_shape_per_requested_id():
    out = _call(f"{PLAIN},{RUNNER}")
    assert set(out) == {str(PLAIN), str(RUNNER)}
    assert out[str(PLAIN)] == {"can_activate": False, "can_overheat": False,
                               "default_active": False, "group_id": 9801,
                               "max_group_active": 0, "hardpoint": None,
                               "max_group_fitted": 0, "max_type_fitted": 0}
    assert out[str(RUNNER)] == {"can_activate": True, "can_overheat": True,
                                "default_active": True, "group_id": 55,
                                "max_group_active": 0, "hardpoint": "turret",
                                "max_group_fitted": 2, "max_type_fitted": 0}


def test_hardpoint_and_fitting_limits_for_fill():
    out = _call(str(ROCKET))[str(ROCKET)]
    assert out["hardpoint"] == "launcher"
    assert out["max_type_fitted"] == 3 and out["max_group_fitted"] == 0


def test_unknown_id_is_answered_as_can_do_nothing():
    out = _call("424242")
    assert out["424242"]["can_activate"] is False and out["424242"]["group_id"] is None
    assert out["424242"]["hardpoint"] is None and out["424242"]["max_group_fitted"] == 0


def test_duplicates_collapse_and_empty_is_empty():
    assert list(_call(f"{PLAIN},{PLAIN}")) == [str(PLAIN)]
    assert _call("") == {}
    assert _call(" , ") == {}


def test_anonymous_is_refused():
    assert _call(str(PLAIN), user_id=None).status_code == 401


def test_junk_and_oversized_lists_are_refused():
    with pytest.raises(HTTPException) as e:
        _call("1,abc")
    assert e.value.status_code == 400
    too_many = ",".join(str(i) for i in range(fitting_mod.MODULE_CAPS_MAX_IDS + 1))
    with pytest.raises(HTTPException) as e:
        _call(too_many)
    assert e.value.status_code == 400
    assert len(_call(",".join(str(i) for i in range(fitting_mod.MODULE_CAPS_MAX_IDS)))) \
        == fitting_mod.MODULE_CAPS_MAX_IDS


def test_old_route_is_gone():
    paths = {getattr(r, "path", "") for r in fitting_mod.router.routes}
    assert "/tools/fitting/module-caps" in paths
    assert "/tools/fitting/can-overheat" not in paths
