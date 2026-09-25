"""Live wormhole system tracker (T-034).

A second-monitor page for active wormhole diving: pick ONE character to
track, the page polls its ESI location every ~15s, and whenever that
character is in J-space it renders the full wormhole system reference
(statics, class, effect — via the shared context builder in
app/routes/wormholes.py) plus an intelligence panel sourced from the
local killmail archive:

  * Who lives here — top attacker corps in the system, last 90 days
  * Capital activity — capital kills in the last year (count + latest)
  * Last structure kill — most recent structure loss in the system

When the tracked character is in K-space, the panel renders a compact
"<name> is in <system>" line instead — there is no wormhole-specific
content to show, and no error: k-space is a normal, expected location.

Location polling uses esi-location.read_location.v1. Previous-system
memory is process-local (survives page reloads, resets on deploy).

This used to be a multi-select tracker: several characters could be
checked at once, and the first one found in J-space (in checkbox order)
became the headline while the rest trailed as "· Name: system". That
design is why a selected character's system could silently differ from
what the panel showed — the headline was "first J-space character among
those ticked", not "the character I picked". The product decision is
single selection: the headline is always the one selected character,
in J-space or not.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Character, Killmail, KillmailAttacker, get_db
from app.db.sde_models import SDESystem, SDEType
from app.esi.client import ESIClient, TokenRevoked, refresh_token
from app.intel.recent_battles import resolve_entity_names
from app.routes.wormholes import build_wh_system_context
from app.sde import lookup as sde

log = logging.getLogger(__name__)
router = APIRouter(tags=["intel"])
templates = Jinja2Templates(directory="app/templates")

_LOCATION_SCOPE = "esi-location.read_location.v1"
WH_SYSTEM_MIN, WH_SYSTEM_MAX = 31_000_000, 31_999_999
CAPITAL_GROUP_IDS = {547, 485, 30, 659, 513, 902, 1538}
RORQUAL_TYPE_ID = 28352
STRUCTURE_CATEGORY_ID = 65

# char_id -> {system_id, system_name, is_j, ts, prev_system_id, prev_system_name}
_last_seen: dict[int, dict] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _fetch_location(char, db: AsyncSession) -> tuple[dict | None, str | None]:
    """Current location for one character: {system_id, system_name, is_j}.

    Returns (location, None) on success or (None, reason) on failure. The
    reason is surfaced in the panel's error state — a revoked token reads
    differently to the user than ESI being briefly unreachable, and only
    one of those is worth re-authenticating over.
    """
    try:
        token = await refresh_token(char, db)
    except TokenRevoked as e:
        log.info("wh_tracker: token revoked for %s: %s", char.character_id, e)
        return None, "token revoked — re-link this character"
    except Exception as e:
        log.info("wh_tracker: token refresh failed for %s: %s", char.character_id, e)
        return None, f"token refresh failed ({type(e).__name__})"

    try:
        client = ESIClient(token, db=db)
        loc = await client.get(f"/characters/{char.character_id}/location/")
        system_id = int(loc["solar_system_id"])
    except Exception as e:
        log.info("wh_tracker: location fetch failed for %s: %s", char.character_id, e)
        return None, f"ESI location request failed ({type(e).__name__})"

    name_row = (await db.execute(
        select(SDESystem.system_name).where(SDESystem.system_id == system_id)
    )).scalar_one_or_none()
    return {
        "system_id": system_id,
        "system_name": name_row or f"System {system_id}",
        "is_j": WH_SYSTEM_MIN <= system_id <= WH_SYSTEM_MAX,
    }, None


def _update_last_seen(char_id: int, loc: dict) -> dict:
    """Track system transitions so the panel can show 'came from X'."""
    prev = _last_seen.get(char_id)
    entry = dict(loc)
    entry["ts"] = _now()
    if prev and prev["system_id"] != loc["system_id"]:
        entry["prev_system_id"] = prev["system_id"]
        entry["prev_system_name"] = prev["system_name"]
    elif prev:
        entry["prev_system_id"] = prev.get("prev_system_id")
        entry["prev_system_name"] = prev.get("prev_system_name")
    else:
        entry["prev_system_id"] = None
        entry["prev_system_name"] = None
    _last_seen[char_id] = entry
    return entry


async def _system_intel(db: AsyncSession, system_id: int) -> dict:
    """Killmail-archive intelligence for one system. All queries are
    bounded by (solar_system_id, killmail_time) so they ride
    ix_killmail_system_time — never a table scan on the 137M-row archive."""
    now = _now()
    c90 = now - timedelta(days=90)
    c365 = now - timedelta(days=365)
    c730 = now - timedelta(days=730)

    # Who lives here — top attacker corps by kill count, 90d.
    resident_rows = (await db.execute(
        select(
            KillmailAttacker.corporation_id,
            KillmailAttacker.alliance_id,
            func.count(func.distinct(Killmail.killmail_id)).label("kills"),
        )
        .join(Killmail, Killmail.killmail_id == KillmailAttacker.killmail_id)
        .where(Killmail.solar_system_id == system_id)
        .where(Killmail.killmail_time >= c90)
        .where(KillmailAttacker.corporation_id.isnot(None))
        .group_by(KillmailAttacker.corporation_id)
        .order_by(func.count(func.distinct(Killmail.killmail_id)).desc())
        .limit(2)
    )).all()

    # Capital activity — capital victims, 365d.
    cap_row = (await db.execute(
        select(func.count(Killmail.killmail_id), func.max(Killmail.killmail_time))
        .select_from(Killmail)
        .join(SDEType, SDEType.type_id == Killmail.victim_ship_type_id)
        .where(Killmail.solar_system_id == system_id)
        .where(Killmail.killmail_time >= c365)
        .where((SDEType.group_id.in_(CAPITAL_GROUP_IDS))
               | (Killmail.victim_ship_type_id == RORQUAL_TYPE_ID))
    )).one()

    # Last structure kill — most recent structure loss, 2y window.
    struct_row = (await db.execute(
        select(Killmail.killmail_time, SDEType.type_name,
               Killmail.victim_corporation_id, Killmail.victim_alliance_id)
        .join(SDEType, SDEType.type_id == Killmail.victim_ship_type_id)
        .where(Killmail.solar_system_id == system_id)
        .where(Killmail.killmail_time >= c730)
        .where(SDEType.category_id == STRUCTURE_CATEGORY_ID)
        .order_by(Killmail.killmail_time.desc())
        .limit(1)
    )).first()

    # Total kills 90d for context.
    total_90d = (await db.execute(
        select(func.count(Killmail.killmail_id))
        .where(Killmail.solar_system_id == system_id)
        .where(Killmail.killmail_time >= c90)
    )).scalar() or 0

    name_ids = [r[0] for r in resident_rows] + [r[1] for r in resident_rows if r[1]]
    if struct_row:
        name_ids += [struct_row[2], struct_row[3]]
    names = await resolve_entity_names([i for i in name_ids if i])

    residents = [{
        "corp_id": r[0],
        "corp_name": names.get(r[0], f"Corp {r[0]}"),
        "alliance_id": r[1],
        "alliance_name": names.get(r[1]) if r[1] else None,
        "kills": r[2],
    } for r in resident_rows]

    return {
        "residents": residents,
        "total_kills_90d": total_90d,
        "capital_kills_1y": int(cap_row[0] or 0),
        "capital_latest": cap_row[1],
        "last_structure_kill": None if not struct_row else {
            "time": struct_row[0],
            "type_name": struct_row[1],
            "owner": names.get(struct_row[3]) or names.get(struct_row[2])
                     or (f"Corp {struct_row[2]}" if struct_row[2] else "Unknown"),
        },
    }


@router.get("/intel/tracker", response_class=HTMLResponse)
async def wh_tracker_page(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")

    r = await db.execute(
        select(Character)
        .where(Character.user_id == user_id)
        .order_by(Character.character_name)
    )
    chars = [
        {"id": c.character_id, "name": c.character_name,
         "has_scope": _LOCATION_SCOPE in (c.scopes or "")}
        for c in r.scalars().all()
    ]
    return templates.TemplateResponse(request, "wh_tracker.html", {"chars": chars})


@router.get("/intel/tracker/poll", response_class=HTMLResponse)
async def wh_tracker_poll(
    request: Request,
    char: str = "",
    shown: str = "",
    db: AsyncSession = Depends(get_db),
):
    """One tracker tick for the single selected character: locate it, then
    render the full wormhole panel (J-space) or a compact location line
    (K-space). The headline is always this character — there is no other
    candidate to pick between any more.

    `shown` is the key of the panel already on screen. When it still matches,
    only the "checked" stamp is refreshed (ISS-052): re-rendering the whole
    panel every 15s reset the page's scroll, because the kill list reloads
    through a one-line placeholder, and wiped the structure-age box."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    char_id: int | None = None
    if char.strip():
        try:
            char_id = int(char)
        except ValueError:
            char_id = None
    if char_id is None:
        return templates.TemplateResponse(
            request, "partials/wh_tracker_panel.html",
            {"state": "none", "checked_at": _now()})

    r = await db.execute(
        select(Character)
        .where(Character.user_id == user_id)
        .where(Character.character_id == char_id))
    selected = r.scalar_one_or_none()
    if not selected or _LOCATION_SCOPE not in (selected.scopes or ""):
        return templates.TemplateResponse(
            request, "partials/wh_tracker_panel.html",
            {"state": "error", "checked_at": _now(),
             "reason": "character not found or missing the location scope"})

    loc, reason = await _fetch_location(selected, db)
    if loc is None:
        return templates.TemplateResponse(
            request, "partials/wh_tracker_panel.html",
            {"state": "error", "checked_at": _now(), "reason": reason})

    entry = _update_last_seen(char_id, loc)
    shown_key = f"{'j' if entry['is_j'] else 'k'}:{char_id}:{entry['system_id']}"
    if shown == shown_key:
        resp = templates.TemplateResponse(
            request, "partials/wh_tracker_checked.html",
            {"checked_at": _now(), "oob": True})
        resp.headers["HX-Reswap"] = "none"   # out-of-band stamp only
        return resp

    if not entry["is_j"]:
        # K-space: no wormhole panel to show, just where the character is.
        # system_info() does one extra region-lookup join beyond what
        # _update_last_seen already resolved — cheap, and the region is
        # worth having on the compact line.
        sys_info = await sde.system_info(db, entry["system_id"])
        return templates.TemplateResponse(
            request, "partials/wh_tracker_panel.html",
            {"state": "kspace",
             "checked_at": _now(),
             "shown_key": shown_key,
             "char_name": selected.character_name,
             "system_name": entry["system_name"],
             "region_name": (sys_info or {}).get("region"),
             "prev_system_name": entry.get("prev_system_name")})

    ctx = await build_wh_system_context(db, entry["system_name"])
    intel = await _system_intel(db, entry["system_id"])
    return templates.TemplateResponse(
        request, "partials/wh_tracker_panel.html",
        {"state": "active",
         "checked_at": _now(),
         "shown_key": shown_key,
         "char_name": selected.character_name,
         "prev_system_name": entry.get("prev_system_name"),
         "intel": intel,
         **(ctx or {"system": None})})
