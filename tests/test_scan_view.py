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
    assert out["counts"] == {"shown": 2, "total": 6, "ok": 3, "hidden": 1, "not_sold": 0,
                             "filtered": 0, "no_sales": 1, "no_cost": 1, "unavailable": 1}
    assert out["type_ids"] == [2001, 2, 3]
    # These rows have no by_hub (a result saved before per-hub figures were
    # kept), so choosing a hub keeps the rows whose best hub it is.
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


def hub_row(**over):
    """One hub's figures as by_hub holds them: row() less the item's keys."""
    r = row(**over)
    for k in ("type_id", "name", "status"):
        del r[k]
    return r


def item(type_id, name, best, **hubs):
    """A row as scans now store it: the best hub's figures at the top and
    every hub with a source in by_hub. `hubs` maps a hub to its overrides."""
    by_hub = {h: hub_row(hub=h, **over) for h, over in hubs.items()}
    return {"type_id": type_id, "name": name, "status": "ok", **by_hub[best], "by_hub": by_hub}


SKIPPED = [{"type_id": 4, "name": "D", "status": "no_sales"},
           {"type_id": 5, "name": "E", "status": "unavailable"},
           {"type_id": 6, "name": "F", "status": "no_cost"},
           {"type_id": 9, "name": "I", "status": "no_cost"}]


def test_counts_add_up_to_every_row_of_the_scan():
    """'Showing X of N': N is every row scanned, and shown + hidden + not
    sold at the chosen hub + filtered out + skipped + unavailable = N
    whatever the filters, for All and for each hub, both for a result with
    per-hub figures and for one saved before them."""
    old = [row(), row(type_id=2, name="B", profit_per_day=-1.0, profit_each=-1.0),
           row(type_id=3, name="C", hub="amarr", source="build", supply=None),
           row(type_id=7, name="G", hub="amarr", profit_per_day=-3.0, supply=None),
           row(type_id=8, name="H", profit_per_day=None)] + SKIPPED
    new = [item(1, "A", "jita", jita={"source": "build", "supply": None, "profit_per_day": 90.0},
                rens={}, amarr={"profit_per_day": -2.0}),
           item(2, "B", "rens", rens={"profit_per_day": 5.0}),
           item(3, "C", "amarr", amarr={"source": "build", "supply": None},
                hek={"profit_per_day": None}, dodixie={"supply": "saturated"}),
           item(7, "G", "dodixie", dodixie={"profit_per_day": -3.0, "supply": None},
                jita={"source": "build", "profit_per_day": -4.0}),
           item(8, "H", "hek", hek={"profit_per_day": 0.0}, rens={"source": "build"})] + SKIPPED
    for rows in (old, new):
        for hub in ("", *v.HUB_NAMES):
            for source in ("", "build", "haul"):
                for losers in ("", "show"):
                    for under in ("", "1"):
                        f = v.parse_filters(hub, source, losers, under)
                        c = v.build(result(rows), f, "profit_day", "")["counts"]
                        parts = (c["shown"] + c["hidden"] + c["not_sold"] + c["filtered"]
                                 + c["no_sales"] + c["no_cost"] + c["unavailable"])
                        assert c["total"] == len(rows) == parts, (f, c)
                        assert c["ok"] == 5
                        if rows is old or not hub:
                            assert c["not_sold"] == 0
    # Items with no figures at the hub are the "not sold" part, not "filtered".
    c = v.build(result(new), v.parse_filters("hek", "", "show", ""), "profit_day", "")["counts"]
    assert (c["shown"], c["not_sold"], c["filtered"]) == (2, 3, 0)


# ---- a hub chosen: every item ranked for that hub ----

