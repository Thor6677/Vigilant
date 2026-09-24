"""Combat boosters in the fitting engine (T-049).

This module is the seam between the engine and the fitting routes/UI. The
shapes below are a contract both sides build against; change them together.

Engine input — what ``calculate_fitting_stats(..., boosters=...)`` takes::

    [{"type_id": int, "side_effects": [effect_id, ...]}, ...]

``side_effects`` lists the side-effect dogma effects the user switched ON.
A booster's primary effects always apply; its side effects are off unless
listed, because in game each one only fires on a per-injection roll (the
booster's ``boosterEffectChanceN`` attribute). An effect ID that is not one
of that booster's side effects is ignored, so a crafted request can never
apply a primary effect twice.

Saved loadout — ``UserFitting.boosters_json``, mirroring ``implants_json``::

    {"<slot>": {"type_id": int, "name": str, "side_effects": [effect_id, ...]}}

keyed by the booster's ``boosterness`` value. Two boosters with the same
boosterness cannot be active together, so the slot is the natural key. Unlike
implantness (1-10), boosterness is not a small fixed range: the SDE uses
values from 1 into the hundreds (accelerators and event boosters each get a
slot of their own), so the UI lists active boosters rather than drawing a
grid of slots.

UI lookup — ``get_booster_info`` returns, per type ID::

    {"type_id": int, "name": str, "slot": int,
     "side_effects": [{"effect_id": int, "label": str, "chance": float | None}]}

``chance`` is the in-game roll probability (0-1) read from the booster's
``boosterEffectChanceN`` attribute, for display only.

A dogma effect is a side effect when the SDE gives it a
``fittingUsageChanceAttributeID``: twelve effects carry one, all
``booster*Penalty``, spread over the 24 classic combat boosters.

How the engine applies a booster
--------------------------------
A booster is a character-owned item, exactly like an implant: its modifier
rows use the same four func/domain shapes (ItemModifier on the ship or the
character, Location*Modifier on modules by group or skill,
OwnerRequiredSkillModifier on modules, drones and charges), and every row is
handed to ``engine._apply_character_modifiers`` — the dispatcher implants use
— so the two can never drift. That dispatcher applies nothing with a stacking
penalty; its docstring gives the dogma and Pyfa basis. Verified against the
live SDE (2026-09-24): 453 published boosters, 2,288 modifier rows, all of
those shapes except one Serenity-only ``LocationGroupModifier``/``charID``
row whose source attribute the booster does not even carry
(scripts/audit_booster_modifiers.py re-checks this after an import).

Side-effect labels name the thing hit and by how much ("Shield capacity
-30%", "Missile explosion radius +30%"): the SDE's attribute display names
alone cannot separate the ship's velocity penalty from the missile one, so
the twelve known effects carry a hand-written name.
"""
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.sde_models import (
    SDEDogmaAttribute, SDEEffect, SDEModifier, SDEType, SDETypeDogmaAttribute,
    SDETypeEffect,
)

# Dogma attribute IDs, looked up by name in the SDE's dogmaAttributes.
ATTR_BOOSTERNESS = 1087            # boosterness — the booster's slot
ATTR_BOOSTER_EFFECT_CHANCE = (     # boosterEffectChance1..5
    1089, 1090, 1091, 1092, 1093,
)

# Dogma operators, as app/fitting/engine.py names them. Repeated here because
# engine.py imports this module at load time, so importing engine at module
# level would be circular; only the label formatter needs them.
_OP_PRE_MUL = 0
_OP_MOD_ADD = 2
_OP_POST_MUL = 4
_OP_POST_PERCENT = 6

# Pilot-facing names for the twelve side effects, keyed by effect ID. The SDE
# only offers the target attribute's display name, which cannot tell
# boosterMaxVelocityPenalty (the ship) from boosterMissileVelocityPenalty
# (its missiles): both hit maxVelocity. Anything not listed falls back to the
# attribute's display name, so a side effect CCP adds later still gets a label.
SIDE_EFFECT_NAMES: dict[int, str] = {
    2735: "Armor HP",
    2736: "Armor repair amount",
    2737: "Shield capacity",
    2739: "Turret optimal range",
    2741: "Turret falloff",
    2745: "Capacitor capacity",
    2746: "Max velocity",
    2747: "Turret tracking",
    2748: "Missile velocity",
    2749: "Missile explosion velocity",
    2791: "Missile explosion radius",
    4970: "Shield boost amount",
}

