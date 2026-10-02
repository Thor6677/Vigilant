"""ISS-074: the one-time rebuild that gives an existing `characters` table
AUTOINCREMENT (app/db/character_ids.py).

This table holds every account's encrypted EVE tokens, so the rebuild is
held to a high bar: every row and every byte of ciphertext preserved, every
index (above all the UNIQUE one on character_id) back afterwards, one
transaction, and no change at all when it can't be sure.

Two shapes of existing table are exercised:
  - "create_all": exactly what Base.metadata.create_all produced before the
    model declared sqlite_autoincrement (a fresh install of v1.7.1).
  - "altered": an older install, whose table predates five columns that the
    startup migrations in app/main.py later added with ALTER TABLE. Column
    order differs from the model's, and two columns carry a DEFAULT the model
    doesn't declare.
"""
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import MetaData, event, insert, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.character_ids import ensure_characters_autoincrement
from app.db.models import Character, CharacterDashboardCache, User, WalletSnapshot

# What create_all emitted for `characters` before the model declared
# sqlite_autoincrement, and the two indexes it built for the model's
# `index=True` columns.
CREATE_ALL_DDL = """CREATE TABLE characters (
    id INTEGER NOT NULL,
    character_id INTEGER NOT NULL,
    character_name VARCHAR NOT NULL,
    corporation_id INTEGER,
    corporation_name VARCHAR,
    alliance_id INTEGER,
    alliance_name VARCHAR,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    token_expiry DATETIME NOT NULL,
    scopes TEXT NOT NULL,
    declined_scopes TEXT NOT NULL,
    is_active BOOLEAN,
    added_at DATETIME,
    last_seen DATETIME,
    sort_order INTEGER,
    security_status FLOAT,
    birthday DATETIME,
    account_group VARCHAR,
    user_id INTEGER,
    is_main BOOLEAN,
    owner_hash VARCHAR,
    PRIMARY KEY (id),
    FOREIGN KEY(user_id) REFERENCES users (id)
)"""

# An older install: the table as first created, then app/main.py's ALTERs.
ALTERED_BASE_DDL = """CREATE TABLE characters (
    id INTEGER NOT NULL,
    character_id INTEGER NOT NULL,
    character_name VARCHAR NOT NULL,
    corporation_id INTEGER,
    corporation_name VARCHAR,
    alliance_id INTEGER,
    alliance_name VARCHAR,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    token_expiry DATETIME NOT NULL,
    scopes TEXT NOT NULL,
    is_active BOOLEAN,
    added_at DATETIME,
    last_seen DATETIME,
    sort_order INTEGER,
    birthday DATETIME,
    account_group VARCHAR,
    PRIMARY KEY (id)
)"""
STARTUP_ALTERS = (
    "ALTER TABLE characters ADD COLUMN security_status REAL",
    "ALTER TABLE characters ADD COLUMN user_id INTEGER REFERENCES users(id)",
    "ALTER TABLE characters ADD COLUMN is_main INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE characters ADD COLUMN declined_scopes TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE characters ADD COLUMN owner_hash TEXT",
)
INDEX_DDL = (
    "CREATE UNIQUE INDEX ix_characters_character_id ON characters (character_id)",
    "CREATE INDEX ix_characters_user_id ON characters (user_id)",
)

SHAPES = {
    "create_all": (CREATE_ALL_DDL,),
    "altered": (ALTERED_BASE_DDL,) + STARTUP_ALTERS,
}

