"""Fitting calculation engine — computes ship stats from dogma attributes.

Applies module passive effects via the modifier pipeline:
  1. Collect modifiers from all fitted modules' effects
  2. Group by target attribute
  3. Apply in operator order: preAssign → modAdd → modSub → postMul → postDiv → postPercent
  4. Stacking penalties on postMul/postPercent where attribute stackable=False

Stacking penalty formula (Pyfa-verified):
  S(n) = e^(-n^2 / 7.1289)   where n is 0-indexed position sorted by |effect - 1|

References: Pyfa eos/modifiedAttributeDict.py, eos/calc.py, docs/fitting-mechanics.md
"""

import math
from collections import defaultdict
from typing import NamedTuple

from sqlalchemy import select, text

from app.fitting.cap_sim import simulate_cap
from app.fitting.boosters import apply_booster_bonuses
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.sde_models import (
    SDETypeDogmaAttribute, SDEModuleSlot, SDEType,
    SDETypeEffect, SDEEffect, SDEModifier, SDEDogmaAttribute,
)
from app.db.sde_models import SDETypeSkillReq, SDETypeBonus

from app.fitting.constants import (
    ATTR_POWER, ATTR_CPU, ATTR_UPGRADE_COST, ATTR_VOLUME,
    ATTR_DRONE_BW_USED, SHIP_STAT_ATTRS,
    ATTR_MASS, ATTR_INERTIA,
    ATTR_CAPACITOR_NEED, ATTR_DURATION, ATTR_SHIELD_RECHARGE_RATE,
    ATTR_CPU_OUTPUT, ATTR_POWER_OUTPUT,
    ATTR_SHIELD_EM_RESONANCE, ATTR_SHIELD_THERM_RESONANCE,
    ATTR_SHIELD_KIN_RESONANCE, ATTR_SHIELD_EXPL_RESONANCE,
    ATTR_ARMOR_EM_RESONANCE, ATTR_ARMOR_THERM_RESONANCE,
    ATTR_ARMOR_KIN_RESONANCE, ATTR_ARMOR_EXPL_RESONANCE,
    ATTR_HULL_EM_RESONANCE, ATTR_HULL_THERM_RESONANCE,
    ATTR_HULL_KIN_RESONANCE, ATTR_HULL_EXPL_RESONANCE,
    ATTR_SHIELD_HP, ATTR_ARMOR_HP, ATTR_HP,
    ATTR_CAPACITOR, ATTR_CAP_RECHARGE,
    ATTR_DAMAGE_MULTIPLIER, ATTR_RATE_OF_FIRE,
    ATTR_EM_DAMAGE, ATTR_EXPLOSIVE_DAMAGE, ATTR_KINETIC_DAMAGE, ATTR_THERMAL_DAMAGE,
    OVERLOAD_SOURCE_ATTR_PREFIX,
    ARMOR_RESONANCE_ATTRS, DAMAGE_TYPE_KEYS,
    EFFECT_ADAPTIVE_ARMOR_HARDENER, EFFECT_POWER_BOOSTER,
    ATTR_CAPACITOR_BONUS, ATTR_CHARGED_ARMOR_DAMAGE_MULTIPLIER,
    CHARGE_GROUP_ATTRS,
    ATTR_DMG_MULT_BONUS_PER_CYCLE, ATTR_DMG_MULT_BONUS_MAX,
    ATTR_TRACKING_SPEED, ATTR_OPTIMAL_RANGE,
    ATTR_MISSILE_DAMAGE_MULTIPLIER, ATTR_MISSILE_DAMAGE_MULTIPLIER_BONUS,
    ATTR_ARMOR_DAMAGE_AMOUNT, ATTR_SHIELD_BONUS,
    ATTR_HI_SLOTS, ATTR_MED_SLOTS, ATTR_LOW_SLOTS,
    ATTR_TURRET_SLOTS, ATTR_LAUNCHER_SLOTS,
    ATTR_HI_SLOT_MODIFIER, ATTR_MED_SLOT_MODIFIER, ATTR_LOW_SLOT_MODIFIER,
    ATTR_TURRET_HARDPOINT_MODIFIER, ATTR_LAUNCHER_HARDPOINT_MODIFIER,
)

# Default skill level assumption (used as fallback when no character is
# selected — models the EVE-standard "All V" fitting target).
DEFAULT_SKILL_LEVEL = 5


async def _ship_scaling_skill_ids(db: AsyncSession, ship_type_id: int) -> list[int]:
    """Return unique skill IDs that scale this ship's per-level hull bonuses.

    Sourced from the SDE's typeBonus (traits) data, which CCP ships with each
    bonus explicitly tagged. Role bonuses (scaling_skill_id IS NULL) are
    skipped — they don't scale with any skill. Most ships have exactly one
    scaling skill (their racial class skill), but T3C hulls have several
    (one per subsystem slot).
    """
    result = await db.execute(
        select(SDETypeBonus.scaling_skill_id)
        .where(SDETypeBonus.type_id == ship_type_id)
        .where(SDETypeBonus.is_role_bonus == False)  # noqa: E712
        .where(SDETypeBonus.scaling_skill_id.isnot(None))
    )
    return list({row[0] for row in result.fetchall() if row[0]})


async def _subsystem_scaling_skill_ids(db: AsyncSession, sub_type_id: int) -> list[int]:
    """Return the skill IDs that scale a subsystem's per-level bonuses.

    For subsystems the scaling skill is the subsystem's own required skill
    (e.g. "Caldari Offensive Systems" for the Tengu's offensive subsystem).
    We use SDETypeSkillReq for this rather than SDETypeBonus — subsystems
    don't have their own traits rows, the bonuses are attached to dogma
    effects gated by the required skill.
    """
    result = await db.execute(
        select(SDETypeSkillReq.skill_type_id).where(SDETypeSkillReq.type_id == sub_type_id)
    )
    return [row[0] for row in result.fetchall()]


def _effective_skill_level(
    scaling_skill_ids: list[int],
    skill_levels: dict[int, int] | None,
) -> int:
    """Pick an effective level for a per-level bonus.

    - No character selected (skill_levels is None): assume All V.
    - No scaling skill IDs (shouldn't normally happen): assume All V too, so
      we don't silently zero-out a bonus we can't identify.
    - Otherwise: take the MIN of the character's levels across the scaling
      skills. Missing = 0, so an untrained class skill drops the bonus to
      zero (matches the in-game reality that you can't fly the ship at all).
    """
    if skill_levels is None:
        return DEFAULT_SKILL_LEVEL
    if not scaling_skill_ids:
        return DEFAULT_SKILL_LEVEL
    return min(skill_levels.get(sid, 0) for sid in scaling_skill_ids)


# CCP JSONL SDE operator IDs (0-indexed from "operation" field in modifierInfo)
OP_PRE_ASSIGN = -1   # Override base value
OP_PRE_MUL = 0       # Multiply before additions
OP_PRE_DIV = 1       # Divide before additions
OP_MOD_ADD = 2       # Flat add
OP_MOD_SUB = 3       # Flat subtract
OP_POST_MUL = 4      # Multiply after additions (stacking penalized if applicable)
OP_POST_DIV = 5      # Divide after additions
OP_POST_PERCENT = 6  # val = val * (1 + modifier/100) — most common
OP_POST_ASSIGN = 7   # Force/lock value

# Effect categories — determines when effects fire
EFFECT_CAT_PASSIVE = 0
EFFECT_CAT_ACTIVE = 1
EFFECT_CAT_ONLINE = 4
EFFECT_CAT_OVERLOAD = 5

# Categories whose modifiers apply as soon as the module is online. Category 1
# (active) belongs here because this engine models a fit as "everything that
# can be running, is" — a hardener's resonance effect (5231), a tracking
# computer's bonus (4559) and an afterburner's speed boost are all category 1,
# and a fitting tool that dropped them would show no resists and no speed.
#
# Overload (5) is deliberately absent: those modifiers target the module's own
# attributes and only fire when the pilot overheats that specific module, so
# they are applied per item in `_apply_item_overload` rather than here.
MODULE_EFFECT_CATS = {EFFECT_CAT_PASSIVE, EFFECT_CAT_ACTIVE, EFFECT_CAT_ONLINE}

# Domain of a modifier that targets the module's own attributes ("this item").
DOMAIN_SELF = "itemID"
# Domain of a modifier on a CHARGE that targets the module holding it.
DOMAIN_PARENT_MODULE = "otherID"

# Stacking penalty constant: 2.67^2 = 7.1289
STACKING_CONSTANT = 7.1289


# ── ISS-015 framework / ISS-029 audit: per-ship modifier overrides ───────
# Historically some ships shipped SDE modifierInfo that targeted the wrong
# filter — the documented case was the Sacrilege (type_id 12019), whose
# damage bonus rows once pointed at skill 3306 (Medium Energy Turret) after
# CCP converted it to a missile boat without updating modifierInfo. This
# table is the integration point to correct such rows: when populated, it
# overrides a modifier's filter_type / filter_value at evaluation time.
#
# Schema:
#   { ship_type_id: { (effect_id, modifying_attribute_id): override } }
# where `override` is a dict with optional keys 'filter_type', 'filter_value'.
# Any key not present falls back to the SDE row's value, so partial
# overrides are fine (e.g. just changing the skill ID while keeping
# filter_type='skill'). Empty entries are a silent no-op.
#
# ISS-029 audit (2026-07-09, against the live SDE, read-only): the current
# modifierInfo is CORRECT for the Sacrilege and every audited Amarr /
# Force-Recon-priority hull (Sacrilege, Zealot, Devoter, Guardian, Curse
# [type_id 20125 — note 17722 is the Vigilant], Pilgrim, Rapier, Huginn,
# Arazu, Lachesis, Falcon, Rook). Each hull's modifier filter_value was
# cross-checked against CCP's own trait keyword (sde_type_bonuses) and
# matches. The Sacrilege now targets 25719 (Heavy Assault Missiles) and
# 3324 (Heavy Missiles) — precisely the value the ISS-015 example proposed
# to inject — and nothing on 12019 references 3306. CCP fixed the redirect
# upstream, so no overrides are warranted. This dict stays empty by design;
# it is not a TODO. See tests/test_ship_modifier_overrides.py.
#
# ISS-029 close-out (2026-09-24): the same question asked of EVERY hull, not
# twelve — scripts/audit_ship_modifiers.py screens all published ships with
# trait rows for a skill filter from a weapon system the hull's traits do
# not name (the ISS-015 shape). Against the live SDE: 419 hulls, 2,155
# skill-filtered modifiers, zero genuine mismatches. The "~200 ships" in
# the original estimate came from Pyfa's HISTORICAL override coverage; CCP
# has since cleaned modifierInfo up. Re-run the script after an SDE import;
# it exits non-zero on an unexplained flag.
#
# If a future hull IS found broken, verify the correct target against
# github.com/pyfa-org/Pyfa (eos/effects/) before adding an entry.
_SHIP_MODIFIER_OVERRIDES: dict[int, dict[tuple[int, int], dict]] = {}


def _modifier_filter(ship_type_id: int, mod) -> tuple[str | None, int | None]:
    """Returns (filter_type, filter_value) for the modifier, applying
    per-ship overrides from ``_SHIP_MODIFIER_OVERRIDES`` where present.
    Identity function when no override is registered. See ISS-015.
    """
    override = _SHIP_MODIFIER_OVERRIDES.get(ship_type_id, {}).get(
        (mod.effect_id, mod.modifying_attribute_id)
    )
    if not override:
        return mod.filter_type, mod.filter_value
    return (
        override.get("filter_type", mod.filter_type),
        override.get("filter_value", mod.filter_value),
    )


def stacking_penalty(n: int) -> float:
    """Stacking penalty multiplier for the nth module (0-indexed)."""
    return math.exp(-(n ** 2) / STACKING_CONSTANT)


def apply_stacking_penalties(modifiers: list[float]) -> float:
    """Apply stacking penalties to a list of multiplicative modifiers.

    Bonuses (>1) and penalties (<1) are sorted and penalized separately.
    Returns the combined product.
    """
    bonuses = sorted([m for m in modifiers if m > 1.0], key=lambda v: -abs(v - 1))
    penalties = sorted([m for m in modifiers if m < 1.0], key=lambda v: -abs(v - 1))

    result = 1.0
    for group in (bonuses, penalties):
        for i, mod in enumerate(group):
            penalty = stacking_penalty(i)
            result *= 1 + (mod - 1) * penalty
    return result


# ── Overload / heat ─────────────────────────────────────────────────────

def _apply_self_modifiers(attrs: dict[int, float], rows: list[tuple[int, int, int]]) -> None:
    """Apply "this item modifies its own attribute" modifier rows in place.

    Each row is (modified_attribute_id, modifying_attribute_id, operator).
    The source value is read from the SAME dict being modified, which is what
    domain="itemID" means: e.g. an EM Armor Hardener II's overload row is
    (984 emDamageResistanceBonus, 1208 overloadHardeningBonus, 6 postPercent),
    so 984 becomes -55 * (1 + 20/100) = -66.

    Reading the source from `attrs` (not from the unmodified SDE row) is
    deliberate — it means a ship or subsystem bonus that boosts the module's
    overload strength is already folded in by the time we get here, matching
    Pyfa, where `getModifiedItemAttr` returns the post-bonus value.
    """
    for target_attr, source_attr, operator in rows:
        source_val = attrs.get(source_attr)
        if source_val is None or source_val == 0:
            continue
        if target_attr not in attrs and target_attr != ATTR_DAMAGE_MULTIPLIER:
            # Nothing to scale. A percentage bonus on an absent attribute is a
            # no-op, not a zero — skip rather than inventing a base value.
            #
            # damageMultiplier is the one exception, and it is not academic:
            # a launcher has no damageMultiplier row in the SDE at all, yet
            # overloadRofBonus/overloadDamageModifier rows target it. Dogma
            # treats an absent damageMultiplier as its default of 1.0, which
            # is what _apply_modifier substitutes. Letting it through keeps
            # the behaviour the hand-written OVERLOAD_ATTR_MAP used to have.
            continue
        _apply_modifier(attrs, target_attr, operator, source_val)


# ── Reactive Armor Hardener phasing ─────────────────────────────────────

def rah_total_resist_points(resonances) -> float:
    """Total resist percentage points a reactive hardener has to distribute.

    The RAH's four armor resonance attributes are its *pool*: an unmodified
    Reactive Armor Hardener carries 0.85 on all four (15% each, 60 points
    total). Skills, ship bonuses and heat can move that number, so it is
    computed from the module's live attributes rather than hardcoded.
    """
    return sum((1.0 - r) * 100.0 for r in resonances)


