import asyncio
import secrets
import base64
import httpx
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import get_settings
from app.db.models import get_db, Character, User
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
    "esi-skills.read_skillqueue.v1",
    "esi-mail.read_mail.v1",
    "esi-characters.read_notifications.v1",
    "esi-contracts.read_character_contracts.v1",
    "esi-planets.manage_planets.v1",
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
    security_status = pub_info.get("security_status")

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

    # ── Determine which User this character belongs to ────────────────────────
    #
    # Case A: User is already logged in (session has user_id) → add character to them.
    # Case B: This character already has a User record → log in as that user.
    # Case C: Brand-new character, no existing user → create a new User account
    #         and designate this character as the main.

    session_user_id: int | None = request.session.get("user_id")

    # Load or create the Character row first
    result = await db.execute(select(Character).where(Character.character_id == character_id))
    existing_char = result.scalar_one_or_none()

    if existing_char:
        # Update tokens and public info
        existing_char.access_token = access_token
        existing_char.refresh_token = refresh_token
        existing_char.token_expiry = token_expiry
        existing_char.scopes = scopes
        existing_char.corporation_id = corporation_id
        existing_char.corporation_name = corporation_name
        existing_char.alliance_id = alliance_id
        existing_char.alliance_name = alliance_name
        existing_char.security_status = security_status
        existing_char.last_seen = datetime.now(timezone.utc)
        char = existing_char
    else:
        char = Character(
            character_id=character_id,
            character_name=verify_data["CharacterName"],
            corporation_id=corporation_id,
            corporation_name=corporation_name,
            alliance_id=alliance_id,
            alliance_name=alliance_name,
            security_status=security_status,
            access_token=access_token,
            refresh_token=refresh_token,
            token_expiry=token_expiry,
            scopes=scopes,
        )
        db.add(char)
        await db.flush()  # assign char.id without committing

    # Resolve which User account this character belongs to
    if session_user_id:
        # Case A: logged-in user is adding a new character
        user_result = await db.execute(select(User).where(User.id == session_user_id))
        user = user_result.scalar_one_or_none()
        if user is None:
            # Stale session; treat as Case C
            session_user_id = None

    if not session_user_id:
        if char.user_id:
            # Case B: character already registered — log in as that user
            user_result = await db.execute(select(User).where(User.id == char.user_id))
            user = user_result.scalar_one_or_none()
            if user is None:
                # Data inconsistency — create a new user
                user = User(
                    main_character_id=character_id,
                    last_login=datetime.now(timezone.utc),
                )
                db.add(user)
                await db.flush()
        else:
            # Case C: brand-new signup — create a User and make this the main
            user = User(
                main_character_id=character_id,
                last_login=datetime.now(timezone.utc),
            )
            db.add(user)
            await db.flush()

    # Link character to user (handles new chars and orphaned existing chars)
    char.user_id = user.id
    user.last_login = datetime.now(timezone.utc)

    await db.commit()

    # ── Update session ────────────────────────────────────────────────────────
    request.session["user_id"] = user.id
    request.session["active_character_id"] = character_id

    # Trigger an immediate sync for this character (new add or re-auth).
    from app.routes.dashboard import _sync_task, _queued_sync
    if character_id not in _queued_sync:
        _queued_sync.add(character_id)
        asyncio.create_task(_sync_task(character_id))

    return RedirectResponse("/dashboard")


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


@router.post("/switch/{character_id}")
async def switch_character(character_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=303)
    # Verify character belongs to this user
    result = await db.execute(
        select(Character).where(
            Character.character_id == character_id,
            Character.user_id == user_id,
        )
    )
    if result.scalar_one_or_none():
        request.session["active_character_id"] = character_id
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/reauth-all")
async def reauth_all(request: Request):
    """Redirect to login to re-authenticate (re-auth one character at a time via normal flow)."""
    return RedirectResponse("/auth/login")


@router.post("/set-main/{character_id}")
async def set_main_character(character_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Designate a character as the main for this user account."""
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    # Verify character belongs to this user
    result = await db.execute(
        select(Character).where(
            Character.character_id == character_id,
            Character.user_id == user_id,
        )
    )
    if not result.scalar_one_or_none():
        return JSONResponse({"error": "Character not found"}, status_code=404)

    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    if user:
        user.main_character_id = character_id
        await db.commit()

    return RedirectResponse("/characters", status_code=303)


@router.post("/remove/{character_id}")
async def remove_character(character_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/dashboard", status_code=303)

    # Only allow removing characters that belong to this user
    result = await db.execute(
        select(Character).where(
            Character.character_id == character_id,
            Character.user_id == user_id,
        )
    )
    char = result.scalar_one_or_none()
    if not char:
        return RedirectResponse("/dashboard", status_code=303)

    # If removing the main character, clear the main designation
    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    if user and user.main_character_id == character_id:
        # Find another character to promote, or leave null
        other_result = await db.execute(
            select(Character).where(
                Character.user_id == user_id,
                Character.character_id != character_id,
            )
        )
        other = other_result.scalars().first()
        user.main_character_id = other.character_id if other else None

    await db.delete(char)
    await db.commit()

    # Update active character if we just removed it
    if request.session.get("active_character_id") == character_id:
        # Pick any remaining character for this user
        remaining_result = await db.execute(
            select(Character.character_id).where(Character.user_id == user_id)
        )
        remaining = remaining_result.scalars().first()
        request.session["active_character_id"] = remaining

    return RedirectResponse("/dashboard", status_code=303)
