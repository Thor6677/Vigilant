"""The corp journal must name the pilot whose role actually read it (ISS-040).

`corp_journal` tries each scoped character in the corp until one is not
refused. The page used to headline corp_chars[0] regardless — the first
scoped pilot in DB order — while the entries came from whichever later
character held the in-game role.
"""
import asyncio
import base64
import json
import tempfile
from datetime import datetime, timedelta

import itsdangerous
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.main as main
import app.routes.journal as journal
from app.db.models import Base, Character, get_db

USER = 31
CORP = 98000077
SCOPE = "esi-wallet.read_corporation_wallets.v1"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _client(user_id):
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    cookie = signer.sign(base64.b64encode(json.dumps({"user_id": user_id}).encode())).decode()
    c = TestClient(main.app, base_url="https://testserver")
    c.cookies.set("vigilant_session", cookie)
    return c


def test_journal_headlines_the_character_that_was_not_refused(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def _seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            # "Aardvark" sorts first and has the scope but not the role;
            # "Zed" holds the Accountant role and is the one ESI answers.
            for cid, name in ((5001, "Aardvark Alt"), (5002, "Zed Accountant")):
                db.add(Character(character_id=cid, character_name=name, user_id=USER,
                                 corporation_id=CORP, corporation_name="Test Corp",
                                 access_token="a", refresh_token="r",
                                 token_expiry=datetime.utcnow() + timedelta(days=1), scopes=SCOPE))
            await db.commit()
    _run(_seed())

    async def override_get_db():
        async with SessionLocal() as s:
            yield s
    main.app.dependency_overrides[get_db] = override_get_db

    async def fake_refresh(char, db):
        return f"token-{char.character_id}"

    async def fake_journal(client, corp_id, division, page=1):
        if client.token == "token-5001":
            raise RuntimeError("403 Forbidden: Character does not have required role(s)")
        return [{"id": 1, "date": "2026-09-24T00:00:00Z", "ref_type": "bounty_prizes",
                 "amount": 1000.0, "balance": 5000.0, "first_party_id": None, "second_party_id": None}]

    async def fake_names(client, ids):
        return {}

    monkeypatch.setattr(journal, "refresh_token", fake_refresh)
    monkeypatch.setattr(journal.esi_corp, "get_corporation_wallet_journal", fake_journal)
    monkeypatch.setattr(journal, "_resolve_names", fake_names)
    try:
        r = _client(USER).get(f"/corporations/{CORP}/journal")
    finally:
        main.app.dependency_overrides.pop(get_db, None)

    assert r.status_code == 200
    assert "Zed Accountant" in r.text, "the pilot whose role read the journal"
    assert "characters/5002/portrait" in r.text
    assert "Aardvark Alt" not in r.text, "the refused pilot must not be presented as the viewer"
