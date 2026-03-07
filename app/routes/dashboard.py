from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.models import get_db, Character
from app.db.cache import cache_stats

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="app/templates")


def get_active_character_id(request: Request) -> int | None:
    return request.session.get("active_character_id")


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if request.session.get("active_character_id"):
        return RedirectResponse("/dashboard")
    return templates.TemplateResponse("index.html", {"request": request})


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    active_id = request.session.get("active_character_id")
    if not active_id:
        return RedirectResponse("/")

    character_ids = request.session.get("character_ids", [])
    result = await db.execute(select(Character).where(Character.character_id.in_(character_ids)))
    characters = result.scalars().all()

    active_char = next((c for c in characters if c.character_id == active_id), None)
    stats = await cache_stats(db)

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "characters": characters,
        "active_char": active_char,
        "cache_stats": stats,
    })
