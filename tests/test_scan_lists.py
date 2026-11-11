"""Hub Scan lists: category, blueprints, watchlist. Names are invented."""
import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import scopes as perms
from app.db.models import Base, Character, MarketWatchlist, MarketWatchlistItem, User
from app.db.sde_models import SDEBlueprintInfo, SDEMarketGroup, SDEType
from app.market import scan_lists

USER_ID, OTHER_ID = 91, 92
NOW = datetime(2026, 11, 10, 12, 0)


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture
def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'lists.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_factory() as db:
            db.add_all([User(id=USER_ID), User(id=OTHER_ID)])
            db.add_all([
                SDEMarketGroup(market_group_id=10, parent_group_id=None,
                               market_group_name="Sample Ships"),
                SDEMarketGroup(market_group_id=11, parent_group_id=10,
                               market_group_name="Sample Frigates"),
                SDEMarketGroup(market_group_id=12, parent_group_id=11,
                               market_group_name="Sample Hulls"),
            ])
            db.add_all([
                SDEType(type_id=2001, type_name="Beta Hull", published=True,
                        market_group_id=12, group_id=1),
                SDEType(type_id=2002, type_name="Alpha Hull", published=True,
                        market_group_id=11, group_id=1),
                SDEType(type_id=2003, type_name="Hidden Hull", published=False,
                        market_group_id=12, group_id=1),
                SDEType(type_id=2004, type_name="Gamma", published=True,
                        market_group_id=None, group_id=1),
                SDEType(type_id=2005, type_name="Other", published=True,
                        market_group_id=99, group_id=1),
            ])
            db.add_all([
                SDEBlueprintInfo(blueprint_type_id=3001, product_type_id=2001),
                SDEBlueprintInfo(blueprint_type_id=3002, product_type_id=2002),
                SDEBlueprintInfo(blueprint_type_id=3004, product_type_id=2004),
            ])
            db.add(Character(
                character_id=501, character_name="Sample Pilot", user_id=USER_ID,
                corporation_id=98000001, corporation_name="Sample Corp",
                access_token="a", refresh_token="r", token_expiry=NOW,
                scopes=f"{perms.BLUEPRINTS} {perms.CORP_BLUEPRINTS}"))
            db.add(MarketWatchlist(id=1, user_id=USER_ID, name="Mine", created_at=NOW))
            db.add_all([MarketWatchlistItem(watchlist_id=1, type_id=t, added_at=NOW)
                        for t in (2001, 2004)])
            await db.commit()

    _run(setup())
    yield session_factory
    _run(engine.dispose())


async def _expand(factory, user_id, kind, ref):
    async with factory() as db:
        return await scan_lists.expand(db, user_id, kind, ref)


def test_category_covers_the_subtree_by_name(factory):
    e = _run(_expand(factory, USER_ID, "category", 10))
    assert e.type_ids == [2002, 2001] and e.total == 2
    assert e.label == "Sample Ships"
    assert e.names == {2002: "Alpha Hull", 2001: "Beta Hull"}
    assert _run(_expand(factory, USER_ID, "category", 12)).label == \
        "Sample Ships › Sample Frigates › Sample Hulls"


def test_category_cap_reports_the_true_total(factory, monkeypatch):
    monkeypatch.setattr(scan_lists, "CAP", 1)
    e = _run(_expand(factory, USER_ID, "category", 10))
    assert e.type_ids == [2002] and e.total == 2


def test_unknown_category_and_kind_raise(factory):
    with pytest.raises(scan_lists.ListNotFound):
        _run(_expand(factory, USER_ID, "category", 4040))
    with pytest.raises(scan_lists.ListNotFound):
        _run(_expand(factory, USER_ID, "nonsense", 0))


def test_blueprints_collapse_duplicates_and_drop_non_market(factory, monkeypatch):
    async def chars(db, char):
        return [{"type_id": 3001}, {"type_id": 3001}, {"type_id": 3004}], "tok"

    async def corp(db, corp_id, holders):
        return [{"type_id": 3002}]

    monkeypatch.setattr(scan_lists.owned_blueprints, "character_rows", chars)
    monkeypatch.setattr(scan_lists.owned_blueprints, "corporation_rows", corp)
    e = _run(_expand(factory, USER_ID, "blueprints", 0))
    assert e.type_ids == [2002, 2001] and e.notes == [] and e.blueprints is None and e.label == "My blueprints"


