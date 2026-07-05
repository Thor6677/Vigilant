"""Daily net-worth snapshots (Phase 5 Task 1).

**Investigation (done up front, per the plan — what's actually synced):**

Only two of the four candidate inputs are persisted in a sync cache and thus
cheap to value in a once-a-day batch job with no extra ESI round trips:

  * **wallet** — `CharacterDashboardCache.wallet` (Float), refreshed by the
    dashboard sync (`_FIELD_FETCHERS["wallet"]`).
  * **assets** — `CharacterAssetCache.assets_json`, a flat JSON list of
    resolved asset stacks (`{type_id, quantity, ...}`), refreshed hourly.

The other two are deliberately EXCLUDED (documented on the page footnote):

  * **market orders / sell-order escrow** — NOT synced anywhere. The
    `orders_json` column on `CharacterDashboardCache` is vestigial (never
    written) and `esi/market.get_character_orders` has no callers. Capturing
    escrow would mean a new per-character ESI sync field — that's Task 5's
    territory, not a valuation shortcut. The `escrow` column is kept at 0.0.
  * **industry jobs (work-in-progress value)** — fetched live per request in
    `industry_jobs.py`, never persisted.

**Valuation rule (the "no per-item ESI" constraint):** exactly ONE global
price map (`app.market.lp.get_price_map`, backed by `/markets/prices/`) is
computed once per snapshot run and reused for every character and every item.
Each asset is valued `quantity x price[type_id]`; items whose `type_id` has no
price (BPCs, some rare/untradeable types) are skipped and *counted* so the
page can show how much was left unvalued rather than silently understating.

**Idempotency:** one row per (character_id, date). The upsert
(`on_conflict_do_update`) overwrites in place, so re-running the job for the
same date — including a same-day container restart re-triggering the daily
tick — never duplicates rows. `total` is per character; the account-wide
total is summed across a user's characters at query time.
"""
from __future__ import annotations

from datetime import date as date_cls, datetime, timezone
import json
import logging

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AsyncSessionLocal,
    Character,
    CharacterAssetCache,
    CharacterDashboardCache,
    NetWorthSnapshot,
)
from app.market.lp import get_price_map

logger = logging.getLogger(__name__)


def _today() -> date_cls:
    """Clock seam — the UTC date the snapshot is filed under. Monkeypatchable."""
    return datetime.now(timezone.utc).date()


def value_assets(
    assets: list[dict] | None, price_map: dict[int, float]
) -> tuple[float, int]:
    """Pure math, no I/O. Sum `quantity x price` over a resolved asset list.

    Returns `(total_value, unpriced_count)`. `unpriced_count` is the number of
    asset stacks whose `type_id` was absent from `price_map` (or unusable) —
    those contribute nothing to the total. Kept separate from I/O so it's
    directly unit-testable against fixture assets + prices.
    """
    total = 0.0
    unpriced = 0
    for a in assets or []:
        tid = a.get("type_id")
        price = price_map.get(tid) if tid is not None else None
        if price is None:
            unpriced += 1
            continue
        qty = a.get("quantity") or 0
        total += price * qty
    return total, unpriced


def _parse_assets(asset_cache: CharacterAssetCache | None) -> list[dict]:
    if asset_cache is None or not asset_cache.assets_json:
        return []
    try:
        data = json.loads(asset_cache.assets_json)
    except (json.JSONDecodeError, TypeError):
        return []
    return data if isinstance(data, list) else []


def build_snapshot_values(
    char: Character,
    dash_cache: CharacterDashboardCache | None,
    asset_cache: CharacterAssetCache | None,
    price_map: dict[int, float],
    on_date: date_cls,
) -> dict | None:
    """Build the upsert `values` dict for one character, or None to skip.

    A character with neither a synced wallet nor any assets has no data worth a
    row — skipping it keeps the chart from being polluted with flat-zero series
    for characters that have never finished a sync.
    """
    wallet = None
    if dash_cache is not None and dash_cache.wallet is not None:
        wallet = float(dash_cache.wallet)

    assets = _parse_assets(asset_cache)
    assets_value, unpriced = value_assets(assets, price_map)

    if wallet is None and not assets:
        return None

    wallet_val = wallet or 0.0
    escrow = 0.0  # not synced — see module docstring.
    total = wallet_val + assets_value + escrow
    return {
        "character_id": char.character_id,
        "date": on_date,
        "user_id": char.user_id,
        "wallet": wallet_val,
        "assets_value": assets_value,
        "escrow": escrow,
        "total": total,
        "unpriced_count": unpriced,
        "recorded_at": datetime.now(timezone.utc).replace(tzinfo=None),
    }


async def take_snapshots(
    db: AsyncSession,
    price_map: dict[int, float],
    characters: list[Character],
    on_date: date_cls | None = None,
) -> dict:
    """Upsert one net-worth row per character for `on_date` (default: today).

    Idempotent — a second call for the same date overwrites in place. Bulk-loads
    both caches for the given characters in two queries (no per-character round
    trip), values each, and issues one upsert per character. Commits once at the
    end so both the background-job session and a request session persist.
    """
    on_date = on_date or _today()
    if not characters:
        return {"date": on_date.isoformat(), "written": 0, "skipped": 0}

    cids = [c.character_id for c in characters]
    dash_rows = (await db.execute(
        select(CharacterDashboardCache).where(
            CharacterDashboardCache.character_id.in_(cids)
        )
    )).scalars().all()
    dash_by_cid = {r.character_id: r for r in dash_rows}
    asset_rows = (await db.execute(
        select(CharacterAssetCache).where(
            CharacterAssetCache.character_id.in_(cids)
        )
    )).scalars().all()
    asset_by_cid = {r.character_id: r for r in asset_rows}

    written = 0
    skipped = 0
    for char in characters:
        values = build_snapshot_values(
            char,
            dash_by_cid.get(char.character_id),
            asset_by_cid.get(char.character_id),
            price_map,
            on_date,
        )
        if values is None:
            skipped += 1
            continue
        stmt = sqlite_insert(NetWorthSnapshot).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["character_id", "date"],
            set_={
                "user_id": stmt.excluded.user_id,
                "wallet": stmt.excluded.wallet,
                "assets_value": stmt.excluded.assets_value,
                "escrow": stmt.excluded.escrow,
                "total": stmt.excluded.total,
                "unpriced_count": stmt.excluded.unpriced_count,
                "recorded_at": stmt.excluded.recorded_at,
            },
        )
        await db.execute(stmt)
        written += 1

    await db.commit()
    return {"date": on_date.isoformat(), "written": written, "skipped": skipped}


async def snapshot_for_characters(
    db: AsyncSession, characters: list[Character], on_date: date_cls | None = None
) -> dict:
    """Convenience wrapper for a request-scoped session (the "Snapshot now"
    button): compute the price map once on the caller's session, then upsert."""
    price_map = await get_price_map(db)
    return await take_snapshots(db, price_map, characters, on_date=on_date)


async def run_daily_snapshot() -> dict:
    """Background-job entry point — opens its own session (never shares one with
    the scheduler or other jobs), snapshots ALL linked characters once for
    today. Piggybacked on the daily tick in `_background_scheduler`."""
    async with AsyncSessionLocal() as db:
        characters = (await db.execute(select(Character))).scalars().all()
        price_map = await get_price_map(db)
        result = await take_snapshots(db, price_map, list(characters))
    logger.info("net-worth daily snapshot: %s", result)
    return result
