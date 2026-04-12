"""Structure timer board — shared timer tracking for reinforced structures."""

import re
import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from app.db.models import get_db, User, StructureTimer, TimerACLGroup, TimerACLEntry, Character
from app.sde import lookup as sde
from sqlalchemy.orm import selectinload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/structure-timers", tags=["structure-timers"])
templates = Jinja2Templates(directory="app/templates")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_countdown(text: str) -> timedelta | None:
    """Parse countdown text like '1d 4h 30m 15s' into a timedelta."""
    if not text or not text.strip():
        return None
    parts = re.findall(r'(\d+)\s*([dhms])', text.strip(), re.IGNORECASE)
    if not parts:
        return None
    total = timedelta()
    for value, unit in parts:
        v = int(value)
        if unit.lower() == 'd':
            total += timedelta(days=v)
        elif unit.lower() == 'h':
            total += timedelta(hours=v)
        elif unit.lower() == 'm':
            total += timedelta(minutes=v)
        elif unit.lower() == 's':
            total += timedelta(seconds=v)
    return total if total.total_seconds() > 0 else None


async def _cleanup_timers(db: AsyncSession):
    """Archive expired timers (1h past expiry), delete old archived (30d)."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    archive_cutoff = now - timedelta(hours=1)
    delete_cutoff = now - timedelta(days=30)

    # Archive expired active timers
    result = await db.execute(
        select(StructureTimer).where(
            StructureTimer.is_archived == False,
            StructureTimer.timer_expires < archive_cutoff,
        )
    )
    for timer in result.scalars().all():
        timer.is_archived = True
        timer.archived_at = now

    # Delete old archived timers
    old_archived = await db.execute(
        select(StructureTimer).where(
            StructureTimer.is_archived == True,
            StructureTimer.archived_at < delete_cutoff,
        )
    )
    for timer in old_archived.scalars().all():
        await db.delete(timer)

    await db.commit()


def _classify_structure_type(type_name: str) -> str:
    """Classify a structure type from its SDE type name."""
    name = (type_name or "").lower()
    # Specific structure matches
    if "keepstar" in name:
        return "keepstar"
    if "fortizar" in name:
        return "fortizar"
    if "astrahus" in name:
        return "astrahus"
    if "sotiyo" in name:
        return "sotiyo"
    if "azbel" in name:
        return "azbel"
    if "raitaru" in name:
        return "raitaru"
    if "tatara" in name:
        return "tatara"
    if "athanor" in name:
        return "athanor"
    if "customs" in name or "poco" in name:
        return "poco"
    if "skyhook" in name:
        return "skyhook"
    if any(s in name for s in ("sovereignty", "ihub", "tcu", "territorial")):
        return "sov"
    return "other"


# Mapping from structure_type value to display label
STRUCTURE_TYPE_LABELS = {
    "astrahus": "Astrahus", "fortizar": "Fortizar", "keepstar": "Keepstar",
    "raitaru": "Raitaru", "azbel": "Azbel", "sotiyo": "Sotiyo",
    "athanor": "Athanor", "tatara": "Tatara",
    "poco": "POCO", "skyhook": "Skyhook", "sov": "Sov", "other": "Other",
    # Legacy values for existing timers
    "citadel": "Citadel", "ec": "EC", "refinery": "Refinery",
}


async def _get_user_identities(db: AsyncSession, user_id: int) -> dict:
    """Get all character/corp/alliance IDs for a user for ACL matching."""
    result = await db.execute(
        select(Character).where(Character.user_id == user_id)
    )
    chars = result.scalars().all()
    char_ids = set()
    corp_ids = set()
    alliance_ids = set()
    for c in chars:
        char_ids.add(c.character_id)
        if c.corporation_id:
            corp_ids.add(c.corporation_id)
        if c.alliance_id:
            alliance_ids.add(c.alliance_id)
    return {"character": char_ids, "corporation": corp_ids, "alliance": alliance_ids}


async def _visible_group_ids(db: AsyncSession, user_id: int) -> set[int] | None:
    """Return set of ACL group IDs this user can see, or None if no filtering needed.
    A timer with acl_group_id=None is always visible."""
    identities = await _get_user_identities(db, user_id)

    # Get all ACL entries
    all_entries = await db.execute(select(TimerACLEntry))
    entries = all_entries.scalars().all()

    visible = set()
    for e in entries:
        if e.entry_type == "character" and e.eve_id in identities["character"]:
            visible.add(e.group_id)
        elif e.entry_type == "corporation" and e.eve_id in identities["corporation"]:
            visible.add(e.group_id)
        elif e.entry_type == "alliance" and e.eve_id in identities["alliance"]:
            visible.add(e.group_id)

    return visible


async def _can_modify_timer(db: AsyncSession, user_id: int, timer) -> bool:
    """Check if user can edit/delete a timer.
    Allowed: timer creator, app admins/managers, or in-game Directors."""
    if timer.created_by == user_id:
        return True
    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        return False
    if user.role in ("admin", "manager"):
        return True
    # Check in-game Director role via ESI
    chars = await db.execute(select(Character).where(Character.user_id == user_id))
    for char in chars.scalars().all():
        if "esi-characters.read_corporation_roles.v1" not in (char.scopes or ""):
            continue
        try:
            from app.esi.client import ESIClient, refresh_token
            from app.esi import character as esi_char
            token = await refresh_token(char, db)
            client = ESIClient(token, db=db)
            roles_data = await client.get(f"/characters/{char.character_id}/roles/")
            roles = roles_data.get("roles", [])
            if "Director" in roles or "CEO" in roles:
                return True
        except Exception:
            continue
    return False


def _timer_visible(timer, visible_groups: set[int]) -> bool:
    """Check if a timer is visible given the user's accessible groups."""
    if timer.acl_group_id is None:
        return True  # No ACL = visible to everyone
    return timer.acl_group_id in visible_groups


