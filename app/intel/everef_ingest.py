"""EVERef historical killmail backfill.

Downloads daily tar.bz2 archives from data.everef.net and bulk-inserts them
into the killmails table, pre-seeding the DB so the zKB character backfill
skips ESI hydration for kills already present.

Each archive contains verbatim ESI killmail payloads — full victim/attacker/
item data, but NO zKillboard metadata (total_value, npc/solo/awox flags).
total_value is left NULL here and patched in by killmail_ingest.backfill_character()
when it sees the kill already in the DB.

Schedule: one year per day, newest-first (2026 → 2016), driven by
dashboard._background_scheduler. Import within each year runs sequentially
with INTER_ARCHIVE_SLEEP between archives to avoid hammering EVERef.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import tarfile
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy import select, func

from app.db.models import (
    AsyncSessionLocal,
    EverefImportDay,
    Killmail,
    KillmailAttacker,
)
from app.intel.killmail_store import _write_items, get_our_char_ids

log = logging.getLogger(__name__)

EVEREF_BASE = "https://data.everef.net/killmails"
START_YEAR = 2016
INTER_ARCHIVE_SLEEP = 5.0  # seconds between daily archives within a year
_CHUNK_SIZE = 200           # kills per DB transaction

_import_running = False

_NOT_FOUND = object()


def is_running() -> bool:
    return _import_running


async def _download_archive(date_str: str) -> bytes | object | None:
    """Download a daily archive.

    Returns:
        bytes   — archive data on success
        _NOT_FOUND — HTTP 404 (no archive for this day; mark as done)
        None    — network/HTTP error (don't mark as done; retry later)
    """
    year = date_str[:4]
    url = f"{EVEREF_BASE}/{year}/killmails-{date_str}.tar.bz2"
    try:
        async with httpx.AsyncClient(
            timeout=120.0,
            headers={"User-Agent": "Vigilant/1.0 EVE Dashboard (personal use)"},
        ) as http:
            resp = await http.get(url)
            if resp.status_code == 200:
                return resp.content
            if resp.status_code == 404:
                log.debug("everef_ingest: no archive for %s (404)", date_str)
                return _NOT_FOUND
            log.warning("everef_ingest: HTTP %s for %s", resp.status_code, date_str)
            return None
    except Exception as e:
        log.warning("everef_ingest: download failed %s: %s", date_str, e)
        return None


def _parse_archive(raw: bytes) -> list[dict]:
    """Extract all killmail JSON payloads from a tar.bz2 archive."""
    payloads: list[dict] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:bz2") as tar:
            for member in tar.getmembers():
                if not member.isfile() or not member.name.endswith(".json"):
                    continue
                f = tar.extractfile(member)
                if f is None:
                    continue
                try:
                    payload = json.loads(f.read())
                    if isinstance(payload, dict) and payload.get("killmail_id"):
                        payloads.append(payload)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
    except Exception as e:
        log.warning("everef_ingest: archive parse error: %s", e)
    return payloads


def _build_kill_rows(
    payload: dict, our_char_ids: set[int]
) -> tuple[Killmail | None, list[KillmailAttacker], list[dict]]:
    """Build ORM rows from an ESI killmail payload. Returns (None, [], []) on error."""
    kid = payload.get("killmail_id")
    khash = payload.get("killmail_hash")
    if not (kid and khash):
        return None, [], []

    try:
        kill_time = datetime.fromisoformat(
            (payload.get("killmail_time") or "").replace("Z", "+00:00")
        ).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None, [], []

    victim = payload.get("victim") or {}
    attackers_raw = payload.get("attackers") or []
    final_blow = next((a for a in attackers_raw if a.get("final_blow")), None)

    victim_char = victim.get("character_id")
    attacker_chars = {a.get("character_id") for a in attackers_raw if a.get("character_id")}
    involves_our = bool(
        (victim_char and victim_char in our_char_ids)
        or (our_char_ids & attacker_chars)
    )
    # EVERef has no zkb.npc flag — infer from attacker list
    is_npc = bool(attackers_raw) and all(a.get("character_id") is None for a in attackers_raw)

    km = Killmail(
        killmail_id=kid,
        killmail_hash=khash,
        killmail_time=kill_time,
        solar_system_id=payload.get("solar_system_id") or 0,
        victim_character_id=victim_char,
        victim_corporation_id=victim.get("corporation_id"),
        victim_alliance_id=victim.get("alliance_id"),
        victim_ship_type_id=victim.get("ship_type_id") or 0,
        total_value=None,  # filled later by zKB backfill
        is_npc=is_npc,
        attacker_count=len(attackers_raw),
        final_blow_character_id=(final_blow or {}).get("character_id"),
        involves_our_char=involves_our,
        fetched_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )

    attacker_rows = [
        KillmailAttacker(
            killmail_id=kid,
            character_id=att.get("character_id"),
            corporation_id=att.get("corporation_id"),
            alliance_id=att.get("alliance_id"),
            ship_type_id=att.get("ship_type_id"),
            weapon_type_id=att.get("weapon_type_id"),
            final_blow=bool(att.get("final_blow", False)),
            damage_done=int(att.get("damage_done") or 0),
            security_status=(
                float(att["security_status"]) if att.get("security_status") is not None else None
            ),
        )
        for att in attackers_raw
    ]

    return km, attacker_rows, victim.get("items") or []


async def _insert_chunk(
    payloads: list[dict], our_char_ids: set[int]
) -> int:
    """Insert a batch of killmails in one transaction. Falls back to per-kill
    inserts on conflict."""
    async with AsyncSessionLocal() as db:
        rows_added = 0
        for payload in payloads:
            km, attacker_rows, items = _build_kill_rows(payload, our_char_ids)
            if km is None:
                continue
            db.add(km)
            for att in attacker_rows:
                db.add(att)
            if items:
                await _write_items(db, km.killmail_id, items, parent_id=None)
            rows_added += 1
        try:
            await db.commit()
            return rows_added
        except Exception:
            await db.rollback()

    # Batch conflict — fall back to per-kill inserts (rare: only if stream
    # inserted a kill between our existence check and this commit).
    inserted = 0
    for payload in payloads:
        km, attacker_rows, items = _build_kill_rows(payload, our_char_ids)
        if km is None:
            continue
        async with AsyncSessionLocal() as db2:
            db2.add(km)
            for att in attacker_rows:
                db2.add(att)
            if items:
                await _write_items(db2, km.killmail_id, items, parent_id=None)
            try:
                await db2.commit()
                inserted += 1
            except Exception:
                pass
    return inserted


async def _bulk_insert(payloads: list[dict], our_char_ids: set[int]) -> int:
    """Batch-insert an archive's kills, skipping those already in the DB."""
    if not payloads:
        return 0

    all_ids = [p["killmail_id"] for p in payloads if p.get("killmail_id")]
    if not all_ids:
        return 0

    async with AsyncSessionLocal() as db:
        existing = {
            r[0]
            for r in (
                await db.execute(
                    select(Killmail.killmail_id).where(
                        Killmail.killmail_id.in_(all_ids)
                    )
                )
            ).all()
        }

    new_payloads = [p for p in payloads if p.get("killmail_id") not in existing]
    if not new_payloads:
        return 0

    inserted = 0
    for i in range(0, len(new_payloads), _CHUNK_SIZE):
        inserted += await _insert_chunk(
            new_payloads[i : i + _CHUNK_SIZE], our_char_ids
        )
    return inserted


async def _mark_done(d: date, count: int) -> None:
    async with AsyncSessionLocal() as db:
        db.add(
            EverefImportDay(
                date=d,
                killmail_count=count,
                imported_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        try:
            await db.commit()
        except Exception:
            pass  # already marked (idempotent)


async def import_day(d: date, our_char_ids: set[int]) -> int:
    """Download and import one daily archive. Returns count of newly inserted
    killmails (0 if already done or no archive). Returns -1 on download error."""
    async with AsyncSessionLocal() as db:
        if await db.get(EverefImportDay, d) is not None:
            return 0

    date_str = d.strftime("%Y-%m-%d")
    raw = await _download_archive(date_str)

    if raw is None:
        return -1  # network error — caller should not mark as done

    if raw is _NOT_FOUND:
        await _mark_done(d, 0)
        return 0

    payloads = _parse_archive(raw)
    inserted = await _bulk_insert(payloads, our_char_ids)
    await _mark_done(d, inserted)
    log.info("everef_ingest: %s → %d/%d new kills", date_str, inserted, len(payloads))
    return inserted


async def import_year(year: int) -> dict:
    """Import all days in a calendar year, newest-first within the year.
    Sets _import_running for the duration. Called as an asyncio task."""
    global _import_running
    _import_running = True
    try:
        today = date.today()
        year_start = date(year, 1, 1)
        year_end = min(date(year, 12, 31), today - timedelta(days=1))

        if year_end < year_start:
            return {"year": year, "inserted": 0, "days": 0}

        our_char_ids = await get_our_char_ids()
        total_inserted = 0
        days_processed = 0

        # Process newest-first within the year (Dec 31 → Jan 1)
        d = year_end
        while d >= year_start:
            result = await import_day(d, our_char_ids)
            if result > 0:
                total_inserted += result
            days_processed += 1
            d -= timedelta(days=1)
            await asyncio.sleep(INTER_ARCHIVE_SLEEP)

        log.info(
            "everef_ingest: year %d complete — %d new kills across %d days",
            year, total_inserted, days_processed,
        )
        return {"year": year, "inserted": total_inserted, "days": days_processed}
    finally:
        _import_running = False


async def find_next_year_to_import(start_year: int = START_YEAR) -> int | None:
    """Return the most-recent year that hasn't been fully imported yet.
    Iterates current year → start_year. Returns None when all done."""
    today = date.today()
    async with AsyncSessionLocal() as db:
        for year in range(today.year, start_year - 1, -1):
            year_start = date(year, 1, 1)
            year_end = min(date(year, 12, 31), today - timedelta(days=1))
            if year_end < year_start:
                continue
            expected = (year_end - year_start).days + 1
            row = await db.execute(
                select(func.count(EverefImportDay.date)).where(
                    EverefImportDay.date >= year_start,
                    EverefImportDay.date <= year_end,
                )
            )
            if (row.scalar() or 0) < expected:
                return year
    return None
