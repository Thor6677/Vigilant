"""Hub Scan ranking rules (spec section 4). All figures invented."""
import pytest

from app.market import scan_rules as r


def fig(**over):
    """A hub's market figures as scan_fetch.figures() gives them."""
    base = {"demand_30": 10.0, "demand_7": 10.0, "trade_days": 30, "reference": 100.0,
            "lowest_sell": 120.0, "stock": 20, "days_of_stock": 2.0,
            "nothing_listed": False, "avg_high_30": 130.0, "ladder": [], "outliers": 0}
    base.update(over)
    return base


def test_captured_uses_the_lower_rate():
    assert r.captured(fig(demand_30=10, demand_7=4), 0.2) == pytest.approx(0.8)
    assert r.captured(fig(demand_30=4, demand_7=10), 0.2) == pytest.approx(0.8)
    assert r.captured(fig(demand_7=0), 0.2) == 0


def test_selling_price_plain_capped_and_none_listed():
    assert r.selling_price(fig()) == (120.0, None)
    # 1.25 x 130 = 162.5; exactly on the cap is not capped
    assert r.selling_price(fig(lowest_sell=162.5)) == (162.5, None)
    assert r.selling_price(fig(lowest_sell=500.0)) == (162.5, "capped")
    assert r.selling_price(fig(lowest_sell=500.0, avg_high_30=None)) == (500.0, None)
    assert r.selling_price(fig(lowest_sell=None, nothing_listed=True)) == (100.0, "none_listed")
    assert r.selling_price(fig(lowest_sell=None, nothing_listed=True, reference=None)) == (None, None)


def test_hub_limit_is_seven_days_of_captured_at_least_one():
    assert r.hub_limit(2.0) == 14
    assert r.hub_limit(0.1) == 1
    assert r.hub_limit(0) == 1


def test_build_quantity_and_its_label():
    assert r.build_quantity(1000, 100, 14) == (10, "budget")
    assert r.build_quantity(10_000, 100, 14) == (14, "hub")
    assert r.build_quantity(50, 100, 14) == (1, "over_budget")
    assert r.build_quantity(1400, 100, 14) == (14, "budget")


def test_haul_walks_the_ladder():
    ladder = [(100.0, 3), (104.0, 5), (109.0, 50)]
    # budget stops it: 3x100 + 5x104 = 820, a 9th unit at 109 would make 929
    assert r.haul_quantity(ladder, 900, 99) == (8, pytest.approx(102.5), "budget")
    # hub limit stops it
    assert r.haul_quantity(ladder, 1e9, 5) == (5, pytest.approx(101.6), "hub")
    # stock runs out
    assert r.haul_quantity(ladder, 1e9, 999) == (58, pytest.approx((300 + 520 + 5450) / 58), "jita")
    # one unit is always taken
    assert r.haul_quantity(ladder, 10, 99) == (1, 100.0, "over_budget")
    assert r.haul_quantity([], 1e9, 99) is None


def test_option_profit_per_day_is_capped_by_captured_rate():
    f = fig(demand_30=10, demand_7=10, stock=0, lowest_sell=200.0, avg_high_30=200.0)
    o = r.option(f, "build", 100.0, 14, "hub", 0.2, 0.03, 0.02)
    # price 200, fees 5% -> 190 - 100 = 90 each; margin 0.9
    assert o["profit_each"] == pytest.approx(90.0)
    assert o["margin"] == pytest.approx(0.9)
    # typical days (0 + 14) / 10 = 1.4 -> 10/day, but captured is 2/day
    assert o["units_per_day"] == pytest.approx(2.0)
    assert o["profit_per_day"] == pytest.approx(180.0)


def test_option_never_divides_by_less_than_a_day():
    f = fig(demand_30=100, demand_7=100, stock=0, lowest_sell=200.0, avg_high_30=200.0)
    o = r.option(f, "build", 100.0, 1, "budget", 1.0, 0, 0)
    assert o["units_per_day"] == pytest.approx(1.0)      # 1 unit / max(0.01, 1) day