def _scan():
    """Jita Hull is best in Jita (by volume) but also sells in Rens for far
    more each; Rens Hull is best in Rens; Amarr Hull sells only in Amarr."""
    return [
        item(11, "Jita Hull", "jita",
             jita={"source": "build", "cost": 50.0, "cost_basis": "benchmark", "price": 80.0,
                   "profit_each": 26.0, "margin": 0.52, "qty": 400, "qty_limit": "budget",
                   "profit_per_day": 900.0, "demand_30": 300.0, "stock": 9000,
                   "days_of_stock": 30.0, "speed": "steady", "supply": None,
                   "other": None},
             rens={"source": "haul", "cost": 120.0, "price": 400.0, "profit_each": 260.0,
                   "margin": 2.17, "qty": 3, "qty_limit": "hub", "profit_per_day": 30.0,
                   "demand_30": 0.5, "stock": 1, "days_of_stock": 2.0, "speed": "slow",
                   "supply": "under", "history_behind": True,
                   "other": {"source": "build", "profit_each": 330.0}}),
        item(12, "Rens Hull", "rens",
             rens={"profit_per_day": 80.0, "price": 170.0, "margin": 0.6},
             amarr={"profit_per_day": 40.0}),
        item(13, "Amarr Hull", "amarr", amarr={"profit_per_day": 500.0}),
    ]


ROUTES = {"jita": {"jumps": 3, "highsec": True}, "rens": {"jumps": 20, "highsec": True},
          "amarr": {"jumps": 9, "highsec": False}}


def test_choosing_a_hub_ranks_every_item_by_that_hubs_figures():
    rens = v.build(result(_scan(), ROUTES), v.parse_filters("rens", "", "", ""), "profit_day", "")
    assert names(rens) == ["Rens Hull", "Jita Hull"]           # 80 / day, then 30 / day
    d = rens["rows"][1]
    assert (d["hub"], d["source"], d["source_text"]) == ("Rens", "haul", "Haul from Jita")
    assert (d["price"], d["qty"], d["qty_limit"]) == ("400.00", "3", "hub limit · 7 days")
    assert (d["profit_each"], d["margin"], d["profit_day"]) == ("+260.00", "217.0%", "+30.00")
    assert (d["sold"], d["stock"], d["stock_days"]) == ("0.5", "1", "2 days")
    assert (d["speed"], d["supply"]) == ("slow", "under")
    assert d["sold_note"] == v.HISTORY_BEHIND_TITLE              # Rens' history lags, not Jita's
    assert d["other_text"] == "Build: +330.00 each"
    assert d["route"] == {"text": "20 j", "sub": "highsec"}
    assert d["link"] == "/market/type/11?qty=3&cost=120.00#mk-hubs"
    assert rens["counts"]["not_sold"] == 1 and rens["counts"]["shown"] == 2
    assert rens["hub_name"] == "Rens" and rens["hub_column"] == "Hub"
    assert rens["hub_line"] == ("Showing what each item earns in Rens. "
                                "Choose All to see each item's best hub.")
    # All is unchanged: each item at its best hub, ranked by that.
    every = v.build(result(_scan(), ROUTES), F, "profit_day", "")
    assert names(every) == ["Jita Hull", "Amarr Hull", "Rens Hull"]
    j = every["rows"][0]
    assert (j["hub"], j["price"], j["route"]["text"]) == ("Jita", "80.00", "3 j")
    assert j["link"] == "/market/type/11?qty=400#mk-hubs"
    assert (every["hub_column"], every["hub_line"], every["hub_name"]) == ("Best hub", "", "")
    assert every["counts"]["not_sold"] == 0
    # Amarr: Amarr Hull first, Rens Hull's Amarr figures second; Jita Hull is
    # not sold there.
    amarr = v.build(result(_scan(), ROUTES), v.parse_filters("amarr", "", "", ""),
                    "profit_day", "")
    assert names(amarr) == ["Amarr Hull", "Rens Hull"]
    assert {r["route"]["text"] for r in amarr["rows"]} == {"9 j"}
    assert amarr["counts"]["not_sold"] == 1


