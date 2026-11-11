"""Hub comparison display strings. Pure: results in, template-ready dicts out."""
from __future__ import annotations

import math

SPEED_LABELS = {"fast": "Fast", "steady": "Steady", "slow": "Slow", "thin": "Thin market"}
SUPPLY_LABELS = {"under": "Under-supplied", "saturated": "Saturated"}
_SUFFIX = {"k": 1e3, "m": 1e6, "b": 1e9}


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


def fmt_rate(v: float) -> str:
    if v >= 10:
        return f"{v:.0f}"
    if 0 < v < 0.05:
        return "<0.1"
    return f"{v:.1f}".rstrip("0").rstrip(".")


def _day(v: float) -> str:
    n = math.floor(v + 0.5)  # round half up, not banker's rounding
    return "<1" if n < 1 else str(n)


def fmt_days_range(best: float | None, typical: float | None) -> str:
    if best is None or typical is None:
        return "—"
    lo, hi = _day(best), _day(typical)
    return lo if lo == hi else f"{lo} – {hi}"


def parse_isk(raw) -> float | None:
    """'180000000', '180,000,000', '180m', '1.2B' → float. None when blank,
    unparseable, negative or not finite."""
    s = str(raw or "").replace(",", "").replace(" ", "").lower()
    if not s:
        return None
    mult = 1.0
    if s[-1] in _SUFFIX:
        mult, s = _SUFFIX[s[-1]], s[:-1]
    try:
        v = float(s) * mult
    except ValueError:
        return None
    if not math.isfinite(v) or v < 0:
        return None
    return v


def hub_row(key: str, name: str, result: dict | None, route: dict | None, qty: int) -> dict:
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
        "route": f"{route['jumps']} j" if route else "",
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

    def _days(r):
        return fmt_days_range(r["list"]["days_best"], r["list"]["days_typical"])

    def _when(r):
        d = _days(r)
        return "under a day" if d == "<1" else f"about {d} days"

    def _profit(r):
        lw = r["list"]
        pct = f" ({lw['margin'] * 100:.1f}%)" if lw["margin"] is not None else ""
        return f"{fmt_signed(lw['profit_each'])} each{pct}"

    if fastest is not None and fastest == margin:
        r = results[fastest]
        boxes.append({"k": "Fastest sale and best margin",
                      "v": f"{names[fastest]} — {_when(r)}",
                      "d": f"{_profit(r)}. {_tag_text(r)}."})
        return boxes
    if fastest is not None:
        r = results[fastest]
        detail = _tag_text(r) + "."
        if r["list"]["profit_each"] is not None:
            detail = f"{_profit(r)}. {detail}"
        boxes.append({"k": "Fastest sale",
                      "v": f"{names[fastest]} — {_when(r)}", "d": detail})
    if margin is not None:
        r = results[margin]
        boxes.append({"k": "Best margin",
                      "v": f"{names[margin]} — {_profit(r)}",
                      "d": f"{_tag_text(r)}. " + ("Under a day." if _days(r) == "<1"
                                             else f"About {_days(r)} days.")})
    return boxes
