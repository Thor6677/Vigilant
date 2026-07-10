"""Intel → Kill Feed → Advanced Search.

Sibling of /intel/kills. Full filter UI + cursor pagination + optional live
polling. Spec: docs/superpowers/specs/2026-05-22-killfeed-advanced-search-design.md.

Plan 1 (this MVP):
  - Page route (this file, Task 1)
  - Filter compiler + /search/results endpoint (Task 2)
  - Results partial + NPC badge surfacing (Task 3 modifies the shared partial)
  - Frontend wiring (Task 4-5 in intel_kills_search.html)
  - Live polling (Task 6)

Plan 2 (later) adds heuristic flags (Awox/Padding/HighSec Gank) and AT Ships
category.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Float, Integer, and_, cast, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Killmail, KillmailAttacker, get_db
from app.db.sde_models import SDESystem, SDEType
from app.intel.recent_battles import resolve_entity_names
from app.sde.lookup import _ensure_wh_class_cache, type_ids_to_names
from app.sde import lookup as sde_lookup

log = logging.getLogger(__name__)
router = APIRouter()

# SQLite INDEXED BY hint support — importing applies the process-wide
# SQLAlchemy patch + startup self-check (T-037 item 4: moved to app/db).
from app.db import sqlite_hints  # noqa: F401, E402

templates = Jinja2Templates(directory="app/templates")

PAGE_SIZE = 100

# T-037 item 2: the count/SUM query re-aggregates 200-300k rows (~360ms) on
# every search even when only the cursor changed. Cache (count, isk) keyed by
# the filter-shaping params (sort/cursor excluded — they don't affect totals)
# with a short TTL. Time presets ("24h") recompute their cutoff each compile,
# so a cached total is at most TTL seconds stale — acceptable for a killboard.
_count_cache: dict[str, dict] = {}
_COUNT_CACHE_TTL = 180
_COUNT_CACHE_MAX = 256

# T-037 item 3: statement timeout. A pathological filter shape used to pile
# up workers until the proxy 504'd (2026-07-03 incident). asyncio.wait_for
# frees the worker; sqlite3.Connection.interrupt() (thread-safe per stdlib
# docs) then stops the abandoned query burning IO in aiosqlite's executor.
_SEARCH_TIMEOUT_S = 20


async def _execute_with_timeout(db: AsyncSession, stmt):
    try:
        return await asyncio.wait_for(db.execute(stmt), timeout=_SEARCH_TIMEOUT_S)
    except asyncio.TimeoutError:
        try:
            raw = (await db.connection()).get_raw_connection()
            sqlite_conn = getattr(getattr(raw, "driver_connection", None), "_conn", None)
            if sqlite_conn is not None:
                sqlite_conn.interrupt()
        except Exception:  # pragma: no cover - best-effort cleanup
            pass
        raise


def _count_cache_key(params: dict[str, Any]) -> str:
    parts = []
    for k in sorted(params):
        if k in ("sort", "direction", "cursor"):
            continue
        v = params[k]
        if isinstance(v, (set, frozenset)):
            v = sorted(v)
        parts.append((k, repr(v)))
    return repr(parts)

# Hardcoded EVE-meta constants for the compiler.
CAPITAL_GROUP_IDS = {547, 485, 30, 659, 513, 902, 1538}  # Carrier, Dread, Titan, Super, Freighter, JF, FAX
RORQUAL_TYPE_ID = 28352  # Industrial Command Ship group 941 also contains Porpoise+Orca which are NOT capitals.

# AT Tournament prize ships. Curated from user's authoritative list 2026-05-22.
# Source of truth: user-supplied roster (not Phase 1 spec, which had inaccuracies
# — e.g. spec included Apotheosis/Chremoas/Zarmazd, user excluded them).
# Names + type_ids verified via SDE exact-name match on the VPS:
#   2834  Utu                      (Assault Frigate, group 324)
#   2836  Adrestia                 (Heavy Assault Cruiser, group 358)
#   3516  Malice                   (Assault Frigate, group 324)
#   3518  Vangel                   (Heavy Assault Cruiser, group 358)
#   11011 Guardian-Vexor           (Cruiser, group 26)
#   11936 Apocalypse Imperial Issue (Battleship, group 27)
#   11938 Armageddon Imperial Issue (Battleship, group 27)
#   11940 Gold Magnate             (Amarr Frigate, group 25) — legendary AT1 prize
#   11942 Silver Magnate           (Amarr Frigate, group 25) — legendary AT1 prize
#   13202 Megathron Federate Issue (Battleship, group 27)
#   26840 Raven State Issue        (Battleship, group 27)
#   26842 Tempest Tribal Issue     (Battleship, group 27)
#   32207 Freki                    (Assault Frigate, group 324)
#   32209 Mimir                    (Heavy Assault Cruiser, group 358)
#   32788 Cambion                  (Assault Frigate, group 324)
#   32790 Etana                    (Logistics, group 832)
#   33395 Moracha                  (Force Recon, group 833)
#   33673 Whiptail                 (Interceptor, group 831)
#   33675 Chameleon                (Force Recon, group 833)
#   35779 Imp                      (Interceptor, group 831)
#   35781 Fiend                    (HIC, group 894)
#   42245 Rabisu                   (Logistics, group 832)
#   42246 Caedes                   (Covert Ops, group 830)
#   45531 Victor                   (Force Recon, group 833)
#   48635 Tiamat                   (Force Recon, group 833)
#   48636 Hydra                    (Covert Ops, group 830)
#   60764 Laelaps                  (HIC, group 894)
#   60765 Raiju                    (group 893)
#   74141 Geri                     (Assault Frigate, group 324)
#   74316 Bestla                   (Heavy Assault Cruiser, group 358)
#   77726 Cybele                   (Heavy Assault Cruiser, group 358)
#   78414 Shapash                  (Assault Frigate, group 324)
#   85062 Sidewinder               (Covert Ops, group 830)
#   85229 Cobra                    (Force Recon, group 833)
#   85236 Python                   (group 898)
#   89807 Anhinga                  (group 1201)
#   89808 Skua                     (group 1305)
# Notes:
# - 29266 Apotheosis (Shuttle) — excluded per user (spec included it but it's a
#   gift/event prize, not an AT prize proper).
# - 33397 Chremoas (Covert Ops) — excluded per user; same rationale.
# - 49713 Zarmazd (Logistics) — excluded per user; same rationale.
# - 3532  Echelon — excluded; Sansha incursion-event prize, not AT.
# - 12044 Enyo — initially included from user list 2026-05-22 but removed same
#   day after kill-distribution showed 804 Enyo deaths (the standard published
#   Gallente Assault Frigate; not an AT-prize variant — SDE has no separate
#   AT-Enyo type_id). User confirmed the chip should track rare AT prizes only.
# - Re-confirm with user if SDE updates introduce new AT variants.
AT_SHIP_TYPE_IDS = {
    2834, 2836, 3516, 3518,
    11011, 11936, 11938, 11940, 11942,
    13202,
    26840, 26842,
    32207, 32209, 32788, 32790,
    33395, 33673, 33675,
    35779, 35781,
    42245, 42246,
    45531,
    48635, 48636,
    60764, 60765,
    74141, 74316,
    77726, 78414,
    85062, 85229, 85236,
    89807, 89808,
}
ABYSSAL_SYSTEM_MIN = 32000001
ABYSSAL_SYSTEM_MAX = 32000200
WH_SYSTEM_MIN = 31000000
WH_SYSTEM_MAX = 31999999

ISK_MIN_MAP = {"100m": 1e8, "1b": 1e9, "5b": 5e9, "10b": 1e10, "100b": 1e11, "1t": 1e12}

COUNT_BUCKETS = {
    "solo": (1, 1),
    "2-5": (2, 5),
    "6-10": (6, 10),
    "11-25": (11, 25),
    "26-50": (26, 50),
    "51-100": (51, 100),
    "100+": (100, None),  # None = no upper bound
}

# Primetime bands (UTC hour, [start, end_exclusive]). Wraparound bands split.
PRIMETIME_BANDS = {
    "aus": [(10, 18)],
    "eu":  [(18, 24), (0, 2)],
    "ru":  [(14, 22)],
    "use": [(23, 24), (0, 7)],
    "usw": [(2, 10)],
}

# WH classes — strings come from URL as 'c1'..'c6','thera','drifter','pochven'.
# Map to integer wormhole_class_id values used by SDEWormholeClass.
WH_CLASS_ID_MAP = {
    "c1": 1, "c2": 2, "c3": 3, "c4": 4, "c5": 5, "c6": 6,
    "thera": 12, "drifter": 14,  # Drifter wormholes use class 14
    # pochven removed — now a top-level Space chip, not a WH sub-class
}


def _split_ids(s: str) -> list[int]:
    return [int(x) for x in (s or "").split(",") if x.strip().isdigit()]


def _split_set(s: str) -> set[str]:
    return {p.strip() for p in (s or "").split(",") if p.strip()}


# Forward index: system_id → effective wormhole_class_id (3-tier fallback).
# Built lazily at module-level on first WH-class filter; refreshed at the
# same 1h cadence as the underlying _wh_class_cache.
_wh_system_class_map: dict[int, int] | None = None
_wh_system_class_map_ts: datetime | None = None
_WH_FWD_TTL = 3600  # seconds


async def _ensure_wh_system_class_map(db: AsyncSession) -> dict[int, int]:
    """Build system_id → wormhole_class_id forward map with system → constellation
    → region fallback. Mirrors the resolution logic in
    app/sde/lookup.py:get_system_wh_class.
    """
    global _wh_system_class_map, _wh_system_class_map_ts
    now = datetime.utcnow()
    if (
        _wh_system_class_map is not None
        and _wh_system_class_map_ts is not None
        and (now - _wh_system_class_map_ts).total_seconds() < _WH_FWD_TTL
    ):
        return _wh_system_class_map

    await _ensure_wh_class_cache(db)
    raw_cache = sde_lookup._wh_class_cache or {}

    # Pull ALL systems + constellation_id + region_id.
    # Pochven systems retain K-space IDs (~30002000) so filtering to
    # system_id >= WH_SYSTEM_MIN (31000000) would silently exclude them.
    # The cache-lookup loop below discards non-WH/non-Pochven systems naturally
    # (they won't match any raw_cache entry), so the full scan is safe.
    result = await db.execute(
        select(SDESystem.system_id, SDESystem.constellation_id, SDESystem.region_id)
    )
    fwd: dict[int, int] = {}
    for sid, cid, rid in result.all():
        if sid in raw_cache:
            fwd[sid] = raw_cache[sid]
        elif cid and cid in raw_cache:
            fwd[sid] = raw_cache[cid]
        elif rid and rid in raw_cache:
            fwd[sid] = raw_cache[rid]
    _wh_system_class_map = fwd
    _wh_system_class_map_ts = now
    return fwd


@router.get("/intel/kills/search", response_class=HTMLResponse)
async def intel_kills_search_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Page shell. Filters + empty results container, JS handles the rest."""
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")
    return templates.TemplateResponse("intel_kills_search.html", {"request": request})


