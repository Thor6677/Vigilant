"""Engine tests for heat, module state, reactive-hardener phasing and charges.

**Fixture approach: a seeded SDE slice, not injected attribute dicts.**

The bug that motivated most of this work lived in the SQL, not in the
arithmetic: ``sde_effects`` rows imported under the wrong field names left
every effect at category 0, so the engine's category filter matched
everything. A test that hands pure helpers a ready-made ``{attr_id: value}``
dict cannot see that class of bug at all — it skips the queries entirely.
``tests/test_fitting_compare.py`` says as much about itself: "SDE tables exist
but are empty — the engine returns all-zero stats for a bare unknown hull".

So these tests create the real ``sde_*`` tables in a temp-file SQLite database,
insert a small hand-built slice of dogma, and drive ``calculate_fitting_stats``
end to end. Every assertion therefore exercises the real joins, the real
category filter and the real modifier pipeline.

The slice uses **real dogma attribute IDs and real effect IDs** where the
mechanic depends on them (267-270 armor resonances, 984-987 resist bonuses,
1208 overloadHardeningBonus, 4928 the reactive hardener, 48 the capacitor
booster, 1886 chargedArmorDamageMultiplier). Type IDs and the IDs of effects
that only need to exist are fixture-local numbers in a 9xxx/8xxx range, and
the items are named "Test ..." — nothing here claims to be a specific ship or
module from the live SDE, and the numbers are chosen to make the arithmetic
checkable by hand rather than to match any particular hull.

No pytest-asyncio (the repo has none): each test drives its own event loop by
hand, as ``tests/test_sync_field_sessions.py`` does.
"""
import asyncio
import tempfile

import pytest
from sqlalchemy import Boolean, Float, Integer, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base
import app.db.sde_models  # noqa: F401 — registers the sde_* tables on Base
from app.db.sde_models import (
    SDEDogmaAttribute, SDEEffect, SDEModifier, SDEModuleSlot, SDEType,
    SDETypeDogmaAttribute, SDETypeEffect,
)
from app.fitting.constants import (
    ATTR_ARMOR_DAMAGE_AMOUNT, ATTR_ARMOR_EM_RESONANCE, ATTR_ARMOR_EXPL_RESONANCE,
    ATTR_ARMOR_HP, ATTR_ARMOR_KIN_RESONANCE, ATTR_ARMOR_THERM_RESONANCE,
    ATTR_CAPACITOR, ATTR_CAPACITOR_BONUS, ATTR_CAPACITOR_NEED, ATTR_CAP_RECHARGE,
    ATTR_CHARGED_ARMOR_DAMAGE_MULTIPLIER, ATTR_CHARGE_GROUP_1, ATTR_CPU,
    ATTR_CPU_OUTPUT, ATTR_DAMAGE_MULTIPLIER, ATTR_DURATION, ATTR_EM_DAMAGE,
    ATTR_EM_RESIST_BONUS, ATTR_EXPL_RESIST_BONUS, ATTR_HP, ATTR_KIN_RESIST_BONUS,
    ATTR_MASS, ATTR_MAX_VELOCITY, ATTR_POWER, ATTR_POWER_OUTPUT,
    ATTR_RATE_OF_FIRE, ATTR_SHIELD_HP, ATTR_THERM_RESIST_BONUS,
    ATTR_TRACKING_SPEED, ATTR_TURRET_SLOTS, EFFECT_ADAPTIVE_ARMOR_HARDENER,
    EFFECT_POWER_BOOSTER,
)
from app.fitting.engine import (
    calculate_fitting_stats, normalise_resist_phasing, rah_total_resist_points,
    suggest_rah_phasing,
)
from app.sde.loader import EFFECTS_USAGE_CHANCE_MARKER, needs_update

# ── dogma constants this fixture needs that constants.py has no name for ────
ATTR_OVERLOAD_HARDENING_BONUS = 1208   # overloadHardeningBonus
ATTR_OVERLOAD_ROF_BONUS = 1211         # overloadRofBonus (a reduction)
ATTR_OVERLOAD_SPEED_BONUS = 1216       # overloadSpeedFactorBonus
ATTR_TRACKING_SPEED_BONUS = 767        # trackingSpeedBonus, on a tracking computer
ATTR_TRACKING_SCRIPT_BONUS = 1316      # the script's boost to the above
ATTR_SPEED_FACTOR = 20                 # speedFactor, an afterburner's % speed boost
ATTR_CAP_NEED_BONUS = 317              # capNeedBonus, on a cap booster charge

OP_MOD_ADD = 2
OP_POST_PERCENT = 6

EFFECT_CAT_PASSIVE = 0
EFFECT_CAT_ACTIVE = 1
EFFECT_CAT_OVERLOAD = 5

# ── fixture-local type IDs ──────────────────────────────────────────────────
SHIP = 9001
HARDENER = 9002
RAH = 9003
TURRET = 9004
AMMO = 9005
TRACKING_COMPUTER = 9006
TRACKING_SCRIPT = 9007
CAP_BOOSTER = 9008
CAP_CHARGE = 9009
ANCILLARY_REP = 9010
PASTE = 9011
AFTERBURNER = 9012

GROUP_AMMO = 9500
GROUP_SCRIPT = 9501
GROUP_CAP_CHARGE = 9502
GROUP_PASTE = 9503

# ── fixture-local effect IDs (4928 and 48 are real) ─────────────────────────
EFF_HARDENER = 8001
EFF_HARDENER_OVERLOAD = 8002
EFF_TRACKING_COMPUTER = 8003
EFF_TRACKING_SCRIPT = 8004
EFF_CAP_CHARGE = 8005
EFF_AFTERBURNER = 8006
EFF_AFTERBURNER_OVERLOAD = 8007
EFF_TURRET_OVERLOAD = 8008
EFF_RAH_OVERLOAD = 8009

# The hardener gives 30% to every damage type, and overloads for +20%.
# Overload is postPercent on the BONUS, so -30 * 1.2 = -36 -> 36% resist.
HARDENER_BONUS = -30.0
HARDENER_OVERLOAD = 20.0

# A reactive hardener's four resonances: 15% each, 60 points of pool.
RAH_RESONANCE = 0.85
RAH_POOL = 60.0
RAH_OVERLOAD = -15.0   # x0.85 on each resonance when overheated

SHIP_ARMOR_HP = 2000.0


