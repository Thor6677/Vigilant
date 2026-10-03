"""app/dashboard/table.py (T-076): per-pilot row builder + sort, and Table
mode's actual template render (every column, default columns, the persisted
picker, sort, and account divider rows).
"""
from datetime import datetime, timezone

import pytest

from app.dashboard import prefs as prefs_mod
from app.dashboard.table import _FAR_FUTURE_TS, build_table_row, sort_table_rows
from tests._dashboard_fixture import CHARACTERS, render_full

SUMMARY = {
    "character_id": 1001,
    "name": "Pilot One",
    "account": "Sample Corp",
    "corporation_name": "Sample Corp",
    "system_name": "Jita",
    "ship_name": "Rifter",
    "wallet": 1_000_000.0,
    "training": {"skill": "Gunnery", "level": 5, "finish_str": "3h", "queue_left_str": "1d", "progress_pct": 45, "warning": "ok"},
    "clones": {"count": 2, "cooldown_str": None},
    "sync": {"status": "idle", "last_str": "5m ago", "stale": "fresh"},
    "needs_reauth": False,
    "flags": [],
}


# ── build_table_row: every column renders something ─────────────────────────

def test_build_table_row_covers_every_declared_column():
    row = build_table_row(SUMMARY, None, None, None, None)
    for key in prefs_mod.TABLE_COLUMNS:
        assert key in row["cells"], f"missing column {key}"
        assert "text" in row["cells"][key] and "sort" in row["cells"][key]


def test_build_table_row_handles_missing_detail_gracefully():
    row = build_table_row(SUMMARY, None, {"tags": [], "note": None}, None, None)
    assert row["cells"]["pi"]["text"] == "—"
    assert row["cells"]["jobs"]["text"] == "—"
    assert row["cells"]["orders"]["text"] == "—"
    assert row["cells"]["net_worth"]["text"] == "—"


def test_build_table_row_fills_detail_columns_when_present():
    detail = {
        "pi": {"colonies": 3, "expiry_str": "2d 1h", "expired": False},
        "industry": {"active": 4, "ready": 1},
        "market": {"open_orders": 2, "escrow": 500_000.0},
        "net_worth": 12_000_000.0,
    }
    row = build_table_row(SUMMARY, detail, None, None, None)
    assert row["cells"]["pi"]["text"] == "2d 1h"
    assert row["cells"]["jobs"]["text"] == "4 active"
    assert row["cells"]["orders"]["text"] == "2 open"
    assert row["cells"]["net_worth"]["sort"] == 12_000_000.0


def test_build_table_row_tags_and_wallet_delta():
    row = build_table_row(SUMMARY, None, {"tags": ["Cyno", "Alt"], "note": None},
                           {"direction": "up", "amount": 250_000.0}, None)
    assert row["tags"] == ["Cyno", "Alt"]
    assert "250,000" in row["cells"]["wallet_7d"]["text"] or "M ISK" in row["cells"]["wallet_7d"]["text"]
    assert row["cells"]["wallet_7d"]["sort"] == 250_000.0


def test_build_table_row_last_sync_sort_uses_raw_datetime_not_display_text():
    dt = datetime(2026, 9, 20, tzinfo=timezone.utc)
    row = build_table_row(SUMMARY, None, None, None, None, last_synced=dt)
    assert row["cells"]["last_sync"]["sort"] == dt.timestamp()
    assert row["cells"]["last_sync"]["text"] == "5m ago"  # from SUMMARY's sync.last_str
    row_never = build_table_row(SUMMARY, None, None, None, None, last_synced=None)
    assert row_never["cells"]["last_sync"]["sort"] == -1.0


def test_build_table_row_queue_end_sort_uses_raw_datetime():
    dt = datetime(2026, 10, 1, tzinfo=timezone.utc)
    row = build_table_row(SUMMARY, None, None, None, dt)
    assert row["cells"]["queue_end"]["sort"] == dt.timestamp()
    row_none = build_table_row(SUMMARY, None, None, None, None)
    # A finite sentinel, not float("inf") — that round-trips through Jinja's
    # tojson as the literal string "Infinity", which the page's client-side
    # sort (parseFloat) reads back as NaN.
    assert row_none["cells"]["queue_end"]["sort"] == _FAR_FUTURE_TS


# ── sort_table_rows ──────────────────────────────────────────────────────────

def _row(name, wallet):
    s = dict(SUMMARY, character_id=hash(name) % 100000, name=name, wallet=wallet)
    return build_table_row(s, None, None, None, None)


