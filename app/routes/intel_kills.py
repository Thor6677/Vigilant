"""Intel → Kill Feed.

Live universe-wide kill feed from killmail.stream's _recent_kills buffer.
Filters: space class (HS/LS/NS/WH + sub-classes + Shattered modifier),
ship search, attacker entity search, victim entity search.

Click a row to expand the detail panel (victim + fitting + ISK + attackers).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.cache import cache_get, cache_set
from app.db.models import Character, Killmail, KillmailAttacker, KillmailItem, get_db
from app.db.sde_models import SDESystem
from app.intel.killmail_stream import _sys_meta_cache, get_recent_kills
from app.intel.recent_battles import resolve_entity_names
from app.sde.lookup import search_ship_types, type_ids_to_names

log = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

MAX_ROWS_INITIAL = 100

# Normalize sec_band's "Highsec"/"Lowsec"/"Nullsec"/"Unknown" return values
# (plus the "w-space" literal set by _resolve_sys_meta) to short codes for
# consistent CSS class names and filter comparisons (used in Task 6 too).
_BAND_NORMALIZE = {
    "Highsec": "hs",
    "Lowsec": "ls",
    "Nullsec": "ns",
    "Unknown": "unknown",
    "w-space": "wh",
}


@router.get("/intel/kills", response_class=HTMLResponse)
async def intel_kills_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Page shell. The feed content is loaded via htmx into the container."""
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")
    return templates.TemplateResponse(
        "intel_kills.html",
        {"request": request},
    )


def _apply_space_filter(
    kills: list[dict],
    spaces: set[str],
    wh_classes: set[str],
    shattered_only: bool,
) -> list[dict]:
    """Filter kills by space-class chips.

    `spaces`: short codes {hs, ls, ns, wh} — OR within set.
    `wh_classes`: {c1..c6, thera, drifter, pochven} — OR within set; AND'd
        with being in WH band.
    `shattered_only`: AND'd on top — requires the system's group_label to
        contain "shattered".
    All-empty = no constraint.

    Within-category OR / cross-category AND: e.g. {hs} + {c5} means
    "(band==hs) AND (band==wh AND first_token==c5)" — impossible, so empty.

    To disambiguate C1 vs C13: split group_label on whitespace and compare
    the first token equality (so "C13 (Shattered)" → "c13", not matched by
    substring "c1").
    """
    if not spaces and not wh_classes and not shattered_only:
        return kills
    out = []
    for k in kills:
        sid = k.get("solar_system_id") or 0
        meta = _sys_meta_cache.get(sid) or {}
        raw_band = meta.get("band") or "Unknown"
        band_norm = _BAND_NORMALIZE.get(raw_band, "unknown")
        group_label = meta.get("group_label") or ""
        first_token = group_label.split(" ")[0].lower() if group_label else ""
        is_wh = band_norm == "wh"

        ok = True
        if spaces:
            ok = ok and (band_norm in spaces)
        if wh_classes:
            ok = ok and is_wh and (first_token in wh_classes)
        if shattered_only:
            ok = ok and ("shattered" in group_label.lower())
        if ok:
            out.append(k)
    return out


