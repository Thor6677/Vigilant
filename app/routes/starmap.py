"""
Serves the React star-map SPA and its API endpoints.

The React app is built to frontend/dist/ by Vite and served at /map/*.
All unmatched /map/* paths return index.html for client-side routing.
"""
import os
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse

router = APIRouter()

# Resolve the built React app directory
FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"


@router.get("/map/{rest_of_path:path}")
async def serve_map(request: Request, rest_of_path: str):
    """
    Serve the React SPA. Static assets (JS/CSS) are served directly;
    all other paths return index.html for client-side routing.
    """
    if not FRONTEND_DIST.exists():
        return HTMLResponse(
            "<h1>Star map not built</h1>"
            "<p>Run <code>cd frontend && npm run build</code> to generate the map.</p>",
            status_code=503,
        )

    # Try to serve static file first (assets/*, data/*, etc.)
    file_path = FRONTEND_DIST / rest_of_path
    if rest_of_path and file_path.exists() and file_path.is_file():
        return FileResponse(file_path)

    # Fall through to index.html for SPA routing
    index = FRONTEND_DIST / "index.html"
    if index.exists():
        return FileResponse(index)

    return HTMLResponse("<h1>Map index.html not found</h1>", status_code=404)


@router.get("/map")
async def serve_map_root():
    """Redirect /map to /map/ for consistent base path."""
    return FileResponse(FRONTEND_DIST / "index.html") if (FRONTEND_DIST / "index.html").exists() else HTMLResponse(
        "<h1>Star map not built</h1>", status_code=503
    )