def suggest_rah_phasing(
    damage_weights: tuple[float, float, float, float],
    total_points: float,
) -> dict[str, float]:
    """Resist points per damage type that best match an incoming damage profile.

    Distributes the module's whole pool in proportion to the incoming damage
    weights. This reproduces the equilibrium of Pyfa's cycle-by-cycle
    simulation (eos/effects.py Effect4928 `adaptiveArmorHardener`) exactly for
    the cases that simulation resolves cleanly:

      - one damage type      → the whole pool on that type   (60/0/0/0)
      - two, evenly split    → half the pool on each         (30/30/0/0)
      - uniform              → an even quarter each          (15/15/15/15)

    For lopsided three- and four-type profiles the real module oscillates
    between neighbouring states and Pyfa averages the loop; proportional
    distribution is a close approximation of that average, not a replay of it.
    It is offered to the UI as a suggestion the pilot can edit, which is why an
    approximation is acceptable here.
    """
    weights = [max(0.0, w) for w in damage_weights]
    total_weight = sum(weights)
    if total_weight <= 0:
        share = [total_points / 4.0] * 4
    else:
        share = [total_points * w / total_weight for w in weights]
    return {key: round(val, 2) for key, val in zip(DAMAGE_TYPE_KEYS, share)}


def normalise_resist_phasing(
    phasing: dict, total_points: float,
) -> tuple[tuple[float, float, float, float], str | None]:
    """Coerce a user-supplied phasing dict into a usable distribution.

    Returns ((em, thermal, kinetic, explosive) points, warning or None).

    A reactive hardener can only ever move points between damage types, never
    create them, so the four values must sum to the module's pool. Rather than
    rejecting a request whose numbers don't add up — the UI's linked inputs can
    easily produce 59.9 through rounding — the distribution is rescaled and the
    caller is handed a warning to surface.

    Each type is also clamped to [0, total_points]: 0 resist is the floor, and
    the ceiling is the whole pool on a single type, which is exactly what the
    in-game module reaches against single-type damage. There is no separate
    per-type cap attribute in the SDE — the cap is emergent, and this is it.
    """
    values = []
    for key in DAMAGE_TYPE_KEYS:
        try:
            values.append(float(phasing.get(key, 0) or 0))
        except (TypeError, ValueError):
            values.append(0.0)

    clamped = [min(max(0.0, v), total_points) for v in values]
    was_clamped = any(abs(c - v) > 1e-9 for c, v in zip(clamped, values))

    current = sum(clamped)
    if current <= 0:
        even = total_points / 4.0
        return (even, even, even, even), (
            "resist_phasing summed to zero; distributed evenly instead"
        )

    if abs(current - total_points) > 0.05 or was_clamped:
        scale = total_points / current
        clamped = [v * scale for v in clamped]
        return tuple(clamped), (
            f"resist_phasing summed to {current:.1f} points, normalised to the "
            f"module's {total_points:.1f}"
        )

    return tuple(clamped), None


async def get_type_dogma_attrs(db: AsyncSession, type_id: int) -> dict[int, float]:
    """Get all dogma attributes for a type. Returns {attribute_id: value}."""
    result = await db.execute(
        select(SDETypeDogmaAttribute.attribute_id, SDETypeDogmaAttribute.value)
        .where(SDETypeDogmaAttribute.type_id == type_id)
    )
    return {row.attribute_id: row.value for row in result.fetchall()}


async def get_types_dogma_attrs(db: AsyncSession, type_ids: list[int]) -> dict[int, dict[int, float]]:
    """Bulk get dogma attributes for multiple types."""
    if not type_ids:
        return {}
    result = await db.execute(
        select(SDETypeDogmaAttribute.type_id, SDETypeDogmaAttribute.attribute_id, SDETypeDogmaAttribute.value)
        .where(SDETypeDogmaAttribute.type_id.in_(type_ids))
    )
    out: dict[int, dict[int, float]] = {}
    for row in result.fetchall():
        out.setdefault(row.type_id, {})[row.attribute_id] = row.value
    return out


async def _get_stackable_flags(db: AsyncSession, attribute_ids: set[int]) -> dict[int, bool]:
    """Get stackable flag for attributes. True = NOT stacking penalized."""
    if not attribute_ids:
        return {}
    result = await db.execute(
        select(SDEDogmaAttribute.attribute_id, SDEDogmaAttribute.stackable)
        .where(SDEDogmaAttribute.attribute_id.in_(attribute_ids))
    )
    return {row.attribute_id: row.stackable for row in result.fetchall()}


async def _get_module_modifiers(
    db: AsyncSession, type_ids: list[int]
) -> dict[int, list[dict]]:
    """Get all passive ship-targeting modifiers for a list of module type IDs.

    Returns {type_id: [modifier_dicts]} where each modifier has:
      modified_attribute_id, modifying_attribute_id, operator, func,
      filter_type, filter_value
    """
    if not type_ids:
        return {}

    # Get passive effects for these types
    result = await db.execute(
        select(SDETypeEffect.type_id, SDETypeEffect.effect_id)
        .join(SDEEffect, SDETypeEffect.effect_id == SDEEffect.effect_id)
        .where(SDETypeEffect.type_id.in_(type_ids))
        .where(SDEEffect.effect_category.in_(MODULE_EFFECT_CATS))
    )
    type_effects: dict[int, list[int]] = defaultdict(list)
    all_effect_ids = set()
    for row in result.fetchall():
        type_effects[row.type_id].append(row.effect_id)
        all_effect_ids.add(row.effect_id)

    if not all_effect_ids:
        return {}

    # Get modifiers for those effects that target the ship domain
    result = await db.execute(
        select(SDEModifier)
        .where(SDEModifier.effect_id.in_(all_effect_ids))
        .where(SDEModifier.domain.in_(["shipID", "itemID"]))
    )
    effect_modifiers: dict[int, list] = defaultdict(list)
    for m in result.scalars().all():
        effect_modifiers[m.effect_id].append({
            "modified_attribute_id": m.modified_attribute_id,
            "modifying_attribute_id": m.modifying_attribute_id,
            "operator": m.operator,
            "func": m.func,
            "domain": m.domain,
            "filter_type": m.filter_type,
            "filter_value": m.filter_value,
        })

    # Map back to type_ids
    out: dict[int, list[dict]] = defaultdict(list)
    for tid, eff_ids in type_effects.items():
        for eff_id in eff_ids:
            out[tid].extend(effect_modifiers.get(eff_id, []))
    return out


async def _get_self_modifiers(
    db: AsyncSession, type_ids: list[int], domain: str,
    overload_only: bool,
) -> dict[int, list[tuple[int, int, int]]]:
    """Fetch modifier rows that target a single item rather than the ship.

    Two shapes share this query:

    - ``domain="itemID", overload_only=True`` — a module's overload rows.
    - ``domain="otherID", overload_only=False`` — a charge's rows, which
      target the module the charge is loaded into.

    Overload rows are selected by their SOURCE attribute's name beginning with
    "overload" rather than by ``effect_category == 5``. Both identify exactly
    the same 49 rows in the SDE (see constants.OVERLOAD_SOURCE_ATTR_PREFIX),
    but the name test also works against a database imported before the
    effect-category field name was corrected in app/sde/loader.py, where every
    effect landed as category 0. That keeps this correct with or without an
    SDE re-import.

    Returns {type_id: [(modified_attr, modifying_attr, operator), ...]}.
    """
    if not type_ids:
        return {}

    query = (
        select(SDETypeEffect.type_id, SDEModifier.modified_attribute_id,
               SDEModifier.modifying_attribute_id, SDEModifier.operator)
        .join(SDEModifier, SDEModifier.effect_id == SDETypeEffect.effect_id)
        .where(SDETypeEffect.type_id.in_(type_ids))
        .where(SDEModifier.domain == domain)
        .where(SDEModifier.func == "ItemModifier")
    )
    if overload_only:
        query = query.join(
            SDEDogmaAttribute,
            SDEDogmaAttribute.attribute_id == SDEModifier.modifying_attribute_id,
        ).where(SDEDogmaAttribute.attribute_name.like(f"{OVERLOAD_SOURCE_ATTR_PREFIX}%"))

    out: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
    for row in (await db.execute(query)).fetchall():
        out[row.type_id].append(
            (row.modified_attribute_id, row.modifying_attribute_id, row.operator)
        )
    return out


async def _get_types_with_effect(
    db: AsyncSession, type_ids: list[int], effect_id: int,
) -> set[int]:
    """Which of these types carry a given dogma effect.

    Used to classify modules by what they actually DO rather than by name — a
    reactive armor hardener is any module with effect 4928, which covers the
    T1 module and its faction variant without a name or group list to keep up
    to date.
    """
    if not type_ids:
        return set()
    result = await db.execute(
        select(SDETypeEffect.type_id)
        .where(SDETypeEffect.type_id.in_(type_ids))
        .where(SDETypeEffect.effect_id == effect_id)
    )
    return {row[0] for row in result.fetchall()}


def _apply_modifier(attrs: dict[int, float], target_attr: int, operator: int, value: float):
    """Apply a single modifier to an attribute dict. Handles damageMultiplier default."""
    if target_attr == ATTR_DAMAGE_MULTIPLIER and target_attr not in attrs:
        attrs[target_attr] = 1.0
    current = attrs.get(target_attr, 0)
    if current == 0 and target_attr == ATTR_DAMAGE_MULTIPLIER:
        current = 1.0

    if operator == OP_MOD_ADD:
        attrs[target_attr] = current + value
    elif operator == OP_POST_PERCENT:
        attrs[target_attr] = current * (1 + value / 100)
    elif operator == OP_POST_MUL:
        attrs[target_attr] = current * value
    elif operator == OP_PRE_MUL:
        attrs[target_attr] = current * value


async def _apply_ship_hull_bonuses(
    db: AsyncSession,
    ship_type_id: int,
    ship_attrs: dict[int, float],
    module_attrs_map: dict[int, dict[int, float]],
    charge_attrs_map: dict[int, dict[int, float]],
    items: list[dict],
    skill_levels: dict[int, int] | None = None,
    scaling_skill_override: list[int] | None = None,
    ship_mods: dict[int, list[tuple[int, float]]] | None = None,
):
    """Apply ship hull bonuses to module and charge attributes.

    A hull's bonuses to its OWN attributes (resists, sig radius, a per-level
    shield HP trait) are recorded in `ship_mods` as (operator, value) when
    it is given, for calculate_fitting_stats() to apply in dogma operator
    order next to the fitted modules' rows — applied here directly, a
    Crow's +5%/level shield HP would land before an extender's flat add
    rather than after it (ISS-043). Without `ship_mods` they are applied
    to `ship_attrs` on the spot.

    Handles three modifier function types:
    - LocationGroupModifier: matches modules by group ID → module_attrs_map
    - LocationRequiredSkillModifier: matches modules by skill req → module_attrs_map
    - OwnerRequiredSkillModifier: matches ALL items (modules, drones, charges)
      by skill req → module_attrs_map for modules/drones, charge_attrs_map for charges

    Uses modifierInfo as the authoritative source for targeting.  typeBonus.jsonl
    showinfo is display-only and unreliable for matching.  Per-level vs role
    detection uses attribute name patterns (shipBonus*/eliteBonus* = per-level).

    `skill_levels` is an optional {skill_id: active_level} dict for the
    selected character; if None the function falls back to All V.
    `scaling_skill_override` lets callers supply the per-level-scaling
    skills directly (used when re-entering this function for a subsystem,
    where the scaling skill is the subsystem's required skill rather than
    a ship traits row).
    """
    # Combine module + charge type IDs for skill-req lookup. Note: we do
    # NOT early-return when both maps are empty — ship-to-self ItemModifier
    # bonuses (resists, sig radius, etc.) still need to fire on a bare hull
    # with no modules fitted.
    all_type_ids = list(module_attrs_map.keys())
    charge_type_ids = list(charge_attrs_map.keys())
    combined_type_ids = list(set(all_type_ids + charge_type_ids))

    # Build group lookup for modules/drones
    result = await db.execute(
        select(SDEType.type_id, SDEType.group_id)
        .where(SDEType.type_id.in_(combined_type_ids))
    )
    group_ids: dict[int, int] = {row.type_id: row.group_id for row in result.fetchall()}

    # Build skill requirement lookup for modules, drones, AND charges
    skill_reqs: dict[int, set[int]] = defaultdict(set)
    result = await db.execute(
        select(SDETypeSkillReq.type_id, SDETypeSkillReq.skill_type_id)
        .where(SDETypeSkillReq.type_id.in_(combined_type_ids))
    )
    for row in result.fetchall():
        skill_reqs[row.type_id].add(row.skill_type_id)

    charge_tid_set = set(charge_type_ids)

    # Determine which source attributes are per-level vs role bonuses.
    per_level_attr_ids: set[int] = set()

    # Get ship's passive effects and their modifiers
    result = await db.execute(
        select(SDETypeEffect.effect_id)
        .join(SDEEffect, SDETypeEffect.effect_id == SDEEffect.effect_id)
        .where(SDETypeEffect.type_id == ship_type_id)
        .where(SDEEffect.effect_category.in_(MODULE_EFFECT_CATS))
    )
    ship_effect_ids = [row[0] for row in result.fetchall()]
    if not ship_effect_ids:
        return

    # Get source attribute names to identify per-level attrs
    result = await db.execute(
        select(SDEModifier.modifying_attribute_id)
        .where(SDEModifier.effect_id.in_(ship_effect_ids))
        .where(SDEModifier.domain.in_(["shipID", "charID"]))
    )
    src_attr_ids = list({row[0] for row in result.fetchall()})
    if src_attr_ids:
        name_result = await db.execute(
            select(SDEDogmaAttribute.attribute_id, SDEDogmaAttribute.attribute_name)
            .where(SDEDogmaAttribute.attribute_id.in_(src_attr_ids))
        )
        for row in name_result.fetchall():
            name = row.attribute_name or ""
            # Per-level attrs: shipBonusCBC1, eliteBonusGunship2,
            # subsystemBonusCaldariOffensive, etc.
            # Role attrs contain "Role": shipBonusRole7, eliteBonusViolatorsRole1
            is_per_level_name = (
                (name.startswith("shipBonus") or name.startswith("eliteBonus")
                 or name.startswith("subsystemBonus"))
                and "Role" not in name
            )
            if is_per_level_name:
                per_level_attr_ids.add(row.attribute_id)

    result = await db.execute(
        select(SDEModifier)
        .where(SDEModifier.effect_id.in_(ship_effect_ids))
        .where(SDEModifier.domain.in_(["shipID", "charID"]))
    )

    # Resolve the per-level scaling skill(s) once. For a ship this comes
    # from SDETypeBonus (traits). For a subsystem the caller overrides it
    # with the subsystem's required skill ID(s).
    if scaling_skill_override is not None:
        scaling_skills = scaling_skill_override
    else:
        scaling_skills = await _ship_scaling_skill_ids(db, ship_type_id)
    effective_skill_level = _effective_skill_level(scaling_skills, skill_levels)

    for mod in result.scalars().all():
        src_val = ship_attrs.get(mod.modifying_attribute_id)
        if src_val is None:
            continue

        # Per-level if source attribute is named shipBonus* or eliteBonus*
        is_per_level = mod.modifying_attribute_id in per_level_attr_ids
        effective_val = src_val * effective_skill_level if is_per_level else src_val
        target_attr = mod.modified_attribute_id

        # Ship-to-self modifier: target is the ship's OWN attribute (shield
        # resists, sig radius, mass, max velocity, etc.) The other branches
        # below match modules/drones/charges only — those silently dropped
        # ship-self bonuses (Drake/HAC/command-ship shield-resist traits etc.)
        # until this branch landed. See project_fitting_modifier_gaps memory
        # item 6 for the full failure case.
        if mod.func == "ItemModifier" and mod.domain == "shipID":
            if ship_mods is not None:
                ship_mods[target_attr].append((mod.operator, effective_val))
            else:
                _apply_modifier(ship_attrs, target_attr, mod.operator, effective_val)
            continue

        # Determine matching type IDs based on func type. ISS-015:
        # apply per-ship overrides where registered (no-op for ships
        # without an override entry).
        filter_type, filter_value = _modifier_filter(ship_type_id, mod)
        matching_type_ids = set()
        matching_charge_ids = set()

        if mod.func == "LocationGroupModifier" and filter_type == "group":
            for tid, gid in group_ids.items():
                if gid == filter_value and tid not in charge_tid_set:
                    matching_type_ids.add(tid)

        elif mod.func == "LocationRequiredSkillModifier" and filter_type == "skill":
            for tid, skills in skill_reqs.items():
                if filter_value in skills and tid not in charge_tid_set:
                    matching_type_ids.add(tid)

        elif mod.func == "OwnerRequiredSkillModifier" and filter_type == "skill":
            # Matches ALL items (modules, drones, charges) requiring the skill
            for tid, skills in skill_reqs.items():
                if filter_value in skills:
                    if tid in charge_tid_set:
                        matching_charge_ids.add(tid)
                    else:
                        matching_type_ids.add(tid)

        if not matching_type_ids and not matching_charge_ids:
            continue

        # Apply to matching modules/drones
        for tid in matching_type_ids:
            if tid not in module_attrs_map:
                continue
            _apply_modifier(module_attrs_map[tid], target_attr, mod.operator, effective_val)

        # Apply to matching charges
        for tid in matching_charge_ids:
            if tid not in charge_attrs_map:
                continue
            _apply_modifier(charge_attrs_map[tid], target_attr, mod.operator, effective_val)



