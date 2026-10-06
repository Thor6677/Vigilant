"""GET /wormholes/system/<name> for a system that does not exist (ISS-123).

The not-found branch re-renders the finder page (wormholes.html) with an
error banner, but used to pass only part of the finder's context: the
template's class_colors / effect_colors lookups raised UndefinedError and
the visitor got a 500. It now answers 404 with the finder page, filters
included, under a "not found" banner.

The system name is invented; the database is an empty temp SQLite file.
"""
import asyncio
import base64
import json
import tempfile

import itsdangerous
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, User, get_db

USER_ID = 9301
_CSRF = "test-csrf-token-wh-unknown-0123456789"


def _run_async(fn):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(fn())
    finally:
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture
def client():
    import app.main as main

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(User(id=USER_ID))
            await db.commit()

    _run_async(_setup)

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    main.app.dependency_overrides[get_db] = override_get_db
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    payload = {"user_id": USER_ID, "csrf_token": _CSRF}
    cookie = signer.sign(base64.b64encode(json.dumps(payload).encode())).decode()
    c = TestClient(main.app, base_url="https://testserver")
    c.cookies.set("vigilant_session", cookie)
    try:
        yield c
    finally:
        main.app.dependency_overrides.pop(get_db, None)

        async def _dispose():
            await engine.dispose()
        _run_async(_dispose)


def test_unknown_system_is_a_404_finder_page_with_a_banner(client):
    resp = client.get("/wormholes/system/J999999")
    assert resp.status_code == 404
    html = resp.text
    assert "System &#39;J999999&#39; not found." in html
    assert 'class="b-banner is-danger"' in html
    # The finder itself renders: class, static and effect filters, coloured.
    assert html.count('data-filter="class"') >= 7
    assert 'data-filter="static_dest"' in html
    assert 'data-filter="effect"' in html
    assert 'style="color:;"' not in html


def test_unknown_system_name_is_escaped_in_the_banner(client):
    resp = client.get("/wormholes/system/%3Cimg%20src%3Dx%3EJ1")
    assert resp.status_code == 404
    assert "<img src=x>" not in resp.text
    assert "System &#39;&lt;img src=x&gt;J1&#39; not found." in resp.text


def test_finder_page_still_renders(client):
    resp = client.get("/wormholes")
    assert resp.status_code == 200
    assert 'data-filter="effect"' in resp.text
    assert "b-banner is-danger" not in resp.text
