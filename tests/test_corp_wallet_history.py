"""Corp wallet history (T-056): hourly per-division snapshots, charted with
honest gaps, readable only by someone ESI lets read the wallet right now.
"""
import asyncio
import base64
import json
import os
import re
import tempfile
from datetime import datetime, timedelta

import httpx
import itsdangerous
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.main as main
import app.routes.corporations as corps
import app.routes.dashboard as dash
from app.db.models import Base, Character, CorpWalletSnapshot, get_db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T0 = datetime(2026, 9, 1, 0, 0, 0)
USER_A, USER_B = 11, 12
PLAYER_CORP, OTHER_CORP, NPC_CORP = 98000001, 98000002, 1000009


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _temp_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    _run(_create())
    return engine, SessionLocal


def _char(cid, user_id, corp_id, scopes="esi-wallet.read_corporation_wallets.v1"):
    return Character(character_id=cid, character_name=f"Pilot {cid}", user_id=user_id,
                     corporation_id=corp_id, access_token="a", refresh_token="r",
                     token_expiry=T0 + timedelta(days=1), scopes=scopes)


# ── the series builder ──────────────────────────────────────────────────────

def test_history_breaks_the_line_across_a_gap_and_sums_present_divisions():
    engine, SessionLocal = _temp_db()

    async def _body():
        async with SessionLocal() as db:
            # Three hourly samples, a six-hour hole, two more. In the last
            # sample division 2 is missing (e.g. ESI omitted an empty one).
            now = datetime.utcnow().replace(microsecond=0)
            base = now - timedelta(hours=12)
            stamps = [base, base + timedelta(hours=1), base + timedelta(hours=2),
                      base + timedelta(hours=8), base + timedelta(hours=9)]
            for i, t in enumerate(stamps):
                db.add(CorpWalletSnapshot(corp_id=PLAYER_CORP, division=1, balance=100 + i, recorded_at=t))
                if i != 4:
                    db.add(CorpWalletSnapshot(corp_id=PLAYER_CORP, division=2, balance=10, recorded_at=t))
            # Another corp's rows must never leak in.
            db.add(CorpWalletSnapshot(corp_id=OTHER_CORP, division=1, balance=999, recorded_at=base))
            await db.commit()
            return await corps._corp_wallet_history(PLAYER_CORP, "1w", db)

    data = _run(_body())
    assert data["samples"] == 5
    # 5 samples + 1 inserted gap marker
    assert len(data["labels"]) == 6
    assert data["series"]["1"] == [100, 101, 102, None, 103, 104]
    assert data["series"]["2"] == [10, 10, 10, None, 10, None]
    assert data["total"] == [110, 111, 112, None, 113, 104]
    assert 999 not in data["total"]


def test_history_range_and_downsampling():
    engine, SessionLocal = _temp_db()

    async def _body():
        async with SessionLocal() as db:
            now = datetime.utcnow().replace(microsecond=0)
            for h in range(24 * 40):  # 40 days hourly
                db.add(CorpWalletSnapshot(corp_id=PLAYER_CORP, division=1, balance=h,
                                          recorded_at=now - timedelta(hours=h)))
            await db.commit()
            week = await corps._corp_wallet_history(PLAYER_CORP, "1w", db)
            month = await corps._corp_wallet_history(PLAYER_CORP, "1m", db)
            return week, month

    week, month = _run(_body())
    assert 24 * 7 <= week["samples"] <= 24 * 7 + 1  # the boundary sample is a coin-flip on wall clock
    assert month["samples"] == corps._WALLET_HISTORY_MAX_POINTS, "30 days of hourly rows are thinned to the point cap"
    assert None not in month["total"], "even sampling must not manufacture gaps"


# ── the writer ──────────────────────────────────────────────────────────────

def test_snapshot_pass_fetches_each_corp_once_and_remembers_403s(monkeypatch):
    engine, SessionLocal = _temp_db()
    monkeypatch.setattr(dash, "AsyncSessionLocal", SessionLocal)
    dash._corp_403_cache.clear()

    async def _seed():
        async with SessionLocal() as db:
            db.add_all([
                _char(1001, USER_A, PLAYER_CORP),          # no role → 403
                _char(1002, USER_A, PLAYER_CORP),          # has the role
                _char(1003, USER_B, PLAYER_CORP),          # different user, same corp — must NOT cause a second fetch
                _char(1004, USER_B, OTHER_CORP),
                _char(1005, USER_B, NPC_CORP),             # NPC corp → skipped
                _char(1006, USER_A, OTHER_CORP, scopes=""),  # no scope → not a candidate
            ])
            await db.commit()
    _run(_seed())

    fetches = []
    denied_once = {"done": False}

    async def fake_client_for(char):
        return object(), None

    async def fake_wallets(client, corp_id):
        # The first character tried for PLAYER_CORP lacks the role: exactly
        # one 403, ever. Everything after that succeeds.
        fetches.append(corp_id)
        if corp_id == PLAYER_CORP and not denied_once["done"]:
            denied_once["done"] = True
            resp = httpx.Response(403, request=httpx.Request("GET", "https://esi.test/"))
            raise httpx.HTTPStatusError("forbidden", request=resp.request, response=resp)
        return [{"division": d, "balance": 1000.0 * d} for d in range(1, 8)]

    monkeypatch.setattr(dash, "_client_for", fake_client_for)
    monkeypatch.setattr(dash.esi_corp, "get_corporation_wallets", fake_wallets)

    written = _run(dash._snapshot_corp_wallets())
    assert written == 14, "seven divisions for each of the two player corps"
    # PLAYER_CORP: one 403 then one success; OTHER_CORP: one success. Never a
    # third attempt on PLAYER_CORP for the second user's character.
    assert sorted(fetches) == sorted([OTHER_CORP, PLAYER_CORP, PLAYER_CORP])
    assert (1001, PLAYER_CORP) in dash._corp_403_cache

    async def _rows():
        async with SessionLocal() as db:
            return (await db.execute(select(CorpWalletSnapshot))).scalars().all()
    rows = _run(_rows())
    assert {r.corp_id for r in rows} == {PLAYER_CORP, OTHER_CORP}
    assert len({r.recorded_at for r in rows}) == 1, "one timestamp for the whole pass"
    assert {r.division for r in rows if r.corp_id == PLAYER_CORP} == set(range(1, 8))

    # Second pass: the 403 is remembered, so PLAYER_CORP goes straight to the
    # character that worked.
    fetches.clear()
    _run(dash._snapshot_corp_wallets())
    assert sorted(fetches) == sorted([OTHER_CORP, PLAYER_CORP])


