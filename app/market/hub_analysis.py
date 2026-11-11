"""Hub comparison maths: is an item worth selling at a trade hub?

Pure functions, no network and no database, so every rule is unit-testable
and the same code can later rank many items for one hub.

Inputs are plain data:
  * `days`   — [{"date": date, "volume": int|None, "average": float|None}, ...]
               daily REGIONAL history (the game publishes no per-station history).
  * `orders` — raw ESI region orders; filtered here to the hub station.

Two limits the page states in its footnote: demand is regional, and traded
volume includes sales to buy orders, so list-and-wait estimates lean optimistic.

The demand window ends on the last day the stored history covers (see
`coverage_end`), not yesterday by the clock: history is cached for a day and
the game publishes a day's row the next day, so the newest day or two are
often not stored yet, and counting them as zero sales would understate demand
and show a false falling trend.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from statistics import median

from app.market.hub_format import days_phrase, fmt_rate

WINDOW_DAYS = 30
SHORT_WINDOW_DAYS = 7

# How far the stored history may lag behind the newest day the game has
# published (in days; see `history_lag`). 0–1 is normal. From
# HISTORY_BEHIND_DAYS the hub is shown with a "history to <date>" note; beyond
# STALE_HISTORY_MAX_DAYS it is shown as unavailable rather than analysed from
# old rows.
HISTORY_BEHIND_DAYS = 2
STALE_HISTORY_MAX_DAYS = 3
# A fetch always includes every day up to this many days before it.
FETCH_COVERS_TO_DAYS = 2
# The game publishes a day's history the next day, late morning UTC. From
# this hour on, yesterday is taken as published (a margin is left on purpose).
PUBLISH_HOUR_UTC = 13

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
    """up / down / flat: the 7-day rate against the 30-day rate."""
    if demand_30 <= 0:
        return "flat"
    if demand_7 > demand_30 * TREND_UP:
        return "up"
    if demand_7 < demand_30 * TREND_DOWN:
        return "down"
    return "flat"


def coverage_end(row_dates: Iterable[date], fetched_on: date) -> date:
    """The last day the stored history can be trusted to cover: the later of
    its newest row and two days before the fetch (`fetched_on`, the UTC date
    of the meta stamp). A fetch always includes everything up to two days
    before it, so days in between with no row had no trades; a later row
    proves later coverage."""
    newest = max(row_dates, default=date.min)
    return max(newest, fetched_on - timedelta(days=FETCH_COVERS_TO_DAYS))


def published_through(moment: datetime) -> date:
    """The newest day whose history the game has published by `moment`
    (UTC; naive is read as UTC): yesterday from PUBLISH_HOUR_UTC, else the
    day before."""
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    back = 1 if moment.hour >= PUBLISH_HOUR_UTC else 2
    return moment.date() - timedelta(days=back)


def history_lag(row_dates: Iterable[date], fetched: datetime, now: datetime) -> int:
    """How many published days the stored history is missing (0 = none).

    Measured from the fetch, not from the last day the item traded: the
    history reaches the later of its newest row and what was published when
    it was fetched, and that is compared with what is published now. So a
    quiet item whose history was fetched within the last day is never
    'behind', however long ago it last traded."""
    reaches = max(max(row_dates, default=date.min), published_through(fetched))
    return max((published_through(now) - reaches).days, 0)


def history_state(row_dates: Iterable[date], fetched: datetime, now: datetime) -> str:
    """'current' (lag 0–1), 'behind' (2–3: analysed, with a note) or
    'stale' (more: the hub is shown as unavailable)."""
    lag = history_lag(row_dates, fetched, now)
    if lag > STALE_HISTORY_MAX_DAYS:
        return "stale"
    if lag >= HISTORY_BEHIND_DAYS:
        return "behind"
    return "current"


def market_figures(days: list[dict], orders: list[dict], station_id: int,
                   qty: int, window_end_exclusive: date) -> dict:
    """Demand, reference price and the counted order book for one hub.

    The window is the WINDOW_DAYS calendar days before `window_end_exclusive`
    (that day itself excluded); the endpoint passes the day after the last
    day the history covers (`coverage_end`). ESI omits days with no trades,
    so volumes are divided by the calendar length, not by the number of
    rows: a day in the window with no row counts as zero. Each d["date"] must
    be a datetime.date."""
    end = window_end_exclusive
    start_30 = end - timedelta(days=WINDOW_DAYS)
    start_7 = end - timedelta(days=SHORT_WINDOW_DAYS)
    in_30 = [d for d in days if start_30 <= d["date"] < end]
    in_7 = [d for d in in_30 if d["date"] >= start_7]

    demand_30 = sum(d.get("volume") or 0 for d in in_30) / WINDOW_DAYS
    demand_7 = sum(d.get("volume") or 0 for d in in_7) / SHORT_WINDOW_DAYS
    trade_days = sum(1 for d in in_30 if (d.get("volume") or 0) > 0)
    averages = [d["average"] for d in in_30 if d.get("average")]
    reference = median(averages) if averages else None

    # An order with no units left is not on the book, whichever side it is on.
    hub = [o for o in orders
           if o.get("location_id") == station_id and (o.get("volume_remain") or 0) > 0]
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


def list_and_wait(fig: dict, qty: int, cost: float | None,
                  sales_tax: float, broker_fee: float) -> dict:
    """Sell-order picture. Rates are fractions (0.0337, not 3.37)."""
    price = fig["lowest_sell"] if fig["lowest_sell"] is not None else fig["reference"]
    demand = fig["demand_30"]
    days_best = qty / demand if demand > 0 else None
    days_typical = (fig["stock"] + qty) / demand if demand > 0 else None
    profit_each = margin = batch_profit = None
    if price is not None and cost is not None:
        profit_each = price * (1 - sales_tax - broker_fee) - cost
        batch_profit = profit_each * qty
        if cost > 0:
            margin = profit_each / cost
    return {"price": price, "profit_each": profit_each, "margin": margin,
            "days_best": days_best, "days_typical": days_typical,
            "batch_profit": batch_profit}


def sell_now(buys: list[dict], qty: int, cost: float | None, sales_tax: float) -> dict:
    """Buy-order picture: fill `qty` from the highest-priced counted buy orders.
    `buys` must already be filtered and sorted (see market_figures)."""
    remaining, isk = qty, 0.0
    for o in buys:
        take = min(remaining, o.get("volume_remain") or 0)
        isk += take * o["price"]
        remaining -= take
        if remaining <= 0:
            break
    fillable = qty - remaining
    if fillable <= 0:
        return {"fillable": 0, "avg_price": None, "profit_each": None, "batch_profit": None}
    avg_price = isk / fillable
    profit_each = batch_profit = None
    if cost is not None:
        profit_each = avg_price * (1 - sales_tax) - cost
        batch_profit = profit_each * fillable
    return {"fillable": fillable, "avg_price": avg_price,
            "profit_each": profit_each, "batch_profit": batch_profit}


def tags(fig: dict, days_typical: float | None) -> tuple[str, str | None]:
    """(speed, supply). Speed is exactly one of thin/fast/steady/slow."""
    if (fig["trade_days"] < THIN_MIN_TRADE_DAYS or fig["demand_30"] < THIN_MIN_DAILY
            or days_typical is None):
        speed = "thin"
    elif days_typical <= FAST_MAX_DAYS:
        speed = "fast"
    elif days_typical <= STEADY_MAX_DAYS:
        speed = "steady"
    else:
        speed = "slow"

    supply = None
    stock_days = fig["days_of_stock"]
    if fig["nothing_listed"]:
        if fig["demand_30"] > 0:
            supply = "under"
    elif stock_days is not None and stock_days < UNDER_MAX_STOCK_DAYS:
        supply = "under"
    elif stock_days is not None and stock_days > SATURATED_MIN_STOCK_DAYS:
        supply = "saturated"
    return speed, supply


def verdict(result: dict, qty: int, has_cost: bool) -> str:
    """One plain sentence, plus a second clause when selling now is a problem.
    Numbers are formatted by `hub_format`, as the table's are, so the
    sentence and the columns always agree."""
    demand, stock = result["demand_30"], result["stock"]
    lw, now = result["list"], result["now"]

    if result["trade_days"] == 0:
        first = "No sales here in the last 30 days."
    elif result["speed"] == "thin":
        first = (f"Traded on {result['trade_days']} of the last 30 days. "
                 "Treat these figures as rough.")
    elif result["nothing_listed"]:
        first = f"Sells about {fmt_rate(demand)} a day and nothing is listed."
    elif result["supply"] == "saturated":
        first = (f"{stock} listed against about {fmt_rate(demand)} a day: "
                 f"{fmt_rate(result['days_of_stock'])} days of stock ahead of you.")
    else:
        scarce = "only " if result["supply"] == "under" else ""
        first = (f"Sells about {fmt_rate(demand)} a day with {scarce}{stock} listed. "
                 f"Your {qty} would take "
                 f"{days_phrase(lw['days_best'], lw['days_typical'])}.")

    loses = has_cost and now["profit_each"] is not None and now["profit_each"] < 0
    if now["fillable"] == 0:
        second = "No buy orders can take your batch."
    elif loses and now["fillable"] < qty:
        second = (f"Buy orders can only take {now['fillable']}, "
                  "at a loss against your cost.")
    elif loses:
        second = "Selling to buy orders loses money against your cost."
    elif now["fillable"] < qty:
        second = f"Buy orders can only take {now['fillable']}."
    else:
        second = ""
    return f"{first} {second}".strip()


def analyse_hub(days: list[dict], orders: list[dict], station_id: int, qty: int,
                cost: float | None, sales_tax: float, broker_fee: float,
                window_end_exclusive: date) -> dict:
    """Everything the page shows for one hub. See the module docstring;
    `window_end_exclusive` is as in `market_figures`."""
    fig = market_figures(days, orders, station_id, qty, window_end_exclusive)
    buys = fig.pop("buys")
    lw = list_and_wait(fig, qty, cost, sales_tax, broker_fee)
    now = sell_now(buys, qty, cost, sales_tax)
    speed, supply = tags(fig, lw["days_typical"])
    result = {**fig, "list": lw, "now": now, "speed": speed, "supply": supply}
    result["verdict"] = verdict(result, qty, cost is not None)
    return result


def best_picks(results: dict[str, dict | None], order: tuple[str, ...]) -> dict:
    """Fastest sale and best margin across hubs. `results[key]` is None for a
    hub whose data could not be loaded. Ties go to the earlier hub in `order`."""
    usable = [(k, results[k]) for k in order if results.get(k) is not None]

    timed = [(k, r) for k, r in usable if r["list"]["days_typical"] is not None]
    steady = [(k, r) for k, r in timed if r["speed"] != "thin"]
    pool = steady or timed
    fastest = min(pool, key=lambda kr: kr[1]["list"]["days_typical"])[0] if pool else None

    priced = [(k, r) for k, r in usable if r["list"]["profit_each"] is not None]
    # "margin" is chosen by list-and-wait profit per unit in ISK: the same ordering
    # as percentage margin, since the cost is the same at every hub.
    margin = max(priced, key=lambda kr: kr[1]["list"]["profit_each"])[0] if priced else None

    return {"fastest": fastest, "margin": margin}