@router.get("/intel/kills/feed", response_class=HTMLResponse)
async def intel_kills_feed(
    request: Request,
    since: int | None = None,
    space: str = "",
    wh_class: str = "",
    shattered: int = 0,
    ship_id: str = "",
    attacker_char: str = "",
    attacker_corp: str = "",
    attacker_alli: str = "",
    victim_char: str = "",
    victim_corp: str = "",
    victim_alli: str = "",
    db: AsyncSession = Depends(get_db),
):
    """Live tail — reads _recent_kills in memory, renders the row partial.

    `since`: if provided, return only kills with killmail_id > since (for
    incremental htmx prepends). Otherwise return up to MAX_ROWS_INITIAL.
    `space`/`wh_class`: comma-separated short codes (e.g. "hs,ns" or
    "c5,c6"). `shattered=1` to require shattered systems only.
    `ship_id`: comma-separated SDE type IDs — OR-multiselect over
    victim.ship_type_id.
    `attacker_char`/`attacker_corp`/`attacker_alli`: comma-separated entity
    IDs — OR within group; AND across the three (any attacker on the kill
    must match at least one of the three groups). Cross-category with
    victim_* / ship_id / space is AND.
    `victim_char`/`victim_corp`/`victim_alli`: same shape, matched against
    the kill's victim.character_id/corporation_id/alliance_id.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    spaces = {s.strip() for s in space.split(",") if s.strip()}
    wh_classes = {c.strip() for c in wh_class.split(",") if c.strip()}
    shattered_only = bool(shattered)
    ship_ids = {int(s) for s in ship_id.split(",") if s.strip().isdigit()}

    def _ids(s: str) -> set[int]:
        return {int(x) for x in s.split(",") if x.strip().isdigit()}

    a_chars = _ids(attacker_char)
    a_corps = _ids(attacker_corp)
    a_allis = _ids(attacker_alli)
    v_chars = _ids(victim_char)
    v_corps = _ids(victim_corp)
    v_allis = _ids(victim_alli)

    kills = get_recent_kills()
    kills = sorted(kills, key=lambda k: k.get("killmail_id") or 0, reverse=True)

    if since:
        kills = [k for k in kills if (k.get("killmail_id") or 0) > since]

    kills = _apply_space_filter(kills, spaces, wh_classes, shattered_only)

    if ship_ids:
        kills = [
            k for k in kills
            if ((k.get("victim") or {}).get("ship_type_id") in ship_ids)
        ]

    if a_chars or a_corps or a_allis:
        kills = [
            k for k in kills
            if any(
                (a.get("character_id") in a_chars)
                or (a.get("corporation_id") in a_corps)
                or (a.get("alliance_id") in a_allis)
                for a in (k.get("attackers") or [])
            )
        ]

    if v_chars or v_corps or v_allis:
        kills = [
            k for k in kills
            if ((k.get("victim") or {}).get("character_id") in v_chars)
            or ((k.get("victim") or {}).get("corporation_id") in v_corps)
            or ((k.get("victim") or {}).get("alliance_id") in v_allis)
        ]

    if not since:
        kills = kills[:MAX_ROWS_INITIAL]

    if not kills:
        return HTMLResponse("")

    enriched = await _enrich_kills(kills, db)
    # total_in_buffer reflects the raw count (unfiltered) so the user sees
    # how much is being hidden by the active filter.
    total_in_buffer = len(get_recent_kills())

    return templates.TemplateResponse(
        "partials/intel_kills_feed.html",
        {
            "request": request,
            "kills": enriched,
            "total_in_buffer": total_in_buffer,
            "newest_id": enriched[0]["killmail_id"] if enriched else (since or 0),
        },
    )


@router.get("/intel/kills/older", response_class=HTMLResponse)
async def intel_kills_older(
    request: Request,
    before: str,
    window_hours: int = 6,
    space: str = "",
    wh_class: str = "",  # accepted for parity but NOT enforced server-side in v1
    shattered: int = 0,  # same
    ship_id: str = "",
    attacker_char: str = "",
    attacker_corp: str = "",
    attacker_alli: str = "",
    victim_char: str = "",
    victim_corp: str = "",
    victim_alli: str = "",
    db: AsyncSession = Depends(get_db),
):
    """Paginate historical kills from the killmails table.

    Returns up to 100 rows in a 6h window strictly older than `before`,
    matching the same broad filter set as the live feed: space band
    (HS/LS/NS/WH), victim ship, attacker entity, victim entity.

    WH sub-class chips + Shattered modifier are deliberately NOT enforced
    server-side here (would require walking constellation → region from
    the SDE on every kill); they apply only to the live in-memory feed
    where `_sys_meta_cache` is pre-resolved.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    try:
        before_dt = datetime.fromisoformat(before.replace("Z", ""))
    except (ValueError, AttributeError):
        return HTMLResponse("<p>Invalid timestamp</p>", status_code=400)
    after_dt = before_dt - timedelta(hours=window_hours)

    stmt = select(Killmail).where(
        Killmail.killmail_time < before_dt,
        Killmail.killmail_time >= after_dt,
    )

    # Victim ship filter
    ship_ids = {int(s) for s in ship_id.split(",") if s.strip().isdigit()}
    if ship_ids:
        stmt = stmt.where(Killmail.victim_ship_type_id.in_(ship_ids))

    # Victim entity filter (OR within category)
    v_chars = {int(s) for s in victim_char.split(",") if s.strip().isdigit()}
    v_corps = {int(s) for s in victim_corp.split(",") if s.strip().isdigit()}
    v_allis = {int(s) for s in victim_alli.split(",") if s.strip().isdigit()}
    victim_conds = []
    if v_chars:
        victim_conds.append(Killmail.victim_character_id.in_(v_chars))
    if v_corps:
        victim_conds.append(Killmail.victim_corporation_id.in_(v_corps))
    if v_allis:
        victim_conds.append(Killmail.victim_alliance_id.in_(v_allis))
    if victim_conds:
        stmt = stmt.where(or_(*victim_conds))

    # Attacker entity filter — EXISTS subquery
    a_chars = {int(s) for s in attacker_char.split(",") if s.strip().isdigit()}
    a_corps = {int(s) for s in attacker_corp.split(",") if s.strip().isdigit()}
    a_allis = {int(s) for s in attacker_alli.split(",") if s.strip().isdigit()}
    if a_chars or a_corps or a_allis:
        att_conds = []
        if a_chars:
            att_conds.append(KillmailAttacker.character_id.in_(a_chars))
        if a_corps:
            att_conds.append(KillmailAttacker.corporation_id.in_(a_corps))
        if a_allis:
            att_conds.append(KillmailAttacker.alliance_id.in_(a_allis))
        att_subq = select(KillmailAttacker.killmail_id).where(
            KillmailAttacker.killmail_id == Killmail.killmail_id,
            or_(*att_conds),
        )
        stmt = stmt.where(exists(att_subq))

    # Space band filter (HS/LS/NS/WH only — WH sub-class deferred to v2)
    spaces = {s.strip() for s in space.split(",") if s.strip()}
    # If all four bands are selected, that's the identity filter — skip.
    if spaces and not ({"hs", "ls", "ns", "wh"} <= spaces):
        sec_conds = []
        if "hs" in spaces:
            sec_conds.append(SDESystem.security >= 0.5)
        if "ls" in spaces:
            sec_conds.append(and_(SDESystem.security > 0.0, SDESystem.security < 0.5))
        if "ns" in spaces:
            sec_conds.append(
                and_(SDESystem.security <= 0.0, SDESystem.system_id < 31000000)
            )
        if "wh" in spaces:
            sec_conds.append(SDESystem.system_id >= 31000000)
        if sec_conds:
            sys_subq = select(SDESystem.system_id).where(or_(*sec_conds))
            stmt = stmt.where(Killmail.solar_system_id.in_(sys_subq))

    stmt = stmt.order_by(Killmail.killmail_time.desc()).limit(100)
    rows = (await db.execute(stmt)).scalars().all()

    if not rows:
        return HTMLResponse(
            f'<div class="kf-empty" data-window="{window_hours}" '
            f'style="text-align:center;color:var(--muted);'
            f'font-size:11px;padding:14px;">'
            f"No older kills in last {window_hours}h matching filters. "
            f'<a href="#" data-expand style="color:var(--accent);">'
            f"Load 24h?</a></div>"
        )

    # Batch-load attackers for all rows to avoid N+1
    kid_list = [r.killmail_id for r in rows]
    att_rows = (
        await db.execute(
            select(
                KillmailAttacker.killmail_id,
                KillmailAttacker.character_id,
                KillmailAttacker.corporation_id,
                KillmailAttacker.alliance_id,
                KillmailAttacker.ship_type_id,
                KillmailAttacker.final_blow,
            ).where(KillmailAttacker.killmail_id.in_(kid_list))
        )
    ).all()

    attackers_by_kid: dict[int, list[dict]] = {}
    for kid, char_id, corp_id, alli_id, ship_id_, fb in att_rows:
        attackers_by_kid.setdefault(kid, []).append(
            {
                "character_id": char_id,
                "corporation_id": corp_id,
                "alliance_id": alli_id,
                "ship_type_id": ship_id_,
                "final_blow": bool(fb),
            }
        )

    # Reshape to the in-memory dict shape that _enrich_kills consumes
    fake_kills = []
    for r in rows:
        fake_kills.append(
            {
                "killmail_id": r.killmail_id,
                "killmail_time": r.killmail_time.isoformat()
                if r.killmail_time
                else None,
                "solar_system_id": r.solar_system_id,
                "victim": {
                    "character_id": r.victim_character_id,
                    "corporation_id": r.victim_corporation_id,
                    "alliance_id": r.victim_alliance_id,
                    "ship_type_id": r.victim_ship_type_id,
                },
                "attackers": attackers_by_kid.get(r.killmail_id, []),
                "zkb": {"totalValue": r.total_value or 0},
            }
        )

    enriched = await _enrich_kills(fake_kills, db)
    return templates.TemplateResponse(
        "partials/intel_kills_feed.html",
        {
            "request": request,
            "kills": enriched,
            "total_in_buffer": len(get_recent_kills()),
            "newest_id": 0,
            "older_mode": True,
        },
    )


