"""Can-fly check for saved fittings (T-072): does a character have the
skills to fly each of a user's saved fits, and if not, what's missing.

Mirrors the shape of the T-069 DPS cache (app/routes/fitting.py's
saved_fittings_dps / dps_cache_key): a fit's skill *requirements* rarely
change, so they're cached on the row (skill_reqs_json / skill_reqs_key) and
only recomputed when the cache key no longer matches. The key hashes ship +
items + the SDE version stamp — no FITTING_ENGINE_VERSION, since nothing
here runs the dogma modifier / stacking-penalty pipeline that constant
guards; a skill requirement is a flat row in SDETypeSkillReq, not a
computed number.

This module never touches ESI and never decides how a character's trained
levels were obtained — `can_fly_summary` takes `levels` as a plain
{skill_id: level} dict so the same code serves both the live overview
partial (skills fetched fresh, see app/routes/character_detail.py) and a
future dashboard stream reading levels off synced data.
"""
import hashlib
import json
import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UserFitting, UserFittingFolder
from app.db.sde_models import SDETypeSkillReq
from app.sde import lookup as sde

logger = logging.getLogger(__name__)

# Every stale fit is refreshed within one can_fly_summary() call — unlike
# the DPS cache, this is not deferred across polls. "Flies N of M saved
# fits" would silently undercount for as many fits as were left stale, and
# a later dashboard stream reads this same summary as a one-shot count, not
# something that improves on a follow-up poll. The batching here is only to
# bound each individual SDETypeSkillReq query's IN-list and commit size —
# a single indexed lookup per chunk, not per fit.
_REQ_BATCH = 25


def _fit_type_ids(ship_type_id: int | None, items_json: str | None) -> set[int]:
    """Every type_id whose skill requirements matter for "can I fly this":
    the ship, every module, every charge and every drone. Cargo is skipped
    — a cargo item's type_id has no bearing on flying the fit."""
    try:
        items = json.loads(items_json) if items_json else []
    except Exception:
        items = []

    type_ids: set[int] = set()
    if ship_type_id:
        type_ids.add(int(ship_type_id))
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("slot") == "cargo":
            continue
        tid = item.get("type_id")
        if tid:
            type_ids.add(int(tid))
        charge_id = item.get("charge_type_id")
        if charge_id:
            type_ids.add(int(charge_id))
    return type_ids


def skill_reqs_cache_key(ship_type_id: int | None, items_json: str | None, sde_stamp: str) -> str:
    """Cache key for one fit's skill-requirement set: ship + items + the SDE
    version stamp (app.sde.lookup.get_sde_version_stamp), hashed together.
    `items_json` is hashed as its already-stored JSON text, matching
    app.fitting.engine.dps_cache_key's reasoning — saving a fit always
    rewrites it, so any real change to what's fitted changes this string.
    No FITTING_ENGINE_VERSION: see the module docstring.
    """
    raw = f"{ship_type_id}|{items_json or '[]'}|{sde_stamp}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _batch_requirements(
    db: AsyncSession, fits: list[tuple[int, int | None, str | None]]
) -> dict[int, dict[int, int]]:
    """Compute skill requirements for a batch of fits with exactly one
    SDETypeSkillReq query (the union of every fit's type_ids in the batch),
    then reduce per fit. `fits` is [(fit_id, ship_type_id, items_json), ...].
    Returns {fit_id: {skill_id: max_required_level}}.
    """
    per_fit_types: dict[int, set[int]] = {}
    all_types: set[int] = set()
    for fit_id, ship_type_id, items_json in fits:
        types = _fit_type_ids(ship_type_id, items_json)
        per_fit_types[fit_id] = types
        all_types |= types

    by_type: dict[int, list[tuple[int, int]]] = {}
    if all_types:
        rows = await db.execute(
            select(
                SDETypeSkillReq.type_id,
                SDETypeSkillReq.skill_type_id,
                SDETypeSkillReq.required_level,
            ).where(SDETypeSkillReq.type_id.in_(all_types))
        )
        for type_id, skill_id, level in rows.all():
            by_type.setdefault(int(type_id), []).append((int(skill_id), int(level)))

    out: dict[int, dict[int, int]] = {}
    for fit_id, types in per_fit_types.items():
        reqs: dict[int, int] = {}
        for t in types:
            for skill_id, level in by_type.get(t, []):
                if level > reqs.get(skill_id, 0):
                    reqs[skill_id] = level
        out[fit_id] = reqs
    return out