# ── Main page ────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def timers_page(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=302)

    await _cleanup_timers(db)

    # Get user's visible ACL groups
    visible_groups = await _visible_group_ids(db, user_id)

    active_result = await db.execute(
        select(StructureTimer)
        .where(StructureTimer.is_archived == False)
        .order_by(StructureTimer.timer_expires.asc())
    )
    active_timers = [t for t in active_result.scalars().all() if _timer_visible(t, visible_groups)]

    archived_result = await db.execute(
        select(StructureTimer)
        .where(StructureTimer.is_archived == True)
        .order_by(StructureTimer.archived_at.desc())
    )
    archived_timers = [t for t in archived_result.scalars().all() if _timer_visible(t, visible_groups)]

    # Get ACL groups for the create form dropdown
    acl_result = await db.execute(
        select(TimerACLGroup).options(selectinload(TimerACLGroup.entries)).order_by(TimerACLGroup.name)
    )
    acl_groups = acl_result.scalars().all()

    # Check if user is admin/manager or in-game director (for edit/delete permissions)
    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    is_privileged = user and user.role in ("admin", "manager")

    if not is_privileged:
        # Check in-game Director/CEO role
        chars = await db.execute(select(Character).where(Character.user_id == user_id))
        for char in chars.scalars().all():
            if "esi-characters.read_corporation_roles.v1" not in (char.scopes or ""):
                continue
            try:
                from app.esi.client import ESIClient, refresh_token
                token = await refresh_token(char, db)
                client = ESIClient(token, db=db)
                roles_data = await client.get(f"/characters/{char.character_id}/roles/")
                if any(r in roles_data.get("roles", []) for r in ("Director", "CEO")):
                    is_privileged = True
                    break
            except Exception:
                continue

    return templates.TemplateResponse("structure_timers.html", {
        "request": request,
        "active_timers": active_timers,
        "archived_timers": archived_timers,
        "acl_groups": acl_groups,
        "user_id": user_id,
        "is_privileged": is_privileged,
    })


# ── Create timer ─────────────────────────────────────────────────────────────