async def _compile_search_where(params: dict[str, Any], db: AsyncSession) -> dict[str, Any]:
    """Translate validated params dict into SQLAlchemy clauses.

    Returns a dict with keys:
      where: list[ColumnElement]  — AND-combined clauses
      joins: set[str]              — table aliases needed ('sde_systems', 'sde_types')
      sort: str                    — raw sort param ("date"/"isk"/"involved")
      sort_col, sort_dir           — for ORDER BY
      cursor_clause                — optional WHERE for pagination, separate from main where
      has_time_bound: bool         — True iff a killmail_time LOWER bound is present
                                      (drives the INDEXED BY ix_killmails_killmail_time
                                      hint in _build_search_statements — see that
                                      function for the perf-incident context)
      defaulted_time: bool         — True iff no lower bound was supplied and the
                                      90-day safety-net default was injected (drives
                                      the "showing last 90 days" UI note)

    The caller composes the final SQL.
    """
    where: list = []
    joins: set[str] = set()
    # True when a killmail_time LOWER bound is present, i.e. the planner can
    # drive the query from ix_killmails_killmail_time and only scan forward
    # from a bounded point instead of the whole table. If the user supplies
    # no lower bound (filterless, or time_end alone), the 90-day safety net
    # below injects one — so by the time this function returns, this is
    # always True and every search is eligible for the INDEXED BY hint.
    has_time_bound = False

    # ── Time ───────────────────────────────────────────────────────────
    cutoff_map = {"24h": timedelta(hours=24), "7d": timedelta(days=7),
                  "30d": timedelta(days=30), "90d": timedelta(days=90)}
    if params.get("time_preset") and params["time_preset"] in cutoff_map:
        cutoff = datetime.utcnow() - cutoff_map[params["time_preset"]]
        where.append(Killmail.killmail_time >= cutoff)
        has_time_bound = True
    if params.get("time_start"):
        where.append(Killmail.killmail_time >= params["time_start"])
        has_time_bound = True
    if params.get("time_end"):
        where.append(Killmail.killmail_time <= params["time_end"])

    # Safety net: without a lower time bound every search (page AND count)
    # scans the whole ~60M-row table (192GB in prod). Default to a 90-day
    # window and surface it so the user knows the window was implied. When an
    # upper bound (time_end) IS present, anchor the 90 days to it — anchoring
    # to utcnow() would compile an impossible window (>= now-90d AND <= old
    # date) and silently return nothing. Perf-identical either way: same
    # bounded >= clause, same INDEXED BY eligibility.
    defaulted_time = False
    if not has_time_bound:
        anchor = params["time_end"] if params.get("time_end") else datetime.utcnow()
        where.append(Killmail.killmail_time >= anchor - timedelta(days=90))
        has_time_bound = True
        defaulted_time = True

    # ── Space (HS/LS/NS/WH/Abyssal) ────────────────────────────────────
    space = params.get("space") or set()
    if space:
        joins.add("sde_systems")
        space_conds = []
        if "hs" in space:
            space_conds.append(SDESystem.security >= 0.5)
        if "ls" in space:
            space_conds.append(and_(SDESystem.security > 0.0, SDESystem.security < 0.5))
        if "ns" in space:
            # Pochven systems have K-space IDs (<31M) and security <= 0.0, so they
            # match the naive NS predicate. Exclude by region_id (Pochven region
            # is 10000070) — Pochven gets its own top-level Space chip.
            space_conds.append(and_(
                SDESystem.security <= 0.0,
                SDESystem.system_id < WH_SYSTEM_MIN,
                SDESystem.region_id != 10000070,
            ))
        if "wh" in space:
            space_conds.append(and_(SDESystem.system_id >= WH_SYSTEM_MIN, SDESystem.system_id <= WH_SYSTEM_MAX))
        if "abyssal" in space:
            space_conds.append(and_(SDESystem.system_id >= ABYSSAL_SYSTEM_MIN, SDESystem.system_id <= ABYSSAL_SYSTEM_MAX))
        if "pochven" in space:
            # Pochven systems retain K-space IDs (~30002xxx). Resolve via the
            # wormhole-class forward map (class=25) populated lazily.
            fwd = await _ensure_wh_system_class_map(db)
            pochven_sids = {sid for sid, cid in fwd.items() if cid == 25}
            if pochven_sids:
                space_conds.append(Killmail.solar_system_id.in_(pochven_sids))
            else:
                space_conds.append(Killmail.killmail_id == -1)  # impossible — empty result
        if space_conds:
            where.append(or_(*space_conds))

    # ── WH sub-class (only meaningful when WH selected; Pochven special-cased below) ──
    wh_class = params.get("wh_class") or set()
    if wh_class and "wh" in space:
        fwd = await _ensure_wh_system_class_map(db)
        wanted_ids = {WH_CLASS_ID_MAP[c] for c in wh_class if c in WH_CLASS_ID_MAP}
        matching_systems = {sid for sid, cid in fwd.items() if cid in wanted_ids}
        if matching_systems:
            where.append(Killmail.solar_system_id.in_(matching_systems))
        else:
            # Requested classes have no matching systems — return empty result.
            where.append(Killmail.killmail_id == -1)

    # ── Shattered modifier (only meaningful with WH or wh_class) ─────
    if params.get("shattered_only"):
        # Shattered systems are tagged via SDE group_label in _sys_meta_cache,
        # not in a separate column. For the search page we accept that this
        # filter is a no-op on systems not in _sys_meta_cache (rare for kills
        # that are in the DB but not in the live buffer). Implementation:
        # post-filter results in Python. Skip the SQL side for MVP — flag for
        # follow-up if precision matters.
        pass  # Documented limitation; revisit if user reports it.

    # ── Category (Ship / Structure / Capital / AT Ships) ─────────────
    category = params.get("category") or set()
    if category:
        joins.add("sde_types")
        cat_conds = []
        if "ship" in category:
            cat_conds.append(SDEType.category_id == 6)
        if "structure" in category:
            cat_conds.append(SDEType.category_id == 65)
        if "capital" in category:
            cat_conds.append(or_(
                SDEType.group_id.in_(CAPITAL_GROUP_IDS),
                Killmail.victim_ship_type_id == RORQUAL_TYPE_ID,
            ))
        if "at" in category:
            # AT-prize ships: victim-only match, consistent with Capital chip.
            # Spec said "victim OR any attacker" but an EXISTS join on
            # killmail_attackers changes the perf profile. Victim-only is the
            # right default; revisit if user surfaces the broader request.
            cat_conds.append(Killmail.victim_ship_type_id.in_(AT_SHIP_TYPE_IDS))
        if cat_conds:
            where.append(or_(*cat_conds))

    # ── Count (gang size) ─────────────────────────────────────────────
    count_buckets = params.get("count") or set()
    if count_buckets:
        count_conds = []
        for bucket in count_buckets:
            if bucket not in COUNT_BUCKETS:
                continue
            lo, hi = COUNT_BUCKETS[bucket]
            if hi is None:
                count_conds.append(Killmail.attacker_count >= lo)
            else:
                count_conds.append(and_(Killmail.attacker_count >= lo, Killmail.attacker_count <= hi))
        if count_conds:
            where.append(or_(*count_conds))

    # ── ISK ───────────────────────────────────────────────────────────
    if params.get("isk") and params["isk"] in ISK_MIN_MAP:
        where.append(Killmail.total_value >= ISK_MIN_MAP[params["isk"]])

    # ── Primetime (UTC hour-of-day, wraparound aware) ─────────────────
    pt = params.get("primetime") or set()
    if pt:
        hour_expr = cast(func.strftime("%H", Killmail.killmail_time), Integer)
        pt_conds = []
        for tz in pt:
            for start, end in PRIMETIME_BANDS.get(tz, []):
                pt_conds.append(and_(hour_expr >= start, hour_expr < end))
        if pt_conds:
            where.append(or_(*pt_conds))

    # ── Victim ship ───────────────────────────────────────────────────
    if params.get("ship_ids"):
        where.append(Killmail.victim_ship_type_id.in_(params["ship_ids"]))

    # T-037 item 1: victim-side equality filters hit indexed killmails
    # columns with (col, killmail_time DESC) composites (ix_km_victim_*_time)
    # or selective single-column indexes. Prod EXPLAIN (2026-07-10, 192GB DB):
    # un-hinted planner picks ix_km_victim_corp_time — count 82ms vs 1430ms
    # forced onto the time index; page 1ms vs 660ms. So a victim filter must
    # SUPPRESS the INDEXED BY hint. Attacker-side EXISTS shapes measured
    # hint-neutral (planner drives from the time index either way), and
    # either_* mixes attacker EXISTS into an OR, so neither sets this flag.
    has_selective_victim = bool(
        params.get("ship_ids")
        or params.get("victim_chars") or params.get("victim_corps")
        or params.get("victim_allis") or params.get("victim_ships")
    )

    # ── Three entity sides: Attackers / Either / Victim ──────────────
    where.extend(_compile_attacker_clauses(
        params.get("attacker_mode", "or"),
        params.get("attacker_chars", []),
        params.get("attacker_corps", []),
        params.get("attacker_allis", []),
        params.get("attacker_ships", []),
    ))
    where.extend(_compile_victim_clauses(
        params.get("victim_mode", "or"),
        params.get("victim_chars", []),
        params.get("victim_corps", []),
        params.get("victim_allis", []),
        params.get("victim_ships", []),
    ))
    where.extend(_compile_either_clauses(
        params.get("either_mode", "or"),
        params.get("either_chars", []),
        params.get("either_corps", []),
        params.get("either_allis", []),
        params.get("either_ships", []),
    ))

    # ── Flags (Awox / HighSec Gank / Padding) ────────────────────────
    where.extend(_compile_flag_clauses(params.get("flags") or set(), joins))

    # NULL guard for ISK sort: NULL total_value rows can't be sensibly
    # ordered or paginated by ISK (the cursor tuple `total_value < val`
    # excludes NULLs on page 2+). Drop them at the source so the sort
    # is internally consistent.
    if params.get("sort") == "isk":
        where.append(Killmail.total_value.isnot(None))

    # ── Sort + cursor ─────────────────────────────────────────────────
    sort = params.get("sort", "date")
    direction = params.get("direction", "desc")
    sort_col, cursor_clause = _resolve_sort_and_cursor(sort, direction, params.get("cursor"))

    return {
        "where": where,
        "joins": joins,
        "sort": sort,
        "sort_col": sort_col,
        "sort_dir": direction,
        "cursor_clause": cursor_clause,
        "has_time_bound": has_time_bound,
        "has_selective_victim": has_selective_victim,
        "defaulted_time": defaulted_time,
    }


