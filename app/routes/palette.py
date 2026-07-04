"""Command palette backend — the Ctrl+K universal search (Phase 2 Task 1).

Serves `/nav/palette?q=` as an htmx partial consumed by the `<dialog id="cmdk">`
in base.html. Pins and recents are localStorage-only (rendered client-side by
base.html JS when the query is empty); this endpoint never sees them.

Query behaviour:
  * empty q → pages-only partial (whole nav registry, admin items gated by
    the session `is_admin` flag). The client overlays Pinned/Recent above it.
  * non-empty q → up to four buckets, each capped:
      Pages       — nav registry, case-insensitive substring on label/group
      Characters  — the session user's own active characters (name LIKE)
      Systems     — SDE solar systems (prefix-first, then substring)
      Items       — published SDE types (prefix-first, then substring)

The pure result-builders (`_flatten_pages`, `_page_results`, `_system_link`)
are unit-tested in tests/test_palette.py. The DB buckets are thin async
wrappers around LIKE-with-LIMIT queries — these SDE tables are small/medium
(systems ~8k, published types ~30k) so an unindexed LIKE with a small LIMIT
is fine; do NOT add indexes for this (per plan).
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AsyncSessionLocal, Character
from app.db.sde_models import SDEGroup, SDERegion, SDESystem, SDEType
from app.nav import NAV_GROUPS

router = APIRouter()
# MUST be named `templates` — main.py's sys.modules loop pushes the nav globals
# (nav_groups / css_v / …) onto every `Jinja2Templates` instance found under
# app.routes.* by this attribute name. Rename it and base.html loses its nav.
templates = Jinja2Templates(directory="app/templates")

# J-space (Anoikis) system id band. Pochven retains k-space ids (~30002xxx),
# so it correctly falls through to the star-map link below.
WH_SYSTEM_MIN = 31000000
WH_SYSTEM_MAX = 31999999

PAGES_CAP = 8
CHARS_CAP = 5
SYSTEMS_CAP = 8
ITEMS_CAP = 8


# ── Pure result-builders (unit-tested) ────────────────────────────────────

def _flatten_pages(is_admin: bool) -> list[dict]:
    """Flatten NAV_GROUPS into navigable page rows.

    Each row: {"label", "url", "group"}. Skips external links (zKillboard,
    Wanderer) and admin-only entries unless `is_admin`. Plain-link groups
    with no items (Corporations, Skill Plans) contribute their own group row.
    De-duplicated by url, preserving first-seen order.
    """
    pages: list[dict] = []
    seen: set[str] = set()

    def _add(label: str, url: str | None, group_label: str) -> None:
        if not url or url.startswith(("http://", "https://", "#")):
            return
        if url in seen:
            return
        seen.add(url)
        pages.append({"label": label, "url": url, "group": group_label})

    for group in NAV_GROUPS:
        if group.get("admin") and not is_admin:
            continue
        if not group.get("items"):
            _add(group["label"], group.get("url"), group["label"])
            continue
        for item in group["items"]:
            if item.get("external"):
                continue
            if item.get("admin") and not is_admin:
                continue
            _add(item["label"], item.get("url"), group["label"])
    return pages


def _page_results(q: str, is_admin: bool) -> list[dict]:
    """Case-insensitive substring match on page label OR group label.

    Empty query returns the full flattened list (client overlays pins/recents);
    non-empty query is capped at PAGES_CAP.
    """
    ql = q.strip().lower()
    if not ql:
        return _flatten_pages(is_admin)
    out: list[dict] = []
    for p in _flatten_pages(is_admin):
        if ql in p["label"].lower() or ql in p["group"].lower():
            out.append(p)
        if len(out) >= PAGES_CAP:
            break
    return out


def _system_link(system_id: int, name: str) -> str:
    """Deep link for a system row.

    J-space (Anoikis, id 31000000-31999999) → the wormhole system detail page.
    Everything else (k-space, and Pochven which retains k-space ids) → the
    star map keyed by numeric system_id via ?focus=. The React map ignores
    unknown query params today, so this degrades gracefully to "open the star
    map"; a future StarMap.tsx wire-up can pan to ?focus on load.
    """
    if WH_SYSTEM_MIN <= system_id <= WH_SYSTEM_MAX:
        return f"/wormholes/system/{quote(name)}"
    return f"/map?focus={system_id}"


# ── Async DB buckets ───────────────────────────────────────────────────────

async def _bucket_characters(db: AsyncSession, user_id: int, q: str,
                             cap: int = CHARS_CAP) -> list[dict]:
    """The session user's own active characters whose name matches `q`."""
    like = f"%{q}%"
    rows = (await db.execute(
        select(Character.character_id, Character.character_name)
        .where(
            Character.user_id == user_id,
            Character.is_active.is_(True),
            Character.character_name.ilike(like),
        )
        .order_by(Character.character_name)
        .limit(cap)
    )).all()
    return [
        {"label": name, "url": f"/character/{cid}", "context": "Character"}
        for cid, name in rows
    ]


