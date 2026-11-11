"""Hub Scan presentation: filters, sorting, links and words. Figures invented."""
from datetime import datetime, timedelta, timezone

from app.market import hub_view
from app.market import scan_view as v


def row(**over):
    base = {"type_id": 2001, "name": "Sample Hull", "status": "ok", "hub": "rens",
            "source": "haul", "cost": 100.0, "cost_basis": "jita", "copy_only": False,
            "price": 160.0, "price_flag": None, "profit_each": 52.0, "margin": 0.52,
            "qty": 7, "qty_limit": "hub", "profit_per_day": 52.0, "demand_30": 5.0,
            "stock": 5, "days_of_stock": 1.0, "speed": "fast", "supply": "under",
            "other": {"source": "build", "profit_each": 22.0}}
    base.update(over)
    return base


def result(rows, routes=None):
    return {"rows": rows, "routes": routes or {}}


F = v.parse_filters("", "", "", "")


def names(out):
    return [r["name"] for r in out["rows"]]


def test_default_filters_hide_items_with_no_profit_per_day_only():
    assert F == {"hub": "", "source": "", "hide_losers": True, "under_only": False}
    assert v.parse_filters("moon", "x", "show", "1") == {
        "hub": "", "source": "", "hide_losers": False, "under_only": True}
    assert v.parse_filters("amarr", "build", "hide", "0") == {
        "hub": "amarr", "source": "build", "hide_losers": True, "under_only": False}


def test_counts_and_filters():
    rows = [row(), row(type_id=2, name="B", profit_per_day=-1.0, profit_each=-1.0),
            row(type_id=3, name="C", hub="amarr", source="build", supply=None),
            {"type_id": 4, "name": "D", "status": "no_sales"},
            {"type_id": 5, "name": "E", "status": "unavailable"},
            {"type_id": 6, "name": "F", "status": "no_cost"}]
    out = v.build(result(rows), F, "profit_day", "")
    assert names(out) == ["Sample Hull", "C"]
    assert out["counts"] == {"shown": 2, "total": 6, "ok": 3, "hidden": 1, "filtered": 0,
                             "no_sales": 1, "no_cost": 1, "unavailable": 1}
    assert out["type_ids"] == [2001, 2, 3]
    only_amarr = v.build(result(rows), v.parse_filters("amarr", "", "", ""), "profit_day", "")
    assert names(only_amarr) == ["C"]
    # The loser is at Rens: unticking the box would not show it here, so it
    # is not counted as hidden by the box, but as filtered out by the hub.
    assert only_amarr["counts"]["hidden"] == 0
    assert only_amarr["counts"]["filtered"] == 2
    under = v.build(result(rows), v.parse_filters("", "", "show", "1"), "profit_day", "")
    assert names(under) == ["Sample Hull", "B"]
    assert under["counts"]["hidden"] == 0
    haul = v.build(result(rows), v.parse_filters("", "haul", "", ""), "profit_day", "")
    assert names(haul) == ["Sample Hull"] and haul["counts"]["hidden"] == 1


def test_counts_add_up_to_every_row_of_the_scan():
    """'Showing X of N': N is every row scanned, and shown + hidden +
    filtered out + skipped + unavailable = N whatever the filters."""
    rows = [row(), row(type_id=2, name="B", profit_per_day=-1.0, profit_each=-1.0),
            row(type_id=3, name="C", hub="amarr", source="build", supply=None),
            row(type_id=7, name="G", hub="amarr", profit_per_day=-3.0, supply=None),
            row(type_id=8, name="H", profit_per_day=None),
            {"type_id": 4, "name": "D", "status": "no_sales"},
            {"type_id": 5, "name": "E", "status": "unavailable"},
            {"type_id": 6, "name": "F", "status": "no_cost"},
            {"type_id": 9, "name": "I", "status": "no_cost"}]
    for hub in ("", "amarr", "rens", "jita"):
        for source in ("", "build", "haul"):
            for losers in ("", "show"):
                for under in ("", "1"):
                    f = v.parse_filters(hub, source, losers, under)
                    c = v.build(result(rows), f, "profit_day", "")["counts"]
                    parts = (c["shown"] + c["hidden"] + c["filtered"] + c["no_sales"]
                             + c["no_cost"] + c["unavailable"])
                    assert c["total"] == len(rows) == parts, (f, c)
                    assert c["ok"] == 5


