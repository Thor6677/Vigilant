"""EVE SSO: logging in, connecting characters, and changing what they share.

Three ways into SSO, and only the last two ever change a character's
permissions:

* ``GET /auth/login`` — identity only. Proves who the user is and logs them
  in; the stored data token and its scopes are left exactly as they were. A
  character Vigilant has never seen gets an account and is sent to the picker.
* ``POST /auth/authorize`` with intent ``signup`` / ``add`` — from the
  permission picker (``GET /auth/connect``): SSO is asked for exactly the
  permissions the user ticked, nothing else.
* ``POST /auth/authorize`` with intent ``update`` — from the Account page:
  re-authorize one character with a new selection. Narrowing revokes the old
  refresh token at EVE and, if the user asked, deletes data collected under the
  withdrawn permissions (app/auth/purge.py).

The catalog of permissions lives in app/auth/scopes.py; ESIClient refuses any
call outside what a token carries (app/esi/scope_guard.py).
"""
import asyncio
import base64
import logging
import secrets
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import scopes as perms
from app.auth.purge import clear_live_state, purge_history
from app.auth.tokens import issued_to_us, revoke_refresh_token
from app.config import get_settings
from app.db.models import AdminAuditLog, Character, User, get_db
from app.esi import character as esi_char
from app.esi import corporation as esi_corp
from app.esi import scope_guard
from app.esi.client import ESIClient, TokenRevoked, _do_refresh

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)

LOGIN = "login"
SIGNUP = "signup"
ADD = "add"
UPDATE = "update"
_INTENTS = (LOGIN, SIGNUP, ADD, UPDATE)


def _sso_redirect(request: Request, intent: str, keys: list[str] | None = None,
                  character_id: int | None = None, purge: bool = False) -> RedirectResponse:
    """Remember what was asked for, then send the browser to EVE SSO.

    The session holds permission KEYS, not scope strings: the session is a
    signed cookie with a ~4 KB budget, and the keys re-expand through the
    catalog on the way back.
    """
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    request.session["oauth_pending"] = {
        "intent": intent,
        "keys": list(keys or []),
        "character_id": character_id,
        "purge": bool(purge),
    }
    params = {
        "response_type": "code",
        "redirect_uri": settings.eve_callback_url,
        "client_id": settings.eve_client_id,
        "state": state,
    }
    if intent == LOGIN:
        scope = settings.eve_login_scope.strip()
    else:
        scope = " ".join(perms.scopes_for(keys or []))
    if scope:
        params["scope"] = scope
    # 303: the permission form arrives as a POST and must become a GET at EVE.
    return RedirectResponse(f"{settings.eve_sso_auth_url}?{urlencode(params)}", status_code=303)


def _flash(request: Request, kind: str, text: str) -> None:
    """One-shot message shown by the next page that renders flashes (Account)."""
    request.session["flash"] = {"kind": kind, "text": text}


@router.get("/login")
async def login(request: Request):
    return _sso_redirect(request, LOGIN)


@router.get("/add-character")
async def add_character_route(request: Request):
    """Old entry point (bookmarks, older links): the picker now comes first."""
    return RedirectResponse("/auth/connect", status_code=303)


@router.get("/connect")
async def connect(request: Request, preset: str = "", db: AsyncSession = Depends(get_db)):
    """The permission picker for a NEW character: signing up, or adding an alt.

    ``?preset=`` picks a starting preset; ``?p=<key>`` (repeatable) preselects
    individual permissions instead, for links that ask for something specific.
    """
    logged_in = bool(request.session.get("user_id"))
    wanted = request.query_params.getlist("p")
    if wanted:
        selected = perms.normalize_keys(wanted)
        active_preset = None
    else:
        active_preset = preset if preset in perms.PRESETS else perms.DEFAULT_PRESET
        selected = list(perms.PRESETS[active_preset])
    return templates.TemplateResponse(request, "permissions_picker.html", {
        "intent": ADD if logged_in else SIGNUP,
        "character": None,
        "selected": set(selected),
        "active_preset": active_preset,
        **picker_context(),
    })


def picker_context() -> dict:
    """Catalog data every rendering of the picker needs."""
    return {
        "permissions": perms.PERMISSIONS,
        "groups": (
            (perms.CHARACTER, "Your character"),
            (perms.CORPORATION, "Your corporation"),
        ),
        "presets": perms.PRESET_LABELS,
        "preset_keys": {k: list(v) for k, v in perms.PRESETS.items()},
    }


