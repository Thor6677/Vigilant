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
`avg_high_30` (the highest daily AVERAGE price in the window, the price cap's
basis: one absurd trade sets a day's `highest` but barely moves its average)
and `ladder` (the counted sell levels within the 10% stock band, for the Jita
haul walk). The stored days still carry `highest`, which nothing reads now.

`ensure_summary` refreshes whichever part is stale. It reuses fresh rows from
`market_history` when the item has been viewed, and NEVER writes that table.
Fetch errors propagate unchanged. The history part is committed before the
orders fetch starts, so a failed orders fetch does not throw away a good
history fetch.

An outage must cost EVE's API little: ESI allows the app a limited number of
error responses a minute, and running out blocks every user's ESI traffic.
So, around each upstream request:

  * failure memory: a (region, type) whose request failed is not requested
    again for FAILURE_TTL (90 s, as `app.market.history`) by any caller in the
    process; `ensure_summary` raises `RecentlyFailed` at once instead. The
    memory is in this module, bounded (expired stamps are dropped on every
    insert, and never more than MAX_REMEMBERED), and a success clears the pair;
  * budget guard: inside the pacer, just before the request, an error budget
    below ERROR_BUDGET_FLOOR (the client's tracker, from EVE's
    X-Esi-Error-Limit-Remain, while that reading's window lasts) raises
    `ErrorBudgetLow` and makes no request;
  * the caller's `tally`, when given: `tally.before()` runs inside the pacer
    just before the request and may raise to make none (a scan that has
    stopped); `tally.after(ok)` runs once the request has answered or failed.
    That is how the runner counts requests and tells a failed request from
    any other error. Neither runs for a part that needs no request.

Writes are SQLite upserts touching only the columns of the part just fetched,
and `ensure_summary` is single-flight per (region, type) within the process: an
`asyncio.Lock` per key, held across the freshness check, the fetches and the
write, with the row re-read after the lock is taken so a second caller finds the
parts fresh. The lock table holds an entry only while someone uses or waits on
it, so it cannot grow.

Known limits: `outliers` counts merged price levels, not orders. Freshness
stamps are the caller's `now`, so pass the current time on each call rather than
a time captured at the start of a long scan.

`Pacer` is the project's bulk-fetch rule (three in flight, batches of ten, a
second between batches), shared by every running scan.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MarketScanSummary
from app.esi.rate_limit import rate_limit_tracker
from app.market import history as market_history
from app.market import orders as market_orders
from app.market.hub_analysis import (STOCK_BAND, WINDOW_DAYS, coverage_end,
                                     history_state, market_figures)

HISTORY_TTL = timedelta(hours=24)
ORDERS_TTL = timedelta(minutes=15)
KEEP_DAYS = 40
MAX_LEVELS = 300
FAILURE_TTL = market_history.FAILURE_TTL     # no new request for a failed pair this long
MAX_REMEMBERED = 5000                         # failure stamps kept at most
ERROR_BUDGET_FLOOR = 30                       # make no request with fewer errors left


class RecentlyFailed(Exception):
    """The pair's request failed less than FAILURE_TTL ago: no request was made."""


class ErrorBudgetLow(Exception):
    """EVE's error budget for the app is below ERROR_BUDGET_FLOOR: no request was made."""


# {(region_id, type_id): when its last request failed}, oldest first.
_failed: dict[tuple[int, int], datetime] = {}


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def recently_failed(region_id: int, type_id: int, now: datetime) -> bool:
    """True when the pair's request failed less than FAILURE_TTL before `now`."""
    stamp = _failed.get((region_id, type_id))
    return stamp is not None and _utc(now) - stamp < FAILURE_TTL


def _remember_failure(key: tuple[int, int], now: datetime) -> None:
    """Stamp a failed request, dropping expired stamps and, past
    MAX_REMEMBERED, the oldest."""
    for k, stamp in list(_failed.items()):
        if now - stamp >= FAILURE_TTL:
            del _failed[k]
    _failed.pop(key, None)          # re-inserted last: the dict stays oldest first
    _failed[key] = now
    while len(_failed) > MAX_REMEMBERED:
        del _failed[next(iter(_failed))]


def error_budget_low() -> bool:
    """True when the client's tracker says EVE's error budget for the app is
    below ERROR_BUDGET_FLOOR. A reading counts only until its window resets
    (`reset_in_seconds` after it was taken); none at all is not low."""
    legacy = rate_limit_tracker.legacy
    if legacy is None or legacy.remaining >= ERROR_BUDGET_FLOOR:
        return False
    age = (datetime.now(timezone.utc) - _utc(legacy.last_updated)).total_seconds()
    return age < legacy.reset_in_seconds


class Pacer:
    """`async with pacer:` around each upstream request."""

    def __init__(self, concurrent: int = 3, batch: int = 10, pause: float = 1.0):
        self._sem = asyncio.Semaphore(concurrent)
        self._lock = asyncio.Lock()
        self._batch, self._pause, self._count = batch, pause, 0

    async def __aenter__(self):
        await self._sem.acquire()
        try:
            async with self._lock:
                self._count += 1
                if self._pause and self._count % self._batch == 0:
                    await asyncio.sleep(self._pause)
        except BaseException:
            self._sem.release()     # cancelled while waiting or pausing
            raise

    async def __aexit__(self, *exc):
        self._sem.release()


# {(region_id, type_id): [lock, users]}; an entry lives only while in use.
_locks: dict[tuple[int, int], list] = {}


@asynccontextmanager
async def _single_flight(key: tuple[int, int]):
    entry = _locks.get(key)
    if entry is None:
        entry = _locks[key] = [asyncio.Lock(), 0]
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if entry[1] == 0 and _locks.get(key) is entry:
            del _locks[key]


async def _upsert(db: AsyncSession, region_id: int, type_id: int, values: dict) -> None:
    """Insert the row or update only `values`' columns, then commit."""
    stmt = sqlite_insert(MarketScanSummary).values(
        region_id=region_id, type_id=type_id, **values)
    await db.execute(stmt.on_conflict_do_update(
        index_elements=["region_id", "type_id"], set_=values))
    await db.commit()


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
    """Phase 1 market figures for one hub from its summary, plus
    `avg_high_30`, `ladder`, `behind` and `ladder_truncated`. None when a part
    is missing or the history is too far behind to treat as current (phase
    1's "stale")."""
    if row is None or row.days_json is None or row.ladder_json is None:
        return None
    now = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
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
    averages = [d["average"] for d in days if start <= d["date"] < end and d["average"]]
    fig["avg_high_30"] = max(averages) if averages else None
    lowest = fig["lowest_sell"]
    fig["ladder"] = ([(o["price"], o["volume_remain"]) for o in orders
                      if lowest <= o["price"] <= lowest * STOCK_BAND]
                     if lowest is not None else [])
    fig["behind"] = state == "behind"
    fig["ladder_truncated"] = bool(row.ladder_truncated)
    return fig


async def _request(fetch, region_id: int, type_id: int, now: datetime, pacer: Pacer,
                   tally) -> list:
    """One upstream request for the pair, with the failure memory, the pacer,
    the budget guard and the caller's tally (see the module docstring)."""
    key = (region_id, type_id)
    if recently_failed(region_id, type_id, now):
        raise RecentlyFailed(f"{region_id}/{type_id} failed under {FAILURE_TTL} ago")
    async with pacer:
        if tally is not None:
            tally.before()
        if error_budget_low():
            raise ErrorBudgetLow()
        try:
            raw = await fetch(region_id, type_id)
        except Exception:
            _remember_failure(key, now)
            if tally is not None:
                tally.after(False)
            raise
    _failed.pop(key, None)
    if tally is not None:
        tally.after(True)
    return raw


async def ensure_summary(db: AsyncSession, region_id: int, station_id: int, type_id: int,
                         now: datetime, pacer: Pacer, tally=None) -> int:
    """Refresh the stale parts of one summary; returns how many upstream
    requests it made (0–2). Raises whatever the fetch raises; `RecentlyFailed`
    or `ErrorBudgetLow` when a part needs a request it will not make; and
    whatever `tally.before()` raises. `db` must be the caller's own session:
    concurrent callers each need their own."""
    now = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    async with _single_flight((region_id, type_id)):
        # Re-read after the lock: a concurrent caller may just have written it.
        row = await db.get(MarketScanSummary, (region_id, type_id), populate_existing=True)
        h_at = _aware(row.history_fetched_at) if row else None
        o_at = _aware(row.orders_fetched_at) if row else None
        need_history = h_at is None or now - h_at >= HISTORY_TTL
        need_orders = o_at is None or now - o_at >= ORDERS_TTL
        requests = 0
        if need_history:
            stored_at = await market_history.fetched_at(db, region_id, type_id)
            if stored_at is not None and now - stored_at < market_history.HISTORY_TTL:
                stored = await market_history.get_history(region_id, type_id, db, fetch=False)
                fetched = stored_at
                days = [[r.date.isoformat(), r.volume or 0, r.average, r.highest]
                        for r in stored][-KEEP_DAYS:]
            else:
                raw = await _request(market_history._fetch_history_esi, region_id, type_id,
                                     now, pacer, tally)
                requests += 1
                days, fetched = compact_days(raw), now
            await _upsert(db, region_id, type_id,
                          {"days_json": json.dumps(days), "history_fetched_at": fetched})
        if need_orders:
            raw = await _request(market_orders._fetch_orders_esi, region_id, type_id,
                                 now, pacer, tally)
            requests += 1
            levels, truncated = ladder_from_orders(raw, station_id)
            await _upsert(db, region_id, type_id,
                          {"ladder_json": json.dumps(levels), "ladder_truncated": truncated,
                           "orders_fetched_at": now})
        return requests
