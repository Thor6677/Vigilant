"""Module state (offline / online / active / overheated) for the fitting tool.

Two halves, both shared by the engine and the fitting page:

* ``get_module_caps`` reads what each module TYPE can do from the SDE: whether
  it can be activated, whether it can be overheated, whether pyfa rests it at
  ONLINE rather than ACTIVE, and its active-per-group limit.
* ``resolve_states`` is pure. Given the fitted items and those caps it returns
  the state of every item, plus warnings. The page mirrors it in JS and both
  are driven by tests/fixtures/module_state_cases.json.

An item's ``active`` key is optional: only the literals true and false count.
Anything else (missing, null, 0, "yes") means "no choice made", which falls
back to the type's default. A valid ``overheated`` is OVERHEATED whatever
``active`` says, since heat only exists on a running module.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.sde_models import (
    SDEEffect, SDEType, SDETypeDogmaAttribute, SDETypeEffect,
)
from app.fitting.constants import (
    ACTIVATION_EFFECT_CATS, ATTR_MAX_GROUP_ACTIVE, DEFAULT_ONLINE_EFFECTS,
    EFFECT_CAT_OVERLOAD, EFFECT_ONLINE, NON_RACK_SLOTS,
    STATE_ACTIVE, STATE_OFFLINE, STATE_ONLINE, STATE_OVERHEATED,
)

# What a type with no SDE rows can do: nothing beyond being online.
_NO_CAPS = {
    "can_activate": False, "can_overheat": False, "default_active": False,
    "group_id": None, "max_group_active": 0,
}


async def get_module_caps(db: AsyncSession, type_ids) -> dict[int, dict]:
    """Per-type state capabilities, keyed by every requested type id.

    Each value has ``can_activate`` (an effect in an activation category other
    than the online effect), ``can_overheat`` (a category-5 effect),
    ``default_active`` (can activate and has no DEFAULT_ONLINE_EFFECTS effect),
    ``group_id`` and ``max_group_active`` (attribute 763 read raw; 0 means no
    limit, as does absence).
    """
    ids = sorted({int(t) for t in type_ids})
    caps = {tid: dict(_NO_CAPS) for tid in ids}
    if not ids:
        return caps

    effects_by_type: dict[int, list[tuple[int, int]]] = {tid: [] for tid in ids}
    result = await db.execute(
        select(SDETypeEffect.type_id, SDETypeEffect.effect_id,
               SDEEffect.effect_category)
        .join(SDEEffect, SDEEffect.effect_id == SDETypeEffect.effect_id)
        .where(SDETypeEffect.type_id.in_(ids))
    )
    for type_id, effect_id, category in result.fetchall():
        effects_by_type[type_id].append((effect_id, category))

    for tid, effects in effects_by_type.items():
        can_activate = any(
            cat in ACTIVATION_EFFECT_CATS and eid != EFFECT_ONLINE
            for eid, cat in effects
        )
        caps[tid]["can_activate"] = can_activate
        caps[tid]["can_overheat"] = any(
            cat == EFFECT_CAT_OVERLOAD for _, cat in effects
        )
        caps[tid]["default_active"] = can_activate and not any(
            eid in DEFAULT_ONLINE_EFFECTS for eid, _ in effects
        )

    result = await db.execute(
        select(SDEType.type_id, SDEType.group_id).where(SDEType.type_id.in_(ids))
    )
    for type_id, group_id in result.fetchall():
        caps[type_id]["group_id"] = group_id

    result = await db.execute(
        select(SDETypeDogmaAttribute.type_id, SDETypeDogmaAttribute.value)
        .where(SDETypeDogmaAttribute.type_id.in_(ids))
        .where(SDETypeDogmaAttribute.attribute_id == ATTR_MAX_GROUP_ACTIVE)
    )
    for type_id, value in result.fetchall():
        caps[type_id]["max_group_active"] = int(value or 0)

    return caps


def resolve_states(items, caps) -> tuple[list, list[str]]:
    """The state of every item, in item order, plus warnings.

    ``caps`` maps type_id to a get_module_caps entry (a type missing from it
    can only be online). Returns ``(states, warnings)``: one state per item,
    which is ``None`` for drones, cargo and fighters (no module state),
    otherwise STATE_OFFLINE / ONLINE / ACTIVE / OVERHEATED. Rigs are always
    ONLINE, as the engine ignores their ``online`` flag.

    Precedence per module: ``online`` false -> OFFLINE; cannot activate ->
    ONLINE; valid ``overheated`` (needs can_overheat) -> OVERHEATED; explicit
    ``active`` -> ACTIVE or ONLINE; else the type default. Then each group
    with ``max_group_active`` > 0 keeps its first N running modules, explicit
    choices ahead of defaulted ones and item order within each; the rest drop
    to ONLINE and one warning per group says so.
    """
    states: list = []
    explicit_run: dict = {}    # group_id -> positions running by choice
    default_run: dict = {}     # group_id -> positions running by default
    limits: dict = {}

    for pos, item in enumerate(items):
        if item.get("slot") in NON_RACK_SLOTS:
            states.append(None)
            continue
        if item.get("slot") == "rig":
            states.append(STATE_ONLINE)
            continue
        if not item.get("online", True):
            states.append(STATE_OFFLINE)
            continue
        cap = caps.get(item.get("type_id")) or _NO_CAPS
        if not cap["can_activate"]:
            states.append(STATE_ONLINE)
            continue

        active = item.get("active")
        if item.get("overheated") and cap["can_overheat"]:
            state, by_choice = STATE_OVERHEATED, True
        elif active is True or active is False:
            state = STATE_ACTIVE if active else STATE_ONLINE
            by_choice = True
        else:
            state = STATE_ACTIVE if cap["default_active"] else STATE_ONLINE
            by_choice = False
        states.append(state)

        group, limit = cap["group_id"], cap["max_group_active"]
        if state >= STATE_ACTIVE and limit > 0 and group is not None:
            limits[group] = limit
            (explicit_run if by_choice else default_run).setdefault(
                group, []).append(pos)

    warnings: list[str] = []
    for group, limit in limits.items():
        ranked = explicit_run.get(group, []) + default_run.get(group, [])
        extra = ranked[limit:]
        for pos in extra:
            states[pos] = STATE_ONLINE
        if extra:
            warnings.append(
                f"At most {limit} module(s) of group {group} can be active at "
                f"once; {len(extra)} set to online."
            )
    return states, warnings
