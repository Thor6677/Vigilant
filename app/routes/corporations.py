import asyncio
import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.models import get_db, Character, AsyncSessionLocal, CorpInventoryThreshold, CorpContractThreshold
from app.esi.client import ESIClient, refresh_token
from app.esi import corporation as esi_corp
from app.esi import universe as esi_universe
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


async def _try_api_call_with_fallback(
    scope_name: str,
    scope_chars: dict[str, list],
    api_call_func,
    corp_id: int,
    db: AsyncSession
) -> tuple[any, str | None]:
    """
    Try an API call with multiple characters, falling back if one fails with 403.
    Returns (result, error_message)
    """
    if scope_name not in scope_chars:
        return None, f"No characters with {scope_name} scope"

    chars_to_try = scope_chars[scope_name]
    last_error = None

    for char in chars_to_try:
        client = await _auth_client(char, db)
        if not client:
            logger.debug("Could not get auth client for %s", char.character_name)
            last_error = f"Failed to refresh token for {char.character_name}"
            continue

        try:
            logger.debug("Trying API call for %s with character %s", scope_name, char.character_name)
            result = await api_call_func(client, corp_id)
            logger.debug("API call succeeded for %s with character %s", scope_name, char.character_name)
            return result, None
        except Exception as e:
            error_str = str(e)
            logger.debug("API call failed for %s with character %s: %s", scope_name, char.character_name, error_str)
            last_error = error_str
            # If 403, try next character; otherwise break
            if "403" not in error_str:
                break

    return None, last_error


