"""Hub Scan ranking rules (spec section 4). Pure: no network, no database.

Input is, per hub, the market figures `scan_fetch.figures()` builds with phase
1's `market_figures` (plus `avg_high_30` and `ladder`), a build cost and the
scan's settings. Output is one row per item: the hub and source (build, or
haul from Jita) with the highest profit per day, and under `by_hub` the best
source at each hub, so a view can rank every item for one hub.

Rates are fractions here (0.20, 0.0337), percentages at the edges.
"""
from __future__ import annotations

import math

from app.market.hub_analysis import tags

HUB_ORDER = ("jita", "amarr", "dodixie", "hek", "rens")
HAUL_SOURCE_HUB = "jita"
STOCK_DAYS = 7          # how many days of the captured rate one seller brings
PRICE_CAP = 1.25        # selling price is at most this multiple of the highest daily
                        # average of the 30 days (a daily high is one trade's price)
_EPS = 1e-9             # float slack: a result that is whole on paper must floor to that whole


def _floor(x: float) -> int:
    """math.floor, forgiving a result that is a whole number up to float noise
    (0.2 * (305 / 7) * 7 is 60.99999999999999, and should count as 61). The
    slack is 1e-9 of a unit, so it never buys a real extra unit."""
    return math.floor(x + _EPS)


def captured(fig: dict, share: float) -> float:
    """Units per day one seller with `share` of the market sells: the share of
    the lower of the 30-day and 7-day rates."""
    return share * min(fig["demand_30"], fig["demand_7"])


def selling_price(fig: dict) -> tuple[float | None, str | None]:
    """(price, flag). flag is "capped" when the lowest sell was pulled down to
    PRICE_CAP x the highest daily average of the 30 days, "none_listed" when
    nothing is listed and the reference price stands in, else None. (None,
    None): no price at all."""
    lowest = fig["lowest_sell"]
    if lowest is None:
        if fig["reference"] is None:
            return None, None
        return fig["reference"], "none_listed"
    high = fig.get("avg_high_30")
    if high and lowest > PRICE_CAP * high:
        return PRICE_CAP * high, "capped"
    return lowest, None


def hub_limit(captured_rate: float) -> int:
    return max(1, _floor(STOCK_DAYS * captured_rate))


def build_quantity(budget: float, cost: float, limit: int) -> tuple[int, str]:
    """(units, which limit applied): "budget", "hub" or "over_budget"."""
    if cost <= 0:
        return limit, "hub"     # nothing to spend, so only the hub limit applies
    affordable = _floor(budget / cost)
    if affordable < 1:
        return 1, "over_budget"
    if limit < affordable:
        return limit, "hub"
    return affordable, "budget"


def haul_quantity(ladder: list, budget: float, limit: int) -> tuple[int, float, str] | None:
    """Walk Jita's competing sell orders, cheapest first: (units, average
    price paid, which limit stopped it), or None when nothing is listed. At
    least one unit is taken even if it costs more than the budget.

    When more than one limit binds at once the label follows this order: the
    hub limit wins; otherwise a budget that runs out before the stock does is
    "budget"; stock fully taken is "jita". Levels with no units are skipped."""
    levels = [(float(p), int(u)) for p, u in ladder if u > 0]
    if not levels:
        return None
    units, spent, stopped = 0, 0.0, "jita"
    for price, available in levels:
        if units >= limit:
            stopped = "hub"
            break
        room = limit - units
        affordable = _floor((budget - spent) / price) if price > 0 else room
        take = min(available, room, max(affordable, 0))
        units += take
        spent += take * price
        if take < available:
            stopped = "hub" if take == room else "budget"
            break
    else:
        stopped = "hub" if units >= limit else "jita"
    if units == 0:
        return 1, levels[0][0], "over_budget"
    return units, spent / units, stopped


def option(fig: dict, source: str, cost: float, qty: int, qty_limit: str,
           share: float, tax: float, broker: float) -> dict | None:
    """One (hub, source) outcome, or None when the hub is not usable (no
    sales in 30 days, or no selling price)."""
    price, flag = selling_price(fig)
    if price is None or fig["demand_30"] <= 0:
        return None
    # Nothing listed, or every listed unit priced above the price we would list
    # at (capped): no stock sits ahead of us, so it does not slow the sale.
    stock = 0 if flag in ("none_listed", "capped") else fig["stock"]
    profit_each = price * (1 - tax - broker) - cost
    days_typical = (stock + qty) / fig["demand_30"]
    units_per_day = min(qty / max(days_typical, 1.0), captured(fig, share))
    return {
        "source": source, "cost": cost, "price": price, "price_flag": flag,
        "profit_each": profit_each,
        "margin": profit_each / cost if cost > 0 else None,
        "qty": qty, "qty_limit": qty_limit, "days_typical": days_typical,
        "units_per_day": units_per_day,
        "profit_per_day": profit_each * units_per_day,
    }


