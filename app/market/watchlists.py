"""Watchlists: a user's named lists of items for Hub Scan. The only reader
and writer of `market_watchlist` and `market_watchlist_item`."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MarketWatchlist, MarketWatchlistItem


async def get(db: AsyncSession, user_id: int, watchlist_id: int) -> MarketWatchlist | None:
    """The list, only if it belongs to `user_id`."""
    return (await db.execute(
        select(MarketWatchlist).where(MarketWatchlist.id == watchlist_id,
                                      MarketWatchlist.user_id == user_id)
    )).scalar_one_or_none()


async def item_ids(db: AsyncSession, watchlist_id: int) -> list[int]:
    return [t for (t,) in (await db.execute(
        select(MarketWatchlistItem.type_id)
        .where(MarketWatchlistItem.watchlist_id == watchlist_id)
    )).all()]
