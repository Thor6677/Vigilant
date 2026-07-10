import hashlib
import sys
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.nav import NAV_GROUPS, item_active, group_active
from app.middleware.csrf import CSRFMiddleware
from app.middleware.csp_nonce import CSPNonceMiddleware
from app.utils.perf import perf_enabled, perf_log

from app.config import get_settings
from app.db.models import init_db, AsyncSessionLocal, CharacterDashboardCache
from app.db.cache import ESICache  # registers table with Base
from app.db.sde_models import SDEType, SDESystem, SDEJump, SDEStation, SDERegion, SDEConstellation, SDEMeta, SDETypeMaterial, SDECompressible, SDEBlueprintInfo, SDEPlanet, SDEPlanetSchematic, SDEPlanetSchematicMaterial, SDEWormholeClass, SDEWormholeType, SDEMoon, SDEStar, SDEDogmaAttribute, SDETypeDogmaAttribute, SDEModuleSlot  # registers SDE tables
from app.sde.loader import ensure_sde_loaded
from app.auth.routes import router as auth_router
from app.routes.dashboard import router as dashboard_router, _background_scheduler
from app.routes.characters import router as characters_router
from app.routes.status import router as status_router
from app.routes.character_detail import router as character_detail_router
from app.routes.assets import router as assets_router
from app.routes.corporations import router as corporations_router
from app.routes.industry import router as industry_router
from app.routes.ambient import router as ambient_router
from app.routes.industry_jobs import router as industry_jobs_router
from app.routes.pi import router as pi_router
from app.routes.journal import router as journal_router
from app.routes.skills import router as skills_router
from app.routes.fittings import router as fittings_router
from app.routes.blueprints import router as blueprints_router
from app.routes.mining import router as mining_router
from app.routes.mining_ledger import router as mining_ledger_router
from app.routes.dscan import router as dscan_router
from app.routes.gatecheck import router as gatecheck_router
from app.routes.intel_kills import router as intel_kills_router
from app.routes.intel_kills_search import router as intel_kills_search_router
from app.routes.intel_entity import router as intel_entity_router
from app.routes.intel_watch import router as intel_watch_router
from app.routes.player_stats import router as player_stats_router
from app.routes.admin import router as admin_router
from app.routes.csp import router as csp_router
from app.routes.skill_plans import router as skill_plans_router
from app.routes.structure_timers import router as structure_timers_router
from app.routes.starmap import router as starmap_router, start_map_poller
from app.routes.images import router as images_router
from app.routes.discordtime import router as discordtime_router
from app.routes.structure_age import router as structure_age_router
from app.routes.wormholes import router as wormholes_router
from app.routes.fitting import router as fitting_router
from app.routes.landings import router as landings_router
from app.routes.palette import router as palette_router
from app.routes.market import router as market_router
from app.routes.networth import router as networth_router
from app.routes.stockpiles import router as stockpiles_router
from app.routes.pnl import router as pnl_router


def _css_version() -> str:
    """Content hash of the site's stylesheets, computed once at process
    startup. The edge proxy serves /static/ as `public, immutable,
    max-age=604800`; base.html appends `?v={{ css_v }}` to every stylesheet
    link so a future CSS change busts the 7-day cache for returning
    browsers instead of waiting it out.
    """
    h = hashlib.md5()
    for p in [
        Path("static/css/tailwind.css"),
        Path("design-system/css/tokens.css"),
        Path("design-system/css/motion.css"),
        Path("design-system/css/components.css"),
        Path("static/css/site.css"),
    ]:
        try:
            h.update(p.read_bytes())
        except OSError:
            pass  # missing file — version stays stable, just doesn't reflect it
    return h.hexdigest()[:8]