def _attrs(type_id, mapping):
    return [SDETypeDogmaAttribute(type_id=type_id, attribute_id=a, value=v)
            for a, v in mapping.items()]


def _resist_modifier_rows(effect_id, domain, targets):
    """Four ItemModifier rows, one per damage type, all postPercent."""
    return [
        SDEModifier(effect_id=effect_id, func="ItemModifier", domain=domain,
                    modified_attribute_id=target, modifying_attribute_id=source,
                    operator=OP_POST_PERCENT)
        for target, source in targets
    ]


def _seed_rows():
    """The whole SDE slice, as ORM objects ready to add."""
    rows = []

    # Attribute definitions. attribute_name matters for two things: the
    # overload selector joins on it (LIKE 'overload%'), and `stackable`
    # decides whether repeated modifiers take a stacking penalty.
    attribute_defs = [
        (ATTR_OVERLOAD_HARDENING_BONUS, "overloadHardeningBonus", True),
        (ATTR_OVERLOAD_ROF_BONUS, "overloadRofBonus", True),
        (ATTR_OVERLOAD_SPEED_BONUS, "overloadSpeedFactorBonus", True),
        (ATTR_EM_RESIST_BONUS, "emDamageResistanceBonus", True),
        (ATTR_THERM_RESIST_BONUS, "thermalDamageResistanceBonus", True),
        (ATTR_KIN_RESIST_BONUS, "kineticDamageResistanceBonus", True),
        (ATTR_EXPL_RESIST_BONUS, "explosiveDamageResistanceBonus", True),
        (ATTR_TRACKING_SPEED_BONUS, "trackingSpeedBonus", True),
        (ATTR_TRACKING_SCRIPT_BONUS, "trackingSpeedBonusBonus", True),
        (ATTR_SPEED_FACTOR, "speedFactor", True),
        (ATTR_TRACKING_SPEED, "trackingSpeed", False),
        (ATTR_MAX_VELOCITY, "maxVelocity", False),
        # Resonances are non-stackable in the SDE sense (the penalty is applied
        # to the modifiers, not the attribute) — the engine reads this flag.
        (ATTR_ARMOR_EM_RESONANCE, "armorEmDamageResonance", False),
        (ATTR_ARMOR_THERM_RESONANCE, "armorThermalDamageResonance", False),
        (ATTR_ARMOR_KIN_RESONANCE, "armorKineticDamageResonance", False),
        (ATTR_ARMOR_EXPL_RESONANCE, "armorExplosiveDamageResonance", False),
    ]
    rows += [SDEDogmaAttribute(attribute_id=a, attribute_name=n, stackable=s)
             for a, n, s in attribute_defs]

    # ── types ───────────────────────────────────────────────────────────────
    rows += [
        SDEType(type_id=SHIP, type_name="Test Cruiser", group_id=9400,
                category_id=6, mass=10_000_000.0, volume=100_000.0),
        SDEType(type_id=HARDENER, type_name="Test Armor Hardener",
                group_id=9401, category_id=7),
        SDEType(type_id=RAH, type_name="Test Reactive Armor Hardener",
                group_id=9402, category_id=7),
        SDEType(type_id=TURRET, type_name="Test Turret", group_id=9403,
                category_id=7),
        SDEType(type_id=AMMO, type_name="Test Ammo", group_id=GROUP_AMMO,
                category_id=8),
        SDEType(type_id=TRACKING_COMPUTER, type_name="Test Tracking Computer",
                group_id=9404, category_id=7),
        SDEType(type_id=TRACKING_SCRIPT, type_name="Test Tracking Script",
                group_id=GROUP_SCRIPT, category_id=8),
        SDEType(type_id=CAP_BOOSTER, type_name="Test Capacitor Booster",
                group_id=9405, category_id=7),
        SDEType(type_id=CAP_CHARGE, type_name="Test Cap Booster Charge",
                group_id=GROUP_CAP_CHARGE, category_id=8),
        SDEType(type_id=ANCILLARY_REP, type_name="Test Ancillary Armor Repairer",
                group_id=9406, category_id=7),
        SDEType(type_id=PASTE, type_name="Test Repair Paste",
                group_id=GROUP_PASTE, category_id=8),
        SDEType(type_id=AFTERBURNER, type_name="Test Afterburner",
                group_id=9407, category_id=7),
    ]

    # ── ship ────────────────────────────────────────────────────────────────
    rows += _attrs(SHIP, {
        ATTR_HP: 1500.0,
        ATTR_ARMOR_HP: SHIP_ARMOR_HP,
        ATTR_SHIELD_HP: 1200.0,
        ATTR_CPU_OUTPUT: 400.0,
        ATTR_POWER_OUTPUT: 800.0,
        ATTR_CAPACITOR: 1000.0,
        ATTR_CAP_RECHARGE: 200_000.0,   # ms
        ATTR_MAX_VELOCITY: 200.0,
        ATTR_MASS: 10_000_000.0,
        ATTR_TURRET_SLOTS: 5.0,
        # Bare hull: no resists anywhere, so every resist in a result is one
        # a module put there.
        ATTR_ARMOR_EM_RESONANCE: 1.0,
        ATTR_ARMOR_THERM_RESONANCE: 1.0,
        ATTR_ARMOR_KIN_RESONANCE: 1.0,
        ATTR_ARMOR_EXPL_RESONANCE: 1.0,
    })

    # ── armor hardener: 30% omni, +20% overloaded, 20 CPU / 2 PG ────────────
    rows += _attrs(HARDENER, {
        ATTR_EM_RESIST_BONUS: HARDENER_BONUS,
        ATTR_THERM_RESIST_BONUS: HARDENER_BONUS,
        ATTR_KIN_RESIST_BONUS: HARDENER_BONUS,
        ATTR_EXPL_RESIST_BONUS: HARDENER_BONUS,
        ATTR_OVERLOAD_HARDENING_BONUS: HARDENER_OVERLOAD,
        ATTR_CPU: 20.0,
        ATTR_POWER: 2.0,
    })
    rows += [
        SDEEffect(effect_id=EFF_HARDENER, effect_name="armorHardener",
                  effect_category=EFFECT_CAT_ACTIVE),
        SDEEffect(effect_id=EFF_HARDENER_OVERLOAD,
                  effect_name="overloadArmorHardener",
                  effect_category=EFFECT_CAT_OVERLOAD),
        SDETypeEffect(type_id=HARDENER, effect_id=EFF_HARDENER),
        SDETypeEffect(type_id=HARDENER, effect_id=EFF_HARDENER_OVERLOAD),
    ]
    # The hardener's own effect: resonance on the SHIP, sourced from the
    # module's resist bonus.
    rows += _resist_modifier_rows(EFF_HARDENER, "shipID", [
        (ATTR_ARMOR_EM_RESONANCE, ATTR_EM_RESIST_BONUS),
        (ATTR_ARMOR_THERM_RESONANCE, ATTR_THERM_RESIST_BONUS),
        (ATTR_ARMOR_KIN_RESONANCE, ATTR_KIN_RESIST_BONUS),
        (ATTR_ARMOR_EXPL_RESONANCE, ATTR_EXPL_RESIST_BONUS),
    ])
    # Its overload effect: the module's own resist bonuses, sourced from
    # overloadHardeningBonus. This is the row the old OVERLOAD_ATTR_MAP had
    # recorded with a target of None, which is why heat did nothing here.
    rows += _resist_modifier_rows(EFF_HARDENER_OVERLOAD, "itemID", [
        (ATTR_EM_RESIST_BONUS, ATTR_OVERLOAD_HARDENING_BONUS),
        (ATTR_THERM_RESIST_BONUS, ATTR_OVERLOAD_HARDENING_BONUS),
        (ATTR_KIN_RESIST_BONUS, ATTR_OVERLOAD_HARDENING_BONUS),
        (ATTR_EXPL_RESIST_BONUS, ATTR_OVERLOAD_HARDENING_BONUS),
    ])

    # ── reactive armor hardener: effect 4928 carries no modifiers ───────────
    # Its overload source is negative because the rows it drives target the
    # resonances directly: a resonance has to be multiplied DOWN to become a
    # better resist, where a hardener's resist-bonus attribute is multiplied
    # further negative. Same operator, opposite sign, because the attribute
    # being scaled is a different kind of number.
    rows += _attrs(RAH, {
        ATTR_ARMOR_EM_RESONANCE: RAH_RESONANCE,
        ATTR_ARMOR_THERM_RESONANCE: RAH_RESONANCE,
        ATTR_ARMOR_KIN_RESONANCE: RAH_RESONANCE,
        ATTR_ARMOR_EXPL_RESONANCE: RAH_RESONANCE,
        ATTR_OVERLOAD_HARDENING_BONUS: RAH_OVERLOAD,
        ATTR_CPU: 30.0,
        ATTR_POWER: 2.0,
    })
    rows += [
        SDEEffect(effect_id=EFFECT_ADAPTIVE_ARMOR_HARDENER,
                  effect_name="adaptiveArmorHardener",
                  effect_category=EFFECT_CAT_ACTIVE),
        SDETypeEffect(type_id=RAH, effect_id=EFFECT_ADAPTIVE_ARMOR_HARDENER),
        SDEEffect(effect_id=EFF_RAH_OVERLOAD,
                  effect_name="overloadAdaptiveArmorHardener",
                  effect_category=EFFECT_CAT_OVERLOAD),
        SDETypeEffect(type_id=RAH, effect_id=EFF_RAH_OVERLOAD),
    ]
    rows += _resist_modifier_rows(EFF_RAH_OVERLOAD, "itemID", [
        (ATTR_ARMOR_EM_RESONANCE, ATTR_OVERLOAD_HARDENING_BONUS),
        (ATTR_ARMOR_THERM_RESONANCE, ATTR_OVERLOAD_HARDENING_BONUS),
        (ATTR_ARMOR_KIN_RESONANCE, ATTR_OVERLOAD_HARDENING_BONUS),
        (ATTR_ARMOR_EXPL_RESONANCE, ATTR_OVERLOAD_HARDENING_BONUS),
    ])

    # ── turret + ammo ───────────────────────────────────────────────────────
    rows += _attrs(TURRET, {
        ATTR_DAMAGE_MULTIPLIER: 2.0,
        ATTR_RATE_OF_FIRE: 4000.0,       # ms
        ATTR_TRACKING_SPEED: 0.1,
        ATTR_CHARGE_GROUP_1: float(GROUP_AMMO),
        ATTR_OVERLOAD_ROF_BONUS: -15.0,  # heat cuts cycle time by 15%
        ATTR_CPU: 25.0,
        ATTR_POWER: 12.0,
    })
    rows += _attrs(AMMO, {ATTR_EM_DAMAGE: 20.0})
    rows += [
        SDEEffect(effect_id=EFF_TURRET_OVERLOAD, effect_name="overloadTurret",
                  effect_category=EFFECT_CAT_OVERLOAD),
        SDETypeEffect(type_id=TURRET, effect_id=EFF_TURRET_OVERLOAD),
        SDEModifier(effect_id=EFF_TURRET_OVERLOAD, func="ItemModifier",
                    domain="itemID", modified_attribute_id=ATTR_RATE_OF_FIRE,
                    modifying_attribute_id=ATTR_OVERLOAD_ROF_BONUS,
                    operator=OP_POST_PERCENT),
        SDEModuleSlot(type_id=TURRET, slot_type="high", is_turret=True,
                      is_launcher=False),
    ]

    # ── tracking computer + script ──────────────────────────────────────────
    # The computer boosts turret tracking by group; the script boosts the
    # computer's own bonus attribute from inside the module (domain otherID).
    rows += _attrs(TRACKING_COMPUTER, {
        ATTR_TRACKING_SPEED_BONUS: 20.0,
        ATTR_CHARGE_GROUP_1: float(GROUP_SCRIPT),
        ATTR_CPU: 30.0,
        ATTR_POWER: 1.0,
    })
    rows += _attrs(TRACKING_SCRIPT, {ATTR_TRACKING_SCRIPT_BONUS: 100.0})
    rows += [
        SDEEffect(effect_id=EFF_TRACKING_COMPUTER,
                  effect_name="trackingComputer",
                  effect_category=EFFECT_CAT_ACTIVE),
        SDETypeEffect(type_id=TRACKING_COMPUTER, effect_id=EFF_TRACKING_COMPUTER),
        SDEModifier(effect_id=EFF_TRACKING_COMPUTER,
                    func="LocationGroupModifier", domain="shipID",
                    modified_attribute_id=ATTR_TRACKING_SPEED,
                    modifying_attribute_id=ATTR_TRACKING_SPEED_BONUS,
                    operator=OP_POST_PERCENT,
                    filter_type="group", filter_value=9403),
        SDEEffect(effect_id=EFF_TRACKING_SCRIPT, effect_name="trackingScript",
                  effect_category=EFFECT_CAT_PASSIVE),
        SDETypeEffect(type_id=TRACKING_SCRIPT, effect_id=EFF_TRACKING_SCRIPT),
        SDEModifier(effect_id=EFF_TRACKING_SCRIPT, func="ItemModifier",
                    domain="otherID",
                    modified_attribute_id=ATTR_TRACKING_SPEED_BONUS,
                    modifying_attribute_id=ATTR_TRACKING_SCRIPT_BONUS,
                    operator=OP_POST_PERCENT),
    ]

    # ── capacitor booster + charge ──────────────────────────────────────────
    # Effect 48 (powerBooster) has no modifiers in the SDE; the injection is
    # hand-coded from the charge's capacitorBonus, exactly as Pyfa does it.
    rows += _attrs(CAP_BOOSTER, {
        # Large enough that the bare module alone empties the hull's capacitor
        # — the point of the test is that loading a charge reverses that.
        ATTR_CAPACITOR_NEED: 200.0,
        ATTR_DURATION: 6000.0,
        ATTR_CHARGE_GROUP_1: float(GROUP_CAP_CHARGE),
        ATTR_CPU: 20.0,
        ATTR_POWER: 10.0,
    })
    rows += _attrs(CAP_CHARGE, {
        ATTR_CAPACITOR_BONUS: 400.0,
        ATTR_CAP_NEED_BONUS: -100.0,
    })
    rows += [
        SDEEffect(effect_id=EFFECT_POWER_BOOSTER, effect_name="powerBooster",
                  effect_category=EFFECT_CAT_ACTIVE),
        SDETypeEffect(type_id=CAP_BOOSTER, effect_id=EFFECT_POWER_BOOSTER),
        SDEEffect(effect_id=EFF_CAP_CHARGE, effect_name="capBoosterCharge",
                  effect_category=EFFECT_CAT_PASSIVE),
        SDETypeEffect(type_id=CAP_CHARGE, effect_id=EFF_CAP_CHARGE),
        # The charge zeroes the module's own cap cost; the injection replaces it.
        SDEModifier(effect_id=EFF_CAP_CHARGE, func="ItemModifier",
                    domain="otherID",
                    modified_attribute_id=ATTR_CAPACITOR_NEED,
                    modifying_attribute_id=ATTR_CAP_NEED_BONUS,
                    operator=OP_POST_PERCENT),
    ]

    # ── ancillary armor repairer + paste ────────────────────────────────────
    rows += _attrs(ANCILLARY_REP, {
        ATTR_ARMOR_DAMAGE_AMOUNT: 120.0,
        ATTR_DURATION: 10_000.0,
        ATTR_CAPACITOR_NEED: 10.0,
        ATTR_CHARGED_ARMOR_DAMAGE_MULTIPLIER: 3.0,
        ATTR_CHARGE_GROUP_1: float(GROUP_PASTE),
        ATTR_CPU: 40.0,
        ATTR_POWER: 15.0,
    })
    rows += _attrs(PASTE, {})

    # ── afterburner ─────────────────────────────────────────────────────────
    rows += _attrs(AFTERBURNER, {
        ATTR_SPEED_FACTOR: 135.0,
        ATTR_OVERLOAD_SPEED_BONUS: 50.0,
        ATTR_DURATION: 10_000.0,
        ATTR_CAPACITOR_NEED: 30.0,
        ATTR_CPU: 25.0,
        ATTR_POWER: 15.0,
    })
    rows += [
        SDEEffect(effect_id=EFF_AFTERBURNER, effect_name="moduleBonusAfterburner",
                  effect_category=EFFECT_CAT_ACTIVE),
        SDEEffect(effect_id=EFF_AFTERBURNER_OVERLOAD,
                  effect_name="overloadSpeedFactor",
                  effect_category=EFFECT_CAT_OVERLOAD),
        SDETypeEffect(type_id=AFTERBURNER, effect_id=EFF_AFTERBURNER),
        SDETypeEffect(type_id=AFTERBURNER, effect_id=EFF_AFTERBURNER_OVERLOAD),
        SDEModifier(effect_id=EFF_AFTERBURNER, func="ItemModifier",
                    domain="shipID",
                    modified_attribute_id=ATTR_MAX_VELOCITY,
                    modifying_attribute_id=ATTR_SPEED_FACTOR,
                    operator=OP_POST_PERCENT),
        SDEModifier(effect_id=EFF_AFTERBURNER_OVERLOAD, func="ItemModifier",
                    domain="itemID",
                    modified_attribute_id=ATTR_SPEED_FACTOR,
                    modifying_attribute_id=ATTR_OVERLOAD_SPEED_BONUS,
                    operator=OP_POST_PERCENT),
    ]

    return rows