# Per-modifier overrides, keyed (effect_id, modifying_attribute_id) like
# engine._SHIP_MODIFIER_OVERRIDES. One entry: effect 2791
# boosterMissileExplosionCloudPenaltyFixed — the explosion-radius side effect
# of Exile and Mindflood — filters on skill 3452, which is Acceleration
# Control, the afterburner skill. As written it matches no charge, so the
# toggle would change nothing. Pyfa's Effect2791 hand-maps it to Missile
# Launcher Operation (3319), the skill every missile requires; followed here
# so the toggle does what its name says. Live SDE checked 2026-09-24.
_BOOSTER_MODIFIER_OVERRIDES: dict[tuple[int, int], dict] = {
    (2791, 1149): {"filter_value": 3319},
}


def _parse_entries(boosters) -> list[tuple[int, set[int]]]:
    """(type_id, requested side-effect IDs) per usable entry; junk is dropped."""
    out: list[tuple[int, set[int]]] = []
    for entry in boosters or ():
        if not isinstance(entry, dict):
            continue
        try:
            type_id = int(entry.get("type_id"))
        except (TypeError, ValueError):
            continue
        requested: set[int] = set()
        raw = entry.get("side_effects") or ()
        if isinstance(raw, (list, tuple, set, frozenset)):
            for value in raw:
                try:
                    requested.add(int(value))
                except (TypeError, ValueError):
                    continue
        out.append((type_id, requested))
    return out


async def _type_attrs(db: AsyncSession, type_ids) -> dict[int, dict[int, float]]:
    """{type_id: {attribute_id: value}} for the given types."""
    type_ids = list(type_ids)
    if not type_ids:
        return {}
    res = await db.execute(
        select(SDETypeDogmaAttribute.type_id, SDETypeDogmaAttribute.attribute_id,
               SDETypeDogmaAttribute.value)
        .where(SDETypeDogmaAttribute.type_id.in_(type_ids))
    )
    out: dict[int, dict[int, float]] = {}
    for row in res.fetchall():
        out.setdefault(row.type_id, {})[row.attribute_id] = row.value
    return out


async def _effects_of(db: AsyncSession, type_ids) -> list[tuple[int, int, int | None]]:
    """(type_id, effect_id, chance attribute or None) for every effect on the types.

    A non-None chance attribute is what makes an effect a side effect.
    """
    type_ids = list(type_ids)
    if not type_ids:
        return []
    res = await db.execute(
        select(SDETypeEffect.type_id, SDETypeEffect.effect_id,
               SDEEffect.fitting_usage_chance_attribute_id)
        .join(SDEEffect, SDEEffect.effect_id == SDETypeEffect.effect_id)
        .where(SDETypeEffect.type_id.in_(type_ids))
    )
    return [
        (row.type_id, row.effect_id, row.fitting_usage_chance_attribute_id)
        for row in res.fetchall()
    ]


def _format_label(name: str, operator: int, value: float) -> str:
    if operator == _OP_POST_PERCENT:
        return f"{name} {value:+g}%"
    if operator in (_OP_PRE_MUL, _OP_POST_MUL):
        return f"{name} x{value:g}"
    if operator == _OP_MOD_ADD:
        return f"{name} {value:+g}"
    return f"{name} {value:g}"


async def get_booster_info(db: AsyncSession, type_ids: list[int]) -> dict[int, dict]:
    """Slot and switchable side effects for each booster type ID.

    Unknown or non-booster type IDs are simply absent from the result. Four
    bulk queries at most, however many IDs are asked about.
    """
    wanted: set[int] = set()
    for value in type_ids or ():
        try:
            wanted.add(int(value))
        except (TypeError, ValueError):
            continue
    if not wanted:
        return {}

    res = await db.execute(
        select(SDEType.type_id, SDEType.type_name, SDETypeDogmaAttribute.value)
        .join(SDETypeDogmaAttribute, and_(
            SDETypeDogmaAttribute.type_id == SDEType.type_id,
            SDETypeDogmaAttribute.attribute_id == ATTR_BOOSTERNESS,
        ))
        .where(SDEType.type_id.in_(list(wanted)))
    )
    info: dict[int, dict] = {
        row.type_id: {
            "type_id": row.type_id,
            "name": row.type_name,
            "slot": int(row.value),
            "side_effects": [],
        }
        for row in res.fetchall()
    }
    if not info:
        return {}

    pairs = [
        (tid, eid, chance_attr)
        for tid, eid, chance_attr in await _effects_of(db, info)
        if chance_attr is not None
    ]
    if not pairs:
        return info

    effect_ids = {eid for _, eid, _ in pairs}
    res = await db.execute(
        select(SDEModifier.effect_id, SDEModifier.modified_attribute_id,
               SDEModifier.modifying_attribute_id, SDEModifier.operator)
        .where(SDEModifier.effect_id.in_(list(effect_ids)))
    )
    # A side effect's rows all share one target and one source — 2736 and
    # 4970 carry two rows, one per module group or skill — so the first row
    # is enough to label it.
    first_row: dict[int, tuple[int, int, int]] = {}
    for row in res.fetchall():
        first_row.setdefault(
            row.effect_id,
            (row.modified_attribute_id, row.modifying_attribute_id, row.operator),
        )

    attr_names: dict[int, str] = {}
    unnamed_targets = {
        first_row[eid][0] for eid in effect_ids
        if eid not in SIDE_EFFECT_NAMES and eid in first_row
    }
    if unnamed_targets:
        res = await db.execute(
            select(SDEDogmaAttribute.attribute_id, SDEDogmaAttribute.display_name,
                   SDEDogmaAttribute.attribute_name)
            .where(SDEDogmaAttribute.attribute_id.in_(list(unnamed_targets)))
        )
        attr_names = {
            row.attribute_id: (row.display_name or row.attribute_name)
            for row in res.fetchall()
        }

    booster_attrs = await _type_attrs(db, {tid for tid, _, _ in pairs})
    for tid, eid, chance_attr in sorted(pairs):
        attrs = booster_attrs.get(tid, {})
        name = SIDE_EFFECT_NAMES.get(eid)
        row = first_row.get(eid)
        if row is None:
            label = name or f"Effect {eid}"
        else:
            target, source, operator = row
            name = name or attr_names.get(target) or f"Attribute {target}"
            value = attrs.get(source)
            label = name if value is None else _format_label(name, operator, value)
        chance = attrs.get(chance_attr)
        info[tid]["side_effects"].append({
            "effect_id": eid,
            "label": label,
            "chance": float(chance) if chance is not None else None,
        })
    return info


