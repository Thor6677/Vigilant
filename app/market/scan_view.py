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
    "capped": "The lowest sell here is more than 1.25× the highest daily average of "
              "the last 30 days, so the capped figure is used. The item page shows "
              "the real lowest sell.",
    "none_listed": "Nothing is listed here; the 30-day typical price is used.",
}
COPY_ONLY_TITLE = "Blueprint comes only as a copy; its cost is not in the benchmark."
# Marks on the shown hub's figures when its data lags (scan_results.compute_rows).
HISTORY_BEHIND_TITLE = "Sales history for this hub is a few days behind."
STOCK_TRUNCATED_TITLE = "More than 300 price levels listed; stock may be understated."
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
# Above the table when a hub is chosen: the rows are that hub's, not the best.
HUB_LINE = "Showing what each item earns in {hub}. Choose All to see each item's best hub."
OLD_RESULT_LINE = ("This result was saved before per-hub figures were kept; rescan to see "
                   "every item for {hub}.")


UNREAD_TAIL = "so this list may be missing items."
UNREAD_ALL = "None of your {what}' blueprints could be read."
INCOMPLETE_SUMMARY = "Some blueprint lists may be incomplete, so this list may be missing items."
# Reason -> the lead of its detail line, in the order shown.
UNREAD_REASONS = (
    ("dead_token", "Need to sign in again"),
    ("no_scope", "Haven't shared blueprints with Vigilant"),
    ("failed", "Couldn't be loaded"),
    ("corporation", "Corporations that couldn't be read"),
    ("incomplete", "List may be incomplete (over 10,000 blueprints)"),
)
UNREAD_KIND = "blueprints_unread"


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def unread_summary(record: dict) -> dict | None:
    """{"summary": sentence, "lines": [grouped detail lines]} for a stored
    `blueprints_unread` record, or None when it has nothing to say. Plain text:
    the template escapes it (names are user-controlled). Reads defensively:
    a stored record is data, not trusted shape. Names are sorted, ignoring case."""
    stored = record.get("reasons")
    stored = stored if isinstance(stored, dict) else {}
    names = {}
    for key, _lead in UNREAD_REASONS:
        raw = stored.get(key)
        names[key] = sorted({str(n) for n in raw if n is not None}, key=lambda n: (n.lower(), n)) \
            if isinstance(raw, list) else []
    lines = [f"{lead}: {', '.join(names[key])}" for key, lead in UNREAD_REASONS if names[key]]
    if not lines:
        return None
    chars, corps = _count(record.get("characters")), _count(record.get("corporations"))
    bad_chars = len(names["dead_token"]) + len(names["no_scope"]) + len(names["failed"])
    bad_corps = len(names["corporation"])
    chars, corps = max(chars, bad_chars), max(corps, bad_corps)
    if not (bad_chars or bad_corps):
        summary = INCOMPLETE_SUMMARY
    elif bad_chars == chars and bad_corps == corps:
        what = ("characters' or corporations" if chars and corps
                else "characters" if chars else "corporations")
        summary = UNREAD_ALL.format(what=what)
    else:
        parts = []
        if bad_chars:
            parts.append(f"{bad_chars} of {_plural(chars, 'character')}")
        if bad_corps:
            parts.append(f"{bad_corps} of {_plural(corps, 'corporation')}")
        summary = f"Blueprints from {' and '.join(parts)} couldn't be read, {UNREAD_TAIL}"
    return {"summary": summary, "lines": lines}


def notes_view(notes) -> list:
    """What the result partial prints above the table, in stored order: a
    plain string stays a line, a `blueprints_unread` record becomes its
    summary dict (or nothing), anything else is dropped."""
    out = []
    for note in notes or []:
        if isinstance(note, str):
            out.append(note)
        elif isinstance(note, dict) and note.get("kind") == UNREAD_KIND:
            item = unread_summary(note)
            if item:
                out.append(item)
    return out


def parse_filters(hub: str, source: str, losers: str, under: str) -> dict:
    """Unknown values mean "no filter". Items with no profit per day are
    hidden unless `losers` is "show"."""
    return {"hub": hub if hub in HUB_NAMES else "",
            "source": source if source in SOURCE_NAMES else "",
            "hide_losers": losers != "show",
            "under_only": under == "1"}


