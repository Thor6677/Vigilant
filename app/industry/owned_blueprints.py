"""Raw blueprint rows from ESI for a character or a corporation. Shared by
Copy Packs (which goes on to locate each blueprint) and Hub Scan (which only
needs the blueprint type ids).

Sequential by design: `refresh_token(char, db)` commits on the caller's
session, which must never be shared by concurrent coroutines.

The ESI callables default to the real ones; a caller may pass its own (Copy
Packs passes the names bound in its module so its tests can fake them there)."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Character
from app.esi import character as esi_char
from app.esi import corporation as esi_corp
from app.esi.client import ESIClient, refresh_token
from app.routes.blueprints import _fetch_all_pages
from app.routes.corporations import _try_api_call_with_fallback

# _fetch_all_pages stops after 10 pages of 1000; a list that long may be cut off.
PAGE_CAP = 10 * 1000


async def character_rows(db: AsyncSession, char: Character, *,
                         refresh=refresh_token,
                         fetch_pages=_fetch_all_pages) -> tuple[list, str]:
    """(raw rows, the token used). Raises on any ESI or token failure."""
    token = await refresh(char, db)
    raw = await fetch_pages(esi_char.get_blueprints,
                            ESIClient(token, db=db), char.character_id)
    return raw, token


async def corporation_rows(db: AsyncSession, corp_id: int,
                           holders: list[Character], *,
                           fallback=_try_api_call_with_fallback,
                           fetch_pages=_fetch_all_pages) -> list | None:
    """Raw rows, or None when no holder could read them (role or scope)."""
    async def fetch_corp(client, cid):
        return await fetch_pages(esi_corp.get_corporation_blueprints, client, cid)

    raw, _err = await fallback(
        "blueprints", {"blueprints": holders}, fetch_corp, corp_id, db)
    return raw