async def _apply_implant_bonuses(
    db: AsyncSession,
    ship_attrs: dict[int, float],
    module_attrs_map: dict[int, dict[int, float]],
    charge_attrs_map: dict[int, dict[int, float]],
    implant_type_ids: list[int],
    ship_mods: dict[int, list[tuple[int, float]]] | None = None,
) -> None:
    """Apply implant attribute modifiers to modules and charges.

    Implants don't scale with level (you either have one plugged in or
    you don't — implantness slot 1-10), so the logic is simpler than
    _apply_all_v_skill_bonuses. We mirror the same domain/func dispatch
    because implants overwhelmingly use OwnerRequiredSkillModifier (a
    skill-filtered modifier on the character) plus the occasional
    ItemModifier targeting the character directly.

    Stat implants in slots 1-5 (perception/willpower/etc.) have no
    combat impact and are no-ops here — they only matter for skill
    training time, which Vigilant doesn't model.
    """
    if not implant_type_ids:
        return

    # Implant modifiers via the implant's type effects. Implants typically
    # have a single passive effect. Use the same domain/func vocabulary as
    # skills + the ItemModifier branch from T-023 (ship-self attrs).
    res = await db.execute(
        select(SDEModifier.effect_id, SDEModifier.modifying_attribute_id,
               SDEModifier.modified_attribute_id, SDEModifier.operator,
               SDEModifier.func, SDEModifier.domain,
               SDEModifier.filter_type, SDEModifier.filter_value,
               SDETypeEffect.type_id)
        .join(SDETypeEffect, SDEModifier.effect_id == SDETypeEffect.effect_id)
        .where(SDETypeEffect.type_id.in_(implant_type_ids))
        .where(SDEModifier.domain.in_(CHARACTER_MODIFIER_DOMAINS))
        .where(SDEModifier.func.in_(CHARACTER_MODIFIER_FUNCS))
    )
    modifiers = res.fetchall()
    if not modifiers:
        return

    # Look up each implant's bonus-source attributes (e.g. damageMultiplierBonus=2.0)
    impl_attrs = await get_types_dogma_attrs(db, implant_type_ids)
    rows = [
        CharacterModifier(
            row.modified_attribute_id, row.modifying_attribute_id, row.operator,
            row.func, row.domain, row.filter_type, row.filter_value,
            impl_attrs.get(row.type_id, {}).get(row.modifying_attribute_id) or 0.0,
        )
        for row in modifiers
    ]
    await _apply_character_modifiers(
        db, ship_attrs, module_attrs_map, charge_attrs_map, rows,
        ship_mods=ship_mods,
    )


# Modifier rows a character-owned item (implant, booster) can carry that the
# engine knows how to place. Shared by implants and app/fitting/boosters.py.
CHARACTER_MODIFIER_DOMAINS = ("shipID", "charID")
CHARACTER_MODIFIER_FUNCS = (
    "ItemModifier",
    "LocationGroupModifier", "LocationRequiredSkillModifier",
    "OwnerRequiredSkillModifier",
)
# Missile Launcher Operation — every missile requires it, which is how dogma
# scopes the character's missileDamageMultiplier to missiles.
SKILL_MISSILE_LAUNCHER_OPERATION = 3319


class CharacterModifier(NamedTuple):
    """One modifier row with its source value already read off the item."""
    modified_attribute_id: int
    modifying_attribute_id: int
    operator: int
    func: str
    domain: str
    filter_type: str | None
    filter_value: int | None
    source_value: float


async def _apply_character_modifiers(
    db: AsyncSession,
    ship_attrs: dict[int, float],
    module_attrs_map: dict[int, dict[int, float]],
    charge_attrs_map: dict[int, dict[int, float]],
    modifiers: list[CharacterModifier],
    ship_mods: dict[int, list[tuple[int, float]]] | None = None,
) -> None:
    """Apply modifier rows the character carries — implants and boosters —
    to the ship, modules and charges in place.

    Dispatch is the domain/func vocabulary skills use:

    - ItemModifier + shipID: the ship's own attribute (CPU subprocessors,
      agility hardwirings, a booster's velocity penalty). Fires on a bare
      hull, so there is no early return when nothing is fitted. With
      `ship_mods` given the row is recorded there as (operator, value)
      instead of being applied, for calculate_fitting_stats() to apply in
      dogma operator order next to the fitted modules' rows: applied here
      directly, a Blue Pill's -30% shield capacity landed before a shield
      extender's +2600 rather than after it (ISS-043).
    - ItemModifier + charID: an attribute of the character entity. The one
      this engine models is missileDamageMultiplier (212), which dogma reads
      when a missile launches, so it is applied to the damage of every charge
      requiring Missile Launcher Operation — the shape Pyfa gives effect 2851
      (missileDMGBonusPassive) for the event damage boosters. The character's
      other attributes (perception, drone control distance, industry rates)
      have no combat stat here and are skipped.
    - LocationGroupModifier / LocationRequiredSkillModifier + shipID: modules
      and drones in the ship, by group or required skill; never charges.
    - OwnerRequiredSkillModifier + charID: anything the character owns that
      requires the skill — charges and drones as well as modules.

    Nothing here takes a stacking penalty, and that is deliberate. The
    penalty is a property of the SOURCE, not of the target attribute's
    `stackable` flag alone: dogma exempts sources in the ship, charge, skill,
    implant and subsystem categories, and boosters are category 20 (Implant),
    group 303. This engine already applies skills and implants that way (see
    _apply_all_v_skill_bonuses). Pyfa agrees — in eos/effects.py an effect
    shared by a module and a booster switches the penalty off for the
    booster (Effect2803 energyWeaponDamageMultiplyPassive: `penalties =
    'booster' not in context`; Effect2851 missileDMGBonusPassive: `penalize =
    False if 'booster' in context else True`), the booster-only effects
    (Effect4951 shieldBoostAmplifierPassiveBooster, Effect2737, Effect4970)
    never pass stackingPenalties=True, and eos/modifiedAttributeDict.py
    `multiply()` defaults stackingPenalties=False, which folds the factor
    into the plain multiplier rather than into a penalty group. So a Strong
    Blue Pill's +30% shield boost sits outside the boost amplifiers' chain.
    """
    if not modifiers:
        return

    combined = list(set(module_attrs_map) | set(charge_attrs_map))
    group_ids: dict[int, int] = {}
    skill_reqs: dict[int, set[int]] = defaultdict(set)
    if combined:
        res = await db.execute(
            select(SDEType.type_id, SDEType.group_id).where(SDEType.type_id.in_(combined))
        )
        group_ids = {r.type_id: r.group_id for r in res.fetchall()}
        res = await db.execute(
            select(SDETypeSkillReq.type_id, SDETypeSkillReq.skill_type_id)
            .where(SDETypeSkillReq.type_id.in_(combined))
        )
        for r in res.fetchall():
            skill_reqs[r.type_id].add(r.skill_type_id)
    charge_tid_set = set(charge_attrs_map)

    for mod in modifiers:
        src_val = mod.source_value
        if not src_val:
            continue

        if mod.func == "ItemModifier":
            if mod.domain == "shipID":
                if ship_mods is not None:
                    ship_mods[mod.modified_attribute_id].append((mod.operator, src_val))
                else:
                    _apply_modifier(ship_attrs, mod.modified_attribute_id, mod.operator, src_val)
            elif (mod.domain == "charID"
                    and mod.modified_attribute_id == ATTR_MISSILE_DAMAGE_MULTIPLIER):
                for tid in charge_tid_set:
                    if SKILL_MISSILE_LAUNCHER_OPERATION not in skill_reqs.get(tid, ()):
                        continue
                    for dmg_attr in (ATTR_EM_DAMAGE, ATTR_THERMAL_DAMAGE,
                                     ATTR_KINETIC_DAMAGE, ATTR_EXPLOSIVE_DAMAGE):
                        if dmg_attr in charge_attrs_map[tid]:
                            _apply_modifier(charge_attrs_map[tid], dmg_attr,
                                            mod.operator, src_val)
            continue

        matching_type_ids: set[int] = set()
        matching_charge_ids: set[int] = set()

        if mod.func == "LocationGroupModifier" and mod.filter_type == "group":
            for tid, gid in group_ids.items():
                if gid == mod.filter_value and tid not in charge_tid_set:
                    matching_type_ids.add(tid)
        elif mod.func == "LocationRequiredSkillModifier" and mod.filter_type == "skill":
            for tid, skills in skill_reqs.items():
                if mod.filter_value in skills and tid not in charge_tid_set:
                    matching_type_ids.add(tid)
        elif mod.func == "OwnerRequiredSkillModifier" and mod.filter_type == "skill":
            for tid, skills in skill_reqs.items():
                if mod.filter_value in skills:
                    if tid in charge_tid_set:
                        matching_charge_ids.add(tid)
                    else:
                        matching_type_ids.add(tid)

        for tid in matching_type_ids:
            if tid in module_attrs_map:
                _apply_modifier(module_attrs_map[tid], mod.modified_attribute_id,
                                mod.operator, src_val)
        for tid in matching_charge_ids:
            if tid in charge_attrs_map:
                _apply_modifier(charge_attrs_map[tid], mod.modified_attribute_id,
                                mod.operator, src_val)


# Drone size + racial specialization skills that have a
# damageMultiplierBonus attr but no OwnerRequiredSkillModifier row to
# propagate it to drones. Pyfa hard-codes these in effects.py — same
# fix here. Each skill's bonus applies to drones requiring that skill.
# Drone Interfacing (3442) is NOT in this list — its modifierInfo
# already targets all drones via skill=3436 (Drones).
_DRONE_SKILL_BONUS_GAPS: tuple[int, ...] = (
    3441,   # Heavy Drone Operation        (5%/level)
    33699,  # Medium Drone Operation       (5%/level)
    24241,  # Light Drone Operation        (5%/level)
    23594,  # Sentry Drone Interfacing     (5%/level)
    12484,  # Amarr Drone Specialization   (2%/level)
    12485,  # Minmatar Drone Specialization
    12486,  # Gallente Drone Specialization
    12487,  # Caldari Drone Specialization
    60515,  # Mutated Drone Specialization
)
# damageMultiplierBonus attribute_id
_ATTR_DAMAGE_MULT_BONUS = 64


async def _apply_drone_skill_bonuses(
    db: AsyncSession,
    module_attrs_map: dict[int, dict[int, float]],
    items: list[dict],
    skill_levels: dict[int, int] | None = None,
) -> None:
    """Apply drone-skill damage bonuses that the SDE doesn't propagate.

    Light/Medium/Heavy Drone Operation, Sentry Drone Interfacing, and the
    four racial drone specializations each carry a `damageMultiplierBonus`
    attr but have no SDEModifier row mapping it to actual drone damage
    (only their internal skillLevel→damageMultiplierBonus self-modifier).
    Pyfa works around this with hard-coded handlers in effects.py.

    For each gap skill, find the drones in `items` that require it and
    multiply their damageMultiplier by (1 + bonus*level/100). Stacking
    bonuses are not penalized — drone skills are explicitly non-stacking
    per CCP design (matches Pyfa).
    """
    drone_type_ids = {i["type_id"] for i in items if i.get("slot") == "drone"}
    if not drone_type_ids:
        return

    # Skill-req lookup: which of our drones require each gap skill?
    skill_reqs: dict[int, set[int]] = defaultdict(set)
    res = await db.execute(
        select(SDETypeSkillReq.type_id, SDETypeSkillReq.skill_type_id)
        .where(SDETypeSkillReq.type_id.in_(list(drone_type_ids)))
        .where(SDETypeSkillReq.skill_type_id.in_(_DRONE_SKILL_BONUS_GAPS))
    )
    for tid, skill_id in res.fetchall():
        skill_reqs[skill_id].add(tid)
    if not skill_reqs:
        return

    # Fetch each gap skill's damageMultiplierBonus value
    res = await db.execute(
        select(SDETypeDogmaAttribute.type_id, SDETypeDogmaAttribute.value)
        .where(SDETypeDogmaAttribute.type_id.in_(list(skill_reqs.keys())))
        .where(SDETypeDogmaAttribute.attribute_id == _ATTR_DAMAGE_MULT_BONUS)
    )
    bonus_pct: dict[int, float] = {tid: val for tid, val in res.fetchall()}

    for skill_id, drones in skill_reqs.items():
        pct = bonus_pct.get(skill_id)
        if not pct:
            continue
        level = DEFAULT_SKILL_LEVEL if skill_levels is None else skill_levels.get(skill_id, 0)
        if not level:
            continue
        mult = 1 + (pct * level) / 100.0
        for tid in drones:
            attrs = module_attrs_map.get(tid)
            if attrs is None:
                continue
            current = attrs.get(ATTR_DAMAGE_MULTIPLIER, 1.0)
            attrs[ATTR_DAMAGE_MULTIPLIER] = current * mult


