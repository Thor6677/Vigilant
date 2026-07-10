"""Route smoke tests for /market/pnl industry/trading splits (T-041 item 2,
plan Task 4).

Pattern follows tests/test_fitting_compare.py: signed-session-cookie
TestClient idiom from tests/test_networth.py plus a get_db dependency
override onto a temp-file sqlite DB, so the route + real SQL run against an
isolated database rather than the app's production `vigilant.db`.
"""
import asyncio
import base64
import json
import tempfile
from datetime import datetime

import itsdangerous
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import (
    Base,
    Character,
    IndustryJobHistory,
    WalletTransaction,
    get_db,
)

USER_ID = 81
CHAR_ID = 90000099


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _client():
    import app.main as main
    return TestClient(main.app)


def _authed_client(user_id=USER_ID):
    """TestClient carrying a signed session cookie for `user_id`. Uses an https
    base_url because the session cookie is Secure outside debug mode."""
    import app.main as main

    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    data = base64.b64encode(json.dumps({"user_id": user_id}).encode())
    cookie = signer.sign(data).decode()
    client = TestClient(main.app, base_url="https://testserver")
    client.cookies.set("vigilant_session", cookie)
    return client


def _make_char(cid=CHAR_ID, user_id=USER_ID) -> Character:
    return Character(
        character_id=cid,
        character_name="PNL Pilot",
        access_token="dummy-access",
        refresh_token="dummy-refresh",
        token_expiry=datetime(2099, 1, 1),
        scopes="",
        user_id=user_id,
    )


def _seeded_app_db(with_jobs=True):
    """Temp sqlite DB with a character + one buy/sell WalletTransaction pair,
    and (when `with_jobs`) one priced + one NULL-cost IndustryJobHistory row.
    Overrides `get_db` on the real app so the route runs against this DB
    instead of the app's production sqlite file. Returns a teardown callable.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(_make_char())
            db.add(WalletTransaction(
                transaction_id=1, character_id=CHAR_ID,
                date=datetime(2026, 6, 1), type_id=34, quantity=100,
                unit_price=5.0, is_buy=True,
            ))
            db.add(WalletTransaction(
                transaction_id=2, character_id=CHAR_ID,
                date=datetime(2026, 6, 5), type_id=34, quantity=100,
                unit_price=10.0, is_buy=False,
            ))
            if with_jobs:
                # Priced job -> synthesized build lot (product type 44),
                # contributes to Industry P&L. A wallet SELL of the same
                # type_id, dated after completion, is what actually
                # REALIZES the build profit (a build lot alone, with no
                # matching sell, never shows up in P&L) — this is the
                # assertion that would fail against the pre-Task-4 route.
                db.add(IndustryJobHistory(
                    job_id=101, character_id=CHAR_ID, activity_id=1,
                    blueprint_type_id=999, product_type_id=44, runs=1,
                    output_qty=10, install_cost=50.0, build_cost=100.0,
                    cost_basis="history",
                    start_date=datetime(2026, 5, 1),
                    completed_date=datetime(2026, 5, 2),
                ))
                db.add(WalletTransaction(
                    transaction_id=3, character_id=CHAR_ID,
                    date=datetime(2026, 6, 10), type_id=44, quantity=10,
                    unit_price=20.0, is_buy=False,
                ))
                # NULL-cost job -> excluded from P&L, counted as "awaiting
                # pricing" in the footnote.
                db.add(IndustryJobHistory(
                    job_id=102, character_id=CHAR_ID, activity_id=1,
                    blueprint_type_id=998, product_type_id=45, runs=1,
                    output_qty=5, install_cost=0.0, build_cost=None,
                    cost_basis=None,
                    start_date=datetime(2026, 5, 1),
                    completed_date=datetime(2026, 5, 2),
                ))
            await db.commit()

    _run(seed())

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    import app.main as main
    main.app.dependency_overrides[get_db] = override_get_db

    def teardown():
        main.app.dependency_overrides.pop(get_db, None)

    return teardown


def test_pnl_page_redirects_when_unauthenticated():
    r = _client().get("/market/pnl", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/"


def test_pnl_page_shows_industry_split_and_awaiting_pricing_note():
    teardown = _seeded_app_db(with_jobs=True)
    try:
        r = _authed_client().get("/market/pnl")
        assert r.status_code == 200
        body = r.text
        assert "Industry" in body
        assert "awaiting pricing" in body
        assert "Trading &amp; Industry P&amp;L" in body
        # The build lot for type 44 was actually sold (see seed comment) ->
        # realized build profit shows in the per-type split annotation, not
        # just the (always-present) page chrome.
        assert "B:" in body
    finally:
        teardown()


def test_pnl_page_renders_with_no_job_rows():
    # Wallet transactions synced but no industry job history at all — the
    # page must still render cleanly (no build lots, no awaiting-pricing
    # note, tiles still shaped correctly).
    teardown = _seeded_app_db(with_jobs=False)
    try:
        r = _authed_client().get("/market/pnl")
        assert r.status_code == 200
        assert "Industry" in r.text
        assert "awaiting pricing" not in r.text
    finally:
        teardown()


def _bare_app_db(extra_rows=()):
    """Temp sqlite DB with just a character (+ optional extra rows), get_db
    overridden on the real app. Returns a teardown callable."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(_make_char())
            for row in extra_rows:
                db.add(row)
            await db.commit()

    _run(seed())

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    import app.main as main
    main.app.dependency_overrides[get_db] = override_get_db

    def teardown():
        main.app.dependency_overrides.pop(get_db, None)

    return teardown


def test_pnl_page_renders_with_no_data_at_all():
    # Character exists but has neither wallet transactions nor industry
    # jobs -> the pre-existing empty state must still render, with no
    # awaiting-pricing note (nothing is excluded).
    teardown = _bare_app_db()
    try:
        r = _authed_client().get("/market/pnl")
        assert r.status_code == 200
        assert "No wallet transactions or completed industry jobs" in r.text
        assert "awaiting pricing" not in r.text
    finally:
        teardown()


def test_pnl_page_empty_state_surfaces_awaiting_pricing_note():
    # A character whose ONLY industry activity is a NULL-cost job (and no
    # wallet transactions) hits the empty-state early return — the page must
    # still explain WHY it's empty (site convention: excluded AND counted).
    teardown = _bare_app_db(extra_rows=[IndustryJobHistory(
        job_id=201, character_id=CHAR_ID, activity_id=1,
        blueprint_type_id=998, product_type_id=45, runs=1,
        output_qty=5, install_cost=0.0, build_cost=None,
        cost_basis=None,
        start_date=datetime(2026, 5, 1),
        completed_date=datetime(2026, 5, 2),
    )])
    try:
        r = _authed_client().get("/market/pnl")
        assert r.status_code == 200
        # Empty state rendered (no priced lots -> no stat tiles) ...
        assert "No wallet transactions or completed industry jobs" in r.text
        # ... but the excluded NULL-cost job is still counted and explained.
        assert "awaiting pricing" in r.text
        assert "1 completed job" in r.text
    finally:
        teardown()
