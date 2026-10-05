"""Tools -> Skill Farm (T-073).

  * `GET  /tools/skill-farm`                        — page: settings, pilot
                                                        table, add-pilot form.
  * `POST /tools/skill-farm/settings`                — htmx: save settings.
  * `POST /tools/skill-farm/pilots`                  — htmx: add a farm pilot.
  * `POST /tools/skill-farm/pilots/{id}/base-sp`     — htmx: edit a pilot's
                                                        base SP floor.
  * `DELETE /tools/skill-farm/pilots/{id}`           — htmx: remove a pilot.

Every mutating endpoint re-renders and returns the same
`partials/skill_farm_content.html` fragment, which the page swaps as one
unit (`#skill-farm-content`) — settings, table and add-form all come back
server-fresh together, so there is no form state to reset after a submit.
The fragment itself carries no `<script>` (htmx fragments render without
the page's CSP nonce, so its interactivity has to be plain `hx-*`
attributes rather than inline JS — see the partial's own header comment).
The page has one small nonced script of its own: on phones a swap would
close the pilot rows that were open, so it re-opens them by pilot id after
each swap.

CSRF: every mutation here is htmx; base.html's `htmx:configRequest` wiring
attaches `X-CSRF-Token` to it automatically, so an unauthenticated caller is
stopped by the CSRF middleware before this module's own `user_id` gate is
even reached.

IDOR: every write goes through app.skillfarm.pilots, which scopes every
lookup/update/delete to the caller's own user_id (and, for add, checks the
character itself belongs to the caller) — this module never trusts a posted
id past that.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import character_skills
from app.db.models import Character, CharacterDashboardCache, get_db
from app.esi.client import ESIClient
from app.sde import lookup as sde
from app.skillfarm import pilots as farm_pilots
from app.skillfarm import prices as farm_prices
from app.skillfarm import rows as farm_rows
from app.skillfarm.constants import (
    LARGE_SKILL_INJECTOR_TYPE_ID,
    PLEX_TYPE_ID,
    SKILL_EXTRACTOR_TYPE_ID,
    SKILL_FLOOR_SP,
)

router = APIRouter(tags=["skill_farm"])
# MUST be named `templates` — main.py's sys.modules loop pushes the nav
# globals onto every Jinja2Templates instance named `templates` under
# app.routes.*.
templates = Jinja2Templates(directory="app/templates")

_JITA = "jita"


async def _build_context(db: AsyncSession, user_id: int, error: str | None = None) -> dict:
    settings_row = await farm_pilots.get_settings(db, user_id)
    pilots = await farm_pilots.list_pilots(db, user_id)
    eligible = await farm_pilots.eligible_characters(db, user_id)

    cids = [p.character_id for p in pilots]
    chars_by_id: dict[int, Character] = {}
    caches_by_id: dict[int, CharacterDashboardCache] = {}
    if cids:
        char_rows = (await db.execute(
            select(Character).where(Character.character_id.in_(cids))
        )).scalars().all()
        chars_by_id = {c.character_id: c for c in char_rows}
        cache_rows = (await db.execute(
            select(CharacterDashboardCache).where(CharacterDashboardCache.character_id.in_(cids))
        )).scalars().all()
        caches_by_id = {c.character_id: c for c in cache_rows}

    # Public market data — no character token needed, so one shared client.
    client = ESIClient("")
    source = settings_row.price_source
    extractor_price = await farm_prices.get_hub_price(client, _JITA, SKILL_EXTRACTOR_TYPE_ID, source)
    lsi_price = await farm_prices.get_hub_price(client, _JITA, LARGE_SKILL_INJECTOR_TYPE_ID, source)
    plex_price = await farm_prices.get_plex_price(client, source)

    type_names = await sde.type_ids_to_names(
        db, [SKILL_EXTRACTOR_TYPE_ID, LARGE_SKILL_INJECTOR_TYPE_ID, PLEX_TYPE_ID]
    )

    rows = []
    for pilot in pilots:
        char = chars_by_id.get(pilot.character_id)
        if char is None:
            # Character left the account since this row was added; the
            # per-character purge hook (app/auth/purge.py) clears the pilot
            # row on every removal path, so this is only a defensive skip
            # for a stale read mid-transaction, never the steady state.
            continue
        cache = caches_by_id.get(pilot.character_id)
        summary = character_skills.skill_summary(char, cache)
        skillqueue = None
        if cache is not None and cache.skillqueue_json:
            try:
                skillqueue = json.loads(cache.skillqueue_json)
            except (TypeError, ValueError):
                skillqueue = None
        rows.append(farm_rows.build_pilot_row(
            pilot_id=pilot.id,
            character_id=pilot.character_id,
            character_name=char.character_name,
            base_sp=pilot.base_sp,
            summary=summary,
            skillqueue=skillqueue,
            lsi_price=lsi_price,
            extractor_price=extractor_price,
            plex_price=plex_price,
            sales_tax_pct=settings_row.sales_tax_pct,
            plex_per_month=settings_row.plex_per_month,
        ))

    totals = farm_rows.totals_row(rows)

    return {
        "error": error,
        "settings": settings_row,
        "eligible": eligible,
        "rows": rows,
        "totals": totals,
        "totals_ready_isk_str": farm_rows.isk_str(totals["ready_isk"]),
        "totals_profit_str": farm_rows.isk_str(totals["profit"]),
        "extractor_price_str": farm_rows.isk_str(extractor_price),
        "lsi_price_str": farm_rows.isk_str(lsi_price),
        "plex_price_str": farm_rows.isk_str(plex_price),
        "extractor_name": type_names.get(SKILL_EXTRACTOR_TYPE_ID) or f"Type {SKILL_EXTRACTOR_TYPE_ID}",
        "lsi_name": type_names.get(LARGE_SKILL_INJECTOR_TYPE_ID) or f"Type {LARGE_SKILL_INJECTOR_TYPE_ID}",
        "plex_name": type_names.get(PLEX_TYPE_ID) or f"Type {PLEX_TYPE_ID}",
        "default_base_sp": SKILL_FLOOR_SP,
    }


@router.get("/tools/skill-farm", response_class=HTMLResponse)
async def skill_farm_page(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")
    ctx = await _build_context(db, user_id)
    return templates.TemplateResponse(request, "skill_farm.html", ctx)


@router.post("/tools/skill-farm/settings", response_class=HTMLResponse)
async def skill_farm_save_settings(
    request: Request,
    sales_tax_pct: float = Form(...),
    plex_per_month: int = Form(...),
    price_source: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    error = None
    try:
        await farm_pilots.save_settings(db, user_id, sales_tax_pct, plex_per_month, price_source)
    except farm_pilots.ValidationError as e:
        error = str(e)
    ctx = await _build_context(db, user_id, error=error)
    return templates.TemplateResponse(request, "partials/skill_farm_content.html", ctx)


@router.post("/tools/skill-farm/pilots", response_class=HTMLResponse)
async def skill_farm_add_pilot(
    request: Request,
    character_id: int = Form(...),
    base_sp: int | None = Form(None),
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    error = None
    try:
        await farm_pilots.add_pilot(db, user_id, character_id, base_sp)
    except farm_pilots.NotOwned:
        # IDOR: the posted character_id isn't the caller's own (or doesn't
        # exist). Per the v1.7.0 ownership convention this fails closed with
        # 404, not a 200 + message — htmx never swaps a non-2xx response, so
        # this only ever fires against a crafted request, never the UI.
        return HTMLResponse("", status_code=404)
    except farm_pilots.ValidationError as e:
        error = str(e)
    ctx = await _build_context(db, user_id, error=error)
    return templates.TemplateResponse(request, "partials/skill_farm_content.html", ctx)


@router.post("/tools/skill-farm/pilots/{pilot_id}/base-sp", response_class=HTMLResponse)
async def skill_farm_update_base_sp(
    request: Request,
    pilot_id: int,
    base_sp: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    error = None
    try:
        await farm_pilots.update_base_sp(db, user_id, pilot_id, base_sp)
    except farm_pilots.NotOwned:
        return HTMLResponse("", status_code=404)
    except farm_pilots.ValidationError as e:
        error = str(e)
    ctx = await _build_context(db, user_id, error=error)
    return templates.TemplateResponse(request, "partials/skill_farm_content.html", ctx)


@router.delete("/tools/skill-farm/pilots/{pilot_id}", response_class=HTMLResponse)
async def skill_farm_remove_pilot(
    request: Request, pilot_id: int, db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    removed = await farm_pilots.remove_pilot(db, user_id, pilot_id)
    if not removed:
        # IDOR / stale id: pilot_id doesn't belong to this caller (or is
        # already gone). Same 404-not-200 treatment as the other two IDOR
        # paths above.
        return HTMLResponse("", status_code=404)
    ctx = await _build_context(db, user_id)
    return templates.TemplateResponse(request, "partials/skill_farm_content.html", ctx)
