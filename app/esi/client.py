import httpx
import json
import base64
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import get_settings
from app.db.models import Character
from app.db.cache import cache_get, cache_set

settings = get_settings()


async def refresh_token(character: Character, db: AsyncSession) -> str:
    """Refresh access token if expired, return valid access token."""
    now = datetime.now(timezone.utc)
    expiry = character.token_expiry
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)

    if expiry - now > timedelta(minutes=5):
        return character.access_token

    credentials = base64.b64encode(
        f"{settings.eve_client_id}:{settings.eve_client_secret}".encode()
    ).decode()

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            settings.eve_sso_token_url,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": character.refresh_token,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    character.access_token = data["access_token"]
    character.refresh_token = data.get("refresh_token", character.refresh_token)
    character.token_expiry = datetime.now(timezone.utc) + timedelta(seconds=data["expires_in"])
    await db.commit()
    return character.access_token


class ESIClient:
    def __init__(self, token: str, db: AsyncSession = None):
        self.token = token
        self.db = db
        self.base = settings.eve_esi_base
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

    async def get(self, path: str, params: dict = None, bypass_cache: bool = False) -> dict | list:
        """Authenticated GET — private character data, not cached."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self.base}{path}",
                headers=self.headers,
                params=params or {},
            )
            resp.raise_for_status()
            return resp.json()

    async def get_public(self, path: str, params: dict = None, bypass_cache: bool = False) -> dict | list:
        """Public GET — cached when db session is available."""
        if self.db and not bypass_cache:
            cached = await cache_get(self.db, path, params)
            if cached is not None:
                return cached

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self.base}{path}",
                headers={"Accept": "application/json"},
                params=params or {},
            )
            resp.raise_for_status()
            data = resp.json()

        if self.db and not bypass_cache:
            await cache_set(self.db, path, data, params)

        return data

    async def post_public(self, path: str, body: list | dict) -> dict | list:
        """Public POST with cache (used for name resolution)."""
        cache_key_params = {"_body": json.dumps(body, sort_keys=True)}
        if self.db:
            cached = await cache_get(self.db, path, cache_key_params)
            if cached is not None:
                return cached

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.base}{path}",
                json=body,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

        if self.db:
            await cache_set(self.db, path, data, cache_key_params)

        return data
