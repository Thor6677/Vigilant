"""Completed-job build-cost valuation (Industry P&L, T-041 item 2).

Cost basis is the job's COMPLETION DATE (user decision in the spec): each
material is valued at the Jita daily average for the nearest day at or
before completion (within 7 days), falling back to the current global
reference price. Basis is tracked per job — "history" only when every
material priced from history; any reference fallback downgrades the whole
job to "reference".

ME is assumed (ESI jobs don't expose blueprint ME): ME 10 manufacturing,
ME 0 reactions — surfaced in the /market/pnl footnote. Facility bonuses
are assumed neutral (jobs don't record their structure/rigs).
"""
from __future__ import annotations

from datetime import date as date_cls, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MarketHistory
from app.industry.manufacturing import calc_material

ME_MANUFACTURING = 10
ME_REACTION = 0
JITA_REGION_ID = 10000002
_HISTORY_WINDOW_DAYS = 7


def job_build_cost(materials, runs, me, install_cost, price_fn):
    """Total build cost for one job, or (None, None) if any material is
    unpriceable. `price_fn(type_id) -> (price, basis) | (None, None)`.
    Returns (cost, basis) where basis is "history" iff every material was
    history-priced, else "reference"."""
    total = float(install_cost or 0.0)
    basis = "history"
    for m in materials:
        price, b = price_fn(m["type_id"])
        if price is None:
            return None, None
        qty = calc_material(m["quantity"], runs, me, 1.0, 0.0, 1.0)
        total += price * qty
        if b != "history":
            basis = "reference"
    return total, basis


async def prices_at_bulk(
    db: AsyncSession,
    type_ids: list[int],
    on_date: date_cls,
    reference_map: dict[int, float],
) -> dict[int, tuple[float, str]]:
    """{type_id: (price, basis)} for every resolvable type. History rows
    (Jita, nearest day <= on_date within the window) win; reference map is
    the fallback; unresolvable types are omitted."""
    out: dict[int, tuple[float, str]] = {}
    if type_ids:
        cutoff = on_date - timedelta(days=_HISTORY_WINDOW_DAYS)
        rows = (await db.execute(
            select(MarketHistory.type_id, MarketHistory.date, MarketHistory.average)
            .where(MarketHistory.region_id == JITA_REGION_ID)
            .where(MarketHistory.type_id.in_(type_ids))
            .where(MarketHistory.date <= on_date)
            .where(MarketHistory.date >= cutoff)
            .order_by(MarketHistory.type_id, MarketHistory.date.desc())
        )).all()
        for tid, _d, avg in rows:
            if tid not in out and avg is not None:
                out[tid] = (float(avg), "history")
    for tid in type_ids:
        if tid not in out and tid in reference_map:
            out[tid] = (float(reference_map[tid]), "reference")
    return out
