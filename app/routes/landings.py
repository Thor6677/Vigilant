"""Landing pages for the Industry, Intel, and Tools top-level menus.

Each menu's top-level link renders a card grid summarizing the sub-tools.
The grids are built from the single-source nav registry (app/nav.py) —
add a page there with a desc/features and its card appears here
automatically. See the landing_group field for cards surfaced on another
group's landing (e.g. the wormhole reference tools live in the Map nav
group but keep their cards on the Intel landing)."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.nav import NAV_GROUPS

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _landing_cards(group_label: str) -> list[dict]:
    """Cards for one landing page, in nav order.

    An item belongs to landing L when it has a desc and either its own
    group is L (and in_landing is set) or it explicitly targets L via
    landing_group. Overview items (url == the landing page itself) are
    skipped — the user is already there.
    """
    landing_urls = {g["url"] for g in NAV_GROUPS if g.get("landing")}
    cards = []
    for group in NAV_GROUPS:
        for item in group["items"]:
            if not item.get("desc"):
                continue
            if item["url"] in landing_urls:
                continue  # Overview card would link to itself
            target = item.get("landing_group") or (group["label"] if item.get("in_landing") else None)
            if target == group_label:
                cards.append({
                    "name": item["label"],
                    "url": item["url"],
                    "desc": item["desc"],
                    "features": item.get("features", []),
                })
    return cards


INDUSTRY_TOOLS = _landing_cards("Industry")
INTEL_TOOLS = _landing_cards("Intel")
TOOLS_TOOLS = _landing_cards("Tools")


@router.get("/industry", response_class=HTMLResponse)
async def industry_landing(request: Request):
    if not request.session.get("user_id"):
        return RedirectResponse("/")
    return templates.TemplateResponse(request, "tool_landing.html", {"page_title": "Industry",
        "page_subtitle": "Manufacturing, hauling, compression, PI — pick a tool.",
        "tools": INDUSTRY_TOOLS})


@router.get("/intel", response_class=HTMLResponse)
async def intel_landing(request: Request):
    if not request.session.get("user_id"):
        return RedirectResponse("/")
    return templates.TemplateResponse(request, "tool_landing.html", {"page_title": "Intel",
        "page_subtitle": "Scans, gate checks, star map, wormhole reference.",
        "tools": INTEL_TOOLS})


@router.get("/tools", response_class=HTMLResponse)
async def tools_landing(request: Request):
    if not request.session.get("user_id"):
        return RedirectResponse("/")
    return templates.TemplateResponse(request, "tool_landing.html", {"page_title": "Tools",
        "page_subtitle": "Fittings, assets, timers, image host, timezone converter.",
        "tools": TOOLS_TOOLS})