# Every app/routes/*.py and app/auth/routes.py module instantiates its own
# Jinja2Templates(directory="app/templates") — each gets its own private
# jinja2.Environment (confirmed in starlette.templating.Jinja2Templates), so
# setting env.globals on just one instance would only cache-bust pages
# rendered through that one router. base.html is the shared layout for all
# of them, so the global has to be pushed onto every instance. All router
# modules are imported above (module-level), so by this point they're all
# present in sys.modules with their `templates` attribute already built.
CSS_V = _css_version()
for _mod_name, _mod in list(sys.modules.items()):
    if _mod_name.startswith(("app.routes.", "app.auth.")):
        _templates = getattr(_mod, "templates", None)
        if isinstance(_templates, Jinja2Templates):
            _templates.env.globals["css_v"] = CSS_V
            # Single-source nav registry (see app/nav.py). base.html renders the
            # desktop nav, mobile menu, and footer from these; landings.py builds
            # its card grids from the same NAV_GROUPS.
            _templates.env.globals["nav_groups"] = NAV_GROUPS
            _templates.env.globals["nav_item_active"] = item_active
            _templates.env.globals["nav_group_active"] = group_active

settings = get_settings()

import logging
logging.basicConfig(level=logging.DEBUG if settings.debug else logging.INFO, format="%(levelname)s: %(name)s: %(message)s")

app = FastAPI(
    title="Vigilant",
    description="EVE Online character dashboard",
    docs_url="/api/docs" if settings.debug else None,
    redoc_url=None,
)

class _RequestTimingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if not perf_enabled():
            return await call_next(request)
        start = time.perf_counter()
        response = await call_next(request)
        total_ms = (time.perf_counter() - start) * 1000.0
        perf_log(f"http {request.method} {request.url.path}", total_ms=total_ms)
        return response


app.add_middleware(_RequestTimingMiddleware)

# CSP nonce middleware (T-012 Step 1). Stamps a per-request nonce on
# request.state.csp_nonce and emits Content-Security-Policy-Report-Only
# with the nonce inlined. Outermost-ish so the nonce is available for
# every handler that renders a template.
app.add_middleware(CSPNonceMiddleware)

# CSRF must be added BEFORE SessionMiddleware so that the latter wraps it —
# Starlette's middleware stack runs the most-recently-added one first, so
# this places SessionMiddleware on the outside (sets up scope["session"])
# and CSRFMiddleware on the inside (reads/writes the token in that session
# before the route handler runs).
app.add_middleware(CSRFMiddleware)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="vigilant_session",
    max_age=86400 * 7,  # 7 days
    https_only=not settings.debug,
    same_site="lax",
)

# /static/ds must be mounted BEFORE /static — Starlette matches mounts in
# registration order and /static would swallow the path. In the image only
# design-system/{css,ambient} exist (Dockerfile copies nothing else).
app.mount("/static/ds", StaticFiles(directory="design-system"), name="static_ds")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/healthz")
async def healthz():
    """Liveness probe for Docker HEALTHCHECK and the deploy script's HTTP
    probe. Cheap — no DB or ESI calls, just confirms uvicorn is serving.
    """
    return {"ok": True}

app.include_router(auth_router)
app.include_router(ambient_router)
app.include_router(dashboard_router)
app.include_router(characters_router)
app.include_router(status_router)
app.include_router(character_detail_router)
app.include_router(assets_router)
app.include_router(corporations_router)
app.include_router(industry_router)
app.include_router(industry_jobs_router)
app.include_router(pi_router)
app.include_router(journal_router)
app.include_router(skills_router)
app.include_router(fittings_router)
app.include_router(blueprints_router)
app.include_router(mining_router)
app.include_router(mining_ledger_router)
app.include_router(gatecheck_router)
app.include_router(intel_watch_router)
app.include_router(intel_kills_router)
app.include_router(intel_kills_search_router)
app.include_router(intel_entity_router)
app.include_router(player_stats_router)
app.include_router(dscan_router)
app.include_router(admin_router)
app.include_router(csp_router)
app.include_router(skill_plans_router)
app.include_router(structure_timers_router)
app.include_router(starmap_router)
app.include_router(images_router)
app.include_router(discordtime_router)
app.include_router(structure_age_router)
app.include_router(wormholes_router)
app.include_router(fitting_router)
app.include_router(landings_router)
app.include_router(palette_router)
app.include_router(market_router)
app.include_router(networth_router)
app.include_router(stockpiles_router)
app.include_router(pnl_router)