async def _resolve_for_feed(
    db: AsyncSession, type_ids: set[int], entity_ids: set[int]
) -> dict[int, str]:
    """Combine SDE ship/type names (local) with ESI char/corp/alliance names
    (cached via resolve_entity_names). Returns one merged {id: name} map.

    Splitting avoids paying an ESI round trip for ship type names that already
    live in our SDE mirror, and avoids confusing the ESI resolver with type IDs
    (different ID namespace, would just negative-cache them)."""
    type_ids = {i for i in type_ids if i}
    entity_ids = {i for i in entity_ids if i}
    out: dict[int, str] = {}
    if type_ids:
        try:
            out.update(await type_ids_to_names(db, list(type_ids)))
        except Exception as e:
            log.debug("intel_kills: type name resolve failed: %s", e)
    if entity_ids:
        try:
            out.update(await resolve_entity_names(list(entity_ids)))
        except Exception as e:
            log.debug("intel_kills: entity name resolve failed: %s", e)
    return out


async def _enrich_kills(kills: list[dict], db: AsyncSession) -> list[dict]:
    """Resolve names + sec band for a batch of kill records from _recent_kills."""
    type_ids: set[int] = set()
    entity_ids: set[int] = set()
    for k in kills:
        v = k.get("victim") or {}
        if v.get("ship_type_id"):
            type_ids.add(v["ship_type_id"])
        for key in ("character_id", "corporation_id", "alliance_id"):
            if v.get(key):
                entity_ids.add(v[key])
        attackers = k.get("attackers") or []
        top = next(
            (a for a in attackers if a.get("final_blow")),
            attackers[0] if attackers else {},
        )
        for key in ("character_id", "corporation_id"):
            if top.get(key):
                entity_ids.add(top[key])

    name_map = await _resolve_for_feed(db, type_ids, entity_ids)

    out = []
    for k in kills:
        v = k.get("victim") or {}
        attackers = k.get("attackers") or []
        top = next(
            (a for a in attackers if a.get("final_blow")),
            attackers[0] if attackers else {},
        )
        sid = k.get("solar_system_id") or 0
        meta = _sys_meta_cache.get(sid) or {}
        raw_band = meta.get("band") or "Unknown"
        out.append({
            "killmail_id": k.get("killmail_id"),
            "killmail_time": k.get("killmail_time"),
            "system_name": meta.get("system_name") or f"#{sid}",
            "system_band": _BAND_NORMALIZE.get(raw_band, "unknown"),
            "system_class_label": meta.get("group_label"),
            "victim_pilot": name_map.get(v.get("character_id"), "?"),
            "victim_corp": name_map.get(v.get("corporation_id"), ""),
            "victim_ship": name_map.get(v.get("ship_type_id"), "?"),
            "victim_ship_type_id": v.get("ship_type_id"),
            "top_attacker_pilot": name_map.get(top.get("character_id"), "?"),
            "top_attacker_corp": name_map.get(top.get("corporation_id"), ""),
            "gang_size": len(attackers),
            "isk": float((k.get("zkb") or {}).get("totalValue") or 0),
        })
    return out