def test_unreadable_corporation_and_failing_character_are_recorded(factory, monkeypatch):
    async def chars(db, char):
        raise RuntimeError("token")

    async def corp(db, corp_id, holders):
        return None

    monkeypatch.setattr(scan_lists.owned_blueprints, "character_rows", chars)
    monkeypatch.setattr(scan_lists.owned_blueprints, "corporation_rows", corp)
    e = _run(_expand(factory, USER_ID, "blueprints", 0))
    assert e.type_ids == [] and e.notes == []
    assert e.blueprints == {"kind": "blueprints_unread", "characters": 1, "corporations": 1,
                            "reasons": {"failed": ["Sample Pilot"],
                                        "corporation": ["Sample Corp"]}}


def test_a_dead_token_is_skipped_with_a_note(factory, monkeypatch):
    async def dead(db, ids):
        return set(ids)

    async def chars(db, char):
        raise AssertionError("must not be asked")

    async def corp(db, corp_id, holders):
        return [{"type_id": 3002}]

    monkeypatch.setattr(scan_lists.perm_status, "dead_token_ids", dead)
    monkeypatch.setattr(scan_lists.owned_blueprints, "character_rows", chars)
    monkeypatch.setattr(scan_lists.owned_blueprints, "corporation_rows", corp)
    e = _run(_expand(factory, USER_ID, "blueprints", 0))
    assert e.type_ids == [2002]
    assert e.notes == []
    assert e.blueprints["reasons"] == {"dead_token": ["Sample Pilot"]}


def test_watchlist_must_belong_to_the_user(factory):
    assert _run(_expand(factory, USER_ID, "watchlist", 1)).type_ids == [2001]
    with pytest.raises(scan_lists.ListNotFound):
        _run(_expand(factory, OTHER_ID, "watchlist", 1))


def test_count_is_none_for_blueprints(factory):
    async def go(kind, ref):
        async with factory() as db:
            return await scan_lists.count(db, USER_ID, kind, ref)
    assert _run(go("blueprints", 0)) is None
    assert _run(go("category", 10)) == 2


def test_category_count_is_a_count_query_matching_expand(factory, monkeypatch):
    """The same subtree and filters as the expansion, without loading rows;
    an unknown category still raises."""
    async def count(ref):
        async with factory() as db:
            return await scan_lists.count(db, USER_ID, "category", ref)

    async def expanded_total(ref):
        async with factory() as db:
            return (await scan_lists.expand(db, USER_ID, "category", ref)).total

    for ref in (10, 11, 12):
        assert _run(count(ref)) == _run(expanded_total(ref)), ref
    assert _run(count(12)) == 1                      # the unpublished type is not counted
    with pytest.raises(scan_lists.ListNotFound):
        _run(count(4040))

    # The count never expands the list.
    async def no_expand(*args, **kwargs):
        raise AssertionError("count expanded the category")
    monkeypatch.setattr(scan_lists, "_category", no_expand)
    assert _run(count(10)) == 2
    # The cap does not cut the count.
    monkeypatch.setattr(scan_lists, "CAP", 1)
    assert _run(count(10)) == 2


def test_watchlist_count_still_follows_the_list(factory):
    async def go(user_id, ref):
        async with factory() as db:
            return await scan_lists.count(db, user_id, "watchlist", ref)
    assert _run(go(USER_ID, 1)) == 1                 # 2004 has no market group
    with pytest.raises(scan_lists.ListNotFound):
        _run(go(OTHER_ID, 1))


def _add_second_pilot(factory):
    async def go():
        async with factory() as db:
            db.add(Character(
                character_id=502, character_name="Second Pilot", user_id=USER_ID,
                corporation_id=98000001, corporation_name="Sample Corp",
                access_token="a", refresh_token="r", token_expiry=NOW,
                scopes=perms.BLUEPRINTS))
            await db.commit()
    _run(go())


def test_one_failing_character_does_not_stop_the_next(factory, monkeypatch):
    _add_second_pilot(factory)

    async def chars(db, char):
        if char.character_name == "Sample Pilot":
            raise RuntimeError("token")
        return [{"type_id": 3001}], "tok"

    async def corp(db, corp_id, holders):
        return []

    monkeypatch.setattr(scan_lists.owned_blueprints, "character_rows", chars)
    monkeypatch.setattr(scan_lists.owned_blueprints, "corporation_rows", corp)
    e = _run(_expand(factory, USER_ID, "blueprints", 0))
    assert e.type_ids == [2001]
    assert e.blueprints["reasons"] == {"failed": ["Sample Pilot"]}
    assert e.blueprints["characters"] == 2


