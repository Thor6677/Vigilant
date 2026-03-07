import secrets
import base64
import httpx
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import get_settings
from app.db.models import get_db, Character
from app.esi.client import ESIClient
from app.esi import character as esi_char
from app.esi import corporation as esi_corp

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()

EVE_SCOPES = " ".join([
    "esi-location.read_location.v1",
    "esi-location.read_ship_type.v1",
    "esi-assets.read_assets.v1",
    "esi-industry.read_character_jobs.v1",
    "esi-clones.read_clones.v1",
    "esi-clones.read_implants.v1",
    "esi-characters.read_corporation_roles.v1",
    "esi-corporations.read_corporation_membership.v1",
    "esi-industry.read_corporation_jobs.v1",
    "esi-assets.read_corporation_assets.v1",
    "esi-markets.read_character_orders.v1",
    "esi-wallet.read_character_wallet.v1",
])


@router.get("/login")
async def login(request: Request):
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    params = {
        "response_type": "code",
        "redirect_uri": settings.eve_callback_url,
        "client_id": settings.eve_client_id,
        "scope": EVE_SCOPES,
        "state": state,
    }
    return RedirectResponse(f"{settings.eve_sso_auth_url}?{urlencode(params)}")


@router.get("/callback")
async def callback(request: Request, code: str, state: str, db: AsyncSession = Depends(get_db)):
    saved_state = request.session.get("oauth_state")
    if not saved_state or saved_state != state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state.")

    credentials = base64.b64encode(
        f"{settings.eve_client_id}:{settings.eve_client_secret}".encode()
    ).decode()

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            settings.eve_sso_token_url,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
            },
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()

        verify_resp = await client.get(
            settings.eve_sso_verify_url,
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )
        verify_resp.raise_for_status()
        verify_data = verify_resp.json()

    character_id = verify_data["CharacterID"]
    access_token = token_data["access_token"]
    refresh_token = token_data["refresh_token"]
    expires_in = token_data.get("expires_in", 1200)
    token_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    scopes = verify_data.get("Scopes", "")

    esi = ESIClient(access_token)
    pub_info = await esi_char.get_public_info(esi, character_id)
    corporation_id = pub_info.get("corporation_id")
    alliance_id = pub_info.get("alliance_id")

    corporation_name = None
    alliance_name = None
    if corporation_id:
        try:
            corp_info = await esi_corp.get_corporation_info(esi, corporation_id)
            corporation_name = corp_info.get("name")
        except Exception:
            pass
    if alliance_id:
        try:
            alliance_info = await esi_corp.get_alliance_info(esi, alliance_id)
            alliance_name = alliance_info.get("name")
        except Exception:
            pass

    result = await db.execute(select(Character).where(Character.character_id == character_id))
    existing = result.scalar_one_or_none()

    if existing:
        existing.access_token = access_token
        existing.refresh_token = refresh_token
        existing.token_expiry = token_expiry
        existing.scopes = scopes
        existing.corporation_id = corporation_id
        existing.corporation_name = corporation_name
        existing.alliance_id = alliance_id
        existing.alliance_name = alliance_name
        existing.last_seen = datetime.now(timezone.utc)
    else:
        db.add(Character(
            character_id=character_id,
            character_name=verify_data["CharacterName"],
            corporation_id=corporation_id,
            corporation_name=corporation_name,
            alliance_id=alliance_id,
            alliance_name=alliance_name,
            access_token=access_token,
            refresh_token=refresh_token,
            token_expiry=token_expiry,
            scopes=scopes,
        ))

    await db.commit()

    # Set active character in session
    request.session["active_character_id"] = character_id
    if "character_ids" not in request.session:
        request.session["character_ids"] = []
    if character_id not in request.session["character_ids"]:
        request.session["character_ids"].append(character_id)

    return RedirectResponse("/dashboard")


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


@router.post("/switch/{character_id}")
async def switch_character(character_id: int, request: Request):
    if character_id in request.session.get("character_ids", []):
        request.session["active_character_id"] = character_id
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/remove/{character_id}")
async def remove_character(character_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Character).where(Character.character_id == character_id))
    char = result.scalar_one_or_none()
    if char:
        await db.delete(char)
        await db.commit()

    ids = request.session.get("character_ids", [])
    if character_id in ids:
        ids.remove(character_id)
    request.session["character_ids"] = ids

    if request.session.get("active_character_id") == character_id:
        request.session["active_character_id"] = ids[0] if ids else None

    return RedirectResponse("/dashboard", status_code=303)
