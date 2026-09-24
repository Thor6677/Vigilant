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
"""
from sqlalchemy.ext.asyncio import AsyncSession

# Dogma attribute IDs, looked up by name in the SDE's dogmaAttributes.
ATTR_BOOSTERNESS = 1087            # boosterness — the booster's slot
ATTR_BOOSTER_EFFECT_CHANCE = (     # boosterEffectChance1..5
    1089, 1090, 1091, 1092, 1093,
)


async def get_booster_info(db: AsyncSession, type_ids: list[int]) -> dict[int, dict]:
    """Slot and switchable side effects for each booster type ID.

    Unknown or non-booster type IDs are simply absent from the result.
    """
    # Contract stub: the engine side of T-049 fills this in.
    return {}


async def apply_booster_bonuses(
    db: AsyncSession,
    ship_attrs: dict,
    module_attrs_map: dict,
    charge_attrs_map: dict,
    boosters: list[dict],
) -> None:
    """Apply each booster's primary effects, plus its switched-on side effects,
    to the ship, modules and charges in place."""
    # Contract stub: the engine side of T-049 fills this in.
    return None
