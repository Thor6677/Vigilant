from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db.models import init_db, AsyncSessionLocal, CharacterDashboardCache
from app.db.cache import ESICache  # registers table with Base
from app.db.sde_models import SDEType, SDESystem, SDEJump, SDEStation, SDERegion, SDEConstellation, SDEMeta  # registers SDE tables
from app.sde.loader import ensure_sde_loaded
from app.auth.routes import router as auth_router
from app.routes.dashboard import router as dashboard_router
from app.routes.chat import router as chat_router
from app.routes.characters import router as characters_router
from app.routes.skills import router as skills_router
from app.routes.status import router as status_router

settings = get_settings()

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(name)s: %(message)s")

app = FastAPI(
    title="CapsuleerAI",
    description="EVE Online AI Assistant powered by Claude",
    docs_url="/api/docs" if settings.debug else None,
    redoc_url=None,
)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="capsuleerai_session",
    max_age=86400 * 30,  # 30 days
    https_only=not settings.debug,
    same_site="lax",
)

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(chat_router)
app.include_router(characters_router)
app.include_router(skills_router)
app.include_router(status_router)


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
    import asyncio
    asyncio.create_task(ensure_sde_loaded())
