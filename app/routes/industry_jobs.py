"""Active industry jobs — combined view across all user characters and their corps."""

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.models import get_db, AsyncSessionLocal, Character, StructureNameCache
from app.esi.client import ESIClient, refresh_token, get_client_safe
from app.esi import industry as esi_industry
from app.esi import universe as esi_universe
from app.sde import lookup as sde

logger = logging.getLogger(__name__)

router = APIRouter(tags=["industry-jobs"])
templates = Jinja2Templates(directory="app/templates")

CHAR_JOBS_SCOPE = "esi-industry.read_character_jobs.v1"
CORP_JOBS_SCOPE = "esi-industry.read_corporation_jobs.v1"

ACTIVITY_NAMES = {
    1: "Manufacturing",
    3: "Time Efficiency Research",
    4: "Material Efficiency Research",
    5: "Copying",
    7: "Reverse Engineering",
    8: "Invention",
    9: "Reactions",
    11: "Reactions",  # historical
}

ACTIVITY_SHORT = {
    1: "Manuf.",
    3: "TE Res.",
    4: "ME Res.",
    5: "Copy",
    7: "Reverse Eng.",
    8: "Invention",
    9: "React.",
    11: "React.",
}

# Station ID ranges — anything >= 1e12 is a player-built structure.
STATION_ID_CEILING = 10 ** 12

# NPC-corp id ceiling. Corp ids below this are always NPC corps (starter corps,
# factions, academies, etc.) — the corp-jobs endpoint will always 403 for those
# because no player holds the Director role in an NPC corp. Skip them entirely.
NPC_CORP_CEILING = 2_000_000

# Concurrency cap for per-page structure lookups.
_STRUCTURE_LOOKUP_CONCURRENCY = 5


async def _fetch_character_jobs(char: Character, include_completed: bool) -> tuple[Character, list, str | None]:
    """Fetch industry jobs for one character. Own session to avoid transaction sharing."""
    if CHAR_JOBS_SCOPE not in (char.scopes or ""):
        return char, [], "missing_scope"
    try:
        async with AsyncSessionLocal() as char_db:
            token = await refresh_token(char, char_db)
            client = ESIClient(token, db=char_db)
            jobs = await esi_industry.get_character_jobs(
                client, char.character_id, include_completed=include_completed,
            )
            return char, list(jobs or []), None
    except Exception as e:
        logger.warning("Character industry jobs fetch failed for %s: %s", char.character_id, e)
        return char, [], f"esi_error: {type(e).__name__}"


async def _fetch_corp_jobs(
    corp_id: int, corp_name: str | None, scope_chars: list[Character], include_completed: bool,
) -> tuple[int, str | None, list, str | None]:
    """Try each scope-carrying character until one succeeds (Director fallback)."""
    last_error: str | None = None
    params = {"include_completed": "true" if include_completed else "false"}
    for ch in scope_chars:
        try:
            async with AsyncSessionLocal() as char_db:
                client = await get_client_safe(ch)
                client.db = char_db
                jobs = await client.get(
                    f"/corporations/{corp_id}/industry/jobs/", params=params,
                )
                return corp_id, corp_name, list(jobs or []), None
        except Exception as e:
            err = str(e)
            last_error = err
            if "403" in err:
                continue  # try next director-privileged alt
            break
    return corp_id, corp_name, [], last_error


