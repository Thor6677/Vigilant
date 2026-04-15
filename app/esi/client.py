from __future__ import annotations
import asyncio
import httpx
import json
import base64
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import get_settings
from app.db.models import Character
from app.db.cache import cache_get, cache_set
from app.esi.rate_limit import rate_limit_tracker, log_event

settings = get_settings()


class TokenRevoked(Exception):
    """Raised when ESI SSO returns 400/401 indicating a revoked or invalid refresh token."""
    pass


# ── Shared HTTP client with connection pooling ────────────────────────────────
# Reused across all ESI requests to avoid TCP handshake overhead.
_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=30,
            limits=httpx.Limits(
                max_connections=80,
                max_keepalive_connections=30,
                keepalive_expiry=120,
            ),
            http2=False,
        )
    return _http_client


# ── In-memory ETag cache for authenticated endpoints ─────────────────────────
# Key: (path, token_hash) → {etag, data}
# This avoids re-downloading unchanged character data.
_etag_cache: OrderedDict[str, dict] = OrderedDict()
_ETAG_CACHE_MAX = 5000


def _etag_key(path: str, token: str) -> str:
    """Build a cache key from path + token (first 16 chars)."""
    return f"{path}:{token[:16]}"


def get_etag_cache_stats() -> dict:
    """Return ETag cache statistics for admin dashboard."""
    return {
        "entries": len(_etag_cache),
        "max_entries": _ETAG_CACHE_MAX,
        "utilization_pct": round(len(_etag_cache) / _ETAG_CACHE_MAX * 100, 1) if _ETAG_CACHE_MAX else 0,
    }


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

    client = get_http_client()
    last_err = None
    for attempt in range(3):
        try:
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
            if resp.status_code in (400, 401):
                # Token revoked or invalid — user must re-authenticate
                raise TokenRevoked(f"SSO returned {resp.status_code}: token revoked or invalid")
            if resp.status_code >= 500 and attempt < 2:
                # Transient SSO error — retry with backoff
                await asyncio.sleep(1 * (2 ** attempt))
                continue
            resp.raise_for_status()
            break
        except TokenRevoked:
            raise
        except httpx.RemoteProtocolError:
            if attempt < 2:
                await asyncio.sleep(1 * (2 ** attempt))
                continue
            raise
    data = resp.json()

    character.access_token = data["access_token"]
    character.refresh_token = data.get("refresh_token", character.refresh_token)
    character.token_expiry = datetime.now(timezone.utc) + timedelta(seconds=data["expires_in"])
    await db.commit()
    return character.access_token


async def get_client(character: Character, db: AsyncSession) -> ESIClient:
    """Get an authenticated ESIClient for a character. Refreshes token if needed."""
    token = await refresh_token(character, db)
    return ESIClient(token, db=db)


async def get_client_safe(character: Character) -> ESIClient:
    """Get an authenticated ESIClient using an isolated DB session for token refresh.

    Use this when running concurrent operations (asyncio.gather) to avoid
    shared-session conflicts.
    """
    from app.db.models import AsyncSessionLocal
    async with AsyncSessionLocal() as token_db:
        result = await token_db.execute(
            select(Character).where(Character.character_id == character.character_id)
        )
        char_fresh = result.scalar_one_or_none()
        if not char_fresh:
            raise ValueError(f"Character {character.character_id} not found")
        token = await refresh_token(char_fresh, token_db)
    return ESIClient(token)


