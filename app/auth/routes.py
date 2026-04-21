import asyncio
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
from app.db.models import get_db, Character, User
from app.esi.client import ESIClient
from app.esi import character as esi_char
from app.esi import corporation as esi_corp

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()

EVE_SCOPES = " ".join([
    "esi-location.read_location.v1",
    "esi-location.read_online.v1",
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
    "esi-skills.read_skills.v1",
    "esi-mail.read_mail.v1",
    "esi-characters.read_notifications.v1",
    "esi-fittings.read_fittings.v1",
    "esi-characters.read_blueprints.v1",
    "esi-industry.read_character_mining.v1",
    "esi-contracts.read_character_contracts.v1",
    "esi-planets.manage_planets.v1",
    "esi-search.search_structures.v1",
    # Structure name resolution — needed by industry jobs, assets, and the
    # star map to turn structure IDs into human-readable names + system info.
    "esi-universe.read_structures.v1",
    # Star map gate route planner — push routes to in-game autopilot
    "esi-ui.write_waypoint.v1",
    # Corp-level scopes — only granted by EVE SSO if the character holds the
    # appropriate in-game corporation role (Accountant, Factory Manager, etc.)
    "esi-wallet.read_corporation_wallets.v1",
    "esi-markets.read_corporation_orders.v1",
    "esi-corporations.read_structures.v1",
    "esi-contracts.read_corporation_contracts.v1",
    "esi-corporations.read_blueprints.v1",
    "esi-industry.read_corporation_mining.v1",
])


def _start_oauth(request: Request, intent: str) -> RedirectResponse:
    """Build the EVE SSO redirect. Intent is 'login' or 'add_character'."""
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    request.session["oauth_intent"] = intent
    params = {
        "response_type": "code",
        "redirect_uri": settings.eve_callback_url,
        "client_id": settings.eve_client_id,
        "scope": EVE_SCOPES,
        "state": state,
    }
    return RedirectResponse(f"{settings.eve_sso_auth_url}?{urlencode(params)}")


@router.get("/login")
async def login(request: Request):
    return _start_oauth(request, "login")


@router.get("/add-character")
async def add_character_route(request: Request):
    """Start EVE SSO to add a character to the current user account."""
    if not request.session.get("user_id"):
        return RedirectResponse("/auth/login")
    return _start_oauth(request, "add_character")