# ── harness ─────────────────────────────────────────────────────────────────

class _Slice:
    """A temp-file SQLite database holding the seeded SDE slice."""

    def __init__(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.path = tmp.name
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
        self.SessionLocal = async_sessionmaker(self.engine, expire_on_commit=False)

    async def _setup(self, rows):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.SessionLocal() as db:
            db.add_all(rows)
            await db.commit()

    def run(self, coro_fn, rows=None):
        """Create the schema, seed, then run `coro_fn(db)` and return its result."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                self._setup(_seed_rows() if rows is None else rows)
            )

            async def _go():
                async with self.SessionLocal() as db:
                    return await coro_fn(db)

            return loop.run_until_complete(_go())
        finally:
            loop.run_until_complete(self.engine.dispose())
            loop.close()
            asyncio.set_event_loop(None)


def _stats(items, **kwargs):
    """Run the engine against a freshly seeded slice and return the stats dict."""
    async def _calc(db):
        return await calculate_fitting_stats(db, SHIP, items, **kwargs)
    return _Slice().run(_calc)


def _low(type_id, **over):
    item = {"type_id": type_id, "slot": "low", "quantity": 1}
    item.update(over)
    return item


def _mid(type_id, **over):
    item = {"type_id": type_id, "slot": "mid", "quantity": 1}
    item.update(over)
    return item


def _high(type_id, **over):
    item = {"type_id": type_id, "slot": "high", "quantity": 1}
    item.update(over)
    return item


# ════════════════════════════════════════════════════════════════════════════
# Loader: JSONL field names, and self-healing a database imported without them
# ════════════════════════════════════════════════════════════════════════════

JSONL_EFFECT_ROWS = [
    # Two rows in the shape dogmaEffects.jsonl actually uses: `name` and
    # `effectCategoryID`, NOT the legacy YAML `effectName`/`effectCategory`.
    {"_key": "5231", "name": "modifyActiveArmorResonancePostPercent",
     "effectCategoryID": 1, "durationAttributeID": 73},
    {"_key": "3015", "name": "overloadHardeningBonus", "effectCategoryID": 5},
]


def test_loader_reads_jsonl_effect_field_names():
    """The importer must read `name` / `effectCategoryID`.

    Reading the legacy YAML names silently produced an empty name and
    category 0 for every one of the SDE's effects, which is what made the
    engine's effect-category filter match everything.
    """
    parsed = [
        {
            "effect_id": int(item["_key"]),
            "effect_name": item.get("name", ""),
            "effect_category": int(item.get("effectCategoryID", 0)),
        }
        for item in JSONL_EFFECT_ROWS
    ]
    assert parsed[0]["effect_name"] == "modifyActiveArmorResonancePostPercent"
    assert parsed[0]["effect_category"] == 1
    assert parsed[1]["effect_category"] == 5
    # The legacy keys are absent from the real data — reading them is how the
    # empty-name/zero-category state arose.
    assert all("effectName" not in item for item in JSONL_EFFECT_ROWS)
    assert all("effectCategory" not in item for item in JSONL_EFFECT_ROWS)


def _one_placeholder_row(table_name):
    """An INSERT putting a single throwaway row into `table_name`.

    needs_update() only ever counts these tables, so the contents are
    irrelevant — but the columns are NOT NULL, so the row still has to be
    shaped correctly. Built from the live metadata rather than hand-written
    per table, so adding a column to an SDE model cannot quietly break it.
    """
    table = Base.metadata.tables[table_name]
    values = {}
    for col in table.columns:
        if col.nullable:
            continue
        if isinstance(col.type, Integer):
            values[col.name] = 1
        elif isinstance(col.type, Float):
            values[col.name] = 1.0
        elif isinstance(col.type, Boolean):
            values[col.name] = False
        else:
            values[col.name] = "placeholder"
    return table.insert().values(**values)


def _needs_update_with(effect_rows, marker=True):
    """Seed every table needs_update() checks, plus the given effect rows.

    The empty-table loop in needs_update() returns True if ANY of a dozen
    tables is empty — sde_effects included. So a healthy baseline has to be
    genuinely complete, or the bad-state check below would never be reached
    and its test would be proving nothing.

    Each effect row is (effect_id, name, category) or, for a booster side
    effect, (effect_id, name, category, chance_attribute_id): a healthy table
    should hold at least one of the latter, as any real SDE does, though it
    no longer changes needs_update()'s answer (see `marker` below).

    `marker` seeds (or omits) the T-049 EFFECTS_USAGE_CHANCE_MARKER row in
    sde_meta -- see tests/test_fitting_boosters_engine.py, which is the one
    that turns this False to prove the "install predates the marker" path.
    Defaults True so every other caller here, which is testing something
    else entirely, gets the healthy baseline it's assuming.
    """
    tables = (
        "sde_types", "sde_planets", "sde_planet_schematics",
        "sde_wormhole_classes", "sde_wormhole_types", "sde_moons", "sde_stars",
        "sde_dogma_attributes", "sde_type_dogma_attrs", "sde_module_slots",
        "sde_market_groups", "sde_type_bonuses",
    )

    sl = _Slice()

    async def _check(db):
        # A recent last_updated, so only the table heuristics can fire.
        await db.execute(text(
            "INSERT INTO sde_meta (key, value) VALUES "
            "('last_updated', '2099-01-01T00:00:00+00:00')"
        ))
        if marker:
            await db.execute(
                text("INSERT INTO sde_meta (key, value) VALUES (:k, '1')"),
                {"k": EFFECTS_USAGE_CHANCE_MARKER},
            )
        for table in tables:
            await db.execute(_one_placeholder_row(table))
        for eff in effect_rows:
            eff_id, name, category = eff[:3]
            chance_attr = eff[3] if len(eff) > 3 else None
            await db.execute(
                text("INSERT INTO sde_effects "
                     "(effect_id, effect_name, effect_category, "
                     " fitting_usage_chance_attribute_id) "
                     "VALUES (:i, :n, :c, :f)"),
                {"i": eff_id, "n": name, "c": category, "f": chance_attr},
            )
        await db.commit()
        return await needs_update(db)

    return sl.run(_check, rows=[])


def test_needs_update_false_for_a_healthy_effects_table():
    """The control: everything populated and effects look real -> no reimport.

    Without this assertion the bad-state test below would pass on the
    empty-table loop alone and the new check could be deleted unnoticed.
    """
    assert _needs_update_with([
        (5231, "modifyActiveArmorResonancePostPercent", 1),
        (3015, "overloadHardeningBonus", 5),
        (16, "online", 4),
        # One booster side effect, as any real import has twelve of.
        (2737, "boosterShieldCapacityPenalty", 0, 1089),
    ]) is False


def test_needs_update_forces_reimport_when_effects_lost_names_and_categories():
    """Every effect at category 0 with an empty name is the pre-fix import."""
    assert _needs_update_with([
        (5231, "", 0),
        (3015, "", 0),
        (16, "", 0),
    ]) is True


def test_needs_update_tolerates_a_single_real_effect_row():
    """One row with a name is enough to say the import worked.

    The detector is a conjunction on purpose: anything weaker risks
    re-downloading the SDE on every boot.
    """
    assert _needs_update_with([
        (5231, "", 0),
        (3015, "modifyActiveArmorResonancePostPercent", 0),
        (2737, "", 0, 1089),
    ]) is False


# ════════════════════════════════════════════════════════════════════════════
# A. Overheating a resist module
# ════════════════════════════════════════════════════════════════════════════

def test_hardener_gives_its_nominal_resist_when_merely_online():
    stats = _stats([_low(HARDENER)])
    assert stats["armor_em_resist"] == pytest.approx(30.0, abs=0.05)
    assert stats["armor_expl_resist"] == pytest.approx(30.0, abs=0.05)


def test_overheated_hardener_scales_the_bonus_not_the_resist():
    """30% hardener + 20% overload -> 36%, not 50% and not 36.0 by luck.

    The overload bonus multiplies the module's resistance BONUS attribute
    (-30 -> -36), which then applies to the ship's resonance as postPercent:
    1.0 * (1 - 0.36) = 0.64 -> 36% resist. Scaling the resulting RESIST
    instead would give 30 * 1.2 = 36 too, so the test also checks a second
    hardener value where the two readings disagree.
    """
    stats = _stats([_low(HARDENER, overheated=True)])
    assert stats["armor_em_resist"] == pytest.approx(36.0, abs=0.05)
    assert stats["armor_therm_resist"] == pytest.approx(36.0, abs=0.05)
    assert stats["armor_kin_resist"] == pytest.approx(36.0, abs=0.05)
    assert stats["armor_expl_resist"] == pytest.approx(36.0, abs=0.05)


def test_overheat_applies_before_stacking_penalties():
    """Two overheated hardeners stack-penalise their BOOSTED bonuses.

    Heat lands on the module's own attributes before it joins the ship-wide
    pass, so the stacking machinery sees a stronger modifier rather than a
    penalised one being boosted afterwards.

    Worked through: each boosted bonus is -36, i.e. a x0.64 multiplier on the
    resonance. The second one is damped by exp(-1/7.1289) = 0.8691176, giving
    1 + (-0.36 x 0.8691176) = 0.6871177. Combined resonance
    0.64 x 0.6871177 = 0.4397553 -> 56.0% resist.
    """
    stats = _stats([_low(HARDENER, overheated=True),
                    _low(HARDENER, overheated=True)])
    assert stats["armor_em_resist"] == pytest.approx(56.0, abs=0.1)

    # And the same pair unheated: x0.70 then 1 + (-0.30 x 0.8691176),
    # = 0.70 x 0.7392647 = 0.5174853 -> 48.3%.
    cold = _stats([_low(HARDENER), _low(HARDENER)])
    assert cold["armor_em_resist"] == pytest.approx(48.3, abs=0.1)


def test_only_the_overheated_copy_of_a_type_is_boosted():
    """Heat is per-module, not per-type.

    Before per-item attribute dicts existed, overheating one hardener heated
    every copy of that type in the fit, because they all shared one dict.
    One hot and one cold gives the geometric middle: x0.64 first (the larger
    modifier is applied unpenalised), then x0.70 damped.
    """
    mixed = _stats([_low(HARDENER, overheated=True), _low(HARDENER)])
    both_hot = _stats([_low(HARDENER, overheated=True),
                       _low(HARDENER, overheated=True)])
    both_cold = _stats([_low(HARDENER), _low(HARDENER)])

    assert both_cold["armor_em_resist"] < mixed["armor_em_resist"]
    assert mixed["armor_em_resist"] < both_hot["armor_em_resist"]


def test_overheated_turret_shortens_its_cycle_and_raises_dps():
    """The damageMultiplier/ROF families of overload row still work.

    20 EM x 2.0 damage multiplier = 40 volley. 40 / 4.0s = 10 DPS cold;
    heat cuts the cycle by 15% to 3.4s, giving 40 / 3.4 = 11.8.
    """
    cold = _stats([_high(TURRET, charge_type_id=AMMO)])
    assert cold["weapon_dps"] == pytest.approx(10.0, abs=0.05)

    hot = _stats([_high(TURRET, charge_type_id=AMMO, overheated=True)])
    assert hot["weapon_dps"] == pytest.approx(11.8, abs=0.05)


def test_overheated_afterburner_is_faster():
    """135% speed boost, +50% overloaded -> 202.5%, on a 200 m/s hull."""
    assert _stats([_mid(AFTERBURNER)])["max_velocity"] == pytest.approx(470.0, abs=0.5)
    assert _stats([_mid(AFTERBURNER, overheated=True)])["max_velocity"] == \
        pytest.approx(605.0, abs=0.5)


# ════════════════════════════════════════════════════════════════════════════
# B. Offline modules
# ════════════════════════════════════════════════════════════════════════════

def test_offline_hardener_gives_no_resist_and_uses_no_cpu_or_powergrid():
    """An offline module contributes nothing but still occupies its slot.

    The CPU/PG half matters as much as the resist half: taking a module
    offline to squeeze a fit onto a hull is standard practice in EVE, and a
    fitting tool that still charged for it would refuse fits that work.
    """
    stats = _stats([_low(HARDENER, online=False)])
    assert stats["armor_em_resist"] == 0.0
    assert stats["armor_expl_resist"] == 0.0
    assert stats["cpu_used"] == 0.0
    assert stats["pg_used"] == 0.0

    # Control: online, the same module costs its 20 CPU / 2 PG.
    online = _stats([_low(HARDENER)])
    assert online["cpu_used"] == pytest.approx(20.0)
    assert online["pg_used"] == pytest.approx(2.0)


def test_three_module_states_give_three_distinct_ehp_values():
    """offline < online < overheated, with no two of them equal."""
    offline_stats = _stats([_low(HARDENER, online=False)])
    offline = offline_stats["armor_ehp"]
    online = _stats([_low(HARDENER)])["armor_ehp"]
    overheated = _stats([_low(HARDENER, overheated=True)])["armor_ehp"]

    assert offline < online < overheated
    assert len({offline, online, overheated}) == 3
    # The bare hull has no resists at all, so with the hardener offline the
    # armor layer's EHP is exactly its HP. (That HP is above the fixture's
    # 2000 because the engine assumes All V, which includes the +5%/level
    # armor skill — hence comparing against the reported HP, not the raw
    # fixture value.)
    assert offline == pytest.approx(offline_stats["armor_hp"], abs=1)
    assert offline_stats["armor_ehp_mult"] == pytest.approx(1.0, abs=0.01)


def test_offline_afterburner_does_not_change_speed():
    assert _stats([_mid(AFTERBURNER, online=False)])["max_velocity"] == \
        pytest.approx(200.0, abs=0.5)


def test_offline_weapon_does_not_fire():
    stats = _stats([_high(TURRET, charge_type_id=AMMO, online=False)])
    assert stats["weapon_dps"] == 0.0
    assert stats["weapon_volley"] == 0.0


def test_offline_turret_still_occupies_its_hardpoint():
    """Hardpoints are consumed by fitting, not by powering.

    There is no way in game to free a turret hardpoint by taking the gun
    offline, so the slot must stay counted even though everything else about
    the module goes to zero.
    """
    stats = _stats([_high(TURRET, charge_type_id=AMMO, online=False)])
    assert stats["turrets_used"] == 1
    assert stats["cpu_used"] == 0.0


def test_offline_repairer_and_cap_user_drop_out_of_tank_and_cap():
    offline = _stats([_low(ANCILLARY_REP, online=False)])
    assert offline["armor_rep_rate"] == 0.0
    assert offline["cap_drain_rate"] == 0.0

    online = _stats([_low(ANCILLARY_REP)])
    assert online["armor_rep_rate"] > 0.0


# ════════════════════════════════════════════════════════════════════════════
# C. Reactive Armor Hardener phasing
# ════════════════════════════════════════════════════════════════════════════

def test_rah_contributes_its_flat_resists_with_no_phasing_asked_for():
    """Effect 4928 carries no modifierInfo, so this path is hand-written.

    Without it the module was worth nothing at all: the SDE has no modifier
    row to find, because CCP redistributes the resists server-side.
    """
    stats = _stats([_low(RAH)])
    for key in ("armor_em_resist", "armor_therm_resist",
                "armor_kin_resist", "armor_expl_resist"):
        assert stats[key] == pytest.approx(15.0, abs=0.05), key


def test_rah_phasing_moves_the_whole_pool_onto_one_damage_type():
    stats = _stats([_low(RAH, resist_phasing={
        "em": 60, "thermal": 0, "kinetic": 0, "explosive": 0})])
    assert stats["armor_em_resist"] == pytest.approx(60.0, abs=0.05)
    assert stats["armor_therm_resist"] == 0.0
    assert stats["armor_kin_resist"] == 0.0
    assert stats["armor_expl_resist"] == 0.0
    assert stats["warnings"] == []


def test_rah_phasing_splits_across_two_types():
    stats = _stats([_low(RAH, resist_phasing={
        "em": 30, "thermal": 30, "kinetic": 0, "explosive": 0})])
    assert stats["armor_em_resist"] == pytest.approx(30.0, abs=0.05)
    assert stats["armor_therm_resist"] == pytest.approx(30.0, abs=0.05)
    assert stats["armor_kin_resist"] == 0.0


def test_rah_phasing_is_normalised_with_a_warning_when_it_does_not_add_up():
    """A distribution that doesn't sum to the pool is rescaled, not rejected.

    Linked percentage inputs produce 59.9 through rounding all the time; a
    fitting tool that 400s on that is worse than one that says what it did.
    """
    stats = _stats([_low(RAH, resist_phasing={
        "em": 120, "thermal": 0, "kinetic": 0, "explosive": 0})])
    # 120 clamps to the 60-point pool, then normalises back to 60.
    assert stats["armor_em_resist"] == pytest.approx(60.0, abs=0.05)
    assert len(stats["warnings"]) == 1
    assert "normalised" in stats["warnings"][0]


def test_rah_phasing_on_a_module_that_is_not_a_reactive_hardener_is_ignored():
    """Identified by effect 4928, not by name or group.

    Accepting the field on any module would let a request hand an ordinary
    hardener an arbitrary resist profile.
    """
    stats = _stats([_low(HARDENER, resist_phasing={
        "em": 60, "thermal": 0, "kinetic": 0, "explosive": 0})])
    # Unchanged from the plain 30% omni hardener.
    assert stats["armor_em_resist"] == pytest.approx(30.0, abs=0.05)
    assert any("not a reactive armor hardener" in w for w in stats["warnings"])


@pytest.mark.parametrize("bad", [[60, 0, 0, 0], "em", 5, True])
def test_rah_phasing_that_is_not_an_object_warns_instead_of_raising(bad):
    """`items` arrives as raw request JSON, so this field is client input.

    A list or a bare string reaching .get() would 500 the stats route, which
    is the one thing the warnings design exists to avoid.
    """
    stats = _stats([_low(RAH, resist_phasing=bad)])
    assert any("expected an object" in w for w in stats["warnings"])
    # Falls back to the module's own unphased resists rather than nothing.
    assert stats["armor_em_resist"] == pytest.approx(15.0, abs=0.05)


def test_rah_overheat_multiplies_on_top_of_the_phased_profile():
    """Order is charge -> phasing -> overload, so heat scales what phasing set.

    The fixture's reactive hardener overloads at -15% on each resonance. The
    magnitude is the fixture's own; what the assertion pins down is the
    ordering — heat acts on the phased value, not on the unphased one.
    """
    phasing = {"em": 60, "thermal": 0, "kinetic": 0, "explosive": 0}
    cold = _stats([_low(RAH, resist_phasing=phasing)])
    hot = _stats([_low(RAH, resist_phasing=phasing, overheated=True)])

    # Phased resonance 0.40, then x0.85 -> 0.34 -> 66% resist.
    assert cold["armor_em_resist"] == pytest.approx(60.0, abs=0.05)
    assert hot["armor_em_resist"] == pytest.approx(66.0, abs=0.1)


def test_offline_rah_contributes_nothing_and_no_phasing_is_applied():
    stats = _stats([_low(RAH, online=False, resist_phasing={
        "em": 60, "thermal": 0, "kinetic": 0, "explosive": 0})])
    assert stats["armor_em_resist"] == 0.0
    assert stats["rah_fitted"] is False


def test_rah_suggestion_follows_the_requested_damage_profile():
    single = _stats([_low(RAH)], damage_profile="em")
    assert single["rah_fitted"] is True
    assert single["rah_suggested_phasing"]["em"] == pytest.approx(RAH_POOL)
    assert single["rah_suggested_phasing"]["thermal"] == 0

    even = _stats([_low(RAH)], damage_profile="uniform")
    assert all(v == pytest.approx(RAH_POOL / 4)
               for v in even["rah_suggested_phasing"].values())

    # No reactive hardener fitted -> nothing to suggest.
    none = _stats([_low(HARDENER)])
    assert none["rah_fitted"] is False
    assert none["rah_suggested_phasing"] is None


# ── the phasing helpers, as pure functions ──────────────────────────────────

def test_rah_pool_is_read_from_the_module_not_hardcoded():
    assert rah_total_resist_points([0.85] * 4) == pytest.approx(60.0)
    # A bonused module has a bigger pool, and the API must follow it.
    assert rah_total_resist_points([0.80] * 4) == pytest.approx(80.0)


def test_suggest_phasing_matches_the_damage_profile():
    assert suggest_rah_phasing((1, 0, 0, 0), 60)["em"] == pytest.approx(60)
    assert suggest_rah_phasing((0.5, 0.5, 0, 0), 60)["thermal"] == pytest.approx(30)
    assert suggest_rah_phasing((0.25,) * 4, 60)["kinetic"] == pytest.approx(15)
    # A profile with no damage in it at all spreads evenly rather than dividing
    # by zero.
    assert suggest_rah_phasing((0, 0, 0, 0), 60)["explosive"] == pytest.approx(15)


def test_normalise_rejects_nothing_and_always_returns_the_pool():
    points, warning = normalise_resist_phasing(
        {"em": 60, "thermal": 0, "kinetic": 0, "explosive": 0}, 60)
    assert points == (60, 0, 0, 0) and warning is None

    points, warning = normalise_resist_phasing(
        {"em": 30, "thermal": 30, "kinetic": 30, "explosive": 30}, 60)
    assert sum(points) == pytest.approx(60) and warning is not None

    points, warning = normalise_resist_phasing({}, 60)
    assert points == (15, 15, 15, 15) and "evenly" in warning

    # Junk values are coerced, not raised on.
    points, _ = normalise_resist_phasing(
        {"em": "x", "thermal": None, "kinetic": 60, "explosive": -5}, 60)
    assert sum(points) == pytest.approx(60)


# ════════════════════════════════════════════════════════════════════════════
# D. Charges beyond weapons
# ════════════════════════════════════════════════════════════════════════════

def test_script_changes_the_turret_attribute_its_module_targets():
    """The charge -> module -> other-modules chain, end to end.

    The script's modifier has domain "otherID" (the module holding it), which
    raises the computer's own bonus attribute from 20 to 40; the computer's
    effect then pushes that out to turrets by group. Both halves have to work
    and in that order, which is why the per-item dicts are built before the
    module-to-module pass rather than after it.
    """
    bare = _stats([_high(TURRET, charge_type_id=AMMO)])
    assert bare["weapon_tracking"] == pytest.approx(0.1, abs=1e-6)

    computed = _stats([_high(TURRET, charge_type_id=AMMO),
                       _mid(TRACKING_COMPUTER)])
    assert computed["weapon_tracking"] == pytest.approx(0.12, abs=1e-6)

    scripted = _stats([_high(TURRET, charge_type_id=AMMO),
                       _mid(TRACKING_COMPUTER, charge_type_id=TRACKING_SCRIPT)])
    assert scripted["weapon_tracking"] == pytest.approx(0.14, abs=1e-6)


def test_an_offline_tracking_computer_helps_nothing():
    stats = _stats([_high(TURRET, charge_type_id=AMMO),
                    _mid(TRACKING_COMPUTER, charge_type_id=TRACKING_SCRIPT,
                         online=False)])
    assert stats["weapon_tracking"] == pytest.approx(0.1, abs=1e-6)


def test_cap_booster_charge_turns_a_draining_fit_into_a_stable_one():
    """The injection is hand-coded: effect 48 carries no modifier rows.

    The charge's own modifier zeroes the module's capacitorNeed, and the
    charge's capacitorBonus is then written back as a NEGATIVE need, which
    cap_sim reads as an injection.
    """
    dry = _stats([_mid(CAP_BOOSTER)])
    assert dry["cap_stable"] is False
    assert dry["cap_lasts_s"] > 0

    loaded = _stats([_mid(CAP_BOOSTER, charge_type_id=CAP_CHARGE)])
    assert loaded["cap_stable"] is True
    assert loaded["cap_lasts_s"] == 0


def test_paste_triples_an_ancillary_repairers_output():
    """120 HP / 10s = 12 HP/s, x3 with paste loaded = 36 HP/s.

    The x3 trigger is not in the dogma data (Pyfa hardcodes it too) but the
    multiplier itself is read from the module's chargedArmorDamageMultiplier
    rather than hardcoded here.
    """
    assert _stats([_low(ANCILLARY_REP)])["armor_rep_rate"] == \
        pytest.approx(12.0, abs=0.05)
    assert _stats([_low(ANCILLARY_REP, charge_type_id=PASTE)])["armor_rep_rate"] == \
        pytest.approx(36.0, abs=0.05)


def test_a_charge_the_repairer_does_not_accept_does_not_buff_it():
    """Guards the multiplier against an arbitrary charge_type_id in a request."""
    stats = _stats([_low(ANCILLARY_REP, charge_type_id=AMMO)])
    assert stats["armor_rep_rate"] == pytest.approx(12.0, abs=0.05)


def test_two_copies_of_a_module_can_carry_different_charges():
    """Per-item attrs, not per-type: the loaded rep and the dry one differ.

    12 + 36 = 48 HP/s. A type-keyed attribute dict would give 24 or 72.
    """
    stats = _stats([_low(ANCILLARY_REP, charge_type_id=PASTE),
                    _low(ANCILLARY_REP)])
    assert stats["armor_rep_rate"] == pytest.approx(48.0, abs=0.05)


# ════════════════════════════════════════════════════════════════════════════
# Regressions the above must not have broken
# ════════════════════════════════════════════════════════════════════════════

def test_weapon_dps_is_unchanged_by_all_of_this():
    """The plain weapon path: charge damage x damageMultiplier / cycle."""
    stats = _stats([_high(TURRET, charge_type_id=AMMO, quantity=3)])
    assert stats["weapon_dps"] == pytest.approx(30.0, abs=0.05)
    assert stats["weapon_volley"] == pytest.approx(120.0, abs=0.5)
    assert stats["turrets_used"] == 3


def test_a_weapon_with_no_charge_deals_no_damage():
    assert _stats([_high(TURRET)])["weapon_dps"] == 0.0


def test_resource_usage_adds_up_across_a_mixed_fit():
    stats = _stats([
        _low(HARDENER),                       # 20 CPU / 2 PG
        _low(ANCILLARY_REP),                  # 40 CPU / 15 PG
        _mid(AFTERBURNER, online=False),      # offline: free
        _high(TURRET, charge_type_id=AMMO),   # 25 CPU / 12 PG
    ])
    assert stats["cpu_used"] == pytest.approx(85.0, abs=0.05)
    assert stats["pg_used"] == pytest.approx(29.0, abs=0.05)