class ESIClient:
    def __init__(self, token: str, db: AsyncSession = None):
        self.token = token
        self.db = db
        self.base = settings.eve_esi_base
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "Vigilant/1.0 (EVE Online personal dashboard)",
        }

    async def _throttle_if_needed(self) -> None:
        delay = rate_limit_tracker.throttle_delay()
        if delay > 0:
            await asyncio.sleep(delay)

    async def _raw_get(self, url: str, req_headers: dict, params: dict):
        client = get_http_client()
        try:
            return await client.get(url, headers=req_headers, params=params)
        except httpx.RemoteProtocolError:
            # ESI sometimes drops connections mid-request; retry once after a short delay
            await asyncio.sleep(1)
            return await client.get(url, headers=req_headers, params=params)

    async def get(self, path: str, params: dict = None, bypass_cache: bool = False) -> dict | list:
        """Authenticated GET with DB cache + ETag support for 304 Not Modified.

        Caching is a two-tier fallthrough:
          1. DB cache (persisted, TTL-based) — skips the network entirely when
             a fresh copy is available. Survives restarts.
          2. ETag cache (in-memory, wiped on restart) — on cache miss, sends
             If-None-Match so the ESI server can short-circuit with 304.
        """
        # Tier 1: DB cache check — survives restarts, skips network entirely.
        if self.db and not bypass_cache:
            try:
                cached = await cache_get(self.db, path, params)
                if cached is not None:
                    return cached
            except Exception:
                # Cache failures must never break the request; fall through.
                pass

        await self._throttle_if_needed()
        url = f"{self.base}{path}"
        req_headers = dict(self.headers)

        # Tier 2: in-memory ETag cache — send If-None-Match for cheap 304.
        ek = _etag_key(path, self.token)
        cached_entry = _etag_cache.get(ek)
        if cached_entry and not bypass_cache:
            req_headers["If-None-Match"] = cached_entry["etag"]

        resp = await self._raw_get(url, req_headers, params or {})
        events = rate_limit_tracker.update_from_response(path, resp.status_code, dict(resp.headers))
        for e in events:
            asyncio.create_task(log_event(e["event_type"], e["path"], dict(resp.headers)))

        # Handle 304 Not Modified — return cached data (mark as recently used)
        if resp.status_code == 304 and cached_entry:
            _etag_cache.move_to_end(ek)
            # Refresh the DB cache so subsequent (and post-restart) calls can
            # skip the network entirely — we just confirmed the data is valid.
            if self.db and not bypass_cache:
                try:
                    await cache_set(self.db, path, cached_entry["data"], params)
                except Exception:
                    pass
            return cached_entry["data"]

        if resp.status_code == 429:
            retry_after = int(float(resp.headers.get("retry-after", "60")))
            asyncio.create_task(log_event("429", path, dict(resp.headers), retry_after))
            await asyncio.sleep(retry_after)
            resp = await self._raw_get(url, self.headers, params or {})
            rate_limit_tracker.update_from_response(path, resp.status_code, dict(resp.headers))
        elif resp.status_code == 420:
            asyncio.create_task(log_event("420", path, dict(resp.headers)))
            await asyncio.sleep(60)
            resp = await self._raw_get(url, self.headers, params or {})
            rate_limit_tracker.update_from_response(path, resp.status_code, dict(resp.headers))
        elif resp.status_code >= 500:
            # 5xx costs 0 tokens per CCP — retry with exponential backoff + jitter
            import random
            for attempt in range(2):
                delay = (1 << attempt) + random.uniform(0, 0.5)
                await asyncio.sleep(delay)
                resp = await self._raw_get(url, req_headers, params or {})
                rate_limit_tracker.update_from_response(path, resp.status_code, dict(resp.headers))
                if resp.status_code < 500:
                    break

        resp.raise_for_status()
        data = resp.json()

        # Store ETag for next request (LRU eviction)
        etag = resp.headers.get("etag")
        if etag:
            while len(_etag_cache) >= _ETAG_CACHE_MAX:
                _etag_cache.popitem(last=False)  # Remove least-recently-used
            _etag_cache[ek] = {"etag": etag, "data": data}

        # Persist to DB cache so the next restart (or concurrent caller) can
        # skip the network entirely. TTL is resolved per-path in the cache
        # module — see _ttl_for_path().
        if self.db and not bypass_cache:
            try:
                await cache_set(self.db, path, data, params)
            except Exception:
                pass

        return data

    async def get_public(self, path: str, params: dict = None, bypass_cache: bool = False) -> dict | list:
        """Public GET — cached when db session is available."""
        if self.db and not bypass_cache:
            cached = await cache_get(self.db, path, params)
            if cached is not None:
                return cached

        await self._throttle_if_needed()
        url = f"{self.base}{path}"
        pub_headers = {"Accept": "application/json"}
        resp = await self._raw_get(url, pub_headers, params or {})
        events = rate_limit_tracker.update_from_response(path, resp.status_code, dict(resp.headers))
        for e in events:
            asyncio.create_task(log_event(e["event_type"], e["path"], dict(resp.headers)))

        if resp.status_code == 429:
            retry_after = int(float(resp.headers.get("retry-after", "60")))
            asyncio.create_task(log_event("429", path, dict(resp.headers), retry_after))
            await asyncio.sleep(retry_after)
            resp = await self._raw_get(url, pub_headers, params or {})
            rate_limit_tracker.update_from_response(path, resp.status_code, dict(resp.headers))
        elif resp.status_code == 420:
            asyncio.create_task(log_event("420", path, dict(resp.headers)))
            await asyncio.sleep(60)
            resp = await self._raw_get(url, pub_headers, params or {})
            rate_limit_tracker.update_from_response(path, resp.status_code, dict(resp.headers))
        elif resp.status_code >= 500:
            import random
            for attempt in range(2):
                delay = (1 << attempt) + random.uniform(0, 0.5)
                await asyncio.sleep(delay)
                resp = await self._raw_get(url, pub_headers, params or {})
                rate_limit_tracker.update_from_response(path, resp.status_code, dict(resp.headers))
                if resp.status_code < 500:
                    break

        resp.raise_for_status()
        data = resp.json()

        if self.db and not bypass_cache:
            await cache_set(self.db, path, data, params)

        return data

    async def post(
        self,
        path: str,
        params: dict = None,
        body: dict | list | None = None,
    ) -> int:
        """Authenticated POST. Returns the HTTP status code.

        Used for endpoints like /ui/autopilot/waypoint/ that take query params
        and return 204 No Content. The caller decides how to interpret the
        status code (the ESI client doesn't try to JSON-decode the body).
        """
        await self._throttle_if_needed()
        url = f"{self.base}{path}"
        client = get_http_client()
        kwargs: dict = {"headers": self.headers, "params": params or {}}
        if body is not None:
            kwargs["json"] = body
        resp = await client.post(url, **kwargs)
        events = rate_limit_tracker.update_from_response(path, resp.status_code, dict(resp.headers))
        for e in events:
            asyncio.create_task(log_event(e["event_type"], e["path"], dict(resp.headers)))

        if resp.status_code == 429:
            retry_after = int(float(resp.headers.get("retry-after", "60")))
            asyncio.create_task(log_event("429", path, dict(resp.headers), retry_after))
            await asyncio.sleep(retry_after)
            resp = await client.post(url, **kwargs)
            rate_limit_tracker.update_from_response(path, resp.status_code, dict(resp.headers))
        elif resp.status_code == 420:
            asyncio.create_task(log_event("420", path, dict(resp.headers)))
            await asyncio.sleep(60)
            resp = await client.post(url, **kwargs)
            rate_limit_tracker.update_from_response(path, resp.status_code, dict(resp.headers))

        return resp.status_code

    async def post_public(self, path: str, body: list | dict) -> dict | list:
        """Public POST with cache (used for name resolution)."""
        cache_key_params = {"_body": json.dumps(body, sort_keys=True)}
        if self.db:
            cached = await cache_get(self.db, path, cache_key_params)
            if cached is not None:
                return cached

        await self._throttle_if_needed()
        url = f"{self.base}{path}"
        client = get_http_client()
        resp = await client.post(url, json=body, headers={"Accept": "application/json"})
        events = rate_limit_tracker.update_from_response(path, resp.status_code, dict(resp.headers))
        for e in events:
            asyncio.create_task(log_event(e["event_type"], e["path"], dict(resp.headers)))

        if resp.status_code == 429:
            retry_after = int(float(resp.headers.get("retry-after", "60")))
            asyncio.create_task(log_event("429", path, dict(resp.headers), retry_after))
            await asyncio.sleep(retry_after)
            resp = await client.post(url, json=body, headers={"Accept": "application/json"})
            rate_limit_tracker.update_from_response(path, resp.status_code, dict(resp.headers))
        elif resp.status_code == 420:
            asyncio.create_task(log_event("420", path, dict(resp.headers)))
            await asyncio.sleep(60)
            resp = await client.post(url, json=body, headers={"Accept": "application/json"})
            rate_limit_tracker.update_from_response(path, resp.status_code, dict(resp.headers))

        resp.raise_for_status()
        data = resp.json()

        if self.db:
            await cache_set(self.db, path, data, cache_key_params)

        return data
