"""Wallet journal pages — character and corporation."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.models import get_db, Character, AsyncSessionLocal
from app.esi.client import ESIClient, refresh_token
from app.esi import character as esi_char
from app.esi import corporation as esi_corp
from app.routes.corporations import _try_api_call_with_fallback

logger = logging.getLogger(__name__)

router = APIRouter(tags=["journal"])
templates = Jinja2Templates(directory="app/templates")

# ── Ref type categories for filtering ─────────────────────────────────────────

REF_TYPE_CATEGORIES = {
    "market": [
        "market_transaction", "market_escrow", "transaction_tax",
        "brokers_fee", "modify_market_order",
    ],
    "bounties": [
        "bounty_prizes", "bounty_prize", "agent_mission_reward",
        "agent_mission_time_bonus_reward", "ess_escrow_transfer",
    ],
    "industry": [
        "industry_job_tax", "manufacturing", "reprocessing_tax",
        "jump_clone_installation_fee", "planetary_import_tax",
        "planetary_export_tax", "planetary_construction",
    ],
    "contracts": [
        "contract_price", "contract_reward", "contract_collateral",
        "contract_price_payment_corp", "contract_brokers_fee",
        "contract_deposit",
    ],
    "insurance": [
        "insurance", "insurance_payout",
    ],
    "transfers": [
        "player_donation", "player_trading", "corporation_account_withdrawal",
        "office_rental_fee",
    ],
    "warfare": [
        "kill_right_fee", "war_fee", "war_ally_contract",
    ],
    "lp_store": [
        "lp_store",
    ],
}

# Flatten for reverse lookup
_TYPE_TO_CATEGORY = {}
for cat, types in REF_TYPE_CATEGORIES.items():
    for t in types:
        _TYPE_TO_CATEGORY[t] = cat

CATEGORY_LABELS = {
    "market": "Market",
    "bounties": "Bounties & Missions",
    "industry": "Industry & PI",
    "contracts": "Contracts",
    "insurance": "Insurance",
    "transfers": "Transfers & Donations",
    "warfare": "Warfare",
    "lp_store": "LP Store",
    "other": "Other",
}


def _categorize(ref_type: str) -> str:
    return _TYPE_TO_CATEGORY.get(ref_type, "other")


def _format_ref_type(ref_type: str) -> str:
    return ref_type.replace("_", " ").title() if ref_type else "Unknown"


async def _resolve_names(client: ESIClient, ids: set[int]) -> dict[int, str]:
    """Batch resolve entity IDs to names."""
    if not ids:
        return {}
    ids = [i for i in ids if i and i > 0]
    if not ids:
        return {}
    try:
        results = await client.post_public("/universe/names/", ids[:1000])
        return {r["id"]: r["name"] for r in results} if results else {}
    except Exception:
        return {}


# ── Character wallet journal ──────────────────────────────────────────────────

@router.get("/character/{character_id}/journal", response_class=HTMLResponse)
async def character_journal(
    request: Request,
    character_id: int,
    page: int = Query(1, ge=1),
    category: str = Query("all"),
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

    scope = "esi-wallet.read_character_wallet.v1"
    if scope not in (char.scopes or ""):
        return templates.TemplateResponse("journal.html", {
            "request": request, "char": char, "entries": [],
            "error": "Wallet scope not available — re-authorize this character.",
            "page": 1, "has_more": False, "category": "all",
            "categories": CATEGORY_LABELS, "is_corp": False, "corp_id": None, "division": None,
        })

    try:
        token = await refresh_token(char, db)
        client = ESIClient(token, db=db)

        # Fetch multiple pages for a richer view
        all_entries = []
        for p in range(1, min(page + 1, 4)):  # Fetch up to 3 pages
            raw = await esi_char.get_wallet_journal(client, character_id, page=p)
            if not raw:
                break
            all_entries.extend(raw)
            if len(raw) < 2500:
                break

        # Resolve entity names
        entity_ids = set()
        for e in all_entries:
            if e.get("first_party_id"):
                entity_ids.add(e["first_party_id"])
            if e.get("second_party_id"):
                entity_ids.add(e["second_party_id"])
        names = await _resolve_names(client, entity_ids)

        # Enrich entries
        entries = []
        for e in all_entries:
            ref_type = e.get("ref_type", "")
            cat = _categorize(ref_type)
            if category != "all" and cat != category:
                continue
            entries.append({
                "id": e.get("id"),
                "date": e.get("date", ""),
                "ref_type": ref_type,
                "ref_type_label": _format_ref_type(ref_type),
                "category": cat,
                "amount": e.get("amount", 0) or 0,
                "balance": e.get("balance"),
                "description": e.get("description", ""),
                "reason": e.get("reason", ""),
                "first_party": names.get(e.get("first_party_id"), ""),
                "second_party": names.get(e.get("second_party_id"), ""),
                "tax": e.get("tax"),
            })

        has_more = len(all_entries) >= 2500 * page

    except Exception as exc:
        logger.warning("Journal fetch failed for char %s: %s", character_id, exc)
        return templates.TemplateResponse("journal.html", {
            "request": request, "char": char, "entries": [],
            "error": f"Failed to load journal: {type(exc).__name__}",
            "page": page, "has_more": False, "category": category,
            "categories": CATEGORY_LABELS, "is_corp": False, "corp_id": None, "division": None,
        })

    return templates.TemplateResponse("journal.html", {
        "request": request, "char": char, "entries": entries,
        "error": None, "page": page, "has_more": has_more,
        "category": category, "categories": CATEGORY_LABELS,
        "is_corp": False, "corp_id": None, "division": None,
    })


@router.get("/character/{character_id}/journal-entries", response_class=HTMLResponse)
async def character_journal_entries(
    request: Request,
    character_id: int,
    page: int = Query(1, ge=1),
    category: str = Query("all"),
    db: AsyncSession = Depends(get_db),
):
    """Htmx partial: load more journal entries."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("")

    char_result = await db.execute(
        select(Character).where(Character.character_id == character_id, Character.user_id == user_id)
    )
    char = char_result.scalar_one_or_none()
    if not char:
        return HTMLResponse("")

    try:
        token = await refresh_token(char, db)
        client = ESIClient(token, db=db)
        raw = await esi_char.get_wallet_journal(client, character_id, page=page)

        entity_ids = set()
        for e in (raw or []):
            if e.get("first_party_id"):
                entity_ids.add(e["first_party_id"])
            if e.get("second_party_id"):
                entity_ids.add(e["second_party_id"])
        names = await _resolve_names(client, entity_ids)

        entries = []
        for e in (raw or []):
            ref_type = e.get("ref_type", "")
            cat = _categorize(ref_type)
            if category != "all" and cat != category:
                continue
            entries.append({
                "id": e.get("id"),
                "date": e.get("date", ""),
                "ref_type": ref_type,
                "ref_type_label": _format_ref_type(ref_type),
                "category": cat,
                "amount": e.get("amount", 0) or 0,
                "balance": e.get("balance"),
                "description": e.get("description", ""),
                "reason": e.get("reason", ""),
                "first_party": names.get(e.get("first_party_id"), ""),
                "second_party": names.get(e.get("second_party_id"), ""),
                "tax": e.get("tax"),
            })

        has_more = len(raw or []) >= 2500

    except Exception:
        entries = []
        has_more = False

    return templates.TemplateResponse("partials/journal_entries.html", {
        "request": request, "entries": entries, "has_more": has_more,
        "page": page, "character_id": character_id, "category": category,
        "is_corp": False, "corp_id": None, "division": None,
    })