def _format_time_remaining(end_dt: datetime, now: datetime) -> tuple[str, str]:
    """Return (display_string, urgency_class). Urgency: 'ready' | 'soon' | 'normal'."""
    secs = (end_dt - now).total_seconds()
    if secs <= 0:
        return "Ready", "ready"
    days = int(secs // 86400)
    hours = int((secs % 86400) // 3600)
    mins = int((secs % 3600) // 60)
    if days > 0:
        text = f"{days}d {hours}h"
    elif hours > 0:
        text = f"{hours}h {mins}m"
    else:
        text = f"{mins}m"
    urgency = "soon" if secs < 3600 else "normal"
    return text, urgency


async def _resolve_installer_names(
    db: AsyncSession, installer_ids: set[int], owned_char_names: dict[int, str],
) -> dict[int, str]:
    """Resolve installer character IDs to names. Use owned chars first, then ESI."""
    resolved: dict[int, str] = {}
    unresolved: list[int] = []
    for cid in installer_ids:
        if cid in owned_char_names:
            resolved[cid] = owned_char_names[cid]
        else:
            unresolved.append(cid)
    if unresolved:
        try:
            pub = ESIClient("", db=db)
            # /universe/names accepts up to 1000 IDs at a time
            for i in range(0, len(unresolved), 1000):
                chunk = unresolved[i : i + 1000]
                data = await pub.post_public("/universe/names/", chunk)
                for entry in data or []:
                    resolved[int(entry["id"])] = entry.get("name") or f"Pilot {entry['id']}"
        except Exception as e:
            logger.info("universe/names resolution for installers failed: %s", e)
    return resolved


async def _resolve_location_names(
    db: AsyncSession,
    location_ids: set[int],
    structure_candidates: dict[int, list[Character]],
) -> dict[int, str]:
    """Resolve structure + station IDs to names.

    Order:
      1. NPC stations (id < 1e12): batch via public /universe/names.
      2. Player structures (id >= 1e12):
         - Check StructureNameCache first (shared with assets page).
         - For structures still unknown, cycle through each candidate
           character that had a job at this structure — their ESI token
           almost certainly has docking rights. get_structure() writes
           successful results back to StructureNameCache so subsequent
           page loads (and the assets page) skip the ESI hop.
    """
    if not location_ids:
        return {}
    resolved: dict[int, str] = {}

    station_ids: list[int] = []
    structure_ids: list[int] = []
    for lid in location_ids:
        if lid >= STATION_ID_CEILING:
            structure_ids.append(lid)
        else:
            station_ids.append(lid)

    # NPC stations: batch /universe/names (public, supports 1000 IDs/batch)
    if station_ids:
        try:
            pub = ESIClient("", db=db)
            for i in range(0, len(station_ids), 1000):
                chunk = station_ids[i : i + 1000]
                data = await pub.post_public("/universe/names/", chunk)
                for entry in data or []:
                    resolved[int(entry["id"])] = entry.get("name") or f"Station {entry['id']}"
        except Exception as e:
            logger.info("universe/names station resolution failed: %s", e)

    if not structure_ids:
        return resolved

    # Player structures: DB cache first
    rows = await db.execute(
        select(StructureNameCache.structure_id, StructureNameCache.name)
        .where(StructureNameCache.structure_id.in_(structure_ids))
    )
    for sid, name in rows.fetchall():
        resolved[sid] = name

    # Unknown structures: try each candidate installer's ESI client
    unknown = [sid for sid in structure_ids if sid not in resolved]
    if not unknown:
        return resolved

    sem = asyncio.Semaphore(_STRUCTURE_LOOKUP_CONCURRENCY)

    async def _lookup(struct_id: int) -> tuple[int, str | None]:
        candidates = structure_candidates.get(struct_id, [])
        async with sem:
            for ch in candidates:
                try:
                    async with AsyncSessionLocal() as s_db:
                        client = await get_client_safe(ch)
                        client.db = s_db
                        # get_structure checks the 401 cache, writes to
                        # StructureNameCache on success, and returns
                        # {"name": ..., "solar_system_id": ...}.
                        data = await esi_universe.get_structure(client, struct_id, db=s_db)
                        name = (data or {}).get("name")
                        if name and name != "Unknown Structure":
                            return struct_id, name
                except Exception as e:
                    # 401/403 from a char that doesn't have access — try the next one
                    logger.debug("Structure %s via %s failed: %s", struct_id, ch.character_name, e)
                    continue
        return struct_id, None

    results = await asyncio.gather(
        *[_lookup(sid) for sid in unknown], return_exceptions=True,
    )
    for r in results:
        if isinstance(r, Exception):
            continue
        sid, name = r
        if name:
            resolved[sid] = name

    return resolved


@router.get("/industry/jobs", response_class=HTMLResponse)
async def industry_jobs_page(
    request: Request,
    include_completed: int = Query(0),
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")

    inc_completed = bool(include_completed)

    # --- Load user characters ----------------------------------------------
    result = await db.execute(
        select(Character).where(Character.user_id == user_id)
    )
    chars = list(result.scalars().all())
    owned_char_names = {c.character_id: c.character_name for c in chars}

    # Fetch in parallel: character jobs + corp jobs.
    # Character side: any char with the character-jobs scope.
    # Corp side: any unique corp where at least one char has the corp-jobs scope;
    # the director-fallback loop inside _fetch_corp_jobs cycles through those
    # characters until one gets a 200.
    char_fetch_targets = [c for c in chars if CHAR_JOBS_SCOPE in (c.scopes or "")]

    corp_scope_by_corp: dict[int, list[Character]] = {}
    corp_names: dict[int, str] = {}
    for c in chars:
        # Skip NPC corps entirely — their corp-jobs endpoint always 403s
        # because no player holds the Director role in an NPC corp.
        if not c.corporation_id or c.corporation_id < NPC_CORP_CEILING:
            continue
        if CORP_JOBS_SCOPE in (c.scopes or ""):
            corp_scope_by_corp.setdefault(c.corporation_id, []).append(c)
            if c.corporation_name:
                corp_names[c.corporation_id] = c.corporation_name

    char_tasks = [_fetch_character_jobs(c, inc_completed) for c in char_fetch_targets]
    corp_tasks = [
        _fetch_corp_jobs(cid, corp_names.get(cid), sc, inc_completed)
        for cid, sc in corp_scope_by_corp.items()
    ]
    char_results, corp_results = await asyncio.gather(
        asyncio.gather(*char_tasks, return_exceptions=True),
        asyncio.gather(*corp_tasks, return_exceptions=True),
    )

    # --- Flatten + tag ------------------------------------------------------
    raw_rows: list[dict] = []
    warnings: list[str] = []

    # Track which owned characters "touch" each structure, so the structure
    # resolver can cycle through their ESI clients to get the name.
    char_by_id = {c.character_id: c for c in chars}
    structure_candidates: dict[int, list[Character]] = {}

    def _mark_structure_candidate(job: dict, owning_char: Character | None):
        """Remember which char could authenticate against the structure."""
        if owning_char is None:
            return
        for key in ("output_location_id", "station_id", "facility_id", "blueprint_location_id"):
            v = job.get(key)
            if v and int(v) >= STATION_ID_CEILING:
                lst = structure_candidates.setdefault(int(v), [])
                if owning_char not in lst:
                    lst.append(owning_char)

    for res in char_results:
        if isinstance(res, Exception):
            logger.warning("Character fetch exception: %s", res)
            continue
        char, jobs, err = res
        if err == "missing_scope":
            continue  # silent — user hasn't granted; shows in dashboard scope UI
        if err:
            if "403" in (err or "") or "401" in (err or ""):
                logger.info("Char %s: %s", char.character_name, err)
            else:
                warnings.append(f"{char.character_name}: {err}")
            continue
        for j in jobs:
            raw_rows.append({
                "job": j,
                "source_kind": "character",
                "source_id": char.character_id,
                "source_name": char.character_name,
            })
            _mark_structure_candidate(j, char)

    for res in corp_results:
        if isinstance(res, Exception):
            logger.warning("Corp fetch exception: %s", res)
            continue
        corp_id, corp_name, jobs, err = res
        if err:
            # 403s are routine — only surface non-auth errors in the UI
            if "403" in (err or "") or "401" in (err or ""):
                logger.info("Corp %s: %s", corp_name or corp_id, err)
            else:
                warnings.append(f"{corp_name or f'Corp {corp_id}'}: {err}")
            continue
        corp_director_candidates = corp_scope_by_corp.get(corp_id, [])
        for j in jobs:
            raw_rows.append({
                "job": j,
                "source_kind": "corporation",
                "source_id": corp_id,
                "source_name": corp_name or f"Corporation {corp_id}",
            })
            # Installer might be a user-owned char; if so prefer them for
            # structure-name resolution.  Otherwise fall back to any
            # director with the corp scope — they likely have docking.
            installer = char_by_id.get(j.get("installer_id") or 0)
            if installer is not None:
                _mark_structure_candidate(j, installer)
            else:
                for ch in corp_director_candidates:
                    _mark_structure_candidate(j, ch)

    # --- Deduplicate character-job that's also visible in a corp job feed ---
    # A job is the same if it has the same job_id across character and corp feeds.
    # Prefer the corp-source entry (it carries the most context for corp-installed jobs).
    seen_job_ids: set[int] = set()
    dedup_rows: list[dict] = []
    for r in sorted(raw_rows, key=lambda r: 0 if r["source_kind"] == "corporation" else 1):
        jid = r["job"].get("job_id")
        if jid is None or jid in seen_job_ids:
            continue
        seen_job_ids.add(jid)
        dedup_rows.append(r)

    # --- Collect IDs for batch resolution -----------------------------------
    product_ids: set[int] = set()
    blueprint_ids: set[int] = set()
    installer_ids: set[int] = set()
    location_ids: set[int] = set()
    for r in dedup_rows:
        j = r["job"]
        if j.get("product_type_id"):
            product_ids.add(int(j["product_type_id"]))
        if j.get("blueprint_type_id"):
            blueprint_ids.add(int(j["blueprint_type_id"]))
        if j.get("installer_id"):
            installer_ids.add(int(j["installer_id"]))
        for key in ("output_location_id", "station_id", "facility_id", "blueprint_location_id"):
            if j.get(key):
                location_ids.add(int(j[key]))

    type_names = await sde.type_ids_to_names(db, list(product_ids | blueprint_ids)) if (product_ids | blueprint_ids) else {}
    installer_names = await _resolve_installer_names(db, installer_ids, owned_char_names)
    location_names = await _resolve_location_names(db, location_ids, structure_candidates)

    # --- Build rendered rows ------------------------------------------------
    now = datetime.now(timezone.utc)
    rows: list[dict] = []
    counts_by_activity: dict[str, int] = {}
    counts_by_source: dict[str, int] = {"character": 0, "corporation": 0}
    counts_by_status: dict[str, int] = {}

    for r in dedup_rows:
        j = r["job"]
        activity_id = j.get("activity_id", 0)
        activity_label = ACTIVITY_NAMES.get(activity_id, f"Activity {activity_id}")
        activity_short = ACTIVITY_SHORT.get(activity_id, activity_label)

        product_id = j.get("product_type_id") or j.get("blueprint_type_id")
        product_name = type_names.get(int(product_id)) if product_id else None
        if product_name is None:
            product_name = f"Type {product_id}" if product_id else "—"

        bp_id = j.get("blueprint_type_id")
        blueprint_name = type_names.get(int(bp_id)) if bp_id else None

        installer_id = j.get("installer_id")
        installer_name = installer_names.get(int(installer_id)) if installer_id else None

        location_id = (
            j.get("output_location_id") or j.get("station_id")
            or j.get("facility_id") or j.get("blueprint_location_id")
        )
        location_name = location_names.get(int(location_id)) if location_id else None
        if location_name is None and location_id:
            kind = "Structure" if location_id >= STATION_ID_CEILING else "Station"
            location_name = f"{kind} {location_id}"

        end_str = j.get("end_date")
        time_remaining = "—"
        urgency = "normal"
        end_dt = None
        if end_str:
            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                time_remaining, urgency = _format_time_remaining(end_dt, now)
            except Exception:
                pass

        raw_status = j.get("status", "active")
        # Override status → "ready" when active but past end_date
        display_status = raw_status
        if raw_status == "active" and end_dt and end_dt <= now:
            display_status = "ready"

        rows.append({
            "job_id": j.get("job_id"),
            "activity_id": activity_id,
            "activity_label": activity_label,
            "activity_short": activity_short,
            "product_id": product_id,
            "product_name": product_name,
            "blueprint_name": blueprint_name,
            "runs": j.get("runs", 1),
            "installer_id": installer_id,
            "installer_name": installer_name,
            "location_id": location_id,
            "location_name": location_name,
            "source_kind": r["source_kind"],
            "source_id": r["source_id"],
            "source_name": r["source_name"],
            "time_remaining": time_remaining,
            "urgency": urgency,
            "end_iso": end_str,
            "end_sort": end_dt.timestamp() if end_dt else float("inf"),
            "status": display_status,
        })
        counts_by_activity[activity_label] = counts_by_activity.get(activity_label, 0) + 1
        counts_by_source[r["source_kind"]] = counts_by_source.get(r["source_kind"], 0) + 1
        counts_by_status[display_status] = counts_by_status.get(display_status, 0) + 1

    rows.sort(key=lambda r: r["end_sort"])

    # Build distinct installer lists for the dropdown filters.  Each entry
    # includes how many jobs it owns so the UI can show "(n)" next to it.
    char_counts: dict[int, int] = {}
    char_labels: dict[int, str] = {}
    corp_counts: dict[int, int] = {}
    corp_labels: dict[int, str] = {}
    for r in rows:
        if r["source_kind"] == "character":
            char_counts[r["source_id"]] = char_counts.get(r["source_id"], 0) + 1
            char_labels[r["source_id"]] = r["source_name"]
        else:
            corp_counts[r["source_id"]] = corp_counts.get(r["source_id"], 0) + 1
            corp_labels[r["source_id"]] = r["source_name"]

    character_filters = sorted(
        [{"id": cid, "name": char_labels[cid], "count": char_counts[cid]} for cid in char_counts],
        key=lambda x: x["name"].lower(),
    )
    corp_filters = sorted(
        [{"id": cid, "name": corp_labels[cid], "count": corp_counts[cid]} for cid in corp_counts],
        key=lambda x: x["name"].lower(),
    )

    return templates.TemplateResponse("industry_jobs.html", {
        "request": request,
        "rows": rows,
        "counts_by_activity": sorted(counts_by_activity.items(), key=lambda kv: -kv[1]),
        "counts_by_source": counts_by_source,
        "counts_by_status": counts_by_status,
        "character_filters": character_filters,
        "corp_filters": corp_filters,
        "total": len(rows),
        "include_completed": inc_completed,
        "char_count_with_scope": len(char_fetch_targets),
        "corp_count_with_scope": len(corp_scope_by_corp),
    })