def _loser(row: dict) -> bool:
    """No profit per day: loses money, has not sold in the last 7 days, or
    has no figure at all (None counts as 0)."""
    return (row.get("profit_per_day") or 0) <= 0


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


def fmt_margin(margin: float | None) -> str:
    """'48.5%', '−4.0%' (the minus sign U+2212, as every other figure), '—'
    for none. The sign comes from the rounded figure: no '−0.0%'."""
    if margin is None:
        return "—"
    pct = round(margin * 100, 1)
    return f"{'−' if pct < 0 else ''}{abs(pct):.1f}%"


def _other_lines(row: dict) -> tuple[str, str]:
    """(the table's line under the source, the card's line at the foot): the
    other source's figure at the same hub, or "n/a" when that source is not
    offered there (a row's `other` is None exactly then: haul at Jita or with
    nothing listed in Jita, build with no build cost)."""
    other = row.get("other")
    if other is not None:
        profit = hub_view.fmt_signed(other["profit_each"])
        return (f"{SHORT_SOURCE_NAMES[other['source']]}: {profit} each",
                f"{SOURCE_NAMES[other['source']]} instead: {profit} each")
    if "other" not in row:
        return "", ""
    missing = "haul" if row["source"] == "build" else "build"
    return f"{SHORT_SOURCE_NAMES[missing]}: n/a", f"{SOURCE_NAMES[missing]}: n/a"


def display_row(row: dict, routes: dict, show_route: bool) -> dict:
    other_text, other_card = _other_lines(row)
    return {
        "name": row["name"], "link": item_link(row),
        "hub": HUB_NAMES[row["hub"]],
        "speed": row["speed"], "speed_text": hub_view.SPEED_LABELS.get(row["speed"], ""),
        "supply": row["supply"] or "",
        "supply_text": hub_view.SUPPLY_LABELS.get(row["supply"], ""),
        "source": row["source"], "source_text": SOURCE_NAMES[row["source"]],
        "other_text": other_text, "other_card": other_card,
        "cost": hub_view.fmt_isk(row["cost"]),
        "cost_basis": "benchmark" if row["cost_basis"] == "benchmark" else "Jita sell orders",
        "copy_only": bool(row.get("copy_only")) and row["source"] == "build",
        "price": hub_view.fmt_isk(row["price"]),
        "price_flag": PRICE_FLAGS.get(row["price_flag"], ""),
        "price_flag_title": PRICE_FLAG_TITLES.get(row["price_flag"], ""),
        "profit_each": hub_view.fmt_signed(row["profit_each"]),
        "profit_sign": _sign(row["profit_each"]),
        "margin": fmt_margin(row["margin"]),
        "sold": hub_format.fmt_rate(row["demand_30"]),
        "sold_note": HISTORY_BEHIND_TITLE if row.get("history_behind") else "",
        "stock": f"{row['stock']:,}",
        "stock_note": STOCK_TRUNCATED_TITLE if row.get("stock_truncated") else "",
        # Days of stock are a rate, formatted as the item page formats them.
        "stock_days": (f"{hub_format.fmt_rate(row['days_of_stock'])} days"
                       if row["days_of_stock"] is not None else ""),
        "qty": f"{row['qty']:,}", "qty_limit": QTY_LIMITS.get(row["qty_limit"], ""),
        "profit_day": hub_view.fmt_signed(row.get("profit_per_day")),
        "profit_day_sign": _sign(row.get("profit_per_day")),
        # The card says "+52.00 / day", or only "—" when there is no figure.
        "has_profit_day": row.get("profit_per_day") is not None,
        "route": _route(routes.get(row["hub"])) if show_route else None,
    }


def routes_unavailable(routes: dict) -> bool:
    return any(isinstance(r, dict) and r.get("unavailable") for r in routes.values())