# ── Corporation wallet journal ────────────────────────────────────────────────

@router.get("/corporations/{corp_id}/journal", response_class=HTMLResponse)
async def corp_journal(
    request: Request,
    corp_id: int,
    division: int = Query(1, ge=1, le=7),
    page: int = Query(1, ge=1),
    category: str = Query("all"),
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")

    # Find a character with corp wallet scope for this corp
    result = await db.execute(select(Character).where(Character.user_id == user_id))
    characters = list(result.scalars().all())

    scope = "esi-wallet.read_corporation_wallets.v1"
    corp_chars = [c for c in characters if c.corporation_id == corp_id and scope in (c.scopes or "")]

    if not corp_chars:
        return templates.TemplateResponse("journal.html", {
            "request": request, "char": characters[0] if characters else None,
            "entries": [], "error": "No character with corp wallet access for this corporation.",
            "page": 1, "has_more": False, "category": "all",
            "categories": CATEGORY_LABELS, "is_corp": True,
            "corp_id": corp_id, "division": division,
        })

    char = corp_chars[0]  # For template display

    try:
        # Try each character until one succeeds (handles 403 from missing Director role)
        all_entries = []
        client = None
        last_error = None
        for c in corp_chars:
            try:
                token = await refresh_token(c, db)
                client = ESIClient(token, db=db)
                for p in range(1, min(page + 1, 4)):
                    raw = await esi_corp.get_corporation_wallet_journal(client, corp_id, division, page=p)
                    if not raw:
                        break
                    all_entries.extend(raw)
                    if len(raw) < 2500:
                        break
                last_error = None
                break  # Success
            except Exception as e:
                last_error = e
                if "403" not in str(e):
                    break  # Non-403 error, don't try next char

        if last_error:
            raise last_error

        entity_ids = set()
        for e in all_entries:
            if e.get("first_party_id"):
                entity_ids.add(e["first_party_id"])
            if e.get("second_party_id"):
                entity_ids.add(e["second_party_id"])
        names = await _resolve_names(client, entity_ids)

        entries = []
        for e in all_entries:
            ref_type = e.get("ref_type", "")
            cat = _categorize(ref_type)
            if category != "all" and cat != category:
                continue
            entries.append({
                "id": e.get("id"),
                "date": e.get("date", ""),
                "ref_type": ref_type,
                "ref_type_label": _format_ref_type(ref_type),
                "category": cat,
                "amount": e.get("amount", 0) or 0,
                "balance": e.get("balance"),
                "description": e.get("description", ""),
                "reason": e.get("reason", ""),
                "first_party": names.get(e.get("first_party_id"), ""),
                "second_party": names.get(e.get("second_party_id"), ""),
                "tax": e.get("tax"),
            })

        has_more = len(all_entries) >= 2500 * page

    except Exception as exc:
        logger.warning("Corp journal fetch failed for corp %s div %s: %s", corp_id, division, exc)
        return templates.TemplateResponse("journal.html", {
            "request": request, "char": char, "entries": [],
            "error": f"Failed to load corp journal: {type(exc).__name__}",
            "page": page, "has_more": False, "category": category,
            "categories": CATEGORY_LABELS, "is_corp": True,
            "corp_id": corp_id, "division": division,
        })

    return templates.TemplateResponse("journal.html", {
        "request": request, "char": char, "entries": entries,
        "error": None, "page": page, "has_more": has_more,
        "category": category, "categories": CATEGORY_LABELS,
        "is_corp": True, "corp_id": corp_id, "division": division,
    })