@app.on_event("startup")
async def startup():
    from sqlalchemy import text
    # One-time migration: drop the pre-v2 killmail tables if they still carry
    # the old `raw_json` column fingerprint. This runs once on first deploy of
    # the redesigned schema, then becomes a no-op forever after.
    async with AsyncSessionLocal() as db:
        try:
            res = await db.execute(text("PRAGMA table_info(killmails)"))
            cols = [r[1] for r in res.fetchall()]
            if "raw_json" in cols:
                for stmt in [
                    "DROP TABLE IF EXISTS killmail_attackers",
                    "DROP TABLE IF EXISTS killmails",
                    "DROP TABLE IF EXISTS character_kill_ingest",
                    "DROP TABLE IF EXISTS detected_battles",
                ]:
                    try:
                        await db.execute(text(stmt))
                        await db.commit()
                    except Exception as drop_exc:
                        await db.rollback()
                        logging.warning("Killmail legacy DROP warning for %r: %s", stmt, drop_exc)
                logging.info("Killmail legacy tables dropped (raw_json fingerprint)")
        except Exception as e:
            logging.warning("Killmail schema migration check failed: %s", e)

    await init_db()
    # Reset any syncs that were stuck in "syncing" from a previous run
    from sqlalchemy import update
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(CharacterDashboardCache)
            .where(CharacterDashboardCache.sync_status == "syncing")
            .values(sync_status="idle")
        )
        await db.commit()
    async with AsyncSessionLocal() as db:
        for stmt in [
            "ALTER TABLE characters ADD COLUMN security_status REAL",
            "ALTER TABLE characters ADD COLUMN user_id INTEGER REFERENCES users(id)",
            "ALTER TABLE characters ADD COLUMN is_main INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE sde_types ADD COLUMN volume REAL",
            "ALTER TABLE sde_types ADD COLUMN portion_size INTEGER",
            "ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'",
            # Skill plan sharing scopes (Phase 1 of corp/alliance/custom ACL rollout)
            "ALTER TABLE skill_plans ADD COLUMN visibility TEXT NOT NULL DEFAULT 'personal'",
            "ALTER TABLE skill_plans ADD COLUMN owner_corp_id INTEGER",
            "ALTER TABLE skill_plans ADD COLUMN owner_alliance_id INTEGER",
            "ALTER TABLE skill_plans ADD COLUMN last_edited_by_user_id INTEGER REFERENCES users(id)",
            "ALTER TABLE skill_plans ADD COLUMN last_edited_at DATETIME",
            # Wormhole reference: planet orbital distance
            "ALTER TABLE sde_planets ADD COLUMN distance_au REAL",
            # Fitting tool: market group on types for module browsing
            "ALTER TABLE sde_types ADD COLUMN market_group_id INTEGER",
            # Fitting engine: mass and capacity from invTypes
            "ALTER TABLE sde_types ADD COLUMN mass REAL",
            "ALTER TABLE sde_types ADD COLUMN capacity REAL",
            # Fitting tool: nested folders for saved fits
            "ALTER TABLE user_fittings ADD COLUMN folder_id INTEGER REFERENCES user_fitting_folders(id) ON DELETE SET NULL",
            # Fitting tool: implant loadout persisted with saved fits (ISS-016)
            "ALTER TABLE user_fittings ADD COLUMN implants_json TEXT NOT NULL DEFAULT '{}'",
            # ESI rate-limit events: soft-archive column for admin dismiss
            "ALTER TABLE esi_rate_limit_events ADD COLUMN archived_at DATETIME",
            # Indexes added in 2026-04 review-followup batch — create_all
            # skips existing tables, so these need explicit DDL.
            "CREATE INDEX IF NOT EXISTS ix_killmail_system_time ON killmails(solar_system_id, killmail_time)",
            # ix_killmails_killmail_time is load-bearing for INDEXED BY in
            # intel_kills_search — a missing index hard-errors every
            # time-bounded search, so self-heal it at startup.
            "CREATE INDEX IF NOT EXISTS ix_killmails_killmail_time ON killmails(killmail_time)",
            "CREATE INDEX IF NOT EXISTS ix_kma_corp_time ON killmail_attackers(corporation_id, killmail_id)",
            "CREATE INDEX IF NOT EXISTS ix_kma_alli_time ON killmail_attackers(alliance_id, killmail_id)",
            "CREATE INDEX IF NOT EXISTS ix_km_victim_corp_time ON killmails(victim_corporation_id, killmail_time DESC)",
            "CREATE INDEX IF NOT EXISTS ix_km_victim_alli_time ON killmails(victim_alliance_id, killmail_time DESC)",
            "CREATE INDEX IF NOT EXISTS ix_km_victim_ship_time ON killmails(victim_ship_type_id, killmail_time DESC)",
            "CREATE INDEX IF NOT EXISTS ix_esi_cache_expires_at ON esi_cache(expires_at)",
            # Top combatant alliance for major-battles widget
            "ALTER TABLE detected_battles ADD COLUMN top_attacker_alliance_id INTEGER",
            "ALTER TABLE detected_battles ADD COLUMN top_attacker_alliance_name TEXT",
            "ALTER TABLE detected_battles ADD COLUMN top_victim_alliance_id INTEGER",
            "ALTER TABLE detected_battles ADD COLUMN top_victim_alliance_name TEXT",
        ]:
            try:
                await db.execute(text(stmt))
                await db.commit()
            except Exception as migration_exc:
                await db.rollback()
                exc_str = str(migration_exc).lower()
                if "duplicate column" not in exc_str and "already exists" not in exc_str:
                    logging.warning("Startup migration warning for %r: %s", stmt, migration_exc)

    # ── Add killmail_attackers columns introduced for /intel/kills ─────
    # SQLite ALTER TABLE ADD COLUMN is idempotent-safe via PRAGMA check.
    async with AsyncSessionLocal() as db:
        cols = {r[1] for r in (await db.execute(text("PRAGMA table_info(killmail_attackers)"))).fetchall()}
        if "damage_done" not in cols:
            await db.execute(text("ALTER TABLE killmail_attackers ADD COLUMN damage_done INTEGER NOT NULL DEFAULT 0"))
        if "security_status" not in cols:
            await db.execute(text("ALTER TABLE killmail_attackers ADD COLUMN security_status REAL"))
        await db.commit()

    # SystemActivitySnapshot uniqueness — guard the insert path against the
    # double-fire race in the hourly poller. CREATE UNIQUE INDEX fails if
    # the table already has duplicates; we delete dups first, then the
    # index install becomes idempotent.
    async with AsyncSessionLocal() as db:
        try:
            res = await db.execute(text(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='uq_system_activity_snapshot'"
            ))
            if res.fetchone() is None:
                # Drop duplicates, keeping the lowest id per (system, captured_at)
                await db.execute(text(
                    "DELETE FROM system_activity_snapshots "
                    "WHERE id NOT IN (SELECT MIN(id) FROM system_activity_snapshots GROUP BY system_id, captured_at)"
                ))
                await db.execute(text(
                    "CREATE UNIQUE INDEX uq_system_activity_snapshot "
                    "ON system_activity_snapshots(system_id, captured_at)"
                ))
                await db.commit()
                logging.info("Installed uq_system_activity_snapshot index.")
        except Exception as e:
            await db.rollback()
            logging.warning("uq_system_activity_snapshot install warning: %s", e)
    # ── Backfill detected_battles.band/group_label for w-space rows that
    # were mis-labeled before we started using get_system_wh_class (which
    # walks system → constellation → region). Surgical: only touches rows
    # whose system actually resolves to a wormhole class, not all rows.
    try:
        from app.intel.recent_battles import wh_class_label
        from app.sde.lookup import get_system_wh_class
        async with AsyncSessionLocal() as db:
            sys_ids = (await db.execute(text(
                "SELECT DISTINCT system_id FROM detected_battles WHERE band != 'w-space'"
            ))).fetchall()
            fixed = 0
            for (sid,) in sys_ids:
                wc = await get_system_wh_class(db, sid)
                label = wh_class_label(wc)
                if label is None:
                    continue
                await db.execute(text(
                    "UPDATE detected_battles SET band='w-space', group_label=:lbl "
                    "WHERE system_id=:sid AND band != 'w-space'"
                ), {"lbl": label, "sid": sid})
                fixed += 1
            if fixed:
                await db.commit()
                logging.info("detected_battles: fixed band/group_label for %d w-space systems", fixed)
    except Exception as e:
        logging.warning("detected_battles wh-class backfill warning: %s", e)

    # ── Auto-promote first user to admin if no admin exists ────────────
    async with AsyncSessionLocal() as db:
        admin_check = await db.execute(text("SELECT id FROM users WHERE is_admin = 1 LIMIT 1"))
        if not admin_check.fetchone():
            await db.execute(text("UPDATE users SET is_admin = 1, role = 'admin' WHERE id = (SELECT MIN(id) FROM users)"))
            await db.commit()
        # Sync role column for existing admins
        await db.execute(text("UPDATE users SET role = 'admin' WHERE is_admin = 1 AND role = 'user'"))
        await db.commit()

    # ── Encrypt plaintext ESI tokens in-place ──────────────────────────
    from app.db.encryption import get_fernet

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text("SELECT id, access_token, refresh_token FROM characters"))).fetchall()
        fernet = get_fernet()
        migrated = 0
        for row in rows:
            char_id, raw_at, raw_rt = row
            needs_update = False
            new_at, new_rt = raw_at, raw_rt
            try:
                fernet.decrypt(raw_at.encode())
            except Exception:
                new_at = fernet.encrypt(raw_at.encode()).decode()
                needs_update = True
            try:
                fernet.decrypt(raw_rt.encode())
            except Exception:
                new_rt = fernet.encrypt(raw_rt.encode()).decode()
                needs_update = True
            if needs_update:
                await db.execute(
                    text("UPDATE characters SET access_token = :at, refresh_token = :rt WHERE id = :id"),
                    {"at": new_at, "rt": new_rt, "id": char_id},
                )
                migrated += 1
        if migrated:
            await db.commit()
            logging.info("Encrypted tokens for %d characters.", migrated)
    import asyncio
    asyncio.create_task(ensure_sde_loaded())
    asyncio.create_task(_background_scheduler())
    start_map_poller()

    from app.config import get_settings as _gs
    _cfg = _gs()
    if (
        _cfg.killmails_enabled
        and _cfg.killmail_battles_enabled
        and _cfg.killmail_stream_enabled
    ):
        from app.intel.killmail_stream import run_consumer, run_sweeper
        asyncio.create_task(run_consumer())
        asyncio.create_task(run_sweeper())

    # Player-count historical backfill — fires once per startup, no-ops if
    # archives are already loaded. Polite throttling + background task so it
    # never blocks startup. See app/intel/player_count_backfill.py.
    async def _kick_backfill():
        try:
            from app.intel.player_count_backfill import auto_backfill_if_needed
            decisions = await auto_backfill_if_needed()
            logging.getLogger(__name__).info(
                "player-count auto-backfill decisions: %s", decisions
            )
        except Exception as e:
            logging.getLogger(__name__).warning(
                "player-count auto-backfill bootstrap error: %s", e
            )
    asyncio.create_task(_kick_backfill())

    async def _kick_zkb_totals():
        try:
            from app.intel.killmail_daily_rollup import auto_zkb_totals_if_needed
            res = await auto_zkb_totals_if_needed()
            logging.getLogger(__name__).info("zkb-totals auto-ingest: %s", res)
        except Exception as e:
            logging.getLogger(__name__).warning("zkb-totals auto-ingest error: %s", e)
    asyncio.create_task(_kick_zkb_totals())

    async def _kick_pcu_rollup():
        try:
            from app.intel.pcu_daily_rollup import auto_backfill_if_empty
            res = await auto_backfill_if_empty()
            logging.getLogger(__name__).info("pcu daily rollup bootstrap: %s", res)
        except Exception as e:
            logging.getLogger(__name__).warning("pcu daily rollup bootstrap error: %s", e)
    asyncio.create_task(_kick_pcu_rollup())

    # /tools/activity SWR cache pre-warm — the long windows cost 10-60s
    # cold (1y raw ISK scan); warming them here means no user ever pays it.
    from app.routes.player_stats import warm_activity_cache
    asyncio.create_task(warm_activity_cache())

    # T-040: one-time resumable ISK backfill (month-chunked, self-skipping
    # once complete). Enables the aggregate-based 5y/all reads below.
    from app.intel.killmail_isk_backfill import run_backfill
    asyncio.create_task(run_backfill())