async def _auth_client(char: Character, db: AsyncSession) -> ESIClient | None:
    from app.esi.client import get_client_safe
    try:
        client = await get_client_safe(char)
        client.db = db  # Attach request-scoped db for cache operations
        return client
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

    # Split into player corps and NPC corps
    player_corps = sorted(
        [c for c in corps.values() if c["corp_id"] >= 2000000],
        key=lambda c: c["corp_name"].lower(),
    )
    npc_corps = sorted(
        [c for c in corps.values() if c["corp_id"] < 2000000],
        key=lambda c: c["corp_name"].lower(),
    )

    return templates.TemplateResponse("corporations.html", {
        "request": request,
        "corps": player_corps,
        "npc_corps": npc_corps,
        "total_corps": len(player_corps) + len(npc_corps),
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

    # All characters with each scope — used to authenticate API calls
    # Maps scope_name -> [list of characters with that scope]
    scope_chars: dict[str, list] = {}
    for char in corp_chars:
        logger.debug("Checking scopes for char %s: %r", char.character_name, char.scopes)
        for scope_name, scope_val in CORP_SCOPES.items():
            if scope_val in (char.scopes or ""):
                if scope_name not in scope_chars:
                    scope_chars[scope_name] = []
                scope_chars[scope_name].append(char)
                logger.debug("Found scope %s for char %s", scope_name, char.character_name)
    logger.debug("Final scope_chars for corp %s: %s", corp_id, {k: [c.character_name for c in v] for k, v in scope_chars.items()})

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
    if "members" in scope_chars:
        member_ids, error = await _try_api_call_with_fallback(
            "members",
            scope_chars,
            esi_corp.get_corporation_members,
            corp_id,
            db
        )
        if member_ids:
            member_count = len(member_ids)
        elif error:
            logger.warning("Corp members fetch failed for %s: %s", corp_id, error)

    # --- Corp wallet (if scope available) ---
    corp_wallets: list | None = None
    corp_wallet_total: float | None = None
    if "wallet" in scope_chars:
        raw, error = await _try_api_call_with_fallback(
            "wallet",
            scope_chars,
            esi_corp.get_corporation_wallets,
            corp_id,
            db
        )
        if raw:
            # Filter out empty divisions, sort by division number
            corp_wallets = sorted(
                [d for d in raw if d.get("balance", 0) != 0],
                key=lambda d: d["division"],
            )
            corp_wallet_total = sum(d.get("balance", 0) for d in raw)
        elif error:
            logger.warning("Corp wallets fetch failed for %s: %s", corp_id, error)

    # --- Corp industry jobs (if scope available) ---
    corp_jobs: list | None = None
    if "industry" in scope_chars:
        raw_jobs, error = await _try_api_call_with_fallback(
            "industry",
            scope_chars,
            esi_corp.get_corporation_jobs,
            corp_id,
            db
        )
        if raw_jobs:
            try:
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
                logger.warning("Corp jobs processing failed for %s: %s", corp_id, e)
        elif error:
            logger.warning("Corp jobs fetch failed for %s: %s", corp_id, error)

    # --- Corp market orders (if scope available) ---
    corp_orders: dict | None = None
    if "orders" in scope_chars:
        raw_orders, error = await _try_api_call_with_fallback(
            "orders",
            scope_chars,
            esi_corp.get_corporation_orders,
            corp_id,
            db
        )
        if raw_orders:
            sell = [o for o in raw_orders if not o.get("is_buy_order")]
            buy = [o for o in raw_orders if o.get("is_buy_order")]
            corp_orders = {
                "sell_count": len(sell),
                "buy_count": len(buy),
                "sell_value": sum(o.get("price", 0) * o.get("volume_remain", 0) for o in sell),
                "buy_value": sum(o.get("price", 0) * o.get("volume_remain", 0) for o in buy),
                "total_count": len(raw_orders),
            }
        elif error:
            logger.warning("Corp orders fetch failed for %s: %s", corp_id, error)

    # --- Corp structures (if scope available) ---
    corp_structures: list | None = None
    if "structures" in scope_chars:
        raw_structs, error = await _try_api_call_with_fallback(
            "structures",
            scope_chars,
            esi_corp.get_corporation_structures,
            corp_id,
            db
        )
        if raw_structs:
            # Cache structure names for use across the app
            await esi_universe.cache_corp_structures(db, raw_structs)
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
            # Auto-detect reinforced structure timers
            try:
                from app.routes.structure_timers import sync_esi_structure_timers
                await sync_esi_structure_timers(db, raw_structs)
            except Exception as e:
                logger.warning("Structure timer sync failed: %s", e)
        elif error:
            logger.error("Corp structures fetch failed for %s: %s", corp_id, error)
    else:
        logger.debug("No structures scope found for corp %s. Available scopes: %s", corp_id, list(scope_chars.keys()))

    # --- Corp contracts (if scope available) ---
    corp_contracts: dict | None = None
    if "contracts" in scope_chars:
        raw_contracts, error = await _try_api_call_with_fallback(
            "contracts",
            scope_chars,
            esi_corp.get_corporation_contracts,
            corp_id,
            db
        )
        if raw_contracts:
            outstanding = [c for c in raw_contracts if c.get("status") == "outstanding"]
            by_type: dict[str, int] = {}
            for contract in outstanding:
                ct = contract.get("type", "unknown").replace("_", " ").title()
                by_type[ct] = by_type.get(ct, 0) + 1
            corp_contracts = {
                "active_count": len(outstanding),
                "by_type": by_type,
            }
        elif error:
            logger.warning("Corp contracts fetch failed for %s: %s", corp_id, error)

    # --- Inventory threshold counts (for nav badge) ---
    inv_result = await db.execute(
        select(CorpInventoryThreshold).where(
            CorpInventoryThreshold.user_id == user_id,
            CorpInventoryThreshold.corp_id == corp_id,
        )
    )
    inv_thresholds = inv_result.scalars().all()
    inv_alert_count = sum(1 for t in inv_thresholds if t.alert_state in ("low", "critical"))

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
        "inv_alert_count": inv_alert_count,
    })


# ── Corp Inventory Tracker ────────────────────────────────────────────────────

HANGAR_LABELS = {
    "": "All Hangars",
    "CorpSAG1": "Hangar 1",
    "CorpSAG2": "Hangar 2",
    "CorpSAG3": "Hangar 3",
    "CorpSAG4": "Hangar 4",
    "CorpSAG5": "Hangar 5",
    "CorpSAG6": "Hangar 6",
    "CorpSAG7": "Hangar 7",
}
CORP_HANGAR_FLAGS = {"CorpSAG1", "CorpSAG2", "CorpSAG3", "CorpSAG4", "CorpSAG5", "CorpSAG6", "CorpSAG7", "CorpDeliveries"}

templates.env.globals["HANGAR_LABELS"] = HANGAR_LABELS


async def _get_corp_scope_chars(user_id: int, corp_id: int, db: AsyncSession) -> dict[str, list]:
    """Get characters grouped by scope for a corp."""
    result = await db.execute(
        select(Character).where(Character.user_id == user_id, Character.corporation_id == corp_id)
    )
    corp_chars = list(result.scalars().all())
    scope_chars: dict[str, list] = {}
    for char in corp_chars:
        for scope_name, scope_val in CORP_SCOPES.items():
            if scope_val in (char.scopes or ""):
                scope_chars.setdefault(scope_name, []).append(char)
    return scope_chars