async def _apply_all_v_skill_bonuses(
    db: AsyncSession,
    module_attrs_map: dict[int, dict[int, float]],
    charge_attrs_map: dict[int, dict[int, float]],
    items: list[dict],
    skill_levels: dict[int, int] | None = None,
):
    """Apply skill bonuses to module, drone, and charge attributes.

    Skills (category 16) have effects that modify items on the ship via:
    - LocationGroupModifier / LocationRequiredSkillModifier (domain=shipID)
      → targets modules/drones in module_attrs_map
    - OwnerRequiredSkillModifier (domain=charID)
      → targets drones in module_attrs_map AND charges in charge_attrs_map

    When `skill_levels` is None, applies the All-V assumption (× 5 for every
    skill). When a character's skill map is passed, each skill's bonus
    scales by that character's actual active level — an untrained skill
    contributes zero.  Skills are never stacking-penalized.
    """
    all_type_ids = list(module_attrs_map.keys())
    charge_type_ids = list(charge_attrs_map.keys())
    combined = list(set(all_type_ids + charge_type_ids))
    if not combined:
        return

    # Build group and skill-requirement lookups for all item types
    result = await db.execute(
        select(SDEType.type_id, SDEType.group_id)
        .where(SDEType.type_id.in_(combined))
    )
    group_ids: dict[int, int] = {row.type_id: row.group_id for row in result.fetchall()}

    skill_reqs: dict[int, set[int]] = defaultdict(set)
    result = await db.execute(
        select(SDETypeSkillReq.type_id, SDETypeSkillReq.skill_type_id)
        .where(SDETypeSkillReq.type_id.in_(combined))
    )
    for row in result.fetchall():
        skill_reqs[row.type_id].add(row.skill_type_id)

    charge_tid_set = set(charge_type_ids)

    # Find all skill modifiers that target items on the ship or owned by character
    result = await db.execute(
        select(SDEModifier.effect_id, SDEModifier.modifying_attribute_id,
               SDEModifier.modified_attribute_id, SDEModifier.operator,
               SDEModifier.func, SDEModifier.filter_type, SDEModifier.filter_value,
               SDETypeEffect.type_id)
        .join(SDETypeEffect, SDEModifier.effect_id == SDETypeEffect.effect_id)
        .join(SDEType, SDETypeEffect.type_id == SDEType.type_id)
        .where(SDEType.category_id == 16)  # Skills
        .where(SDEModifier.domain.in_(["shipID", "charID"]))
        .where(SDEModifier.func.in_([
            "LocationGroupModifier",
            "LocationRequiredSkillModifier",
            "OwnerRequiredSkillModifier",
        ]))
    )

    skill_modifiers = result.fetchall()
    if not skill_modifiers:
        return

    # Get skill bonus attributes for all relevant skills
    skill_type_ids = list({row.type_id for row in skill_modifiers})
    skill_attrs_map = await get_types_dogma_attrs(db, skill_type_ids)

    for row in skill_modifiers:
        skill_tid = row.type_id
        src_attr_id = row.modifying_attribute_id
        target_attr_id = row.modified_attribute_id
        operator = row.operator
        func = row.func
        filter_type = row.filter_type
        filter_value = row.filter_value

        # Get the skill's bonus attribute base value
        skill_attrs = skill_attrs_map.get(skill_tid, {})
        src_val = skill_attrs.get(src_attr_id)
        if src_val is None or src_val == 0:
            continue

        # Skill bonus scales by character's level in the skill providing
        # the bonus (row.type_id IS the skill's type_id via the join).
        lvl = DEFAULT_SKILL_LEVEL if skill_levels is None else skill_levels.get(skill_tid, 0)
        if lvl == 0:
            continue
        effective_val = src_val * lvl

        # Find matching items
        matching_modules = set()
        matching_charges = set()

        if func == "LocationGroupModifier" and filter_type == "group" and filter_value:
            for tid, gid in group_ids.items():
                if gid == filter_value and tid not in charge_tid_set:
                    matching_modules.add(tid)

        elif func == "LocationRequiredSkillModifier" and filter_type == "skill" and filter_value:
            for tid, skills in skill_reqs.items():
                if filter_value in skills and tid not in charge_tid_set:
                    matching_modules.add(tid)

        elif func == "OwnerRequiredSkillModifier" and filter_type == "skill" and filter_value:
            for tid, skills in skill_reqs.items():
                if filter_value in skills:
                    if tid in charge_tid_set:
                        matching_charges.add(tid)
                    else:
                        matching_modules.add(tid)

        # Apply to matching modules/drones
        for tid in matching_modules:
            if tid in module_attrs_map:
                _apply_modifier(module_attrs_map[tid], target_attr_id, operator, effective_val)

        # Apply to matching charges
        for tid in matching_charges:
            if tid in charge_attrs_map:
                _apply_modifier(charge_attrs_map[tid], target_attr_id, operator, effective_val)


async def get_ship_stats(db: AsyncSession, ship_type_id: int) -> dict:
    """Fetch all displayable base stats for a ship from dogma attributes."""
    attrs = await get_type_dogma_attrs(db, ship_type_id)
    stats = {}
    for name, attr_id in SHIP_STAT_ATTRS.items():
        stats[name] = attrs.get(attr_id, 0)

    stats["align_time"] = _calc_align_time(stats.get("inertia", 0), stats.get("mass", 0))

    # Resists (convert resonance to resist percentage)
    stats["shield_em_resist"] = _resonance_to_resist(attrs.get(ATTR_SHIELD_EM_RESONANCE, 1.0))
    stats["shield_therm_resist"] = _resonance_to_resist(attrs.get(ATTR_SHIELD_THERM_RESONANCE, 1.0))
    stats["shield_kin_resist"] = _resonance_to_resist(attrs.get(ATTR_SHIELD_KIN_RESONANCE, 1.0))
    stats["shield_expl_resist"] = _resonance_to_resist(attrs.get(ATTR_SHIELD_EXPL_RESONANCE, 1.0))
    stats["armor_em_resist"] = _resonance_to_resist(attrs.get(ATTR_ARMOR_EM_RESONANCE, 1.0))
    stats["armor_therm_resist"] = _resonance_to_resist(attrs.get(ATTR_ARMOR_THERM_RESONANCE, 1.0))
    stats["armor_kin_resist"] = _resonance_to_resist(attrs.get(ATTR_ARMOR_KIN_RESONANCE, 1.0))
    stats["armor_expl_resist"] = _resonance_to_resist(attrs.get(ATTR_ARMOR_EXPL_RESONANCE, 1.0))
    stats["hull_em_resist"] = _resonance_to_resist(attrs.get(ATTR_HULL_EM_RESONANCE, 1.0))
    stats["hull_therm_resist"] = _resonance_to_resist(attrs.get(ATTR_HULL_THERM_RESONANCE, 1.0))
    stats["hull_kin_resist"] = _resonance_to_resist(attrs.get(ATTR_HULL_KIN_RESONANCE, 1.0))
    stats["hull_expl_resist"] = _resonance_to_resist(attrs.get(ATTR_HULL_EXPL_RESONANCE, 1.0))

    return stats


# Built-in damage profiles: (em, thermal, kinetic, explosive) fractions
DAMAGE_PROFILES = {
    "uniform": (0.25, 0.25, 0.25, 0.25),
    "em": (1.0, 0.0, 0.0, 0.0),
    "thermal": (0.0, 1.0, 0.0, 0.0),
    "kinetic": (0.0, 0.0, 1.0, 0.0),
    "explosive": (0.0, 0.0, 0.0, 1.0),
    "guristas": (0.0, 0.18, 0.82, 0.0),
    "serpentis": (0.0, 0.55, 0.45, 0.0),
    "angel": (0.0, 0.0, 0.08, 0.92),
    "blood": (0.50, 0.48, 0.0, 0.02),
    "sansha": (0.53, 0.47, 0.0, 0.0),
    "sleeper": (0.22, 0.36, 0.28, 0.14),
    "triglavian": (0.0, 0.65, 0.0, 0.35),
    "drifter": (0.0, 0.0, 0.0, 1.0),
}

def resolve_damage_profile(
    name: str = "uniform",
    custom: list[float] | tuple[float, ...] | None = None,
) -> tuple[float, float, float, float]:
    """Resolve an (em, therm, kin, expl) weight tuple for EHP weighting.

    Presentation-layer helper (paired with ``_calc_ehp``) — it never touches
    the dogma modifier pipeline, only how the already-computed layer HP/resists
    are combined into displayed effective HP.

    Precedence: an explicit ``custom`` list wins over the named preset. Custom
    weights are normalized to sum 1.0 so arbitrary slider input (e.g. 40/30/20/10
    or raw 4/3/2/1) yields well-defined EHP. A degenerate all-zero custom list
    falls back to the named preset (or uniform).
    """
    if custom is not None:
        try:
            vals = [max(0.0, float(x)) for x in custom]
        except (TypeError, ValueError):
            vals = []
        if len(vals) == 4:
            total = sum(vals)
            if total > 0:
                return (vals[0] / total, vals[1] / total,
                        vals[2] / total, vals[3] / total)
    return DAMAGE_PROFILES.get(name, DAMAGE_PROFILES["uniform"])


# Target ship's resist profile by damage type (em, therm, kin, expl) — values
# 0.0–1.0. Used to compute effective ("resist-weighted") DPS: how much DPS
# actually lands on a target with these resists. Numbers are approximate
# averages across NPC ships of each faction; players can pick the closest
# preset rather than entering 4 resists by hand. "uniform" = no resists.
TARGET_RESIST_PROFILES = {
    "uniform":    (0.00, 0.00, 0.00, 0.00),
    "em":         (0.00, 0.00, 0.00, 0.00),
    "thermal":    (0.00, 0.00, 0.00, 0.00),
    "kinetic":    (0.00, 0.00, 0.00, 0.00),
    "explosive":  (0.00, 0.00, 0.00, 0.00),
    "sansha":     (0.45, 0.55, 0.00, 0.00),
    "blood":      (0.45, 0.60, 0.00, 0.00),
    "guristas":   (0.00, 0.50, 0.55, 0.40),
    "serpentis":  (0.00, 0.55, 0.45, 0.40),
    "angel":      (0.55, 0.50, 0.40, 0.30),
    "sleeper":    (0.60, 0.60, 0.60, 0.60),
    "triglavian": (0.60, 0.65, 0.50, 0.65),
    "drifter":    (0.55, 0.55, 0.55, 0.55),
}


def _apply_charge_to_module(
    module_attrs: dict[int, float],
    charge_attrs: dict[int, float],
    charge_rows: list[tuple[int, int, int]],
    charge_group_id: int | None,
    is_injector: bool,
) -> None:
    """Fold a loaded charge's effects into its parent module's attributes.

    Most of this is plain dogma. A charge's effects carry modifiers with
    domain "otherID" — "the module I am loaded into" — and the source value is
    read from the charge while the target lives on the module. A Tracking Speed
    Script is the canonical case: its modifier row targets the tracking
    computer's own trackingSpeedBonus, so the module's bonus grows while the
    script is loaded. The module then pushes that boosted bonus out to the
    turrets through its own effect, which the module-to-module pass in
    `calculate_fitting_stats` handles — which is why the per-item dicts are
    built before that pass runs and not after it.

    Two mechanics are NOT in the dogma data and are hand-coded here, as they
    are in Pyfa:

    1. Capacitor injection. The charge's `capacitorBonus` (67) has no modifier
       row anywhere; Pyfa's Effect48 `powerBooster` assigns
       `capacitorNeed = -capAmount`. The generic path above has already zeroed
       the module's own capacitorNeed via the charge's `capNeedBonus` (317,
       -100%), so the negative value is the whole cap effect. `cap_sim` reads a
       negative cap_need as an injection.
    2. Ancillary armour repairers. Effect 5275 `fueledArmorRepair` carries no
       modifiers; Pyfa hardcodes a x3 for Nanite Repair Paste. The SDE does
       record the multiplier as `chargedArmorDamageMultiplier` (1886) on the
       module, so the value is read from the data even though the trigger is
       not. It applies only when the loaded charge is one the module actually
       accepts, so an arbitrary charge_type_id in the request cannot buff a rep.
    """
    for target_attr, source_attr, operator in charge_rows:
        source_val = charge_attrs.get(source_attr)
        if source_val is None or source_val == 0:
            continue
        if target_attr not in module_attrs and target_attr != ATTR_DAMAGE_MULTIPLIER:
            # Same exception as _apply_self_modifiers: dogma's default for an
            # absent damageMultiplier is 1.0, not "skip".
            continue
        _apply_modifier(module_attrs, target_attr, operator, source_val)

    if is_injector:
        injected = charge_attrs.get(ATTR_CAPACITOR_BONUS, 0)
        if injected:
            module_attrs[ATTR_CAPACITOR_NEED] = -injected

    charge_multiplier = module_attrs.get(ATTR_CHARGED_ARMOR_DAMAGE_MULTIPLIER)
    accepted_groups = {module_attrs.get(a) for a in CHARGE_GROUP_ATTRS}
    if (
        charge_multiplier
        and charge_group_id is not None
        and charge_group_id in accepted_groups
        and module_attrs.get(ATTR_ARMOR_DAMAGE_AMOUNT)
    ):
        module_attrs[ATTR_ARMOR_DAMAGE_AMOUNT] *= charge_multiplier


def _apply_rah_phasing(attrs: dict[int, float], phasing: dict) -> str | None:
    """Override a reactive hardener's four resonances with a phasing request.

    The module's current resonances define the size of the pool; the request
    says how to spread it. Returns a warning string when the request had to be
    adjusted, or None when it was used as given.
    """
    resonances = [attrs.get(attr_id, 1.0) for attr_id in ARMOR_RESONANCE_ATTRS]
    total_points = rah_total_resist_points(resonances)
    if total_points <= 0:
        return "resist_phasing ignored: the module has no resistance to distribute"

    points, warning = normalise_resist_phasing(phasing, total_points)
    for attr_id, type_points in zip(ARMOR_RESONANCE_ATTRS, points):
        attrs[attr_id] = 1.0 - type_points / 100.0
    return warning


