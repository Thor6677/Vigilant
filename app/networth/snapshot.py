"""Daily net-worth snapshots (Phase 5 Task 1).

**Investigation (done up front, per the plan — what's actually synced):**

Only two of the four candidate inputs are persisted in a sync cache and thus
cheap to value in a once-a-day batch job with no extra ESI round trips:

  * **wallet** — `CharacterDashboardCache.wallet` (Float), refreshed by the
    dashboard sync (`_FIELD_FETCHERS["wallet"]`).
  * **assets** — `CharacterAssetCache.assets_json`, a flat JSON list of
    resolved asset stacks (`{type_id, quantity, ...}`), refreshed hourly.

Two further inputs joined in T-041 item 4, once their sync fields landed
("orders" and "industry" in dashboard.py's `_FIELD_FETCHERS`, hourly):

  * **market orders** (`CharacterDashboardCache.orders_json`) — buy orders
    contribute their ISK `escrow`; sell orders contribute goods value
    (global reference price of the type, falling back to the order's own
    ask price, x volume_remain). Both land in the `escrow` column.
  * **industry jobs** (`industry_json`) — active/paused/ready jobs valued
    at product reference price x runs x per-run product quantity
    (SDEBlueprintInfo). Jobs whose product has no reference price are
    skipped. Lands in `industry_value`.

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


def _parse_json_list(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return data if isinstance(data, list) else []


def value_orders(orders: list[dict], price_map: dict[int, float]) -> float:
    """Market-locked value: buy-order ISK escrow + sell-order goods value.

    Sell orders are valued at the global reference price when available
    (consistent with asset valuation), falling back to the order's own ask
    price — an unpriced type listed for sale still has the seller's price
    as a defensible estimate. Pure math, unit-testable."""
    total = 0.0
    for o in orders:
        if o.get("is_buy_order"):
            total += float(o.get("escrow") or 0.0)
        else:
            price = price_map.get(o.get("type_id"))
            if price is None:
                price = float(o.get("price") or 0.0)
            total += price * (o.get("volume_remain") or 0)
    return total


def value_industry_jobs(
    jobs: list[dict],
    price_map: dict[int, float],
    product_qty_map: dict[int, int],
) -> float:
    """Work-in-progress value of active industry jobs.

    Valued as the OUTPUT: reference price of the product x runs x per-run
    product quantity (from SDEBlueprintInfo, keyed by blueprint_type_id;
    defaults to 1). Jobs without a priced product contribute nothing —
    consistent with how unpriced assets are skipped. Pure math."""
    total = 0.0
    for j in jobs:
        pid = j.get("product_type_id")
        price = price_map.get(pid) if pid is not None else None
        if price is None:
            continue
        qty = product_qty_map.get(j.get("blueprint_type_id"), 1) or 1
        total += price * (j.get("runs") or 0) * qty
    return total


def build_snapshot_values(
    char: Character,
    dash_cache: CharacterDashboardCache | None,
    asset_cache: CharacterAssetCache | None,
    price_map: dict[int, float],
    on_date: date_cls,
    product_qty_map: dict[int, int] | None = None,
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

    escrow = 0.0
    industry_value = 0.0
    if dash_cache is not None:
        escrow = value_orders(_parse_json_list(dash_cache.orders_json), price_map)
        industry_value = value_industry_jobs(
            _parse_json_list(dash_cache.industry_json), price_map,
            product_qty_map or {})

    wallet_val = wallet or 0.0
    total = wallet_val + assets_value + escrow + industry_value
    return {
        "character_id": char.character_id,
        "date": on_date,
        "user_id": char.user_id,
        "wallet": wallet_val,
        "assets_value": assets_value,
        "escrow": escrow,
        "industry_value": industry_value,
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

    # Per-run product quantities for WIP valuation — one bulk query for
    # exactly the blueprints appearing in the synced industry jobs.
    bp_ids: set[int] = set()
    for r in dash_rows:
        for j in _parse_json_list(r.industry_json):
            if j.get("blueprint_type_id"):
                bp_ids.add(j["blueprint_type_id"])
    product_qty_map: dict[int, int] = {}
    if bp_ids:
        from app.db.sde_models import SDEBlueprintInfo
        qty_rows = (await db.execute(
            select(SDEBlueprintInfo.blueprint_type_id,
                   SDEBlueprintInfo.product_quantity)
            .where(SDEBlueprintInfo.blueprint_type_id.in_(bp_ids))
        )).all()
        product_qty_map = {r[0]: (r[1] or 1) for r in qty_rows}

    written = 0
    skipped = 0
    for char in characters:
        values = build_snapshot_values(
            char,
            dash_by_cid.get(char.character_id),
            asset_by_cid.get(char.character_id),
            price_map,
            on_date,
            product_qty_map=product_qty_map,
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
                "industry_value": stmt.excluded.industry_value,
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
