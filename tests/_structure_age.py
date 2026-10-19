"""Shared fixtures for the structure-age anchor tests: a throwaway database
seeded with a synthetic calibration table, and a signed-session TestClient.

The synthetic table is a straight line — POINTS triff points, one per day,
RATE IDs apart, the newest NEWEST_AGE days before today — so every expected
date in these tests is simple arithmetic. Ids and names are invented."""
import asyncio
import base64
import json
import tempfile
from datetime import datetime, time, timedelta, timezone

import itsdangerous
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, StructureAgeCalibration, User

BASE_ID = 1_050_000_000_000
RATE = 14_000_000
POINTS = 200
NEWEST_AGE = 101            # days between the newest point and today
CSRF = "test-csrf-token"


def today():
    return datetime.now(timezone.utc).date()


def point_id(i: int) -> int:
    return BASE_ID + i * RATE


def point_date(i: int):
    """Anchor date of point i; point POINTS-1 is NEWEST_AGE days ago."""
    return today() - timedelta(days=NEWEST_AGE + POINTS - 1 - i)


NEWEST_ID = point_id(POINTS - 1)


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def make_db(users=()):
    """A fresh schema holding the synthetic triff points and the given
    (user_id, role) users. Returns (engine, SessionLocal)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as db:
        for uid, role in users:
            db.add(User(id=uid, role=role, is_admin=role in ("admin", "manager")))
        for i in range(POINTS):
            mid = datetime.combine(point_date(i), time())
            db.add(StructureAgeCalibration(
                structure_id=point_id(i), anchor_mid=mid,
                anchor_low=mid - timedelta(days=3), anchor_high=mid + timedelta(days=3),
                days_wide=6.0, fetched_at=datetime(2026, 6, 6), source="triff"))
        await db.commit()
    return engine, SessionLocal


def client(user_id=None):
    """A TestClient with a signed session cookie: logged in as user_id, or —
    when user_id is None — a session holding only a CSRF token, which gets a
    POST past the CSRF middleware so the route's own gate answers."""
    import app.main as main
    session = {"csrf_token": CSRF}
    if user_id is not None:
        session["user_id"] = user_id
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    cookie = signer.sign(base64.b64encode(json.dumps(session).encode())).decode()
    c = TestClient(main.app, base_url="https://testserver")
    c.cookies.set("vigilant_session", cookie)
    c.headers.update({"X-CSRF-Token": CSRF})
    return c


def override_db(SessionLocal):
    """Point the app's get_db at SessionLocal; returns the undo callable."""
    import app.main as main
    from app.db.models import get_db

    async def _override():
        async with SessionLocal() as s:
            yield s
    main.app.dependency_overrides[get_db] = _override
    return lambda: main.app.dependency_overrides.pop(get_db, None)


def showinfo(structure_id: int) -> str:
    return f"<url=showinfo:35833//{structure_id}>J123456 - Sample Citadel (Sample Corp)</url>"