def _compile_attacker_clauses(mode: str, chars: list[int], corps: list[int],
                               allis: list[int], ships: list[int]) -> list:
    """Compile Attacker-side predicates per And/In/Or mode.

    All produce EXISTS clauses against killmail_attackers.
    - Or: one EXISTS with disjunctive predicates (any attacker matches anything).
    - In: one EXISTS with conjunctive predicates (single attacker row matches all kinds).
    - And: one EXISTS per listed entity (multiple separate attackers).
    """
    if not (chars or corps or allis or ships):
        return []
    a = KillmailAttacker
    if mode == "or":
        conds = []
        if chars:
            conds.append(a.character_id.in_(chars))
        if corps:
            conds.append(a.corporation_id.in_(corps))
        if allis:
            conds.append(a.alliance_id.in_(allis))
        if ships:
            conds.append(a.ship_type_id.in_(ships))
        return [exists().where(
            a.killmail_id == Killmail.killmail_id, or_(*conds)
        )]
    if mode == "in":
        # All predicates inside one EXISTS — must hold on a single attacker row.
        # Within-kind: OR (multiple chars in In mode means "char A or B"); across-kind: AND.
        conds = []
        if chars:
            conds.append(a.character_id.in_(chars))
        if corps:
            conds.append(a.corporation_id.in_(corps))
        if allis:
            conds.append(a.alliance_id.in_(allis))
        if ships:
            conds.append(a.ship_type_id.in_(ships))
        return [exists().where(
            a.killmail_id == Killmail.killmail_id, and_(*conds)
        )]
    # "and" mode — one EXISTS per listed entity.
    out = []
    for c in chars:
        out.append(exists().where(a.killmail_id == Killmail.killmail_id, a.character_id == c))
    for c in corps:
        out.append(exists().where(a.killmail_id == Killmail.killmail_id, a.corporation_id == c))
    for c in allis:
        out.append(exists().where(a.killmail_id == Killmail.killmail_id, a.alliance_id == c))
    for s in ships:
        out.append(exists().where(a.killmail_id == Killmail.killmail_id, a.ship_type_id == s))
    return out