def test_option_for_unusable_hub_is_none():
    assert r.option(fig(demand_30=0), "build", 100.0, 1, "budget", 0.2, 0, 0) is None
    assert r.option(fig(lowest_sell=None, nothing_listed=True, reference=None),
                    "build", 100.0, 1, "budget", 0.2, 0, 0) is None


def test_best_picks_highest_profit_per_day_across_hubs_and_sources():
    jita = fig(lowest_sell=100.0, avg_high_30=110.0, ladder=[(100.0, 1000)], stock=1000)
    rens = fig(lowest_sell=160.0, avg_high_30=170.0, stock=5, demand_30=5, demand_7=5)
    out = r.best_for_item({"jita": jita, "rens": rens, "amarr": None},
                          build_cost=130.0, budget=1e6, share=0.2, tax=0.0, broker=0.0)
    assert out["status"] == "ok"
    assert (out["hub"], out["source"]) == ("rens", "haul")     # 60 each beats 30 each
    assert out["cost"] == pytest.approx(100.0) and out["cost_basis"] == "jita"
    assert out["other"] == {"source": "build", "profit_each": pytest.approx(30.0)}
    assert out["qty"] == 7 and out["qty_limit"] == "hub"       # 7 days x 20% x 5/day


def test_haul_is_not_offered_at_jita():
    jita = fig(lowest_sell=200.0, avg_high_30=210.0, ladder=[(200.0, 50)])
    out = r.best_for_item({"jita": jita}, build_cost=100.0, budget=1e6,
                          share=0.2, tax=0, broker=0)
    assert (out["hub"], out["source"]) == ("jita", "build")
    assert out["other"] is None


def test_tie_goes_to_higher_margin_then_hub_order():
    a = fig(lowest_sell=200.0, avg_high_30=200.0, stock=0)
    out = r.best_for_item({"amarr": dict(a), "rens": dict(a)}, build_cost=100.0,
                          budget=1e9, share=0.2, tax=0, broker=0)
    assert out["hub"] == "amarr"


def test_statuses_for_items_with_nothing_to_show():
    no_sales = r.best_for_item({"jita": fig(demand_30=0, demand_7=0)}, build_cost=100.0,
                               budget=1e6, share=0.2, tax=0, broker=0)
    assert no_sales == {"status": "no_sales"}
    no_cost = r.best_for_item({"jita": fig(ladder=[])}, build_cost=None,
                              budget=1e6, share=0.2, tax=0, broker=0)
    assert no_cost == {"status": "no_cost"}


def test_row_carries_tags_and_market_figures():
    rens = fig(lowest_sell=160.0, avg_high_30=170.0, stock=5, days_of_stock=1.0,
               demand_30=5, demand_7=5)
    out = r.best_for_item({"rens": rens}, build_cost=100.0, budget=1e6,
                          share=0.2, tax=0, broker=0)
    # qty 7 (hub limit), stock 5, 5/day: (5 + 7) / 5 = 2.4 days, <= 5 -> fast
    assert out["speed"] == "fast"
    assert out["supply"] == "under"
    assert (out["demand_30"], out["stock"], out["days_of_stock"]) == (5, 5, 1.0)
    assert out["price"] == 160.0 and out["price_flag"] is None


def test_none_listed_price_assumes_no_stock_ahead():
    f = fig(lowest_sell=None, nothing_listed=True, reference=100.0, stock=50, demand_30=10, demand_7=10)
    o = r.option(f, "build", 50.0, 14, "hub", 0.2, 0, 0)
    assert o["price_flag"] == "none_listed" and o["days_typical"] == pytest.approx(1.4)


def test_losing_item_is_still_a_row_with_negative_profit():
    f = fig(lowest_sell=80.0, avg_high_30=90.0, stock=0)
    out = r.best_for_item({"jita": f}, build_cost=100.0, budget=1e6, share=0.2, tax=0, broker=0)
    assert out["status"] == "ok" and out["profit_each"] < 0 and out["profit_per_day"] < 0


def test_thin_best_hub_is_tagged_thin():
    f = fig(trade_days=2, lowest_sell=200.0, avg_high_30=200.0)
    out = r.best_for_item({"jita": f}, build_cost=100.0, budget=1e6, share=0.2, tax=0, broker=0)
    assert out["speed"] == "thin"