# Fake pilots: (EVE character id, name, access token, refresh token).
PILOTS = [
    (92_000_001, "Pilot A", "at-pilot-a", "rt-pilot-a"),
    (92_000_002, "Test Alt", "at-test-alt", "rt-test-alt"),
    (92_000_003, "Pilot C", "at-pilot-c", "rt-pilot-c"),
    (92_000_004, "Pilot D", "at-pilot-d", "rt-pilot-d"),
]
REMOVED = 92_000_002   # leaves a gap in the ids: 1, 3, 4


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _build_legacy(Session, shape):
    """The `shape` table with both indexes, its neighbours, and three pilots
    whose tokens were encrypted by the ORM."""
    async with Session() as db:
        conn = await db.connection()
        await conn.run_sync(lambda c: User.__table__.create(c))
        for stmt in SHAPES[shape] + INDEX_DDL:
            await db.execute(text(stmt))
        await conn.run_sync(lambda c: CharacterDashboardCache.__table__.create(c))
        await conn.run_sync(lambda c: WalletSnapshot.__table__.create(c))
        await db.commit()

        user = User(role="user")
        db.add(user)
        await db.commit()
        expiry = datetime(2026, 1, 2, 3, 4, 5)
        for n, (eve_id, name, at, rt) in enumerate(PILOTS):
            db.add(Character(
                character_id=eve_id, character_name=name, access_token=at, refresh_token=rt,
                token_expiry=expiry, scopes="scope.one scope.two", user_id=user.id,
                is_main=(n == 0), security_status=-1.5 if n == 2 else None,
                birthday=datetime(2010, 5, 6) if n == 3 else None,
                owner_hash="hash-%d" % n if n != 3 else None,
            ))
            await db.commit()
        await db.execute(text("DELETE FROM characters WHERE character_id = :c"), {"c": REMOVED})
        for eve_id, *_ in PILOTS:
            if eve_id == REMOVED:
                continue
            db.add(CharacterDashboardCache(character_id=eve_id, sync_status="idle"))
            db.add(WalletSnapshot(character_id=eve_id, balance=1.0,
                                  recorded_at=datetime(2026, 1, 1)))
        await db.commit()