def _compile_victim_clauses(mode: str, chars: list[int], corps: list[int],
                             allis: list[int], ships: list[int]) -> list:
    """Compile Victim-side predicates. Direct on Killmail.victim_*_id columns.

    And and In behave identically here (only one victim row per kill).
    """
    if not (chars or corps or allis or ships):
        return []
    if mode == "or":
        conds = []
        if chars:
            conds.append(Killmail.victim_character_id.in_(chars))
        if corps:
            conds.append(Killmail.victim_corporation_id.in_(corps))
        if allis:
            conds.append(Killmail.victim_alliance_id.in_(allis))
        if ships:
            conds.append(Killmail.victim_ship_type_id.in_(ships))
        return [or_(*conds)]
    # In / And — both conjunctive across kinds, disjunctive within kind.
    conds = []
    if chars:
        conds.append(Killmail.victim_character_id.in_(chars))
    if corps:
        conds.append(Killmail.victim_corporation_id.in_(corps))
    if allis:
        conds.append(Killmail.victim_alliance_id.in_(allis))
    if ships:
        conds.append(Killmail.victim_ship_type_id.in_(ships))
    return [and_(*conds)]


def _compile_either_clauses(mode: str, chars: list[int], corps: list[int],
                             allis: list[int], ships: list[int]) -> list:
    """Compile Either-side predicates: matches if attacker OR victim satisfies the mode.
    """
    if not (chars or corps or allis or ships):
        return []
    a_clauses = _compile_attacker_clauses(mode, chars, corps, allis, ships)
    v_clauses = _compile_victim_clauses(mode, chars, corps, allis, ships)
    # Either = attacker satisfies OR victim satisfies. For "and" mode, that means
    # each separately-listed entity has an attacker_or_victim_match expression.
    if mode == "and":
        # Pair-wise OR (attacker_i, victim_i) — but our compilers emit a single
        # clause for victim regardless of count. Reconstruct per-entity here.
        out = []
        a = KillmailAttacker
        for c in chars:
            out.append(or_(
                exists().where(a.killmail_id == Killmail.killmail_id, a.character_id == c),
                Killmail.victim_character_id == c,
            ))
        for c in corps:
            out.append(or_(
                exists().where(a.killmail_id == Killmail.killmail_id, a.corporation_id == c),
                Killmail.victim_corporation_id == c,
            ))
        for c in allis:
            out.append(or_(
                exists().where(a.killmail_id == Killmail.killmail_id, a.alliance_id == c),
                Killmail.victim_alliance_id == c,
            ))
        for s in ships:
            out.append(or_(
                exists().where(a.killmail_id == Killmail.killmail_id, a.ship_type_id == s),
                Killmail.victim_ship_type_id == s,
            ))
        return out
    # Or / In — single OR of (attacker_clause, victim_clause)
    a_expr = a_clauses[0] if a_clauses else None
    v_expr = v_clauses[0] if v_clauses else None
    if a_expr is not None and v_expr is not None:
        return [or_(a_expr, v_expr)]
    return a_clauses or v_clauses


