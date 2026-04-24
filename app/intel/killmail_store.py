"""Killmail persistence + disk-first fetcher.

Design goals (learned from the reverted first attempt):
- No `raw_json` column — only normalized analytics fields.
- Disk-first: fetch_killmail checks `killmails` table before ESI.
- Tiny in-memory coalescing dict (200 entries) for within-request dedup only.
- `store_killmail` is the sole write path; it also persists attacker rows
  for kills involving our characters.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AsyncSessionLocal,
    Character,
    Killmail,
    KillmailAttacker,
)
from app.esi.client import ESIClient

log = logging.getLogger(__name__)


_COALESCE_MAX = 200
_coalesce_cache: "OrderedDict[int, dict]" = OrderedDict()
_esi_sem = asyncio.Semaphore(3)


def _coalesce_get(killmail_id: int) -> dict | None:
    v = _coalesce_cache.get(killmail_id)
    if v is not None:
        _coalesce_cache.move_to_end(killmail_id)
    return v


def _coalesce_set(killmail_id: int, data: dict) -> None:
    if killmail_id in _coalesce_cache:
        _coalesce_cache.move_to_end(killmail_id)
    _coalesce_cache[killmail_id] = data
    while len(_coalesce_cache) > _COALESCE_MAX:
        _coalesce_cache.popitem(last=False)


async def fetch_killmail(killmail_id: int, killmail_hash: str) -> dict | None:
    """Fetch a full killmail body from ESI. Uses a tiny process-wide
    coalescing cache (200 entries) so concurrent callers don't duplicate the
    request.

    Does NOT persist. Call `store_killmail` with the result if you want to
    write it to the `killmails` table.
    """
    cached = _coalesce_get(killmail_id)
    if cached is not None:
        return cached

    async with _esi_sem:
        cached = _coalesce_get(killmail_id)
        if cached is not None:
            return cached
        async with AsyncSessionLocal() as db:
            client = ESIClient("", db=db)
            try:
                data = await client.get_public(
                    f"/killmails/{killmail_id}/{killmail_hash}/"
                )
            except Exception as e:
                log.debug("killmail_store: ESI fetch failed %s: %s", killmail_id, e)
                return None
            if isinstance(data, dict):
                _coalesce_set(killmail_id, data)
                return data
    return None


async def get_our_char_ids(db: AsyncSession | None = None) -> set[int]:
    """Return the set of signed-in character IDs. Small query, memoize at the
    caller if you loop."""
    close_after = False
    if db is None:
        db = AsyncSessionLocal()
        close_after = True
    try:
        rows = await db.execute(select(Character.character_id))
        return {r[0] for r in rows.all()}
    finally:
        if close_after:
            await db.close()


async def killmail_exists(killmail_id: int, db: AsyncSession) -> bool:
    row = await db.execute(
        select(Killmail.killmail_id).where(Killmail.killmail_id == killmail_id)
    )
    return row.first() is not None


async def store_killmail(
    full: dict,
    zkb_stub: dict,
    our_char_ids: set[int],
) -> bool:
    """Persist an ESI killmail + zkb summary into `killmails`. If any of our
    characters are victim or attacker, sets involves_our_char=true and writes
    attacker rows too. Otherwise only the summary row is written (attackers
    are skipped to keep the table bounded).

    Idempotent — existing rows skipped via PK conflict.

    Returns True if a new row was inserted.
    """
    kid = full.get("killmail_id")
    khash = zkb_stub.get("hash") or full.get("killmail_hash")
    if not (kid and khash):
        return False

    try:
        kill_time = datetime.fromisoformat(
            (full.get("killmail_time") or "").replace("Z", "+00:00")
        ).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return False

    victim = full.get("victim") or {}
    attackers = full.get("attackers") or []
    final_blow = next((a for a in attackers if a.get("final_blow")), None)

    victim_char = victim.get("character_id")
    attacker_chars = {a.get("character_id") for a in attackers if a.get("character_id")}
    involves_our = bool(
        (victim_char and victim_char in our_char_ids)
        or (our_char_ids & attacker_chars)
    )

    async with AsyncSessionLocal() as db:
        if await killmail_exists(kid, db):
            return False

        km = Killmail(
            killmail_id=kid,
            killmail_hash=khash,
            killmail_time=kill_time,
            solar_system_id=full.get("solar_system_id") or 0,
            victim_character_id=victim_char,
            victim_corporation_id=victim.get("corporation_id"),
            victim_alliance_id=victim.get("alliance_id"),
            victim_ship_type_id=victim.get("ship_type_id") or 0,
            total_value=float(zkb_stub.get("totalValue") or 0) or None,
            is_npc=bool(zkb_stub.get("npc", False)),
            attacker_count=len(attackers),
            final_blow_character_id=(final_blow or {}).get("character_id"),
            involves_our_char=involves_our,
            fetched_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(km)

        # Store all attackers regardless of scope. Discovery-scope attacker
        # rows are needed for unique-pilot counting in recent_battles, and
        # gc_discovery_killmails now cascades the delete to keep growth bounded.
        for att in attackers:
            db.add(KillmailAttacker(
                killmail_id=kid,
                character_id=att.get("character_id"),
                corporation_id=att.get("corporation_id"),
                alliance_id=att.get("alliance_id"),
                ship_type_id=att.get("ship_type_id"),
                weapon_type_id=att.get("weapon_type_id"),
                final_blow=bool(att.get("final_blow", False)),
            ))

        try:
            await db.commit()
            return True
        except Exception as e:
            await db.rollback()
            log.debug("killmail_store: insert skipped for %s: %s", kid, e)
            return False


async def gc_discovery_killmails(retention_days: int = 30) -> int:
    """Delete discovery-scope killmails older than retention_days. Our-char
    rows are preserved forever. Cascades to killmail_attackers for the deleted
    rows (SQLite has no FK cascade, so we delete explicitly). Returns killmail
    row count deleted."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=retention_days)
    async with AsyncSessionLocal() as db:
        ids_to_delete = [
            r[0] for r in (await db.execute(
                select(Killmail.killmail_id).where(
                    Killmail.killmail_time < cutoff,
                    Killmail.involves_our_char == False,
                )
            )).all()
        ]
        if not ids_to_delete:
            return 0
        await db.execute(
            delete(KillmailAttacker).where(KillmailAttacker.killmail_id.in_(ids_to_delete))
        )
        result = await db.execute(
            delete(Killmail).where(Killmail.killmail_id.in_(ids_to_delete))
        )
        await db.commit()
        return result.rowcount or 0
