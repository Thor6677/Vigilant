"""
Character detail page with wallet history chart and journal.
"""
import json
import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.models import get_db, Character, CharacterDashboardCache, WalletSnapshot, AsyncSessionLocal
from app.esi.client import ESIClient, refresh_token
from app.esi.character import get_wallet_journal
from dateutil import parser as iso_parser

logger = logging.getLogger(__name__)

router = APIRouter(tags=["character_detail"])
templates = Jinja2Templates(directory="app/templates")

_RANGE_DAYS = {"1d": 1, "5d": 5, "1w": 7, "1m": 30, "6m": 180, "1y": 365}
_MAX_CHART_POINTS = 400


def _downsample(snapshots: list, target: int) -> list:
    """Return at most `target` evenly-spaced snapshots."""
    if len(snapshots) <= target:
        return snapshots
    step = len(snapshots) / target
    return [snapshots[int(i * step)] for i in range(target)]


async def _get_chart_data(character_id: int, range_key: str, db: AsyncSession) -> dict:
    days = _RANGE_DAYS.get(range_key, 7)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    # recorded_at is stored as naive UTC
    since_naive = since.replace(tzinfo=None)

    result = await db.execute(
        select(WalletSnapshot)
        .where(
            WalletSnapshot.character_id == character_id,
            WalletSnapshot.recorded_at >= since_naive,
        )
        .order_by(WalletSnapshot.recorded_at)
    )
    snapshots = result.scalars().all()
    snapshots = _downsample(snapshots, _MAX_CHART_POINTS)

    labels = [s.recorded_at.strftime("%Y-%m-%dT%H:%M:%SZ") for s in snapshots]
    values = [s.balance for s in snapshots]
    return {"labels": labels, "values": values}