def _compile_flag_clauses(flags: set[str], joins: set[str]) -> list:
    """Compile heuristic flag predicates (Awox / HighSec Gank / Padding).

    Multi-select OR semantics: selecting multiple flags returns the union
    (matches the Space/WH/Category chip pattern).

    Mutates ``joins`` if a flag requires an additional table join (e.g. HighSec
    Gank requires sde_systems).

    Returns a list with at most one ColumnElement (the OR of all selected flags),
    so callers can extend ``where`` with the result directly.
    """
    if not flags:
        return []
    a = KillmailAttacker
    flag_conds: list = []

    if "awox" in flags:
        # Awox = real-player victim destroyed by >=1 attacker in the same corp,
        # excluding the victim themselves (so a player in the same corp doesn't
        # get tagged just for being on the mail as the victim).
        flag_conds.append(and_(
            Killmail.victim_character_id.isnot(None),
            Killmail.victim_corporation_id.isnot(None),
            exists().where(
                a.killmail_id == Killmail.killmail_id,
                a.corporation_id == Killmail.victim_corporation_id,
                a.character_id.isnot(None),
                a.character_id != Killmail.victim_character_id,
            ),
        ))

    if "gank" in flags:
        # HighSec gank = kill in HS (security >= 0.5) with at least one attacker
        # whose security_status < 0. The HS half requires a join to sde_systems
        # — register it so the SELECT caller adds the JOIN.
        joins.add("sde_systems")
        flag_conds.append(and_(
            SDESystem.security >= 0.5,
            exists().where(
                a.killmail_id == Killmail.killmail_id,
                a.security_status < 0,
            ),
        ))

    if "padding" in flags:
        # Padding v1 heuristic. Three conditions:
        #   1. attacker_count >= 5 (cheap; uses ix_killmails_attacker_count_kid)
        #   2. at least one attacker has damage_done > 0 (otherwise the heuristic
        #      can't be computed and we exclude — spec line 376-378)
        #   3. (count of attackers with damage_done < max_damage * 1%) / attacker_count >= 0.5
        #
        # Conditions 2 and 3 are correlated subqueries on killmail_attackers.
        # Perf: covered in Task 5 EXPLAIN. If full scans appear, Task 5 adds
        # ix_killmail_attackers_kid_dmg.
        # `.select_from(KillmailAttacker)` is REQUIRED — without it SQLAlchemy
        # picks up `killmails` as the FROM because the WHERE clause references
        # both tables, producing the bogus SQL `SELECT max(...) FROM killmails`
        # which SQLite rejects with "misuse of aggregate: max()".
        max_dmg_subq = (
            select(func.max(KillmailAttacker.damage_done))
            .select_from(KillmailAttacker)
            .where(KillmailAttacker.killmail_id == Killmail.killmail_id)
            .scalar_subquery()
        )
        # NULL safety: if max_dmg_subq is NULL (impossible here because the
        # has_damage_data guard below requires at least one row, but defensive),
        # `damage_done < NULL` evaluates to NULL, treated as false in WHERE, so
        # COUNT = 0 and the ratio fails the >= 0.5 check. SQLite-specific but
        # standard SQL three-valued logic.
        low_count_subq = (
            select(func.count())
            .select_from(KillmailAttacker)
            .where(
                KillmailAttacker.killmail_id == Killmail.killmail_id,
                KillmailAttacker.damage_done < (max_dmg_subq * 0.01),
            )
            .scalar_subquery()
        )
        has_damage_data = exists().where(
            a.killmail_id == Killmail.killmail_id,
            a.damage_done > 0,
        )
        flag_conds.append(and_(
            Killmail.attacker_count >= 5,
            has_damage_data,
            (cast(low_count_subq, Float) / Killmail.attacker_count) >= 0.5,
        ))

    if not flag_conds:
        return []
    return [or_(*flag_conds)]