async def apply_booster_bonuses(
    db: AsyncSession,
    ship_attrs: dict,
    module_attrs_map: dict,
    charge_attrs_map: dict,
    boosters: list[dict],
) -> None:
    """Apply each booster's primary effects, plus its switched-on side effects,
    to the ship, modules and charges in place."""
    # engine.py imports this module at load time, so import it here instead.
    from app.fitting.engine import (
        CHARACTER_MODIFIER_DOMAINS, CHARACTER_MODIFIER_FUNCS, CharacterModifier,
        _apply_character_modifiers,
    )

    entries = _parse_entries(boosters)
    if not entries:
        return
    booster_attrs = await _type_attrs(db, {tid for tid, _ in entries})

    # One booster per slot, first entry wins. A type without boosterness is
    # not a booster: a crafted request naming a module or a ship must not
    # push that item's effects through here.
    active: list[tuple[int, set[int]]] = []
    slots_taken: set[int] = set()
    for type_id, requested in entries:
        attrs = booster_attrs.get(type_id)
        if not attrs or ATTR_BOOSTERNESS not in attrs:
            continue
        slot = int(attrs[ATTR_BOOSTERNESS])
        if slot in slots_taken:
            continue
        slots_taken.add(slot)
        active.append((type_id, requested))
    if not active:
        return

    # Primary effects always; side effects only when asked for, and only the
    # booster's own — an effect ID from elsewhere, or a primary effect listed
    # as a side effect, changes nothing.
    requested_by_type = dict(active)
    selected: dict[int, set[int]] = {tid: set() for tid in requested_by_type}
    for tid, eid, chance_attr in await _effects_of(db, selected):
        if chance_attr is None or eid in requested_by_type[tid]:
            selected[tid].add(eid)
    effect_ids = set().union(*selected.values())
    if not effect_ids:
        return

    res = await db.execute(
        select(SDEModifier.effect_id, SDEModifier.modified_attribute_id,
               SDEModifier.modifying_attribute_id, SDEModifier.operator,
               SDEModifier.func, SDEModifier.domain,
               SDEModifier.filter_type, SDEModifier.filter_value)
        .where(SDEModifier.effect_id.in_(list(effect_ids)))
        .where(SDEModifier.domain.in_(CHARACTER_MODIFIER_DOMAINS))
        .where(SDEModifier.func.in_(CHARACTER_MODIFIER_FUNCS))
    )
    rows_by_effect: dict[int, list] = {}
    for row in res.fetchall():
        rows_by_effect.setdefault(row.effect_id, []).append(row)

    modifiers: list[CharacterModifier] = []
    for tid, effects in selected.items():
        attrs = booster_attrs[tid]
        for eid in sorted(effects):
            for row in rows_by_effect.get(eid, ()):
                source_value = attrs.get(row.modifying_attribute_id)
                if not source_value:
                    continue
                override = _BOOSTER_MODIFIER_OVERRIDES.get(
                    (eid, row.modifying_attribute_id), {}
                )
                modifiers.append(CharacterModifier(
                    row.modified_attribute_id, row.modifying_attribute_id,
                    row.operator, row.func, row.domain,
                    override.get("filter_type", row.filter_type),
                    override.get("filter_value", row.filter_value),
                    source_value,
                ))
    if modifiers:
        await _apply_character_modifiers(
            db, ship_attrs, module_attrs_map, charge_attrs_map, modifiers,
        )
