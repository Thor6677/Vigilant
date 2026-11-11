"""Watchlists: a user's named lists of items for Hub Scan. The only reader
and writer of `market_watchlist` and `market_watchlist_item`.

Every function that takes a watchlist id also takes the user's id and acts
only on that user's list: anything else is "not found" (None / False), which
the routes answer with a 404. The limits live here, not in the routes:
MAX_LISTS lists per user, MAX_ITEMS items per list, names of 1 to MAX_NAME
characters after trimming, unique per user whatever their case. Only
published market types can be added.

Names are the user's own text. They are stored as typed (whitespace
collapsed, control characters dropped) and only ever rendered through Jinja's
autoescape."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import case, delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MarketWatchlist, MarketWatchlistItem
from app.db.sde_models import SDEType
from app.market import scan_results

MAX_LISTS = 20
MAX_ITEMS = 300
MAX_NAME = 60
# Ids are bound into SQLite, which refuses integers past 2**63; ESI types
# every type id as int32. Anything outside 1..MAX_ID is "no such id".
MAX_ID = 2 ** 31 - 1
SEARCH_MIN = 2
SEARCH_MAX = 64
SEARCH_LIMIT = 15

TAKEN = "You already have a list with that name."


class WatchlistError(Exception):
    """A message fit to show the user."""


def parse_id(raw) -> int | None:
    """A list or type id from a form or path: an integer in 1..MAX_ID, else None."""
    try:
        number = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return number if 1 <= number <= MAX_ID else None


def clean_name(raw) -> str:
    """Whitespace collapsed, control characters dropped, cut to MAX_NAME."""
    text = "".join(ch for ch in str(raw or "") if ch.isprintable() or ch.isspace())
    name = " ".join(text.split())[:MAX_NAME].rstrip()
    if not name:
        raise WatchlistError("Give the list a name.")
    return name


def _now() -> datetime:
    """Naive UTC, the form the DateTime columns store."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def get(db: AsyncSession, user_id: int, watchlist_id: int) -> MarketWatchlist | None:
    """The list, only if it belongs to `user_id`."""
    if not 1 <= watchlist_id <= MAX_ID:
        return None
    return (await db.execute(
        select(MarketWatchlist).where(MarketWatchlist.id == watchlist_id,
                                      MarketWatchlist.user_id == user_id)
    )).scalar_one_or_none()


async def all_for(db: AsyncSession, user_id: int) -> list[MarketWatchlist]:
    """The user's lists, by name whatever its case."""
    return list((await db.execute(
        select(MarketWatchlist).where(MarketWatchlist.user_id == user_id)
        .order_by(func.lower(MarketWatchlist.name), MarketWatchlist.id)
    )).scalars())


async def item_ids(db: AsyncSession, watchlist_id: int) -> list[int]:
    return [t for (t,) in (await db.execute(
        select(MarketWatchlistItem.type_id)
        .where(MarketWatchlistItem.watchlist_id == watchlist_id)
    )).all()]


async def items(db: AsyncSession, watchlist_id: int) -> list[tuple[int, str]]:
    """(type_id, name) by name. A type the SDE no longer has shows its id."""
    rows = (await db.execute(
        select(MarketWatchlistItem.type_id, SDEType.type_name)
        .join(SDEType, SDEType.type_id == MarketWatchlistItem.type_id, isouter=True)
        .where(MarketWatchlistItem.watchlist_id == watchlist_id))).all()
    return sorted(((t, n or str(t)) for t, n in rows), key=lambda r: (r[1].lower(), r[0]))


async def _name_taken(db: AsyncSession, user_id: int, name: str,
                      except_id: int | None = None) -> bool:
    rows = (await db.execute(
        select(MarketWatchlist.id, MarketWatchlist.name)
        .where(MarketWatchlist.user_id == user_id))).all()
    return any(n.casefold() == name.casefold() and i != except_id for i, n in rows)


async def create(db: AsyncSession, user_id: int, raw_name) -> int:
    """The new list's id. Raises WatchlistError for a bad or taken name, or
    when the user already has MAX_LISTS lists."""
    name = clean_name(raw_name)
    count = (await db.execute(select(func.count()).select_from(MarketWatchlist)
                              .where(MarketWatchlist.user_id == user_id))).scalar() or 0
    if count >= MAX_LISTS:
        raise WatchlistError(f"You can have up to {MAX_LISTS} watchlists.")
    if await _name_taken(db, user_id, name):
        raise WatchlistError(TAKEN)
    wl = MarketWatchlist(user_id=user_id, name=name, created_at=_now())
    db.add(wl)
    try:
        await db.flush()
        new_id = wl.id
        await db.commit()
    except IntegrityError:
        # Two creates of the same name at once: the unique key caught the second.
        await db.rollback()
        raise WatchlistError(TAKEN) from None
    return new_id


