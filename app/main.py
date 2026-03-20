from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db.models import init_db, AsyncSessionLocal, CharacterDashboardCache, WalletSnapshot, CharacterAssetCache
from app.db.cache import ESICache  # registers table with Base
from app.db.sde_models import SDEType, SDESystem, SDEJump, SDEStation, SDERegion, SDEConstellation, SDEMeta  # registers SDE tables
from app.sde.loader import ensure_sde_loaded
from app.auth.routes import router as auth_router
from app.routes.dashboard import router as dashboard_router, _background_scheduler
from app.routes.chat import router as chat_router
from app.routes.characters import router as characters_router
from app.routes.status import router as status_router
from app.routes.character_detail import router as character_detail_router
from app.routes.assets import router as assets_router
from app.routes.corporations import router as corporations_router

settings = get_settings()

import logging
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(name)s: %(message)s")

app = FastAPI(
    title="Vigilant",
    description="EVE Online character dashboard",
    docs_url="/api/docs" if settings.debug else None,
    redoc_url=None,
)

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
app.include_router(chat_router)
app.include_router(characters_router)
app.include_router(status_router)
app.include_router(character_detail_router)
app.include_router(assets_router)
app.include_router(corporations_router)


@app.on_event("startup")
async def startup():
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
    from sqlalchemy import text
    async with AsyncSessionLocal() as db:
        for stmt in [
            "ALTER TABLE characters ADD COLUMN security_status REAL",
            "ALTER TABLE characters ADD COLUMN user_id INTEGER REFERENCES users(id)",
            "ALTER TABLE characters ADD COLUMN is_main INTEGER NOT NULL DEFAULT 0",
        ]:
            try:
                await db.execute(text(stmt))
                await db.commit()
            except Exception as migration_exc:
                await db.rollback()
                exc_str = str(migration_exc).lower()
                if "duplicate column" not in exc_str and "already exists" not in exc_str:
                    logging.warning("Startup migration warning for %r: %s", stmt, migration_exc)
    # ── Encrypt plaintext ESI tokens in-place ──────────────────────────
    from sqlalchemy import text as sql_text
    from app.db.encryption import get_fernet
    from cryptography.fernet import InvalidToken as FernetInvalidToken

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(sql_text("SELECT id, access_token, refresh_token FROM characters"))).fetchall()
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
                    sql_text("UPDATE characters SET access_token = :at, refresh_token = :rt WHERE id = :id"),
                    {"at": new_at, "rt": new_rt, "id": char_id},
                )
                migrated += 1
        if migrated:
            await db.commit()
            logging.info("Encrypted tokens for %d characters.", migrated)
    import asyncio
    asyncio.create_task(ensure_sde_loaded())
    asyncio.create_task(_background_scheduler())