def _options_at(hub: str, fig: dict, build_cost: float | None, ladder: list | None,
                budget: float, share: float, tax: float, broker: float) -> list[dict]:
    limit = hub_limit(captured(fig, share))
    out = []
    if build_cost is not None:
        qty, label = build_quantity(budget, build_cost, limit)
        o = option(fig, "build", build_cost, qty, label, share, tax, broker)
        if o:
            out.append(o)
    if hub != HAUL_SOURCE_HUB and ladder:
        walked = haul_quantity(ladder, budget, limit)
        if walked:
            qty, cost, label = walked
            o = option(fig, "haul", cost, qty, label, share, tax, broker)
            if o:
                out.append(o)
    return out


def _json_safe(row: dict) -> dict:
    """The row is stored as JSON: no inf or nan may reach it."""
    def clean(v):
        if isinstance(v, float) and not math.isfinite(v):
            return None
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items()}
        return v
    return clean(row)


def _key(o: dict) -> tuple[float, float]:
    """How options rank: profit per day, then margin (none ranks lowest)."""
    return (o["profit_per_day"], o["margin"] if o["margin"] is not None else -math.inf)


def _hub_row(hub: str, fig: dict, opts: list[dict]) -> dict:
    """The hub's row: its best option (build before haul on an exact tie),
    with the hub's tags and market figures and the other source's figure."""
    o = None
    for x in opts:
        if o is None or _key(x) > _key(o):
            o = x
    others = [x for x in opts if x is not o]
    other = others[0] if others else None
    speed, supply = tags(fig, o["days_typical"])
    return {
        "hub": hub, "source": o["source"],
        "cost": o["cost"], "cost_basis": "benchmark" if o["source"] == "build" else "jita",
        "price": o["price"], "price_flag": o["price_flag"],
        "profit_each": o["profit_each"], "margin": o["margin"],
        "qty": o["qty"], "qty_limit": o["qty_limit"],
        "profit_per_day": o["profit_per_day"],
        "demand_30": fig["demand_30"], "stock": fig["stock"],
        "days_of_stock": fig["days_of_stock"],
        "speed": speed, "supply": supply,
        "other": {"source": other["source"], "profit_each": other["profit_each"]} if other else None,
    }


def best_for_item(figs: dict[str, dict | None], build_cost: float | None, budget: float,
                  share: float, tax: float, broker: float) -> dict:
    """The item's row. `figs[hub]` is None for a hub with no usable data.

    {"status": "no_sales"}  no hub sold any in 30 days (or none has a price)
    {"status": "no_cost"}   neither a build cost nor anything listed in Jita
    {"status": "ok", ...}   the best (hub, source); see the keys below

    An ok row also has `by_hub`: for every hub with at least one source
    offered, that hub's row (the same keys as the best, without `status`):
    its best source by profit per day, ties to the higher margin. The best
    hub is the one whose row ranks highest, ties to hub order, and the
    top-level keys are a copy of `by_hub[hub]`, so the two cannot disagree.
    """
    jita = figs.get(HAUL_SOURCE_HUB)
    ladder = jita.get("ladder") if jita else None
    usable = {h: f for h in HUB_ORDER
              if (f := figs.get(h)) is not None
              and f["demand_30"] > 0 and selling_price(f)[0] is not None}
    if not usable:
        return {"status": "no_sales"}
    if build_cost is None and not ladder:
        return {"status": "no_cost"}

    by_hub: dict[str, dict] = {}
    best = None     # (sort key, hub)
    for rank, hub in enumerate(h for h in HUB_ORDER if h in usable):
        fig = usable[hub]
        opts = _options_at(hub, fig, build_cost, ladder, budget, share, tax, broker)
        if not opts:
            continue
        row = by_hub[hub] = _hub_row(hub, fig, opts)
        key = (*_key(row), -rank)
        if best is None or key > best[0]:
            best = (key, hub)
    if best is None:
        return {"status": "no_cost"}

    return _json_safe({"status": "ok", **by_hub[best[1]], "by_hub": by_hub})
