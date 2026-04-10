"""Discord Timestamp Generator — creates Discord <t:UNIX:FORMAT> tags."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(tags=["tools"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/tools/discordtime", response_class=HTMLResponse)
async def discordtime_page(request: Request):
    return templates.TemplateResponse("discordtime.html", {"request": request})
