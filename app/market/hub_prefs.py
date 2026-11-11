"""Per-user hub comparison preferences. A missing row means defaults, so
existing accounts need no migration."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MarketHubPrefs
from app.market.pnl import BROKER_FEE_RATE, SALES_TAX_RATE

DEFAULT_SALES_TAX_PCT = round(SALES_TAX_RATE * 100, 4)
DEFAULT_BROKER_FEE_PCT = round(BROKER_FEE_RATE * 100, 4)


DEFAULT_SCAN_BUDGET_ISK = 500_000_000.0
MIN_SCAN_BUDGET_ISK = 1_000_000.0
MAX_SCAN_BUDGET_ISK = 100_000_000_000.0
DEFAULT_MARKET_SHARE_PCT = 20.0


@dataclass(frozen=True)
class HubPrefs:
    sales_tax_pct: float
    broker_fee_pct: float
    origin_system_id: int | None
    scan_budget_isk: float = DEFAULT_SCAN_BUDGET_ISK
    market_share_pct: float = DEFAULT_MARKET_SHARE_PCT


def clamp_budget(raw, default: float = DEFAULT_SCAN_BUDGET_ISK) -> float:
    """ISK in 1M–100B, or `default` when `raw` is blank or not a number."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return max(MIN_SCAN_BUDGET_ISK, min(MAX_SCAN_BUDGET_ISK, v))


def clamp_share(raw, default: float = DEFAULT_MARKET_SHARE_PCT) -> float:
    """A percentage in 1–100, or `default`."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return max(1.0, min(100.0, v))


def clamp_pct(raw, default: float) -> float:
    """A percentage in 0–100, or `default` when `raw` is blank or not a number."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return max(0.0, min(100.0, v))


async def get_prefs(db: AsyncSession, user_id: int) -> HubPrefs:
    row = (await db.execute(
        select(MarketHubPrefs).where(MarketHubPrefs.user_id == user_id)
    )).scalar_one_or_none()
    if row is None:
        return HubPrefs(DEFAULT_SALES_TAX_PCT, DEFAULT_BROKER_FEE_PCT, None)
    return HubPrefs(row.sales_tax_pct, row.broker_fee_pct, row.origin_system_id,
                    row.scan_budget_isk or DEFAULT_SCAN_BUDGET_ISK,
                    row.market_share_pct or DEFAULT_MARKET_SHARE_PCT)


async def save_prefs(db: AsyncSession, user_id: int, sales_tax_pct, broker_fee_pct,
                     origin_system_id: int | None) -> HubPrefs:
    tax = clamp_pct(sales_tax_pct, DEFAULT_SALES_TAX_PCT)
    broker = clamp_pct(broker_fee_pct, DEFAULT_BROKER_FEE_PCT)
    row = (await db.execute(
        select(MarketHubPrefs).where(MarketHubPrefs.user_id == user_id)
    )).scalar_one_or_none()
    if row is None:
        row = MarketHubPrefs(user_id=user_id)
        db.add(row)
    row.sales_tax_pct = tax
    row.broker_fee_pct = broker
    row.origin_system_id = origin_system_id
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return HubPrefs(tax, broker, origin_system_id)


async def save_scan_prefs(db: AsyncSession, user_id: int, budget_isk, share_pct) -> None:
    """Budget per item and market share only; fees and origin are untouched."""
    row = (await db.execute(
        select(MarketHubPrefs).where(MarketHubPrefs.user_id == user_id)
    )).scalar_one_or_none()
    if row is None:
        row = MarketHubPrefs(user_id=user_id,
                             sales_tax_pct=DEFAULT_SALES_TAX_PCT,
                             broker_fee_pct=DEFAULT_BROKER_FEE_PCT)
        db.add(row)
    row.scan_budget_isk = clamp_budget(budget_isk)
    row.market_share_pct = clamp_share(share_pct)
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