def _build_office_to_structure_map(all_assets: list) -> dict[int, int]:
    """Map office item_ids to structure_ids.

    Corp hangar items have location_id = office item_id (not the structure).
    Office items have location_flag='OfficeFolder' and location_id = structure_id.
    """
    return {
        a["item_id"]: a["location_id"]
        for a in all_assets
        if a.get("location_flag") == "OfficeFolder"
    }


@router.get("/{corp_id}/inventory", response_class=HTMLResponse)
async def corp_inventory(corp_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Inventory tracker page for a corporation."""
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")

    # Get corp name
    result = await db.execute(
        select(Character).where(Character.user_id == user_id, Character.corporation_id == corp_id)
    )
    corp_chars = list(result.scalars().all())
    if not corp_chars:
        return RedirectResponse("/corporations")
    corp_name = corp_chars[0].corporation_name or "Unknown Corp"

    # Get existing thresholds
    thresh_result = await db.execute(
        select(CorpInventoryThreshold).where(
            CorpInventoryThreshold.user_id == user_id,
            CorpInventoryThreshold.corp_id == corp_id,
        ).order_by(CorpInventoryThreshold.location_name, CorpInventoryThreshold.type_name)
    )
    thresholds = thresh_result.scalars().all()

    # Group by location
    by_location: dict[int, dict] = {}
    for t in thresholds:
        lid = t.location_id
        if lid not in by_location:
            by_location[lid] = {"location_name": t.location_name or f"Location {lid}", "items": []}
        by_location[lid]["items"].append(t)

    return templates.TemplateResponse("corp_inventory.html", {
        "request": request,
        "corp_id": corp_id,
        "corp_name": corp_name,
        "by_location": by_location,
        "thresholds": thresholds,
        "hangar_labels": HANGAR_LABELS,
    })


@router.get("/{corp_id}/inventory/scan", response_class=HTMLResponse)
async def corp_inventory_scan(corp_id: int, location_id: int = 0, request: Request = None, db: AsyncSession = Depends(get_db)):
    """Scan corp hangar items at a specific structure."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("")
    if not location_id:
        return HTMLResponse('<div class="b-empty">Select a structure above.</div>')

    scope_chars = await _get_corp_scope_chars(user_id, corp_id, db)
    if "assets" not in scope_chars:
        return HTMLResponse('<div class="b-empty">No characters with corp asset scope.</div>')

    raw_assets, error = await _try_api_call_with_fallback(
        "assets", scope_chars, esi_corp.get_corporation_assets, corp_id, db
    )
    if not raw_assets:
        return HTMLResponse(f'<div class="b-empty">Failed to fetch assets{": " + error if error else ""}.</div>')

    # Map office item_ids to structure_ids, then filter hangar items at the target structure
    office_map = _build_office_to_structure_map(raw_assets)
    hangar_assets = [
        a for a in raw_assets
        if a.get("location_flag", "") in CORP_HANGAR_FLAGS
        and office_map.get(a.get("location_id")) == location_id
    ]

    if not hangar_assets:
        return HTMLResponse('<div class="b-empty">No items found in corp hangars at this structure.</div>')

    # Aggregate: (type_id, flag) -> quantity
    agg: dict[tuple, int] = {}
    type_ids: set[int] = set()
    for a in hangar_assets:
        key = (a["type_id"], a.get("location_flag", ""))
        agg[key] = agg.get(key, 0) + a.get("quantity", 1)
        type_ids.add(a["type_id"])

    # Resolve type names
    type_names = await sde.type_ids_to_names(db, list(type_ids)) if type_ids else {}

    # Get location name from structures
    location_name = f"Location {location_id}"
    if "structures" in scope_chars:
        structs, _ = await _try_api_call_with_fallback(
            "structures", scope_chars, esi_corp.get_corporation_structures, corp_id, db
        )
        if structs:
            await esi_universe.cache_corp_structures(db, structs)
            for s in structs:
                if s.get("structure_id") == location_id:
                    location_name = s.get("name", location_name)
                    break
    if location_name.startswith("Location "):
        cached = await esi_universe.get_cached_structure_name(db, location_id)
        if cached:
            location_name = cached

    # Already-monitored items
    existing = await db.execute(
        select(CorpInventoryThreshold).where(
            CorpInventoryThreshold.user_id == user_id,
            CorpInventoryThreshold.corp_id == corp_id,
            CorpInventoryThreshold.location_id == location_id,
        )
    )
    monitored_keys = {(t.type_id, t.location_flag) for t in existing.scalars().all()}

    # Build item list sorted by type name
    items = []
    for (tid, flag), qty in agg.items():
        items.append({
            "location_id": location_id,
            "type_id": tid,
            "type_name": type_names.get(tid, f"Type {tid}"),
            "flag": flag,
            "flag_label": HANGAR_LABELS.get(flag, flag),
            "quantity": qty,
            "monitored": (tid, flag) in monitored_keys,
        })
    items.sort(key=lambda x: x["type_name"])

    return templates.TemplateResponse("partials/corp_inventory_scan.html", {
        "request": request,
        "corp_id": corp_id,
        "location_name": location_name,
        "items": items,
    })


@router.get("/{corp_id}/inventory/type-search", response_class=HTMLResponse)
async def corp_inventory_type_search(corp_id: int, q: str = "", db: AsyncSession = Depends(get_db)):
    """SDE type search for adding items to monitor."""
    if len(q) < 2:
        return HTMLResponse("")
    results = await sde.search_types(db, q, limit=12)
    html = ""
    for r in results:
        html += (
            f'<div class="b-row" style="cursor:pointer;" '
            f'onclick="selectSearchItem({r["type_id"]}, \'{r["type_name"].replace(chr(39), "&#39;")}\')">'
            f'<img src="https://images.evetech.net/types/{r["type_id"]}/icon?size=32" '
            f'style="width:24px;height:24px;border:1px solid var(--border);">'
            f'<span class="b-row-val" style="text-align:left;flex:1;">{r["type_name"]}</span>'
            f'</div>'
        )
    return HTMLResponse(html)


@router.get("/{corp_id}/inventory/structures", response_class=HTMLResponse)
async def corp_inventory_structures(corp_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Return structure options for location picker."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("")

    scope_chars = await _get_corp_scope_chars(user_id, corp_id, db)
    structs, _ = await _try_api_call_with_fallback(
        "structures", scope_chars, esi_corp.get_corporation_structures, corp_id, db
    )
    html = '<option value="">Select structure...</option>'
    if structs:
        await esi_universe.cache_corp_structures(db, structs)
        for s in sorted(structs, key=lambda x: x.get("name", "")):
            sid = s.get("structure_id", 0)
            name = s.get("name", "Unknown")
            html += f'<option value="{sid}">{name}</option>'
    return HTMLResponse(html)


@router.post("/{corp_id}/inventory/threshold", response_class=HTMLResponse)
async def corp_inventory_add_threshold(corp_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Add or update an inventory threshold."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("")

    form = await request.form()
    type_id = int(form.get("type_id", 0))
    location_id = int(form.get("location_id", 0))
    location_flag = form.get("location_flag", "")
    threshold_low = int(form.get("threshold_low", 0))
    threshold_critical = int(form.get("threshold_critical", 0))
    location_name = form.get("location_name", "")
    type_name = form.get("type_name", "")

    if not type_id or not location_id:
        return HTMLResponse('<div class="b-empty" style="color:var(--danger);">Item and location are required.</div>')

    # Resolve names if not provided
    if not type_name:
        tn = await sde.type_id_to_name(db, type_id)
        type_name = tn or f"Type {type_id}"

    # Check for existing threshold
    existing = await db.execute(
        select(CorpInventoryThreshold).where(
            CorpInventoryThreshold.user_id == user_id,
            CorpInventoryThreshold.corp_id == corp_id,
            CorpInventoryThreshold.location_id == location_id,
            CorpInventoryThreshold.type_id == type_id,
            CorpInventoryThreshold.location_flag == location_flag,
        )
    )
    thresh = existing.scalar_one_or_none()
    if thresh:
        thresh.threshold_low = threshold_low
        thresh.threshold_critical = threshold_critical
    else:
        thresh = CorpInventoryThreshold(
            user_id=user_id,
            corp_id=corp_id,
            location_id=location_id,
            location_name=location_name,
            location_flag=location_flag,
            type_id=type_id,
            type_name=type_name,
            threshold_low=threshold_low,
            threshold_critical=threshold_critical,
        )
        db.add(thresh)
    await db.commit()

    # Return refreshed items partial
    return await _render_inventory_items(user_id, corp_id, db, request)


@router.post("/{corp_id}/inventory/threshold/{threshold_id}/delete", response_class=HTMLResponse)
async def corp_inventory_delete_threshold(corp_id: int, threshold_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Remove a tracked item."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("")

    result = await db.execute(
        select(CorpInventoryThreshold).where(
            CorpInventoryThreshold.id == threshold_id,
            CorpInventoryThreshold.user_id == user_id,
        )
    )
    thresh = result.scalar_one_or_none()
    if thresh:
        await db.delete(thresh)
        await db.commit()

    return await _render_inventory_items(user_id, corp_id, db, request)


@router.post("/{corp_id}/inventory/check", response_class=HTMLResponse)
async def corp_inventory_check(corp_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Refresh inventory counts against thresholds."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("")

    await check_corp_inventory(user_id, corp_id, db)
    return await _render_inventory_items(user_id, corp_id, db, request)


async def check_corp_inventory(user_id: int, corp_id: int, db: AsyncSession, emit_notifications: bool = False):
    """Check live corp assets against thresholds and update alert states."""
    thresh_result = await db.execute(
        select(CorpInventoryThreshold).where(
            CorpInventoryThreshold.user_id == user_id,
            CorpInventoryThreshold.corp_id == corp_id,
        )
    )
    thresholds = thresh_result.scalars().all()
    if not thresholds:
        return

    scope_chars = await _get_corp_scope_chars(user_id, corp_id, db)
    if "assets" not in scope_chars:
        return

    wanted_structure_ids = {t.location_id for t in thresholds}

    raw_assets, _ = await _try_api_call_with_fallback(
        "assets", scope_chars, esi_corp.get_corporation_assets, corp_id, db
    )
    if raw_assets is None:
        return

    # Map office containers to structure IDs
    office_map = _build_office_to_structure_map(raw_assets)

    # Aggregate quantities: (structure_id, type_id, flag) -> qty
    agg: dict[tuple, int] = {}
    for a in raw_assets:
        if a.get("location_flag", "") not in CORP_HANGAR_FLAGS:
            continue
        struct_id = office_map.get(a.get("location_id"))
        if struct_id not in wanted_structure_ids:
            continue
        key = (struct_id, a["type_id"], a.get("location_flag", ""))
        agg[key] = agg.get(key, 0) + a.get("quantity", 1)

    now = datetime.now(timezone.utc)
    for t in thresholds:
        if t.location_flag:
            qty = agg.get((t.location_id, t.type_id, t.location_flag), 0)
        else:
            # Sum across all hangars at this structure
            qty = sum(v for (sid, tid, _), v in agg.items() if sid == t.location_id and tid == t.type_id)

        old_state = t.alert_state
        if t.threshold_critical > 0 and qty <= t.threshold_critical:
            new_state = "critical"
        elif t.threshold_low > 0 and qty <= t.threshold_low:
            new_state = "low"
        else:
            new_state = "ok"

        t.current_quantity = qty
        t.alert_state = new_state
        t.last_checked = now

        # Emit notifications on state transitions
        if emit_notifications and new_state != old_state and new_state != "ok":
            from app.routes.dashboard import _emit_notification
            ntype = "inventory_critical" if new_state == "critical" else "inventory_low"
            title = "Inventory Critical" if new_state == "critical" else "Inventory Low"
            _emit_notification(user_id, {
                "type": ntype,
                "title": title,
                "body": f"{t.type_name} at {t.location_name} — {qty}/{t.threshold_critical if new_state == 'critical' else t.threshold_low}",
                "icon": f"https://images.evetech.net/types/{t.type_id}/icon?size=64",
            })

    await db.commit()


async def _render_inventory_items(user_id: int, corp_id: int, db: AsyncSession, request: Request) -> HTMLResponse:
    """Render the inventory items partial."""
    thresh_result = await db.execute(
        select(CorpInventoryThreshold).where(
            CorpInventoryThreshold.user_id == user_id,
            CorpInventoryThreshold.corp_id == corp_id,
        ).order_by(CorpInventoryThreshold.location_name, CorpInventoryThreshold.type_name)
    )
    thresholds = thresh_result.scalars().all()

    by_location: dict[int, dict] = {}
    for t in thresholds:
        lid = t.location_id
        if lid not in by_location:
            by_location[lid] = {"location_name": t.location_name or f"Location {lid}", "items": []}
        by_location[lid]["items"].append(t)

    return templates.TemplateResponse("partials/corp_inventory_items.html", {
        "request": request,
        "corp_id": corp_id,
        "by_location": by_location,
        "thresholds": thresholds,
    })


# ── Contract Threshold Monitoring ──────────────────────────────────────────────

@router.get("/{corp_id}/contracts", response_class=HTMLResponse)
async def corp_contracts_page(corp_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Contract threshold monitoring page."""
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=302)

    corp_name = await _get_corp_name(corp_id, user_id, db)

    thresh_result = await db.execute(
        select(CorpContractThreshold).where(
            CorpContractThreshold.user_id == user_id,
            CorpContractThreshold.corp_id == corp_id,
        ).order_by(CorpContractThreshold.match_label)
    )
    thresholds = thresh_result.scalars().all()

    return templates.TemplateResponse("corp_contracts.html", {
        "request": request,
        "corp_id": corp_id,
        "corp_name": corp_name,
        "thresholds": thresholds,
    })


@router.post("/{corp_id}/contracts/threshold", response_class=HTMLResponse)
async def corp_contract_add_threshold(corp_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Add or update a contract threshold."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("")

    form = await request.form()
    match_type = form.get("match_type", "title")  # "item" or "title"
    threshold_low = int(form.get("threshold_low", 0))
    threshold_critical = int(form.get("threshold_critical", 0))

    if match_type == "item":
        type_id = int(form.get("type_id", 0))
        if not type_id:
            return HTMLResponse('<div class="b-empty" style="color:var(--danger);">Select an item type.</div>')
        type_name = form.get("type_name", "")
        if not type_name:
            tn = await sde.type_id_to_name(db, type_id)
            type_name = tn or f"Type {type_id}"
        match_value = str(type_id)
        match_label = type_name
    else:
        keyword = form.get("keyword", "").strip()
        if not keyword:
            return HTMLResponse('<div class="b-empty" style="color:var(--danger);">Enter a title keyword.</div>')
        match_value = keyword
        match_label = f'Title: "{keyword}"'
        type_id = None

    # Check for existing threshold
    existing = await db.execute(
        select(CorpContractThreshold).where(
            CorpContractThreshold.user_id == user_id,
            CorpContractThreshold.corp_id == corp_id,
            CorpContractThreshold.match_type == match_type,
            CorpContractThreshold.match_value == match_value,
        )
    )
    thresh = existing.scalar_one_or_none()
    if thresh:
        thresh.threshold_low = threshold_low
        thresh.threshold_critical = threshold_critical
    else:
        thresh = CorpContractThreshold(
            user_id=user_id,
            corp_id=corp_id,
            match_type=match_type,
            match_value=match_value,
            match_label=match_label,
            type_id=type_id,
            threshold_low=threshold_low,
            threshold_critical=threshold_critical,
        )
        db.add(thresh)
    await db.commit()

    return await _render_contract_items(user_id, corp_id, db, request)


@router.post("/{corp_id}/contracts/threshold/{threshold_id}/delete", response_class=HTMLResponse)
async def corp_contract_delete_threshold(corp_id: int, threshold_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Remove a tracked contract threshold."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("")

    result = await db.execute(
        select(CorpContractThreshold).where(
            CorpContractThreshold.id == threshold_id,
            CorpContractThreshold.user_id == user_id,
        )
    )
    thresh = result.scalar_one_or_none()
    if thresh:
        await db.delete(thresh)
        await db.commit()

    return await _render_contract_items(user_id, corp_id, db, request)


@router.post("/{corp_id}/contracts/check", response_class=HTMLResponse)
async def corp_contract_check(corp_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Manually refresh contract counts."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("")

    await check_corp_contracts(user_id, corp_id, db)
    return await _render_contract_items(user_id, corp_id, db, request)


async def check_corp_contracts(user_id: int, corp_id: int, db: AsyncSession, emit_notifications: bool = False):
    """Check outstanding corp contracts against thresholds and update alert states."""
    thresh_result = await db.execute(
        select(CorpContractThreshold).where(
            CorpContractThreshold.user_id == user_id,
            CorpContractThreshold.corp_id == corp_id,
        )
    )
    thresholds = thresh_result.scalars().all()
    if not thresholds:
        return

    scope_chars = await _get_corp_scope_chars(user_id, corp_id, db)
    if "contracts" not in scope_chars:
        return

    # Fetch all outstanding corp contracts
    contracts, _ = await _try_api_call_with_fallback(
        "contracts", scope_chars, esi_corp.get_corporation_contracts, corp_id, db
    )
    if contracts is None:
        return

    outstanding = [c for c in contracts if c.get("status") == "outstanding" and c.get("type") == "item_exchange"]

    # Check which thresholds need item-level matching
    item_thresholds = [t for t in thresholds if t.match_type == "item"]
    title_thresholds = [t for t in thresholds if t.match_type == "title"]

    # For title thresholds — simple keyword match on contract title
    title_counts: dict[int, int] = {}
    for t in title_thresholds:
        keyword = t.match_value.lower()
        count = sum(1 for c in outstanding if keyword in (c.get("title") or "").lower())
        title_counts[t.id] = count

    # For item thresholds — need to fetch contract items
    item_counts: dict[int, int] = {}
    if item_thresholds:
        wanted_type_ids = {t.type_id for t in item_thresholds if t.type_id}

        # Fetch items for each outstanding contract (with semaphore to limit concurrency)
        sem = asyncio.Semaphore(10)
        contract_items_cache: dict[int, set[int]] = {}  # contract_id -> set of type_ids

        async def fetch_items(contract: dict):
            cid = contract.get("contract_id")
            if not cid:
                return
            async with sem:
                try:
                    # Use a fresh session for concurrent requests
                    async with AsyncSessionLocal() as sess:
                        char = scope_chars["contracts"][0]
                        token = await refresh_token(char, sess)
                        client = ESIClient(token, db=sess)
                        items = await esi_corp.get_corporation_contract_items(client, corp_id, cid)
                        if isinstance(items, list):
                            contract_items_cache[cid] = {i.get("type_id") for i in items}
                except Exception:
                    pass

        await asyncio.gather(*[fetch_items(c) for c in outstanding])

        # Count contracts containing each wanted type
        for t in item_thresholds:
            count = sum(1 for type_ids in contract_items_cache.values() if t.type_id in type_ids)
            item_counts[t.id] = count

    # Update all thresholds
    now = datetime.now(timezone.utc)
    for t in thresholds:
        count = title_counts.get(t.id) or item_counts.get(t.id, 0)
        old_state = t.alert_state

        if t.threshold_critical > 0 and count <= t.threshold_critical:
            new_state = "critical"
        elif t.threshold_low > 0 and count <= t.threshold_low:
            new_state = "low"
        else:
            new_state = "ok"

        t.current_count = count
        t.alert_state = new_state
        t.last_checked = now

        if emit_notifications and new_state != old_state and new_state != "ok":
            from app.routes.dashboard import _emit_notification
            ntype = "contract_critical" if new_state == "critical" else "contract_low"
            title = "Contracts Critical" if new_state == "critical" else "Contracts Low"
            _emit_notification(user_id, {
                "type": ntype,
                "title": title,
                "body": f"{t.match_label} — {count}/{t.threshold_critical if new_state == 'critical' else t.threshold_low} contracts",
                "icon": f"https://images.evetech.net/types/{t.type_id}/icon?size=64" if t.type_id else "/static/logo.png",
            })

    await db.commit()


async def _get_corp_name(corp_id: int, user_id: int, db: AsyncSession) -> str:
    """Get corp name from user's characters."""
    result = await db.execute(select(Character).where(Character.user_id == user_id))
    for char in result.scalars().all():
        if char.corporation_id == corp_id and char.corporation_name:
            return char.corporation_name
    return f"Corp {corp_id}"


async def _render_contract_items(user_id: int, corp_id: int, db: AsyncSession, request: Request) -> HTMLResponse:
    """Render the contract threshold items partial."""
    thresh_result = await db.execute(
        select(CorpContractThreshold).where(
            CorpContractThreshold.user_id == user_id,
            CorpContractThreshold.corp_id == corp_id,
        ).order_by(CorpContractThreshold.match_label)
    )
    thresholds = thresh_result.scalars().all()

    return templates.TemplateResponse("partials/corp_contract_items.html", {
        "request": request,
        "corp_id": corp_id,
        "thresholds": thresholds,
    })
