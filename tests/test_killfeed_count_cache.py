"""Tests for the /intel/kills/search count/SUM cache key (ISS-030).

Bug: _count_cache_key dropped `sort` from the cache key on the premise that
it never affects totals. True for `direction` and `cursor`, but sort=="isk"
adds a NULL guard on total_value to _compile_search_where's WHERE (killmails
with no total_value can't be sensibly ordered/paginated by ISK) — so an
identical filter set produces a different total_count/total_isk under
sort=date vs sort=isk, while both shared one cache entry. Whichever sort
populated the cache first got served to the other, silently.

Fix: derive a single boolean (_nonnull_value_only) that both the
where-builder and _count_cache_key consult, so the key tracks the thing that
actually changes the WHERE instead of a raw param that mostly doesn't.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Killmail
from app.routes.intel_kills_search import (
    _build_search_statements,
    _compile_search_where,
    _count_cache_key,
    _nonnull_value_only,
)


def _compile(params: dict) -> dict:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_compile_search_where(params, db=None))
    finally:
        loop.close()


# ── Key-level tests ─────────────────────────────────────────────────────

def test_nonnull_value_only_true_only_for_isk_sort():
    assert _nonnull_value_only({"sort": "isk"}) is True
    assert _nonnull_value_only({"sort": "date"}) is False
    assert _nonnull_value_only({"sort": "involved"}) is False
    assert _nonnull_value_only({}) is False


def test_cache_key_differs_between_isk_and_date_sort():
    # Same otherwise-identical filters, only sort differs — the NULL guard
    # makes these two searches count different rows, so they must NOT share
    # a cache entry.
    base = {"time_preset": "7d", "direction": "desc", "cursor": None}
    key_date = _count_cache_key({**base, "sort": "date"})
    key_isk = _count_cache_key({**base, "sort": "isk"})
    assert key_date != key_isk


def test_cache_key_same_for_date_and_involved_sort():
    # date and involved compile an identical WHERE (neither adds the NULL
    # guard) — they should keep sharing one cache entry rather than
    # fragmenting it for no reason.
    base = {"time_preset": "7d", "direction": "desc", "cursor": None}
    key_date = _count_cache_key({**base, "sort": "date"})
    key_involved = _count_cache_key({**base, "sort": "involved"})
    assert key_date == key_involved


def test_cache_key_ignores_direction_and_cursor():
    base = {"time_preset": "7d", "sort": "isk"}
    key_a = _count_cache_key({**base, "direction": "desc", "cursor": None})
    key_b = _count_cache_key({**base, "direction": "asc", "cursor": "12345"})
    assert key_a == key_b


def test_cache_key_still_varies_with_other_filters():
    # Sanity check the key isn't accidentally collapsed to just the sort
    # flag — unrelated filter params must still separate entries.
    key_a = _count_cache_key({"time_preset": "7d", "sort": "date", "victim_corps": [1]})
    key_b = _count_cache_key({"time_preset": "7d", "sort": "date", "victim_corps": [2]})
    assert key_a != key_b


# ── Behavioural test: the two sorts actually count different rows ──────
#
# Mirrors the session_factory pattern in test_killfeed_search_index_hint.py:
# a real in-memory SQLite table built the same way models.py declares it, so
# this exercises the compiled statements, not just the key function.

@pytest.fixture()
def session_factory():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Killmail.__table__.create(c))

    loop.run_until_complete(_init())
    yield async_sessionmaker(engine, expire_on_commit=False)
    loop.close()


def _km(kid: int, total_value) -> Killmail:
    return Killmail(
        killmail_id=kid,
        killmail_hash="deadbeef",
        killmail_time=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1),
        solar_system_id=30000142,
        victim_ship_type_id=670,
        total_value=total_value,
    )


def test_isk_sort_and_date_sort_count_different_totals(session_factory):
    # Two killmails in the window: one has a total_value, one doesn't (e.g.
    # ISK not yet backfilled). date-sort counts both; isk-sort excludes the
    # NULL row via the guard in _compile_search_where.
    async def run():
        async with session_factory() as s:
            s.add(_km(1, total_value=1e8))
            s.add(_km(2, total_value=None))
            await s.commit()

            compiled_date = await _compile_search_where(
                {"time_preset": "7d", "sort": "date", "direction": "desc"}, s
            )
            _, count_stmt_date = _build_search_statements(compiled_date, live=0, since=0)
            result_date = (await s.execute(count_stmt_date)).one()

            compiled_isk = await _compile_search_where(
                {"time_preset": "7d", "sort": "isk", "direction": "desc"}, s
            )
            _, count_stmt_isk = _build_search_statements(compiled_isk, live=0, since=0)
            result_isk = (await s.execute(count_stmt_isk)).one()

            return result_date, result_isk

    result_date, result_isk = asyncio.get_event_loop().run_until_complete(run())

    assert int(result_date[0]) == 2      # counts both rows, NULL included
    assert int(result_isk[0]) == 1       # NULL-guard drops the unbackfilled row

    # And the params that produced each result key differently, so a real
    # cache lookup couldn't have served one result to the other's request.
    key_date = _count_cache_key({"time_preset": "7d", "sort": "date", "direction": "desc"})
    key_isk = _count_cache_key({"time_preset": "7d", "sort": "isk", "direction": "desc"})
    assert key_date != key_isk
