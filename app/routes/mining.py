"""Mining ledger — character and aggregated corporation view."""

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.models import get_db, AsyncSessionLocal, Character, MiningLedgerEntry
from app.esi.client import ESIClient, refresh_token
from app.esi import character as esi_char
from app.esi import market as esi_market
from app.sde import lookup as sde

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mining"])
templates = Jinja2Templates(directory="app/templates")


async def _fetch_all_mining(client: ESIClient, character_id: int) -> list:
    """Fetch all pages of the character mining ledger."""
    all_entries = []
    for page in range(1, 10):
        data = await esi_char.get_mining(client, character_id, page=page)
        if not data or not isinstance(data, list):
            break
        all_entries.extend(data)
        if len(data) < 1000:
            break
    return all_entries


async def _sync_and_fetch_mining(client: ESIClient, character_id: int, db: AsyncSession) -> list:
    """Fetch from ESI, store new entries, return ALL historical entries from DB."""
    # 1. Fetch fresh data from ESI (last 30 days)
    esi_entries = await _fetch_all_mining(client, character_id)

    # 2. Upsert into DB — build lookup of existing entries to avoid duplicates
    if esi_entries:
        try:
            existing = await db.execute(
                select(MiningLedgerEntry).where(MiningLedgerEntry.character_id == character_id)
            )
            existing_keys = {
                (r.date, r.type_id, r.solar_system_id)
                for r in existing.scalars().all()
            }

            new_entries = []
            for e in esi_entries:
                key = (e.get("date", ""), e.get("type_id", 0), e.get("solar_system_id", 0))
                if key not in existing_keys:
                    new_entries.append(MiningLedgerEntry(
                        character_id=character_id,
                        date=e["date"],
                        type_id=e["type_id"],
                        solar_system_id=e["solar_system_id"],
                        quantity=e["quantity"],
                    ))
                    existing_keys.add(key)

            if new_entries:
                db.add_all(new_entries)
                await db.commit()
                logger.info("Stored %d new mining entries for char %s", len(new_entries), character_id)

            # Update quantities for today's entries (they can change during the day)
            from sqlalchemy import update, and_
            for e in esi_entries:
                await db.execute(
                    update(MiningLedgerEntry).where(
                        and_(
                            MiningLedgerEntry.character_id == character_id,
                            MiningLedgerEntry.date == e["date"],
                            MiningLedgerEntry.type_id == e["type_id"],
                            MiningLedgerEntry.solar_system_id == e["solar_system_id"],
                        )
                    ).values(quantity=e["quantity"])
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    # 3. Return ALL historical entries from DB
    result = await db.execute(
        select(MiningLedgerEntry)
        .where(MiningLedgerEntry.character_id == character_id)
        .order_by(MiningLedgerEntry.date.desc())
    )
    rows = result.scalars().all()
    return [
        {
            "date": r.date,
            "type_id": r.type_id,
            "solar_system_id": r.solar_system_id,
            "quantity": r.quantity,
        }
        for r in rows
    ]


async def _get_price_map(db: AsyncSession, type_ids: set[int]) -> dict[int, float]:
    """Fetch global average prices."""
    price_map: dict[int, float] = {}
    try:
        client = ESIClient("", db=db)
        all_prices = await esi_market.get_market_prices(client)
        for p in all_prices:
            tid = p.get("type_id")
            if tid in type_ids:
                price_map[tid] = p.get("average_price") or p.get("adjusted_price") or 0
    except Exception:
        pass
    return price_map


def _aggregate_ledger(entries: list, type_names: dict, system_names: dict, price_map: dict) -> dict:
    """Process raw mining entries into aggregated views."""

    # Per-day totals
    by_date: dict[str, dict] = defaultdict(lambda: {"quantity": 0, "value": 0.0, "types": set()})
    # Per-ore totals
    by_ore: dict[int, dict] = {}
    # Per-system totals
    by_system: dict[int, dict] = {}
    # Daily entries for detail view
    daily_entries = []

    total_quantity = 0
    total_value = 0.0

    for e in entries:
        date = e.get("date", "")
        tid = e.get("type_id", 0)
        sid = e.get("solar_system_id", 0)
        qty = e.get("quantity", 0)
        price = price_map.get(tid, 0)
        value = qty * price
        ore_name = type_names.get(tid, f"Ore {tid}")
        sys_name = system_names.get(sid, f"System {sid}")

        total_quantity += qty
        total_value += value

        by_date[date]["quantity"] += qty
        by_date[date]["value"] += value
        by_date[date]["types"].add(ore_name)

        if tid not in by_ore:
            by_ore[tid] = {"type_id": tid, "name": ore_name, "quantity": 0, "value": 0.0}
        by_ore[tid]["quantity"] += qty
        by_ore[tid]["value"] += value

        if sid not in by_system:
            by_system[sid] = {"system_id": sid, "name": sys_name, "quantity": 0, "value": 0.0}
        by_system[sid]["quantity"] += qty
        by_system[sid]["value"] += value

        daily_entries.append({
            "date": date,
            "ore_name": ore_name,
            "type_id": tid,
            "system_name": sys_name,
            "quantity": qty,
            "value": value,
        })

    # Sort
    daily_entries.sort(key=lambda x: x["date"], reverse=True)
    ore_list = sorted(by_ore.values(), key=lambda x: x["value"], reverse=True)
    system_list = sorted(by_system.values(), key=lambda x: x["value"], reverse=True)
    date_list = [{"date": d, **v, "types": len(v["types"])} for d, v in sorted(by_date.items(), reverse=True)]

    return {
        "entries": daily_entries,
        "by_ore": ore_list,
        "by_system": system_list,
        "by_date": date_list,
        "total_quantity": total_quantity,
        "total_value": total_value,
        "days_active": len(by_date),
    }


@router.get("/character/{character_id}/mining", response_class=HTMLResponse)
async def character_mining(
    request: Request,
    character_id: int,
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")

    char_result = await db.execute(
        select(Character).where(Character.character_id == character_id, Character.user_id == user_id)
    )
    char = char_result.scalar_one_or_none()
    if not char:
        return RedirectResponse("/dashboard")

    char_info = {
        "character_id": char.character_id,
        "character_name": char.character_name,
        "corporation_id": char.corporation_id,
        "corporation_name": char.corporation_name,
    }

    scope = "esi-industry.read_character_mining.v1"
    if scope not in (char.scopes or ""):
        return templates.TemplateResponse("mining.html", {
            "request": request, "char": char_info, "data": None,
            "error": "Mining scope not available — re-authorize this character.",
            "is_corp": False, "corp_id": None, "characters": [],
        })

    try:
        token = await refresh_token(char, db)
        client = ESIClient(token, db=db)
        raw = await _sync_and_fetch_mining(client, character_id, db)

        # Resolve names
        type_ids = list({e["type_id"] for e in raw})
        system_ids = list({e["solar_system_id"] for e in raw})
        type_names = await sde.type_ids_to_names(db, type_ids) if type_ids else {}

        system_names = {}
        for sid in system_ids:
            info = await sde.system_info(db, sid)
            if info:
                system_names[sid] = info["system_name"]

        price_map = await _get_price_map(db, set(type_ids))
        data = _aggregate_ledger(raw, type_names, system_names, price_map)

    except Exception as exc:
        logger.warning("Mining fetch failed for char %s: %s", character_id, exc, exc_info=True)
        return templates.TemplateResponse("mining.html", {
            "request": request, "char": char_info, "data": None,
            "error": f"Failed to load mining data: {type(exc).__name__}",
            "is_corp": False, "corp_id": None, "characters": [],
        })

    return templates.TemplateResponse("mining.html", {
        "request": request, "char": char_info, "data": data,
        "error": None, "is_corp": False, "corp_id": None, "characters": [],
    })


@router.get("/corporations/{corp_id}/mining", response_class=HTMLResponse)
async def corp_mining(
    request: Request,
    corp_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Aggregated mining view across all characters in a corporation."""
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")

    result = await db.execute(select(Character).where(Character.user_id == user_id))
    all_characters = list(result.scalars().all())

    # Find characters in this corp with mining scope
    scope = "esi-industry.read_character_mining.v1"
    corp_chars = [c for c in all_characters if c.corporation_id == corp_id and scope in (c.scopes or "")]

    char_info = {
        "character_id": corp_chars[0].character_id if corp_chars else None,
        "character_name": corp_chars[0].character_name if corp_chars else None,
        "corporation_name": corp_chars[0].corporation_name if corp_chars else None,
    }

    if not corp_chars:
        return templates.TemplateResponse("mining.html", {
            "request": request, "char": char_info, "data": None,
            "error": "No characters with mining scope in this corporation. Re-authorize to grant mining permissions.",
            "is_corp": True, "corp_id": corp_id,
            "characters": [],
        })

    try:
        # Fetch mining ledger for each corp character in parallel
        # Each character gets its own DB session to avoid shared-session concurrency issues
        all_raw = []
        char_names_used = []

        async def _fetch_for_char(c):
            async with AsyncSessionLocal() as char_db:
                token = await refresh_token(c, char_db)
                client = ESIClient(token, db=char_db)
                entries = await _sync_and_fetch_mining(client, c.character_id, char_db)
                return c.character_name, entries

        results = await asyncio.gather(*[_fetch_for_char(c) for c in corp_chars], return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                continue
            name, entries = r
            char_names_used.append(name)
            all_raw.extend(entries)

        # Resolve names
        type_ids = list({e["type_id"] for e in all_raw})
        system_ids = list({e["solar_system_id"] for e in all_raw})
        type_names = await sde.type_ids_to_names(db, type_ids) if type_ids else {}

        system_names = {}
        for sid in system_ids:
            info = await sde.system_info(db, sid)
            if info:
                system_names[sid] = info["system_name"]

        price_map = await _get_price_map(db, set(type_ids))
        data = _aggregate_ledger(all_raw, type_names, system_names, price_map)

    except Exception as exc:
        logger.warning("Corp mining fetch failed for corp %s: %s", corp_id, exc, exc_info=True)
        return templates.TemplateResponse("mining.html", {
            "request": request, "char": char_info, "data": None,
            "error": f"Failed to load mining data: {type(exc).__name__}",
            "is_corp": True, "corp_id": corp_id, "characters": [],
        })

    return templates.TemplateResponse("mining.html", {
        "request": request, "char": char_info, "data": data,
        "error": None, "is_corp": True, "corp_id": corp_id,
        "characters": char_names_used,
    })
