"""Hub comparison preferences: defaults, save/load, validation."""
import asyncio
import tempfile

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, User
from app.market import hub_prefs

USER_ID = 91


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture
def session_factory():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as db:
            db.add(User(id=USER_ID))
            await db.commit()

    _run(setup())
    yield factory
    _run(engine.dispose())


def test_defaults_when_no_row(session_factory):
    async def go():
        async with session_factory() as db:
            return await hub_prefs.get_prefs(db, USER_ID)
    prefs = _run(go())
    assert prefs.sales_tax_pct == pytest.approx(3.37)
    assert prefs.broker_fee_pct == pytest.approx(1.5)
    assert prefs.origin_system_id is None


def test_save_then_load_and_update_in_place(session_factory):
    async def go():
        async with session_factory() as db:
            await hub_prefs.save_prefs(db, USER_ID, 4.5, 1.0, 30000142)
        async with session_factory() as db:
            await hub_prefs.save_prefs(db, USER_ID, 2.0, 0.5, None)
        async with session_factory() as db:
            return await hub_prefs.get_prefs(db, USER_ID)
    prefs = _run(go())
    assert (prefs.sales_tax_pct, prefs.broker_fee_pct, prefs.origin_system_id) == (2.0, 0.5, None)


@pytest.mark.parametrize("raw, default, expected", [
    ("4.5", 3.37, 4.5), (150, 3.37, 100.0), (-2, 3.37, 0.0),
    ("abc", 3.37, 3.37), (None, 1.5, 1.5), ("nan", 1.5, 1.5), ("", 1.5, 1.5),
])
def test_clamp_pct(raw, default, expected):
    assert hub_prefs.clamp_pct(raw, default) == pytest.approx(expected)


def test_scan_defaults_without_a_row(session_factory):
    async def go():
        async with session_factory() as db:
            return await hub_prefs.get_prefs(db, USER_ID)
    p = _run(go())
    assert p.scan_budget_isk == 500_000_000
    assert p.market_share_pct == 20.0


def test_save_scan_prefs_clamps_and_keeps_fees(session_factory):
    async def go():
        async with session_factory() as db:
            await hub_prefs.save_prefs(db, USER_ID, 8, 2, 30099001)
            await hub_prefs.save_scan_prefs(db, USER_ID, 5, 250)
            low = await hub_prefs.get_prefs(db, USER_ID)
            await hub_prefs.save_scan_prefs(db, USER_ID, 9e12, 0)
            high = await hub_prefs.get_prefs(db, USER_ID)
            return low, high
    low, high = _run(go())
    assert (low.scan_budget_isk, low.market_share_pct) == (1_000_000, 100.0)
    assert (high.scan_budget_isk, high.market_share_pct) == (100_000_000_000, 1.0)
    assert (high.sales_tax_pct, high.broker_fee_pct, high.origin_system_id) == (8, 2, 30099001)


def test_save_prefs_keeps_scan_settings(session_factory):
    async def go():
        async with session_factory() as db:
            await hub_prefs.save_scan_prefs(db, USER_ID, 2e9, 35)
            await hub_prefs.save_prefs(db, USER_ID, 4, 1, None)
            return await hub_prefs.get_prefs(db, USER_ID)
    p = _run(go())
    assert (p.scan_budget_isk, p.market_share_pct) == (2_000_000_000, 35.0)


# ---- the two Hub Scan columns at the database level ----

def test_an_insert_without_the_scan_columns_gets_their_defaults(session_factory):
    """create_all gives the columns a server default, so a row inserted without
    them (raw SQL, as the migration's ALTER TABLE does for old rows) is valid."""
    from sqlalchemy import text

    async def go():
        async with session_factory() as db:
            await db.execute(text(
                "INSERT INTO market_hub_prefs (user_id, sales_tax_pct, broker_fee_pct) "
                "VALUES (:u, 3.37, 1.5)"), {"u": USER_ID})
            await db.commit()
            return (await db.execute(text(
                "SELECT scan_budget_isk, market_share_pct FROM market_hub_prefs "
                "WHERE user_id = :u"), {"u": USER_ID})).one()
    assert tuple(_run(go())) == (500_000_000, 20)


def _main_alters():
    """The two Hub Scan statements, read out of app/main.py's startup
    migration list (they are inline there, not importable)."""
    import re
    from pathlib import Path

    source = Path(__file__).resolve().parents[1].joinpath("app", "main.py").read_text()
    found = re.findall(r'"(ALTER TABLE market_hub_prefs ADD COLUMN [^"]+)"', source)
    budget = [s for s in found if " scan_budget_isk " in s]
    share = [s for s in found if " market_share_pct " in s]
    assert len(budget) == 1 and len(share) == 1, found
    return budget[0], share[0]


def test_the_startup_migration_fills_old_rows_and_is_harmless_twice(tmp_path):
    """A market_hub_prefs table from before Hub Scan, with a row in it: the
    app's own ALTER statements give that row 500M / 20, and running them a
    second time (every startup does) changes nothing."""
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    budget_sql, share_sql = _main_alters()
    assert budget_sql == ("ALTER TABLE market_hub_prefs ADD COLUMN scan_budget_isk "
                          "FLOAT NOT NULL DEFAULT 500000000")
    assert share_sql == ("ALTER TABLE market_hub_prefs ADD COLUMN market_share_pct "
                         "FLOAT NOT NULL DEFAULT 20")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")

    async def go():
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE TABLE market_hub_prefs (user_id INTEGER PRIMARY KEY, "
                "sales_tax_pct FLOAT NOT NULL, broker_fee_pct FLOAT NOT NULL, "
                "origin_system_id INTEGER, updated_at DATETIME)"))
            await conn.execute(text(
                "INSERT INTO market_hub_prefs (user_id, sales_tax_pct, broker_fee_pct) "
                "VALUES (91, 4.0, 2.0)"))
        refused = []
        for _round in range(2):
            for stmt in (budget_sql, share_sql):
                try:
                    async with engine.begin() as conn:
                        await conn.execute(text(stmt))
                except OperationalError as exc:
                    # What main.py's loop swallows on a second run.
                    assert "duplicate column" in str(exc).lower()
                    refused.append(stmt)
        async with engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT sales_tax_pct, broker_fee_pct, scan_budget_isk, market_share_pct "
                "FROM market_hub_prefs WHERE user_id = 91"))).one()
            count = (await conn.execute(text(
                "SELECT COUNT(*) FROM market_hub_prefs"))).scalar()
        async with async_sessionmaker(engine)() as db:       # as the app reads it
            prefs = await hub_prefs.get_prefs(db, 91)
        await engine.dispose()
        return tuple(row), count, refused, prefs
    row, count, refused, prefs = _run(go())
    assert row == (4.0, 2.0, 500_000_000, 20) and count == 1
    assert refused == [budget_sql, share_sql]       # the second run only, and harmlessly
    assert (prefs.scan_budget_isk, prefs.market_share_pct) == (500_000_000, 20)
    assert (prefs.sales_tax_pct, prefs.broker_fee_pct) == (4.0, 2.0)