async def _build_item_attrs(
    db: AsyncSession,
    items: list[dict],
    module_attrs_map: dict[int, dict[int, float]],
    charge_attrs_map: dict[int, dict[int, float]],
) -> tuple[list[tuple[dict, dict]], set[int], list[str]]:
    """Give every fitted item its own attribute dict.

    Everything upstream of this point is keyed by type_id, which is right for
    anything that depends only on what a module IS — skills, hull bonuses,
    implants. It is wrong for anything that depends on how a *particular copy*
    is being used: which ammo is loaded, whether this one is overheated, how a
    reactive hardener is phased. Previously a single overheated module heated
    every copy of its type, and a charge only ever reached weapon damage.

    Returns (list of (item, attrs) in `items` order, reactive-hardener type
    IDs, warnings for the caller to surface).

    Application order per item is charge → phasing → overload, so heat
    multiplies on top of everything else, exactly as in game. Note that all
    three are ordinary multiplications on the module's OWN attributes, applied
    before the module joins the ship-wide modifier pass — which is what makes
    "heat is applied before stacking penalties" true without any special
    handling: an overheated hardener's boosted resistance bonus simply enters
    the existing stacking machinery as a stronger modifier.
    """
    slotted = [i for i in items if i.get("slot") not in ("drone", "cargo")]
    slotted_type_ids = list({i["type_id"] for i in slotted})
    charge_type_ids = list({
        i["charge_type_id"] for i in items if i.get("charge_type_id")
    })

    overload_rows = await _get_self_modifiers(
        db, slotted_type_ids, DOMAIN_SELF, overload_only=True,
    )
    charge_rows = await _get_self_modifiers(
        db, charge_type_ids, DOMAIN_PARENT_MODULE, overload_only=False,
    )
    rah_type_ids = await _get_types_with_effect(
        db, slotted_type_ids, EFFECT_ADAPTIVE_ARMOR_HARDENER,
    )
    injector_type_ids = await _get_types_with_effect(
        db, slotted_type_ids, EFFECT_POWER_BOOSTER,
    )

    charge_groups: dict[int, int] = {}
    if charge_type_ids:
        result = await db.execute(
            select(SDEType.type_id, SDEType.group_id)
            .where(SDEType.type_id.in_(charge_type_ids))
        )
        charge_groups = {row.type_id: row.group_id for row in result.fetchall()}

    enriched: list[tuple[dict, dict]] = []
    warnings: list[str] = []

    for item in items:
        type_id = item["type_id"]
        base_attrs = module_attrs_map.get(type_id, {})

        # Drones and cargo have no per-copy state, so they keep sharing the
        # type-level dict rather than paying for a copy each.
        if item.get("slot") in ("drone", "cargo"):
            enriched.append((item, base_attrs))
            continue

        attrs = dict(base_attrs)

        # An offline module runs no effects at all, so none of these apply —
        # it still gets its own dict so downstream code can treat all items
        # uniformly.
        if item.get("online", True):
            charge_type_id = item.get("charge_type_id")
            if charge_type_id:
                _apply_charge_to_module(
                    attrs,
                    charge_attrs_map.get(charge_type_id, {}),
                    charge_rows.get(charge_type_id, []),
                    charge_groups.get(charge_type_id),
                    type_id in injector_type_ids,
                )

            phasing = item.get("resist_phasing")
            if phasing:
                # `items` reaches the engine as whatever JSON the request
                # carried, so this is the one field here that is unvalidated
                # client input. A list or a bare string would otherwise reach
                # .get() and 500 the handler — warn like every other bad
                # phasing instead.
                if not isinstance(phasing, dict):
                    warnings.append(
                        "resist_phasing ignored: expected an object keyed by "
                        "damage type"
                    )
                elif type_id in rah_type_ids:
                    warning = _apply_rah_phasing(attrs, phasing)
                    if warning:
                        warnings.append(warning)
                else:
                    warnings.append(
                        f"resist_phasing ignored for type {type_id}: not a "
                        f"reactive armor hardener"
                    )

            if item.get("overheated"):
                _apply_self_modifiers(attrs, overload_rows.get(type_id, []))

        enriched.append((item, attrs))

    return enriched, rah_type_ids, warnings


