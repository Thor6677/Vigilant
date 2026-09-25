"""Stop using, and optionally delete, data from permissions a user withdrew.

Two tiers, because they answer different questions:

* **Live state** — the current-value caches features read as if they were
  fresh: the dashboard cache columns (wallet balance, location, clones, …),
  the asset list, the character's corp roles, and the raw ESI response cache.
  Cleared on EVERY narrowing, purge box or not. Left behind they would keep
  feeding features with frozen values — net-worth snapshots would record a
  months-old balance as today's, stockpiles would count assets Vigilant may no
  longer read, and stale corp roles would keep granting skill-plan edit rights
  the character can no longer prove.
* **History** — what Vigilant accumulated over time: wallet snapshots and
  transactions, industry job history, the mining ledger, and each net-worth
  row's component. Deleted only when the user ticks "also delete data already
  collected" (T-064: ask every time).

Scope is the character's OWN data. Corporation-level data (corp wallet
history, corp inventory) is shared by the whole corporation and may have been
fetched with another member's token, so it is left alone; the picker says so.
"""
from __future__ import annotations

import json
import logging
from typing import Iterable

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.cache import ESICache
from app.db.models import (
    CharacterAssetCache, CharacterCorpRoles, CharacterDashboardCache,
    IndustryJobHistory, MiningLedgerEntry, NetWorthSnapshot, WalletSnapshot,
    WalletTransaction,
)

logger = logging.getLogger(__name__)

# permission key -> dashboard-cache columns to clear, and the sync fields
# (FIELD_SCOPES keys in app/routes/dashboard.py) whose bookkeeping goes too.
_CACHE_COLUMNS: dict[str, tuple[str, ...]] = {
    "wallet": ("wallet",),
    "orders": ("orders_json",),
    "location": ("location_json",),
    "skills": ("skillqueue_json",),
    "clones": ("clones_json",),
    "industry": ("industry_json",),
    "planets": ("pi_json",),
    "contracts": ("contracts_json",),
    "notifications": ("notifications_json",),
    "mail": ("mail_json",),
}
_SYNC_FIELDS: dict[str, tuple[str, ...]] = {
    "wallet": ("wallet", "transactions"),
    "orders": ("orders",),
    "assets": ("assets",),
    "location": ("location",),
    "skills": ("skillqueue",),
    "clones": ("clones",),
    "industry": ("industry",),
    "planets": ("pi",),
    "contracts": ("contracts",),
    "notifications": ("notifications",),
    "corp_roles": ("roles",),
}
# Net-worth components each permission fed. The row's total is recomputed from
# what is left rather than the whole history being dropped.
_NETWORTH_COMPONENTS: dict[str, tuple[str, ...]] = {
    "wallet": ("wallet",),
    "orders": ("escrow",),
    "assets": ("assets_value", "unpriced_count"),
    "industry": ("industry_value",),
}


async def clear_esi_cache(db: AsyncSession, character_id: int) -> int:
    """Drop every cached authenticated ESI response for this character.

    Cache keys are "<hash>:@<principal>|<path…>" (app/db/cache.py), and the
    principal for a character token is CHARACTER:EVE:<id>.
    """
    marker = f"%:@CHARACTER:EVE:{int(character_id)}|%"
    res = await db.execute(delete(ESICache).where(ESICache.key.like(marker)))
    return res.rowcount or 0


async def clear_live_state(db: AsyncSession, character_id: int,
                           keys: Iterable[str]) -> dict[str, int]:
    """Drop the current-value caches fed by the given permissions. Runs on
    every narrowing. Caller commits. Returns counts for the audit log."""
    keys = set(keys)
    counts: dict[str, int] = {}
    cid = int(character_id)

    cache = (await db.execute(select(CharacterDashboardCache).where(
        CharacterDashboardCache.character_id == cid))).scalar_one_or_none()
    if cache is not None:
        cleared = [c for k in keys for c in _CACHE_COLUMNS.get(k, ())]
        for col in cleared:
            setattr(cache, col, None)
        fields = {f for k in keys for f in _SYNC_FIELDS.get(k, ())}
        for attr in ("field_synced_json", "sync_warnings_json"):
            raw = getattr(cache, attr)
            if raw and fields:
                try:
                    data = json.loads(raw)
                    for f in fields:
                        data.pop(f, None)
                    setattr(cache, attr, json.dumps(data))
                except (ValueError, TypeError):
                    setattr(cache, attr, None)
        counts["dashboard_cache_columns"] = len(cleared)

    if "assets" in keys:
        counts["asset_cache"] = await _delete(db, CharacterAssetCache, cid)
    if "corp_roles" in keys:
        counts["corp_roles"] = await _delete(db, CharacterCorpRoles, cid)
    counts["esi_cache"] = await clear_esi_cache(db, cid)
    logger.info("cleared live state for withdrawn permissions %s, character %s: %s",
                sorted(keys), cid, counts)
    return counts


async def _delete(db: AsyncSession, model, cid: int) -> int:
    res = await db.execute(delete(model).where(model.character_id == cid))
    return res.rowcount or 0


async def purge_history(db: AsyncSession, character_id: int,
                        keys: Iterable[str]) -> dict[str, int]:
    """Delete what Vigilant accumulated under the given permissions. Only when
    the user asked. Caller commits. Returns counts for the audit log."""
    keys = set(keys)
    counts: dict[str, int] = {}
    cid = int(character_id)

    if "wallet" in keys:
        counts["wallet_snapshots"] = await _delete(db, WalletSnapshot, cid)
        counts["wallet_transactions"] = await _delete(db, WalletTransaction, cid)
    if "industry" in keys:
        counts["industry_job_history"] = await _delete(db, IndustryJobHistory, cid)
    if "mining" in keys:
        counts["mining_ledger_entries"] = await _delete(db, MiningLedgerEntry, cid)

    components = [c for k in keys for c in _NETWORTH_COMPONENTS.get(k, ())]
    if components:
        values = {c: 0 for c in components}
        res = await db.execute(update(NetWorthSnapshot)
                               .where(NetWorthSnapshot.character_id == cid)
                               .values(**values))
        await db.execute(update(NetWorthSnapshot)
                         .where(NetWorthSnapshot.character_id == cid)
                         .values(total=NetWorthSnapshot.wallet + NetWorthSnapshot.assets_value
                                 + NetWorthSnapshot.escrow + NetWorthSnapshot.industry_value))
        counts["net_worth_rows_adjusted"] = res.rowcount or 0

    logger.info("purged history for withdrawn permissions %s, character %s: %s",
                sorted(keys), cid, counts)
    return counts
