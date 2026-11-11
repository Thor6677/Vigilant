"""What a Hub Scan covers: a list kind and reference -> market type ids.

  category    ref = market group id: every published type in the node's subtree
  blueprints  ref = 0: products of the user's and their corporations' blueprints
  watchlist   ref = watchlist id (must belong to the user)

At most CAP items, first by name; `total` is the uncapped count so the page
can say "300 of 412 scanned"."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
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
    return sorted(((t, n) for t, n in out), key=lambda r: (r[1] or "").lower())


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
    rows = sorted(((t, n) for t, n in rows), key=lambda r: (r[1] or "").lower())
    label = " › ".join(p["market_group_name"] for p in path)
    return _expanded(label, rows, [])


async def blueprint_type_ids(db: AsyncSession, user_id: int) -> tuple[set[int], list[str]]:
    """(blueprint type ids held by the user's characters and corporations, notes)."""
    chars = (await db.execute(
        select(Character).where(Character.user_id == user_id)
    )).scalars().all()
    found: set[int] = set()
    notes: list[str] = []
    try:
        dead = await perm_status.dead_token_ids(db, [c.character_id for c in chars])
    except Exception as exc:
        logger.info("hub scan: dead-token lookup failed: %s", exc)
        dead = set()
    for char in chars:
        if perms.BLUEPRINTS not in (char.scopes or ""):
            notes.append(f"{char.character_name} hasn't shared blueprints with Vigilant.")
            continue
        if char.character_id in dead:
            notes.append(f"{char.character_name} needs to sign in again, so "
                         "their blueprints aren't counted.")
            continue
        char_name, char_id = char.character_name, char.character_id
        try:
            raw, _token = await owned_blueprints.character_rows(db, char)
        except Exception:
            logger.warning("hub scan: blueprints for %s failed", char_id, exc_info=True)
            await db.rollback()
            # A rollback expires every loaded row; reload them so the loop's
            # later attribute reads don't lazy-load outside a greenlet.
            for c in chars:
                await db.refresh(c)
            notes.append(f"Couldn't load {char_name}'s blueprints.")
            continue
        found.update(int(r["type_id"]) for r in raw or [] if r.get("type_id"))
    corps: dict[int, list[Character]] = {}
    for c in chars:
        if c.corporation_id and c.corporation_id >= NPC_CORP_CEILING:
            corps.setdefault(c.corporation_id, []).append(c)
    for corp_id, members in sorted(corps.items()):
        name = members[0].corporation_name or f"Corp {corp_id}"
        holders = [c for c in members if perms.CORP_BLUEPRINTS in (c.scopes or "")]
        raw = await owned_blueprints.corporation_rows(db, corp_id, holders) if holders else None
        if raw is None:
            notes.append(f"{name}'s blueprints couldn't be read, so they aren't included.")
            continue
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


async def count(db: AsyncSession, user_id: int, kind: str, ref: int) -> int | None:
    """Items in the list before the cap, or None for blueprints (only known
    after asking ESI, which a scan does)."""
    if kind == "blueprints":
        return None
    return (await expand(db, user_id, kind, ref)).total
