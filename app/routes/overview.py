"""Cross-character overview page — aggregated stats across all characters."""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.db.models import get_db, Character, CharacterDashboardCache, WalletSnapshot
from app.routes.characters import _process_skillqueue, group_skill_data

router = APIRouter(tags=["overview"])
templates = Jinja2Templates(directory="app/templates")


def _format_isk_py(amount: float) -> str:
    if amount >= 1_000_000_000_000:
        return f"{amount / 1_000_000_000_000:.2f}T ISK"
    if amount >= 1_000_000_000:
        return f"{amount / 1_000_000_000:.2f}B ISK"
    if amount >= 1_000_000:
        return f"{amount / 1_000_000:.2f}M ISK"
    return f"{amount:,.0f} ISK"


@router.get("/overview", response_class=HTMLResponse)
async def overview(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")

    result = await db.execute(select(Character).where(Character.user_id == user_id))
    characters = list(result.scalars().all())
    if not characters:
        return RedirectResponse("/dashboard")

    cids = [c.character_id for c in characters]
    cache_result = await db.execute(
        select(CharacterDashboardCache).where(CharacterDashboardCache.character_id.in_(cids))
    )
    caches = {c.character_id: c for c in cache_result.scalars().all()}

    # Aggregate
    total_wallet = 0
    total_jobs = 0
    total_sell = 0
    total_buy = 0
    total_unread_mail = 0
    total_unread_notifs = 0
    all_industry = []
    all_orders = []
    char_rows = []

    skillqueue_raw = {}

    for char in characters:
        cache = caches.get(char.character_id)
        wallet = cache.wallet if cache else None
        if wallet:
            total_wallet += wallet

        def _load(field):
            val = getattr(cache, field, None) if cache else None
            return json.loads(val) if val else None

        industry = _load("industry_json")
        orders = _load("orders_json")
        location = _load("location_json")
        mail = _load("mail_json")
        notifs = _load("notifications_json")
        sq = _load("skillqueue_json")

        skillqueue_raw[char.character_id] = (
            "no_scope" if "esi-skills.read_skillqueue.v1" not in (char.scopes or "")
            else sq
        )

        if industry and isinstance(industry, dict):
            cnt = industry.get("active_count", 0)
            total_jobs += cnt
            if cnt > 0:
                all_industry.append({
                    "char": char, "count": cnt,
                    "soonest": industry.get("soonest_time_str"),
                    "product": industry.get("soonest_product"),
                })

        if orders and isinstance(orders, dict):
            s = orders.get("sell_count", 0)
            b = orders.get("buy_count", 0)
            total_sell += s
            total_buy += b
            if s + b > 0:
                all_orders.append({"char": char, "sell": s, "buy": b})

        if isinstance(mail, dict):
            total_unread_mail += mail.get("unread_count", 0)
        if isinstance(notifs, dict):
            total_unread_notifs += notifs.get("unread_count", 0)

        char_rows.append({
            "char": char,
            "wallet": wallet,
            "wallet_str": _format_isk_py(wallet) if wallet else "—",
            "location": location,
            "industry_count": industry.get("active_count", 0) if isinstance(industry, dict) else 0,
            "order_count": (orders.get("sell_count", 0) + orders.get("buy_count", 0)) if isinstance(orders, dict) else 0,
            "is_online": location.get("is_online", False) if isinstance(location, dict) else False,
        })

    # Sort by wallet descending
    char_rows.sort(key=lambda x: x["wallet"] or 0, reverse=True)

    # Process skill data
    skill_data = await _process_skillqueue(characters, skillqueue_raw, db)

    return templates.TemplateResponse("overview.html", {
        "request": request,
        "characters": characters,
        "char_rows": char_rows,
        "skill_data": skill_data,
        "total_wallet": total_wallet,
        "total_wallet_str": _format_isk_py(total_wallet),
        "total_jobs": total_jobs,
        "total_sell": total_sell,
        "total_buy": total_buy,
        "total_unread_mail": total_unread_mail,
        "total_unread_notifs": total_unread_notifs,
        "all_industry": all_industry,
        "all_orders": all_orders,
        "char_count": len(characters),
    })