def test_sorting_with_a_hub_uses_that_hubs_figures():
    f = v.parse_filters("rens", "", "", "")
    # Best-hub prices are 80 (Jita Hull) and 170 (Rens Hull); in Rens they are
    # 400 and 170, which reverses the order.
    assert names(v.build(result(_scan()), f, "price", "")) == ["Jita Hull", "Rens Hull"]
    assert names(v.build(result(_scan()), F, "price", "")) == ["Rens Hull", "Amarr Hull",
                                                               "Jita Hull"]
    assert names(v.build(result(_scan()), f, "margin", "asc")) == ["Rens Hull", "Jita Hull"]
    assert names(v.build(result(_scan()), f, "qty", "")) == ["Rens Hull", "Jita Hull"]
    assert names(v.build(result(_scan()), f, "stock", "asc")) == ["Rens Hull", "Jita Hull"]


def test_filters_apply_to_the_chosen_hubs_row():
    rows = [item(21, "Loses In Rens", "jita",
                 jita={"profit_per_day": 50.0, "source": "build", "supply": None},
                 rens={"profit_per_day": -5.0, "profit_each": -1.0}),
            item(22, "Hauls To Rens", "jita", jita={"source": "build", "supply": None},
                 rens={"source": "haul", "supply": "under"}),
            item(23, "Under In Jita", "jita", jita={"source": "build", "supply": "under",
                                                    "profit_per_day": 100.0},
                 rens={"source": "build", "supply": None, "profit_per_day": 10.0})]

    def rens(source="", losers="", under=""):
        return v.build(result(rows), v.parse_filters("rens", source, losers, under),
                       "profit_day", "")

    # Hide no-profit: the Rens row loses, though the item's best makes money.
    out = rens()
    assert "Loses In Rens" not in names(out) and out["counts"]["hidden"] == 1
    assert "Loses In Rens" in names(rens(losers="show"))
    # Source: the Rens row's source, not the best's (all three build in Jita).
    assert names(rens(source="haul", losers="show")) == ["Hauls To Rens", "Loses In Rens"]
    assert names(rens(source="build")) == ["Under In Jita"]
    assert rens(source="build")["counts"]["filtered"] == 2
    # Under-supplied: the Rens row's tag. Hauls To Rens and Loses In Rens are
    # under-supplied in Rens but not at their best hub; Under In Jita the
    # other way round.
    under = rens(under="1", losers="show")
    assert names(under) == ["Hauls To Rens", "Loses In Rens"]
    assert under["counts"]["filtered"] == 1
    everywhere = v.build(result(rows), v.parse_filters("", "", "show", "1"), "profit_day", "")
    assert names(everywhere) == ["Under In Jita"]


def test_an_old_result_keeps_best_hub_rows_and_says_to_rescan():
    """Saved before by_hub: choosing a hub keeps the rows whose best hub it
    is, as before, with a line saying a rescan would show every item."""
    rows = [row(name="Rens Best"), row(type_id=2, name="Amarr Best", hub="amarr")]
    out = v.build(result(rows), v.parse_filters("rens", "", "", ""), "profit_day", "")
    assert names(out) == ["Rens Best"]
    assert out["hub_line"] == ("This result was saved before per-hub figures were kept; "
                               "rescan to see every item for Rens.")
    assert out["hub_column"] == "Best hub"
    assert (out["counts"]["filtered"], out["counts"]["not_sold"]) == (1, 0)
    every = v.build(result(rows), F, "profit_day", "")
    assert every["hub_line"] == "" and names(every) == ["Rens Best", "Amarr Best"]


def test_at_hub_reads_one_hubs_row():
    r = item(11, "Jita Hull", "jita", jita={"profit_per_day": 9.0}, rens={"qty": 3})
    here = v.at_hub(r, "rens")
    assert (here["type_id"], here["name"], here["status"]) == (11, "Jita Hull", "ok")
    assert (here["hub"], here["qty"]) == ("rens", 3) and "by_hub" not in here
    assert v.at_hub(r, "hek") is None


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


# ── unread blueprint owners ────────────────────────────────────────────────