async def fit_skill_requirements(db: AsyncSession, fit) -> dict[int, int]:
    """Skill requirements for one saved fit: {skill_id: max_required_level}
    across the ship, every module, charge and drone (cargo skipped). `fit`
    only needs `.ship_type_id` and `.items_json` — a UserFitting instance or
    a column-select Row both work.

    Only direct requirements are read from SDETypeSkillReq — a module
    requiring Skill X at level N implies X's own prerequisites, so there is
    nothing to recurse into (matches fit_skill_check in
    app/routes/fitting.py).
    """
    result = await _batch_requirements(db, [(0, fit.ship_type_id, fit.items_json)])
    return result.get(0, {})


def evaluate_fit(levels: dict[int, int], reqs: dict[int, int]) -> dict:
    """Pure: compare trained `levels` against a fit's `reqs`.

    Returns {"can_fly": bool, "missing": [{"skill_id", "have", "need"}, ...]}
    with `missing` sorted by the biggest gap first (ties broken by the
    higher required level, then skill_id, for a deterministic order).
    """
    missing = []
    for skill_id, need in reqs.items():
        have = int(levels.get(skill_id, 0))
        if have < need:
            missing.append({"skill_id": skill_id, "have": have, "need": need})
    missing.sort(key=lambda m: (-(m["need"] - m["have"]), -m["need"], m["skill_id"]))
    return {"can_fly": not missing, "missing": missing}


async def can_fly_summary(db: AsyncSession, user_id: int, levels: dict[int, int]) -> dict:
    """Evaluate every fit `user_id` has saved against `levels`.

    Returns {"total": int, "can_fly": int, "fits": [...]}, where each fit
    dict is {"id", "name", "ship_type_id", "ship_name", "folder",
    "can_fly", "missing"} — "missing" in the same shape evaluate_fit
    returns. Fills any stale requirement caches (bounded per-chunk queries,
    see _REQ_BATCH) before evaluating, so the caller never sees a partial
    or undercounted result. Independent of how `levels` was obtained.
    """
    sde_stamp = await sde.get_sde_version_stamp(db)

    rows = (await db.execute(
        select(
            UserFitting.id, UserFitting.name, UserFitting.ship_type_id,
            UserFitting.items_json, UserFitting.folder_id,
            UserFitting.skill_reqs_json, UserFitting.skill_reqs_key,
        ).where(UserFitting.user_id == user_id)
    )).all()

    if not rows:
        return {"total": 0, "can_fly": 0, "fits": []}

    reqs_by_fit: dict[int, dict[int, int]] = {}
    stale: list[tuple[int, int | None, str, str]] = []
    for r in rows:
        items_json = r.items_json or "[]"
        key = skill_reqs_cache_key(r.ship_type_id, items_json, sde_stamp)
        if r.skill_reqs_key == key and r.skill_reqs_json is not None:
            try:
                raw = json.loads(r.skill_reqs_json)
                reqs_by_fit[r.id] = {int(k): int(v) for k, v in raw.items()}
                continue
            except Exception:
                pass
        stale.append((r.id, r.ship_type_id, items_json, key))

    for i in range(0, len(stale), _REQ_BATCH):
        chunk = stale[i:i + _REQ_BATCH]
        computed = await _batch_requirements(db, [(fid, sid, ij) for fid, sid, ij, _ in chunk])
        for fid, _sid, _ij, key in chunk:
            reqs = computed.get(fid, {})
            reqs_by_fit[fid] = reqs
            await db.execute(
                update(UserFitting).where(UserFitting.id == fid)
                .values(skill_reqs_json=json.dumps(reqs), skill_reqs_key=key)
            )
        await db.commit()

    folder_ids = {r.folder_id for r in rows if r.folder_id}
    folder_names: dict[int, str] = {}
    if folder_ids:
        frows = await db.execute(
            select(UserFittingFolder.id, UserFittingFolder.name)
            .where(UserFittingFolder.id.in_(folder_ids))
        )
        folder_names = {fid: name for fid, name in frows.all()}

    ship_type_ids = {r.ship_type_id for r in rows if r.ship_type_id}
    ship_names = await sde.type_ids_to_names(db, list(ship_type_ids)) if ship_type_ids else {}

    fits_out = []
    can_fly_n = 0
    for r in rows:
        result = evaluate_fit(levels, reqs_by_fit.get(r.id, {}))
        if result["can_fly"]:
            can_fly_n += 1
        fits_out.append({
            "id": r.id,
            "name": r.name,
            "ship_type_id": r.ship_type_id,
            "ship_name": ship_names.get(r.ship_type_id, f"Ship {r.ship_type_id}"),
            "folder": folder_names.get(r.folder_id, "") if r.folder_id else "",
            "can_fly": result["can_fly"],
            "missing": result["missing"],
        })

    fits_out.sort(key=lambda f: (not f["can_fly"], f["ship_name"].lower(), f["name"].lower()))
    return {"total": len(rows), "can_fly": can_fly_n, "fits": fits_out}