def test_sort_table_rows_by_pilot_name_asc():
    rows = [_row("Charlie", 1), _row("Alpha", 2), _row("Bravo", 3)]
    out = sort_table_rows(rows, {"key": "pilot", "dir": "asc"})
    assert [r["cells"]["pilot"]["text"] for r in out] == ["Alpha", "Bravo", "Charlie"]


def test_sort_table_rows_by_wallet_desc():
    rows = [_row("A", 10.0), _row("B", 30.0), _row("C", 20.0)]
    out = sort_table_rows(rows, {"key": "wallet", "dir": "desc"})
    assert [r["cells"]["wallet"]["sort"] for r in out] == [30.0, 20.0, 10.0]


def test_sort_table_rows_unknown_key_falls_back_to_pilot():
    rows = [_row("Bravo", 1), _row("Alpha", 2)]
    out = sort_table_rows(rows, {"key": "not_a_real_column", "dir": "asc"})
    assert [r["cells"]["pilot"]["text"] for r in out] == ["Alpha", "Bravo"]


# ── Template render: every column, defaults, picker, sort, dividers ────────

def _table_rows_for_render(rows_data):
    return [build_table_row(s, None, tags, delta, None) for s, tags, delta in rows_data]


def test_table_mode_renders_default_columns_only():
    rows = _table_rows_for_render([(SUMMARY, None, None)])
    html = render_full(
        "custom", dash_mode="table",
        TABLE_COLUMNS=prefs_mod.TABLE_COLUMNS,
        table_rows=rows,
    )
    assert 'id="dash-table"' in html
    # Default-picked columns get a <th data-col="..."> header; a non-default
    # one (e.g. escrow) does not — the column PICKER always lists the whole
    # catalog (checked further below), so this checks the header specifically.
    for key in prefs_mod.DEFAULT_TABLE_COLUMNS:
        assert f'data-col="{key}"' in html
    for key in set(prefs_mod.TABLE_COLUMNS) - set(prefs_mod.DEFAULT_TABLE_COLUMNS):
        assert f'data-col="{key}"' not in html


def test_table_mode_column_picker_lists_the_whole_catalog():
    rows = _table_rows_for_render([(SUMMARY, None, None)])
    html = render_full(
        "custom", dash_mode="table",
        TABLE_COLUMNS=prefs_mod.TABLE_COLUMNS,
        table_rows=rows,
    )
    for key in prefs_mod.TABLE_COLUMNS:
        assert f'value="{key}"' in html


def test_table_mode_can_show_every_column_when_all_are_picked():
    rows = _table_rows_for_render([(SUMMARY, {"tags": ["Cyno"], "note": None}, None)])
    html = render_full(
        "custom", dash_mode="table",
        TABLE_COLUMNS=prefs_mod.TABLE_COLUMNS,
        table_rows=rows,
        prefs_patch={"table_columns": list(prefs_mod.TABLE_COLUMNS)},
    )
    for key in prefs_mod.TABLE_COLUMNS:
        assert f'data-col="{key}"' in html
    assert "Cyno" in html  # tag chip rendered


def test_table_mode_account_dividers_only_when_sorted_by_account():
    row_a = build_table_row(dict(SUMMARY, character_id=1, account="Alpha Corp"), None, None, None, None)
    row_b = build_table_row(dict(SUMMARY, character_id=2, account="Bravo Corp"), None, None, None, None)
    html = render_full(
        "custom", dash_mode="table",
        TABLE_COLUMNS=prefs_mod.TABLE_COLUMNS,
        table_rows=[row_a, row_b],
        prefs_patch={"table_sort": {"key": "account", "dir": "asc"}},
    )
    assert 'class="dash-table-divider"' in html
    assert "Alpha Corp" in html and "Bravo Corp" in html


def test_table_mode_no_dividers_when_sorted_by_a_different_column():
    row_a = build_table_row(dict(SUMMARY, character_id=1, account="Alpha Corp"), None, None, None, None)
    html = render_full(
        "custom", dash_mode="table",
        TABLE_COLUMNS=prefs_mod.TABLE_COLUMNS,
        table_rows=[row_a],
        prefs_patch={"table_sort": {"key": "pilot", "dir": "asc"}},
    )
    assert 'class="dash-table-divider"' not in html


def test_table_mode_empty_state():
    html = render_full(
        "custom", dash_mode="table",
        TABLE_COLUMNS=prefs_mod.TABLE_COLUMNS,
        table_rows=[],
    )
    assert "No pilots to show" in html


# ── ISS-087 / mobile design §5.8: training-state fixes ─────────────────────

