"""Hub Scan presentation: filter, sort and word the stored rows. Pure: no
database. Numbers go through `hub_view` / `hub_format`, so a figure reads the
same here as on the item page's hub comparison."""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlencode

from app.market import hub_format, hub_view

HUB_NAMES = {"jita": "Jita", "amarr": "Amarr", "dodixie": "Dodixie", "hek": "Hek", "rens": "Rens"}
SOURCE_NAMES = {"build": "Build", "haul": "Haul from Jita"}
SHORT_SOURCE_NAMES = {"build": "Build", "haul": "Haul"}
QTY_LIMITS = {"budget": "of budget", "hub": "hub limit · 7 days",
              "jita": "Jita stock", "over_budget": "over budget"}
PRICE_FLAGS = {"capped": "price capped", "none_listed": "none listed"}
PRICE_FLAG_TITLES = {
    "capped": "The lowest sell here is more than 1.25× the 30-day high, so the "
              "capped figure is used. The item page shows the real lowest sell.",
    "none_listed": "Nothing is listed here; the 30-day typical price is used.",
}
COPY_ONLY_TITLE = "Blueprint comes only as a copy; its cost is not in the benchmark."
SORTS = {   # parameter -> row key
    "profit_day": "profit_per_day", "item": "name", "hub": "hub", "cost": "cost",
    "price": "price", "profit": "profit_each", "margin": "margin",
    "sold": "demand_30", "stock": "days_of_stock", "qty": "qty",
}
TEXT_SORTS = ("item", "hub")     # ascending by default; the rest descending
DEFAULT_SORT = "profit_day"

FOOTNOTE = ("Profit / day is profit each × the units you would sell per day at that hub: "
            "your quantity over the typical days to sell it, never more than your market "
            "share of the hub's daily sales (the lower of its 30-day and 7-day rates). "
            "Quantity is what the budget covers, limited to 7 days of that share and, for "
            "hauls, to what Jita has listed near its lowest price. Sales history is per "
            "region and includes sales to buy orders, so the figures lean optimistic.")


def parse_filters(hub: str, source: str, losers: str, under: str) -> dict:
    """Unknown values mean "no filter". Items with no profit per day are
    hidden unless `losers` is "show"."""
    return {"hub": hub if hub in HUB_NAMES else "",
            "source": source if source in SOURCE_NAMES else "",
            "hide_losers": losers != "show",
            "under_only": under == "1"}


def _loser(row: dict) -> bool:
    """No profit per day: loses money, or has not sold in the last 7 days."""
    return row["profit_per_day"] <= 0


def _passes(row: dict, f: dict) -> bool:
    """Every filter but "hide losers"."""
    if f["hub"] and row["hub"] != f["hub"]:
        return False
    if f["source"] and row["source"] != f["source"]:
        return False
    if f["under_only"] and row["supply"] != "under":
        return False
    return True


def _sorted(rows: list[dict], sort: str, descending: bool) -> list[dict]:
    key = SORTS.get(sort, SORTS[DEFAULT_SORT])
    if key in ("name", "hub"):
        return sorted(rows, key=lambda r: str(r[key]).lower(), reverse=descending)
    present = [r for r in rows if r.get(key) is not None]
    absent = [r for r in rows if r.get(key) is None]       # always last
    return sorted(present, key=lambda r: r[key], reverse=descending) + absent


def item_link(row: dict) -> str:
    """The item's hub comparison with what the scan used carried over. A build
    row sends only the quantity: the item page works out the same benchmark."""
    params = {"qty": row["qty"]}
    if row["source"] == "haul" and row.get("cost") is not None:
        params["cost"] = f"{row['cost']:.2f}"
    return f"/market/type/{row['type_id']}?{urlencode(params)}#mk-hubs"


def _route(route) -> dict:
    if route is None:
        return {"text": "no route", "sub": ""}
    if route.get("unavailable"):
        return {"text": "—", "sub": ""}
    return {"text": f"{route['jumps']} j",
            "sub": "highsec" if route["highsec"] else "leaves highsec"}


def _sign(v: float | None) -> str:
    """'pos', 'neg' or '' (zero, or nothing), as the figure is shown."""
    if v is None or round(v, 2) == 0:
        return ""
    return "pos" if v > 0 else "neg"


