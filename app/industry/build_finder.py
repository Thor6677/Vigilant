"""Build-profitability finder (Phase 4 Task 4) — pure ranking math.

Given a set of buildable products (each with its blueprint's per-run material
list and per-run output quantity) plus a global {type_id: unit price} map, rank
them by manufacturing margin. All I/O (SDE queries, the ESI price map) is done
by the route in `app/routes/industry.py`; this module is deliberately pure so
the ranking is directly unit-testable against fixture costs/prices.

Cost engine reuse: `build_cost_per_unit` calls `calc_material` from
`app.industry.manufacturing` — the exact same ME/structure/rig/security modifier
math the manufacturing calculator uses. No modifier math is duplicated here.

**Per-unit accounting (the trap):** a blueprint run consumes its material list
once and yields `product_quantity` units of the product (ammo, charges, and many
components yield >1 per run). So cost/unit = (sum of one run's material costs) ÷
product_quantity, and the margin is compared against the product's *unit* market
price. Skipping the divide inflates per-unit cost ~100x for those items.

**Unpriced handling (mirrors `app/market/lp.py`):** if the product is unpriced,
OR any required material is unpriced, the row is EXCLUDED from the ranking
(shown with "—", sorted last) rather than zero-filled. Treating an unpriced
material as free would understate cost and float phantom-profit items to the top
— the exact failure the LP tool guards against.

**Invention overhead (optional):** `rank_builds` accepts an optional
`invention` dict keyed by `product_type_id` —
`{"overhead_per_unit": float | None, "invented_me": int, "skill_missing": bool}`.
The overhead itself (datacores + decryptor + failed-attempt expectation) is
precomputed upstream by the route via `app.industry.invention` — this module
just consumes the number. For a product present in the dict, the build-cost
half of the row is recomputed at `invented_me` (an invented T2 BPC starts at
ME2, not necessarily the page's chosen ME) instead of the page `me`, then
`invention_overhead` is added on top. A `None` overhead (e.g. an unpriced
decryptor) forces the row unpriced — never silently zero-costed — the same
"excluded, sorts last" convention as an unpriced material. Products absent
from the dict, or when `invention` is omitted/`None` entirely, behave
exactly as before this feature existed.
"""
from __future__ import annotations

from app.industry.manufacturing import calc_material


def build_cost_per_unit(
    materials: list[dict],
    product_quantity: int,
    me: int,
    struct_mat: float,
    rig_mat_base: float,
    sec_mult: float,
    price_map: dict[int, float],
) -> float | None:
    """Manufacturing cost for ONE unit of the product, or None if any required
    material is unpriced (→ the whole build is treated as unpriced).

    `materials` is the blueprint's per-run material list:
    `[{"type_id": int, "quantity": int}, ...]`. Quantities are adjusted per run
    (`runs=1`) via the shared `calc_material` engine, summed at market price,
    then divided by `product_quantity` (per-run output, min 1).
    """
    if product_quantity < 1:
        product_quantity = 1
    run_cost = 0.0
    for m in materials:
        price = price_map.get(m["type_id"])
        if price is None:
            return None
        adj = calc_material(m["quantity"], 1, me, struct_mat, rig_mat_base, sec_mult)
        run_cost += price * adj
    return run_cost / product_quantity


def rank_builds(
    products: list[dict],
    me: int,
    struct_mat: float,
    rig_mat_base: float,
    sec_mult: float,
    price_map: dict[int, float],
    invention: dict[int, dict] | None = None,
) -> list[dict]:
    """Rank buildable products by margin % descending.

    Each `products` entry:
      `{"product_type_id", "product_name", "blueprint_type_id",
        "product_quantity", "materials": [{"type_id", "quantity"}, ...]}`

    A row is `priced` only when its product has a market price AND its build
    cost resolved to a positive number (all materials priced, cost > 0).
    Unpriced rows keep their place in the table (cost/sell/margin shown as
    None) but sort AFTER every priced row — same convention as the LP tool.

    `invention` (optional) is keyed by `product_type_id` — see the module
    docstring. For a product present in it, build cost is computed at
    `invented_me` instead of `me` and `overhead_per_unit` is added; a `None`
    overhead forces the row unpriced. Products absent from `invention` (or
    `invention=None`) are unaffected.
    """
    invention = invention or {}
    rows = []
    for p in products:
        product_id = p["product_type_id"]
        inv = invention.get(product_id)

        invention_overhead = None
        invented_me = None
        skill_missing = False
        effective_me = me
        if inv is not None:
            invention_overhead = inv.get("overhead_per_unit")
            invented_me = inv.get("invented_me")
            skill_missing = bool(inv.get("skill_missing", False))
            if invented_me is not None:
                effective_me = invented_me

        cost = build_cost_per_unit(
            p.get("materials") or [], p.get("product_quantity") or 1,
            effective_me, struct_mat, rig_mat_base, sec_mult, price_map,
        )
        if inv is not None:
            if invention_overhead is None or cost is None:
                cost = None
            else:
                cost = cost + invention_overhead

        sell = price_map.get(product_id)
        priced = cost is not None and cost > 0 and sell is not None
        margin_isk = (sell - cost) if priced else None
        margin_pct = (margin_isk / cost * 100.0) if priced else None
        rows.append({
            "product_type_id": product_id,
            "product_name": p["product_name"],
            "blueprint_type_id": p.get("blueprint_type_id"),
            "cost_per_unit": cost,
            "sell_per_unit": sell,
            "margin_isk": margin_isk,
            "margin_pct": margin_pct,
            "priced": priced,
            "invention_overhead": invention_overhead,
            "invented_me": invented_me,
            "skill_missing": skill_missing,
        })
    rows.sort(key=lambda r: (r["margin_pct"] is None, -(r["margin_pct"] or 0.0)))
    return rows
