"""Delete data Vigilant collected under permissions a user has withdrawn.

Runs only when the user ticks "also delete data already collected" while
narrowing a character's permissions (Account page); withdrawing a permission
without it stops all FUTURE reads — the token no longer carries the scope and
ESIClient's guard refuses the call — but keeps what was already stored.

Scope of the purge is the character's OWN data. Corporation-level data (corp
wallet history, corp inventory) is shared by the whole corporation and may
have been fetched with another member's token, so it is left alone; the notice
on the Account page says so.

The character's slice of the raw ESI response cache is cleared on EVERY
narrowing, purge or not: it is only a cache, and nothing withdrawn should stay
in it.
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


async def purge_permissions(db: AsyncSession, character_id: int,
                            keys: Iterable[str]) -> dict[str, int]:
    """Delete this character's stored data for the given permission keys.

    Caller commits. Returns row counts per table for the audit log.
    """
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

    async def _delete(model, label):
        res = await db.execute(delete(model).where(model.character_id == cid))
        counts[label] = res.rowcount or 0

    if "wallet" in keys:
        await _delete(WalletSnapshot, "wallet_snapshots")
        await _delete(WalletTransaction, "wallet_transactions")
    if "assets" in keys:
        await _delete(CharacterAssetCache, "asset_cache")
    if "industry" in keys:
        await _delete(IndustryJobHistory, "industry_job_history")
    if "mining" in keys:
        await _delete(MiningLedgerEntry, "mining_ledger_entries")
    if "corp_roles" in keys:
        await _delete(CharacterCorpRoles, "corp_roles")

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

    counts["esi_cache"] = await clear_esi_cache(db, cid)
    logger.info("purged withdrawn permissions %s for character %s: %s", sorted(keys), cid, counts)
    return counts