def test_no_profit_per_day_figure_is_treated_as_zero():
    """A row with profit_per_day None: hidden by the box, shown last when
    the box is off, and drawn as a dash."""
    rows = [row(), row(type_id=2, name="Blank Hull", profit_per_day=None)]
    out = v.build(result(rows), F, "profit_day", "")
    assert names(out) == ["Sample Hull"] and out["counts"]["hidden"] == 1
    show = v.parse_filters("", "", "show", "")
    for sort, direction in (("profit_day", ""), ("profit_day", "asc"), ("item", ""),
                            ("margin", "")):
        assert set(names(v.build(result(rows), show, sort, direction))) == {
            "Sample Hull", "Blank Hull"}
    assert names(v.build(result(rows), show, "profit_day", "asc"))[-1] == "Blank Hull"
    d = v.display_row(row(profit_per_day=None), {}, False)
    assert (d["profit_day"], d["profit_day_sign"], d["has_profit_day"]) == ("—", "", False)
    assert v.display_row(row(), {}, False)["has_profit_day"] is True


def test_margin_uses_the_minus_sign():
    assert v.fmt_margin(0.485) == "48.5%"
    assert v.fmt_margin(-0.04) == "−4.0%" and "-" not in v.fmt_margin(-0.04)
    assert v.fmt_margin(-0.0004) == "0.0%"           # rounds to zero: no sign
    assert v.fmt_margin(None) == "—"
    assert v.display_row(row(margin=-0.25), {}, False)["margin"] == "−25.0%"


def test_zero_profit_per_day_is_hidden_too():
    """Profitable each but no sales this week: nothing a day, hidden by default."""
    rows = [row(), row(type_id=2, name="Quiet Hull", profit_per_day=0.0)]
    out = v.build(result(rows), F, "profit_day", "")
    assert names(out) == ["Sample Hull"] and out["counts"]["hidden"] == 1
    shown = v.build(result(rows), v.parse_filters("", "", "show", ""), "profit_day", "")
    assert names(shown) == ["Sample Hull", "Quiet Hull"]


def test_sorting_defaults_and_direction():
    rows = [row(name="B", profit_per_day=5.0, margin=None), row(name="A", profit_per_day=9.0)]
    assert names(v.build(result(rows), F, "profit_day", "")) == ["A", "B"]
    assert names(v.build(result(rows), F, "profit_day", "asc")) == ["B", "A"]
    assert names(v.build(result(rows), F, "item", "")) == ["A", "B"]
    assert names(v.build(result(rows), F, "item", "desc")) == ["B", "A"]
    assert names(v.build(result(rows), F, "margin", "")) == ["A", "B"]          # None last
    assert names(v.build(result(rows), F, "margin", "asc")) == ["A", "B"]       # still last
    out = v.build(result(rows), F, "nonsense", "")
    assert out["sort"] == "profit_day" and out["direction"] == "desc"
    assert v.build(result(rows), F, "item", "")["direction"] == "asc"


def test_hub_sort_is_by_name():
    rows = [row(name="R", hub="rens"), row(name="A", hub="amarr"), row(name="J", hub="jita")]
    assert names(v.build(result(rows), F, "hub", "")) == ["A", "J", "R"]


def test_item_links():
    assert v.item_link(row()) == "/market/type/2001?qty=7&cost=100.00#mk-hubs"
    assert v.item_link(row(cost=1234567.891)) == "/market/type/2001?qty=7&cost=1234567.89#mk-hubs"
    assert v.item_link(row(source="build", cost_basis="benchmark")) == "/market/type/2001?qty=7#mk-hubs"


def test_display_words():
    d = v.display_row(row(price_flag="capped", copy_only=True), {}, False)
    assert d["price_flag"] == "price capped" and d["copy_only"] is False   # haul row
    assert d["price_flag_title"].startswith("The lowest sell here")
    assert d["other_text"] == "Build: +22.00 each"
    assert d["other_card"] == "Build instead: +22.00 each"
    assert d["qty_limit"] == "hub limit · 7 days" and d["cost_basis"] == "Jita sell orders"
    assert d["route"] is None
    assert (d["speed_text"], d["supply_text"]) == ("Fast", "Under-supplied")
    assert d["stock_days"] == "1 days" and d["sold"] == "5"
    b = v.display_row(row(source="build", cost_basis="benchmark", copy_only=True,
                          other={"source": "haul", "profit_each": -3e6}), {}, False)
    assert b["copy_only"] is True and b["cost_basis"] == "benchmark"
    assert b["other_text"] == "Haul: −3.0M each"
    assert b["other_card"] == "Haul from Jita instead: −3.0M each"
    r = v.display_row(row(), {"rens": {"jumps": 20, "highsec": True}}, True)
    assert r["route"] == {"text": "20 j", "sub": "highsec"}
    assert v.display_row(row(), {"rens": {"jumps": 9, "highsec": False}}, True)["route"] == {
        "text": "9 j", "sub": "leaves highsec"}
    assert v.display_row(row(), {"rens": {"unavailable": True}}, True)["route"]["text"] == "—"
    assert v.display_row(row(), {"rens": None}, True)["route"]["text"] == "no route"


