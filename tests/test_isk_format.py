"""T-067: the wallet journals' ISK formatter (partials/_isk_format.html).

The thresholds used to compare the signed amount, so every expense printed
raw — a character journal showed "-3780900000 ISK" beside "+3.78B ISK", and
the Expenses and Net totals did the same. Amounts under 1 ISK rounded to "0".
"""
import asyncio
import base64
import json
import tempfile
from datetime import datetime, timedelta

import itsdangerous
import pytest
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_ENV = Environment(loader=FileSystemLoader("app/templates"), autoescape=True)
_TMPL = _ENV.from_string(
    '{% from "partials/_isk_format.html" import format_isk, format_isk_signed %}'
    '{{ format_isk(v) }}|{{ format_isk_signed(v) }}')


def _fmt(v):
    plain, signed = _TMPL.render(v=v).split("|")
    return plain.strip(), signed.strip()


@pytest.mark.parametrize("amount, plain, signed", [
    (35000754031.9884, "35.00B", "+35.00B"),
    (3780900000, "3.78B", "+3.78B"),
    (-3780900000, "-3.78B", "-3.78B"),          # was "-3780900000"
    (-29446996803, "-29.45B", "-29.45B"),       # the Expenses total seen on prod
    (-2.5e12, "-2.50T", "-2.50T"),
    (-5_000_000, "-5.00M", "-5.00M"),
    (-531857, "-531.9K", "-531.9K"),
    (1500, "1.5K", "+1.5K"),
    (-1500, "-1.5K", "-1.5K"),
    (999, "999", "+999"),
    (-10, "-10", "-10"),
    (0.01, "0.01", "+0.01"),                    # was "0"
    (0.4, "0.40", "+0.40"),
    (-0.5, "-0.50", "-0.50"),
    (0, "0", "0"),                              # zero is not a gain: no "+"
    (0.0, "0", "0"),
    (None, "—", "—"),
])
def test_format_isk(amount, plain, signed):
    assert _fmt(amount) == (plain, signed)


def test_journal_uses_the_shared_formatter():
    page = open("app/templates/journal.html").read()
    assert '{% from "partials/_isk_format.html" import format_isk, format_isk_signed %}' in page
    assert "{% macro format_isk" not in page     # no second copy to drift


def test_corp_journal_page_shortens_expenses_and_totals(monkeypatch):
    """End to end through the real route and template."""
    import app.main as main
    import app.routes.journal as journal
    from app.db.models import Base, Character, User, get_db

    user, corp = 41, 98000088
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(Character(character_id=6001, character_name="Wallet Pilot", user_id=user,
                             corporation_id=corp, corporation_name="Test Corp",
                             access_token="a", refresh_token="r",
                             token_expiry=datetime.utcnow() + timedelta(days=1),
                             scopes="esi-wallet.read_corporation_wallets.v1"))
            db.add(User(id=user))
            await db.commit()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(seed())
    finally:
        loop.close()

    async def override_get_db():
        async with SessionLocal() as s:
            yield s

    async def fake_refresh(char, db):
        return "token"

    async def fake_journal(client, corp_id, division, page=1):
        return [
            {"id": 1, "date": "2026-09-24T00:00:00Z", "ref_type": "bounty_prizes",
             "amount": 3780900000.0, "balance": 35000754031.99},
            {"id": 2, "date": "2026-09-24T01:00:00Z", "ref_type": "market_escrow",
             "amount": -5000000000.0, "balance": 31219854031.99},
            {"id": 3, "date": "2026-09-24T02:00:00Z", "ref_type": "player_donation",
             "amount": 0.0, "balance": 31219854031.99},
        ]

    async def fake_names(client, ids):
        return {}

    main.app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(journal, "refresh_token", fake_refresh)
    monkeypatch.setattr(journal.esi_corp, "get_corporation_wallet_journal", fake_journal)
    monkeypatch.setattr(journal, "_resolve_names", fake_names)
    try:
        signer = itsdangerous.TimestampSigner(main.settings.secret_key)
        cookie = signer.sign(base64.b64encode(json.dumps({"user_id": user}).encode())).decode()
        c = TestClient(main.app, base_url="https://testserver")
        c.cookies.set("vigilant_session", cookie)
        r = c.get(f"/corporations/{corp}/journal")
    finally:
        main.app.dependency_overrides.pop(get_db, None)

    assert r.status_code == 200
    text = " ".join(r.text.split())
    assert "+3.78B ISK" in text
    assert "-5.00B ISK" in text and "-5000000000" not in text      # the entry
    assert "-1.22B ISK" in text                                    # the Net total
    assert "+0 ISK" not in text                                    # a zero donation
