"""Hub comparison maths: is an item worth selling at a trade hub?

Pure functions, no network and no database, so every rule is unit-testable
and the same code can later rank many items for one hub.

Inputs are plain data:
  * `days`   — [{"date": date, "volume": int|None, "average": float|None}, ...]
               daily REGIONAL history (the game publishes no per-station history).
  * `orders` — raw ESI region orders; filtered here to the hub station.

Two limits the page states in its footnote: demand is regional, and traded
volume includes sales to buy orders, so list-and-wait estimates lean optimistic.
"""
from __future__ import annotations

from datetime import date, timedelta
from statistics import median

WINDOW_DAYS = 30
SHORT_WINDOW_DAYS = 7

OUTLIER_FLOOR = 0.5     # sell orders under this share of the reference price are ignored
STOCK_BAND = 1.10       # "competing stock" = listed within 10% of the lowest sell
TREND_UP = 1.2
TREND_DOWN = 0.8

# Tag thresholds. Starting values, expected to be tuned after real use.
THIN_MIN_TRADE_DAYS = 15
THIN_MIN_DAILY = 1.0
FAST_MAX_DAYS = 5.0
STEADY_MAX_DAYS = 14.0
UNDER_MAX_STOCK_DAYS = 3.0
SATURATED_MIN_STOCK_DAYS = 30.0


def trend(demand_7: float, demand_30: float) -> str:
    if demand_30 <= 0:
        return "flat"
    if demand_7 > demand_30 * TREND_UP:
        return "up"
    if demand_7 < demand_30 * TREND_DOWN:
        return "down"
    return "flat"


def market_figures(days: list[dict], orders: list[dict], station_id: int,
                   qty: int, today: date) -> dict:
    """Demand, reference price and the counted order book for one hub.

    The window is the WINDOW_DAYS calendar days ending yesterday. ESI omits
    days with no trades, so volumes are divided by the calendar length, not
    by the number of rows."""
    start_30 = today - timedelta(days=WINDOW_DAYS)
    start_7 = today - timedelta(days=SHORT_WINDOW_DAYS)
    in_30 = [d for d in days if start_30 <= d["date"] < today]
    in_7 = [d for d in in_30 if d["date"] >= start_7]

    demand_30 = sum(d.get("volume") or 0 for d in in_30) / WINDOW_DAYS
    demand_7 = sum(d.get("volume") or 0 for d in in_7) / SHORT_WINDOW_DAYS
    trade_days = sum(1 for d in in_30 if (d.get("volume") or 0) > 0)
    averages = [d["average"] for d in in_30 if d.get("average")]
    reference = median(averages) if averages else None

    hub = [o for o in orders if o.get("location_id") == station_id]
    sells = [o for o in hub if not o.get("is_buy_order")]
    if reference is not None:
        floor = reference * OUTLIER_FLOOR
        counted = [o for o in sells if o["price"] >= floor]
    else:
        counted = list(sells)
    counted.sort(key=lambda o: o["price"])
    lowest_sell = counted[0]["price"] if counted else None

    buys = [
        o for o in hub
        if o.get("is_buy_order")
        and (o.get("min_volume") or 1) <= qty
        and (lowest_sell is None or o["price"] <= lowest_sell)
    ]
    buys.sort(key=lambda o: -o["price"])

    stock = 0
    if lowest_sell is not None:
        ceiling = lowest_sell * STOCK_BAND
        stock = sum(o.get("volume_remain") or 0 for o in counted if o["price"] <= ceiling)

    days_of_stock = None
    if lowest_sell is not None and demand_30 > 0:
        days_of_stock = stock / demand_30

    return {
        "demand_30": demand_30,
        "demand_7": demand_7,
        "trade_days": trade_days,
        "trend": trend(demand_7, demand_30),
        "reference": reference,
        "lowest_sell": lowest_sell,
        "highest_buy": buys[0]["price"] if buys else None,
        "outliers": len(sells) - len(counted),
        "stock": stock,
        "days_of_stock": days_of_stock,
        "nothing_listed": lowest_sell is None,
        "buys": buys,
    }