def at_hub(row: dict, hub: str) -> dict | None:
    """An ok row as it reads at `hub`: the item with that hub's own best
    source and figures (scan_rules.best_for_item's `by_hub`), or None when
    nothing is offered there (no sales in 30 days, no price, or no source)."""
    figures = row["by_hub"].get(hub)
    if figures is None:
        return None
    return {"type_id": row["type_id"], "name": row["name"], "status": row["status"], **figures}


def _rows_for(ok: list[dict], hub: str) -> tuple[list[dict], int]:
    """(the rows the filters work on, how many items are not sold at `hub`).
    With no hub chosen: every item's best row. With one: every item ranked
    for that hub. A row saved before `by_hub` was kept stays its best row,
    which the hub filter in _passes then keeps only when its best hub is the
    one chosen (the old behaviour)."""
    if not hub:
        return ok, 0
    out, not_sold = [], 0
    for r in ok:
        if "by_hub" not in r:
            out.append(r)
        elif (here := at_hub(r, hub)) is not None:
            out.append(here)
        else:
            not_sold += 1
    return out, not_sold


def build(result: dict, filters: dict, sort: str, direction: str) -> dict:
    """Everything the result partial needs.

    With a hub chosen, every item is ranked for that hub (`at_hub`); with
    All, by its best hub. The counts add up to `total`, every row of the scan
    ("Showing X of N"): shown + hidden + not_sold + filtered + no_sales +
    no_cost + unavailable. `hidden` counts the priced rows that the "hide"
    box alone keeps out (they pass every other filter); `not_sold` the priced
    items with no figures at the chosen hub; `filtered` the rest that the
    source or under-supplied filters keep out (and, for a result saved
    before per-hub figures were kept, the hub filter), whether or not they
    make money. `ok` is every priced row, whatever the filters."""
    rows = result["rows"]
    routes = result.get("routes") or {}
    ok = [r for r in rows if r["status"] == "ok"]
    hub = filters["hub"]
    for_hub, not_sold = _rows_for(ok, hub)
    old_shape = bool(hub) and any("by_hub" not in r for r in ok)
    candidates = [r for r in for_hub if _passes(r, filters)]
    if filters["hide_losers"]:
        shown = [r for r in candidates if not _loser(r)]
    else:
        shown = candidates
    sort = sort if sort in SORTS else DEFAULT_SORT
    descending = direction == "desc" if sort in TEXT_SORTS else direction != "asc"
    show_route = bool(routes)
    if not hub:
        hub_line = ""
    elif old_shape:
        hub_line = OLD_RESULT_LINE.format(hub=HUB_NAMES[hub])
    else:
        hub_line = HUB_LINE.format(hub=HUB_NAMES[hub])
    return {
        "notes": notes_view(result.get("notes")),
        "rows": [display_row(r, routes, show_route)
                 for r in _sorted(shown, sort, descending)],
        "show_route": show_route,
        "routes_note": (hub_view.ROUTES_UNAVAILABLE_NOTE
                        if show_route and routes_unavailable(routes) else ""),
        "hub_name": HUB_NAMES.get(hub, ""),
        # The rows are for the chosen hub, not each item's best: say so.
        "hub_column": "Hub" if hub and not old_shape else "Best hub",
        "hub_line": hub_line,
        "counts": {
            "shown": len(shown), "total": len(rows), "ok": len(ok),
            "hidden": len(candidates) - len(shown),
            "not_sold": not_sold,
            "filtered": len(for_hub) - len(candidates),
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


def settings_text(settings: dict) -> str:
    """The settings a result was computed with (its stored settings, not the
    user's current ones): 'Budget 500M per item · market share 20% · sales
    tax 3.37% · broker fee 1.5% · from Amarr', the last part only with a
    starting system. Plain text: the template escapes it."""
    parts = [f"Budget {fmt_budget(settings['budget_isk'])} per item",
             f"market share {fmt_pct(settings['share_pct'])}%",
             f"sales tax {fmt_pct(settings['sales_tax_pct'])}%",
             f"broker fee {fmt_pct(settings['broker_fee_pct'])}%"]
    if settings.get("origin_system_id") and settings.get("origin_name"):
        parts.append(f"from {settings['origin_name']}")
    return " · ".join(parts)