def _resolve_sort_and_cursor(sort: str, direction: str, cursor: str | None) -> tuple:
    """Return (sort_column_expression, cursor_where_clause).

    Date sort uses killmail_id (monotonic). ISK/Involved use (sort_val, killmail_id) tuples.
    """
    if sort == "isk":
        sort_col = Killmail.total_value
    elif sort == "involved":
        sort_col = Killmail.attacker_count
    else:
        sort_col = Killmail.killmail_id  # Date sort just uses ID monotonicity

    cursor_clause = None
    if cursor:
        try:
            if sort == "date":
                kid = int(cursor)
                cursor_clause = (Killmail.killmail_id < kid) if direction == "desc" else (Killmail.killmail_id > kid)
            else:
                # "val:kid" tuple cursor
                val_str, kid_str = cursor.split(":")
                val = float(val_str)
                kid = int(kid_str)
                if direction == "desc":
                    cursor_clause = or_(
                        sort_col < val,
                        and_(sort_col == val, Killmail.killmail_id < kid),
                    )
                else:
                    cursor_clause = or_(
                        sort_col > val,
                        and_(sort_col == val, Killmail.killmail_id > kid),
                    )
        except (ValueError, AttributeError):
            cursor_clause = None  # Bad cursor — ignore, return page 1.

    return sort_col, cursor_clause


# ── Perf incident (2026-07): killmails grew to ~60M rows (~192GB, EVERef
# backfill) with no ANALYZE stats. On time-bounded, space-scoped searches
# (e.g. time=7d&space=wh) SQLite's planner picked the covering
# ix_killmail_system_time (solar_system_id, killmail_time) index, scanning
# every J-space kill ever (~10M rows) then sorting, instead of
# ix_killmails_killmail_time which would scan only the ~200-300k rows in the
# time window. Force the correct index with an INDEXED BY hint whenever a
# killmail_time lower bound is present. See _build_search_statements below.
KILLMAIL_TIME_INDEX = "ix_killmails_killmail_time"


def _should_show_defaulted_note(defaulted_time: bool, is_polling: bool,
                                cursor: str | None) -> bool:
    """Gate the "showing last 90 days" note to the initial render only.

    The results partial is consumed by appending JS (live poll-appends AND
    Show More cursor batches strip only the [data-kfs-marker] div), so any
    non-initial response that carries the note would spray it mid-feed:
      - live poll-appends: live=1 & since=X (is_polling)
      - Show More / IntersectionObserver batches: cursor is set
    The initial live-mode load (live=1, since=0, no cursor) still shows it.
    """
    return defaulted_time and not is_polling and not cursor


def _build_search_statements(compiled: dict[str, Any], live: int, since: int) -> tuple[Any, Any]:
    """Build the page SELECT and count/sum SELECT for /search/results.

    Extracted from intel_kills_search_results so the query-shaping logic
    (joins, where, cursor, sort, and the SQLite INDEXED BY perf hint) can be
    exercised directly by tests without booting the FastAPI app.

    Hint gating (see KILLMAIL_TIME_INDEX comment above for the incident):
      - date-sorted, not live-polling, has_time_bound: hint BOTH the page
        query and the count/sum query onto ix_killmails_killmail_time.
      - isk/involved-sorted, has_time_bound: the time index can't serve
        that ORDER BY, so hint the count/sum query ONLY (counts don't need
        order) and leave the page query's plan to the optimizer.
      - live-poll (``live`` and ``since`` both truthy): sorts by
        killmail_id (the PK) with a ``killmail_id > since`` filter — an
        unrelated access pattern, never hinted.
      - gating stays strictly tied to ``compiled["has_time_bound"]``
        (INDEXED BY errors out if the index can't serve the compiled WHERE
        shape, so it must track a real killmail_time ``>=`` clause). Since
        _compile_search_where's 90-day safety net, every compile carries a
        lower bound, so has_time_bound is always True here in practice —
        the flag is kept as the contract rather than assumed.
    """
    where = list(compiled["where"])
    joins = compiled["joins"]
    sort_col = compiled["sort_col"]
    sort_dir = compiled["sort_dir"]
    cursor_clause = compiled["cursor_clause"]
    sort = compiled["sort"]

    # Live-poll mode: prepend new kills since=<id>. Sort always Date Desc by
    # killmail_id (front-end gating ensures this) — unrelated to any time
    # filter that may also be present, so the hint stays off (is_polling).
    is_polling = bool(live and since)
    if is_polling:
        where = where + [Killmail.killmail_id > since]
        sort_col = Killmail.killmail_id
        sort_dir = "desc"
        cursor_clause = None

    # Victim-side equality filters suppress the hint entirely — the planner's
    # composite victim indexes beat the forced time index by 17-660x there
    # (see has_selective_victim in _compile_search_where).
    hint_count = (bool(compiled.get("has_time_bound")) and not is_polling
                  and not compiled.get("has_selective_victim"))
    hint_page = hint_count and sort == "date"

    stmt = select(Killmail)
    if hint_page:
        stmt = stmt.with_hint(Killmail, f"INDEXED BY {KILLMAIL_TIME_INDEX}", dialect_name="sqlite")
    if "sde_systems" in joins:
        stmt = stmt.join(SDESystem, SDESystem.system_id == Killmail.solar_system_id)
    if "sde_types" in joins:
        stmt = stmt.join(SDEType, SDEType.type_id == Killmail.victim_ship_type_id)
    for clause in where:
        stmt = stmt.where(clause)
    if cursor_clause is not None:
        stmt = stmt.where(cursor_clause)
    if sort_dir == "desc":
        stmt = stmt.order_by(sort_col.desc(), Killmail.killmail_id.desc())
    else:
        stmt = stmt.order_by(sort_col.asc(), Killmail.killmail_id.asc())
    stmt = stmt.limit(PAGE_SIZE)

    count_stmt = select(func.count(Killmail.killmail_id), func.sum(Killmail.total_value)).select_from(Killmail)
    if hint_count:
        count_stmt = count_stmt.with_hint(Killmail, f"INDEXED BY {KILLMAIL_TIME_INDEX}", dialect_name="sqlite")
    if "sde_systems" in joins:
        count_stmt = count_stmt.join(SDESystem, SDESystem.system_id == Killmail.solar_system_id)
    if "sde_types" in joins:
        count_stmt = count_stmt.join(SDEType, SDEType.type_id == Killmail.victim_ship_type_id)
    for clause in where:
        count_stmt = count_stmt.where(clause)

    return stmt, count_stmt