def test_haul_only_item_without_build_cost():
    jita = fig(lowest_sell=100.0, avg_high_30=110.0, ladder=[(100.0, 100)])
    rens = fig(lowest_sell=160.0, avg_high_30=170.0)
    out = r.best_for_item({"jita": jita, "rens": rens}, build_cost=None, budget=1e6,
                          share=0.2, tax=0, broker=0)
    assert (out["hub"], out["source"]) == ("rens", "haul") and out["other"] is None


def test_row_has_no_inf_or_nan_and_zero_cost_margin_is_none():
    import json, math
    f = fig(lowest_sell=200.0, avg_high_30=200.0, days_of_stock=float("inf"))
    out = r.best_for_item({"jita": f}, build_cost=0.0, budget=1e6, share=0.2, tax=0, broker=0)
    assert out["margin"] is None and out["days_of_stock"] is None
    json.dumps(out, allow_nan=False)


def test_whole_results_are_not_floored_one_short():
    # 7 x 0.2 x (305 / 7) is 61 on paper but 60.99999999999999 in floats
    assert r.hub_limit(0.2 * (305 / 7)) == 61
    assert r.build_quantity(2273369.28, 2315.04, 10**9) == (982, "budget")


def test_haul_exact_budget_takes_the_last_unit_and_never_overspends():
    ladder = [(1118.22, 5), (1151.65, 1)]
    budget = 6742.75
    units, avg, label = r.haul_quantity(ladder, budget, 99)
    assert units == 6 and label == "jita"
    assert units * avg <= budget + 0.01


def test_build_with_no_cost_is_limited_by_the_hub():
    assert r.build_quantity(1000, 0, 14) == (14, "hub")
    assert r.build_quantity(1000, -5.0, 3) == (3, "hub")


def test_capped_price_has_no_stock_ahead():
    kw = dict(demand_30=10, demand_7=10, lowest_sell=500.0, avg_high_30=130.0)
    big = r.option(fig(stock=500, **kw), "build", 50.0, 14, "hub", 0.2, 0, 0)
    none = r.option(fig(stock=0, **kw), "build", 50.0, 14, "hub", 0.2, 0, 0)
    assert big["price_flag"] == "capped"
    assert big["days_typical"] == pytest.approx(none["days_typical"]) == pytest.approx(1.4)
    assert big["units_per_day"] == pytest.approx(none["units_per_day"])


def test_tie_on_profit_per_day_goes_to_the_higher_margin():
    amarr = fig(lowest_sell=150.0, avg_high_30=150.0, stock=0, demand_30=10, demand_7=10)
    rens = fig(lowest_sell=200.0, avg_high_30=200.0, stock=0, demand_30=5, demand_7=5)
    # amarr: 50 each x 2/day = 100; rens: 100 each x 1/day = 100; margins 0.5 vs 1.0
    out = r.best_for_item({"amarr": amarr, "rens": rens}, build_cost=100.0,
                          budget=1e9, share=0.2, tax=0, broker=0)
    assert out["hub"] == "rens"
    assert out["profit_per_day"] == pytest.approx(100.0)


LADDER = [(100.0, 3), (104.0, 5), (109.0, 50)]


def test_haul_stop_label_order_when_limits_coincide():
    assert r.haul_quantity(LADDER, 1e9, 3) == (3, 100.0, "hub")      # hub limit met exactly at a level's end
    assert r.haul_quantity(LADDER, 820, 99)[2] == "budget"           # budget ends before stock does
    assert r.haul_quantity([(100.0, 3)], 300, 99)[2] == "jita"       # all stock taken, budget exact
    assert r.haul_quantity([(100.0, 0), (101.0, 2)], 1e9, 99) == (2, 101.0, "jita")


