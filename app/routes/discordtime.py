"""Discord Timestamp Generator — creates Discord <t:UNIX:FORMAT> tags."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(tags=["tools"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/tools/discordtime", response_class=HTMLResponse)
async def discordtime_page(request: Request):
    # ISS-044: login-only. The generator runs on actions.js, which base.html
    # loads only for a session, so a stranger got a page that did nothing.
    if not request.session.get("user_id"):
        return RedirectResponse("/")
    return templates.TemplateResponse(request, "discordtime.html", {})
