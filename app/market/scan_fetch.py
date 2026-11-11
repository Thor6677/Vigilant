"""Hub Scan's market data: one compact summary row per (region, type).

A scan covers hundreds of items in five regions. Going through
`app.market.history` would store ~400 daily rows per pair, which is the bulk
ingest that module exists to avoid, so a scan keeps its own row instead:

  * the last KEEP_DAYS daily history rows as [iso date, volume, average,
    highest], good for HISTORY_TTL;
  * the hub station's sell orders merged by price, cheapest first (at most
    MAX_LEVELS levels), good for ORDERS_TTL.

`figures()` turns a row back into the inputs phase 1's `market_figures` takes
and calls it, so the shared numbers cannot drift from the item page. It adds
`high_30` (the highest daily high in the window) and `ladder` (the counted
sell levels within the 10% stock band, for the Jita haul walk).

`ensure_summary` refreshes whichever part is stale. It reuses fresh rows from
`market_history` when the item has been viewed, and NEVER writes that table.
Fetch errors propagate: the runner counts them.

`Pacer` is the project's bulk-fetch rule (three in flight, batches of ten, a
second between batches), shared by every running scan.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MarketScanSummary
from app.market import history as market_history
from app.market import orders as market_orders
from app.market.hub_analysis import (STOCK_BAND, WINDOW_DAYS, coverage_end,
                                     history_state, market_figures)

HISTORY_TTL = timedelta(hours=24)
ORDERS_TTL = timedelta(minutes=15)
KEEP_DAYS = 40
MAX_LEVELS = 300


class Pacer:
    """`async with pacer:` around each upstream request."""

    def __init__(self, concurrent: int = 3, batch: int = 10, pause: float = 1.0):
        self._sem = asyncio.Semaphore(concurrent)
        self._lock = asyncio.Lock()
        self._batch, self._pause, self._count = batch, pause, 0

    async def __aenter__(self):
        await self._sem.acquire()
        async with self._lock:
            self._count += 1
            if self._pause and self._count % self._batch == 0:
                await asyncio.sleep(self._pause)

    async def __aexit__(self, *exc):
        self._sem.release()


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def compact_days(raw: list[dict]) -> list[list]:
    """ESI history rows -> the newest KEEP_DAYS as [iso, volume, average, highest]."""
    rows = []
    for d in raw or []:
        day = str(d.get("date") or "")[:10]
        if len(day) == 10:
            rows.append([day, d.get("volume") or 0, d.get("average"), d.get("highest")])
    rows.sort(key=lambda r: r[0])
    return rows[-KEEP_DAYS:]


def ladder_from_orders(orders: list[dict], station_id: int) -> tuple[list[list], bool]:
    """([[price, units], ...] cheapest first, truncated?) for sell orders with
    units left at the hub station."""
    by_price: dict[float, int] = {}
    for o in orders or []:
        if (o.get("is_buy_order") or o.get("location_id") != station_id
                or (o.get("volume_remain") or 0) <= 0):
            continue
        by_price[o["price"]] = by_price.get(o["price"], 0) + o["volume_remain"]
    levels = [[p, by_price[p]] for p in sorted(by_price)]
    return levels[:MAX_LEVELS], len(levels) > MAX_LEVELS


def figures(row: MarketScanSummary | None, station_id: int, now: datetime) -> dict | None:
    """Phase 1 market figures for one hub from its summary, plus `high_30`,
    `ladder` and `behind`. None when a part is missing or the history is too
    far behind to treat as current (phase 1's "stale")."""
    if row is None or row.days_json is None or row.ladder_json is None:
        return None
    fetched = _aware(row.history_fetched_at)
    if fetched is None or row.orders_fetched_at is None:
        return None
    days = [{"date": date.fromisoformat(d), "volume": v, "average": a, "highest": h}
            for d, v, a, h in json.loads(row.days_json)]
    dates = [d["date"] for d in days]
    state = history_state(dates, fetched, now)
    if state == "stale":
        return None
    covered = min(coverage_end(dates, fetched.date()), now.date() - timedelta(days=1))
    end = covered + timedelta(days=1)
    orders = [{"is_buy_order": False, "price": p, "volume_remain": u,
               "location_id": station_id, "min_volume": 1}
              for p, u in json.loads(row.ladder_json)]
    fig = market_figures(days, orders, station_id, 1, end)
    fig.pop("buys", None)
    start = end - timedelta(days=WINDOW_DAYS)
    highs = [d["highest"] for d in days if start <= d["date"] < end and d["highest"]]
    fig["high_30"] = max(highs) if highs else None
    lowest = fig["lowest_sell"]
    fig["ladder"] = ([(o["price"], o["volume_remain"]) for o in orders
                      if lowest <= o["price"] <= lowest * STOCK_BAND]
                     if lowest is not None else [])
    fig["behind"] = state == "behind"
    return fig


async def ensure_summary(db: AsyncSession, region_id: int, station_id: int, type_id: int,
                         now: datetime, pacer: Pacer) -> int:
    """Refresh the stale parts of one summary; returns how many upstream
    requests it made (0–2). Raises whatever the fetch raises. `db` must be the
    caller's own session: concurrent callers each need their own."""
    now = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    row = await db.get(MarketScanSummary, (region_id, type_id))
    h_at = _aware(row.history_fetched_at) if row else None
    o_at = _aware(row.orders_fetched_at) if row else None
    need_history = h_at is None or now - h_at >= HISTORY_TTL
    need_orders = o_at is None or now - o_at >= ORDERS_TTL
    if not (need_history or need_orders):
        return 0
    requests = 0
    days = fetched = levels = truncated = None
    if need_history:
        stored_at = await market_history.fetched_at(db, region_id, type_id)
        if stored_at is not None and now - stored_at < market_history.HISTORY_TTL:
            stored = await market_history.get_history(region_id, type_id, db, fetch=False)
            fetched = stored_at
            days = [[r.date.isoformat(), r.volume or 0, r.average, r.highest]
                    for r in stored][-KEEP_DAYS:]
        else:
            async with pacer:
                raw = await market_history._fetch_history_esi(region_id, type_id)
            requests += 1
            days, fetched = compact_days(raw), now
    if need_orders:
        async with pacer:
            raw = await market_orders._fetch_orders_esi(region_id, type_id)
        requests += 1
        levels, truncated = ladder_from_orders(raw, station_id)
    if row is None:
        row = MarketScanSummary(region_id=region_id, type_id=type_id)
        db.add(row)
    if need_history:
        row.days_json, row.history_fetched_at = json.dumps(days), fetched
    if need_orders:
        row.ladder_json, row.ladder_truncated, row.orders_fetched_at = (
            json.dumps(levels), truncated, now)
    await db.commit()
    return requests
