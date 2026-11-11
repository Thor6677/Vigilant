"""Hub Scan watchlists (app/market/watchlists.py). Names and ids are invented."""
import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, MarketScanResult, MarketWatchlist, MarketWatchlistItem, User
from app.db.sde_models import SDEType
from app.market import scan_results, watchlists
from app.market.watchlists import WatchlistError

USER_ID, OTHER_ID = 91, 92
SCANNED = datetime(2026, 11, 10, 14, 5, tzinfo=timezone.utc)
SETTINGS = scan_results.Settings(5e8, 20.0, 3.37, 1.5)


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
    """Two users; 2001 and 2002 are market types, 2003 has no market group,
    2004 is unpublished. 2005 and 2006 carry LIKE wildcards in their names."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'watchlists.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_factory() as db:
            db.add_all([User(id=USER_ID), User(id=OTHER_ID)])
            db.add_all([
                SDEType(type_id=2001, type_name="Sample Hull", published=True,
                        market_group_id=10, group_id=1),
                SDEType(type_id=2002, type_name="Alpha Sample Rig", published=True,
                        market_group_id=10, group_id=1),
                SDEType(type_id=2003, type_name="Sample Relic", published=True,
                        market_group_id=None, group_id=1),
                SDEType(type_id=2004, type_name="Sample Prototype", published=False,
                        market_group_id=10, group_id=1),
                SDEType(type_id=2005, type_name="Sample 100% Hull", published=True,
                        market_group_id=10, group_id=1),
                SDEType(type_id=2006, type_name="Sample_Under Hull", published=True,
                        market_group_id=10, group_id=1),
            ])
            await db.commit()

    _run(setup())
    yield session_factory
    _run(engine.dispose())


def _call(factory, fn, *args):
    async def go():
        async with factory() as db:
            return await fn(db, *args)
    return _run(go())


def _names(factory, user_id):
    return [w.name for w in _call(factory, watchlists.all_for, user_id)]


def _count(factory, model, *where):
    async def go():
        async with factory() as db:
            return (await db.execute(select(func.count()).select_from(model).where(*where))).scalar()
    return _run(go())


def test_create_cleans_and_limits_names(factory):
    wid = _call(factory, watchlists.create, USER_ID, "  My \t  list \n")
    assert isinstance(wid, int)
    assert _names(factory, USER_ID) == ["My list"]
    for blank in ("", "   ", None, "\x00\x01"):
        with pytest.raises(WatchlistError, match="Give the list a name"):
            _call(factory, watchlists.create, USER_ID, blank)
    long_id = _call(factory, watchlists.create, USER_ID, "x" * 90)
    long_name = next(w.name for w in _call(factory, watchlists.all_for, USER_ID) if w.id == long_id)
    assert long_name == "x" * 60
    with pytest.raises(WatchlistError, match="already have a list"):
        _call(factory, watchlists.create, USER_ID, "my LIST")
    # The same name is fine for another user.
    _call(factory, watchlists.create, OTHER_ID, "My list")
    assert _names(factory, OTHER_ID) == ["My list"]
    assert sorted(_names(factory, USER_ID)) == ["My list", "x" * 60]


def test_lists_come_by_name_whatever_the_case(factory):
    for name in ("beta", "Zulu", "Alpha"):
        _call(factory, watchlists.create, USER_ID, name)
    assert _names(factory, USER_ID) == ["Alpha", "beta", "Zulu"]


def test_cut_name_drops_a_trailing_space():
    assert watchlists.clean_name("a" * 59 + " b") == "a" * 59


def test_twenty_lists_at_most(factory, monkeypatch):
    assert watchlists.MAX_LISTS == 20
    monkeypatch.setattr(watchlists, "MAX_LISTS", 2)
    _call(factory, watchlists.create, USER_ID, "One")
    _call(factory, watchlists.create, USER_ID, "Two")
    with pytest.raises(WatchlistError, match="up to 2 watchlists"):
        _call(factory, watchlists.create, USER_ID, "Three")
    assert sorted(_names(factory, USER_ID)) == ["One", "Two"]
    _call(factory, watchlists.create, OTHER_ID, "Three")      # per user


def test_rename_and_ownership(factory):
    first = _call(factory, watchlists.create, USER_ID, "First")
    _call(factory, watchlists.create, USER_ID, "Second")
    assert _call(factory, watchlists.rename, USER_ID, first, "  Renamed   one ") == "Renamed one"
    assert sorted(_names(factory, USER_ID)) == ["Renamed one", "Second"]
    assert _call(factory, watchlists.rename, OTHER_ID, first, "Mine now") is None
    assert "Mine now" not in _names(factory, USER_ID) + _names(factory, OTHER_ID)
    with pytest.raises(WatchlistError, match="already have a list"):
        _call(factory, watchlists.rename, USER_ID, first, "SECOND")
    with pytest.raises(WatchlistError, match="Give the list a name"):
        _call(factory, watchlists.rename, USER_ID, first, "  ")
    # Its own name, or a change of case only, is fine.
    assert _call(factory, watchlists.rename, USER_ID, first, "renamed ONE") == "renamed ONE"
    assert sorted(_names(factory, USER_ID)) == ["Second", "renamed ONE"]
    assert _call(factory, watchlists.rename, USER_ID, 999, "Gone") is None


def test_add_items_validates_dedupes_and_fills_to_the_limit(factory, monkeypatch):
    assert watchlists.MAX_ITEMS == 300
    wid = _call(factory, watchlists.create, USER_ID, "Hulls")
    monkeypatch.setattr(watchlists, "MAX_ITEMS", 1)
    # 2001 twice; 2002 does not fit; 2003 (no market group), 2004 (unpublished),
    # 999 (unknown) and junk are ignored.
    added = _call(factory, watchlists.add_items, USER_ID, wid,
                  [2001, "2001", 2002, 2003, 2004, 999, "junk", -5, 2 ** 70])
    assert added == (1, 1)
    assert _call(factory, watchlists.items, wid) == [(2001, "Sample Hull")]
    assert _call(factory, watchlists.add_items, OTHER_ID, wid, [2002]) is None
    # Already on the list: nothing added, nothing left out.
    assert _call(factory, watchlists.add_items, USER_ID, wid, [2001]) == (0, 0)
    monkeypatch.setattr(watchlists, "MAX_ITEMS", 300)
    assert _call(factory, watchlists.add_items, USER_ID, wid, [2002, 2001]) == (1, 0)
    # By name, case-insensitively.
    assert _call(factory, watchlists.items, wid) == [(2002, "Alpha Sample Rig"),
                                                     (2001, "Sample Hull")]


def test_is_market_type(factory):
    assert _call(factory, watchlists.is_market_type, 2001) is True
    for t in (2003, 2004, 999, None, 0, 2 ** 40):
        assert _call(factory, watchlists.is_market_type, t) is False, t


def test_parse_id_is_bounded():
    assert watchlists.parse_id("12") == 12 and watchlists.parse_id(" 7 ") == 7
    for junk in ("", "x", "1.5", "0", "-3", None, str(2 ** 31), "99999999999999999999"):
        assert watchlists.parse_id(junk) is None, junk


async def _save(db, user_id, kind, ref):
    await scan_results.save(db, user_id, kind, ref, label="Sample", status="complete",
                            item_total=0, item_scanned=0, settings=SETTINGS, notes=[],
                            routes={}, rows=[], now=SCANNED)


def test_remove_item_and_list_and_its_result(factory):
    wid = _call(factory, watchlists.create, USER_ID, "Doomed")
    keep = _call(factory, watchlists.create, USER_ID, "Kept")
    _call(factory, watchlists.add_items, USER_ID, wid, [2001, 2002])
    _call(factory, watchlists.add_items, USER_ID, keep, [2001])
    _call(factory, _save, USER_ID, "watchlist", wid)
    _call(factory, _save, USER_ID, "watchlist", keep)
    _call(factory, _save, USER_ID, "category", 10)

    assert _call(factory, watchlists.remove_item, OTHER_ID, wid, 2001) is False
    assert _call(factory, watchlists.remove_item, USER_ID, wid, 2001) is True
    assert _call(factory, watchlists.item_ids, wid) == [2002]
    assert _call(factory, watchlists.remove_item, USER_ID, wid, 2001) is True   # not there: fine

    assert _call(factory, watchlists.remove, OTHER_ID, wid) is False
    assert _count(factory, MarketWatchlist, MarketWatchlist.id == wid) == 1
    assert _call(factory, watchlists.remove, USER_ID, wid) is True
    assert _count(factory, MarketWatchlist, MarketWatchlist.id == wid) == 0
    assert _count(factory, MarketWatchlistItem, MarketWatchlistItem.watchlist_id == wid) == 0
    assert _call(factory, scan_results.load, USER_ID, "watchlist", wid) is None
    # The other list, its items and result, and the category result stay.
    assert _call(factory, watchlists.item_ids, keep) == [2001]
    assert _call(factory, scan_results.load, USER_ID, "watchlist", keep) is not None
    assert _call(factory, scan_results.load, USER_ID, "category", 10) is not None
    assert _call(factory, watchlists.remove, USER_ID, wid) is False
    assert _count(factory, MarketScanResult) == 2


def test_get_refuses_another_users_list_and_out_of_range_ids(factory):
    wid = _call(factory, watchlists.create, USER_ID, "Mine")
    assert _call(factory, watchlists.get, USER_ID, wid).name == "Mine"
    assert _call(factory, watchlists.get, OTHER_ID, wid) is None
    assert _call(factory, watchlists.get, USER_ID, 2 ** 70) is None


def test_search_finds_market_types_and_escapes_wildcards(factory):
    found = _call(factory, watchlists.search_types, "  sample  ")
    names = [n for _t, n in found]
    # Market types only; names starting with the text first, then by name.
    assert names == ["Sample 100% Hull", "Sample Hull", "Sample_Under Hull", "Alpha Sample Rig"]
    assert _call(factory, watchlists.search_types, "rig") == [(2002, "Alpha Sample Rig")]
    assert _call(factory, watchlists.search_types, "s") == []                # too short
    # Wildcards match themselves, never everything.
    for q in ("%%", "__", "%_", "\\%", "%"):
        assert _call(factory, watchlists.search_types, q) == [], q
    assert _call(factory, watchlists.search_types, "0%") == [(2005, "Sample 100% Hull")]
    assert _call(factory, watchlists.search_types, "e_u") == [(2006, "Sample_Under Hull")]
    assert len(watchlists.clean_query("a" * 500)) == watchlists.SEARCH_MAX


def test_search_returns_at_most_fifteen(factory):
    async def many():
        async with factory() as db:
            db.add_all([SDEType(type_id=4000 + i, type_name=f"Bulk Item {i:02d}",
                                published=True, market_group_id=10, group_id=1)
                        for i in range(20)])
            await db.commit()
    _run(many())
    assert len(_call(factory, watchlists.search_types, "bulk")) == 15
