"""Unified mining ledger — grouped by corporation with cross-corp selection."""

import asyncio
import json
import logging
from collections import defaultdict

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.models import get_db, AsyncSessionLocal, Character
from app.esi.client import ESIClient, refresh_token
from app.routes.mining import _sync_and_fetch_mining, _get_price_map, _aggregate_ledger
from app.sde import lookup as sde

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mining-ledger"])
templates = Jinja2Templates(directory="app/templates")

MINING_SCOPE = "esi-industry.read_character_mining.v1"

# Curated 12-color palette for dark background
ORE_COLORS = [
    "#5b8def", "#e06c75", "#56b6c2", "#d19a66", "#98c379", "#c678dd",
    "#e5c07b", "#61afef", "#be5046", "#7ec699", "#d4bfff", "#c8a951",
]


def _build_chart_data(entries: list, type_names: dict, price_map: dict) -> dict:
    """Build stacked bar chart data from raw mining entries."""
    # date -> {type_id -> quantity}, date -> total_isk
    date_ores: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    date_isk: dict[str, float] = defaultdict(float)
    ore_totals: dict[int, float] = defaultdict(float)

    for e in entries:
        date = e.get("date", "")
        tid = e.get("type_id", 0)
        qty = e.get("quantity", 0)
        price = price_map.get(tid, 0)
        date_ores[date][tid] += qty
        date_isk[date] += qty * price
        ore_totals[tid] += qty * price

    if not date_ores:
        return {"dates": [], "ores": [], "stacks": {}, "isk_values": [], "ore_colors": {}}

    # Top 10 ores by value, rest grouped as "Other"
    sorted_ores = sorted(ore_totals.items(), key=lambda x: x[1], reverse=True)
    top_ores = [tid for tid, _ in sorted_ores[:10]]
    other_ores = {tid for tid, _ in sorted_ores[10:]}

    dates = sorted(date_ores.keys())
    ore_names = [type_names.get(tid, f"Ore {tid}") for tid in top_ores]
    if other_ores:
        ore_names.append("Other")

    stacks: dict[str, list[int]] = {name: [] for name in ore_names}
    isk_values: list[float] = []

    for date in dates:
        day_data = date_ores[date]
        for i, tid in enumerate(top_ores):
            stacks[ore_names[i]].append(day_data.get(tid, 0))
        if other_ores:
            stacks["Other"].append(sum(day_data.get(tid, 0) for tid in other_ores))
        isk_values.append(round(date_isk[date], 2))

    ore_colors = {}
    for i, name in enumerate(ore_names):
        ore_colors[name] = ORE_COLORS[i % len(ORE_COLORS)]

    return {
        "dates": dates,
        "ores": ore_names,
        "stacks": stacks,
        "isk_values": isk_values,
        "ore_colors": ore_colors,
    }


