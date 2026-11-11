"""Hub comparison display strings. Pure: results in, template-ready dicts out.

Rates and day counts are formatted by `hub_format`, which the verdict
sentence (`hub_analysis`) uses too."""
from __future__ import annotations

import math
import re
from datetime import date

from app.market.hub_format import days_phrase, fmt_days_range, fmt_rate

SPEED_LABELS = {"fast": "Fast", "steady": "Steady", "slow": "Slow", "thin": "Thin market"}
SUPPLY_LABELS = {"under": "Under-supplied", "saturated": "Saturated"}
_SUFFIX = {"k": 1e3, "m": 1e6, "b": 1e9}
# A typed cost above this (a quadrillion ISK) is not one anyone means; it is
# treated as unreadable, which also keeps every product with a quantity finite.
MAX_ISK = 1e15
_DECIMAL_COMMA = re.compile(r",\d{1,2}[kmb]?$")

# Where the cost per unit came from → how the first note names it.
_COST_SOURCES = {
    "benchmark": ("benchmark (materials at EVE's average reference price, ME {me}, "
                  "no job fees or invention cost)"),
    "typed": "your figure",
    "build": "from your build",
}
NO_COST_NOTE = "No blueprint for this item — enter a cost per unit to see profit and margin."
NO_PRICE_NOTE = ("No benchmark cost right now (a material has no price) — enter a cost per "
                 "unit to see profit and margin.")
# Why there is no benchmark (hub_cost.Benchmark.missing) → the note that says so.
_NO_COST_NOTES = {"no_blueprint": NO_COST_NOTE, "unpriced": NO_PRICE_NOTE}
UNREADABLE_COST_BENCHMARK = "Could not read that cost; using the benchmark."
UNREADABLE_COST_NONE = "Could not read that cost; enter a number such as 180m."


def fmt_isk(v: float | None) -> str:
    if v is None:
        return "—"
    a = abs(v)
    # Pick the unit from the ROUNDED value so we never print four digits
    # before the point (999_950 -> "1.0M", not "1000.0K").
    if round(a / 1e6, 1) >= 1000:
        return f"{v / 1e9:.2f}B"
    if a >= 1e9:
        return f"{v / 1e9:.2f}B"
    if round(a / 1e3, 1) >= 1000:
        return f"{v / 1e6:.1f}M"
    if a >= 1e6:
        return f"{v / 1e6:.1f}M"
    if round(a, 2) >= 1000:
        return f"{v / 1e3:.1f}K"
    if a >= 1e3:
        return f"{v / 1e3:.1f}K"
    return f"{v:.2f}"


def _negative(v: float) -> bool:
    """Negative after rounding to 2 decimals (so -0.001 is not shown as -0.00)."""
    return round(v, 2) < 0


def fmt_signed(v: float | None) -> str:
    if v is None:
        return "—"
    return ("−" if _negative(v) else "+") + fmt_isk(abs(v))


def _sign(v: float | None) -> str:
    if v is None:
        return ""
    return "neg" if _negative(v) else "pos"


def parse_isk(raw) -> float | None:
    """'180000000', '180,000,000', '180m', '1.2B' → float. None when blank,
    unparseable, negative, not finite or above MAX_ISK, and when a comma is
    followed by only one or two digits at the end ('1,5', '12,50', '1,5m'),
    which may be a decimal comma; thousands commas ('1,500') read as usual.
    '-0' reads as 0."""
    s = str(raw or "").replace(" ", "").lower()
    if _DECIMAL_COMMA.search(s):
        return None
    s = s.replace(",", "")
    if not s:
        return None
    mult = 1.0
    if s[-1] in _SUFFIX:
        mult, s = _SUFFIX[s[-1]], s[:-1]
    try:
        v = float(s) * mult
    except ValueError:
        return None
    if not math.isfinite(v) or v < 0 or v > MAX_ISK:
        return None
    return 0.0 if v == 0 else v   # no negative zero


def fmt_cost_input(v: float) -> str:
    """A cost for the input field: at most 2 decimals, trailing zeros trimmed
    (4.35 → '4.35', 0.4 → '0.4', 268000000.0 → '268000000'). parse_isk reads
    it back as the same value once that value is rounded to 2 decimals."""
    return f"{v:.2f}".rstrip("0").rstrip(".")


def cost_placeholder(benchmark: float | None) -> str:
    """What the blank cost field shows: the benchmark it stands for, if any."""
    return f"benchmark {fmt_isk(benchmark)}" if benchmark is not None else "enter a cost"


def cost_note(cost: float | None, source: str | None, me: int,
              missing: str | None = None) -> str:
    """The first note under the inputs: which cost per unit the figures use,
    or, with none, why there is no benchmark (`missing`, as in
    hub_cost.Benchmark: 'no_blueprint' or 'unpriced')."""
    if cost is None or source is None:
        return _NO_COST_NOTES.get(missing, NO_COST_NOTE)
    return f"Cost used: {fmt_isk(cost)} each — {_COST_SOURCES[source].format(me=me)}."


