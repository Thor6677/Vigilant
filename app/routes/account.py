"""Account › Characters & permissions.

Where a user sees, per character, exactly which permissions its token carries
— and changes them. Changing goes back through EVE SSO (app/auth/routes.py,
intent "update"): widening needs EVE's consent, and narrowing is done with a
fresh, smaller token plus revocation of the old one, so the promise holds at
EVE and not only inside Vigilant.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import scopes as perms
from app.auth import status as perm_status
from app.auth.routes import UPDATE, picker_context
from app.db.models import Character, CharacterDashboardCache, UserNotifySettings, get_db
from app.notify import user_discord

logger = logging.getLogger(__name__)

router = APIRouter(tags=["account"])
templates = Jinja2Templates(directory="app/templates")

DISCORD_ANCHOR = "/account#discord-alerts"


def _flash(request: Request, kind: str, text: str) -> None:
    request.session["flash"] = {"kind": kind, "text": text}


def _refuse_anonymous(request: Request):
    """401 for htmx/JSON callers, a redirect home for a browser, like the
    page routes around it."""
    if request.headers.get("hx-request") or "application/json" in request.headers.get("accept", ""):
        return HTMLResponse("", status_code=401)
    return RedirectResponse("/", status_code=303)


def _notify_view(row: UserNotifySettings | None) -> dict:
    """The Discord-alerts section's context. The URL itself never leaves the
    server: only its masked form does."""
    url = row.discord_webhook_url if row else None
    chosen = (user_discord.parse_alert_types(row.alert_types) if row
              else list(user_discord.DEFAULT_ALERT_TYPES))
    last_at = row.last_at if row else None
    return {
        "set": bool(url),
        "masked": user_discord.mask_webhook_url(url),
        "enabled": bool(row.enabled) if row else True,
        "last_at": last_at.strftime("%Y-%m-%d %H:%M UTC") if last_at else None,
        "last_ok": row.last_ok if row else None,
        "last_error": row.last_error if row else None,
        "chosen": set(chosen),
        "groups": user_discord.ALERT_TYPE_GROUPS,
        "dropped": user_discord.dropped_count(row.user_id) if row else 0,
    }


async def _notify_row(db: AsyncSession, user_id: int) -> UserNotifySettings | None:
    return (await db.execute(select(UserNotifySettings).where(
        UserNotifySettings.user_id == user_id))).scalar_one_or_none()


def _char_view(char: Character, warnings: dict | None) -> dict:
    """Plain dict for the template (no lazy loads on a detached row)."""
    return {
        "character_id": char.character_id,
        "character_name": char.character_name,
        "corporation_name": char.corporation_name,
        "alliance_name": char.alliance_name,
        "is_main": bool(char.is_main),
        "scopes": char.scopes or "",
        "declined_scopes": char.declined_scopes or "",
        "token_failed": perm_status.token_failed(warnings),
        **perm_status.summary(char),
    }


@router.get("/account", response_class=HTMLResponse)
async def account_page(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=303)
    chars = (await db.execute(
        select(Character).where(Character.user_id == user_id)
        .order_by(Character.is_main.desc(), Character.character_name))).scalars().all()
    caches = {c.character_id: c for c in (await db.execute(
        select(CharacterDashboardCache).where(
            CharacterDashboardCache.character_id.in_([c.character_id for c in chars])))).scalars().all()}
    views = []
    for char in chars:
        cache = caches.get(char.character_id)
        try:
            warnings = json.loads(cache.sync_warnings_json) if cache and cache.sync_warnings_json else {}
        except (ValueError, TypeError):
            warnings = {}
        views.append(_char_view(char, warnings))
    return templates.TemplateResponse(request, "account.html", {
        "characters": views,
        "flash": request.session.pop("flash", None),
        "total_permissions": len(perms.PERMISSIONS),
        "notify": _notify_view(await _notify_row(db, user_id)),
    })


# ── Discord alerts (T-075) ───────────────────────────────────────────────────
# Post/redirect/get with a flash, like the rest of the Account page. The URL
# field is write-only: it comes back empty on every render, a blank field
# keeps what is saved, and only Remove clears it.


@router.post("/account/notifications/discord")
async def discord_alerts_save(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return _refuse_anonymous(request)
    form = await request.form()
    raw_url = str(form.get("discord_webhook_url") or "").strip()
    chosen = [str(t) for t in form.getlist("alert_types")]
    enabled = form.get("enabled") is not None

    new_url = None
    if raw_url:
        ok, problem = user_discord.validate_discord_webhook_url(raw_url)
        if ok:
            problem, _ = await user_discord.vetted_addresses(raw_url)
        if problem:
            _flash(request, "danger", f"Webhook not saved: {problem}.")
            return RedirectResponse(DISCORD_ANCHOR, status_code=303)
        new_url = raw_url

    row = await _notify_row(db, user_id)
    if row is None:
        if not new_url:
            _flash(request, "danger", "Paste a Discord webhook URL first.")
            return RedirectResponse(DISCORD_ANCHOR, status_code=303)
        row = UserNotifySettings(user_id=user_id)
        db.add(row)
    if new_url:
        row.discord_webhook_url = new_url
        row.last_at = row.last_ok = row.last_error = None   # described the old target
        row.enabled = True                                  # a new URL un-pauses
    else:
        row.enabled = enabled
    row.alert_types = user_discord.serialize_alert_types(chosen)
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()
    user_discord.invalidate(user_id)
    if new_url:
        logger.info("user discord alert: user=%s saved webhook=%s",
                    user_id, user_discord.webhook_log_ref(new_url))
    _flash(request, "ok", "Discord alert settings saved."
           + ("" if row.alert_types else " No alert types are ticked, so nothing will be sent."))
    return RedirectResponse(DISCORD_ANCHOR, status_code=303)


@router.post("/account/notifications/discord/test")
async def discord_alerts_test(request: Request, db: AsyncSession = Depends(get_db)):
    """One test message, sent now with the send timeout, result shown."""
    user_id = request.session.get("user_id")
    if not user_id:
        return _refuse_anonymous(request)
    row = await _notify_row(db, user_id)
    if row is None or not row.discord_webhook_url:
        _flash(request, "danger", "No webhook saved yet.")
        return RedirectResponse(DISCORD_ANCHOR, status_code=303)
    outcome = await user_discord.send_test_message(db, user_id, row.discord_webhook_url)
    if outcome.ok:
        _flash(request, "ok", "Test message sent — check your Discord channel.")
    else:
        _flash(request, "danger", f"Test message not delivered: {outcome.error}.")
    return RedirectResponse(DISCORD_ANCHOR, status_code=303)


@router.post("/account/notifications/discord/remove")
async def discord_alerts_remove(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return _refuse_anonymous(request)
    row = await _notify_row(db, user_id)
    if row is not None:
        row.discord_webhook_url = None
        row.last_at = row.last_ok = row.last_error = None
        row.enabled = True
        row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await db.commit()
    user_discord.invalidate(user_id)
    logger.info("user discord alert: user=%s removed webhook", user_id)
    _flash(request, "ok", "Discord webhook removed.")
    return RedirectResponse(DISCORD_ANCHOR, status_code=303)


@router.get("/account/permissions/{character_id}", response_class=HTMLResponse)
async def change_permissions(character_id: int, request: Request, welcome: int = 0,
                             db: AsyncSession = Depends(get_db)):
    """The picker, preset to what this character shares now.

    ``?add=<key>`` (repeatable) ticks extra permissions on top — the "share
    it" links in missing-permission notices land here.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=303)
    char = (await db.execute(select(Character).where(
        Character.character_id == character_id,
        Character.user_id == user_id))).scalar_one_or_none()
    if char is None:
        return RedirectResponse("/account", status_code=303)
    current = perms.keys_for_scopes(char.scopes)
    adding = perms.normalize_keys(request.query_params.getlist("add"))
    if welcome and not current:
        selected = set(perms.PRESETS[perms.DEFAULT_PRESET])
        active_preset = perms.DEFAULT_PRESET
    else:
        selected = set(perms.normalize_keys(current + adding))
        active_preset = None
    return templates.TemplateResponse(request, "permissions_picker.html", {
        "intent": UPDATE,
        "welcome": bool(welcome),
        "character": _char_view(char, None),
        "current": set(current),
        "adding": set(adding),
        "selected": selected,
        "active_preset": active_preset,
        **picker_context(),
    })