def test_capped_haul_end_to_end():
    jita = fig(demand_30=0, demand_7=0, ladder=[(1000.0, 2), (1050.0, 10)])
    amarr = fig(lowest_sell=2000.0, avg_high_30=1500.0, demand_30=3, demand_7=3, stock=4)
    out = r.best_for_item({"jita": jita, "amarr": amarr}, build_cost=None, budget=2500,
                          share=0.2, tax=0.04, broker=0.03)
    # price: 2000 > 1.25 x 1500 = 1875 -> 1875, capped, so stock (4) counts as 0
    # captured = 0.2 x 3 = 0.6/day; hub limit floor(7 x 0.6 = 4.2) = 4
    # haul: 2 @ 1000 = 2000; then 500 left buys 0 @ 1050 -> 2 units, avg 1000, "budget"
    # profit_each = 1875 x (1 - 0.07) - 1000 = 1743.75 - 1000 = 743.75
    # days_typical = (0 + 2) / 3 = 0.667; units/day = min(2 / max(0.667, 1), 0.6) = 0.6
    # profit_per_day = 743.75 x 0.6 = 446.25
    assert (out["hub"], out["source"]) == ("amarr", "haul")
    assert out["price_flag"] == "capped" and out["price"] == pytest.approx(1875.0)
    assert out["qty"] == 2 and out["qty_limit"] == "budget"
    assert out["cost"] == pytest.approx(1000.0)
    assert out["profit_each"] == pytest.approx(743.75)
    assert out["profit_per_day"] == pytest.approx(446.25)


def test_no_sales_in_seven_days_earns_nothing_per_day():
    f = fig(demand_30=10, demand_7=0, stock=0, lowest_sell=200.0, avg_high_30=200.0)
    o = r.option(f, "build", 100.0, 1, "budget", 0.2, 0, 0)
    assert o["profit_each"] > 0 and o["profit_per_day"] == 0


# ---- the price cap's basis: one spike trade must not lift it ----

def _hub_figs(station, averages, highest, ladder):
    """scan_fetch.figures() for 30 days of history ending yesterday (the last
    day first in `averages` / `highest`) and a stored sell ladder."""
    import json
    from datetime import datetime, timedelta, timezone

    from app.db.models import MarketScanSummary
    from app.market import scan_fetch

    now = datetime(2026, 11, 10, 14, 0, tzinfo=timezone.utc)
    days = [[(now.date() - timedelta(days=n + 1)).isoformat(), 5, averages[n], highest[n]]
            for n in range(30)]
    row = MarketScanSummary(region_id=1, type_id=1, days_json=json.dumps(days),
                            history_fetched_at=now.replace(tzinfo=None),
                            ladder_json=json.dumps(ladder), ladder_truncated=False,
                            orders_fetched_at=now.replace(tzinfo=None))
    return scan_fetch.figures(row, station, now)


def test_one_spike_day_does_not_lift_the_price_cap():
    """Amarr: one trade at 6M set that day's high, but the day's average stayed
    near 600k, and one listing sits at 6M. The cap is 1.25 x the highest daily
    average (605k): Amarr is priced at 756,250, never 6M, and Rens, listed at
    800k under its own cap, ranks first."""
    flat = [600_000.0] * 30
    amarr = _hub_figs(60008494, [605_000.0] + flat[1:], [6_000_000.0] + [640_000.0] * 29,
                      [[6_000_000.0, 1]])
    rens = _hub_figs(60004588, [650_000.0] * 30, [700_000.0] * 30, [[800_000.0, 3]])
    jita = _hub_figs(60003760, [520_000.0] * 30, [540_000.0] * 30, [[500_000.0, 100]])
    assert amarr["avg_high_30"] == 605_000.0
    assert r.selling_price(amarr) == (pytest.approx(756_250.0), "capped")
    out = r.best_for_item({"jita": jita, "amarr": amarr, "rens": rens}, build_cost=None,
                          budget=5e8, share=0.2, tax=0, broker=0)
    assert (out["hub"], out["source"]) == ("rens", "haul")
    assert out["price"] == 800_000.0 and out["price_flag"] is None
    # Amarr on its own: still the capped price, not the 6M listing.
    alone = r.best_for_item({"jita": jita, "amarr": amarr}, build_cost=None,
                            budget=5e8, share=0.2, tax=0, broker=0)
    assert alone["hub"] == "amarr" and alone["price"] == pytest.approx(756_250.0)
    assert alone["price_flag"] == "capped"