@pytest.mark.parametrize("warning", ["ok", "warning", "critical"])
def test_queue_end_text_is_the_whole_queue_while_training(warning):
    """Queue End is when the WHOLE queue runs out (queue_left_str "1d"), not
    when the current skill finishes (finish_str "3h")."""
    s = dict(SUMMARY, training=dict(SUMMARY["training"], warning=warning))
    dt = datetime(2026, 10, 1, tzinfo=timezone.utc)
    row = build_table_row(s, None, None, None, dt)
    assert row["cells"]["queue_end"]["text"] == "1d"
    assert row["cells"]["queue_end"]["sort"] == dt.timestamp()


@pytest.mark.parametrize("warning", ["paused", "empty", "no_scope", "error"])
def test_queue_end_is_a_dash_and_sorts_last_unless_training(warning):
    # queue_left_str is deliberately still "1d" here: the gate, not missing
    # data, is what has to produce the dash.
    s = dict(SUMMARY, training=dict(SUMMARY["training"], warning=warning))
    row = build_table_row(s, None, None, None, datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert row["cells"]["queue_end"]["text"] == "—"
    assert row["cells"]["queue_end"]["sort"] == _FAR_FUTURE_TS


def test_training_column_reads_paused_even_with_a_next_skill():
    """A paused queue still has a first skill (the skill-queue processor sets
    current_skill from it), which used to win over the paused branch."""
    s = dict(SUMMARY, training=dict(SUMMARY["training"], warning="paused",
                                    finish_str=None, queue_left_str=None))
    row = build_table_row(s, None, None, None, None)
    assert row["cells"]["training"]["text"] == "Paused"


# ── Task 7b: paused pilots are not "training" (ISS-087) ─────────────────────

import types

from app.routes import dashboard as dash_mod
from tests._dashboard_fixture import build_context


def _env():
    return dash_mod.templates.env


def test_group_header_does_not_count_paused_pilots_as_training():
    chars = [types.SimpleNamespace(character_id=1), types.SimpleNamespace(character_id=2)]
    skill_map = {
        1: {"current_skill": "Gunnery", "warning": "ok", "queue_end": None},
        # Real paused data: the queue has entries, so current_skill is set.
        2: {"current_skill": "Navigation", "warning": "paused", "queue_end": None},
    }
    tmpl = _env().from_string(
        '{% from "partials/dashboard_group_header.html" import group_header %}'
        '{{ group_header("Main", chars, {}, skill_map, {}, True, False) }}')
    html = tmpl.render(chars=chars, skill_map=skill_map)
    assert "training 1/2" in html


def test_compact_row_shows_paused_even_with_a_next_skill():
    s = dict(build_context("custom")["pilot_summaries"][1004])
    s["training"] = {**s["training"], "skill": "Navigation", "level": 3, "warning": "paused"}
    tmpl = _env().from_string(
        '{% from "partials/dashboard_compact_row.html" import pilot_row %}{{ pilot_row(s) }}')
    html = tmpl.render(s=s)
    # The training cell is followed by the flags cell (both before and after
    # Task 10, which only adds an inline flags copy inside the name cell).
    training = html.split('class="dash-compact-training"', 1)[1].split('class="dash-compact-flags"', 1)[0]
    assert "Paused" in training
    assert "Navigation" not in training


def test_training_sort_ranks_active_then_paused_then_empty():
    def row(warning, skill):
        summary = {**SUMMARY, "training": {**SUMMARY["training"], "warning": warning, "skill": skill}}
        return build_table_row(summary, None, None, None, None)["cells"]["training"]["sort"]
    active_a = row("ok", "Armor")
    active_z = row("critical", "Zebra")
    paused = row("paused", "Aaa First Alphabetically")
    empty = row("empty", None)
    assert sorted([empty, paused, active_z, active_a]) == [active_a, active_z, paused, empty]


def test_training_rank_orders_states():
    from app.dashboard.table import training_rank
    assert [training_rank(w) for w in ("ok", "warning", "critical", "paused", "empty", "no_scope", "error", None)] == [0, 0, 0, 1, 2, 3, 3, 3]


def test_page_level_training_sort_uses_training_rank():
    # /dashboard?sort=training (Cards/Compact/Detailed ordering) must use the
    # same state rule as the Table column, not "has a current_skill".
    import inspect
    from app.routes import dashboard as dash_routes
    src = inspect.getsource(dash_routes)
    branch = src.split('elif sort == "training":', 1)[1].split("elif sort ==", 1)[0]
    assert "training_rank(" in branch
    assert "current_skill" not in branch