@router.post("/authorize")
async def authorize(request: Request, intent: str = Form(...),
                    character_id: int | None = Form(None),
                    purge: str | None = Form(None),
                    db: AsyncSession = Depends(get_db)):
    """Start SSO for the picker's selection. POST, and so CSRF-checked: a link
    that could narrow someone's permissions and purge their data must not be
    something another site can make them follow."""
    form = await request.form()
    keys = perms.normalize_keys(form.getlist("p"))
    user_id = request.session.get("user_id")

    if intent not in (SIGNUP, ADD, UPDATE):
        raise HTTPException(status_code=400, detail="Unknown intent.")
    if intent in (ADD, UPDATE) and not user_id:
        return RedirectResponse("/auth/login", status_code=303)
    if intent == SIGNUP and user_id:
        intent = ADD
    if intent == UPDATE:
        owned = (await db.execute(select(Character).where(
            Character.character_id == character_id,
            Character.user_id == user_id))).scalar_one_or_none()
        if owned is None:
            return RedirectResponse("/account", status_code=303)
    return _sso_redirect(request, intent, keys,
                         character_id=character_id if intent == UPDATE else None,
                         purge=(intent == UPDATE and purge == "1"))


async def _exchange_code(code: str) -> tuple[dict, dict]:
    credentials = base64.b64encode(
        f"{settings.eve_client_id}:{settings.eve_client_secret}".encode()
    ).decode()
    async with httpx.AsyncClient(timeout=10.0) as client:
        token_resp = await client.post(
            settings.eve_sso_token_url,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "authorization_code", "code": code},
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()

        verify_resp = await client.get(
            settings.eve_sso_verify_url,
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )
        verify_resp.raise_for_status()
        return token_data, verify_resp.json()


async def _public_metadata(access_token: str, character_id: int) -> dict:
    esi = ESIClient(access_token)
    pub_info = await esi_char.get_public_info(esi, character_id)
    meta = {
        "corporation_id": pub_info.get("corporation_id"),
        "alliance_id": pub_info.get("alliance_id"),
        "security_status": pub_info.get("security_status"),
        "birthday": None,
        "corporation_name": None,
        "alliance_name": None,
    }
    birthday_str = pub_info.get("birthday")  # ISO 8601 format
    if birthday_str:
        try:
            from dateutil import parser
            meta["birthday"] = parser.isoparse(birthday_str).replace(tzinfo=None)
        except Exception as exc:
            logger.debug("birthday parse failed for %s: %s", birthday_str, exc)
    if meta["corporation_id"]:
        try:
            corp_info = await esi_corp.get_corporation_info(esi, meta["corporation_id"])
            meta["corporation_name"] = corp_info.get("name")
        except Exception as exc:
            logger.debug("corp name lookup failed for %s: %s", meta["corporation_id"], exc)
    if meta["alliance_id"]:
        try:
            alliance_info = await esi_corp.get_alliance_info(esi, meta["alliance_id"])
            meta["alliance_name"] = alliance_info.get("name")
        except Exception as exc:
            logger.debug("alliance name lookup failed for %s: %s", meta["alliance_id"], exc)
    return meta


def _apply_metadata(char: Character, meta: dict) -> None:
    for field in ("corporation_id", "corporation_name", "alliance_id",
                  "alliance_name", "security_status"):
        setattr(char, field, meta[field])
    if meta["birthday"] and not char.birthday:
        char.birthday = meta["birthday"]
    char.last_seen = datetime.now(timezone.utc)


async def _allowed_to_register(db: AsyncSession, character_id: int, meta: dict) -> bool:
    """If allowlist entries exist, only listed characters/corps/alliances may
    create a new account."""
    from app.db.models import RegistrationAllowlist
    allowlist = (await db.execute(select(RegistrationAllowlist))).scalars().all()
    if not allowlist:
        return True
    for entry in allowlist:
        if entry.entry_type == "character" and entry.eve_id == character_id:
            return True
        if entry.entry_type == "corporation" and entry.eve_id == meta["corporation_id"]:
            return True
        if entry.entry_type == "alliance" and entry.eve_id == meta["alliance_id"]:
            return True
    return False


def _start_session(request: Request, user: User, character_id: int) -> None:
    request.session["user_id"] = user.id
    request.session["active_character_id"] = character_id
    request.session["is_admin"] = user.role in ("admin", "manager")
    request.session["role"] = user.role


def _queue_sync(character_id: int) -> None:
    from app.routes.dashboard import _sync_task, _queued_sync
    if character_id not in _queued_sync:
        _queued_sync[character_id] = datetime.now(timezone.utc)
        asyncio.create_task(_sync_task(character_id))