# ---- by_hub: the best source at each hub, for the Hub filter ----

KW = dict(budget=1e6, share=0.2, tax=0.0, broker=0.0)
ROW_KEYS = {"hub", "source", "cost", "cost_basis", "price", "price_flag", "profit_each",
            "margin", "qty", "qty_limit", "profit_per_day", "demand_30", "stock",
            "days_of_stock", "speed", "supply", "other"}


def _best_part(out):
    return {k: v for k, v in out.items() if k not in ("status", "by_hub")}


def test_by_hub_has_a_row_for_every_hub_with_a_source_offered():
    jita = fig(lowest_sell=100.0, avg_high_30=110.0, ladder=[(100.0, 1000)], stock=1000)
    rens = fig(lowest_sell=160.0, avg_high_30=170.0, stock=5, demand_30=5, demand_7=5)
    amarr = fig(lowest_sell=150.0, avg_high_30=150.0)
    hek = fig(demand_30=0, demand_7=0)                      # no sales: not usable
    out = r.best_for_item({"jita": jita, "amarr": amarr, "dodixie": None, "hek": hek,
                           "rens": rens}, build_cost=130.0, **KW)
    assert set(out["by_hub"]) == {"jita", "amarr", "rens"}
    for hub, row in out["by_hub"].items():
        assert set(row) == ROW_KEYS and row["hub"] == hub
    assert set(_best_part(out)) == ROW_KEYS
    # Amarr is the best: 50 each x 2/day beats Rens' 60 each x 1/day. Every
    # hub's row is kept with its own best source and figures: Jita can only
    # build (and loses money there), Rens hauls at its own price and rate.
    assert out["hub"] == "amarr" and out["profit_per_day"] == pytest.approx(100.0)
    j = out["by_hub"]["jita"]
    assert (j["source"], j["other"], j["price"]) == ("build", None, 100.0)
    assert j["profit_each"] == pytest.approx(-30.0) and j["profit_per_day"] < 0
    a = out["by_hub"]["amarr"]
    assert (a["source"], a["cost_basis"]) == ("haul", "jita")
    assert a["other"] == {"source": "build", "profit_each": pytest.approx(20.0)}
    assert (a["demand_30"], a["stock"], a["days_of_stock"]) == (10.0, 20, 2.0)
    s = out["by_hub"]["rens"]
    assert (s["source"], s["price"], s["qty"], s["qty_limit"]) == ("haul", 160.0, 7, "hub")
    assert s["profit_each"] == pytest.approx(60.0) and s["profit_per_day"] == pytest.approx(60.0)
    assert s["other"] == {"source": "build", "profit_each": pytest.approx(30.0)}
    assert (s["demand_30"], s["stock"], s["supply"]) == (5, 5, "under")


def test_the_best_is_a_copy_of_its_hubs_row():
    """The top-level row and by_hub[best] come from one code path: they
    cannot disagree, in any of these cases."""
    jita = fig(lowest_sell=100.0, avg_high_30=110.0, ladder=[(100.0, 1000)], stock=1000)
    rens = fig(lowest_sell=160.0, avg_high_30=170.0, stock=5, demand_30=5, demand_7=5)
    cases = [
        ({"jita": jita, "rens": rens, "amarr": None}, 130.0),
        ({"jita": fig(lowest_sell=200.0, avg_high_30=210.0, ladder=[(200.0, 50)])}, 100.0),
        ({"amarr": fig(lowest_sell=200.0, avg_high_30=200.0, stock=0),
          "rens": fig(lowest_sell=200.0, avg_high_30=200.0, stock=0)}, 100.0),
        ({"amarr": fig(lowest_sell=150.0, avg_high_30=150.0, stock=0),
          "rens": fig(lowest_sell=200.0, avg_high_30=200.0, stock=0, demand_30=5,
                      demand_7=5)}, 100.0),
        ({"jita": jita, "rens": rens}, None),
        ({"jita": fig(lowest_sell=80.0, avg_high_30=90.0, stock=0)}, 100.0),
    ]
    for figs, cost in cases:
        out = r.best_for_item(figs, build_cost=cost, **KW)
        assert out["status"] == "ok"
        assert _best_part(out) == out["by_hub"][out["hub"]], figs
    # The tie rules hold for the per-hub rows too: equal profit per day
    # goes to the higher margin, then to hub order.
    out = r.best_for_item(cases[3][0], build_cost=100.0, budget=1e9, share=0.2, tax=0, broker=0)
    assert out["hub"] == "rens" and out["by_hub"]["amarr"]["profit_per_day"] == pytest.approx(
        out["profit_per_day"])
    out = r.best_for_item(cases[2][0], build_cost=100.0, budget=1e9, share=0.2, tax=0, broker=0)
    assert out["hub"] == "amarr" and set(out["by_hub"]) == {"amarr", "rens"}


