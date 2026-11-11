"""What a Hub Scan covers: a list kind and reference -> market type ids.

  category    ref = market group id: every published type in the node's subtree
  blueprints  ref = 0: products of the user's and their corporations' blueprints
  watchlist   ref = watchlist id (must belong to the user)

At most CAP items, first by name; `total` is the uncapped count so the page
can say "300 of 412 scanned"."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import scopes as perms, status as perm_status
from app.db.models import Character
from app.db.sde_models import SDEBlueprintInfo, SDEType
from app.industry import owned_blueprints
from app.market import watchlists
from app.routes.industry_jobs import NPC_CORP_CEILING
from app.sde import lookup as sde

logger = logging.getLogger(__name__)

CAP = 300
KINDS = ("category", "blueprints", "watchlist")


class ListNotFound(Exception):
    """Unknown kind, unknown category, or a watchlist that is not the user's."""


@dataclass
class Expanded:
    label: str
    type_ids: list[int]                 # at most CAP, ordered by name
    names: dict[int, str]
    total: int
    notes: list[str] = field(default_factory=list)


async def _market_types(db: AsyncSession, type_ids) -> list[tuple[int, str]]:
    """(type_id, name) ordered by name for the published market types among `type_ids`."""
    ids = list(set(type_ids))
    out: list[tuple[int, str]] = []
    for i in range(0, len(ids), 500):
        out += (await db.execute(
            select(SDEType.type_id, SDEType.type_name).where(
                SDEType.type_id.in_(ids[i:i + 500]),
                SDEType.published.is_(True),
                SDEType.market_group_id.is_not(None))
        )).all()
    return sorted(((t, n) for t, n in out), key=lambda r: ((r[1] or "").lower(), r[0]))


def _expanded(label: str, rows: list[tuple[int, str]], notes: list[str]) -> Expanded:
    kept = rows[:CAP]
    return Expanded(label, [t for t, _ in kept], {t: n for t, n in kept}, len(rows), notes)


async def _category(db: AsyncSession, group_id: int) -> Expanded:
    path = await sde.get_market_group_path(db, group_id)
    if not path:
        raise ListNotFound()
    groups = await sde.get_market_group_descendants(db, group_id)
    rows = (await db.execute(
        select(SDEType.type_id, SDEType.type_name).where(
            SDEType.market_group_id.in_(groups), SDEType.published.is_(True))
    )).all()
    rows = sorted(((t, n) for t, n in rows), key=lambda r: ((r[1] or "").lower(), r[0]))
    label = " › ".join(p["market_group_name"] for p in path)
    return _expanded(label, rows, [])


@dataclass
class _Snap:
    """What the blueprint walk needs from a Character, copied out up front so a
    rollback (which expires every ORM row) can't break later reads."""
    character_id: int
    character_name: str
    scopes: str
    corporation_id: int | None
    corporation_name: str | None
    char: Character
    alive: bool = True


def _bp_cap_note(name: str, raw) -> list[str]:
    if raw is not None and len(raw) >= owned_blueprints.PAGE_CAP:
        return [f"{name}'s blueprint list may be incomplete (over 10,000 blueprints)."]
    return []


