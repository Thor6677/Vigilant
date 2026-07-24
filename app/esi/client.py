from __future__ import annotations
import asyncio
import httpx
import json
import base64
import hashlib
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


def _etag_key(path: str, principal: str) -> str:
    """Build an in-memory ETag cache key from path + requesting identity.

    Namespaced by the caller's stable principal (see _principal_from_token) so
    the tier-2 ETag cache is per-identity, matching the tier-1 DB cache. (The
    previous token[:16] was effectively constant — every EVE JWT shares the
    same base64 header prefix — so this key was path-global.)
    """
    return f"{path}:{principal}"


def _principal_from_token(token: str) -> str:
    """Stable per-identity namespace for the authenticated DB cache.

    EVE SSO access tokens are JWTs whose `sub` claim is `CHARACTER:EVE:<id>`.
    The character id is stable across token refreshes, so it namespaces cached
    responses per identity without thrashing the cache every ~20 minutes when
    the token rotates. This is what keeps a corp-privileged response fetched by
    a role-holder from being served to a different (role-less) same-corp
    member: their requests carry a different principal, miss the cache, and hit
    ESI — which returns the per-token 403 the app already handles.

    Used only as a cache namespace, never for authorization, so the JWT is not
    signature-verified. If it cannot be parsed, fall back to a hash of the
    token itself — still per-identity, just not stable across refreshes.
    """
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # restore base64 padding
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        sub = payload.get("sub")
        if sub:
            return str(sub)  # e.g. "CHARACTER:EVE:123456789"
    except Exception:
        pass
    return "tok:" + hashlib.sha256(token.encode()).hexdigest()[:16]


# Global ESI throttle: a single asyncio.Event coordinates 429/420 backoff
# across all in-flight coroutines. Without this, a fan-out of N requests
# that all hit 429 each sleeps independently for `retry-after` seconds —
# correct, but wasteful and (worse) every coroutine then races to retry
# at the same instant, which can re-trigger the 429.
_global_throttle = asyncio.Event()
_global_throttle.set()
_global_throttle_until: float = 0.0


def _set_global_throttle(seconds: float) -> None:
    """Block all ESI calls for `seconds`. If already blocked for longer, no-op."""
    global _global_throttle_until
    loop = asyncio.get_event_loop()
    until = loop.time() + seconds
    if until <= _global_throttle_until:
        return
    _global_throttle_until = until
    _global_throttle.clear()
    # Schedule the release. Using call_later avoids spawning a coroutine
    # we'd have to track for cancellation.
    loop.call_later(seconds, _global_throttle.set)


def get_etag_cache_stats() -> dict:
    """Return ETag cache statistics for admin dashboard."""
    return {
        "entries": len(_etag_cache),
        "max_entries": _ETAG_CACHE_MAX,
        "utilization_pct": round(len(_etag_cache) / _ETAG_CACHE_MAX * 100, 1) if _ETAG_CACHE_MAX else 0,
    }


_refresh_locks: dict[int, asyncio.Lock] = {}


def _get_refresh_lock(character_id: int) -> asyncio.Lock:
    """Per-character lock so two coroutines refreshing the same character
    can't both submit refresh_token to SSO concurrently. CCP rotates the
    refresh_token on each call, so the older response that lands second
    overwrites the newer one — and the now-unused refresh_token is revoked,
    silently breaking the character on the next refresh."""
    lock = _refresh_locks.get(character_id)
    if lock is None:
        lock = asyncio.Lock()
        _refresh_locks[character_id] = lock
    return lock


async def refresh_token(character: Character, db: AsyncSession) -> str:
    """Refresh access token if expired, return valid access token."""
    now = datetime.now(timezone.utc)
    expiry = character.token_expiry
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)

    if expiry - now > timedelta(minutes=5):
        return character.access_token

    async with _get_refresh_lock(character.character_id):
        # Re-read after acquiring the lock — another coroutine may have just
        # refreshed this character. Reload from the same session so we see
        # the updated token (caller's session, or the get_client_safe()
        # isolated session — either way the row was committed).
        await db.refresh(character)
        now = datetime.now(timezone.utc)
        expiry = character.token_expiry
        if expiry and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry and expiry - now > timedelta(minutes=5):
            return character.access_token
        return await _do_refresh(character, db)