async def _bucket_systems(db: AsyncSession, q: str,
                          cap: int = SYSTEMS_CAP) -> list[dict]:
    """SDE solar systems matching `q`; prefix matches sort ahead of substrings.

    Context is the region name (falls back to a bare "System" label)."""
    prefix = f"{q}%"
    sub = f"%{q}%"
    is_prefix = case((SDESystem.system_name.ilike(prefix), 0), else_=1)
    rows = (await db.execute(
        select(SDESystem.system_id, SDESystem.system_name, SDERegion.region_name)
        .outerjoin(SDERegion, SDERegion.region_id == SDESystem.region_id)
        .where(SDESystem.system_name.ilike(sub))
        .order_by(is_prefix, func.length(SDESystem.system_name),
                  SDESystem.system_name)
        .limit(cap)
    )).all()
    return [
        {"label": name, "url": _system_link(sid, name),
         "context": region or "System"}
        for sid, name, region in rows
    ]


async def _bucket_items(db: AsyncSession, q: str,
                        cap: int = ITEMS_CAP) -> list[dict]:
    """Published SDE types matching `q`; prefix matches sort ahead of substrings.

    Context is the item group name. Deep-links to the manufacturing calculator
    with a ?search= prefill (base.html industry.html consumes it)."""
    prefix = f"{q}%"
    sub = f"%{q}%"
    is_prefix = case((SDEType.type_name.ilike(prefix), 0), else_=1)
    rows = (await db.execute(
        select(SDEType.type_id, SDEType.type_name, SDEGroup.group_name)
        .outerjoin(SDEGroup, SDEGroup.group_id == SDEType.group_id)
        .where(SDEType.published.is_(True), SDEType.type_name.ilike(sub))
        .order_by(is_prefix, func.length(SDEType.type_name), SDEType.type_name)
        .limit(cap)
    )).all()
    return [
        {"label": name,
         "url": f"/industry/manufacturing?search={quote(name)}",
         "context": group or "Item"}
        for _tid, name, group in rows
    ]


# ── Endpoint ───────────────────────────────────────────────────────────────

@router.get("/nav/palette", response_class=HTMLResponse)
async def palette(request: Request):
    """htmx partial for the command palette. 401 (empty body) if unauthenticated."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    is_admin = bool(request.session.get("is_admin"))
    # 64 chars is beyond any real search; caps LIKE-pattern cost from scripted clients
    q = (request.query_params.get("q") or "").strip()[:64]

    ctx: dict = {
        "q": q,
        "pages": _page_results(q, is_admin),
        "characters": [],
        "systems": [],
        "items": [],
    }
    if q:
        async with AsyncSessionLocal() as db:
            ctx["characters"] = await _bucket_characters(db, user_id, q)
            ctx["systems"] = await _bucket_systems(db, q)
            ctx["items"] = await _bucket_items(db, q)

    return templates.TemplateResponse(request, "partials/palette_results.html", ctx)