async def rename(db: AsyncSession, user_id: int, watchlist_id: int, raw_name) -> str | None:
    """The new name, or None when the list is not the user's. Raises
    WatchlistError for a bad name or one another of their lists has."""
    wl = await get(db, user_id, watchlist_id)
    if wl is None:
        return None
    name = clean_name(raw_name)
    if await _name_taken(db, user_id, name, except_id=wl.id):
        raise WatchlistError(TAKEN)
    wl.name = name
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise WatchlistError(TAKEN) from None
    return name


async def remove(db: AsyncSession, user_id: int, watchlist_id: int) -> bool:
    """Delete the list, its items and its saved scan result, in one
    transaction. False when the list is not the user's. A scan of the list
    that is still running is the caller's to cancel first."""
    wl = await get(db, user_id, watchlist_id)
    if wl is None:
        return False
    await db.execute(delete(MarketWatchlistItem)
                     .where(MarketWatchlistItem.watchlist_id == wl.id))
    await scan_results.delete_for(db, user_id, "watchlist", wl.id, commit=False)
    await db.delete(wl)
    await db.commit()
    return True


async def _market_type_ids(db: AsyncSession, type_ids: list[int]) -> set[int]:
    """The published market types among `type_ids`."""
    found: set[int] = set()
    for i in range(0, len(type_ids), 500):
        found.update(t for (t,) in (await db.execute(
            select(SDEType.type_id).where(
                SDEType.type_id.in_(type_ids[i:i + 500]), SDEType.published.is_(True),
                SDEType.market_group_id.is_not(None)))).all())
    return found


async def is_market_type(db: AsyncSession, type_id: int | None) -> bool:
    return type_id is not None and 1 <= type_id <= MAX_ID and bool(
        await _market_type_ids(db, [type_id]))


async def add_items(db: AsyncSession, user_id: int, watchlist_id: int,
                    type_ids) -> tuple[int, int] | None:
    """(added, left out because the list is full), or None when the list is
    not the user's. Unknown, unpublished and non-market types are ignored, as
    are items already on the list; the rest are added in the order given
    until the list holds MAX_ITEMS."""
    wl = await get(db, user_id, watchlist_id)
    if wl is None:
        return None
    wanted = list(dict.fromkeys(t for t in (parse_id(x) for x in type_ids) if t is not None))
    valid = await _market_type_ids(db, wanted)
    have = set(await item_ids(db, wl.id))
    new = [t for t in wanted if t in valid and t not in have]
    room = max(MAX_ITEMS - len(have), 0)
    chosen = new[:room]
    added = 0
    if chosen:
        now = _now()
        # One statement, so an item another request added meanwhile is skipped
        # on its own and never costs the rest of the batch.
        result = await db.execute(
            sqlite_insert(MarketWatchlistItem).values(
                [{"watchlist_id": wl.id, "type_id": t, "added_at": now} for t in chosen]
            ).on_conflict_do_nothing(index_elements=["watchlist_id", "type_id"]))
        added = result.rowcount
    await db.commit()
    return added, len(new[room:])


async def remove_item(db: AsyncSession, user_id: int, watchlist_id: int, type_id: int) -> bool:
    """False when the list is not the user's; removing an item that is not
    on the list is not an error."""
    wl = await get(db, user_id, watchlist_id)
    if wl is None:
        return False
    await db.execute(delete(MarketWatchlistItem).where(
        MarketWatchlistItem.watchlist_id == wl.id, MarketWatchlistItem.type_id == type_id))
    await db.commit()
    return True


def clean_query(raw) -> str:
    """The search box's text: trimmed, whitespace collapsed, at most SEARCH_MAX."""
    return " ".join(str(raw or "").split())[:SEARCH_MAX]


def _like_escape(text: str) -> str:
    """`text` matched literally inside a LIKE pattern (escape character \\)."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def search_types(db: AsyncSession, raw_query) -> list[tuple[int, str]]:
    """Up to SEARCH_LIMIT published market types whose name contains the
    query (SEARCH_MIN+ characters), names starting with it first. `%` and `_`
    are matched as themselves, so no query matches everything."""
    q = clean_query(raw_query)
    if len(q) < SEARCH_MIN:
        return []
    text = _like_escape(q)
    is_prefix = case((SDEType.type_name.ilike(f"{text}%", escape="\\"), 0), else_=1)
    rows = (await db.execute(
        select(SDEType.type_id, SDEType.type_name).where(
            SDEType.type_name.ilike(f"%{text}%", escape="\\"),
            SDEType.published.is_(True), SDEType.market_group_id.is_not(None))
        .order_by(is_prefix, SDEType.type_name, SDEType.type_id)
        .limit(SEARCH_LIMIT))).all()
    return [(t, n) for t, n in rows]