async def calculate_fitting_stats(
    db: AsyncSession, ship_type_id: int, items: list[dict],
    damage_profile: str = "uniform",
    skill_levels: dict[int, int] | None = None,
    target_resist_profile: str = "uniform",
    implants: list[int] | None = None,
    damage_profile_custom: list[float] | None = None,
    boosters: list[dict] | None = None,
) -> dict:
    """Calculate aggregate fitting stats for a ship + modules.

    ``boosters`` takes the entry shape documented in app/fitting/boosters.py.

    Applies the dogma modifier pipeline:
    1. Get base ship attributes
    2. Collect all passive modifiers from fitted modules
    3. Apply modifiers with stacking penalties
    4. Compute derived stats (align time, resists)
    """
    # Get raw ship attributes — mass/capacity from invTypes if not in dogma
    ship_attrs = await get_type_dogma_attrs(db, ship_type_id)
    if ATTR_MASS not in ship_attrs or ship_attrs[ATTR_MASS] == 0:
        type_result = await db.execute(
            select(SDEType.mass).where(SDEType.type_id == ship_type_id)
        )
        type_mass = type_result.scalar_one_or_none()
        if type_mass:
            ship_attrs[ATTR_MASS] = type_mass

    # Modifiers to the ship's OWN attributes from everything that is not a
    # fitted module — fitting skills, implants, boosters, hull and subsystem
    # traits — collected as {attribute_id: [(operator, value)]} and applied in
    # Step 2 in dogma operator order next to the modules' rows (ISS-043).
    # Applied on the spot, as they used to be, a skill's +25% shield HP landed
    # BEFORE a shield extender's +2600 rather than after it, because the
    # extender is a module and modules are collected later: a Drake with a
    # Large Shield Extender II showed 9475 shield HP where (5500 + 2600) x
    # 1.25 = 10125 is right. These sources are never stacking-penalized; the
    # modules' chain is untouched.
    ship_mods: dict[int, list[tuple[int, float]]] = defaultdict(list)

    # Collect all module type IDs (excluding drones and cargo)
    # Only online modules contribute to fitting (offline modules skip CPU/PG/effects)
    fitted_items = [i for i in items
                    if i.get("slot") not in ("drone", "cargo")
                    and i.get("online", True)]
    module_type_ids = list({item["type_id"] for item in fitted_items})
    all_type_ids = list({item["type_id"] for item in items})

    # Separate subsystem items — they need special handling (slot modifiers,
    # per-level scaling) and must be excluded from the normal modifier pipeline.
    subsystem_type_ids = set(
        i["type_id"] for i in fitted_items if i.get("slot") == "subsystem"
    )
    non_sub_module_type_ids = [t for t in module_type_ids if t not in subsystem_type_ids]

    # Get module attributes and modifiers (subsystems excluded from modifiers
    # — they're processed separately below)
    module_attrs_map = await get_types_dogma_attrs(db, all_type_ids) if all_type_ids else {}
    module_modifiers = await _get_module_modifiers(db, non_sub_module_type_ids) if non_sub_module_type_ids else {}

    # Build charge attrs map early so bonuses (ship hull, skills) can modify it.
    # Deep-copied so we can mutate charge damage values.
    charge_type_ids = list({
        item["charge_type_id"] for item in items
        if item.get("charge_type_id")
    })
    charge_attrs_map: dict[int, dict[int, float]] = {}
    if charge_type_ids:
        raw_charge = await get_types_dogma_attrs(db, charge_type_ids)
        charge_attrs_map = {tid: dict(attrs) for tid, attrs in raw_charge.items()}

    # Overload, charges and reactive-hardener phasing are NOT applied here.
    # They are per-ITEM concerns — two identical turrets can carry different
    # ammo, and only one of a pair of hardeners may be overheated — whereas
    # `module_attrs_map` is keyed by type_id and shared by every copy. They are
    # applied to per-item copies once this type-level pipeline is complete;
    # see `_build_item_attrs` below.

    # ── Apply T3C subsystem bonuses ─────────────────────────────────────────
    # Subsystems provide:
    # 1. Slot/hardpoint modifiers (special attrs not in the dogma effect pipeline)
    # 2. ItemModifier bonuses to ship attributes (CPU, PG, cap, velocity, etc.)
    # 3. LocationGroupModifier/LocationRequiredSkillModifier bonuses to modules
    # 4. OwnerRequiredSkillModifier bonuses to charges (missile damage, etc.)
    #
    # Per-level attributes (subsystemBonus*) are scaled by skill level.
    # This section must run BEFORE skill bonuses so that MOD_ADD contributions
    # (CPU, PG, HP) are included in the skill percentage calculation.
    if subsystem_type_ids:
        # 1. Apply slot/hardpoint modifiers (attributes not in effect pipeline)
        for item in fitted_items:
            if item.get("slot") != "subsystem":
                continue
            sub_attrs = module_attrs_map.get(item["type_id"], {})
            ship_attrs[ATTR_HI_SLOTS] = ship_attrs.get(ATTR_HI_SLOTS, 0) + sub_attrs.get(ATTR_HI_SLOT_MODIFIER, 0)
            ship_attrs[ATTR_MED_SLOTS] = ship_attrs.get(ATTR_MED_SLOTS, 0) + sub_attrs.get(ATTR_MED_SLOT_MODIFIER, 0)
            ship_attrs[ATTR_LOW_SLOTS] = ship_attrs.get(ATTR_LOW_SLOTS, 0) + sub_attrs.get(ATTR_LOW_SLOT_MODIFIER, 0)
            ship_attrs[ATTR_TURRET_SLOTS] = ship_attrs.get(ATTR_TURRET_SLOTS, 0) + sub_attrs.get(ATTR_TURRET_HARDPOINT_MODIFIER, 0)
            ship_attrs[ATTR_LAUNCHER_SLOTS] = ship_attrs.get(ATTR_LAUNCHER_SLOTS, 0) + sub_attrs.get(ATTR_LAUNCHER_HARDPOINT_MODIFIER, 0)

        # 2. Get subsystem modifiers and detect per-level source attributes
        sub_modifiers = await _get_module_modifiers(db, list(subsystem_type_ids))

        sub_src_attr_ids = set()
        for tid in subsystem_type_ids:
            for mod in sub_modifiers.get(tid, []):
                sub_src_attr_ids.add(mod["modifying_attribute_id"])

        sub_per_level_ids: set[int] = set()
        if sub_src_attr_ids:
            name_result = await db.execute(
                select(SDEDogmaAttribute.attribute_id, SDEDogmaAttribute.attribute_name)
                .where(SDEDogmaAttribute.attribute_id.in_(sub_src_attr_ids))
            )
            for row in name_result.fetchall():
                name = row.attribute_name or ""
                if name.startswith("subsystemBonus") and "Role" not in name:
                    sub_per_level_ids.add(row.attribute_id)

        # 3. Collect ItemModifier bonuses to the ship's own attributes
        #    (CPU, PG, drone BW, cap, velocity, agility, etc.) into
        #    `ship_mods`, where Step 2 applies them in dogma operator order
        #    together with every other source: MOD_ADD before POST_MUL before
        #    POST_PERCENT, whichever item contributed which.
        #    Algorithm reference: pyfa eos/modifiedAttributeDict.py:308-416
        #    (__calculateValue — accumulates into operator-type buckets, applies
        #    in fixed order); theorycrafter FittingEngine.kt:3490-3582
        #    (iterates Operation.entries in enum declaration order).
        for item in fitted_items:
            if item.get("slot") != "subsystem":
                continue
            tid = item["type_id"]
            sub_attrs = module_attrs_map.get(tid, {})
            # Per-subsystem scaling skill lookup (e.g. Caldari Offensive
            # Systems for the Tengu's offensive subsystem). Cached for the
            # duration of this subsystem's pass.
            sub_scaling_skills = await _subsystem_scaling_skill_ids(db, tid)
            sub_effective_level = _effective_skill_level(sub_scaling_skills, skill_levels)
            for mod in sub_modifiers.get(tid, []):
                if mod["func"] != "ItemModifier" or mod["domain"] != "shipID":
                    continue
                src_val = sub_attrs.get(mod["modifying_attribute_id"])
                if src_val is None:
                    continue
                if mod["modifying_attribute_id"] in sub_per_level_ids:
                    src_val *= sub_effective_level
                ship_mods[mod["modified_attribute_id"]].append(
                    (mod["operator"], src_val)
                )

    # ── Type-level bonuses: skills, implants, boosters, hull, subsystems ──
    # These are keyed by type_id and must land in module_attrs_map and
    # charge_attrs_map BEFORE the per-item dicts below are copied from them.
    # From v1.4.0 to v1.4.2 this block ran after that copy, so a skill,
    # implant or hull bonus changed a type-level dict nothing read any more
    # and DPS, rep and application figures lost every one of them; only
    # charges (read from charge_attrs_map directly) kept theirs.
    # ── Apply fitting-skill bonuses to ship attributes ────────────────────
    # These core skills use ItemModifier with domain=shipID to add +5% per
    # level to the ship's own CPU/PG/HP attributes. They're applied outside
    # the dogma modifier graph because the graph doesn't target the ship's
    # own attrs (only modules/drones/charges).  When `skill_levels` is
    # supplied, each bonus scales by the character's actual level in that
    # specific skill; otherwise All V (× 1.25) is assumed. Collected rather
    # than applied, so an extender's or plate's flat add lands first.
    _FITTING_SKILLS_FIVE_PCT = [
        (3426, ATTR_CPU_OUTPUT),       # CPU Management
        (3413, ATTR_POWER_OUTPUT),     # Power Grid Management
        (3419, ATTR_SHIELD_HP),        # Shield Management
        (3394, ATTR_ARMOR_HP),         # Hull Upgrades
        (3392, ATTR_HP),               # Mechanics
    ]
    for skill_id, attr_id in _FITTING_SKILLS_FIVE_PCT:
        lvl = DEFAULT_SKILL_LEVEL if skill_levels is None else skill_levels.get(skill_id, 0)
        if lvl:
            ship_mods[attr_id].append((OP_POST_PERCENT, 5.0 * lvl))

    # ── Apply All-V weapon/support skill bonuses to module attributes ─────
    # Skills like Surgical Strike, Rapid Firing, etc. have modifiers that
    # target modules by group or required skill. At All V, the bonus is
    # skill_base_attr * 5, applied as postPercent.
    await _apply_all_v_skill_bonuses(
        db, module_attrs_map, charge_attrs_map, items, skill_levels=skill_levels,
    )

    # Fill the drone-skill modifier gaps the SDE doesn't propagate
    # (Light/Med/Heavy Drone Operation + racial specs + Sentry Interfacing).
    await _apply_drone_skill_bonuses(
        db, module_attrs_map, items, skill_levels=skill_levels,
    )

    # Apply implant bonuses (slots 1-10). Stat-training implants in slots
    # 1-5 are no-ops here; combat hardwirings in slots 6-10 modify damage,
    # range, cap recharge, agility, CPU/PG, etc. Mix of OwnerRequiredSkillModifier
    # (damage-via-skill) and ItemModifier+shipID (ship-self attrs like CPU).
    if implants:
        await _apply_implant_bonuses(
            db, ship_attrs, module_attrs_map, charge_attrs_map, implants,
            ship_mods=ship_mods,
        )

    # Combat boosters (T-049): primary effects always, side effects only
    # when switched on. Entry shape and rules live in app/fitting/boosters.py.
    if boosters:
        await apply_booster_bonuses(
            db, ship_attrs, module_attrs_map, charge_attrs_map, boosters,
            ship_mods=ship_mods,
        )

    # ── Apply ship hull bonuses to module/charge attributes ──────────────
    # Makes deep copies so we don't mutate cached SDE data
    module_attrs_map = {tid: dict(attrs) for tid, attrs in module_attrs_map.items()}
    await _apply_ship_hull_bonuses(
        db, ship_type_id, ship_attrs, module_attrs_map, charge_attrs_map, items,
        skill_levels=skill_levels, ship_mods=ship_mods,
    )

    # ── Apply subsystem bonuses to module/charge attributes ──────────────
    # Subsystem LocationGroupModifier/LocationRequiredSkillModifier/
    # OwnerRequiredSkillModifier bonuses work like ship hull bonuses but
    # originate from the fitted subsystem. Re-use _apply_ship_hull_bonuses
    # with each subsystem as the source (per-level detection extended to
    # cover subsystemBonus* attributes).
    if subsystem_type_ids:
        for item in fitted_items:
            if item.get("slot") != "subsystem":
                continue
            sub_attrs = module_attrs_map.get(item["type_id"], {})
            sub_scaling = await _subsystem_scaling_skill_ids(db, item["type_id"])
            await _apply_ship_hull_bonuses(
                db, item["type_id"], sub_attrs,
                module_attrs_map, charge_attrs_map, items,
                skill_levels=skill_levels,
                scaling_skill_override=sub_scaling,
            )


    # ── Per-item attributes ───────────────────────────────────────────────
    # The type-level pipeline (subsystems, hull bonuses, skills) is finished.
    # Everything above depends only on what a module IS, which is why keying
    # by type_id was right for it. Everything below depends on how a
    # particular copy is being USED — which ammo or script is loaded, whether
    # this one is overheated, how a reactive hardener is phased — so each
    # fitted item gets its own attribute dict from here on.
    #
    # This must happen BEFORE the module-to-module pass below, not after it.
    # A tracking computer's strength attribute is raised by its script and
    # then pushed out to the turrets by the module's own effect; building the
    # per-item dicts afterwards meant that pass read the module's unscripted
    # value, so a loaded script changed the module and nothing else.
    enriched_items, rah_type_ids, warnings = await _build_item_attrs(
        db, items, module_attrs_map, charge_attrs_map,
    )
    enriched_fitted = [
        (item, attrs) for item, attrs in enriched_items
        if item.get("slot") not in ("drone", "cargo") and item.get("online", True)
    ]

    # Per-item attribute dicts grouped by type, for the pass below to write
    # into. Drones and cargo share their type-level dict by design (they have
    # no per-copy state), so the same object can appear under several items —
    # dedupe by identity or a group bonus would be applied to them twice.
    item_attrs_by_type: dict[int, list[dict[int, float]]] = defaultdict(list)
    for _item, _attrs in enriched_items:
        bucket = item_attrs_by_type[_item["type_id"]]
        if not any(existing is _attrs for existing in bucket):
            bucket.append(_attrs)

    # ── Apply module-to-module bonuses (Bastion, Siege, etc.) ──────────────
    # Some modules (Bastion, Siege) have effects that modify OTHER modules
    # via LocationRequiredSkillModifier / LocationGroupModifier.
    # These are different from ship hull bonuses — the source attrs are on
    # the fitted module, not the ship.
    if non_sub_module_type_ids:
        # Get all cross-module modifiers from fitted module effects.
        # Includes OwnerRequiredSkillModifier (domain=charID) for damage mods
        # like DDA that target drones by skill requirement.
        mod_cross_result = await db.execute(
            select(SDETypeEffect.type_id, SDEModifier.modifying_attribute_id,
                   SDEModifier.modified_attribute_id, SDEModifier.operator,
                   SDEModifier.func, SDEModifier.filter_type, SDEModifier.filter_value)
            .join(SDEEffect, SDETypeEffect.effect_id == SDEEffect.effect_id)
            .join(SDEModifier, SDEModifier.effect_id == SDETypeEffect.effect_id)
            .where(SDETypeEffect.type_id.in_(non_sub_module_type_ids))
            .where(SDEEffect.effect_category.in_(MODULE_EFFECT_CATS))
            .where(SDEModifier.domain.in_(["shipID", "charID"]))
            .where(SDEModifier.func.in_([
                "LocationRequiredSkillModifier",
                "LocationGroupModifier",
                "OwnerRequiredSkillModifier",
            ]))
        )

        cross_mods = mod_cross_result.fetchall()
        if cross_mods:
            # Source modules, per copy rather than per type: two tracking
            # computers can hold different scripts, and only one of a pair of
            # damage mods may be overheated, so each contributes its own
            # source value.
            source_items: dict[int, list[tuple[int, dict[int, float]]]] = defaultdict(list)
            for _item, _attrs in enriched_fitted:
                source_items[_item["type_id"]].append(
                    (_item.get("quantity", 1), _attrs)
                )

            # Build skill/group lookups for all module types
            all_tid_list = list(module_attrs_map.keys())
            _skill_reqs: dict[int, set[int]] = defaultdict(set)
            sr_result = await db.execute(
                select(SDETypeSkillReq.type_id, SDETypeSkillReq.skill_type_id)
                .where(SDETypeSkillReq.type_id.in_(all_tid_list))
            )
            for row in sr_result.fetchall():
                _skill_reqs[row.type_id].add(row.skill_type_id)

            _group_ids: dict[int, int] = {}
            gr_result = await db.execute(
                select(SDEType.type_id, SDEType.group_id)
                .where(SDEType.type_id.in_(all_tid_list))
            )
            _group_ids = {row.type_id: row.group_id for row in gr_result.fetchall()}

            # Get stackable flags for stacking penalty check
            cross_target_attrs = {cm[2] for cm in cross_mods}
            _stackable = await _get_stackable_flags(db, cross_target_attrs)

            # Collect cross-module multipliers grouped by (target_tid, target_attr, source_type).
            # Grouping by source type means copies of the same damage mod stack-penalize
            # each other, but different module types (e.g. Bastion vs Heat Sinks)
            # are independent groups whose products are multiplied together.
            # Key: (target_tid, target_attr, source_type_id) → list of multipliers
            cross_collectors: dict[tuple[int, int, int], list[float]] = defaultdict(list)

            for cm in cross_mods:
                src_type_id = cm[0]
                sources = source_items.get(src_type_id)
                if not sources:
                    continue

                # Find target modules/drones
                matching = set()
                if cm[4] == "LocationRequiredSkillModifier" and cm[5] == "skill":
                    for tid, skills in _skill_reqs.items():
                        if tid != src_type_id and cm[6] in skills:
                            matching.add(tid)
                elif cm[4] == "LocationGroupModifier" and cm[5] == "group":
                    for tid, gid in _group_ids.items():
                        if tid != src_type_id and gid == cm[6]:
                            matching.add(tid)
                elif cm[4] == "OwnerRequiredSkillModifier" and cm[5] == "skill":
                    for tid, skills in _skill_reqs.items():
                        if tid != src_type_id and cm[6] in skills:
                            matching.add(tid)

                if not matching:
                    continue

                target_attr = cm[2]
                operator = cm[3]

                for tid in matching:
                    targets = item_attrs_by_type.get(tid)
                    if not targets:
                        continue

                    for copies, src_attrs in sources:
                        src_val = src_attrs.get(cm[1])
                        if src_val is None:
                            continue

                        if operator == OP_POST_MUL:
                            for _ in range(copies):
                                cross_collectors[(tid, target_attr, src_type_id)].append(src_val)
                        elif operator == OP_POST_PERCENT:
                            for _ in range(copies):
                                cross_collectors[(tid, target_attr, src_type_id)].append(1.0 + src_val / 100.0)
                        elif operator == OP_MOD_ADD:
                            for attrs in targets:
                                attrs[target_attr] = (
                                    attrs.get(target_attr, 0) + src_val * copies
                                )

            # Apply multipliers per group with stacking penalties, then combine.
            # Reorganize: (target_tid, target_attr) → list of per-group products.
            combined: dict[tuple[int, int], float] = defaultdict(lambda: 1.0)
            for (tid, target_attr, src_tid), multipliers in cross_collectors.items():
                is_stackable = _stackable.get(target_attr, True)
                if is_stackable or len(multipliers) <= 1:
                    group_product = 1.0
                    for m in multipliers:
                        group_product *= m
                else:
                    group_product = apply_stacking_penalties(multipliers)
                combined[(tid, target_attr)] *= group_product

            for (tid, target_attr), product in combined.items():
                for attrs in item_attrs_by_type.get(tid, []):
                    if target_attr == ATTR_DAMAGE_MULTIPLIER and target_attr not in attrs:
                        attrs[target_attr] = 1.0
                    current = attrs.get(target_attr, 0)
                    if current == 0 and target_attr == ATTR_DAMAGE_MULTIPLIER:
                        current = 1.0
                    attrs[target_attr] = current * product

    # ── Collect character-level missile damage multiplier (BCU mechanism) ──
    # BCU sets character attr 212 (missileDamageMultiplier) via ItemModifier
    # with domain=charID.  Accumulate from all online fitted modules that have
    # this modifier, then apply as a global scale on missile charge damage.
    char_missile_dmg_mult = 1.0
    if module_type_ids:
        bcu_result = await db.execute(
            select(SDETypeEffect.type_id, SDEModifier.modifying_attribute_id,
                   SDEModifier.operator)
            .join(SDEEffect, SDETypeEffect.effect_id == SDEEffect.effect_id)
            .join(SDEModifier, SDEModifier.effect_id == SDETypeEffect.effect_id)
            .where(SDETypeEffect.type_id.in_(module_type_ids))
            .where(SDEEffect.effect_category.in_(MODULE_EFFECT_CATS))
            .where(SDEModifier.domain == "charID")
            .where(SDEModifier.func == "ItemModifier")
            .where(SDEModifier.modified_attribute_id == ATTR_MISSILE_DAMAGE_MULTIPLIER)
        )
        bcu_mods = bcu_result.fetchall()
        if bcu_mods:
            # Count copies and collect multipliers
            module_copies: dict[int, int] = defaultdict(int)
            for item in fitted_items:
                module_copies[item["type_id"]] += item.get("quantity", 1)
            bcu_multipliers: list[float] = []
            for row in bcu_mods:
                src_tid = row.type_id
                src_attr_id = row.modifying_attribute_id
                operator = row.operator
                src_val = module_attrs_map.get(src_tid, {}).get(src_attr_id)
                if src_val is None or src_val == 0:
                    continue
                copies = module_copies.get(src_tid, 0)
                for _ in range(copies):
                    if operator == OP_PRE_MUL:
                        bcu_multipliers.append(src_val)
                    elif operator == OP_POST_PERCENT:
                        bcu_multipliers.append(1.0 + src_val / 100.0)
            if bcu_multipliers:
                # BCU multipliers are stacking-penalized
                stackable_result = await _get_stackable_flags(
                    db, {ATTR_MISSILE_DAMAGE_MULTIPLIER}
                )
                is_stackable = stackable_result.get(ATTR_MISSILE_DAMAGE_MULTIPLIER, True)
                if is_stackable or len(bcu_multipliers) <= 1:
                    for m in bcu_multipliers:
                        char_missile_dmg_mult *= m
                else:
                    char_missile_dmg_mult *= apply_stacking_penalties(bcu_multipliers)

    # Fetch module slot info for turret/launcher counting. Keyed off every
    # slotted item rather than `module_type_ids` (which is online-only),
    # because an offline module still occupies its hardpoint.
    slotted_type_ids = list({
        i["type_id"] for i in items if i.get("slot") not in ("drone", "cargo")
    })
    slot_info = {}
    if slotted_type_ids:
        slot_result = await db.execute(
            select(SDEModuleSlot.type_id, SDEModuleSlot.is_turret, SDEModuleSlot.is_launcher)
            .where(SDEModuleSlot.type_id.in_(slotted_type_ids))
        )
        for row in slot_result.fetchall():
            slot_info[row.type_id] = {"is_turret": row.is_turret, "is_launcher": row.is_launcher}

    # Fetch drone volumes
    drone_items = [i for i in items if i.get("slot") == "drone"]
    drone_volumes = {}
    if drone_items:
        drone_type_ids = list({d["type_id"] for d in drone_items})
        vol_result = await db.execute(
            select(SDEType.type_id, SDEType.volume)
            .where(SDEType.type_id.in_(drone_type_ids))
        )
        drone_volumes = {row.type_id: row.volume or 0 for row in vol_result.fetchall()}

    # ── Step 1: Collect all attribute modifiers from fitted modules ────────
    # modifier_collectors[target_attr_id] = list of (operator, value) tuples
    mod_collectors: dict[int, list[tuple[int, float]]] = defaultdict(list)

    # A reactive armor hardener's resonances are collected apart from every
    # other resist module. Its defining effect (4928) carries no modifierInfo
    # — CCP redistributes the resists server-side — so the module contributed
    # nothing at all until this branch existed. Pyfa applies it with
    # penaltyGroup='preMul', i.e. its own stacking group, and since only one
    # reactive hardener may be fitted (maxGroupFitted = 1) it is always first
    # in that group and therefore unpenalised.
    rah_collectors: dict[int, list[float]] = defaultdict(list)

    for item, mod_attrs in enriched_fitted:
        tid = item["type_id"]
        qty = item.get("quantity", 1)
        mods = module_modifiers.get(tid, [])

        if tid in rah_type_ids:
            for attr_id in ARMOR_RESONANCE_ATTRS:
                resonance = mod_attrs.get(attr_id)
                if resonance is not None and resonance != 1.0:
                    rah_collectors[attr_id].append(resonance)

        for mod in mods:
            # Only apply ship-domain modifiers (or item-domain for self-modifying)
            if mod["domain"] != "shipID":
                continue

            # Get the source attribute value from the module
            src_val = mod_attrs.get(mod["modifying_attribute_id"])
            if src_val is None:
                continue

            target_attr = mod["modified_attribute_id"]
            operator = mod["operator"]

            # Apply for each copy of the module
            for _ in range(qty):
                mod_collectors[target_attr].append((operator, src_val))

    # ── Step 2: Apply modifiers to ship attributes ────────────────────────
    # Get stackable flags for all modified attributes
    all_modified_attrs = set(mod_collectors.keys())
    stackable_flags = await _get_stackable_flags(db, all_modified_attrs)

    # Build modified ship attributes. Two kinds of row meet here per
    # attribute: the fitted modules' (`mod_collectors`, stacking-penalized
    # when the attribute is not stackable) and everything else's
    # (`ship_mods`: fitting skills, implants, boosters, hull and subsystem
    # traits, never penalized). Both go through the one operator order
    # below, which is what puts an extender's flat add ahead of Shield
    # Management's percentage whichever was collected first (ISS-043).
    modified_attrs = dict(ship_attrs)

    for attr_id in set(mod_collectors) | set(ship_mods):
        modifiers = mod_collectors.get(attr_id, [])
        own = ship_mods.get(attr_id, [])
        combined = modifiers + own
        base = modified_attrs.get(attr_id, 0)

        # Group by CCP operator. Only the multiplicative buckets keep the
        # module rows apart, for the stacking penalty.
        pre_assigns = [v for op, v in combined if op == OP_PRE_ASSIGN]
        pre_muls = [v for op, v in combined if op == OP_PRE_MUL]
        pre_divs = [v for op, v in combined if op == OP_PRE_DIV]
        mod_adds = [v for op, v in combined if op == OP_MOD_ADD]
        mod_subs = [v for op, v in combined if op == OP_MOD_SUB]
        post_muls = [v for op, v in modifiers if op == OP_POST_MUL]
        own_post_muls = [v for op, v in own if op == OP_POST_MUL]
        post_divs = [v for op, v in combined if op == OP_POST_DIV]
        post_pcts = [v for op, v in modifiers if op == OP_POST_PERCENT]
        own_post_pcts = [v for op, v in own if op == OP_POST_PERCENT]
        post_assigns = [v for op, v in combined if op == OP_POST_ASSIGN]

        # POST_ASSIGN: force/lock — skip all other modifiers
        if post_assigns:
            modified_attrs[attr_id] = post_assigns[-1]
            continue

        val = base

        # PRE_ASSIGN: override base
        if pre_assigns:
            val = pre_assigns[-1]

        # PRE_MUL / PRE_DIV
        for m in pre_muls:
            val *= m
        for d in pre_divs:
            if d != 0:
                val /= d

        # MOD_ADD / MOD_SUB: flat changes
        for a in mod_adds:
            val += a
        for s in mod_subs:
            val -= s

        # POST_MUL: multiplicative — stacking penalized if attribute not stackable
        is_stackable = stackable_flags.get(attr_id, True)
        if post_muls:
            if is_stackable:
                for m in post_muls:
                    val *= m
            else:
                val *= apply_stacking_penalties(post_muls)
        for m in own_post_muls:
            val *= m

        # POST_DIV
        for d in post_divs:
            if d != 0:
                val /= d

        # POST_PERCENT: val = val * (1 + modifier/100) — stacking penalized if needed
        if post_pcts:
            pct_muls = [1.0 + p / 100.0 for p in post_pcts]
            if is_stackable:
                for m in pct_muls:
                    val *= m
            else:
                val *= apply_stacking_penalties(pct_muls)
        for p in own_post_pcts:
            val *= 1.0 + p / 100.0

        modified_attrs[attr_id] = val

    # Reactive hardener resonances, in their own stacking group (see above).
    for attr_id, resonances in rah_collectors.items():
        modified_attrs[attr_id] = (
            modified_attrs.get(attr_id, 1.0) * apply_stacking_penalties(resonances)
        )

    # ── Step 3: Calculate resource usage (from module attributes) ─────────
    cpu_used = 0.0
    pg_used = 0.0
    calibration_used = 0.0
    turrets_used = 0
    launchers_used = 0
    drone_bw_used = 0.0
    drone_bay_used = 0.0

    for item, mod_attrs in enriched_items:
        tid = item["type_id"]
        qty = item.get("quantity", 1)
        slot = item.get("slot", "")

        if slot == "drone":
            drone_bw_used += mod_attrs.get(ATTR_DRONE_BW_USED, 0) * qty
            drone_bay_used += drone_volumes.get(tid, 0) * qty
            continue

        if slot == "cargo":
            continue

        # Hardpoints are consumed by the module being FITTED, not by it being
        # powered: an offline turret still occupies its turret hardpoint, and
        # taking it offline to free one is not a thing you can do in game.
        # Counted before the offline check below, which is about power.
        si = slot_info.get(tid, {})
        if si.get("is_turret"):
            turrets_used += qty
        if si.get("is_launcher"):
            launchers_used += qty

        # Offline modules draw no CPU and no powergrid — taking a module
        # offline to fit the rest of a tight fit is a standard EVE technique.
        # Rigs are exempt because calibration is consumed by installation, and
        # a rig has no online/offline state to begin with.
        if not item.get("online", True) and slot != "rig":
            continue

        cpu_used += mod_attrs.get(ATTR_CPU, 0) * qty
        pg_used += mod_attrs.get(ATTR_POWER, 0) * qty

        if slot == "rig":
            calibration_used += mod_attrs.get(ATTR_UPGRADE_COST, 0) * qty

    # ── Step 4: Build stats from modified attributes ──────────────────────
    def mattr(attr_id, default=0):
        return modified_attrs.get(attr_id, default)

    # Recalculate derived stats from modified attributes
    align_time = _calc_align_time(mattr(ATTR_INERTIA), mattr(ATTR_MASS))

    # Recalculate resists from modified resonances
    shield_em_res = mattr(ATTR_SHIELD_EM_RESONANCE, 1.0)
    shield_therm_res = mattr(ATTR_SHIELD_THERM_RESONANCE, 1.0)
    shield_kin_res = mattr(ATTR_SHIELD_KIN_RESONANCE, 1.0)
    shield_expl_res = mattr(ATTR_SHIELD_EXPL_RESONANCE, 1.0)
    armor_em_res = mattr(ATTR_ARMOR_EM_RESONANCE, 1.0)
    armor_therm_res = mattr(ATTR_ARMOR_THERM_RESONANCE, 1.0)
    armor_kin_res = mattr(ATTR_ARMOR_KIN_RESONANCE, 1.0)
    armor_expl_res = mattr(ATTR_ARMOR_EXPL_RESONANCE, 1.0)
    hull_em_res = mattr(ATTR_HULL_EM_RESONANCE, 1.0)
    hull_therm_res = mattr(ATTR_HULL_THERM_RESONANCE, 1.0)
    hull_kin_res = mattr(ATTR_HULL_KIN_RESONANCE, 1.0)
    hull_expl_res = mattr(ATTR_HULL_EXPL_RESONANCE, 1.0)

    # EHP — uniform damage profile (25/25/25/25)
    shield_hp = mattr(ATTR_SHIELD_HP)
    armor_hp = mattr(ATTR_ARMOR_HP)
    hull_hp = mattr(ATTR_HP)
    dmg_prof = resolve_damage_profile(damage_profile, damage_profile_custom)
    shield_ehp = _calc_ehp(shield_hp, shield_em_res, shield_therm_res, shield_kin_res, shield_expl_res, dmg_prof)
    armor_ehp = _calc_ehp(armor_hp, armor_em_res, armor_therm_res, armor_kin_res, armor_expl_res, dmg_prof)
    hull_ehp = _calc_ehp(hull_hp, hull_em_res, hull_therm_res, hull_kin_res, hull_expl_res, dmg_prof)
    total_ehp = shield_ehp + armor_ehp + hull_ehp

    # ── Capacitor simulation (discrete-event) ──────────────────────────
    cap_capacity = mattr(ATTR_CAPACITOR)
    cap_recharge_ms = mattr(ATTR_CAP_RECHARGE)  # in ms

    # Build module drain list for the simulator
    cap_sim_modules: list[dict] = []
    for item, mod_attrs in enriched_fitted:
        tid = item["type_id"]
        qty = item.get("quantity", 1)
        # Negative for a capacitor booster with a charge loaded — cap_sim
        # reads that as an injection rather than a drain.
        cap_need = mod_attrs.get(ATTR_CAPACITOR_NEED, 0)
        duration = mod_attrs.get(ATTR_DURATION, 0) or mod_attrs.get(ATTR_RATE_OF_FIRE, 0)
        if cap_need != 0 and duration > 0:
            si = slot_info.get(tid, {})
            cap_sim_modules.append({
                "cap_need": cap_need,
                "duration_ms": duration,
                "count": qty,
                "stagger": not si.get("is_turret", False),  # turrets fire together
            })

    cap_result = simulate_cap(cap_capacity, cap_recharge_ms, cap_sim_modules)
    cap_stable = cap_result["stable"]
    cap_stable_pct = cap_result["stable_pct"]
    cap_lasts_s = cap_result["time_to_empty_s"]
    peak_cap_recharge = cap_result["peak_recharge"]
    total_cap_drain = cap_result["total_drain"]

    # Shield recharge (passive tank)
    shield_recharge_ms = mattr(ATTR_SHIELD_RECHARGE_RATE, 0)
    shield_recharge_s = shield_recharge_ms / 1000 if shield_recharge_ms else 0
    peak_shield_recharge = _calc_peak_recharge(shield_hp, shield_recharge_s)

    # ── Active tank (rep/s) ──────────────────────────────────────────────
    armor_rep_rate = 0.0
    shield_rep_rate = 0.0
    for item, mod_attrs in enriched_fitted:
        qty = item.get("quantity", 1)
        duration = mod_attrs.get(ATTR_DURATION, 0)
        if duration <= 0:
            continue
        cycle_s = duration / 1000.0
        # Armor repair modules have armorDamageAmount (attr 84)
        armor_rep = mod_attrs.get(ATTR_ARMOR_DAMAGE_AMOUNT, 0)
        if armor_rep > 0:
            armor_rep_rate += (armor_rep / cycle_s) * qty
        # Shield boost modules have shieldBonus (attr 68)
        shield_rep = mod_attrs.get(ATTR_SHIELD_BONUS, 0)
        if shield_rep > 0:
            shield_rep_rate += (shield_rep / cycle_s) * qty

    # ── Step 6: DPS calculation ──────────────────────────────────────────
    # charge_attrs_map was built early and modified by skill/hull bonus steps.
    weapon_dps = 0.0
    weapon_dps_max_spool = 0.0  # DPS at max spool (Triglavian)
    weapon_volley = 0.0
    drone_dps = 0.0
    spool_time_s = 0  # Time to reach max spool
    # Per-type DPS accumulators for resist-weighted effective DPS calc.
    # Order: (em, therm, kin, expl)
    weapon_dps_typed = [0.0, 0.0, 0.0, 0.0]
    drone_dps_typed = [0.0, 0.0, 0.0, 0.0]
    # Turret application figures, taken from the first firing turret. A
    # tracking or optimal-range script changes exactly these and nothing else,
    # so without them the response could not show that a loaded script had
    # done anything at all. Reported per turret rather than summed — they are
    # properties of a gun, not quantities that add up across a fit.
    weapon_tracking = 0.0
    weapon_optimal_range = 0.0

    # Determine how many drones of each type are active (within bandwidth).
    # Sort by bandwidth cost ascending to maximize active drone count,
    # matching Pyfa's approach of capping by bandwidth.
    # Algorithm reference: pyfa eos/saveddata/drone.py:163-168 (amountActive
    # gates DPS); theorycrafter Mechanics.kt:1204-1236 (maxDroneGroupSize
    # caps at bandwidth/bwPerDrone).
    drone_bw_total_ship = mattr(SHIP_STAT_ATTRS["drone_bandwidth"])
    drone_active_counts: dict[int, int] = {}  # type_id → active count
    drone_entries = [(i["type_id"], i.get("quantity", 1)) for i in items if i.get("slot") == "drone"]
    # Sort by per-drone DPS descending so highest-DPS drones fill first
    # (matches typical player behavior — activate strongest drones).
    drone_bw_per = {}
    drone_dps_per: dict[int, float] = {}
    for tid, qty in drone_entries:
        if tid not in drone_bw_per:
            d_attrs = module_attrs_map.get(tid, {})
            drone_bw_per[tid] = d_attrs.get(ATTR_DRONE_BW_USED, 0)
            d_dmg = sum(d_attrs.get(a, 0) for a in (ATTR_EM_DAMAGE, ATTR_THERMAL_DAMAGE, ATTR_KINETIC_DAMAGE, ATTR_EXPLOSIVE_DAMAGE))
            d_mult = d_attrs.get(ATTR_DAMAGE_MULTIPLIER, 1)
            d_cycle = d_attrs.get(ATTR_DURATION, 0) or d_attrs.get(ATTR_RATE_OF_FIRE, 0)
            drone_dps_per[tid] = (d_dmg * d_mult / (d_cycle / 1000)) if d_cycle > 0 and d_dmg > 0 else 0
    drone_entries.sort(key=lambda x: -drone_dps_per.get(x[0], 0))
    bw_remaining = drone_bw_total_ship
    for tid, qty in drone_entries:
        bw_each = drone_bw_per.get(tid, 0)
        if bw_each <= 0:
            drone_active_counts[tid] = drone_active_counts.get(tid, 0) + qty
            continue
        can_fit = int(bw_remaining / bw_each) if bw_each > 0 else qty
        active = min(qty, can_fit)
        drone_active_counts[tid] = drone_active_counts.get(tid, 0) + active
        bw_remaining -= active * bw_each

    for item, mod_attrs in enriched_items:
        tid = item["type_id"]
        qty = item.get("quantity", 1)
        slot = item.get("slot", "")

        if slot == "drone":
            # Drone DPS: only count drones within bandwidth
            active_qty = min(qty, drone_active_counts.get(tid, 0))
            if active_qty <= 0:
                continue
            # Decrement so subsequent entries of same type don't double-count
            drone_active_counts[tid] = drone_active_counts.get(tid, 0) - active_qty
            em = mod_attrs.get(ATTR_EM_DAMAGE, 0)
            therm = mod_attrs.get(ATTR_THERMAL_DAMAGE, 0)
            kin = mod_attrs.get(ATTR_KINETIC_DAMAGE, 0)
            expl = mod_attrs.get(ATTR_EXPLOSIVE_DAMAGE, 0)
            dmg_mult = mod_attrs.get(ATTR_DAMAGE_MULTIPLIER, 1)
            cycle = mod_attrs.get(ATTR_DURATION, 0) or mod_attrs.get(ATTR_RATE_OF_FIRE, 0)
            if cycle > 0 and (em + therm + kin + expl) > 0:
                volley = (em + therm + kin + expl) * dmg_mult
                drone_dps += (volley / (cycle / 1000)) * active_qty
                # Per-type contribution for resist-weighted DPS
                cycle_s = cycle / 1000
                drone_dps_typed[0] += (em * dmg_mult / cycle_s) * active_qty
                drone_dps_typed[1] += (therm * dmg_mult / cycle_s) * active_qty
                drone_dps_typed[2] += (kin * dmg_mult / cycle_s) * active_qty
                drone_dps_typed[3] += (expl * dmg_mult / cycle_s) * active_qty
            continue

        if slot in ("cargo", "drone"):
            continue

        # Offline weapons don't fire
        if not item.get("online", True):
            continue

        # Turret/Launcher DPS: charge damage × module damageMultiplier / cycleTime
        charge_tid = item.get("charge_type_id")
        if not charge_tid:
            continue

        charge_attrs = charge_attrs_map.get(charge_tid, {})
        em = charge_attrs.get(ATTR_EM_DAMAGE, 0)
        therm = charge_attrs.get(ATTR_THERMAL_DAMAGE, 0)
        kin = charge_attrs.get(ATTR_KINETIC_DAMAGE, 0)
        expl = charge_attrs.get(ATTR_EXPLOSIVE_DAMAGE, 0)
        total_dmg = em + therm + kin + expl
        if total_dmg <= 0:
            continue

        dmg_mult = mod_attrs.get(ATTR_DAMAGE_MULTIPLIER, 1)
        cycle = mod_attrs.get(ATTR_RATE_OF_FIRE, 0) or mod_attrs.get(ATTR_DURATION, 0)
        if cycle <= 0:
            continue

        # Apply character-level missile damage multiplier (from BCU) to launchers.
        # Launchers don't have their own damageMultiplier — all damage comes from
        # the charge.  BCU scales charge damage via the character's attr 212.
        si = slot_info.get(tid, {})
        if si.get("is_launcher") and char_missile_dmg_mult != 1.0:
            total_dmg *= char_missile_dmg_mult

        if not weapon_tracking and not weapon_optimal_range:
            weapon_tracking = mod_attrs.get(ATTR_TRACKING_SPEED, 0) or 0.0
            weapon_optimal_range = mod_attrs.get(ATTR_OPTIMAL_RANGE, 0) or 0.0

        volley = total_dmg * dmg_mult
        weapon_volley += volley * qty
        weapon_dps += (volley / (cycle / 1000)) * qty
        # Per-type contribution for resist-weighted DPS. char_missile_dmg_mult
        # already folded into total_dmg above; preserve the same proportions
        # across the four types so the typed sum matches the scalar volley.
        if total_dmg > 0:
            cycle_s = cycle / 1000
            scale = (volley / (em + therm + kin + expl)) if (em + therm + kin + expl) > 0 else 0
            weapon_dps_typed[0] += (em * scale / cycle_s) * qty
            weapon_dps_typed[1] += (therm * scale / cycle_s) * qty
            weapon_dps_typed[2] += (kin * scale / cycle_s) * qty
            weapon_dps_typed[3] += (expl * scale / cycle_s) * qty

        # Spool-up: Triglavian entropic disintegrators ramp damage per cycle.
        # Spool is a separate multiplier on the volley (matching Pyfa/in-game):
        #   spool_volley = base_volley × (1 + spoolBoost)
        # where spoolBoost = damageMultiplierBonusMax (already modified by ship
        # hull bonuses like Babaroga's +100%).  The base volley already includes
        # all skill/ship/mod multipliers on damageMultiplier.
        spool_per_cycle = mod_attrs.get(ATTR_DMG_MULT_BONUS_PER_CYCLE, 0)
        spool_max = mod_attrs.get(ATTR_DMG_MULT_BONUS_MAX, 0)
        if spool_per_cycle > 0 and spool_max > 0:
            spool_volley = volley * (1 + spool_max)
            weapon_dps_max_spool += (spool_volley / (cycle / 1000)) * qty
            cycles_to_max = math.ceil(spool_max / spool_per_cycle)
            spool_time_s = max(spool_time_s, int(cycles_to_max * cycle / 1000))
        else:
            weapon_dps_max_spool += (volley / (cycle / 1000)) * qty

    total_dps = weapon_dps + drone_dps
    total_dps_max_spool = weapon_dps_max_spool + drone_dps

    # Resist-weighted effective DPS against the target's resist profile.
    # effective = raw_per_type * (1 - target_resist_per_type), summed.
    target_resists = TARGET_RESIST_PROFILES.get(target_resist_profile,
                                                TARGET_RESIST_PROFILES["uniform"])
    effective_weapon_dps = sum(
        weapon_dps_typed[i] * (1.0 - target_resists[i]) for i in range(4)
    )
    effective_drone_dps = sum(
        drone_dps_typed[i] * (1.0 - target_resists[i]) for i in range(4)
    )
    effective_total_dps = effective_weapon_dps + effective_drone_dps
    # Spool ratio (only weapons can spool) — apply the same uplift to typed
    # weapon DPS for the effective max-spool figure.
    weapon_spool_ratio = (weapon_dps_max_spool / weapon_dps) if weapon_dps > 0 else 1.0
    effective_total_dps_max_spool = (effective_weapon_dps * weapon_spool_ratio
                                     + effective_drone_dps)

    # ── Reactive hardener phasing suggestion ─────────────────────────────
    # Computed whenever a reactive hardener is fitted and running, so the UI
    # can offer a one-click "match incoming damage" button without having to
    # know the module's pool size. Only one may be fitted per ship
    # (maxGroupFitted = 1), so the first match is the only match.
    # `rah_fitted` tracks the MODULE, not the suggestion: a reactive hardener
    # whose pool has somehow gone to zero is still fitted, and a UI that keys
    # its phasing controls off this flag should still show them.
    rah_fitted = False
    rah_suggested_phasing = None
    for item, mod_attrs in enriched_fitted:
        if item["type_id"] not in rah_type_ids:
            continue
        rah_fitted = True
        pool = rah_total_resist_points(
            [mod_attrs.get(attr_id, 1.0) for attr_id in ARMOR_RESONANCE_ATTRS]
        )
        if pool > 0:
            rah_suggested_phasing = suggest_rah_phasing(dmg_prof, pool)
        break

    return {
        "cpu_used": round(cpu_used, 1),
        "cpu_total": round(mattr(SHIP_STAT_ATTRS["cpu_output"]), 1),
        "pg_used": round(pg_used, 1),
        "pg_total": round(mattr(SHIP_STAT_ATTRS["pg_output"]), 1),
        "calibration_used": round(calibration_used),
        "calibration_total": round(mattr(SHIP_STAT_ATTRS["calibration_output"])),
        "turrets_used": turrets_used,
        "turrets_total": int(mattr(SHIP_STAT_ATTRS["turret_slots"])),
        "launchers_used": launchers_used,
        "launchers_total": int(mattr(SHIP_STAT_ATTRS["launcher_slots"])),
        "drone_bw_used": round(drone_bw_used, 1),
        "drone_bw_total": round(mattr(SHIP_STAT_ATTRS["drone_bandwidth"]), 1),
        "drone_bay_used": round(drone_bay_used, 1),
        "drone_bay_total": round(mattr(SHIP_STAT_ATTRS["drone_capacity"]), 1),
        "hull_hp": round(hull_hp),
        "armor_hp": round(armor_hp),
        "shield_hp": round(shield_hp),
        "shield_ehp": round(shield_ehp),
        "armor_ehp": round(armor_ehp),
        "hull_ehp": round(hull_ehp),
        "total_ehp": round(total_ehp),
        "max_velocity": round(mattr(SHIP_STAT_ATTRS["max_velocity"]), 1),
        "mass": round(mattr(SHIP_STAT_ATTRS["mass"])),
        "inertia": round(mattr(SHIP_STAT_ATTRS["inertia"]), 4),
        "align_time": round(align_time, 1),
        "warp_speed_au_s": round(
            mattr(SHIP_STAT_ATTRS["base_warp_speed"])
            * mattr(SHIP_STAT_ATTRS["warp_speed_multiplier"]),
            2,
        ),
        "capacitor": round(mattr(SHIP_STAT_ATTRS["capacitor"]), 1),
        "cap_recharge": round(mattr(SHIP_STAT_ATTRS["cap_recharge"]) / 1000, 1) if mattr(SHIP_STAT_ATTRS["cap_recharge"]) else 0,
        "max_target_range": round(mattr(SHIP_STAT_ATTRS["max_target_range"]) / 1000, 1),
        "max_locked_targets": int(mattr(SHIP_STAT_ATTRS["max_locked_targets"])),
        "scan_resolution": round(mattr(SHIP_STAT_ATTRS["scan_resolution"]), 1),
        "sig_radius": round(mattr(SHIP_STAT_ATTRS["sig_radius"]), 1),
        "lock_time": round(_calc_lock_time(
            mattr(SHIP_STAT_ATTRS["scan_resolution"]),
            mattr(SHIP_STAT_ATTRS["sig_radius"]),
        ), 1),
        "cargo_capacity": round(mattr(SHIP_STAT_ATTRS["cargo_capacity"]), 1),
        "hi_slots": int(mattr(SHIP_STAT_ATTRS["hi_slots"])),
        "med_slots": int(mattr(SHIP_STAT_ATTRS["med_slots"])),
        "low_slots": int(mattr(SHIP_STAT_ATTRS["low_slots"])),
        "rig_slots": int(mattr(SHIP_STAT_ATTRS["rig_slots"])),
        "shield_em_resist": _resonance_to_resist(shield_em_res),
        "shield_therm_resist": _resonance_to_resist(shield_therm_res),
        "shield_kin_resist": _resonance_to_resist(shield_kin_res),
        "shield_expl_resist": _resonance_to_resist(shield_expl_res),
        "armor_em_resist": _resonance_to_resist(armor_em_res),
        "armor_therm_resist": _resonance_to_resist(armor_therm_res),
        "armor_kin_resist": _resonance_to_resist(armor_kin_res),
        "armor_expl_resist": _resonance_to_resist(armor_expl_res),
        "hull_em_resist": _resonance_to_resist(hull_em_res),
        "hull_therm_resist": _resonance_to_resist(hull_therm_res),
        "hull_kin_resist": _resonance_to_resist(hull_kin_res),
        "hull_expl_resist": _resonance_to_resist(hull_expl_res),
        # Cap stability
        "cap_stable": cap_stable,
        "cap_stable_pct": round(cap_stable_pct, 1),
        "cap_drain_rate": round(total_cap_drain, 1),
        "peak_cap_recharge": round(peak_cap_recharge, 1),
        "cap_lasts_s": round(cap_lasts_s),
        # Shield passive recharge
        "peak_shield_recharge": round(peak_shield_recharge, 1),
        # EHP multiplier per layer (1 / avg_resonance) for HP→EHP conversion
        "shield_ehp_mult": round(shield_ehp / shield_hp, 2) if shield_hp > 0 else 1.0,
        "armor_ehp_mult": round(armor_ehp / armor_hp, 2) if armor_hp > 0 else 1.0,
        "hull_ehp_mult": round(hull_ehp / hull_hp, 2) if hull_hp > 0 else 1.0,
        # Active tank
        "armor_rep_rate": round(armor_rep_rate, 1),
        "shield_rep_rate": round(shield_rep_rate, 1),
        # DPS
        "weapon_dps": round(weapon_dps, 1),
        "drone_dps": round(drone_dps, 1),
        "total_dps": round(total_dps, 1),
        "weapon_volley": round(weapon_volley),
        "weapon_dps_max_spool": round(weapon_dps_max_spool, 1),
        # Per-turret application, after any loaded script. 0 when nothing is
        # firing or the weapon has no such attribute (launchers).
        "weapon_tracking": round(weapon_tracking, 4),
        "weapon_optimal_range": round(weapon_optimal_range, 1),
        "total_dps_max_spool": round(total_dps_max_spool, 1),
        "spool_time_s": spool_time_s,
        # Resist-weighted effective DPS against target_resist_profile.
        # When the profile is "uniform" (no resists) these equal the raw
        # DPS values above; the template only shows them when they differ.
        "effective_weapon_dps": round(effective_weapon_dps, 1),
        "effective_drone_dps": round(effective_drone_dps, 1),
        "effective_total_dps": round(effective_total_dps, 1),
        "effective_total_dps_max_spool": round(effective_total_dps_max_spool, 1),
        "target_resist_profile": target_resist_profile,
        # Reactive Armor Hardener: None unless one is fitted and online. The
        # values are resist percentage points per damage type, in the shape
        # the per-item `resist_phasing` request field expects, so the UI can
        # send back what it was given.
        "rah_fitted": rah_fitted,
        "rah_suggested_phasing": rah_suggested_phasing,
        # Non-fatal problems with the request (a phasing distribution that
        # had to be normalised, a resist_phasing on a module that is not a
        # reactive hardener). Surfaced rather than raised: a fitting tool that
        # 400s on a rounding error is worse than one that says what it did.
        "warnings": warnings,
    }