async def _do_refresh(character: Character, db: AsyncSession) -> str:
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
    def __init__(self, token: str, db: AsyncSession | None = None, *, cache_enabled: bool | None = None):
        """Authenticated ESI client.

        cache_enabled controls whether GETs go through the db-backed cache.
        For backward compat, passing a non-None `db` enables the cache. The db
        argument itself is NOT stored — the cache layer always uses an isolated
        AsyncSessionLocal internally, so passing a request-scoped session into
        an asyncio.gather fan-out used to be safe-by-accident; now the field
        cannot be misused for writes against a shared session.
        """
        self.token = token
        # Namespaces the authenticated DB cache to this token's identity so a
        # role-gated response is never served to a different caller. See F1.
        self.principal = _principal_from_token(token)
        self.cache_enabled = cache_enabled if cache_enabled is not None else (db is not None)
        self.base = settings.eve_esi_base
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "Vigilant/1.0 (EVE Online personal dashboard)",
        }

    async def _throttle_if_needed(self) -> None:
        # Wait for any global ESI throttle (set by a sibling coroutine that
        # hit 429/420) before consulting the per-group limiter.
        if not _global_throttle.is_set():
            await _global_throttle.wait()
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
        if self.cache_enabled and not bypass_cache:
            try:
                cached = await cache_get(None, path, params, principal=self.principal)
                if cached is not None:
                    return cached
            except Exception:
                # Cache failures must never break the request; fall through.
                pass

        await self._throttle_if_needed()
        url = f"{self.base}{path}"
        req_headers = dict(self.headers)

        # Tier 2: in-memory ETag cache — send If-None-Match for cheap 304.
        ek = _etag_key(path, self.principal)
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
            if self.cache_enabled and not bypass_cache:
                try:
                    await cache_set(None, path, cached_entry["data"], params, principal=self.principal)
                except Exception:
                    pass
            return cached_entry["data"]

        if resp.status_code == 429:
            retry_after = int(float(resp.headers.get("retry-after", "60")))
            asyncio.create_task(log_event("429", path, dict(resp.headers), retry_after))
            # Block ALL ESI traffic for retry_after seconds. Sibling coroutines
            # already in flight will await this event before their next call.
            _set_global_throttle(retry_after)
            await _global_throttle.wait()
            resp = await self._raw_get(url, self.headers, params or {})
            rate_limit_tracker.update_from_response(path, resp.status_code, dict(resp.headers))
        elif resp.status_code == 420:
            asyncio.create_task(log_event("420", path, dict(resp.headers)))
            _set_global_throttle(60)
            await _global_throttle.wait()
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
        if self.cache_enabled and not bypass_cache:
            try:
                await cache_set(None, path, data, params, principal=self.principal)
            except Exception:
                pass

        return data

    async def get_public(self, path: str, params: dict = None, bypass_cache: bool = False) -> dict | list:
        """Public GET — cached when db session is available."""
        if self.cache_enabled and not bypass_cache:
            cached = await cache_get(None, path, params)
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
            _set_global_throttle(retry_after)
            await _global_throttle.wait()
            resp = await self._raw_get(url, pub_headers, params or {})
            rate_limit_tracker.update_from_response(path, resp.status_code, dict(resp.headers))
        elif resp.status_code == 420:
            asyncio.create_task(log_event("420", path, dict(resp.headers)))
            _set_global_throttle(60)
            await _global_throttle.wait()
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

        if self.cache_enabled and not bypass_cache:
            await cache_set(None, path, data, params)

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
            _set_global_throttle(retry_after)
            await _global_throttle.wait()
            resp = await client.post(url, **kwargs)
            rate_limit_tracker.update_from_response(path, resp.status_code, dict(resp.headers))
        elif resp.status_code == 420:
            asyncio.create_task(log_event("420", path, dict(resp.headers)))
            _set_global_throttle(60)
            await _global_throttle.wait()
            resp = await client.post(url, **kwargs)
            rate_limit_tracker.update_from_response(path, resp.status_code, dict(resp.headers))

        return resp.status_code

    async def post_public(self, path: str, body: list | dict) -> dict | list:
        """Public POST with cache (used for name resolution)."""
        cache_key_params = {"_body": json.dumps(body, sort_keys=True)}
        if self.cache_enabled:
            cached = await cache_get(None, path, cache_key_params)
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
            _set_global_throttle(retry_after)
            await _global_throttle.wait()
            resp = await client.post(url, json=body, headers={"Accept": "application/json"})
            rate_limit_tracker.update_from_response(path, resp.status_code, dict(resp.headers))
        elif resp.status_code == 420:
            asyncio.create_task(log_event("420", path, dict(resp.headers)))
            _set_global_throttle(60)
            await _global_throttle.wait()
            resp = await client.post(url, json=body, headers={"Accept": "application/json"})
            rate_limit_tracker.update_from_response(path, resp.status_code, dict(resp.headers))

        resp.raise_for_status()
        data = resp.json()

        if self.cache_enabled:
            await cache_set(None, path, data, cache_key_params)

        return data