@router.post("/create", response_class=HTMLResponse)
async def create_timer(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=302)

    form = await request.form()

    # Parse timer expiry
    mode = form.get("timer_mode", "countdown")
    timer_expires = None
    if mode == "countdown":
        td = _parse_countdown(form.get("countdown_text", ""))
        if td:
            timer_expires = datetime.now(timezone.utc).replace(tzinfo=None) + td
    elif mode == "absolute":
        try:
            dt_str = form.get("datetime_utc", "")
            if dt_str:
                timer_expires = datetime.fromisoformat(dt_str).replace(tzinfo=None)
        except ValueError:
            pass

    if not timer_expires:
        return RedirectResponse("/structure-timers", status_code=302)

    structure_name = form.get("structure_name", "").strip()
    if not structure_name:
        return RedirectResponse("/structure-timers", status_code=302)

    acl_group_id = form.get("acl_group_id", "")
    acl_group_id = int(acl_group_id) if acl_group_id else None

    timer = StructureTimer(
        structure_name=structure_name,
        structure_type=form.get("structure_type", "other"),
        system_name=form.get("system_name", "").strip() or "Unknown",
        region_name=form.get("region_name", "").strip() or None,
        owner_name=form.get("owner_name", "").strip() or "Unknown",
        disposition=form.get("disposition", "hostile"),
        timer_phase=form.get("timer_phase", "armor"),
        timer_expires=timer_expires,
        priority=form.get("priority", "normal"),
        notes=form.get("notes", "").strip() or None,
        source="manual",
        acl_group_id=acl_group_id,
        created_by=user_id,
    )
    db.add(timer)
    await db.commit()
    return RedirectResponse("/structure-timers", status_code=302)


# ── Edit timer ───────────────────────────────────────────────────────────────

@router.post("/{timer_id}/edit", response_class=HTMLResponse)
async def edit_timer(timer_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=302)

    result = await db.execute(select(StructureTimer).where(StructureTimer.id == timer_id))
    timer = result.scalar_one_or_none()
    if not timer:
        return RedirectResponse("/structure-timers", status_code=302)

    if not await _can_modify_timer(db, user_id, timer):
        return RedirectResponse("/structure-timers", status_code=302)

    form = await request.form()

    # Parse timer expiry
    mode = form.get("timer_mode", "countdown")
    if mode == "countdown":
        td = _parse_countdown(form.get("countdown_text", ""))
        if td:
            timer.timer_expires = datetime.now(timezone.utc).replace(tzinfo=None) + td
    elif mode == "absolute":
        try:
            dt_str = form.get("datetime_utc", "")
            if dt_str:
                timer.timer_expires = datetime.fromisoformat(dt_str).replace(tzinfo=None)
        except ValueError:
            pass

    name = form.get("structure_name", "").strip()
    if name:
        timer.structure_name = name
    timer.structure_type = form.get("structure_type", timer.structure_type)
    timer.system_name = form.get("system_name", "").strip() or timer.system_name
    timer.region_name = form.get("region_name", "").strip() or None
    timer.owner_name = form.get("owner_name", "").strip() or timer.owner_name
    timer.disposition = form.get("disposition", timer.disposition)
    timer.timer_phase = form.get("timer_phase", timer.timer_phase)
    timer.priority = form.get("priority", timer.priority)
    timer.notes = form.get("notes", "").strip() or None
    acl_val = form.get("acl_group_id", "")
    timer.acl_group_id = int(acl_val) if acl_val else None

    await db.commit()
    return RedirectResponse("/structure-timers", status_code=302)


# ── Delete timer ─────────────────────────────────────────────────────────────

@router.post("/{timer_id}/delete", response_class=HTMLResponse)
async def delete_timer(timer_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=302)

    result = await db.execute(select(StructureTimer).where(StructureTimer.id == timer_id))
    timer = result.scalar_one_or_none()
    if timer and await _can_modify_timer(db, user_id, timer):
        await db.delete(timer)
        await db.commit()

    return RedirectResponse("/structure-timers", status_code=302)


# ── ACL management ───────────────────────────────────────────────────────────

@router.post("/acl/create", response_class=HTMLResponse)
async def create_acl_group(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=302)

    form = await request.form()
    name = form.get("name", "").strip()
    if not name:
        return RedirectResponse("/structure-timers", status_code=302)

    db.add(TimerACLGroup(name=name, created_by=user_id))
    await db.commit()
    return RedirectResponse("/structure-timers", status_code=302)


