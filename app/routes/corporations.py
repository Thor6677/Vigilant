import asyncio
import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.models import get_db, Character, AsyncSessionLocal
from app.esi.client import ESIClient, refresh_token
from app.esi import corporation as esi_corp
from app.sde import lookup as sde

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/corporations", tags=["corporations"])
templates = Jinja2Templates(directory="app/templates")

# Corp-level ESI scopes we check for
CORP_SCOPES = {
    "members":    "esi-corporations.read_corporation_membership.v1",
    "wallet":     "esi-wallet.read_corporation_wallets.v1",
    "industry":   "esi-industry.read_corporation_jobs.v1",
    "orders":     "esi-markets.read_corporation_orders.v1",
    "structures": "esi-corporations.read_structures.v1",
    "contracts":  "esi-contracts.read_corporation_contracts.v1",
    "assets":     "esi-assets.read_corporation_assets.v1",
}

ACTIVITY_NAMES = {
    1: "Manufacturing",
    3: "Time Efficiency Research",
    4: "Material Efficiency Research",
    5: "Copying",
    8: "Invention",
    9: "Reactions",
}

# Structure states: what they mean from an EVE player's perspective
STRUCTURE_STATE_CLASS = {
    "shield_vulnerable":    "",           # normal online state
    "fitting_invulnerable": "is-muted",   # briefly invulnerable while being fit
    "onlining_vulnerable":  "is-warn",
    "anchor_vulnerable":    "is-warn",
    "anchoring":            "is-warn",
    "deploy_vulnerable":    "is-warn",
    "shield_reinforce":     "is-danger",  # under attack — first timer
    "armor_reinforce":      "is-danger",  # second timer
    "hull_reinforce":       "is-danger",  # final timer
    "hull_vulnerable":      "is-danger",  # no more timers — dying
    "unanchored":           "is-muted",
}


def _format_isk(value) -> str:
    if value is None:
        return "N/A"
    value = float(value)
    if abs(value) >= 1e12:
        return f"{value / 1e12:.2f}T ISK"
    if abs(value) >= 1e9:
        return f"{value / 1e9:.2f}B ISK"
    if abs(value) >= 1e6:
        return f"{value / 1e6:.2f}M ISK"
    return f"{value:,.0f} ISK"


def _fuel_remaining(fuel_expires_str: str | None) -> str | None:
    if not fuel_expires_str:
        return None
    try:
        expires = datetime.fromisoformat(fuel_expires_str.replace("Z", "+00:00"))
        remaining = expires - datetime.now(timezone.utc)
        if remaining.total_seconds() <= 0:
            return "EXPIRED"
        days = remaining.days
        hours = remaining.seconds // 3600
        if days > 0:
            return f"{days}d {hours}h"
        return f"{hours}h"
    except Exception:
        return None


templates.env.filters["format_isk"] = _format_isk
templates.env.filters["fuel_remaining"] = _fuel_remaining
templates.env.globals["STRUCTURE_STATE_CLASS"] = STRUCTURE_STATE_CLASS


async def _auth_client(char: Character, db: AsyncSession) -> ESIClient | None:
    try:
        async with AsyncSessionLocal() as token_db:
            result = await token_db.execute(
                select(Character).where(Character.character_id == char.character_id)
            )
            char_fresh = result.scalar_one_or_none()
            if not char_fresh:
                logger.warning("Character %s (%s) not found in database", char.character_name, char.character_id)
                return None
            token = await refresh_token(char_fresh, token_db)
            logger.debug("Successfully refreshed token for %s (%s)", char.character_name, char.character_id)
        return ESIClient(token, db=db)
    except Exception as e:
        logger.error("Token refresh failed for char %s (%s): %s", char.character_name, char.character_id, e, exc_info=True)
        return None


