import time

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

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
from app.routes.admin import router as admin_router
from app.routes.skill_plans import router as skill_plans_router
from app.routes.structure_timers import router as structure_timers_router
from app.routes.starmap import router as starmap_router, start_map_poller
from app.routes.images import router as images_router
from app.routes.discordtime import router as discordtime_router
from app.routes.wormholes import router as wormholes_router
from app.routes.fitting import router as fitting_router
from app.routes.landings import router as landings_router

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

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="vigilant_session",
    max_age=86400 * 30,  # 30 days
    https_only=not settings.debug,
    same_site="lax",
)

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(auth_router)
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
app.include_router(dscan_router)
app.include_router(admin_router)
app.include_router(skill_plans_router)
app.include_router(structure_timers_router)
app.include_router(starmap_router)
app.include_router(images_router)
app.include_router(discordtime_router)
app.include_router(wormholes_router)
app.include_router(fitting_router)
app.include_router(landings_router)


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
            # ESI rate-limit events: soft-archive column for admin dismiss
            "ALTER TABLE esi_rate_limit_events ADD COLUMN archived_at DATETIME",
        ]:
            try:
                await db.execute(text(stmt))
                await db.commit()
            except Exception as migration_exc:
                await db.rollback()
                exc_str = str(migration_exc).lower()
                if "duplicate column" not in exc_str and "already exists" not in exc_str:
                    logging.warning("Startup migration warning for %r: %s", stmt, migration_exc)
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