# ── the gate ────────────────────────────────────────────────────────────────

def _client(user_id=None):
    client = TestClient(main.app, base_url="https://testserver")
    if user_id is not None:
        signer = itsdangerous.TimestampSigner(main.settings.secret_key)
        cookie = signer.sign(base64.b64encode(json.dumps({"user_id": user_id}).encode())).decode()
        client.cookies.set("vigilant_session", cookie)
    return client


@pytest.fixture
def gated_app(monkeypatch):
    engine, SessionLocal = _temp_db()

    async def _seed():
        async with SessionLocal() as db:
            db.add_all([_char(2001, USER_A, PLAYER_CORP), _char(2002, USER_B, PLAYER_CORP)])
            for h in range(5):
                db.add(CorpWalletSnapshot(corp_id=PLAYER_CORP, division=1, balance=50,
                                          recorded_at=datetime.utcnow() - timedelta(hours=h)))
            await db.commit()
    _run(_seed())

    async def override_get_db():
        async with SessionLocal() as session:
            yield session
    main.app.dependency_overrides[get_db] = override_get_db

    # USER_A's character can read the wallet live; USER_B's cannot (403 —
    # in the corp, scoped, but without the in-game role).
    async def fake_fallback(scope_name, scope_chars, api_call_func, corp_id, db):
        chars = scope_chars.get(scope_name, [])
        if any(c.user_id == USER_A for c in chars):
            return [{"division": 1, "balance": 50.0}], None
        return None, "403"
    monkeypatch.setattr(corps, "_try_api_call_with_fallback", fake_fallback)
    yield
    main.app.dependency_overrides.pop(get_db, None)


def test_member_without_the_role_gets_no_rows_and_no_chart(gated_app):
    r = _client(USER_B).get(f"/corporations/{PLAYER_CORP}/wallet-history?range=1w")
    assert r.status_code == 404
    assert "labels" not in r.text
    # And the detail page for that user carries no history section at all —
    # exactly what they saw before this feature existed.
    r = _client(USER_B).get(f"/corporations/{PLAYER_CORP}/detail")
    assert r.status_code == 200
    assert "corp-wallet-history" not in r.text
    assert "Balance history" not in r.text


def test_member_who_can_read_the_wallet_live_gets_the_series(gated_app):
    r = _client(USER_A).get(f"/corporations/{PLAYER_CORP}/wallet-history?range=1w")
    assert r.status_code == 200
    body = r.json()
    assert body["samples"] == 5 and body["range"] == "1w"
    assert body["series"]["1"] == [50.0] * 5
    r = _client(USER_A).get(f"/corporations/{PLAYER_CORP}/detail")
    assert r.status_code == 200
    assert "corp-wallet-history" in r.text
    assert 'data-click="corpWalletRange"' in r.text


def test_anonymous_and_unknown_range(gated_app):
    assert _client().get(f"/corporations/{PLAYER_CORP}/wallet-history").status_code == 404
    r = _client(USER_A).get(f"/corporations/{PLAYER_CORP}/wallet-history?range=all-time")
    assert r.status_code == 200 and r.json()["range"] == "1m"


# ── page-level contract ─────────────────────────────────────────────────────

def test_partial_carries_data_only_and_page_breaks_lines_on_gaps():
    with open(os.path.join(ROOT, "app/templates/partials/corp_detail.html"), encoding="utf-8") as fh:
        partial = fh.read()
    assert "<script" not in partial
    assert 'data-chart="{{ wallet_history|tojson|forceescape }}"' in partial
    with open(os.path.join(ROOT, "app/templates/corporations.html"), encoding="utf-8") as fh:
        page = fh.read()
    assert "chart.js@" in page
    assert "window.corpWalletRange = function" in page
    assert re.search(r"spanGaps:\s*false", page), "a gap must break the line, never be bridged"
    assert "corp-detail-" in page and "htmx:afterSwap" in page
