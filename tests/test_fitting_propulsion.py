"""Afterburner / microwarpdrive mass, speed and signature (ISS-153).

In the real SDE effects 6731 (afterburner) and 6730 (microwarpdrive) have NO
modifier rows, so none of this can come out of the generic modifier pass; the
engine computes it in code, as Pyfa does. The seed in
``tests/test_fitting_engine.py`` models that real shape: both modules carry
speedFactor / speedBoostFactor / massAddition (and an MWD signatureRadiusBonus)
and no modifiers at all.

Seed hull: mass 10e6, maxVelocity 200, inertia 3.0, signature 100. Each
propulsion module adds 5e6 mass and has speedBoostFactor 15e6, so with no other
mass thrust / mass is 15e6 / 15e6 = 1 and the speed factor is
1 + speedFactor / 100.
"""
import math

import pytest

from app.db.sde_models import (
    SDEEffect, SDEModifier, SDEType, SDETypeDogmaAttribute, SDETypeEffect,
)
from app.fitting.constants import ATTR_MASS, ATTR_MAX_VELOCITY

from tests.test_fitting_engine import (
    AFTERBURNER, MWD, ATTR_MASS_ADDITION, OP_MOD_ADD, OP_POST_PERCENT,
    EFFECT_CAT_PASSIVE, SHIP, _Slice, _low, _mid, _seed_rows,
)
from app.fitting.engine import calculate_fitting_stats

PLATE = 9101
OVERDRIVE = 9102
ATTR_OVERDRIVE_BONUS = 9201   # fixture-local source attribute
EFF_PLATE = 9301
EFF_OVERDRIVE = 9302


def _rows_with_extras():
    """The shared seed plus a plate (+5e6 mass) and an overdrive (+10% speed)."""
    rows = _seed_rows()
    rows += [
        SDEType(type_id=PLATE, type_name="Test Armor Plate", group_id=9410,
                category_id=7),
        SDETypeDogmaAttribute(type_id=PLATE, attribute_id=ATTR_MASS_ADDITION,
                              value=5_000_000.0),
        SDEEffect(effect_id=EFF_PLATE, effect_name="testPlateMass",
                  effect_category=EFFECT_CAT_PASSIVE),
        SDETypeEffect(type_id=PLATE, effect_id=EFF_PLATE),
        SDEModifier(effect_id=EFF_PLATE, func="ItemModifier", domain="shipID",
                    modified_attribute_id=ATTR_MASS,
                    modifying_attribute_id=ATTR_MASS_ADDITION,
                    operator=OP_MOD_ADD),
        SDEType(type_id=OVERDRIVE, type_name="Test Overdrive", group_id=9411,
                category_id=7),
        SDETypeDogmaAttribute(type_id=OVERDRIVE, attribute_id=ATTR_OVERDRIVE_BONUS,
                              value=10.0),
        SDEEffect(effect_id=EFF_OVERDRIVE, effect_name="testOverdriveSpeed",
                  effect_category=EFFECT_CAT_PASSIVE),
        SDETypeEffect(type_id=OVERDRIVE, effect_id=EFF_OVERDRIVE),
        SDEModifier(effect_id=EFF_OVERDRIVE, func="ItemModifier", domain="shipID",
                    modified_attribute_id=ATTR_MAX_VELOCITY,
                    modifying_attribute_id=ATTR_OVERDRIVE_BONUS,
                    operator=OP_POST_PERCENT),
    ]
    return rows


def _stats(items):
    async def _calc(db):
        return await calculate_fitting_stats(db, SHIP, items)
    return _Slice().run(_calc, rows=_rows_with_extras())


def test_afterburner_adds_mass_and_align_time_rises():
    """Mass 10e6 + 5e6 = 15e6; align = -ln(0.25) * 3.0 * mass / 1e6.

    Bare: 1.3863 * 3.0 * 10 = 41.6 s. With the AB: 1.3863 * 3.0 * 15 = 62.4 s.
    """
    bare = _stats([])
    ab = _stats([_mid(AFTERBURNER)])
    assert bare["mass"] == 10_000_000
    assert bare["align_time"] == pytest.approx(-math.log(0.25) * 3.0 * 10, abs=0.1)
    assert ab["mass"] == 15_000_000
    assert ab["align_time"] == pytest.approx(-math.log(0.25) * 3.0 * 15, abs=0.1)
    assert ab["align_time"] > bare["align_time"]
    # An afterburner does not touch the signature.
    assert ab["sig_radius"] == pytest.approx(100.0, abs=0.1)


def test_microwarpdrive_speed_signature_and_mass():
    """thrust / mass = 1, speedFactor 500: 200 * (1 + 5.0) = 1200 m/s.

    Signature bonus 500%: 100 * 6 = 600 m. Mass 15e6.
    """
    mwd = _stats([_mid(MWD)])
    assert mwd["max_velocity"] == pytest.approx(1200.0, abs=0.5)
    assert mwd["sig_radius"] == pytest.approx(600.0, abs=0.1)
    assert mwd["mass"] == 15_000_000