@router.get("/character/{character_id}", response_class=HTMLResponse)
async def character_detail(
    character_id: int,
    request: Request,
    range: str = "1w",
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/dashboard")

    char_result = await db.execute(
        select(Character).where(Character.character_id == character_id, Character.user_id == user_id)
    )
    char = char_result.scalar_one_or_none()
    if not char:
        return RedirectResponse("/dashboard")

    cache_result = await db.execute(
        select(CharacterDashboardCache).where(CharacterDashboardCache.character_id == character_id)
    )
    cache = cache_result.scalar_one_or_none()

    # Parse cached data
    queue_remaining = 0
    skillqueue = []
    total_sp_in_queue = 0
    active_skill = None
    if cache and cache.skillqueue_json:
        try:
            sq = json.loads(cache.skillqueue_json)
            if isinstance(sq, list):
                skillqueue = sq
                if skillqueue:
                    active_skill = skillqueue[0].copy()
                    # Look up skill name from SDE
                    skill_id = active_skill.get("skill_id")
                    if skill_id:
                        from app.db.sde_models import SDEType
                        result = await db.execute(
                            select(SDEType).where(SDEType.type_id == skill_id)
                        )
                        skill_type = result.scalar_one_or_none()
                        if skill_type:
                            active_skill["skill_name"] = skill_type.type_name
                        else:
                            active_skill["skill_name"] = f"Skill {skill_id}"
                    else:
                        active_skill["skill_name"] = "Unknown"
                    total_sp_in_queue = sum(s.get("level_end_sp", 0) for s in skillqueue)

                    # Calculate remaining time for active skill
                    finish_date_str = active_skill.get("finish_date")
                    if finish_date_str:
                        try:
                            finish_date = iso_parser.isoparse(finish_date_str)
                            now_utc = datetime.now(timezone.utc)
                            remaining = (finish_date - now_utc).total_seconds()
                            active_skill["remaining_time"] = max(0, remaining)
                        except Exception:
                            active_skill["remaining_time"] = 0
                    
                    # Calculate total queue finish time
                    if skillqueue:
                        last_skill = skillqueue[-1]
                        last_finish_str = last_skill.get("finish_date")
                        if last_finish_str:
                            try:
                                last_finish = iso_parser.isoparse(last_finish_str)
                                now_utc = datetime.now(timezone.utc)
                                queue_remaining = (last_finish - now_utc).total_seconds()
                                queue_remaining = max(0, queue_remaining)
                            except Exception:
                                queue_remaining = 0
            else:
                skillqueue = sq.get("skills", [])
                total_sp_in_queue = sq.get("total_sp", 0)
                active_skill = sq.get("active", None)
        except Exception as e:
            logger.warning("Failed to parse skillqueue for char %s: %s", character_id, e)


    # Fetch all trained skills to calculate total SP
    total_trained_sp = 0
    try:
        if "esi-skills.read_skills.v1" in (char.scopes or ""):
            async with AsyncSessionLocal() as token_db:
                char_result2 = await token_db.execute(
                    select(Character).where(Character.character_id == character_id)
                )
                char_fresh = char_result2.scalar_one_or_none()
                token = await refresh_token(char_fresh, token_db)
            client = ESIClient(token, db=db)
            # Fetch skills from ESI
            raw_skills = await client.get(f/characters/{character_id}/skills/, public)
            if raw_skills and isinstance(raw_skills, dict) and skills in raw_skills:
                skills_list = raw_skills.get(skills, [])
                total_trained_sp = sum(s.get(skillpoints_in_skill, 0) for s in skills_list)
    except Exception as e:
        logger.warning(Failed to fetch total SP for char %s: %s, character_id, e)

        zkill = []
    if cache and cache.zkill_json:
        try:
            zkill = json.loads(cache.zkill_json)
        except Exception as e:
            logger.warning("Failed to parse zkill for char %s: %s", character_id, e)

    
    assets = []
    if cache and cache.skillqueue_json:  # Check if we have the assets column (reuse for now)
        pass  # Assets not yet cached, will be added in future update
    
    # Calculate kill/loss stats from zkill data
    kills = sum(1 for km in zkill if not km.get("is_loss"))
    losses = sum(1 for km in zkill if km.get("is_loss"))

    # Fetch wallet journal (live ESI call)
    journal = []
    journal_error = None
    if "esi-wallet.read_character_wallet.v1" in (char.scopes or ""):
        try:
            async with AsyncSessionLocal() as token_db:
                char_result2 = await token_db.execute(
                    select(Character).where(Character.character_id == character_id)
                )
                char_fresh = char_result2.scalar_one_or_none()
                token = await refresh_token(char_fresh, token_db)
            client = ESIClient(token, db=db)
            raw = await get_wallet_journal(client, character_id, page=1)
            journal = raw[:20] if raw else []
        except Exception as e:
            logger.warning("Wallet journal fetch failed for char %s: %s", character_id, e)
            journal_error = "fetch_failed"
    else:
        journal_error = "missing_scope"

    # Initial chart data (default range)
    chart_data = await _get_chart_data(character_id, range, db)

    current_wallet = cache.wallet if cache else None

    return templates.TemplateResponse("character_detail.html", {
        "request": request,
        "char": char,
        "current_wallet": current_wallet,
        "journal": journal,
        "journal_error": journal_error,
        "chart_data_json": json.dumps(chart_data),
        "active_range": range,
        "ranges": list(_RANGE_DAYS.keys()),
        "active_skill": active_skill,
        "skillqueue": skillqueue,
        "total_sp_in_queue": total_sp_in_queue,
        "zkill": zkill,
        "kills": kills,
        "losses": losses,
        "assets": assets,
        "queue_remaining": queue_remaining,
        "now": datetime.utcnow(),
    })


@router.get("/character/{character_id}/wallet/chart.json")
async def wallet_chart_json(
    character_id: int,
    request: Request,
    range: str = "1w",
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    ownership = await db.execute(
        select(Character).where(Character.character_id == character_id, Character.user_id == user_id)
    )
    if not ownership.scalar_one_or_none():
        return JSONResponse({"error": "forbidden"}, status_code=403)

    data = await _get_chart_data(character_id, range, db)
    return JSONResponse(data)


# Helper function to get skill name from skill_id
async def get_skill_name(skill_id: int, db: AsyncSession) -> str:
    """Look up skill name from SDE database"""
    from app.db.sde_models import SDEType
    try:
        result = await db.execute(
            select(SDEType).where(SDEType.type_id == skill_id)
        )
        skill_type = result.scalar_one_or_none()
        return skill_type.type_name if skill_type else f"Skill {skill_id}"
    except Exception:
        return f"Skill {skill_id}"
