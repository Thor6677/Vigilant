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

**No invention/decryptor math:** T2 invention costs (datacores, decryptors,
invention probability) are out of scope for this task and deliberately not
modeled — the finder prices T2 builds off their raw BPC material list only. A
follow-up ticket covers invention. This is surfaced in the page footnote.
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
) -> list[dict]:
    """Rank buildable products by margin % descending.

    Each `products` entry:
      `{"product_type_id", "product_name", "blueprint_type_id",
        "product_quantity", "materials": [{"type_id", "quantity"}, ...]}`

    A row is `priced` only when its product has a market price AND its build
    cost resolved to a positive number (all materials priced, cost > 0).
    Unpriced rows keep their place in the table (cost/sell/margin shown as
    None) but sort AFTER every priced row — same convention as the LP tool.
    """
    rows = []
    for p in products:
        cost = build_cost_per_unit(
            p.get("materials") or [], p.get("product_quantity") or 1,
            me, struct_mat, rig_mat_base, sec_mult, price_map,
        )
        sell = price_map.get(p["product_type_id"])
        priced = cost is not None and cost > 0 and sell is not None
        margin_isk = (sell - cost) if priced else None
        margin_pct = (margin_isk / cost * 100.0) if priced else None
        rows.append({
            "product_type_id": p["product_type_id"],
            "product_name": p["product_name"],
            "blueprint_type_id": p.get("blueprint_type_id"),
            "cost_per_unit": cost,
            "sell_per_unit": sell,
            "margin_isk": margin_isk,
            "margin_pct": margin_pct,
            "priced": priced,
        })
    rows.sort(key=lambda r: (r["margin_pct"] is None, -(r["margin_pct"] or 0.0)))
    return rows