def _calc_align_time(inertia: float, mass: float) -> float:
    if not inertia or not mass:
        return 0
    return -math.log(0.25) * inertia * mass / 1_000_000


def _calc_lock_time(scan_resolution: float, target_sig_radius: float) -> float:
    """Time to lock a target with the given sig radius.

    Formula: 40000 / (scanResolution * asinh(sigRadius)^2), capped at 30 min.
    Uses own sig radius as default target (self-lock estimate).
    """
    if scan_resolution <= 0 or target_sig_radius <= 0:
        return 0
    asinh_sig = math.asinh(target_sig_radius)
    if asinh_sig <= 0:
        return 0
    lock_time = 40000.0 / (scan_resolution * asinh_sig ** 2)
    return min(lock_time, 1800.0)  # cap at 30 minutes


def _resonance_to_resist(resonance: float) -> float:
    """Convert damage resonance (0-1) to resist percentage (0-100)."""
    return round((1.0 - resonance) * 100, 1)


def _calc_ehp(
    hp: float, em_res: float, therm_res: float, kin_res: float, expl_res: float,
    damage_profile: tuple[float, float, float, float] = (0.25, 0.25, 0.25, 0.25),
) -> float:
    """Calculate EHP for one layer against a damage profile.

    Resonance is 0-1 where 0 = 100% resist, 1 = 0% resist.
    EHP = HP / weighted_avg(resonances, damage_profile)

    damage_profile is (em, therm, kin, expl) fractions summing to 1.0.
    """
    if hp <= 0:
        return 0
    em_w, th_w, ki_w, ex_w = damage_profile
    weighted_res = em_res * em_w + therm_res * th_w + kin_res * ki_w + expl_res * ex_w
    if weighted_res <= 0:
        return hp * 1000  # near-infinite EHP at 100% resist
    return hp / weighted_res


