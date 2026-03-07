from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db.models import init_db
from app.auth.routes import router as auth_router
from app.routes.dashboard import router as dashboard_router
from app.routes.chat import router as chat_router

settings = get_settings()

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


@app.on_event("startup")
async def startup():
    await init_db()
