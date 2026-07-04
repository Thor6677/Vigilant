"""Tests for the SQLite INDEXED BY perf hint on /intel/kills/search/results.

Perf incident (2026-07): killmails grew to ~60M rows (~192GB, EVERef
backfill) with no ANALYZE stats. On time-bounded, space-scoped searches
(e.g. time=7d&space=wh) SQLite's planner picked the covering
ix_killmail_system_time (solar_system_id, killmail_time) index, scanning
every J-space kill ever (~10M rows) then sorting, instead of
ix_killmails_killmail_time which would scan only the ~200-300k rows in the
time window. The fix forces the correct index via with_hint(dialect_name=
"sqlite") whenever a killmail_time lower bound is compiled.

Note: stock SQLAlchemy's SQLiteCompiler does not implement
get_from_hint_text (only MySQL/Oracle/MSSQL/Sybase do), so
with_hint(dialect_name="sqlite") silently compiles to a no-op unless
patched — app/routes/intel_kills_search.py patches it at import time.
Importing that module (done implicitly by importing _compile_search_where /
_build_search_statements below) is what makes these tests meaningful.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, Killmail
from app.routes.intel_kills_search import (
    KILLMAIL_TIME_INDEX,
    _build_search_statements,
    _compile_search_where,
    _should_show_defaulted_note,
)


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=sqlite_dialect.dialect()))


def _compile(params: dict) -> dict:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_compile_search_where(params, db=None))
    finally:
        loop.close()


# ── Compile-level tests: does the hint text actually land in the SQL? ──────

def test_time_bound_date_sort_hints_both_page_and_count():
    compiled = _compile({"time_preset": "7d", "sort": "date", "direction": "desc"})
    stmt, count_stmt = _build_search_statements(compiled, live=0, since=0)
    page_sql = _sql(stmt)
    count_sql = _sql(count_stmt)
    assert f"FROM killmails INDEXED BY {KILLMAIL_TIME_INDEX}" in page_sql
    assert f"FROM killmails INDEXED BY {KILLMAIL_TIME_INDEX}" in count_sql


def test_custom_time_start_also_counts_as_time_bound():
    compiled = _compile({
        "time_start": datetime(2026, 1, 1),
        "sort": "date",
        "direction": "desc",
    })
    assert compiled["has_time_bound"] is True
    stmt, count_stmt = _build_search_statements(compiled, live=0, since=0)
    assert f"INDEXED BY {KILLMAIL_TIME_INDEX}" in _sql(stmt)
    assert f"INDEXED BY {KILLMAIL_TIME_INDEX}" in _sql(count_stmt)


def test_time_end_alone_gets_default_lower_bound():
    # An upper bound alone would force a scan from the dawn of the table
    # forward — the same whole-table pathology a filterless search hits. The
    # 90d safety net now kicks in: has_time_bound becomes True, defaulted_time
    # is set, the hint is applied, AND the user's upper bound is preserved.
    compiled = _compile({
        "time_end": datetime(2026, 1, 1),
        "sort": "date",
        "direction": "desc",
    })
    assert compiled["has_time_bound"] is True
    assert compiled.get("defaulted_time") is True
    stmt, count_stmt = _build_search_statements(compiled, live=0, since=0)
    page_sql = _sql(stmt)
    count_sql = _sql(count_stmt)
    assert f"INDEXED BY {KILLMAIL_TIME_INDEX}" in page_sql
    assert f"INDEXED BY {KILLMAIL_TIME_INDEX}" in count_sql
    # Both the defaulted lower bound and the user's explicit upper bound land.
    assert "killmail_time >=" in count_sql
    assert "killmail_time <=" in count_sql


def test_time_end_alone_default_lower_bound_anchors_to_end_date():
    # The injected default must anchor to the user's end date, not utcnow():
    # with an old end date, utcnow()-90d would compile an impossible window
    # (>= now-90d AND <= 2020-01-01) that silently returns nothing.
    end = datetime(2020, 1, 1)
    compiled = _compile({"time_end": end, "sort": "date", "direction": "desc"})
    assert compiled["has_time_bound"] is True
    assert compiled.get("defaulted_time") is True
    lower_bounds = [
        c.right.value for c in compiled["where"]
        if getattr(getattr(c, "operator", None), "__name__", "") == "ge"
    ]
    assert lower_bounds == [end - timedelta(days=90)]  # satisfiable: lower < upper
    stmt, count_stmt = _build_search_statements(compiled, live=0, since=0)
    assert f"INDEXED BY {KILLMAIL_TIME_INDEX}" in _sql(stmt)
    assert f"INDEXED BY {KILLMAIL_TIME_INDEX}" in _sql(count_stmt)


def test_filterless_search_gets_default_time_bound_and_hint():
    compiled = _compile({"sort": "date", "direction": "desc"})
    assert compiled["has_time_bound"] is True
    assert compiled.get("defaulted_time") is True
    stmt, count_stmt = _build_search_statements(compiled, live=0, since=0)
    assert f"INDEXED BY {KILLMAIL_TIME_INDEX}" in _sql(stmt)
    assert f"INDEXED BY {KILLMAIL_TIME_INDEX}" in _sql(count_stmt)
    assert "killmail_time >=" in _sql(count_stmt)


def test_defaulted_note_only_on_initial_render():
    # The results partial is APPENDED by the consumer JS on live poll-appends
    # and Show More cursor batches (only [data-kfs-marker] is stripped —
    # intel_kills_search.html ~line 584), so the note must render only on the
    # initial request or filterless scrolling sprays it mid-feed.
    # Initial render, filterless (also the live=1&since=0 first load): shows.
    assert _should_show_defaulted_note(True, False, None) is True
    # Show More / IntersectionObserver batch (cursor set): suppressed.
    assert not _should_show_defaulted_note(True, False, "123456")
    # Live poll-append (live=1 & since=X → is_polling): suppressed.
    assert not _should_show_defaulted_note(True, True, None)
    # Not defaulted: never shows, regardless of path.
    assert not _should_show_defaulted_note(False, False, None)


def test_explicit_time_preset_is_not_defaulted():
    # A search with a real lower bound must NOT be flagged as defaulted, so
    # the "showing last 90 days" note never shows for intentionally-bounded
    # searches.
    compiled = _compile({"time_preset": "7d", "sort": "date", "direction": "desc"})
    assert compiled["has_time_bound"] is True
    assert compiled.get("defaulted_time") is False


def test_isk_sort_hints_count_only_not_page():
    # The time index can't serve an ISK-ordered scan, so only the count
    # query (which has no ORDER BY) gets the hint.
    compiled = _compile({"time_preset": "7d", "sort": "isk", "direction": "desc"})
    stmt, count_stmt = _build_search_statements(compiled, live=0, since=0)
    assert "INDEXED BY" not in _sql(stmt)
    assert f"INDEXED BY {KILLMAIL_TIME_INDEX}" in _sql(count_stmt)


def test_involved_sort_hints_count_only_not_page():
    compiled = _compile({"time_preset": "7d", "sort": "involved", "direction": "desc"})
    stmt, count_stmt = _build_search_statements(compiled, live=0, since=0)
    assert "INDEXED BY" not in _sql(stmt)
    assert f"INDEXED BY {KILLMAIL_TIME_INDEX}" in _sql(count_stmt)


def test_live_poll_never_hinted_even_with_time_bound():
    # live-poll (live and since both set) sorts by killmail_id (the PK)
    # with a killmail_id > since filter — a different access pattern that
    # should never be hinted, regardless of any time filter also present.
    compiled = _compile({"time_preset": "7d", "sort": "date", "direction": "desc"})
    stmt, count_stmt = _build_search_statements(compiled, live=1, since=123456)
    assert "INDEXED BY" not in _sql(stmt)
    assert "INDEXED BY" not in _sql(count_stmt)
    assert "killmails.killmail_id > " in _sql(stmt)


def test_live_without_since_behaves_like_normal_date_search():
    # live=1 with since=0 (falsy) is the initial live-mode page load, not
    # the polling branch — normal hinting rules apply.
    compiled = _compile({"time_preset": "7d", "sort": "date", "direction": "desc"})
    stmt, count_stmt = _build_search_statements(compiled, live=1, since=0)
    assert f"INDEXED BY {KILLMAIL_TIME_INDEX}" in _sql(stmt)
    assert f"INDEXED BY {KILLMAIL_TIME_INDEX}" in _sql(count_stmt)


# ── Behavioral test: does INDEXED BY actually execute against a real index? ─
#
# INDEXED BY is unforgiving — if the named index doesn't exist or can't
# serve the compiled WHERE shape, SQLite raises an error instead of falling
# back to a table scan. This proves the hinted statements run cleanly
# against a table built the same way models.py declares it (Column(...,
# index=True) auto-creates ix_killmails_killmail_time on Table.create()).

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


def _km(kid: int, days_ago: int, isk: float = 1e8) -> Killmail:
    return Killmail(
        killmail_id=kid,
        killmail_hash="deadbeef",
        killmail_time=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_ago),
        solar_system_id=30000142,
        victim_ship_type_id=670,
        total_value=isk,
    )


def test_hinted_statements_execute_and_filter_correctly(session_factory):
    async def run():
        async with session_factory() as s:
            s.add(_km(1, days_ago=1))    # inside 7d window
            s.add(_km(2, days_ago=20))   # outside 7d window
            await s.commit()

            compiled = await _compile_search_where(
                {"time_preset": "7d", "sort": "date", "direction": "desc"}, s
            )
            stmt, count_stmt = _build_search_statements(compiled, live=0, since=0)
            rows = (await s.execute(stmt)).scalars().all()
            result = (await s.execute(count_stmt)).one()
            return rows, result

    rows, result = asyncio.get_event_loop().run_until_complete(run())
    assert [r.killmail_id for r in rows] == [1]
    assert int(result[0]) == 1
    assert float(result[1]) == 1e8