def rec(chars=0, corps=0, **reasons):
    return {"kind": "blueprints_unread", "characters": chars, "corporations": corps,
            "reasons": reasons}


def test_summary_for_characters_only():
    got = v.unread_summary(rec(25, 0, failed=[f"Sample Pilot {i}" for i in range(18)]))
    assert got["summary"] == ("Blueprints from 18 of 25 characters couldn't be read, "
                              "so this list may be missing items.")


def test_summary_for_corporations_only_even_with_characters_considered():
    got = v.unread_summary(rec(3, 6, corporation=["Sample Corp A", "Sample Corp B"]))
    assert got["summary"] == ("Blueprints from 2 of 6 corporations couldn't be read, "
                              "so this list may be missing items.")


def test_summary_for_both():
    got = v.unread_summary(rec(25, 6, failed=[f"P{i}" for i in range(17)],
                               dead_token=["Q"], corporation=["A", "B", "C", "D"]))
    assert got["summary"] == ("Blueprints from 18 of 25 characters and 4 of 6 corporations "
                              "couldn't be read, so this list may be missing items.")


def test_summary_singular():
    got = v.unread_summary(rec(2, 1, no_scope=["Sample Pilot A"], corporation=["Sample Corp"]))
    assert got["summary"] == ("Blueprints from 1 of 2 characters and 1 of 1 corporation "
                              "couldn't be read, so this list may be missing items.")


def test_summary_when_everything_failed():
    both = v.unread_summary(rec(1, 1, failed=["A"], corporation=["Y"]))
    assert both["summary"] == "None of your characters' or corporations' blueprints could be read."
    assert both["lines"]       # the detail is kept
    only = v.unread_summary(rec(2, 0, failed=["A", "B"]))
    assert only["summary"] == "None of your characters' blueprints could be read."


def test_incomplete_owners_are_not_counted_as_unread():
    mixed = v.unread_summary(rec(5, 1, failed=["A"], incomplete=["Q"]))
    assert mixed["summary"].startswith("Blueprints from 1 of 5 characters couldn't")
    assert mixed["lines"][-1] == "List may be incomplete (over 10,000 blueprints): Q"
    only = v.unread_summary(rec(5, 1, incomplete=["Q"]))
    assert "couldn't be read" not in only["summary"]
    assert only["lines"] == ["List may be incomplete (over 10,000 blueprints): Q"]


def test_detail_lines_are_grouped_by_reason_with_sorted_names():
    got = v.unread_summary(rec(
        9, 2, incomplete=["Q"], corporation=["Sample Corp Z", "Sample Corp Y"],
        failed=["delta", "Echo", "Foxtrot"], no_scope=["C", "B"], dead_token=["A"]))
    assert got["lines"] == [
        "Need to sign in again: A",
        "Haven't shared blueprints with Vigilant: B, C",
        "Couldn't be loaded: delta, Echo, Foxtrot",
        "Corporations that couldn't be read: Sample Corp Y, Sample Corp Z",
        "List may be incomplete (over 10,000 blueprints): Q",
    ]


def test_nothing_to_report_shows_nothing():
    assert v.unread_summary(rec(3, 1)) is None
    assert v.unread_summary({"kind": "blueprints_unread"}) is None
    assert v.unread_summary({"kind": "blueprints_unread", "reasons": "junk"}) is None


def test_notes_view_keeps_plain_lines_and_old_results_as_they_were():
    assert v.notes_view(["a", "b"]) == ["a", "b"]
    assert v.notes_view(None) == [] and v.notes_view([]) == []
    got = v.notes_view([rec(1, 0, failed=["A"]), "cap note", {"kind": "other"}, 5, rec(1, 0)])
    assert got[1] == "cap note" and len(got) == 2
    assert got[0]["summary"].startswith("None of your characters'")
    assert v.build(result([]) | {"notes": ["x"]}, F, "profit_day", "")["notes"] == ["x"]