@router.get("", response_class=HTMLResponse)
async def corporations_page(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")

    result = await db.execute(select(Character).where(Character.user_id == user_id))
    characters = list(result.scalars().all())

    # Group by corporation
    corps: dict[int, dict] = {}
    for char in sorted(characters, key=lambda c: (c.sort_order, c.character_name)):
        corp_id = char.corporation_id
        if not corp_id:
            continue
        if corp_id not in corps:
            corps[corp_id] = {
                "corp_id": corp_id,
                "corp_name": char.corporation_name or "Unknown Corp",
                "alliance_id": char.alliance_id,
                "alliance_name": char.alliance_name,
                "characters": [],
                "available_scopes": [],
            }
        corps[corp_id]["characters"].append(char)

    # Determine which corp scopes we have per corporation
    for corp in corps.values():
        scope_set: set[str] = set()
        for char in corp["characters"]:
            for scope_name, scope_val in CORP_SCOPES.items():
                if scope_val in (char.scopes or ""):
                    scope_set.add(scope_name)
        corp["available_scopes"] = sorted(scope_set)

    sorted_corps = sorted(corps.values(), key=lambda c: c["corp_name"].lower())

    return templates.TemplateResponse("corporations.html", {
        "request": request,
        "corps": sorted_corps,
    })


@router.get("/{corp_id}/detail", response_class=HTMLResponse)
async def corp_detail(
    corp_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("")

    result = await db.execute(
        select(Character).where(
            Character.user_id == user_id,
            Character.corporation_id == corp_id,
        )
    )
    corp_chars = list(result.scalars().all())

    if not corp_chars:
        return HTMLResponse('<div class="b-empty">No characters in this corporation.</div>')

    # First character with each scope — used to authenticate API calls
    scope_char: dict[str, Character] = {}
    for char in corp_chars:
        logger.debug("Checking scopes for char %s: %r", char.character_name, char.scopes)
        for scope_name, scope_val in CORP_SCOPES.items():
            if scope_name not in scope_char and scope_val in (char.scopes or ""):
                logger.debug("Found scope %s for char %s", scope_name, char.character_name)
                scope_char[scope_name] = char
    logger.debug("Final scope_char for corp %s: %s", corp_id, list(scope_char.keys()))

    # --- Public corp info (no auth needed) ---
    pub_client = ESIClient("", db=db)
    corp_info: dict = {}
    try:
        corp_info = await esi_corp.get_corporation_info(pub_client, corp_id)
    except Exception as e:
        logger.warning("Corp info fetch failed for %s: %s", corp_id, e)

    # Resolve CEO name
    ceo_name: str | None = None
    ceo_id = corp_info.get("ceo_id")
    if ceo_id:
        try:
            ceo_data = await pub_client.get_public(f"/characters/{ceo_id}/")
            ceo_name = ceo_data.get("name")
        except Exception:
            pass

    # --- Corp member count (if scope available) ---
    member_count: int | None = None
    if "members" in scope_char:
        client = await _auth_client(scope_char["members"], db)
        if client:
            try:
                member_ids = await esi_corp.get_corporation_members(client, corp_id)
                member_count = len(member_ids)
            except Exception as e:
                logger.warning("Corp members fetch failed for %s: %s", corp_id, e)

    # --- Corp wallet (if scope available) ---
    corp_wallets: list | None = None
    corp_wallet_total: float | None = None
    if "wallet" in scope_char:
        client = await _auth_client(scope_char["wallet"], db)
        if client:
            try:
                raw = await esi_corp.get_corporation_wallets(client, corp_id)
                # Filter out empty divisions, sort by division number
                corp_wallets = sorted(
                    [d for d in raw if d.get("balance", 0) != 0],
                    key=lambda d: d["division"],
                )
                corp_wallet_total = sum(d.get("balance", 0) for d in raw)
            except Exception as e:
                logger.warning("Corp wallets fetch failed for %s: %s", corp_id, e)

    # --- Corp industry jobs (if scope available) ---
    corp_jobs: list | None = None
    if "industry" in scope_char:
        client = await _auth_client(scope_char["industry"], db)
        if client:
            try:
                raw_jobs = await esi_corp.get_corporation_jobs(client, corp_id)
                now = datetime.now(timezone.utc)
                active = []
                for job in raw_jobs:
                    if job.get("status") != "active":
                        continue
                    product_id = job.get("product_type_id")
                    product_name = await sde.type_id_to_name(db, product_id) if product_id else None
                    end_raw = job.get("end_date", "")
                    time_remaining = None
                    if end_raw:
                        end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
                        secs = (end_dt - now).total_seconds()
                        if secs > 0:
                            days = int(secs // 86400)
                            hours = int((secs % 86400) // 3600)
                            mins = int((secs % 3600) // 60)
                            if days > 0:
                                time_remaining = f"{days}d {hours}h"
                            elif hours > 0:
                                time_remaining = f"{hours}h {mins}m"
                            else:
                                time_remaining = f"{mins}m"
                        else:
                            time_remaining = "Ready"
                    active.append({
                        "activity_name": ACTIVITY_NAMES.get(job.get("activity_id", 0), "Unknown"),
                        "product_name": product_name,
                        "runs": job.get("runs", 1),
                        "time_remaining": time_remaining,
                        "installer_id": job.get("installer_id"),
                    })
                corp_jobs = active
            except Exception as e:
                logger.warning("Corp jobs fetch failed for %s: %s", corp_id, e)

    # --- Corp market orders (if scope available) ---
    corp_orders: dict | None = None
    if "orders" in scope_char:
        client = await _auth_client(scope_char["orders"], db)
        if client:
            try:
                raw_orders = await esi_corp.get_corporation_orders(client, corp_id)
                sell = [o for o in raw_orders if not o.get("is_buy_order")]
                buy = [o for o in raw_orders if o.get("is_buy_order")]
                corp_orders = {
                    "sell_count": len(sell),
                    "buy_count": len(buy),
                    "sell_value": sum(o.get("price", 0) * o.get("volume_remain", 0) for o in sell),
                    "buy_value": sum(o.get("price", 0) * o.get("volume_remain", 0) for o in buy),
                    "total_count": len(raw_orders),
                }
            except Exception as e:
                logger.warning("Corp orders fetch failed for %s: %s", corp_id, e)

    # --- Corp structures (if scope available) ---
    corp_structures: list | None = None
    if "structures" in scope_char:
        structures_char = scope_char["structures"]
        logger.debug("Structures scope found for corp %s, using character %s (ID: %s)", corp_id, structures_char.character_name, structures_char.character_id)
        client = await _auth_client(structures_char, db)
        if client:
            try:
                raw_structs = await esi_corp.get_corporation_structures(client, corp_id)
                logger.debug("Got %d raw structures for corp %s", len(raw_structs), corp_id)
                enriched = []
                for s in raw_structs:
                    type_id = s.get("type_id")
                    system_id = s.get("system_id")
                    type_name = await sde.type_id_to_name(db, type_id) if type_id else f"Type {type_id}"
                    sys_info = await sde.system_info(db, system_id) if system_id else None
                    fuel_str = _fuel_remaining(s.get("fuel_expires"))
                    state = s.get("state", "unknown")
                    enriched.append({
                        "name": s.get("name", "Unknown Structure"),
                        "type_name": type_name or f"Type {type_id}",
                        "system_name": sys_info["system_name"] if sys_info else f"System {system_id}",
                        "region": sys_info.get("region") if sys_info else None,
                        "state": state,
                        "state_class": STRUCTURE_STATE_CLASS.get(state, "is-warn"),
                        "fuel_remaining": fuel_str,
                        "fuel_expires": s.get("fuel_expires", "")[:10] if s.get("fuel_expires") else None,
                        "services": s.get("services", []),
                        "reinforce_hour": s.get("reinforce_hour"),
                        "state_timer_end": s.get("state_timer_end", "")[:10] if s.get("state_timer_end") else None,
                    })
                corp_structures = sorted(enriched, key=lambda s: s["name"])
                logger.info("Successfully fetched %d structures for corp %s", len(corp_structures), corp_id)
            except Exception as e:
                logger.error("Corp structures fetch failed for %s: %s", corp_id, e, exc_info=True)
        else:
            logger.warning("Failed to get auth client for structures scope for corp %s", corp_id)
    else:
        logger.debug("No structures scope found for corp %s. Available scopes: %s", corp_id, list(scope_char.keys()))

    # --- Corp contracts (if scope available) ---
    corp_contracts: dict | None = None
    if "contracts" in scope_char:
        client = await _auth_client(scope_char["contracts"], db)
        if client:
            try:
                raw_contracts = await esi_corp.get_corporation_contracts(client, corp_id)
                outstanding = [c for c in raw_contracts if c.get("status") == "outstanding"]
                by_type: dict[str, int] = {}
                for contract in outstanding:
                    ct = contract.get("type", "unknown").replace("_", " ").title()
                    by_type[ct] = by_type.get(ct, 0) + 1
                corp_contracts = {
                    "active_count": len(outstanding),
                    "by_type": by_type,
                }
            except Exception as e:
                logger.warning("Corp contracts fetch failed for %s: %s", corp_id, e)

    return templates.TemplateResponse("partials/corp_detail.html", {
        "request": request,
        "corp_id": corp_id,
        "corp_info": corp_info,
        "corp_chars": corp_chars,
        "ceo_name": ceo_name,
        "member_count": member_count,
        "corp_wallets": corp_wallets,
        "corp_wallet_total": corp_wallet_total,
        "corp_jobs": corp_jobs,
        "corp_orders": corp_orders,
        "corp_structures": corp_structures,
        "corp_contracts": corp_contracts,
    })