async def _table_sql(db, name="characters"):
    return (await db.execute(text(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :n"), {"n": name})).scalar()


async def _indexes(db):
    """(name, unique, origin, partial, columns) for every index on characters."""
    out = []
    for _seq, name, unique, origin, partial in (await db.execute(
            text("PRAGMA index_list(characters)"))).all():
        cols = tuple(r[2] for r in (await db.execute(
            text(f'PRAGMA index_info("{name}")'))).all())
        out.append((name, unique, origin, partial, cols))
    return sorted(out)


async def _raw(db):
    """Every stored value with its storage class, per column, by id: what the
    database holds, never passed through the ORM's decryption."""
    cols = [r[1] for r in (await db.execute(text("PRAGMA table_info(characters)"))).all()]
    return {
        c: (await db.execute(text(
            f'SELECT id, typeof("{c}"), "{c}" FROM characters ORDER BY id'))).all()
        for c in cols
    }


async def _snapshot(db):
    return await _table_sql(db), await _raw(db), await _indexes(db)


def _scenario(tmp_path, shape, body, *, foreign_keys=False):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
        if foreign_keys:
            @event.listens_for(engine.sync_engine, "connect")
            def _fk_on(dbapi_connection, _record):
                cur = dbapi_connection.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _build_legacy(Session, shape)
            return await body(Session)
        finally:
            await engine.dispose()
    return _run(go())


async def _rebuild(Session):
    async with Session() as db:
        return await ensure_characters_autoincrement(db)


@pytest.fixture(params=sorted(SHAPES))
def shape(request):
    return request.param


# ── The rebuild ─────────────────────────────────────────────────────────────

def test_the_rebuild_keeps_every_row_byte_for_byte(tmp_path, shape):
    async def body(Session):
        async with Session() as db:
            before = await _raw(db)
        assert await _rebuild(Session) is True
        async with Session() as db:
            return before, await _raw(db)
    before, after = _scenario(tmp_path, shape, body)
    assert [r[0] for r in before["id"]] == [1, 3, 4]
    assert before == after
    # The stored tokens really are ciphertext, not the plaintext.
    assert all(r[2].startswith("gAAAA") for r in after["access_token"])


def test_tokens_still_decrypt_through_the_orm_after_the_rebuild(tmp_path, shape):
    async def body(Session):
        assert await _rebuild(Session) is True
        async with Session() as db:
            rows = (await db.execute(select(Character).order_by(Character.id))).scalars().all()
            return [(c.character_id, c.character_name, c.access_token, c.refresh_token)
                    for c in rows]
    assert _scenario(tmp_path, shape, body) == [p for p in PILOTS if p[0] != REMOVED]


def test_the_rebuild_keeps_every_index(tmp_path, shape):
    async def body(Session):
        async with Session() as db:
            before = await _indexes(db)
        assert await _rebuild(Session) is True
        async with Session() as db:
            return before, await _indexes(db)
    before, after = _scenario(tmp_path, shape, body)
    assert ("ix_characters_character_id", 1, "c", 0, ("character_id",)) in before
    assert after == before


def test_an_index_the_model_does_not_declare_is_kept_too(tmp_path, shape):
    async def body(Session):
        async with Session() as db:
            await db.execute(text(
                "CREATE INDEX ix_local_by_name ON characters (character_name, corporation_id)"))
            await db.commit()
            before = await _indexes(db)
        assert await _rebuild(Session) is True
        async with Session() as db:
            return before, await _indexes(db)
    before, after = _scenario(tmp_path, shape, body)
    assert any(i[0] == "ix_local_by_name" for i in before)
    assert after == before


def test_planner_statistics_survive_the_rebuild(tmp_path, shape):
    """DROP TABLE deletes a table's sqlite_stat1/sqlite_stat4 rows; prod has
    them (sampled ANALYZE, daily PRAGMA optimize), so they are carried over."""
    async def stats(db):
        out = {}
        for t in ("sqlite_stat1", "sqlite_stat4"):
            if (await db.execute(text(
                    "SELECT 1 FROM sqlite_master WHERE name = :t"), {"t": t})).first():
                out[t] = (await db.execute(text(
                    f"SELECT * FROM {t} WHERE tbl = 'characters' ORDER BY rowid"))).all()
        return out

    async def body(Session):
        async with Session() as db:
            await db.execute(text("ANALYZE"))
            await db.commit()
            before = await stats(db)
        assert await _rebuild(Session) is True
        async with Session() as db:
            return before, await stats(db)
    before, after = _scenario(tmp_path, shape, body)
    assert {r[1] for r in before["sqlite_stat1"]} == {
        "ix_characters_character_id", "ix_characters_user_id"}
    assert after == before


def test_a_duplicate_character_id_is_still_refused_after_the_rebuild(tmp_path, shape):
    async def body(Session):
        assert await _rebuild(Session) is True
        async with Session() as db:
            db.add(Character(character_id=PILOTS[0][0], character_name="Pilot A Again",
                             access_token="x", refresh_token="y",
                             token_expiry=datetime(2026, 1, 1), scopes=""))
            with pytest.raises(IntegrityError):
                await db.commit()
    _scenario(tmp_path, shape, body)


def test_the_rebuilt_table_has_autoincrement_and_does_not_reuse_ids(tmp_path, shape):
    async def body(Session):
        assert await _rebuild(Session) is True
        async with Session() as db:
            sql = await _table_sql(db)
            seq = (await db.execute(text(
                "SELECT seq FROM sqlite_sequence WHERE name = 'characters'"))).all()
            leftover = (await db.execute(text(
                "SELECT count(*) FROM sqlite_master WHERE name LIKE '%rebuild%'"))).scalar()
            leftover_seq = (await db.execute(text(
                "SELECT count(*) FROM sqlite_sequence WHERE name != 'characters' "
                "AND name != 'users'"))).scalar()
            # Remove the newest, add another: it must not get id 4 back.
            await db.execute(text("DELETE FROM characters WHERE id = 4"))
            await db.commit()
            c = Character(character_id=92_000_099, character_name="Pilot New",
                          access_token="a", refresh_token="r",
                          token_expiry=datetime(2026, 1, 1), scopes="")
            db.add(c)
            await db.commit()
            return sql, seq, leftover, leftover_seq, c.id
    sql, seq, leftover, leftover_seq, new_id = _scenario(tmp_path, shape, body)
    assert "AUTOINCREMENT" in sql.upper()
    assert seq == [(4,)]
    assert leftover == 0 and leftover_seq == 0
    assert new_id == 5


def test_the_rebuilt_table_matches_a_fresh_install(tmp_path, shape):
    """Same columns, types, constraints and indexes as create_all now makes."""
    async def fresh():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(lambda c: User.__table__.create(c))
                await conn.run_sync(lambda c: Character.__table__.create(c))
            async with async_sessionmaker(engine)() as db:
                return await _shape_of(db)
        finally:
            await engine.dispose()

    async def body(Session):
        assert await _rebuild(Session) is True
        async with Session() as db:
            return await _shape_of(db)

    assert _scenario(tmp_path, shape, body) == _run(fresh())


async def _shape_of(db):
    cols = (await db.execute(text("PRAGMA table_info(characters)"))).all()
    fks = (await db.execute(text("PRAGMA foreign_key_list(characters)"))).all()
    sql = (await _table_sql(db)).replace('"characters"', "characters")
    return [tuple(c) for c in cols], [tuple(f) for f in fks], sql, await _indexes(db)


def test_other_tables_still_reference_characters_after_the_rebuild(tmp_path, shape):
    """Their REFERENCES characters(character_id) resolve to the rebuilt table,
    and foreign_key_check finds the UNIQUE parent index it needs."""
    async def body(Session):
        assert await _rebuild(Session) is True
        async with Session() as db:
            out = {}
            for child in ("character_dashboard_cache", "wallet_snapshots"):
                fks = (await db.execute(text(f"PRAGMA foreign_key_list({child})"))).all()
                problems = (await db.execute(text(f"PRAGMA foreign_key_check({child})"))).all()
                out[child] = ([(f[2], f[3], f[4]) for f in fks], problems)
            out["characters"] = (None, (await db.execute(
                text("PRAGMA foreign_key_check(characters)"))).all())
            return out
    out = _scenario(tmp_path, shape, body)
    for child in ("character_dashboard_cache", "wallet_snapshots"):
        assert out[child] == ([("characters", "character_id", "character_id")], [])
    assert out["characters"] == (None, [])


def test_foreign_key_check_does_notice_a_missing_parent_index(tmp_path):
    """Control for the test above: without the UNIQUE index on
    characters.character_id, foreign_key_check refuses outright."""
    async def body(Session):
        async with Session() as db:
            await db.execute(text("DROP INDEX ix_characters_character_id"))
            await db.commit()
            with pytest.raises(OperationalError, match="foreign key mismatch"):
                await db.execute(text("PRAGMA foreign_key_check(character_dashboard_cache)"))
    _scenario(tmp_path, "create_all", body)


def test_a_second_call_is_a_no_op(tmp_path, shape):
    async def body(Session):
        assert await _rebuild(Session) is True
        async with Session() as db:
            first = await _snapshot(db)
        assert await _rebuild(Session) is False
        async with Session() as db:
            return first, await _snapshot(db)
    first, second = _scenario(tmp_path, shape, body)
    assert first == second


def test_a_fresh_install_is_left_alone(tmp_path):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
        Session = async_sessionmaker(engine)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(lambda c: User.__table__.create(c))
                await conn.run_sync(lambda c: Character.__table__.create(c))
            async with Session() as db:
                before = await _table_sql(db)
            changed = await _rebuild(Session)
            async with Session() as db:
                return changed, before, await _table_sql(db)
        finally:
            await engine.dispose()
    changed, before, after = _run(go())
    assert changed is False and before == after


def test_a_database_without_a_characters_table_is_left_alone(tmp_path):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")
        try:
            return await _rebuild(async_sessionmaker(engine))
        finally:
            await engine.dispose()
    assert _run(go()) is False


def test_the_rebuild_logs_what_it_did_and_how_long_it_took(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="app.db.character_ids")
    _scenario(tmp_path, "altered", _rebuild)
    lines = [r.getMessage() for r in caplog.records if r.name == "app.db.character_ids"]
    assert len(lines) == 1
    assert "AUTOINCREMENT" in lines[0] and "3 rows, 2 indexes" in lines[0]
    assert re.search(r"\d+\.\d{3}s$", lines[0])


# ── When it must not touch the table ────────────────────────────────────────

async def _add_unknown_column(db):
    await db.execute(text("ALTER TABLE characters ADD COLUMN something_local TEXT"))


async def _add_trigger(db):
    await db.execute(text(
        "CREATE TRIGGER tr_local AFTER DELETE ON characters "
        "BEGIN DELETE FROM character_dashboard_cache "
        "WHERE character_id = old.character_id; END"))


async def _add_view(db):
    await db.execute(text(
        "CREATE VIEW v_local AS SELECT character_id, character_name FROM characters"))


async def _no_change(db):
    pass


@pytest.mark.parametrize("setup,foreign_keys,why", [
    (_add_unknown_column, False, "unknown columns"),
    (_add_trigger, False, "trigger"),
    (_add_view, False, "view"),
    (_no_change, True, "foreign key"),
], ids=["unknown-column", "trigger", "view", "foreign-keys-on"])
def test_the_rebuild_bails_and_changes_nothing(tmp_path, shape, setup, foreign_keys, why, caplog):
    caplog.set_level(logging.WARNING, logger="app.db.character_ids")

    async def body(Session):
        async with Session() as db:
            await setup(db)
            await db.commit()
            before = await _snapshot(db)
        changed = await _rebuild(Session)
        async with Session() as db:
            return changed, before, await _snapshot(db)

    changed, before, after = _scenario(tmp_path, shape, body, foreign_keys=foreign_keys)
    assert changed is False
    assert after == before
    assert "AUTOINCREMENT" not in after[0].upper()
    assert any(why in r.getMessage() for r in caplog.records)


def test_a_table_with_an_inline_unique_constraint_is_left_alone(tmp_path, caplog):
    """An automatic index (from an inline UNIQUE) has no SQL to replay, and the
    model's DDL wouldn't recreate it, so the rebuild refuses."""
    caplog.set_level(logging.WARNING, logger="app.db.character_ids")
    inline = CREATE_ALL_DDL.replace(
        "character_id INTEGER NOT NULL,", "character_id INTEGER NOT NULL UNIQUE,", 1)
    SHAPES["inline_unique"] = (inline,)
    try:
        async def body(Session):
            async with Session() as db:
                before = await _snapshot(db)
            changed = await _rebuild(Session)
            async with Session() as db:
                return changed, before, await _snapshot(db)
        changed, before, after = _scenario(tmp_path, "inline_unique", body)
    finally:
        del SHAPES["inline_unique"]
    assert any(i[2] == "u" for i in before[2])
    assert changed is False and after == before
    assert any("automatic index" in r.getMessage() for r in caplog.records)


class _FailOn:
    """Wraps a session; the first statement containing `needle` raises."""

    def __init__(self, db, needle):
        self._db, self._needle = db, needle

    async def execute(self, stmt, *args, **kwargs):
        if self._needle in str(stmt):
            raise RuntimeError(f"injected failure at {self._needle!r}")
        return await self._db.execute(stmt, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._db, name)


@pytest.mark.parametrize("needle", [
    "INSERT INTO",            # after the new table is created
    "RENAME TO",              # after the old table is dropped
    "CREATE UNIQUE INDEX",    # after the rename, while indexes are replayed
])
def test_a_failure_part_way_through_leaves_the_original_table(tmp_path, shape, needle):
    async def body(Session):
        async with Session() as db:
            before = await _snapshot(db)
        async with Session() as db:
            with pytest.raises(RuntimeError, match="injected failure"):
                await ensure_characters_autoincrement(_FailOn(db, needle))
        async with Session() as db:
            leftover = (await db.execute(text(
                "SELECT name FROM sqlite_master WHERE name LIKE '%rebuild%'"))).all()
            return before, await _snapshot(db), leftover
    before, after, leftover = _scenario(tmp_path, shape, body)
    assert after == before
    assert leftover == []


def test_a_failed_verification_rolls_back(tmp_path, monkeypatch):
    """The checks run before COMMIT; if one fails, nothing is kept."""
    import app.db.character_ids as mod

    async def body(Session):
        async with Session() as db:
            before = await _snapshot(db)
        real = mod._index_signature
        calls = {"n": 0}

        async def lose_an_index(db):
            calls["n"] += 1
            sig = await real(db)
            return sig if calls["n"] == 1 else sig[1:]
        monkeypatch.setattr(mod, "_index_signature", lose_an_index)
        with pytest.raises(RuntimeError, match="index"):
            await _rebuild(Session)
        async with Session() as db:
            return before, await _snapshot(db)
    before, after = _scenario(tmp_path, "altered", body)
    assert after == before


# ── Rolling back to the previous release ────────────────────────────────────

def test_the_previous_release_runs_on_a_rebuilt_table(tmp_path, shape):
    """v1.7.1 has the same columns and no AUTOINCREMENT in its model. Its
    startup ALTERs must be no-ops, its index check must find nothing to do,
    and its queries (no autoincrement in the mapped table) must still work."""
    old_md = MetaData()
    old_users = User.__table__.to_metadata(old_md)
    old_users.dialect_options["sqlite"]["autoincrement"] = False
    old_chars = Character.__table__.to_metadata(old_md)
    old_chars.dialect_options["sqlite"]["autoincrement"] = False

    async def body(Session):
        assert await _rebuild(Session) is True
        async with Session() as db:
            before = await _snapshot(db)
            for stmt in STARTUP_ALTERS:
                with pytest.raises(OperationalError, match="duplicate column"):
                    await db.execute(text(stmt))
                await db.rollback()
            # Its startup index check (_create_missing_indexes), for this table.
            conn = await db.connection()
            await conn.run_sync(lambda c: [i.create(bind=c, checkfirst=True)
                                           for i in old_chars.indexes])
            await db.commit()
            unchanged = await _snapshot(db) == before

            res = await db.execute(insert(old_chars).values(
                character_id=92_000_050, character_name="Pilot Old Release",
                access_token="at-old", refresh_token="rt-old",
                token_expiry=datetime(2026, 1, 1), scopes="", declined_scopes=""))
            await db.commit()
            new_id = res.inserted_primary_key[0]
            got = (await db.execute(select(old_chars.c.access_token, old_chars.c.refresh_token)
                                    .where(old_chars.c.character_id == 92_000_050))).one()
            return unchanged, new_id, tuple(got)
    unchanged, new_id, got = _scenario(tmp_path, shape, body)
    assert unchanged
    assert new_id == 5
    assert got == ("at-old", "rt-old")


def test_the_apps_connections_do_not_enforce_foreign_keys():
    """The rebuild drops `characters` while other tables reference it. That is
    only safe because this app's connections leave foreign_keys OFF (SQLite's
    default; the connect hook in app/db/models.py doesn't change it)."""
    from app.db.models import engine

    async def go():
        try:
            async with engine.connect() as conn:
                return (await conn.execute(text("PRAGMA foreign_keys"))).scalar()
        finally:
            await engine.dispose()
    assert _run(go()) == 0