@router.post("/acl/{group_id}/add", response_class=HTMLResponse)
async def acl_add_entry(group_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    form = await request.form()
    entry_type = form.get("entry_type", "corporation")
    eve_id = form.get("eve_id", "")
    name = form.get("name", "").strip()

    if entry_type not in ("character", "corporation", "alliance") or not eve_id:
        return RedirectResponse("/structure-timers", status_code=302)

    try:
        eve_id_int = int(eve_id)
    except ValueError:
        return RedirectResponse("/structure-timers", status_code=302)

    # Resolve name if not provided
    if not name:
        from app.esi.client import ESIClient
        try:
            client = ESIClient("")
            names_data = await client.post_public("/universe/names/", [eve_id_int])
            if names_data:
                name = names_data[0].get("name", f"{entry_type} {eve_id_int}")
        except Exception:
            name = f"{entry_type.title()} {eve_id_int}"

    db.add(TimerACLEntry(
        group_id=group_id, entry_type=entry_type,
        eve_id=eve_id_int, name=name,
    ))
    await db.commit()
    return RedirectResponse("/structure-timers", status_code=302)


@router.post("/acl/{group_id}/remove/{entry_id}", response_class=HTMLResponse)
async def acl_remove_entry(group_id: int, entry_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    result = await db.execute(
        select(TimerACLEntry).where(TimerACLEntry.id == entry_id, TimerACLEntry.group_id == group_id)
    )
    entry = result.scalar_one_or_none()
    if entry:
        await db.delete(entry)
        await db.commit()
    return RedirectResponse("/structure-timers", status_code=302)


@router.post("/acl/{group_id}/delete", response_class=HTMLResponse)
async def delete_acl_group(group_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    result = await db.execute(select(TimerACLGroup).where(TimerACLGroup.id == group_id))
    group = result.scalar_one_or_none()
    if group:
        # Unlink timers using this group
        timer_result = await db.execute(
            select(StructureTimer).where(StructureTimer.acl_group_id == group_id)
        )
        for t in timer_result.scalars().all():
            t.acl_group_id = None
        await db.delete(group)
        await db.commit()
    return RedirectResponse("/structure-timers", status_code=302)


# ── ACL member search (reuses owner search pattern) ──────────────────────────

@router.get("/acl/search", response_class=HTMLResponse)
async def acl_search(request: Request, db: AsyncSession = Depends(get_db)):
    q = request.query_params.get("q", "").strip()
    category = request.query_params.get("category", "corporation")
    if len(q) < 3:
        return HTMLResponse("")

    from app.esi.client import ESIClient, get_client

    # Try authenticated fuzzy search
    search_scope = "esi-search.search_structures.v1"
    char_result = await db.execute(
        select(Character).where(Character.scopes.contains(search_scope)).limit(1)
    )
    char = char_result.scalar_one_or_none()

    results = []
    if char:
        try:
            client = await get_client(char, db)
            search_data = await client.get(
                f"/characters/{char.character_id}/search/",
                params={"categories": category, "search": q, "strict": "false"},
            )
            ids = search_data.get(category, [])[:8]
            if ids:
                pub = ESIClient("")
                names_data = await pub.post_public("/universe/names/", ids)
                results = [{"id": item["id"], "name": item["name"]} for item in names_data]
        except Exception:
            pass

    # Fallback: exact match
    if not results:
        try:
            pub = ESIClient("")
            cat_map = {"character": "characters", "corporation": "corporations", "alliance": "alliances"}
            id_data = await pub.post_public("/universe/ids/", [q])
            for item in id_data.get(cat_map.get(category, "corporations"), []):
                results.append({"id": item["id"], "name": item["name"]})
        except Exception:
            return HTMLResponse('<div style="font-size:10px;color:var(--danger);padding:0.25rem;">Search failed.</div>')

    if not results:
        return HTMLResponse('<div style="font-size:10px;color:var(--muted);padding:0.25rem;">No results.</div>')

    from html import escape
    # Image URLs by category
    img_base = {
        "character": "https://images.evetech.net/characters/{}/portrait?size=32",
        "corporation": "https://images.evetech.net/corporations/{}/logo?size=32",
        "alliance": "https://images.evetech.net/alliances/{}/logo?size=32",
    }
    html = []
    for r in results:
        safe = escape(r["name"], quote=True)
        img_url = img_base.get(category, "").format(r["id"])
        html.append(
            f'<div style="padding:0.25rem 0.5rem;font-size:10px;color:var(--text);'
            f'cursor:pointer;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:0.4rem;" '
            f'onmouseover="this.style.background=\'var(--border)\'" '
            f'onmouseout="this.style.background=\'none\'" '
            f'data-id="{r["id"]}" data-name="{safe}" '
            f'onclick="selectACLEntry({r["id"]}, \'{safe}\')">'
            f'<img src="{img_url}" style="width:16px;height:16px;border-radius:2px;" onerror="this.style.display=\'none\'">'
            f'{safe}</div>'
        )
    return HTMLResponse("".join(html))


# ── Search endpoints (typeahead) ─────────────────────────────────────────────

def _render_search_results(results: list[dict], name_key: str, id_key: str, js_fn: str) -> str:
    """Render search results as HTML dropdown items."""
    from html import escape
    if not results:
        return '<div style="font-size:10px;color:var(--muted);padding:0.25rem;">No results.</div>'
    html = []
    for r in results:
        safe_name = escape(r[name_key], quote=True)
        html.append(
            f'<div style="padding:0.25rem 0.5rem;font-size:10px;color:var(--text);'
            f'cursor:pointer;border-bottom:1px solid var(--border);" '
            f'onmouseover="this.style.background=\'var(--border)\'" '
            f'onmouseout="this.style.background=\'none\'" '
            f'onclick="{js_fn}(\'{safe_name}\')">{safe_name}</div>'
        )
    return "".join(html)


@router.get("/search/systems", response_class=HTMLResponse)
async def search_systems(request: Request, db: AsyncSession = Depends(get_db)):
    q = request.query_params.get("q", "").strip()
    if len(q) < 2:
        return HTMLResponse("")
    results = await sde.search_systems(db, q, limit=8)
    items = [{"name": f"{r['system_name']} ({r['security']:.1f})", "id": r["system_id"]} for r in results]
    if not items:
        return HTMLResponse('<div style="font-size:10px;color:var(--muted);padding:0.25rem;">No systems found.</div>')
    from html import escape
    html = []
    for r in results:
        safe = escape(r["system_name"], quote=True)
        sec = r["security"]
        sec_color = "var(--success)" if sec >= 0.5 else "var(--accent)" if sec > 0.0 else "var(--danger)"
        html.append(
            f'<div style="padding:0.25rem 0.5rem;font-size:10px;color:var(--text);'
            f'cursor:pointer;border-bottom:1px solid var(--border);" '
            f'onmouseover="this.style.background=\'var(--border)\'" '
            f'onmouseout="this.style.background=\'none\'" '
            f'onclick="selectSystem(\'{safe}\')">'
            f'{safe} <span style="color:{sec_color};">{sec:.1f}</span></div>'
        )
    return HTMLResponse("".join(html))


@router.get("/search/regions", response_class=HTMLResponse)
async def search_regions(request: Request, db: AsyncSession = Depends(get_db)):
    q = request.query_params.get("q", "").strip()
    if len(q) < 2:
        return HTMLResponse("")
    results = await sde.search_regions(db, q, limit=8)
    return HTMLResponse(_render_search_results(
        [{"name": r["region_name"], "id": r["region_id"]} for r in results],
        "name", "id", "selectRegion"
    ))


@router.get("/search/owners", response_class=HTMLResponse)
async def search_owners(request: Request, db: AsyncSession = Depends(get_db)):
    q = request.query_params.get("q", "").strip()
    if len(q) < 3:
        return HTMLResponse("")

    from app.db.models import Character
    from app.esi.client import ESIClient, get_client

    # Try authenticated fuzzy search first
    search_scope = "esi-search.search_structures.v1"
    char_result = await db.execute(
        select(Character).where(Character.scopes.contains(search_scope)).limit(1)
    )
    char = char_result.scalar_one_or_none()

    results = []
    if char:
        try:
            client = await get_client(char, db)
            # Search both corporations and alliances
            for category in ("corporation", "alliance"):
                search_data = await client.get(
                    f"/characters/{char.character_id}/search/",
                    params={"categories": category, "search": q, "strict": "false"},
                )
                ids = search_data.get(category, [])[:5]
                if ids:
                    pub_client = ESIClient("")
                    names_data = await pub_client.post_public("/universe/names/", ids)
                    for item in names_data:
                        results.append({"id": item["id"], "name": item["name"], "type": category})
        except Exception:
            pass

    # Fallback: exact match via /universe/ids/
    if not results:
        try:
            pub_client = ESIClient("")
            id_data = await pub_client.post_public("/universe/ids/", [q])
            for cat, etype in [("corporations", "corporation"), ("alliances", "alliance")]:
                for item in id_data.get(cat, []):
                    results.append({"id": item["id"], "name": item["name"], "type": etype})
        except Exception:
            return HTMLResponse('<div style="font-size:10px;color:var(--danger);padding:0.25rem;">Search failed.</div>')

    if not results:
        return HTMLResponse('<div style="font-size:10px;color:var(--muted);padding:0.25rem;">No results.</div>')

    from html import escape
    img_base = {
        "corporation": "https://images.evetech.net/corporations/{}/logo?size=32",
        "alliance": "https://images.evetech.net/alliances/{}/logo?size=32",
    }
    cat_labels = {"corporation": "Corp", "alliance": "Alliance"}
    html = []
    for r in results:
        safe = escape(r["name"], quote=True)
        img_url = img_base.get(r["type"], "").format(r["id"])
        label = cat_labels.get(r["type"], "")
        html.append(
            f'<div style="padding:0.25rem 0.5rem;font-size:10px;color:var(--text);'
            f'cursor:pointer;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:0.4rem;" '
            f'onmouseover="this.style.background=\'var(--border)\'" '
            f'onmouseout="this.style.background=\'none\'" '
            f'onclick="selectOwner(\'{safe}\')">'
            f'<img src="{img_url}" style="width:16px;height:16px;border-radius:2px;" onerror="this.style.display=\'none\'">'
            f'{safe} <span style="color:var(--muted);font-size:8px;">{label}</span></div>'
        )
    return HTMLResponse("".join(html))


# ── ESI auto-detection (called from corporations.py) ─────────────────────────

REINFORCE_STATES = {
    "shield_reinforce": "shield",
    "armor_reinforce": "armor",
    "hull_reinforce": "hull",
    "anchoring": "anchoring",
    "unanchoring": "unanchoring",
}


async def sync_esi_structure_timers(db: AsyncSession, raw_structs: list):
    """Auto-create/update timers for reinforced corp structures."""
    for s in raw_structs:
        state = s.get("state", "")
        timer_end_str = s.get("state_timer_end")
        if state not in REINFORCE_STATES or not timer_end_str:
            continue

        phase = REINFORCE_STATES[state]
        structure_id = s.get("structure_id")
        try:
            timer_expires = datetime.fromisoformat(
                timer_end_str.replace("Z", "+00:00")
            ).replace(tzinfo=None)
        except (ValueError, TypeError):
            continue

        # Check for existing active ESI timer for this structure + phase
        existing = await db.execute(
            select(StructureTimer).where(
                StructureTimer.esi_structure_id == structure_id,
                StructureTimer.timer_phase == phase,
                StructureTimer.is_archived == False,
            )
        )
        timer = existing.scalar_one_or_none()

        if timer:
            timer.timer_expires = timer_expires
            continue

        # Resolve names
        system_id = s.get("system_id")
        type_id = s.get("type_id")
        sys_info = await sde.system_info(db, system_id) if system_id else None
        type_name = await sde.type_id_to_name(db, type_id) if type_id else "Unknown"

        db.add(StructureTimer(
            structure_name=s.get("name", "Unknown Structure"),
            structure_type=_classify_structure_type(type_name or ""),
            system_name=sys_info["system_name"] if sys_info else f"System {system_id}",
            region_name=sys_info.get("region") if sys_info else None,
            owner_name="Our Corp",
            disposition="friendly",
            timer_phase=phase,
            timer_expires=timer_expires,
            priority="critical",
            source="esi",
            esi_structure_id=structure_id,
        ))

    await db.commit()
