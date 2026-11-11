"""Benchmark build cost for the hub comparison.

One product, costed the way Build Finder costs a row: materials at EVE's
global average reference price (`/markets/prices/`), neutral facility. The
blueprint is assumed researched to ME 10, or ME 2 when the product is
invented (the base ME of an invented copy). Job install fees and invention
overhead are NOT included — the page labels the figure with these assumptions
(the wording is `hub_view.cost_note`).

Some blueprints exist only as copies (loyalty stores, drops): the blueprint
itself is not sold on the market and the product is not invented. What the
copy costs is not in the benchmark either, and can be most of the real cost,
so `Benchmark.copy_only` lets the page say so.
"""
from __future__ import annotations

from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.sde_models import SDEBlueprintInfo, SDEBlueprintMaterial, SDEType
from app.industry.build_finder import build_cost_per_unit
from app.market import lp as market_lp
from app.sde import lookup as sde

BENCHMARK_ME = 10
INVENTED_ME = 2

# Why a benchmark has no cost (Benchmark.missing); hub_view words each.
NO_BLUEPRINT = "no_blueprint"   # nothing manufactures this item
UNPRICED = "unpriced"           # a material has no price (or the price list is empty)


class Benchmark(NamedTuple):
    cost: float | None          # per unit, or None (see `missing`)
    me: int                     # the ME assumed
    missing: str | None = None  # NO_BLUEPRINT or UNPRICED when cost is None
    copy_only: bool = False     # the blueprint is only obtainable as a copy


async def benchmark(db: AsyncSession, product_type_id: int,
                    price_map: dict | None = None) -> Benchmark:
    """The benchmark cost per unit and the ME assumed. No cost when the
    product has no manufacturing blueprint (`missing` NO_BLUEPRINT) or a
    material has no price, including when the price list could not be loaded
    and is empty (UNPRICED). With several blueprints for one product, the
    lowest blueprint_type_id that actually has manufacturing materials is
    used. `copy_only` is set when that blueprint has no market group and the
    product is not invented. `price_map` is the price list when the caller has
    already fetched it (a scan fetches it once); None fetches it here."""
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
        return Benchmark(None, BENCHMARK_ME, NO_BLUEPRINT)
    materials = [{"type_id": tid, "quantity": qty} for tid, qty in rows]
    invented = await sde.get_invention_data(db, [product_type_id])
    me = INVENTED_ME if invented else BENCHMARK_ME
    on_market = (await db.execute(
        select(SDEType.market_group_id).where(SDEType.type_id == bp.blueprint_type_id)
    )).scalar_one_or_none()
    copy_only = not invented and on_market is None
    if price_map is None:
        price_map = await market_lp.get_price_map(db)
    cost = build_cost_per_unit(materials, bp.product_quantity or 1, me, 1.0, 0.0, 1.0, price_map)
    return Benchmark(cost, me, UNPRICED if cost is None else None, copy_only)