def test_signs_and_missing_figures():
    d = v.display_row(row(profit_each=-5.0, profit_per_day=-2.0, margin=None, other=None,
                          supply=None, days_of_stock=None, price_flag="none_listed"), {}, False)
    assert (d["profit_sign"], d["profit_day_sign"]) == ("neg", "neg")
    assert d["profit_each"] == "−5.00" and d["margin"] == "—"
    # other None: building is not offered at this hub (a haul row), so "n/a".
    assert d["other_text"] == "Build: n/a" and d["other_card"] == "Build: n/a"
    assert d["supply"] == ""
    assert d["stock_days"] == "" and d["price_flag"] == "none listed"
    assert v.display_row(row(profit_per_day=0.0), {}, False)["profit_day_sign"] == ""


def test_routes_note_only_when_the_planner_was_unavailable():
    rows = [row()]
    assert v.build(result(rows), F, "profit_day", "")["routes_note"] == ""
    ok = {"jita": {"jumps": 3, "highsec": True}, "rens": None}
    assert v.build(result(rows, ok), F, "profit_day", "")["routes_note"] == ""
    down = {"jita": {"unavailable": True}, "rens": {"jumps": 20, "highsec": True}}
    out = v.build(result(rows, down), F, "profit_day", "")
    assert out["routes_note"] == hub_view.ROUTES_UNAVAILABLE_NOTE and out["show_route"] is True
    assert v.build(result(rows), F, "profit_day", "")["show_route"] is False


def test_scanned_text_is_eve_time():
    aware = datetime(2026, 11, 10, 14, 5, tzinfo=timezone.utc)
    assert v.scanned_text(aware) == "2026-11-10 14:05"
    assert v.scanned_text(aware.astimezone(timezone(timedelta(hours=2)))) == "2026-11-10 14:05"
    assert v.scanned_text(datetime(2026, 11, 10, 14, 5)) == "2026-11-10 14:05"


def test_budget_field_reads_back_exactly():
    for value, text in ((5e8, "500M"), (1.5e9, "1.5B"), (2e9, "2B"), (1e6, "1M"),
                        (1234567.0, "1234567"), (1.25e8, "125M")):
        assert v.fmt_budget(value) == text
        assert hub_view.parse_isk(text) == value
    assert v.fmt_pct(20.0) == "20" and v.fmt_pct(3.37) == "3.37"


def test_the_other_source_says_n_a_only_when_it_is_not_offered():
    """A Jita build row (haul is never offered at Jita) and a haul row with no
    build cost say "n/a" for the other source; a row that has the other
    source's figure shows it; a row without the key shows nothing."""
    jita_build = v.display_row(row(hub="jita", source="build", cost_basis="benchmark",
                                   other=None), {}, False)
    assert (jita_build["other_text"], jita_build["other_card"]) == (
        "Haul: n/a", "Haul from Jita: n/a")
    haul_only = v.display_row(row(other=None), {}, False)
    assert (haul_only["other_text"], haul_only["other_card"]) == ("Build: n/a", "Build: n/a")
    assert v.display_row(row(), {}, False)["other_text"] == "Build: +22.00 each"
    bare = row()
    del bare["other"]
    assert v.display_row(bare, {}, False)["other_text"] == ""


def test_lagging_data_marks_the_sold_and_stock_figures():
    plain = v.display_row(row(), {}, False)
    assert plain["sold_note"] == "" and plain["stock_note"] == ""
    marked = v.display_row(row(history_behind=True, stock_truncated=True), {}, False)
    assert marked["sold_note"] == "Sales history for this hub is a few days behind."
    assert marked["stock_note"] == (
        "More than 300 price levels listed; stock may be understated.")
    assert v.display_row(row(history_behind=False, stock_truncated=True), {},
                         False)["sold_note"] == ""


def test_capped_price_tooltip_names_the_daily_average():
    title = v.PRICE_FLAG_TITLES["capped"]
    assert "more than 1.25× the highest daily average of the last 30 days" in title
    assert "30-day high" not in title


def test_settings_text_says_what_the_result_was_computed_with():
    s = {"budget_isk": 5e8, "share_pct": 20.0, "sales_tax_pct": 3.37,
         "broker_fee_pct": 1.5, "origin_system_id": None, "origin_name": ""}
    assert v.settings_text(s) == (
        "Budget 500M per item · market share 20% · sales tax 3.37% · broker fee 1.5%")
    s.update(budget_isk=1.5e9, share_pct=12.5, origin_system_id=30099001,
             origin_name="Sample System")
    assert v.settings_text(s) == ("Budget 1.5B per item · market share 12.5% · "
                                  "sales tax 3.37% · broker fee 1.5% · from Sample System")