def _calc_peak_recharge(capacity: float, recharge_time_s: float) -> float:
    """Peak recharge rate at 25% capacity.

    Formula: 2.5 * capacity / recharge_time
    Source: Pyfa eos/saveddata/fit.py, docs/fitting-mechanics.md
    """
    if capacity <= 0 or recharge_time_s <= 0:
        return 0
    return 2.5 * capacity / recharge_time_s


def _find_cap_stable_pct(capacity: float, recharge_s: float, drain_rate: float) -> float:
    """Find the equilibrium cap percentage where recharge = drain.

    Cap recharge: dC/dt = (10 * C_max / tau) * (sqrt(C/C_max) - C/C_max)
    At equilibrium: recharge_rate = drain_rate
    Solve: (10 * C_max / tau) * (sqrt(p) - p) = drain_rate
    where p = C/C_max (fraction)

    Rearranging: sqrt(p) - p = drain_rate * tau / (10 * C_max)
    Let k = drain_rate * tau / (10 * C_max)
    sqrt(p) - p = k  →  sqrt(p) = k + p  →  p = (k + p)^2

    Binary search for p in [0, 0.25] where peak is at p=0.25.
    """
    if capacity <= 0 or recharge_s <= 0:
        return 100.0
    k = drain_rate * recharge_s / (10 * capacity)
    # Max possible k is 0.25 (at peak recharge). If k > 0.25, not stable.
    if k > 0.25:
        return 0.0

    # Binary search
    lo, hi = 0.0, 1.0
    for _ in range(50):
        mid = (lo + hi) / 2
        p_sqrt = math.sqrt(mid)
        recharge_at_mid = p_sqrt - mid  # normalized recharge
        if recharge_at_mid > k:
            lo = mid  # can sustain at higher cap
        else:
            hi = mid
    return round(lo * 100, 1)
