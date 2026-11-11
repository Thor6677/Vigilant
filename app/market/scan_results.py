"""Hub Scan results: computing rows from summaries, and the saved result per
(user, list). The only reader and writer of `market_scan_result`."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MarketScanResult, MarketScanSummary
from app.esi.market import APPRAISAL_HUBS
from app.market import scan_fetch, scan_rules

SUMMARY_MAX_AGE = timedelta(days=2)
CATEGORY_RESULT_MAX_IDLE = timedelta(days=30)
OPENED_STAMP_INTERVAL = timedelta(hours=1)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    budget_isk: float
    share_pct: float
    sales_tax_pct: float
    broker_fee_pct: float
    origin_system_id: int | None = None
    origin_name: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _utc(dt: datetime) -> datetime:
    """Aware UTC. Naive is read as UTC (what SQLite hands back)."""
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _naive(dt: datetime) -> datetime:
    """Naive UTC, the form the DateTime columns store and compare."""
    return _utc(dt).replace(tzinfo=None)


async def load_summaries(db: AsyncSession, type_ids: list[int]) -> dict[tuple[int, int], MarketScanSummary]:
    regions = [h["region_id"] for h in APPRAISAL_HUBS.values()]
    out: dict[tuple[int, int], MarketScanSummary] = {}
    for i in range(0, len(type_ids), 400):
        for row in (await db.execute(
            select(MarketScanSummary).where(
                MarketScanSummary.type_id.in_(type_ids[i:i + 400]),
                MarketScanSummary.region_id.in_(regions))
        )).scalars():
            out[(row.region_id, row.type_id)] = row
    return out


def compute_rows(summaries: dict, type_ids: list[int], names: dict[int, str],
                 benchmarks: dict, settings: Settings, failed: set[int],
                 now: datetime) -> list[dict]:
    """One row per type id, in the order given. `benchmarks[type_id]` is a
    `hub_cost.Benchmark` (or missing: no build cost). An ok row also says
    whether the best hub's sales history is a few days behind
    (`history_behind`) and whether its sell ladder was cut at
    scan_fetch.MAX_LEVELS (`stock_truncated`)."""
    rows = []
    for type_id in type_ids:
        base = {"type_id": type_id, "name": names.get(type_id, str(type_id))}
        if type_id in failed:
            rows.append({**base, "status": "unavailable"})
            continue
        # A key for every hub; None where there is no usable summary.
        figs = {key: scan_fetch.figures(summaries.get((hub["region_id"], type_id)),
                                        hub["station_id"], now)
                for key, hub in APPRAISAL_HUBS.items()}
        bench = benchmarks.get(type_id)
        best = scan_rules.best_for_item(
            figs, bench.cost if bench else None, settings.budget_isk,
            settings.share_pct / 100, settings.sales_tax_pct / 100,
            settings.broker_fee_pct / 100)
        if best["status"] == "ok":
            best["copy_only"] = bool(bench and bench.copy_only)
            fig = figs[best["hub"]]
            best["history_behind"] = bool(fig.get("behind"))
            best["stock_truncated"] = bool(fig.get("ladder_truncated"))
        rows.append({**base, **best})
    return rows


async def _find(db: AsyncSession, user_id: int, kind: str, ref: int) -> MarketScanResult | None:
    return (await db.execute(
        select(MarketScanResult).where(MarketScanResult.user_id == user_id,
                                       MarketScanResult.list_kind == kind,
                                       MarketScanResult.list_ref == ref)
    )).scalar_one_or_none()


def _dumps(value) -> str:
    # allow_nan=False: a NaN/Infinity would be invalid JSON; fail loudly instead.
    return json.dumps(value, allow_nan=False)


async def save(db: AsyncSession, user_id: int, kind: str, ref: int, *, label: str,
               status: str, item_total: int, item_scanned: int, settings: Settings,
               notes: list[str], routes: dict, rows: list[dict], now: datetime) -> None:
    # Serialise first so a bad value raises before the session is touched.
    settings_json, notes_json = _dumps(settings.as_dict()), _dumps(notes)
    routes_json, rows_json = _dumps(routes), _dumps(rows)
    stamp = _naive(now)
    values = {
        "label": label[:200], "status": status, "scanned_at": stamp, "opened_at": stamp,
        "item_total": item_total, "item_scanned": item_scanned,
        "settings_json": settings_json, "notes_json": notes_json,
        "routes_json": routes_json, "rows_json": rows_json,
    }
    # One atomic upsert on the unique key: two racing saves cannot both insert.
    stmt = sqlite_insert(MarketScanResult).values(
        user_id=user_id, list_kind=kind, list_ref=ref, **values)
    await db.execute(stmt.on_conflict_do_update(
        index_elements=["user_id", "list_kind", "list_ref"], set_=values))
    await db.commit()


async def load(db: AsyncSession, user_id: int, kind: str, ref: int,
               now: datetime | None = None) -> dict | None:
    """The saved result as plain data (`scanned_at` aware UTC), or None.
    May write: stamps `opened_at` when the stored stamp is at least
    OPENED_STAMP_INTERVAL old. The result is built before any write, and a
    failed stamp is rolled back and logged, never raised."""
    row = await _find(db, user_id, kind, ref)
    if row is None:
        return None
    result = {
        "kind": row.list_kind, "ref": row.list_ref, "label": row.label,
        "status": row.status, "scanned_at": _utc(row.scanned_at),
        "item_total": row.item_total, "item_scanned": row.item_scanned,
        "settings": json.loads(row.settings_json), "notes": json.loads(row.notes_json),
        "routes": json.loads(row.routes_json), "rows": json.loads(row.rows_json),
    }
    stamp = _naive(now or datetime.now(timezone.utc))
    if stamp - row.opened_at >= OPENED_STAMP_INTERVAL:
        try:
            row.opened_at = stamp
            await db.commit()
        except Exception:
            log.warning("Hub Scan: could not stamp opened_at", exc_info=True)
            try:
                await db.rollback()
            except Exception:
                log.warning("Hub Scan: rollback after failed stamp also failed", exc_info=True)
    return result


async def exists(db: AsyncSession, user_id: int, kind: str, ref: int) -> bool:
    """Whether the user has a saved result for the list. Reads one id only
    and writes nothing (unlike `load`, which may stamp `opened_at`)."""
    return (await db.execute(
        select(MarketScanResult.id).where(MarketScanResult.user_id == user_id,
                                          MarketScanResult.list_kind == kind,
                                          MarketScanResult.list_ref == ref).limit(1)
    )).first() is not None


async def delete_for(db: AsyncSession, user_id: int, kind: str, ref: int, *,
                     commit: bool = True) -> None:
    """`commit=False` leaves the delete in the caller's transaction (deleting
    a watchlist removes the list, its items and its result together)."""
    await db.execute(delete(MarketScanResult).where(
        MarketScanResult.user_id == user_id, MarketScanResult.list_kind == kind,
        MarketScanResult.list_ref == ref))
    if commit:
        await db.commit()


async def cleanup(db: AsyncSession, now: datetime) -> tuple[int, int]:
    """(summaries deleted, results deleted). A summary goes when BOTH parts are
    older than SUMMARY_MAX_AGE (or absent). Meant to run from the daily
    scheduler. A summary row always carries at least one stamped part once
    written, so a row mid-scan is not removed."""
    now = _naive(now)
    cutoff = now - SUMMARY_MAX_AGE
    s = await db.execute(delete(MarketScanSummary).where(
        or_(MarketScanSummary.history_fetched_at.is_(None),
            MarketScanSummary.history_fetched_at < cutoff),
        or_(MarketScanSummary.orders_fetched_at.is_(None),
            MarketScanSummary.orders_fetched_at < cutoff)))
    r = await db.execute(delete(MarketScanResult).where(
        MarketScanResult.list_kind == "category",
        MarketScanResult.opened_at < now - CATEGORY_RESULT_MAX_IDLE))
    await db.commit()
    return s.rowcount or 0, r.rowcount or 0