@router.get("/callback")
async def callback(request: Request, code: str, state: str, db: AsyncSession = Depends(get_db)):
    saved_state = request.session.get("oauth_state")
    if not saved_state or saved_state != state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state.")
    request.session.pop("oauth_state", None)
    pending = request.session.pop("oauth_pending", None) or {}
    intent = pending.get("intent") if pending.get("intent") in _INTENTS else LOGIN

    token_data, verify_data = await _exchange_code(code)
    character_id = verify_data["CharacterID"]
    character_name = verify_data["CharacterName"]
    access_token = token_data["access_token"]
    refresh_token = token_data["refresh_token"]
    token_expiry = datetime.now(timezone.utc) + timedelta(seconds=token_data.get("expires_in", 1200))
    # What the token can actually do, from its own `scp` claim — the same source
    # ESIClient's guard reads, so the stored scopes and the guard never
    # disagree. /oauth/verify (on CCP's deprecation list) is only a fallback.
    granted = perms.join_scopes(scope_guard.granted_scopes(access_token)
                                or perms.parse_scopes(verify_data.get("Scopes", "")))

    meta = await _public_metadata(access_token, character_id)
    existing = (await db.execute(
        select(Character).where(Character.character_id == character_id))).scalar_one_or_none()

    if (not existing or not existing.user_id) and not await _allowed_to_register(db, character_id, meta):
        return templates.TemplateResponse(request, "index.html", {
            "error": "Registration is restricted. Your character, corporation, "
                     "or alliance is not on the allowlist."})

    # ── Identity-only login ──────────────────────────────────────────────
    if intent == LOGIN:
        new_account = False
        if existing and existing.user_id:
            user = (await db.execute(select(User).where(User.id == existing.user_id))).scalar_one_or_none()
            if not user:
                # Stale FK — recreate the user and re-claim the character.
                user = User()
                db.add(user)
                await db.flush()
                existing.user_id = user.id
                existing.is_main = True
        elif existing:
            # Orphaned character (created before the user system) — adopt it.
            user = User()
            db.add(user)
            await db.flush()
            existing.user_id = user.id
            existing.is_main = True
        else:
            # A character Vigilant has never seen. The login token carries no
            # permissions; keep it so the row is valid, and send the user
            # straight to the picker to choose what to share.
            user = User()
            db.add(user)
            await db.flush()
            existing = Character(
                character_id=character_id, character_name=character_name,
                user_id=user.id, is_main=True, access_token=access_token,
                refresh_token=refresh_token, token_expiry=token_expiry,
                scopes=granted, declined_scopes="",
            )
            db.add(existing)
            new_account = True
        # Deliberately NOT touching access_token / refresh_token / scopes on an
        # existing character: logging in must never change what it shares.
        # The login token is simply dropped. It is not revoked either — it
        # carries no scopes, and revoking a grant for the same character and
        # application risks taking the stored data token down with it.
        _apply_metadata(existing, meta)
        existing.character_name = character_name
        user.last_login = datetime.now(timezone.utc)
        await db.commit()
        _start_session(request, user, character_id)
        if new_account:
            return RedirectResponse(f"/account/permissions/{character_id}?welcome=1", status_code=303)
        _queue_sync(character_id)
        return RedirectResponse("/dashboard", status_code=303)

    # ── Picker flows: signup / add / update ──────────────────────────────
    current_user_id = request.session.get("user_id")
    requested = perms.scopes_for(pending.get("keys") or [])

    if intent in (ADD, UPDATE) and not current_user_id:
        return RedirectResponse("/auth/login", status_code=303)

    if intent == UPDATE:
        target = pending.get("character_id")
        if character_id != target or not existing or existing.user_id != current_user_id:
            wanted = (await db.execute(select(Character.character_name).where(
                Character.character_id == target))).scalar_one_or_none()
            _flash(request, "danger",
                   f"You authorized {character_name} in EVE, but you were changing "
                   f"{wanted or 'another character'}'s permissions. Nothing was changed — "
                   f"pick the same character on the EVE page.")
            return RedirectResponse("/account", status_code=303)

    if intent == ADD and existing and existing.user_id and existing.user_id != current_user_id:
        # Character is already owned by a different account — reject.
        return RedirectResponse("/dashboard?error=character_claimed", status_code=303)

    if intent == SIGNUP:
        if existing and existing.user_id:
            user = (await db.execute(select(User).where(User.id == existing.user_id))).scalar_one_or_none()
            if not user:
                user = User()
                db.add(user)
                await db.flush()
                existing.user_id = user.id
                existing.is_main = True
        else:
            user = User()
            db.add(user)
            await db.flush()
            if existing:
                existing.user_id = user.id
                existing.is_main = True
    else:
        user = (await db.execute(select(User).where(User.id == current_user_id))).scalar_one_or_none()
        if user is None:
            request.session.clear()
            return RedirectResponse("/auth/login", status_code=303)

    old_refresh = existing.refresh_token if existing else None
    old_is_ours = issued_to_us(existing.access_token) if existing else False
    old_scopes = perms.parse_scopes(existing.scopes) if existing else set()
    if existing is None:
        existing = Character(
            character_id=character_id, character_name=character_name,
            user_id=user.id, is_main=(intent == SIGNUP),
            access_token=access_token, refresh_token=refresh_token,
            token_expiry=token_expiry, scopes=granted, declined_scopes="",
        )
        db.add(existing)
    existing.user_id = user.id
    existing.character_name = character_name
    existing.access_token = access_token
    existing.refresh_token = refresh_token
    existing.token_expiry = token_expiry
    existing.scopes = granted
    existing.declined_scopes = perms.join_scopes(perms.declined_after(requested))
    _apply_metadata(existing, meta)
    if intent == SIGNUP:
        user.last_login = datetime.now(timezone.utc)

    granted_set = perms.parse_scopes(granted)
    added = granted_set - old_scopes
    removed = old_scopes - granted_set
    not_granted = set(requested) - granted_set
    db.add(AdminAuditLog(
        user_id=user.id, character_id=character_id, event_type="permissions_changed",
        detail=(f"{intent}: +{len(added)} -{len(removed)} scopes"
                + (f"; EVE did not grant {len(not_granted)}" if not_granted else "")),
        ip_address=request.client.host if request.client else None,
    ))
    await db.commit()

    # The superseded token: revoke it at EVE, then prove the new one still
    # refreshes (a revocation that also took the new grant down would
    # otherwise only surface as a failed sync later).
    if old_refresh and old_refresh != refresh_token and old_is_ours:
        if await revoke_refresh_token(old_refresh):
            try:
                await _do_refresh(existing, db)
            except TokenRevoked:
                logger.error("new token for %s stopped working after revoking the old one", character_id)
                _flash(request, "danger",
                       "EVE revoked the new authorization together with the old one. "
                       "Please change this character's permissions once more.")

    purged_note = ""
    if removed:
        # A permission is withdrawn when ANY of its scopes went away.
        gone = [p.key for p in perms.PERMISSIONS if set(p.scopes) & removed]
        # Always: nothing withdrawn keeps feeding features as if it were live.
        await clear_live_state(db, character_id, gone)
        if pending.get("purge"):
            counts = await purge_history(db, character_id, gone)
            db.add(AdminAuditLog(
                user_id=user.id, character_id=character_id, event_type="permissions_purged",
                detail=", ".join(f"{k}={v}" for k, v in sorted(counts.items()) if v) or "nothing stored",
            ))
            purged_note = " History collected under the withdrawn permissions was deleted."
        await db.commit()

    if request.session.get("flash") is None:
        if not_granted:
            names = sorted({(perms.permission_for_scope(s).label if perms.permission_for_scope(s) else s)
                            for s in not_granted})
            _flash(request, "warn",
                   "EVE did not grant: " + ", ".join(names) + ". If this keeps happening, the "
                   "scope may not be enabled on this Vigilant instance's EVE application.")
        elif intent == UPDATE:
            _flash(request, "ok", f"Permissions for {character_name} updated.{purged_note}")

    if intent == SIGNUP:
        _start_session(request, user, character_id)
    else:
        request.session["active_character_id"] = character_id
        request.session["is_admin"] = user.role in ("admin", "manager")
        request.session["role"] = user.role

    _queue_sync(character_id)
    return RedirectResponse("/account" if intent == UPDATE else "/dashboard", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    resp = RedirectResponse("/", status_code=303)
    # Clear client-side storage on logout so per-account notification data
    # (character names, corp inventory, structure-attack alerts) cached in
    # localStorage does not leak to the next user of a shared browser. The
    # client also clears its own keys as a fallback for browsers with weak
    # Clear-Site-Data support (see static/js/notifications.js). F4.
    resp.headers["Clear-Site-Data"] = '"storage"'
    return resp


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

    old_refresh = char.refresh_token
    old_is_ours = issued_to_us(char.access_token)
    await db.delete(char)
    await db.commit()
    # Removing a character is withdrawing every permission it granted.
    if old_is_ours:
        await revoke_refresh_token(old_refresh)

    if request.session.get("active_character_id") == character_id:
        # Fall back to the main character.
        main_result = await db.execute(
            select(Character).where(Character.user_id == user_id, Character.is_main == True)
        )
        main_char = main_result.scalar_one_or_none()
        request.session["active_character_id"] = main_char.character_id if main_char else None

    return RedirectResponse("/dashboard", status_code=303)
