"""Stop using, and optionally delete, data from permissions a user withdrew,
or from a character that left an account.

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

On a narrowing (ISS-072) the small live state is cleared in the request
(clear_live_state). The ESI response cache is not: finding a character's
entries is a scan of a large table, so it is cleared after the response
(clear_esi_cache_in_background) by the same batched helper the removal purge
uses. If the process stops before that runs, the entries simply expire: every
authenticated read checks the token's current scopes before it looks at the
cache (app/esi/scope_guard.py), so a withdrawn scope's entries can't be served
in the meantime. History, when asked for, is deleted in the request, in
batches, since it can be a year of wallet snapshots.

Scope is the character's OWN data. Corporation-level data (corp wallet
history, corp inventory) is shared by the whole corporation and may have been
fetched with another member's token, so it is left alone; the picker says so.

Taking a character off an account (ISS-060)
-------------------------------------------
Four paths do it: self-removal on the Account page, admin remove-character,
admin remove-user, and an EVE owner change (a transfer). All four call
remove_character_from_account(), and every table with a character_id column
is classified below (LIVE_STATE_TABLES, HISTORY_TABLES,
PER_CHARACTER_USER_TABLES, KEPT_TABLES, REMOVAL_TABLES), so a new one can't be
missed again: tests/test_character_removal.py fails on any that isn't.

* In the request: the small live state, the user's own rows about the
  character, and the character row itself are deleted, and a pending purge is
  recorded in `character_purges`. Token revocation stays with each caller
  (a transfer never revokes; see app/auth/routes.py's module docstring).
* In the background (run_due_purges, from the scheduler): the character's
  ESI response cache and, when asked, its history. Both can be large — a
  year of wallet snapshots is ~260k rows, and finding a character's
  esi_cache rows means scanning that whole table — and SQLite has one
  writer, so the deletes go in small batches, one short transaction each.
  History is always deleted for an admin removal or a transfer; on
  self-removal only when the user ticks the box.
* A purge only starts PURGE_DELAY after the removal: a sync already running
  can still write for up to the sync timeout, and waiting it out means its
  late rows are purged too. The sync itself also re-checks before its final
  commit (sync_must_not_write).
* If the character is linked again before its history is gone, the purge
  stops and keeps what is left, so the re-added pilot's new rows survive.
  Live-state sweeps are guarded the same way; esi_cache entries are always
  cleared, since a dropped cache entry is only a refetch.
* The transfer path is the exception: its caller links the character to the
  new owner in the same request, so a background purge would always find it
  "re-added" and the new owner would inherit the old owner's history. Its
  history is deleted in the request instead, still in batches, before the row
  goes (only its esi_cache clean-up is left to the background). If the
  process dies half way, the row is still there under the old owner, so the
  new owner's next sign-in detects the transfer again and finishes it.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import delete, exists, or_, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.cache import ESICache
from app.db.models import (
    AdminAuditLog, AsyncSessionLocal, Character, CharacterAssetCache, CharacterCorpRoles,
    CharacterDashboardCache, CharacterPurge, NetWorthSnapshot,
)

logger = logging.getLogger(__name__)

# permission key -> dashboard-cache columns to clear, and the sync fields
# (FIELD_SCOPES keys in app/routes/dashboard.py) whose bookkeeping goes too.
_CACHE_COLUMNS: dict[str, tuple[str, ...]] = {
    "wallet": ("wallet",),
    "orders": ("orders_json",),
    "location": ("location_json",),
    # T-073: skills_json (esi-skills.read_skills.v1) joins skillqueue_json
    # under the same "skills" permission key — withdrawing it clears both.
    "skills": ("skillqueue_json", "skills_json"),
    "clones": ("clones_json",),
    "industry": ("industry_json",),
    "planets": ("pi_json",),
    "contracts": ("contracts_json",),
    "notifications": ("notifications_json",),
    "mail": ("mail_json",),   # legacy column; live mail sits in the ESI cache, cleared below
}
_SYNC_FIELDS: dict[str, tuple[str, ...]] = {
    "wallet": ("wallet", "transactions"),
    "orders": ("orders",),
    "assets": ("assets",),
    "location": ("location",),
    "skills": ("skillqueue", "skills"),
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


def _esi_cache_marker(character_id: int) -> str:
    """LIKE pattern for a character's authenticated ESI cache entries.

    Cache keys are "<hash>:@<principal>|<path…>" (app/db/cache.py), and the
    principal for a character token is CHARACTER:EVE:<id>.
    """
    return f"%:@CHARACTER:EVE:{int(character_id)}|%"


async def clear_live_state(db: AsyncSession, character_id: int,
                           keys: Iterable[str]) -> dict[str, int]:
    """Drop the current-value caches fed by the given permissions. Runs on
    every narrowing. Caller commits. Returns counts for the audit log.

    Not the ESI response cache: the caller schedules
    clear_esi_cache_in_background for after the response (ISS-072)."""
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
    logger.info("cleared live state for withdrawn permissions %s, character %s: %s",
                sorted(keys), cid, counts)
    return counts


async def clear_esi_cache_in_background(bind, character_id: int) -> int | None:
    """Clear a character's ESI response cache after a narrowing (ISS-072).

    Scheduled by the SSO callback to run after its response, in its own
    session on `bind` (the request's engine). It is the removal purge's
    batched clear: a read-only key scan, then deletes by key in short
    transactions. It never writes to character_purges, which would tell the
    resync the callback queued to drop its results (sync_must_not_write).
    Failures are logged, never raised: the response has already gone.
    Returns the number of entries deleted, or None if it failed.
    """
    cid = int(character_id)
    try:
        async with AsyncSession(bind, expire_on_commit=False) as db:
            deleted = await _clear_esi_cache_batched(db, cid)
    except Exception as e:
        logger.warning("ESI cache clear after narrowing failed for character %s: %s: %s",
                       cid, type(e).__name__, e)
        return None
    logger.info("cleared %s ESI cache entries after narrowing, character %s", deleted, cid)
    return deleted


async def _delete(db: AsyncSession, model, cid: int) -> int:
    res = await db.execute(delete(model).where(model.character_id == cid))
    return res.rowcount or 0


# permission key -> history tables purge_history deletes a character's rows from.
_HISTORY_ROWS: dict[str, tuple[str, ...]] = {
    "wallet": ("wallet_snapshots", "wallet_transactions"),
    "industry": ("industry_job_history",),
    "mining": ("mining_ledger_entries",),
}


async def purge_history(db: AsyncSession, character_id: int,
                        keys: Iterable[str]) -> dict[str, int]:
    """Delete what Vigilant accumulated under the given permissions. Only when
    the user asked. Returns counts for the audit log.

    The rows go PURGE_BATCH_ROWS at a time, each batch its own short
    transaction committed on `db` (a year of wallet snapshots is ~260k rows,
    too many for one DELETE under SQLite's one write lock; ISS-072). Whatever
    the caller had pending goes in the first of those commits. The net-worth
    adjustment at the end is not committed: the caller commits it.

    The net-worth UPDATEs stay single statements: net_worth_snapshots holds
    one row per character per day (primary key character_id, date), so they
    touch at most one row for each day the character has been valued.
    """
    keys = set(keys)
    counts: dict[str, int] = {}
    cid = int(character_id)

    for key, tables in _HISTORY_ROWS.items():
        if key in keys:
            for table in tables:
                counts[table], _ = await _delete_in_batches(db, table, cid, only_if_gone=False)

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


# Tables that hold a user's own rows about one of their characters (tags,
# notes, farm settings, dismissed alerts), keyed by a character_id column.
# Every path that takes a character off an account calls
# purge_character_user_rows, so a feature only has to list its table here:
# self-removal, admin remove-character, admin remove-user, and an EVE owner
# change (_release_transferred). Rows keyed by user_id alone are covered on
# user removal by USER_OWNED_TABLES in app/routes/admin.py instead.
PER_CHARACTER_USER_TABLES: tuple[str, ...] = (
    "character_tags",  # T-074: pilot role tags and private note
    "dashboard_attention_dismissals",
    "skill_farm_pilots",  # T-073
)


async def purge_character_user_rows(db: AsyncSession, character_id: int) -> int:
    """Delete one character's rows from every PER_CHARACTER_USER_TABLES table.
    Returns the number of rows deleted. Doesn't commit; the caller's removal
    commits it together with the character row."""
    total = 0
    for table in PER_CHARACTER_USER_TABLES:
        res = await db.execute(
            text(f"DELETE FROM {table} WHERE character_id = :cid"), {"cid": character_id})
        total += res.rowcount or 0
    return total


# ── ISS-060: what taking a character off an account does to each table ──────
# Every table with a column named character_id or ending in _character_id is
# in exactly one of the five groups (PER_CHARACTER_USER_TABLES above is the
# third). tests/test_character_removal.py fails, naming the table, on one
# that isn't. Adding such a table means deciding here.

# Current values Vigilant keeps re-fetching while the character is linked.
# Small (one row, or a few, per character), so deleted in the removal
# request, and swept again by the background purge in case a sync or ingest
# that was already running wrote one back. The character's ESI response
# cache belongs here too, but it is keyed by token principal rather than a
# character_id column and finding its rows is a scan of a large table, so
# only background work clears it: the purge after a removal, and
# clear_esi_cache_in_background after a narrowing.
LIVE_STATE_TABLES: tuple[str, ...] = (
    "character_dashboard_cache",
    "character_asset_cache",
    "character_corp_roles",
    # zKillboard backfill cursor. Nothing reads it for an unlinked character;
    # a re-added one starts its backfill over, and the killmails it found are
    # public data that stays either way.
    "character_kill_ingest",
)

# What Vigilant accumulated over time; can be large. Deleted by the background
# purge in batches: always for admin removal and a transfer (in the request
# for a transfer, see the module docstring), on self-removal only when asked.
# Removal deletes the character's net-worth rows outright, rather than zeroing
# components as a narrowing does.
HISTORY_TABLES: tuple[str, ...] = (
    "wallet_snapshots",
    "wallet_transactions",
    "industry_job_history",
    "mining_ledger_entries",
    "net_worth_snapshots",
)

# Left alone on purpose.
KEPT_TABLES: dict[str, str] = {
    "killmails": "public game data (victim and final-blow ids), shared by every user",
    "killmail_attackers": "public game data, shared by every user",
    "admin_audit_log": "the audit trail outlives what it records",
    "user_fittings": ("source_character_id only records which pilot a fit was imported "
                      "from; the fit is the user's own and stays in their library"),
}

# Removal's own bookkeeping.
REMOVAL_TABLES: dict[str, str] = {
    "characters": "the character row itself, deleted by the removal request",
    "character_purges": "the pending-purge queue; the background purge deletes its own row",
}

REASON_SELF = "self"
REASON_ADMIN_CHARACTER = "admin_character"
REASON_ADMIN_USER = "admin_user"
REASON_TRANSFER = "transfer"
REMOVAL_REASONS = (REASON_SELF, REASON_ADMIN_CHARACTER, REASON_ADMIN_USER, REASON_TRANSFER)

# A sync already running when the character was removed can still write until
# it finishes or hits _SYNC_TIMEOUT (300s, app/routes/dashboard.py); the purge
# starts after that plus a margin. tests/test_character_removal.py keeps the
# two in step.
PURGE_DELAY = timedelta(seconds=300 + 120)
PURGE_BATCH_ROWS = 5000
ESI_CACHE_BATCH_KEYS = 500
# Between batches, so a sync waiting on the write lock gets its turn.
BATCH_PAUSE_SECONDS = 0.05
# How far before a sync's own start time a removal still counts as "while it
# ran": the sync reads the character a moment before it notes its start, and
# a commit can wait up to busy_timeout (10s) in between. Erring early only
# makes a sync that started just after a removal skip one write.
_SYNC_START_SLACK = timedelta(seconds=60)

_NOT_LINKED = "NOT EXISTS (SELECT 1 FROM characters WHERE character_id = :cid)"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _between_batches() -> None:
    """The pause between two batches (a seam the tests use to act mid-purge)."""
    await asyncio.sleep(BATCH_PAUSE_SECONDS)


def _naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


async def _delete_rows(db: AsyncSession, table: str, cid: int, *, only_if_gone: bool = False) -> int:
    """One unbatched DELETE of a character's rows. For small tables only."""
    guard = f" AND {_NOT_LINKED}" if only_if_gone else ""
    res = await db.execute(text(f"DELETE FROM {table} WHERE character_id = :cid{guard}"), {"cid": cid})
    return res.rowcount or 0


async def _delete_in_batches(db: AsyncSession, table: str, cid: int, *,
                             only_if_gone: bool) -> tuple[int, int]:
    """Delete a character's rows from `table` PURGE_BATCH_ROWS at a time, one
    short transaction per batch, so the write lock is never held for long.
    Returns (rows deleted, batches run).

    With `only_if_gone`, the "character is not linked" check sits inside each
    DELETE statement, so it is decided atomically with the delete: a re-add
    committed between two batches stops the very next one. A batch that
    deletes fewer rows than asked (possibly none) ends the loop; the caller
    tells "done" from "linked again" by checking the characters table.
    """
    guard = f" AND {_NOT_LINKED}" if only_if_gone else ""
    stmt = text(
        f"DELETE FROM {table} WHERE rowid IN "
        f"(SELECT rowid FROM {table} WHERE character_id = :cid LIMIT :n){guard}"
    )
    deleted = batches = 0
    while True:
        res = await db.execute(stmt, {"cid": cid, "n": PURGE_BATCH_ROWS})
        await db.commit()
        n = res.rowcount or 0
        deleted += n
        batches += 1
        if n < PURGE_BATCH_ROWS:
            return deleted, batches
        await _between_batches()


async def _clear_esi_cache_batched(db: AsyncSession, cid: int) -> int:
    """Drop every cached authenticated ESI response for this character,
    without holding the write lock through a table scan.

    The LIKE is a read-only SELECT of keys (a WAL reader never blocks the
    writer), then the rows go by primary key, ESI_CACHE_BATCH_KEYS per
    transaction. The only way esi_cache is cleared per character: by the
    removal purge, and after a narrowing (clear_esi_cache_in_background).
    """
    keys = list((await db.execute(
        select(ESICache.key).where(ESICache.key.like(_esi_cache_marker(cid))))).scalars())
    deleted = 0
    for i in range(0, len(keys), ESI_CACHE_BATCH_KEYS):
        res = await db.execute(
            delete(ESICache).where(ESICache.key.in_(keys[i:i + ESI_CACHE_BATCH_KEYS]))
            .execution_options(synchronize_session=False))
        await db.commit()
        deleted += res.rowcount or 0
        await _between_batches()
    return deleted


async def _is_linked(db: AsyncSession, cid: int) -> bool:
    return (await db.execute(
        select(Character.id).where(Character.character_id == cid).limit(1))).first() is not None


async def _record_purge(db: AsyncSession, cid: int, *, reason: str, delete_history: bool) -> None:
    now = _utcnow()
    stmt = sqlite_insert(CharacterPurge).values(
        character_id=cid, removed_at=now, delete_history=delete_history,
        reason=reason, created_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["character_id"],
        set_={
            "removed_at": stmt.excluded.removed_at,
            "reason": stmt.excluded.reason,
            "delete_history": or_(CharacterPurge.delete_history, stmt.excluded.delete_history),
        },
    )
    await db.execute(stmt)


async def remove_character_from_account(db: AsyncSession, char: Character, *,
                                        reason: str, delete_history: bool) -> dict[str, int]:
    """Take a character off its account: the one helper every removal path calls.

    Deletes the small live state (LIVE_STATE_TABLES), the user's own rows
    about the character (purge_character_user_rows) and the character row,
    and records a pending purge for the background to clear its ESI response
    cache and, if `delete_history`, its history. Doesn't commit: the caller
    commits all of it in one transaction, and handles token revocation.

    `reason` is one of REMOVAL_REASONS. For REASON_TRANSFER with
    `delete_history`, the history is deleted here instead, in batches that
    each commit on `db`, before the row goes (see the module docstring for
    why); rows a running sync wrote between batches go in the caller's commit.
    """
    if reason not in REMOVAL_REASONS:
        raise ValueError(f"unknown removal reason {reason!r}")
    cid = int(char.character_id)
    history_now = reason == REASON_TRANSFER and delete_history
    counts: dict[str, int] = {}

    if history_now:
        for table in HISTORY_TABLES:
            counts[table], _ = await _delete_in_batches(db, table, cid, only_if_gone=False)
    for table in LIVE_STATE_TABLES:
        counts[table] = await _delete_rows(db, table, cid)
    if history_now:
        for table in HISTORY_TABLES:
            counts[table] += await _delete_rows(db, table, cid)
    counts["per_character_user_rows"] = await purge_character_user_rows(db, cid)
    await db.delete(char)
    await _record_purge(db, cid, reason=reason,
                        delete_history=delete_history and not history_now)
    logger.info("removing character %s (%s, history %s): %s", cid, reason,
                "deleted now" if history_now else ("queued for deletion" if delete_history else "kept"),
                counts)
    return counts


async def sync_must_not_write(db: AsyncSession, character_id: int, sync_started: datetime) -> bool:
    """True when a sync that started at `sync_started` must drop its results
    instead of committing them: the character is gone, or it was taken off an
    account after the sync started. The row alone can't tell the second case,
    because a transfer links the character to its new owner in the same
    request that removed it; the pending purge the removal recorded can.
    That purge row outlives any sync it could matter to (PURGE_DELAY is
    longer than the sync timeout).

    Called by _sync_fields just before its final commit. Runs with autoflush
    off: flushing the sync's pending changes to a deleted cache row would
    raise before the question is even asked.
    """
    cid = int(character_id)
    since = _naive_utc(sync_started) - _SYNC_START_SLACK
    gone = ~exists().where(Character.character_id == cid)
    removed = exists().where(CharacterPurge.character_id == cid, CharacterPurge.removed_at >= since)
    with db.no_autoflush:
        return bool((await db.execute(select(or_(gone, removed)))).scalar())


async def run_due_purges(now: datetime | None = None, *, session_factory=None) -> list[dict]:
    """Run every pending purge whose PURGE_DELAY has passed, oldest first.

    Called by the background scheduler about once a minute, as its own task
    so a long purge never holds up the scheduler loop; also what resumes a
    purge a restart interrupted. A job that fails is logged and left in place
    for the next run. Returns one summary per job that ran.
    """
    factory = session_factory or AsyncSessionLocal
    cutoff = _naive_utc(now or _utcnow()) - PURGE_DELAY
    try:
        async with factory() as db:
            jobs = (await db.execute(
                select(CharacterPurge.character_id, CharacterPurge.removed_at,
                       CharacterPurge.delete_history, CharacterPurge.reason)
                .where(CharacterPurge.removed_at <= cutoff)
                .order_by(CharacterPurge.removed_at))).all()
    except Exception as e:
        logger.warning("character purge: could not read pending purges: %s", e)
        return []
    results = []
    for cid, removed_at, delete_history, reason in jobs:
        try:
            results.append(await _run_purge(factory, cid, removed_at, bool(delete_history), reason))
        except Exception as e:
            logger.warning("character purge for %s failed, will retry: %s", cid, e)
    return results


async def _run_purge(factory, cid: int, removed_at: datetime, delete_history: bool,
                     reason: str) -> dict:
    counts: dict[str, int] = {}
    outcome = "done"
    async with factory() as db:
        counts["esi_cache"] = await _clear_esi_cache_batched(db, cid)
        for table in LIVE_STATE_TABLES:
            n = await _delete_rows(db, table, cid, only_if_gone=True)
            await db.commit()
            if n:
                counts[table] = n
        if delete_history:
            for table in HISTORY_TABLES:
                counts[table], _ = await _delete_in_batches(db, table, cid, only_if_gone=True)
                if await _is_linked(db, cid):
                    outcome = "abandoned"
                    break

        # Only this job's row: a removal recorded while it ran moved
        # removed_at on, and that one still has to wait out its own syncs.
        await db.execute(delete(CharacterPurge).where(
            CharacterPurge.character_id == cid, CharacterPurge.removed_at == removed_at))
        history = "deleted" if delete_history else "kept"
        if outcome == "abandoned":
            history = "kept (the character was linked again before it was all deleted)"
        detail = (f"Background clean-up after removal ({reason}); history {history}. "
                  f"Rows deleted: {json.dumps(counts, sort_keys=True)}")
        db.add(AdminAuditLog(
            user_id=None, character_id=cid, detail=detail,
            event_type="character_purge_done" if outcome == "done" else "character_purge_abandoned",
        ))
        await db.commit()
    logger.info("character purge %s for character %s (%s, history %s): %s",
                outcome, cid, reason, history, counts)
    return {"character_id": cid, "reason": reason, "outcome": outcome, "counts": counts}