async def blueprint_type_ids(db: AsyncSession, user_id: int) -> tuple[set[int], list[str]]:
    """(blueprint type ids held by the user's characters and corporations, notes)."""
    chars = (await db.execute(
        select(Character).where(Character.user_id == user_id)
    )).scalars().all()
    snaps = [_Snap(c.character_id, c.character_name, c.scopes or "",
                   c.corporation_id, c.corporation_name, c) for c in chars]
    found: set[int] = set()
    notes: list[str] = []
    try:
        dead = await perm_status.dead_token_ids(db, [s.character_id for s in snaps])
    except Exception as exc:
        logger.info("hub scan: dead-token lookup failed: %s", exc)
        dead = set()
    for i, snap in enumerate(snaps):
        if not snap.alive:
            continue
        if perms.BLUEPRINTS not in snap.scopes:
            notes.append(f"{snap.character_name} hasn't shared blueprints with Vigilant.")
            continue
        if snap.character_id in dead:
            notes.append(f"{snap.character_name} needs to sign in again, so "
                         "their blueprints aren't counted.")
            continue
        try:
            raw, _token = await owned_blueprints.character_rows(db, snap.char)
        except Exception:
            logger.warning("hub scan: blueprints for %s failed", snap.character_id,
                           exc_info=True)
            notes.append(f"Couldn't load {snap.character_name}'s blueprints.")
            try:
                await db.rollback()
            except Exception:
                logger.warning("hub scan: rollback failed", exc_info=True)
            # A rollback expires every loaded row. Reload the ones still needed
            # as ORM objects; one that can't be reloaded is dropped, not fatal.
            for j, other in enumerate(snaps):
                if not other.alive:
                    continue
                try:
                    await db.refresh(other.char)
                except Exception:
                    logger.warning("hub scan: reloading %s failed", other.character_id,
                                   exc_info=True)
                    other.alive = False
                    if j > i:
                        notes.append(f"Couldn't load {other.character_name}'s blueprints.")
            continue
        notes += _bp_cap_note(snap.character_name, raw)
        found.update(int(r["type_id"]) for r in raw or [] if r.get("type_id"))
    corps: dict[int, list[_Snap]] = {}
    for s in snaps:
        if s.corporation_id and s.corporation_id >= NPC_CORP_CEILING:
            corps.setdefault(s.corporation_id, []).append(s)
    for corp_id, members in sorted(corps.items()):
        name = members[0].corporation_name or f"Corp {corp_id}"
        holders = [m.char for m in members
                   if m.alive and perms.CORP_BLUEPRINTS in m.scopes]
        raw = await owned_blueprints.corporation_rows(db, corp_id, holders) if holders else None
        if raw is None:
            notes.append(f"{name}'s blueprints couldn't be read, so they aren't included.")
            continue
        notes += _bp_cap_note(name, raw)
        found.update(int(r["type_id"]) for r in raw if r.get("type_id"))
    return found, notes


async def _blueprints(db: AsyncSession, user_id: int) -> Expanded:
    bp_ids, notes = await blueprint_type_ids(db, user_id)
    products: set[int] = set()
    ids = list(bp_ids)
    for i in range(0, len(ids), 500):
        products.update(p for (p,) in (await db.execute(
            select(SDEBlueprintInfo.product_type_id)
            .where(SDEBlueprintInfo.blueprint_type_id.in_(ids[i:i + 500]))
        )).all() if p)
    return _expanded("My blueprints", await _market_types(db, products), notes)


async def _watchlist(db: AsyncSession, user_id: int, watchlist_id: int) -> Expanded:
    wl = await watchlists.get(db, user_id, watchlist_id)
    if wl is None:
        raise ListNotFound()
    return _expanded(wl.name, await _market_types(db, await watchlists.item_ids(db, wl.id)), [])


async def expand(db: AsyncSession, user_id: int, kind: str, ref: int) -> Expanded:
    if kind == "category":
        return await _category(db, ref)
    if kind == "blueprints":
        return await _blueprints(db, user_id)
    if kind == "watchlist":
        return await _watchlist(db, user_id, ref)
    raise ListNotFound()


async def _category_count(db: AsyncSession, group_id: int) -> int:
    """`_category(...).total` without loading the rows: a COUNT over the same
    subtree and the same filters."""
    if not await sde.get_market_group_path(db, group_id):
        raise ListNotFound()
    groups = await sde.get_market_group_descendants(db, group_id)
    return (await db.execute(
        select(func.count()).select_from(SDEType).where(
            SDEType.market_group_id.in_(groups), SDEType.published.is_(True))
    )).scalar() or 0


async def count(db: AsyncSession, user_id: int, kind: str, ref: int) -> int | None:
    """Items in the list before the cap, or None for blueprints (only known
    after asking ESI, which a scan does). Raises ListNotFound as `expand`."""
    if kind == "blueprints":
        return None
    if kind == "category":
        return await _category_count(db, ref)
    return (await expand(db, user_id, kind, ref)).total