def test_speed_reads_the_final_mass_including_a_plate():
    """The thrust divides by the LATE mass: hull 10e6 + AB 5e6 + plate 5e6.

    thrust / mass = 135 * 15e6 / 20e6 / 100 = 1.0125, so the speed is
    200 * 2.0125 = 402.5. Reading the mass before the plate would give 470.
    """
    stats = _stats([_mid(AFTERBURNER), _low(PLATE)])
    assert stats["mass"] == 20_000_000
    assert stats["max_velocity"] == pytest.approx(402.5, abs=0.5)


def test_afterburner_is_not_stacking_penalised_with_an_overdrive():
    """The AB factor has its own penalty group, apart from the overdrive's.

    Independent: 200 * 1.10 (overdrive) * 2.35 (AB) = 517.0.
    If both were penalised together the weaker 1.10 would be scaled by 0.869
    (200 * 2.35 * 1.0869 = 510.8), which is what this guards against.
    """
    stats = _stats([_mid(AFTERBURNER), _low(OVERDRIVE)])
    assert stats["max_velocity"] == pytest.approx(517.0, abs=0.5)


def test_only_the_first_propulsion_module_applies():
    """maxGroupActive is 1 and no module carries an `active` choice: both rest
    at the default (active), and the first in item order is the one running.

    [AB, MWD] -> AB only: 200 * 2.35 = 470, mass 15e6, signature unchanged.
    [MWD, AB] -> MWD only: 200 * 6 = 1200, signature 600.
    """
    ab_first = _stats([_mid(AFTERBURNER), _mid(MWD)])
    assert ab_first["max_velocity"] == pytest.approx(470.0, abs=0.5)
    assert ab_first["mass"] == 15_000_000
    assert ab_first["sig_radius"] == pytest.approx(100.0, abs=0.1)

    mwd_first = _stats([_mid(MWD), _mid(AFTERBURNER)])
    assert mwd_first["max_velocity"] == pytest.approx(1200.0, abs=0.5)
    assert mwd_first["mass"] == 15_000_000
    assert mwd_first["sig_radius"] == pytest.approx(600.0, abs=0.1)


def test_offline_afterburner_adds_no_mass_and_no_speed():
    stats = _stats([_mid(AFTERBURNER, online=False)])
    assert stats["mass"] == 10_000_000
    assert stats["max_velocity"] == pytest.approx(200.0, abs=0.5)


def test_overheated_microwarpdrive():
    """Heat raises speedFactor by 50%: 500 -> 750, so 200 * (1 + 7.5) = 1700.

    Heat does not change the signature bloom: still 600 m.
    """
    stats = _stats([_mid(MWD, overheated=True)])
    assert stats["max_velocity"] == pytest.approx(1700.0, abs=0.5)
    assert stats["sig_radius"] == pytest.approx(600.0, abs=0.1)


def test_explicit_active_choice_beats_item_order():
    """Choosing a module makes it the running one, whatever its position.

    With the group limit seeded (maxGroupActive 1, as in the real SDE) the
    resolver keeps the module the user switched on and rests the default one.
    [AB, MWD active] -> MWD: 1200 m/s, signature 600.
    [AB active:false, MWD] -> MWD (the AB is held online).
    [AB, MWD active:false] -> AB.
    """
    def run(items):
        async def _calc(db):
            return await calculate_fitting_stats(db, SHIP, items)
        rows = _rows_with_extras()
        rows += [SDETypeDogmaAttribute(type_id=t, attribute_id=763, value=1.0)
                 for t in (AFTERBURNER, MWD)]
        return _Slice().run(_calc, rows=rows)

    mwd_chosen = run([_mid(AFTERBURNER), _mid(MWD, active=True)])
    assert mwd_chosen["max_velocity"] == pytest.approx(1200.0, abs=0.5)
    assert mwd_chosen["sig_radius"] == pytest.approx(600.0, abs=0.1)
    assert mwd_chosen["module_states"] == [0, 1]

    ab_held = run([_mid(AFTERBURNER, active=False), _mid(MWD)])
    assert ab_held["max_velocity"] == pytest.approx(1200.0, abs=0.5)

    mwd_held = run([_mid(AFTERBURNER), _mid(MWD, active=False)])
    assert mwd_held["max_velocity"] == pytest.approx(470.0, abs=0.5)
    assert mwd_held["sig_radius"] == pytest.approx(100.0, abs=0.1)


def test_a_propulsion_module_held_at_online_adds_nothing():
    """Online but not running: no mass, no speed, no signature."""
    stats = _stats([_mid(MWD, active=False)])
    assert stats["mass"] == 10_000_000
    assert stats["max_velocity"] == pytest.approx(200.0, abs=0.5)
    assert stats["sig_radius"] == pytest.approx(100.0, abs=0.1)


def test_without_the_group_limit_the_first_running_module_still_wins():
    """A seed lacking attribute 763 must still run only one propulsion module."""
    stats = _stats([_mid(AFTERBURNER, active=False), _mid(MWD), _mid(AFTERBURNER)])
    assert stats["max_velocity"] == pytest.approx(1200.0, abs=0.5)
    assert stats["mass"] == 15_000_000