def display_row(row: dict, routes: dict, show_route: bool) -> dict:
    other = row.get("other")
    other_profit = hub_view.fmt_signed(other["profit_each"]) if other else ""
    return {
        "name": row["name"], "link": item_link(row),
        "hub": HUB_NAMES[row["hub"]],
        "speed": row["speed"], "speed_text": hub_view.SPEED_LABELS.get(row["speed"], ""),
        "supply": row["supply"] or "",
        "supply_text": hub_view.SUPPLY_LABELS.get(row["supply"], ""),
        "source": row["source"], "source_text": SOURCE_NAMES[row["source"]],
        # The table's line under the source, and the card's line at the foot.
        "other_text": (f"{SHORT_SOURCE_NAMES[other['source']]}: {other_profit} each"
                       if other else ""),
        "other_card": (f"{SOURCE_NAMES[other['source']]} instead: {other_profit} each"
                       if other else ""),
        "cost": hub_view.fmt_isk(row["cost"]),
        "cost_basis": "benchmark" if row["cost_basis"] == "benchmark" else "Jita sell orders",
        "copy_only": bool(row.get("copy_only")) and row["source"] == "build",
        "price": hub_view.fmt_isk(row["price"]),
        "price_flag": PRICE_FLAGS.get(row["price_flag"], ""),
        "price_flag_title": PRICE_FLAG_TITLES.get(row["price_flag"], ""),
        "profit_each": hub_view.fmt_signed(row["profit_each"]),
        "profit_sign": _sign(row["profit_each"]),
        "margin": f"{row['margin'] * 100:.1f}%" if row["margin"] is not None else "—",
        "sold": hub_format.fmt_rate(row["demand_30"]),
        "stock": f"{row['stock']:,}",
        # Days of stock are a rate, formatted as the item page formats them.
        "stock_days": (f"{hub_format.fmt_rate(row['days_of_stock'])} days"
                       if row["days_of_stock"] is not None else ""),
        "qty": f"{row['qty']:,}", "qty_limit": QTY_LIMITS.get(row["qty_limit"], ""),
        "profit_day": hub_view.fmt_signed(row["profit_per_day"]),
        "profit_day_sign": _sign(row["profit_per_day"]),
        "route": _route(routes.get(row["hub"])) if show_route else None,
    }


def routes_unavailable(routes: dict) -> bool:
    return any(isinstance(r, dict) and r.get("unavailable") for r in routes.values())


def build(result: dict, filters: dict, sort: str, direction: str) -> dict:
    """Everything the result partial needs. `hidden` counts the rows that the
    "hide" box alone keeps out: they pass every other filter."""
    rows = result["rows"]
    routes = result.get("routes") or {}
    ok = [r for r in rows if r["status"] == "ok"]
    candidates = [r for r in ok if _passes(r, filters)]
    if filters["hide_losers"]:
        shown = [r for r in candidates if not _loser(r)]
    else:
        shown = candidates
    sort = sort if sort in SORTS else DEFAULT_SORT
    descending = direction == "desc" if sort in TEXT_SORTS else direction != "asc"
    show_route = bool(routes)
    return {
        "rows": [display_row(r, routes, show_route)
                 for r in _sorted(shown, sort, descending)],
        "show_route": show_route,
        "routes_note": (hub_view.ROUTES_UNAVAILABLE_NOTE
                        if show_route and routes_unavailable(routes) else ""),
        "counts": {
            "shown": len(shown), "ok": len(ok),
            "hidden": len(candidates) - len(shown),
            "no_sales": sum(1 for r in rows if r["status"] == "no_sales"),
            "no_cost": sum(1 for r in rows if r["status"] == "no_cost"),
            "unavailable": sum(1 for r in rows if r["status"] == "unavailable"),
        },
        "sort": sort,
        "direction": "desc" if descending else "asc",
        "type_ids": [r["type_id"] for r in ok],
    }


def scanned_text(scanned_at: datetime) -> str:
    """'2026-11-10 14:05' in EVE time (UTC). Naive is read as UTC."""
    if scanned_at.tzinfo is None:
        scanned_at = scanned_at.replace(tzinfo=timezone.utc)
    return scanned_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def fmt_budget(v: float) -> str:
    """The budget field's value: '500M', '1.5B', or plain digits when no short
    form reads back (through hub_view.parse_isk) as exactly `v`."""
    for suffix, size in (("B", 1e9), ("M", 1e6)):
        n = v / size
        if n >= 1:
            text = f"{n:.3f}".rstrip("0").rstrip(".") + suffix
            if hub_view.parse_isk(text) == v:
                return text
    return hub_view.fmt_cost_input(v)


def fmt_pct(v: float) -> str:
    """A percentage for a field: 20, 2.5, 3.37."""
    return f"{v:.4f}".rstrip("0").rstrip(".")