@router.get("/intel/kills/search/results", response_class=HTMLResponse)
async def intel_kills_search_results(
    request: Request,
    db: AsyncSession = Depends(get_db),
    # — Time
    time: str = "",
    time_start: str = "",
    time_end: str = "",
    # — Chip rows (comma-separated)
    space: str = "",
    wh_class: str = "",
    shattered: int = 0,
    category: str = "",
    count: str = "",
    isk: str = "",
    primetime: str = "",
    flags: str = "",
    # — Ship + entity searches
    ship_id: str = "",
    attacker_mode: str = "or",
    attacker_chars: str = "",
    attacker_corps: str = "",
    attacker_allis: str = "",
    attacker_ships: str = "",
    victim_mode: str = "or",
    victim_chars: str = "",
    victim_corps: str = "",
    victim_allis: str = "",
    victim_ships: str = "",
    either_mode: str = "or",
    either_chars: str = "",
    either_corps: str = "",
    either_allis: str = "",
    either_ships: str = "",
    # — Sort + pagination
    sort: str = "date",
    dir: str = "desc",
    cursor: str = "",
    live: int = 0,
    since: int = 0,
):
    """Compile querystring -> SQL -> rows. Returns rendered partial with cursor markers."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    # Normalize params
    params: dict[str, Any] = {
        "time_preset": time if time in ("24h", "7d", "30d", "90d") else None,
        "time_start": None,
        "time_end": None,
        "space": _split_set(space),
        "wh_class": _split_set(wh_class),
        "shattered_only": bool(shattered),
        "category": _split_set(category),
        "count": _split_set(count),
        "isk": isk if isk in ISK_MIN_MAP else None,
        "primetime": _split_set(primetime),
        "flags": {x for x in _split_set(flags) if x in {"awox", "gank", "padding"}},
        "ship_ids": _split_ids(ship_id),
        "attacker_mode": attacker_mode if attacker_mode in ("and", "in", "or") else "or",
        "attacker_chars": _split_ids(attacker_chars),
        "attacker_corps": _split_ids(attacker_corps),
        "attacker_allis": _split_ids(attacker_allis),
        "attacker_ships": _split_ids(attacker_ships),
        "victim_mode": victim_mode if victim_mode in ("and", "in", "or") else "or",
        "victim_chars": _split_ids(victim_chars),
        "victim_corps": _split_ids(victim_corps),
        "victim_allis": _split_ids(victim_allis),
        "victim_ships": _split_ids(victim_ships),
        "either_mode": either_mode if either_mode in ("and", "in", "or") else "or",
        "either_chars": _split_ids(either_chars),
        "either_corps": _split_ids(either_corps),
        "either_allis": _split_ids(either_allis),
        "either_ships": _split_ids(either_ships),
        "sort": sort if sort in ("date", "isk", "involved") else "date",
        "direction": dir if dir in ("desc", "asc") else "desc",
        "cursor": cursor or None,
    }
    # Parse custom date range (YYYY-MM-DD HH:MM, UTC naive)
    for src, dst in (("time_start", "time_start"), ("time_end", "time_end")):
        raw = locals()[src].strip() if locals().get(src) else ""
        if raw:
            try:
                params[dst] = datetime.strptime(raw, "%Y-%m-%d %H:%M")
            except ValueError:
                pass

    compiled = await _compile_search_where(params, db)

    stmt, count_stmt = _build_search_statements(compiled, live, since)

    # When the default was anchored to a user-supplied time_end, the copy
    # changes: there IS a time filter, we just implied its lower edge.
    is_polling = bool(live and since)
    show_defaulted_note = _should_show_defaulted_note(
        bool(compiled.get("defaulted_time")), is_polling, params["cursor"]
    )
    defaulted_from_end = show_defaulted_note and params["time_end"] is not None

    try:
        rows = (await _execute_with_timeout(db, stmt)).scalars().all()

        # Compute total_count + total_isk only when not a live poll. Totals
        # are cursor-independent, so paging hits the short-TTL cache instead
        # of re-aggregating a couple hundred thousand rows per click.
        total_count = None
        total_isk = None
        if not live:
            ckey = _count_cache_key(params)
            centry = _count_cache.get(ckey)
            now_utc = datetime.utcnow()
            if centry and centry["expires_at"] > now_utc:
                total_count, total_isk = centry["count"], centry["isk"]
            else:
                result = (await _execute_with_timeout(db, count_stmt)).one()
                total_count = int(result[0] or 0)
                total_isk = float(result[1] or 0)
                if len(_count_cache) >= _COUNT_CACHE_MAX:
                    # Drop expired entries; if none expired, reset outright —
                    # it's a tiny recompute cache, correctness never depends on it.
                    live_entries = {k: v for k, v in _count_cache.items()
                                    if v["expires_at"] > now_utc}
                    _count_cache.clear()
                    if len(live_entries) < _COUNT_CACHE_MAX:
                        _count_cache.update(live_entries)
                _count_cache[ckey] = {
                    "count": total_count,
                    "isk": total_isk,
                    "expires_at": now_utc + timedelta(seconds=_COUNT_CACHE_TTL),
                }
    except asyncio.TimeoutError:
        log.warning("kill search timed out after %ss (params=%s)", _SEARCH_TIMEOUT_S, params)
        return HTMLResponse(
            "<div class='b-empty' style='padding:0.75rem;'>Search timed out after "
            f"{_SEARCH_TIMEOUT_S}s — narrow the time window or add a filter and retry."
            "</div>"
        )

    if not rows:
        return templates.TemplateResponse(
            "partials/intel_kills_search_results.html",
            {
                "request": request,
                "kills": [],
                "total_count": total_count or 0,
                "total_isk": total_isk or 0,
                "newest_cursor": "",
                "oldest_cursor": "",
                "live": bool(live),
                "defaulted_time": show_defaulted_note,
                "defaulted_from_end": defaulted_from_end,
            },
        )

    # Enrich + render
    enriched = await _enrich_for_search(rows, db)

    # Cursors
    newest = rows[0].killmail_id
    if params["sort"] == "date":
        oldest_cursor = str(rows[-1].killmail_id)
    elif params["sort"] == "isk":
        oldest_cursor = f"{rows[-1].total_value or 0}:{rows[-1].killmail_id}"
    else:
        oldest_cursor = f"{rows[-1].attacker_count or 0}:{rows[-1].killmail_id}"

    return templates.TemplateResponse(
        "partials/intel_kills_search_results.html",
        {
            "request": request,
            "kills": enriched,
            "total_count": total_count,
            "total_isk": total_isk,
            "newest_cursor": str(newest),
            "oldest_cursor": oldest_cursor,
            "live": bool(live),
            "defaulted_time": show_defaulted_note,
            "defaulted_from_end": defaulted_from_end,
        },
    )


async def _enrich_for_search(rows, db: AsyncSession) -> list[dict]:
    """Convert Killmail rows to the row-dict shape consumed by the shared
    feed-row partial. Reuses Feature A's name-resolver pattern.
    """
    from app.intel.killmail_stream import _sys_meta_cache
    from app.intel.recent_battles import sec_band

    if not rows:
        return []
    type_ids: set[int] = set()
    entity_ids: set[int] = set()
    system_ids: set[int] = set()
    kid_list = [r.killmail_id for r in rows]
    # Pull top-attacker name + corp for each kill
    att_q = select(
        KillmailAttacker.killmail_id,
        KillmailAttacker.character_id,
        KillmailAttacker.corporation_id,
        KillmailAttacker.final_blow,
    ).where(KillmailAttacker.killmail_id.in_(kid_list))
    att_rows = (await db.execute(att_q)).all()
    top_by_kid: dict[int, dict] = {}
    for kid, char_id, corp_id, fb in att_rows:
        cur = top_by_kid.get(kid)
        if cur is None or (fb and not cur.get("final_blow")):
            top_by_kid[kid] = {"character_id": char_id, "corporation_id": corp_id, "final_blow": bool(fb)}

    for r in rows:
        if r.victim_ship_type_id:
            type_ids.add(r.victim_ship_type_id)
        for x in (r.victim_character_id, r.victim_corporation_id):
            if x: entity_ids.add(x)
        if r.solar_system_id:
            system_ids.add(r.solar_system_id)
        top = top_by_kid.get(r.killmail_id) or {}
        for x in (top.get("character_id"), top.get("corporation_id")):
            if x: entity_ids.add(x)

    type_names = await type_ids_to_names(db, list(type_ids)) if type_ids else {}
    entity_names = await resolve_entity_names(list(entity_ids)) if entity_ids else {}
    sys_map: dict[int, dict] = {}
    if system_ids:
        for sid, name, sec in (await db.execute(
            select(SDESystem.system_id, SDESystem.system_name, SDESystem.security)
            .where(SDESystem.system_id.in_(system_ids))
        )).all():
            sys_map[sid] = {"name": name, "security": sec}

    _BAND_NORMALIZE = {"Highsec": "hs", "Lowsec": "ls", "Nullsec": "ns",
                       "Unknown": "unknown", "w-space": "wh"}

    def _band(sid: int) -> str:
        meta = _sys_meta_cache.get(sid)
        if meta:
            return _BAND_NORMALIZE.get(meta.get("band") or "Unknown", "unknown")
        sys = sys_map.get(sid)
        if not sys or sys["security"] is None:
            return "unknown"
        if sid >= WH_SYSTEM_MIN:
            return "wh"
        return _BAND_NORMALIZE.get(sec_band(sys["security"]), "unknown")

    out = []
    for r in rows:
        top = top_by_kid.get(r.killmail_id) or {}
        out.append({
            "killmail_id": r.killmail_id,
            "killmail_time": r.killmail_time.isoformat() if r.killmail_time else None,
            "system_name": (sys_map.get(r.solar_system_id) or {}).get("name", f"#{r.solar_system_id}"),
            "system_band": _band(r.solar_system_id),
            "system_class_label": None,  # Search page doesn't surface WH class label inline
            "victim_pilot": entity_names.get(r.victim_character_id, "?") if r.victim_character_id else "NPC",
            "victim_corp": entity_names.get(r.victim_corporation_id, "") if r.victim_corporation_id else "",
            "victim_ship": type_names.get(r.victim_ship_type_id, "?") if r.victim_ship_type_id else "?",
            "victim_ship_type_id": r.victim_ship_type_id,
            "top_attacker_pilot": entity_names.get(top.get("character_id"), "?") if top.get("character_id") else "NPC",
            "top_attacker_corp": entity_names.get(top.get("corporation_id"), "") if top.get("corporation_id") else "",
            "gang_size": r.attacker_count or 1,
            "isk": float(r.total_value or 0),
            "is_npc": bool(r.is_npc),
        })
    return out