async def _fetch_chars_mining(chars: list[Character], db: AsyncSession):
    """Fetch mining data for multiple characters in parallel.

    Returns (all_raw_entries, per_char_stats, char_names_used, type_names, system_names, price_map).
    """
    all_raw = []
    per_char: dict[int, dict] = {}
    char_names_used = []

    async def _fetch_one(c):
        # Each character needs its own session to avoid concurrent transaction conflicts
        async with AsyncSessionLocal() as char_db:
            token = await refresh_token(c, char_db)
            client = ESIClient(token, db=char_db)
            entries = await _sync_and_fetch_mining(client, c.character_id, char_db)
            return c.character_id, c.character_name, entries

    results = await asyncio.gather(*[_fetch_one(c) for c in chars], return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            logger.warning("Mining fetch failed: %s", r)
            continue
        cid, cname, entries = r
        char_names_used.append(cname)
        all_raw.extend(entries)
        # Quick per-char stats (quantity + entry count)
        qty = sum(e.get("quantity", 0) for e in entries)
        per_char[cid] = {"quantity": qty, "entries": len(entries)}

    # Resolve names + prices once for the combined set
    type_ids = list({e["type_id"] for e in all_raw})
    system_ids = list({e["solar_system_id"] for e in all_raw})
    type_names = await sde.type_ids_to_names(db, type_ids) if type_ids else {}
    system_names = await sde.system_ids_to_names(db, system_ids) if system_ids else {}
    price_map = await _get_price_map(db, set(type_ids))

    # Compute per-char value now that we have prices
    for r in results:
        if isinstance(r, Exception):
            continue
        cid, _, entries = r
        val = sum(e.get("quantity", 0) * price_map.get(e.get("type_id", 0), 0) for e in entries)
        days = len({e.get("date") for e in entries})
        per_char[cid]["value"] = val
        per_char[cid]["days"] = days

    return all_raw, per_char, char_names_used, type_names, system_names, price_map


@router.get("/industry/mining-ledger", response_class=HTMLResponse)
async def mining_ledger(request: Request, db: AsyncSession = Depends(get_db)):
    """Landing page — corporation cards, no ESI calls."""
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")

    result = await db.execute(select(Character).where(Character.user_id == user_id))
    characters = list(result.scalars().all())

    corps: dict[int, dict] = {}
    for char in characters:
        cid = char.corporation_id or 0
        if cid not in corps:
            corps[cid] = {
                "corp_id": cid,
                "corp_name": char.corporation_name or "No Corporation",
                "total": 0,
                "with_scope": 0,
                "characters": [],
            }
        corps[cid]["total"] += 1
        has_scope = MINING_SCOPE in (char.scopes or "")
        if has_scope:
            corps[cid]["with_scope"] += 1
        corps[cid]["characters"].append({
            "character_id": char.character_id,
            "character_name": char.character_name,
            "has_scope": has_scope,
        })

    # Sort corps by name, put "No Corporation" last
    corps_list = sorted(corps.values(), key=lambda c: (c["corp_id"] == 0, c["corp_name"]))

    return templates.TemplateResponse("mining_ledger.html", {
        "request": request,
        "corps": corps_list,
    })


@router.get("/industry/mining-ledger/corp/{corp_id}", response_class=HTMLResponse)
async def mining_ledger_corp(request: Request, corp_id: int, db: AsyncSession = Depends(get_db)):
    """HTMX partial — fetch mining stats for all characters in a corp."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("")

    result = await db.execute(select(Character).where(Character.user_id == user_id))
    all_chars = list(result.scalars().all())
    corp_chars = [c for c in all_chars if (c.corporation_id or 0) == corp_id and MINING_SCOPE in (c.scopes or "")]

    if not corp_chars:
        return templates.TemplateResponse("partials/mining_ledger_corp.html", {
            "request": request,
            "corp_id": corp_id,
            "char_stats": [],
            "corp_total_value": 0,
            "corp_total_quantity": 0,
            "corp_days": 0,
            "error": None,
        })

    try:
        _, per_char, _, _, _, _ = await _fetch_chars_mining(corp_chars, db)
    except Exception as exc:
        logger.warning("Mining ledger corp %s failed: %s", corp_id, exc)
        return templates.TemplateResponse("partials/mining_ledger_corp.html", {
            "request": request,
            "corp_id": corp_id,
            "char_stats": [],
            "corp_total_value": 0,
            "corp_total_quantity": 0,
            "corp_days": 0,
            "error": str(exc),
        })

    char_stats = []
    for c in corp_chars:
        stats = per_char.get(c.character_id, {})
        char_stats.append({
            "character_id": c.character_id,
            "character_name": c.character_name,
            "value": stats.get("value", 0),
            "quantity": stats.get("quantity", 0),
            "days": stats.get("days", 0),
        })
    char_stats.sort(key=lambda x: x["value"], reverse=True)

    return templates.TemplateResponse("partials/mining_ledger_corp.html", {
        "request": request,
        "corp_id": corp_id,
        "char_stats": char_stats,
        "corp_total_value": sum(s["value"] for s in char_stats),
        "corp_total_quantity": sum(s["quantity"] for s in char_stats),
        "corp_days": max((s["days"] for s in char_stats), default=0),
        "error": None,
    })


@router.get("/industry/mining-ledger/view", response_class=HTMLResponse)
async def mining_ledger_view(request: Request, char_ids: str = "", db: AsyncSession = Depends(get_db)):
    """HTMX partial — aggregated ledger view for selected character IDs."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("")

    try:
        ids = [int(x.strip()) for x in char_ids.split(",") if x.strip()]
    except ValueError:
        return HTMLResponse('<div class="b-empty">Invalid character IDs.</div>')

    if not ids:
        return HTMLResponse('<div class="b-empty">No characters selected.</div>')

    result = await db.execute(
        select(Character).where(Character.user_id == user_id, Character.character_id.in_(ids))
    )
    chars = [c for c in result.scalars().all() if MINING_SCOPE in (c.scopes or "")]

    if not chars:
        return HTMLResponse('<div class="b-empty">No selected characters have the mining scope.</div>')

    try:
        all_raw, _, char_names, type_names, system_names, price_map = await _fetch_chars_mining(chars, db)
    except Exception as exc:
        logger.warning("Mining ledger view failed: %s", exc)
        return HTMLResponse(f'<div class="b-empty" style="color:var(--danger);">Failed to load: {type(exc).__name__}</div>')

    data = _aggregate_ledger(all_raw, type_names, system_names, price_map)
    chart_data = _build_chart_data(all_raw, type_names, price_map)

    return templates.TemplateResponse("partials/mining_ledger_data.html", {
        "request": request,
        "data": data,
        "characters": char_names,
        "chart_data_json": json.dumps(chart_data),
    })
