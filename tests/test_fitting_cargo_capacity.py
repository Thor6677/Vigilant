"""A hull's cargo capacity comes from the type table (ISS-154).

Attribute 38 (capacity) is on no ship's dogma rows: the SDE keeps a ship's hold
in the type table only. The engine read attribute 38 alone, so the Cargo stat
was 0 for every hull; mass already had a type-table fallback and capacity now
has the same one, taken before the ship-self passes so a cargo rig, expander or
hauler skill modifies a real base. A dogma value, when a hull has one, wins.

The SDE slice is the one in tests/test_fitting_engine.py. Names and ids are
invented.
"""
import pytest

from app.db.sde_models import (
    SDEEffect, SDEModifier, SDEType, SDETypeDogmaAttribute, SDETypeEffect,
)
from app.fitting.constants import ATTR_CARGO_CAPACITY
from app.fitting.engine import calculate_fitting_stats
from tests.test_fitting_engine import OP_POST_PERCENT, SHIP, _Slice, _low, _seed_rows

HOLD = 830.0
EXPANDER = 9601
ATTR_EXPANDER_BONUS = 9611
EFF_EXPANDER = 8601


def _rows(type_capacity=HOLD, dogma_capacity=None, expander=False):
    rows = _seed_rows()
    for r in rows:
        if isinstance(r, SDEType) and r.type_id == SHIP:
            r.capacity = type_capacity
    if dogma_capacity is not None:
        rows.append(SDETypeDogmaAttribute(type_id=SHIP, attribute_id=ATTR_CARGO_CAPACITY,
                                          value=dogma_capacity))
    if expander:
        rows += [
            SDEType(type_id=EXPANDER, type_name="Test Cargohold Expander", group_id=9600,
                    category_id=7),
            SDETypeDogmaAttribute(type_id=EXPANDER, attribute_id=ATTR_EXPANDER_BONUS, value=27.5),
            SDEEffect(effect_id=EFF_EXPANDER, effect_name="testCargoExpander", effect_category=0),
            SDETypeEffect(type_id=EXPANDER, effect_id=EFF_EXPANDER),
            SDEModifier(effect_id=EFF_EXPANDER, func="ItemModifier", domain="shipID",
                        modified_attribute_id=ATTR_CARGO_CAPACITY,
                        modifying_attribute_id=ATTR_EXPANDER_BONUS,
                        operator=OP_POST_PERCENT),
        ]
    return rows


def _cargo(items=(), **row_args):
    async def _calc(db):
        return await calculate_fitting_stats(db, SHIP, list(items))
    return _Slice().run(_calc, rows=_rows(**row_args))["cargo_capacity"]


def test_cargo_comes_from_the_type_table_when_dogma_has_none():
    assert _cargo() == pytest.approx(HOLD, abs=0.05)


def test_a_dogma_capacity_wins_over_the_type_table():
    assert _cargo(dogma_capacity=900.0) == pytest.approx(900.0, abs=0.05)


def test_a_hull_with_no_hold_anywhere_still_reads_zero():
    assert _cargo(type_capacity=None) == 0.0
    assert _cargo(type_capacity=0.0) == 0.0


def test_a_cargo_module_modifies_the_real_base():
    assert _cargo([_low(EXPANDER)], expander=True) == pytest.approx(HOLD * 1.275, abs=0.05)