@router.get("/intel/kills/resolve")
async def intel_kills_resolve(
    request: Request,
    q: str = "",
    kind: str = "ship",
    db: AsyncSession = Depends(get_db),
):
    """Autocomplete proxy for kill-feed filters.

    - `kind=ship`: local SDE substring match against published ships
      (categoryID=6). Returns up to 8 `{id, name, kind}` rows.
    - `kind=entity`: ESI /universe/ids autocomplete. Returns up to 5 of
      each kind (character / corporation / alliance) labeled `kind`.
      Cached 24h via the shared ESI cache (TTL keyed on
      `/intel_kills/resolve_entity` in `_ttl_for_path`).

    Requires auth; returns [] for unknown kinds.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse([], status_code=401)
    q = (q or "").strip()
    if not q or len(q) < 2:
        return JSONResponse([])
    if kind == "ship":
        return JSONResponse(await search_ship_types(db, q))
    if kind == "entity":
        return JSONResponse(await _resolve_entity(q))
    return JSONResponse([])


async def _resolve_entity(q: str) -> list[dict]:
    """ESI `/universe/ids/` autocomplete, cached 24h.

    Returns up to 5 entries from each of {character, corporation, alliance}
    with a `kind` label so the client can disambiguate same-numeric IDs
    across kinds.
    """
    cache_path = f"/intel_kills/resolve_entity/{q.lower()}"
    cached = await cache_get(None, cache_path)
    if cached is not None:
        return cached

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                "https://esi.evetech.net/latest/universe/ids/",
                json=[q],
                headers={"User-Agent": "Vigilant/1.0 (happyfun.fatman@gmail.com)"},
            )
            r.raise_for_status()
            data = r.json() if r.content else {}
    except Exception as e:
        log.warning("intel_kills: resolve entity failed for %r: %s", q, e)
        return []

    out: list[dict] = []
    for kind_key, list_key in (
        ("character", "characters"),
        ("corporation", "corporations"),
        ("alliance", "alliances"),
    ):
        for item in (data.get(list_key) or [])[:5]:
            if item.get("id") and item.get("name"):
                out.append({"id": item["id"], "name": item["name"], "kind": kind_key})

    await cache_set(None, cache_path, out)
    return out


def _slot_label(flag: int) -> str:
    if 11 <= flag <= 18:
        return "Low slots"
    if 19 <= flag <= 26:
        return "Mid slots"
    if 27 <= flag <= 34:
        return "High slots"
    if 92 <= flag <= 98:
        return "Rigs"
    if 125 <= flag <= 132:
        return "Subsystems"
    if flag == 5:
        return "Cargo"
    if flag == 87:
        return "Drone bay"
    if flag == 89:
        return "Implants"
    if flag == 90:
        return "Booster bay"
    return "Other"


_SLOT_ORDER = [
    "High slots", "Mid slots", "Low slots", "Rigs", "Subsystems",
    "Drone bay", "Cargo", "Implants", "Booster bay",
]


@router.get("/intel/kills/{killmail_id}/detail", response_class=HTMLResponse)
async def intel_kills_detail(
    killmail_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Inline detail panel: victim card + fitting + ISK + attackers.

    Cached aggressively (immutable) — killmails are write-once.
    Items-less old kills degrade to a "view on zKB" link.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    km = (await db.execute(
        select(Killmail).where(Killmail.killmail_id == killmail_id)
    )).scalars().first()
    if not km:
        raise HTTPException(404)

    items = (await db.execute(
        select(KillmailItem).where(KillmailItem.killmail_id == killmail_id)
    )).scalars().all()
    attackers = (await db.execute(
        select(KillmailAttacker).where(KillmailAttacker.killmail_id == killmail_id)
    )).scalars().all()

    # Collect IDs for name resolution
    type_ids: set[int] = set()
    entity_ids: set[int] = set()
    if km.victim_ship_type_id:
        type_ids.add(km.victim_ship_type_id)
    for x in (km.victim_character_id, km.victim_corporation_id, km.victim_alliance_id):
        if x:
            entity_ids.add(x)
    for a in attackers:
        if a.ship_type_id:
            type_ids.add(a.ship_type_id)
        if a.weapon_type_id:
            type_ids.add(a.weapon_type_id)
        for x in (a.character_id, a.corporation_id, a.alliance_id):
            if x:
                entity_ids.add(x)
    for it in items:
        type_ids.add(it.item_type_id)

    type_name_map = await type_ids_to_names(db, list(type_ids)) if type_ids else {}
    entity_name_map = await resolve_entity_names(list(entity_ids)) if entity_ids else {}
    name_map = {**type_name_map, **entity_name_map}

    system_name = f"#{km.solar_system_id}"
    if km.solar_system_id:
        row = (await db.execute(
            select(SDESystem.system_name).where(SDESystem.system_id == km.solar_system_id)
        )).first()
        if row:
            system_name = row[0]

    # Group items by slot
    slots_dict: dict[str, list[dict]] = {}
    for it in items:
        label = _slot_label(it.flag)
        if label == "Other":
            continue
        slots_dict.setdefault(label, []).append({
            "type_id": it.item_type_id,
            "name": name_map.get(it.item_type_id, f"#{it.item_type_id}"),
            "qty_destroyed": it.quantity_destroyed or 0,
            "qty_dropped": it.quantity_dropped or 0,
            "destroyed": (it.quantity_destroyed or 0) > 0,
            "dropped": (it.quantity_dropped or 0) > 0,
        })
    slots_ordered = [
        {"label": lbl, "items": slots_dict[lbl]}
        for lbl in _SLOT_ORDER if lbl in slots_dict
    ]

    # Sort attackers: final blow first, then damage_done DESC
    attackers_sorted = sorted(
        attackers,
        key=lambda a: (not a.final_blow, -(a.damage_done or 0)),
    )
    max_damage = max((a.damage_done or 0) for a in attackers) if attackers else 0
    total_damage = sum(a.damage_done or 0 for a in attackers)

    our_char_ids: set[int] = set()
    att_char_ids = [a.character_id for a in attackers if a.character_id]
    if att_char_ids:
        rows = await db.execute(
            select(Character.character_id).where(Character.character_id.in_(att_char_ids))
        )
        our_char_ids = {r[0] for r in rows.all()}

    attackers_view = []
    for a in attackers_sorted:
        dmg = a.damage_done or 0
        attackers_view.append({
            "pilot_id": a.character_id,
            "pilot": name_map.get(a.character_id, "?") if a.character_id else "NPC",
            "corp": name_map.get(a.corporation_id, "") if a.corporation_id else "",
            "ship_id": a.ship_type_id,
            "ship": name_map.get(a.ship_type_id, "?") if a.ship_type_id else "—",
            "weapon": name_map.get(a.weapon_type_id, "—") if a.weapon_type_id else "—",
            "damage": dmg,
            "damage_pct": int(100 * dmg / max_damage) if max_damage else 0,
            "share_pct": round(100 * dmg / total_damage, 1) if total_damage else 0,
            "final_blow": bool(a.final_blow),
            "internal_link": (a.character_id in our_char_ids) if a.character_id else False,
            "has_damage": dmg > 0,
        })

    response = templates.TemplateResponse(
        "partials/intel_kills_detail.html",
        {
            "request": request,
            "kid": killmail_id,
            "km": km,
            "victim_pilot": name_map.get(km.victim_character_id, "?") if km.victim_character_id else "NPC",
            "victim_corp": name_map.get(km.victim_corporation_id, "") if km.victim_corporation_id else "",
            "victim_ship": name_map.get(km.victim_ship_type_id, "?") if km.victim_ship_type_id else "?",
            "system_name": system_name,
            "slots": slots_ordered,
            "items_present": bool(items),
            "attackers": attackers_view,
            "attacker_count": len(attackers),
            "total_damage": total_damage,
            "total_destroyed": km.total_value or 0,
        },
    )
    response.headers["Cache-Control"] = "max-age=86400, immutable"
    return response