def test_a_failing_reload_after_rollback_does_not_raise(factory, monkeypatch):
    _add_second_pilot(factory)

    async def chars(db, char):
        raise RuntimeError("token")

    async def corp(db, corp_id, holders):
        raise AssertionError("no holder survived, so it must not be asked")

    async def broken_refresh(self, *a, **k):
        raise RuntimeError("row gone")

    monkeypatch.setattr(scan_lists.owned_blueprints, "character_rows", chars)
    monkeypatch.setattr(scan_lists.owned_blueprints, "corporation_rows", corp)
    monkeypatch.setattr(scan_lists.AsyncSession, "refresh", broken_refresh)
    e = _run(_expand(factory, USER_ID, "blueprints", 0))
    assert e.type_ids == []
    assert sorted(e.blueprints["reasons"]["failed"]) == ["Sample Pilot", "Second Pilot"]


def test_a_full_page_records_the_owner_as_possibly_incomplete(factory, monkeypatch):
    async def chars(db, char):
        return [{"type_id": 3001}, {"type_id": 3002}], "tok"

    async def corp(db, corp_id, holders):
        return [{"type_id": 3001}, {"type_id": 3002}]

    monkeypatch.setattr(scan_lists.owned_blueprints, "PAGE_CAP", 2)
    monkeypatch.setattr(scan_lists.owned_blueprints, "character_rows", chars)
    monkeypatch.setattr(scan_lists.owned_blueprints, "corporation_rows", corp)
    e = _run(_expand(factory, USER_ID, "blueprints", 0))
    assert e.notes == []
    assert e.blueprints["reasons"] == {"incomplete": ["Sample Pilot", "Sample Corp"]}


def test_a_mix_of_outcomes_is_one_record_with_counts(factory, monkeypatch):
    """No scope, dead token, failed load, over-cap list and an all-fine
    character, plus an unreadable corporation and a fine one."""
    async def go():
        async with factory() as db:
            for cid, name, corp, scopes in (
                    (601, "Sample Pilot B", 98000002, ""),
                    (602, "Sample Pilot C", 98000002, perms.BLUEPRINTS),
                    (603, "Sample Pilot D", 98000002, perms.BLUEPRINTS),
                    (604, "Sample Pilot E", 98000002, perms.BLUEPRINTS),
                    (605, "Sample Pilot F", 98000002, perms.BLUEPRINTS)):
                db.add(Character(character_id=cid, character_name=name, user_id=USER_ID,
                                 corporation_id=corp, corporation_name="Other Corp",
                                 access_token="a", refresh_token="r", token_expiry=NOW,
                                 scopes=scopes))
            await db.commit()
    _run(go())

    async def dead(db, ids):
        return {602}

    async def chars(db, char):
        if char.character_name == "Sample Pilot D":
            raise RuntimeError("token")
        if char.character_name == "Sample Pilot E":
            return [{"type_id": 3001}, {"type_id": 3002}], "tok"
        return [{"type_id": 3001}], "tok"

    async def corp(db, corp_id, holders):
        return None if corp_id == 98000002 else [{"type_id": 3001}]

    monkeypatch.setattr(scan_lists.owned_blueprints, "PAGE_CAP", 2)
    monkeypatch.setattr(scan_lists.perm_status, "dead_token_ids", dead)
    monkeypatch.setattr(scan_lists.owned_blueprints, "character_rows", chars)
    monkeypatch.setattr(scan_lists.owned_blueprints, "corporation_rows", corp)
    e = _run(_expand(factory, USER_ID, "blueprints", 0))
    rec = e.blueprints
    assert rec["kind"] == "blueprints_unread"
    assert rec["characters"] == 6 and rec["corporations"] == 2
    assert rec["reasons"] == {
        "dead_token": ["Sample Pilot C"], "no_scope": ["Sample Pilot B"],
        "failed": ["Sample Pilot D"], "corporation": ["Other Corp"],
        "incomplete": ["Sample Pilot E"]}
    assert e.notes == []


def test_same_name_types_keep_the_lower_id_at_the_cap(factory, monkeypatch):
    async def add():
        async with factory() as db:
            db.add_all([
                SDEType(type_id=2102, type_name="Twin Hull", published=True,
                        market_group_id=12, group_id=1),
                SDEType(type_id=2101, type_name="Twin Hull", published=True,
                        market_group_id=12, group_id=1),
            ])
            await db.commit()
    _run(add())
    monkeypatch.setattr(scan_lists, "CAP", 3)
    e = _run(_expand(factory, USER_ID, "category", 10))
    # Alpha Hull, Beta Hull, then the twin with the lower id.
    assert e.type_ids == [2002, 2001, 2101] and e.total == 4
    e = _run(_expand(factory, USER_ID, "watchlist", 1))
    assert e.type_ids == [2001]
