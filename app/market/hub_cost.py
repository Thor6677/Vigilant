"""Benchmark build cost for the hub comparison.

One product, costed the way Build Finder costs a row: materials at EVE's
global average reference price (`/markets/prices/`), neutral facility. The
blueprint is assumed researched to ME 10, or ME 2 when the product is
invented (the base ME of an invented copy). Job install fees and invention
overhead are NOT included — the page labels the figure with these assumptions
(the wording is `hub_view.cost_note`).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.sde_models import SDEBlueprintInfo, SDEBlueprintMaterial
from app.industry.build_finder import build_cost_per_unit
from app.market import lp as market_lp
from app.sde import lookup as sde

BENCHMARK_ME = 10
INVENTED_ME = 2


async def benchmark(db: AsyncSession, product_type_id: int) -> tuple[float | None, int]:
    """(cost per unit | None, ME assumed). None when the product has no
    manufacturing blueprint or any material is unpriced. With several
    blueprints for one product, the lowest blueprint_type_id that actually
    has manufacturing materials is used."""
    blueprints = (await db.execute(
        select(SDEBlueprintInfo.blueprint_type_id, SDEBlueprintInfo.product_quantity)
        .where(SDEBlueprintInfo.product_type_id == product_type_id)
        .order_by(SDEBlueprintInfo.blueprint_type_id)
    )).all()
    bp = rows = None
    for candidate in blueprints:
        rows = (await db.execute(
            select(SDEBlueprintMaterial.material_type_id, SDEBlueprintMaterial.quantity)
            .where(SDEBlueprintMaterial.blueprint_type_id == candidate.blueprint_type_id)
            .where(SDEBlueprintMaterial.activity_id == 1)
        )).all()
        if rows:
            bp = candidate
            break
    if bp is None:
        return None, BENCHMARK_ME
    materials = [{"type_id": tid, "quantity": qty} for tid, qty in rows]
    invented = await sde.get_invention_data(db, [product_type_id])
    me = INVENTED_ME if invented else BENCHMARK_ME
    price_map = await market_lp.get_price_map(db)
    cost = build_cost_per_unit(materials, bp.product_quantity or 1, me, 1.0, 0.0, 1.0, price_map)
    return cost, me


async def benchmark_cost(db: AsyncSession, product_type_id: int) -> float | None:
    return (await benchmark(db, product_type_id))[0]