def _pct(v: float) -> str:
    """A percentage as the fields show it: 3.37, 2.5, 8, 100."""
    return f"{v:.4f}".rstrip("0").rstrip(".")


def qty_note(qty: int) -> str:
    """For a quantity that could not be used as typed (the field keeps it)."""
    return f"Quantity used: {qty}."


def fee_note(label: str, pct: float) -> str:
    """For a fee that could not be used as typed: 'Sales tax used: 3.37%.'"""
    return f"{label} used: {_pct(pct)}%."


def hub_row(key: str, name: str, result: dict | None, route: dict | None, qty: int,
            origin_set: bool = False, history_to: date | None = None) -> dict:
    """`route` is None both when no starting system is set and when one is set
    but the hub cannot be reached from it; `origin_set` tells them apart.
    `history_to` is the last day this hub's history covers, given only when
    that is far enough behind to mention (see hub_analysis.history_state)."""
    if result is None:
        return {"key": key, "name": name, "unavailable": True}
    lw, now = result["list"], result["now"]
    stock_days = result["days_of_stock"]
    outliers = result["outliers"]
    return {
        "key": key, "name": name, "unavailable": False,
        "speed": result["speed"], "speed_label": SPEED_LABELS[result["speed"]],
        "supply": result["supply"],
        "supply_label": SUPPLY_LABELS.get(result["supply"], ""),
        "verdict": result["verdict"],
        "outlier_note": (f"{outliers} outlier{'s' if outliers != 1 else ''} ignored"
                         if outliers else ""),
        "history_note": f"history to {history_to.isoformat()}" if history_to else "",
        "lowest_sell": ("Nothing listed" if result["nothing_listed"]
                        else fmt_isk(result["lowest_sell"])),
        "highest_buy": fmt_isk(result["highest_buy"]),
        "demand_30": fmt_rate(result["demand_30"]),
        "demand_7": fmt_rate(result["demand_7"]),
        "trend": result["trend"],
        "stock": str(result["stock"]),
        "stock_days": f"{fmt_rate(stock_days)} days" if stock_days is not None else "",
        "lw_profit": fmt_signed(lw["profit_each"]), "lw_sign": _sign(lw["profit_each"]),
        "lw_margin": f"{lw['margin'] * 100:.1f}%" if lw["margin"] is not None else "—",
        "lw_days": fmt_days_range(lw["days_best"], lw["days_typical"]),
        "lw_batch": fmt_signed(lw["batch_profit"]),
        "now_avg": fmt_isk(now["avg_price"]) if now["fillable"] else "no buyers",
        "now_fill": f"fills {now['fillable']} of {qty}" if now["fillable"] else "",
        "now_profit": fmt_signed(now["profit_each"]), "now_sign": _sign(now["profit_each"]),
        "now_batch": fmt_signed(now["batch_profit"]),
        "route": (f"{route['jumps']} j" if route
                  else "no route" if origin_set else ""),
        "route_note": ("highsec" if route["highsec"] else "leaves highsec") if route else "",
    }


def _tag_text(result: dict) -> str:
    parts = [SPEED_LABELS[result["speed"]]]
    if result["supply"]:
        parts.append(SUPPLY_LABELS[result["supply"]])
    return " · ".join(parts)


def pick_boxes(picks: dict, results: dict[str, dict | None], names: dict[str, str]) -> list[dict]:
    """The 'fastest sale' / 'best margin' boxes above the hubs. Every key in
    `picks` must be present with a non-None result in `results` and a name in
    `names` (which `hub_analysis.best_picks` guarantees)."""
    fastest, margin = picks["fastest"], picks["margin"]
    boxes = []

    def _when(r):
        """'under a day', 'up to 8 days', 'about 2 – 5 days': the same
        numbers as the table's days column (hub_format)."""
        best, typical = r["list"]["days_best"], r["list"]["days_typical"]
        if best is None or typical is None:  # no sales in the window: no estimate
            return ""
        return days_phrase(best, typical, approx="about")

    def _profit(r):
        lw = r["list"]
        pct = f" ({lw['margin'] * 100:.1f}%)" if lw["margin"] is not None else ""
        return f"{fmt_signed(lw['profit_each'])} each{pct}"

    if fastest is not None and fastest == margin:
        r = results[fastest]
        boxes.append({"k": "Fastest sale and best margin",
                      "v": f"{names[fastest]} — {_when(r)}" if _when(r) else names[fastest],
                      "d": f"{_profit(r)}. {_tag_text(r)}."})
        return boxes
    if fastest is not None:
        r = results[fastest]
        detail = _tag_text(r) + "."
        if r["list"]["profit_each"] is not None:
            detail = f"{_profit(r)}. {detail}"
        boxes.append({"k": "Fastest sale",
                      "v": f"{names[fastest]} — {_when(r)}" if _when(r) else names[fastest],
                      "d": detail})
    if margin is not None:
        r = results[margin]
        when = _when(r)
        detail = _tag_text(r) + "."
        if when:
            detail += f" {when[0].upper()}{when[1:]}."
        boxes.append({"k": "Best margin",
                      "v": f"{names[margin]} — {_profit(r)}", "d": detail})
    return boxes