def test_a_hub_where_only_haul_is_offered():
    """No build cost: Jita sells the item but offers no source (no haul at
    Jita), so it has no row; Amarr and Rens have haul rows with no other."""
    jita = fig(lowest_sell=100.0, avg_high_30=110.0, ladder=[(100.0, 100)])
    amarr = fig(lowest_sell=130.0, avg_high_30=140.0)
    rens = fig(lowest_sell=160.0, avg_high_30=170.0)
    out = r.best_for_item({"jita": jita, "amarr": amarr, "rens": rens}, build_cost=None, **KW)
    assert set(out["by_hub"]) == {"amarr", "rens"}
    for hub in ("amarr", "rens"):
        row = out["by_hub"][hub]
        assert (row["source"], row["cost_basis"], row["other"]) == ("haul", "jita", None)
        assert row["cost"] == pytest.approx(100.0)
    assert out["by_hub"]["amarr"]["price"] == 130.0 and out["hub"] == "rens"


def test_jita_has_no_haul_in_its_row():
    jita = fig(lowest_sell=200.0, avg_high_30=210.0, ladder=[(150.0, 50)])
    rens = fig(lowest_sell=180.0, avg_high_30=190.0)
    out = r.best_for_item({"jita": jita, "rens": rens}, build_cost=100.0, **KW)
    j = out["by_hub"]["jita"]
    assert (j["source"], j["other"]) == ("build", None)
    # Rens has both sources; the other one is kept as its figure.
    assert out["by_hub"]["rens"]["other"]["source"] in ("build", "haul")
    assert out["by_hub"]["rens"]["other"]["source"] != out["by_hub"]["rens"]["source"]


def test_an_item_usable_at_only_one_hub():
    out = r.best_for_item({"jita": fig(demand_30=0, demand_7=0, ladder=[]), "amarr": fig(),
                           "dodixie": None, "hek": fig(lowest_sell=None, reference=None),
                           "rens": None}, build_cost=60.0, **KW)
    assert out["hub"] == "amarr" and list(out["by_hub"]) == ["amarr"]
    assert _best_part(out) == out["by_hub"]["amarr"]


def test_nested_hub_rows_are_json_safe():
    """An infinite days of stock and a zero cost on a hub that is not the
    best reach by_hub as None, never as inf or nan."""
    import json
    jita = fig(lowest_sell=200.0, avg_high_30=200.0, days_of_stock=float("inf"),
               demand_30=1, demand_7=1)
    rens = fig(lowest_sell=300.0, avg_high_30=300.0, days_of_stock=float("nan"))
    out = r.best_for_item({"jita": jita, "rens": rens}, build_cost=0.0, **KW)
    assert out["hub"] == "rens"
    assert out["by_hub"]["jita"]["days_of_stock"] is None
    assert out["by_hub"]["jita"]["margin"] is None
    assert out["days_of_stock"] is None
    json.dumps(out, allow_nan=False)


def test_items_with_nothing_to_show_have_no_by_hub():
    for out in (r.best_for_item({"jita": fig(demand_30=0, demand_7=0)}, build_cost=1.0, **KW),
                r.best_for_item({"jita": fig(ladder=[])}, build_cost=None, **KW)):
        assert "by_hub" not in out
