"""Hub Scan ranking rules (spec section 4). All figures invented."""
import pytest

from app.market import scan_rules as r


def fig(**over):
    """A hub's market figures as scan_fetch.figures() gives them."""
    base = {"demand_30": 10.0, "demand_7": 10.0, "trade_days": 30, "reference": 100.0,
            "lowest_sell": 120.0, "stock": 20, "days_of_stock": 2.0,
            "nothing_listed": False, "high_30": 130.0, "ladder": [], "outliers": 0}
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
    assert r.selling_price(fig(lowest_sell=500.0, high_30=None)) == (500.0, None)
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
    f = fig(demand_30=10, demand_7=10, stock=0, lowest_sell=200.0, high_30=200.0)
    o = r.option(f, "build", 100.0, 14, "hub", 0.2, 0.03, 0.02)
    # price 200, fees 5% -> 190 - 100 = 90 each; margin 0.9
    assert o["profit_each"] == pytest.approx(90.0)
    assert o["margin"] == pytest.approx(0.9)
    # typical days (0 + 14) / 10 = 1.4 -> 10/day, but captured is 2/day
    assert o["units_per_day"] == pytest.approx(2.0)
    assert o["profit_per_day"] == pytest.approx(180.0)


def test_option_never_divides_by_less_than_a_day():
    f = fig(demand_30=100, demand_7=100, stock=0, lowest_sell=200.0, high_30=200.0)
    o = r.option(f, "build", 100.0, 1, "budget", 1.0, 0, 0)
    assert o["units_per_day"] == pytest.approx(1.0)      # 1 unit / max(0.01, 1) day


def test_option_for_unusable_hub_is_none():
    assert r.option(fig(demand_30=0), "build", 100.0, 1, "budget", 0.2, 0, 0) is None
    assert r.option(fig(lowest_sell=None, nothing_listed=True, reference=None),
                    "build", 100.0, 1, "budget", 0.2, 0, 0) is None


def test_best_picks_highest_profit_per_day_across_hubs_and_sources():
    jita = fig(lowest_sell=100.0, high_30=110.0, ladder=[(100.0, 1000)], stock=1000)
    rens = fig(lowest_sell=160.0, high_30=170.0, stock=5, demand_30=5, demand_7=5)
    out = r.best_for_item({"jita": jita, "rens": rens, "amarr": None},
                          build_cost=130.0, budget=1e6, share=0.2, tax=0.0, broker=0.0)
    assert out["status"] == "ok"
    assert (out["hub"], out["source"]) == ("rens", "haul")     # 60 each beats 30 each
    assert out["cost"] == pytest.approx(100.0) and out["cost_basis"] == "jita"
    assert out["other"] == {"source": "build", "profit_each": pytest.approx(30.0)}
    assert out["qty"] == 7 and out["qty_limit"] == "hub"       # 7 days x 20% x 5/day


def test_haul_is_not_offered_at_jita():
    jita = fig(lowest_sell=200.0, high_30=210.0, ladder=[(200.0, 50)])
    out = r.best_for_item({"jita": jita}, build_cost=100.0, budget=1e6,
                          share=0.2, tax=0, broker=0)
    assert (out["hub"], out["source"]) == ("jita", "build")
    assert out["other"] is None


def test_tie_goes_to_higher_margin_then_hub_order():
    a = fig(lowest_sell=200.0, high_30=200.0, stock=0)
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
    rens = fig(lowest_sell=160.0, high_30=170.0, stock=5, days_of_stock=1.0,
               demand_30=5, demand_7=5)
    out = r.best_for_item({"rens": rens}, build_cost=100.0, budget=1e6,
                          share=0.2, tax=0, broker=0)
    assert out["speed"] in ("fast", "steady", "slow", "thin")
    assert out["supply"] == "under"
    assert (out["demand_30"], out["stock"], out["days_of_stock"]) == (5, 5, 1.0)
    assert out["price"] == 160.0 and out["price_flag"] is None


def test_none_listed_price_assumes_no_stock_ahead():
    f = fig(lowest_sell=None, nothing_listed=True, reference=100.0, stock=50, demand_30=10, demand_7=10)
    o = r.option(f, "build", 50.0, 14, "hub", 0.2, 0, 0)
    assert o["price_flag"] == "none_listed" and o["days_typical"] == pytest.approx(1.4)


def test_losing_item_is_still_a_row_with_negative_profit():
    f = fig(lowest_sell=80.0, high_30=90.0, stock=0)
    out = r.best_for_item({"jita": f}, build_cost=100.0, budget=1e6, share=0.2, tax=0, broker=0)
    assert out["status"] == "ok" and out["profit_each"] < 0 and out["profit_per_day"] < 0


def test_thin_best_hub_is_tagged_thin():
    f = fig(trade_days=2, lowest_sell=200.0, high_30=200.0)
    out = r.best_for_item({"jita": f}, build_cost=100.0, budget=1e6, share=0.2, tax=0, broker=0)
    assert out["speed"] == "thin"


def test_haul_only_item_without_build_cost():
    jita = fig(lowest_sell=100.0, high_30=110.0, ladder=[(100.0, 100)])
    rens = fig(lowest_sell=160.0, high_30=170.0)
    out = r.best_for_item({"jita": jita, "rens": rens}, build_cost=None, budget=1e6,
                          share=0.2, tax=0, broker=0)
    assert (out["hub"], out["source"]) == ("rens", "haul") and out["other"] is None


def test_row_has_no_inf_or_nan_and_zero_cost_margin_is_none():
    import json, math
    f = fig(lowest_sell=200.0, high_30=200.0, days_of_stock=float("inf"))
    out = r.best_for_item({"jita": f}, build_cost=0.0, budget=1e6, share=0.2, tax=0, broker=0)
    assert out["margin"] is None and out["days_of_stock"] is None
    json.dumps(out, allow_nan=False)