@router.get("/callback")
async def callback(request: Request, code: str, state: str, db: AsyncSession = Depends(get_db)):
    saved_state = request.session.get("oauth_state")
    if not saved_state or saved_state != state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state.")

    intent = request.session.pop("oauth_intent", "login")
    request.session.pop("oauth_state", None)
    if intent not in ("login", "add_character"):
        intent = "login"

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
    birthday_str = pub_info.get("birthday")  # ISO 8601 format
    birthday = None
    if birthday_str:
        try:
            from dateutil import parser
            birthday = parser.isoparse(birthday_str).replace(tzinfo=None)
        except Exception:
            pass

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
    existing_char = result.scalar_one_or_none()

    # ── Registration allowlist check ──────────────────────────────────
    # If allowlist entries exist, only allowed characters/corps/alliances can register new accounts
    if not existing_char or not existing_char.user_id:
        from app.db.models import RegistrationAllowlist
        allow_result = await db.execute(select(RegistrationAllowlist))
        allowlist = allow_result.scalars().all()
        if allowlist:  # Allowlist is active
            allowed = False
            for entry in allowlist:
                if entry.entry_type == "character" and entry.eve_id == character_id:
                    allowed = True
                    break
                if entry.entry_type == "corporation" and entry.eve_id == corporation_id:
                    allowed = True
                    break
                if entry.entry_type == "alliance" and entry.eve_id == alliance_id:
                    allowed = True
                    break
            if not allowed:
                return templates.TemplateResponse("index.html", {
                    "request": request,
                    "error": "Registration is restricted. Your character, corporation, or alliance is not on the allowlist.",
                })

    if intent == "login":
        if existing_char and existing_char.user_id:
            # Character already has an owner — log in as that user.
            user_result = await db.execute(select(User).where(User.id == existing_char.user_id))
            user = user_result.scalar_one_or_none()
            if not user:
                # Stale FK — recreate user and re-claim character.
                user = User()
                db.add(user)
                await db.flush()
                existing_char.user_id = user.id
                existing_char.is_main = True
        elif existing_char:
            # Orphaned character (created before user system) — adopt into new account.
            user = User()
            db.add(user)
            await db.flush()
            existing_char.user_id = user.id
            existing_char.is_main = True
        else:
            # Brand new character — create both.
            user = User()
            db.add(user)
            await db.flush()
            existing_char = Character(
                character_id=character_id,
                character_name=verify_data["CharacterName"],
                user_id=user.id,
                is_main=True,
                access_token=access_token,
                refresh_token=refresh_token,
                token_expiry=token_expiry,
                scopes=scopes,
                birthday=birthday,
                corporation_id=corporation_id,
                corporation_name=corporation_name,
                alliance_id=alliance_id,
                alliance_name=alliance_name,
                security_status=security_status,
            )
            db.add(existing_char)

        # Update tokens and metadata.
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
        user.last_login = datetime.now(timezone.utc)

        await db.commit()

        request.session["user_id"] = user.id
        request.session["active_character_id"] = character_id
        request.session["is_admin"] = user.role in ("admin", "manager")
        request.session["role"] = user.role

    else:  # add_character
        current_user_id = request.session.get("user_id")
        if not current_user_id:
            return RedirectResponse("/auth/login")

        if existing_char and existing_char.user_id and existing_char.user_id != current_user_id:
            # Character is already owned by a different account — reject.
            return RedirectResponse("/dashboard?error=character_claimed")

        if existing_char:
            existing_char.user_id = current_user_id
        else:
            existing_char = Character(
                character_id=character_id,
                character_name=verify_data["CharacterName"],
                user_id=current_user_id,
                is_main=False,
                access_token=access_token,
                refresh_token=refresh_token,
                token_expiry=token_expiry,
                scopes=scopes,
                birthday=birthday,
                corporation_id=corporation_id,
                corporation_name=corporation_name,
                alliance_id=alliance_id,
                alliance_name=alliance_name,
                security_status=security_status,
            )
            db.add(existing_char)

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

        await db.commit()

        request.session["active_character_id"] = character_id
        # Refresh admin flag
        user_result = await db.execute(select(User).where(User.id == current_user_id))
        current_user = user_result.scalar_one_or_none()
        if current_user:
            request.session["is_admin"] = current_user.role in ("admin", "manager")
            request.session["role"] = current_user.role

    # Trigger an immediate sync for this character.
    from app.routes.dashboard import _sync_task, _queued_sync
    if character_id not in _queued_sync:
        _queued_sync[character_id] = datetime.now(timezone.utc)
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

    result = await db.execute(
        select(Character).where(
            Character.character_id == character_id,
            Character.user_id == user_id,
        )
    )
    if result.scalar_one_or_none():
        request.session["active_character_id"] = character_id
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/remove/{character_id}")
async def remove_character(character_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/dashboard", status_code=303)

    result = await db.execute(
        select(Character).where(
            Character.character_id == character_id,
            Character.user_id == user_id,
        )
    )
    char = result.scalar_one_or_none()
    if not char:
        return RedirectResponse("/dashboard", status_code=303)

    if char.is_main:
        # Cannot remove the main character — it is the account identity.
        return RedirectResponse("/dashboard?error=cannot_remove_main", status_code=303)

    await db.delete(char)
    await db.commit()

    if request.session.get("active_character_id") == character_id:
        # Fall back to the main character.
        main_result = await db.execute(
            select(Character).where(Character.user_id == user_id, Character.is_main == True)
        )
        main_char = main_result.scalar_one_or_none()
        request.session["active_character_id"] = main_char.character_id if main_char else None

    return RedirectResponse("/dashboard", status_code=303)
