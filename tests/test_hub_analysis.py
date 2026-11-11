"""Hub comparison maths (spec section 4). Pure functions, no I/O.
Names and prices are invented."""
from datetime import date, timedelta

import pytest

from app.market import hub_analysis as ha
from app.market import hub_format as hf

TODAY = date(2026, 11, 10)
STATION = 60008494
OTHER_STATION = 60000001


def _days(volumes, average=100.0):
    """volumes[0] is yesterday, volumes[1] the day before, and so on.
    A None entry is a day with no history row."""
    return [
        {"date": TODAY - timedelta(days=i + 1), "volume": v, "average": average}
        for i, v in enumerate(volumes) if v is not None
    ]


def _sell(price, vol, station=STATION):
    return {"is_buy_order": False, "price": price, "volume_remain": vol,
            "location_id": station, "min_volume": 1}


def _buy(price, vol, min_volume=1, station=STATION):
    return {"is_buy_order": True, "price": price, "volume_remain": vol,
            "location_id": station, "min_volume": min_volume}


def _fig(days, orders, qty=10):
    return ha.market_figures(days, orders, STATION, qty, TODAY)


# ── demand ──────────────────────────────────────────────────────────────

def test_missing_days_count_as_zero_trades():
    fig = _fig(_days([30] * 10 + [None] * 20), [])
    assert fig["demand_30"] == pytest.approx(10.0)   # 300 units / 30 days
    assert fig["demand_7"] == pytest.approx(30.0)    # 210 units / 7 days
    assert fig["trade_days"] == 10


def test_window_is_the_30_days_ending_yesterday():
    days = [
        {"date": TODAY, "volume": 999, "average": 100.0},                       # today: excluded
        {"date": TODAY - timedelta(days=1), "volume": 30, "average": 100.0},    # in
        {"date": TODAY - timedelta(days=30), "volume": 30, "average": 100.0},   # in (oldest)
        {"date": TODAY - timedelta(days=31), "volume": 999, "average": 100.0},  # excluded
    ]
    assert _fig(days, [])["demand_30"] == pytest.approx(2.0)


def test_window_ends_the_day_before_window_end_exclusive():
    covered_to = TODAY - timedelta(days=3)
    days = [{"date": covered_to - timedelta(days=i), "volume": 100, "average": 100.0}
            for i in range(40)]
    fig = ha.market_figures(days, [], STATION, 10,
                            window_end_exclusive=covered_to + timedelta(days=1))
    assert fig["demand_30"] == pytest.approx(100.0)
    assert fig["demand_7"] == pytest.approx(100.0)
    assert fig["trend"] == "flat"


# ── how far the stored history reaches ─────────────────────────────────

def test_coverage_end_is_the_newest_row_when_it_is_newer_than_fetch_minus_two():
    rows = [TODAY - timedelta(days=5), TODAY - timedelta(days=1), TODAY - timedelta(days=3)]
    assert ha.coverage_end(rows, TODAY) == TODAY - timedelta(days=1)


def test_coverage_end_without_rows_is_two_days_before_the_fetch():
    assert ha.coverage_end([], TODAY) == TODAY - timedelta(days=2)


def test_coverage_end_with_only_old_rows_is_two_days_before_the_fetch():
    # An item that stopped trading: the fetch proves the quiet days are quiet.
    assert ha.coverage_end([TODAY - timedelta(days=60)], TODAY - timedelta(days=1)) == (
        TODAY - timedelta(days=3))


@pytest.mark.parametrize("behind, lag, state", [
    (1, 0, "current"), (2, 1, "current"), (3, 2, "behind"), (4, 3, "behind"),
    (5, 4, "stale"), (60, 59, "stale"),
])
def test_history_lag_and_state(behind, lag, state):
    covered_to = TODAY - timedelta(days=behind)
    assert ha.history_lag(covered_to, TODAY) == lag
    assert ha.history_state(covered_to, TODAY) == state


def test_reference_price_is_the_median_daily_average():
    days = [
        {"date": TODAY - timedelta(days=1), "volume": 1, "average": 100.0},
        {"date": TODAY - timedelta(days=2), "volume": 1, "average": 200.0},
        {"date": TODAY - timedelta(days=3), "volume": 1, "average": 900.0},
    ]
    assert _fig(days, [])["reference"] == 200.0


def test_no_history_means_no_reference_and_zero_demand():
    fig = _fig([], [])
    assert fig["reference"] is None
    assert fig["demand_30"] == 0.0
    assert fig["trade_days"] == 0


@pytest.mark.parametrize("d7, d30, expected", [
    (12.1, 10.0, "up"), (12.0, 10.0, "flat"), (8.0, 10.0, "flat"),
    (7.9, 10.0, "down"), (5.0, 0.0, "flat"),
])
def test_trend_boundaries(d7, d30, expected):
    assert ha.trend(d7, d30) == expected


def test_seven_day_window_includes_day_minus_7_but_not_minus_8():
    days = [
        {"date": TODAY - timedelta(days=7), "volume": 70, "average": 100.0},   # in the 7-day window
        {"date": TODAY - timedelta(days=8), "volume": 700, "average": 100.0},  # in 30-day only
    ]
    fig = _fig(days, [])
    assert fig["demand_7"] == pytest.approx(10.0)    # 70 / 7
    assert fig["demand_30"] == pytest.approx(770 / 30)


def test_no_average_anywhere_means_no_reference_and_no_outliers():
    days = [{"date": TODAY - timedelta(days=i), "volume": 10, "average": None}
            for i in range(1, 11)]
    fig = _fig(days, [_sell(1.0, 5), _sell(100.0, 5)])
    assert fig["reference"] is None
    assert fig["lowest_sell"] == 1.0
    assert fig["outliers"] == 0


# ── orders ──────────────────────────────────────────────────────────────

def test_sell_orders_under_half_the_reference_are_dropped_and_counted():
    fig = _fig(_days([10] * 30, average=200.0),
               [_sell(90.0, 5), _sell(100.0, 5), _sell(210.0, 5)])
    assert fig["lowest_sell"] == 100.0     # 100 is exactly half: kept
    assert fig["outliers"] == 1


def test_without_a_reference_no_sell_order_is_dropped():
    fig = _fig([], [_sell(1.0, 5), _sell(100.0, 5)])
    assert fig["lowest_sell"] == 1.0
    assert fig["outliers"] == 0


def test_orders_at_other_stations_are_ignored():
    fig = _fig(_days([10] * 30),
               [_sell(50.0, 5, station=OTHER_STATION), _sell(100.0, 5),
                _buy(99.0, 5, station=OTHER_STATION), _buy(90.0, 5)])
    assert fig["lowest_sell"] == 100.0
    assert fig["highest_buy"] == 90.0


def test_buy_orders_the_batch_cannot_fill_or_that_cross_are_dropped():
    fig = _fig(_days([10] * 30),
               [_sell(100.0, 5),
                _buy(95.0, 5),
                _buy(99.0, 100, min_volume=50),   # needs 50, batch is 10
                _buy(120.0, 5)],                  # above the lowest sell
               qty=10)
    assert fig["highest_buy"] == 95.0
    assert [o["price"] for o in fig["buys"]] == [95.0]


def test_stock_counts_units_within_ten_percent_of_the_lowest_sell():
    fig = _fig(_days([10] * 30),
               [_sell(100.0, 5), _sell(110.0, 7), _sell(111.0, 9)])
    assert fig["stock"] == 12
    assert fig["days_of_stock"] == pytest.approx(1.2)   # 12 / 10 a day
    assert fig["nothing_listed"] is False


def test_stock_band_excludes_an_order_just_above_ten_percent():
    fig = _fig(_days([10] * 30), [_sell(100.0, 5), _sell(110.5, 7)])
    assert fig["stock"] == 5


def test_nothing_listed():
    fig = _fig(_days([10] * 30), [_buy(90.0, 5)])
    assert fig["lowest_sell"] is None
    assert fig["stock"] == 0
    assert fig["days_of_stock"] is None
    assert fig["nothing_listed"] is True


def test_days_of_stock_absent_when_demand_is_zero():
    assert _fig([], [_sell(100.0, 5)])["days_of_stock"] is None


# ── list and wait ───────────────────────────────────────────────────────

def _base_fig(**over):
    fig = {"demand_30": 10.0, "demand_7": 10.0, "trade_days": 30, "trend": "flat",
           "reference": 100.0, "lowest_sell": 100.0, "highest_buy": 90.0,
           "outliers": 0, "stock": 12, "days_of_stock": 1.2, "nothing_listed": False}
    fig.update(over)
    return fig


def test_list_and_wait_profit_margin_and_days():
    lw = ha.list_and_wait(_base_fig(), qty=8, cost=80.0, sales_tax=0.05, broker_fee=0.01)
    assert lw["price"] == 100.0
    assert lw["profit_each"] == pytest.approx(14.0)      # 100 * 0.94 - 80
    assert lw["margin"] == pytest.approx(0.175)
    assert lw["days_best"] == pytest.approx(0.8)         # 8 / 10
    assert lw["days_typical"] == pytest.approx(2.0)      # (12 + 8) / 10
    assert lw["batch_profit"] == pytest.approx(112.0)


def test_list_and_wait_uses_the_reference_price_when_nothing_is_listed():
    fig = _base_fig(lowest_sell=None, stock=0, days_of_stock=None, nothing_listed=True)
    lw = ha.list_and_wait(fig, qty=5, cost=None, sales_tax=0.05, broker_fee=0.01)
    assert lw["price"] == 100.0
    assert lw["days_typical"] == pytest.approx(0.5)


def test_list_and_wait_without_a_cost_has_no_profit():
    lw = ha.list_and_wait(_base_fig(), qty=8, cost=None, sales_tax=0.05, broker_fee=0.01)
    assert lw["profit_each"] is None and lw["margin"] is None and lw["batch_profit"] is None
    assert lw["days_typical"] == pytest.approx(2.0)


def test_list_and_wait_with_zero_demand_has_no_days():
    lw = ha.list_and_wait(_base_fig(demand_30=0.0), qty=8, cost=80.0,
                          sales_tax=0.05, broker_fee=0.01)
    assert lw["days_best"] is None and lw["days_typical"] is None


def test_list_and_wait_with_no_price_at_all():
    fig = _base_fig(lowest_sell=None, reference=None, nothing_listed=True)
    lw = ha.list_and_wait(fig, qty=8, cost=80.0, sales_tax=0.05, broker_fee=0.01)
    assert lw["price"] is None and lw["profit_each"] is None


# ── sell now ────────────────────────────────────────────────────────────

_BUYS = [_buy(95.0, 5), _buy(90.0, 3), _buy(80.0, 10)]


def test_sell_now_walks_buy_orders_from_the_top():
    now = ha.sell_now(_BUYS, qty=10, cost=80.0, sales_tax=0.05)
    assert now["fillable"] == 10
    assert now["avg_price"] == pytest.approx(90.5)        # (475 + 270 + 160) / 10
    assert now["profit_each"] == pytest.approx(5.975)     # 90.5 * 0.95 - 80
    assert now["batch_profit"] == pytest.approx(59.75)


def test_sell_now_batch_larger_than_the_buy_side():
    now = ha.sell_now(_BUYS, qty=30, cost=None, sales_tax=0.05)
    assert now["fillable"] == 18
    assert now["profit_each"] is None


def test_sell_now_with_no_buyers():
    now = ha.sell_now([], qty=10, cost=80.0, sales_tax=0.05)
    assert now == {"fillable": 0, "avg_price": None, "profit_each": None, "batch_profit": None}


# ── tags ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("typical, expected", [
    (5.0, "fast"), (5.01, "steady"), (14.0, "steady"), (14.01, "slow"),
])
def test_speed_tag_boundaries(typical, expected):
    assert ha.tags(_base_fig(), typical)[0] == expected


@pytest.mark.parametrize("over", [{"trade_days": 14}, {"demand_30": 0.99}])
def test_thin_market_replaces_the_speed_tag(over):
    assert ha.tags(_base_fig(**over), 1.0)[0] == "thin"


@pytest.mark.parametrize("over, expected", [
    ({"days_of_stock": 2.99}, "under"),
    ({"days_of_stock": 3.0}, None),
    ({"days_of_stock": 30.0}, None),
    ({"days_of_stock": 30.01}, "saturated"),
    ({"nothing_listed": True, "lowest_sell": None, "days_of_stock": None}, "under"),
    ({"nothing_listed": True, "lowest_sell": None, "days_of_stock": None,
      "demand_30": 0.0}, None),
])
def test_supply_tag_boundaries(over, expected):
    assert ha.tags(_base_fig(**over), 1.0)[1] == expected


# ── worked example ──────────────────────────────────────────────────────

def _worked():
    days = _days([5] * 30, average=330e6)
    orders = [
        _sell(331e6, 8), _sell(335e6, 6),      # counted, inside the 10% band
        _sell(400e6, 20),                      # counted, outside the band
        _sell(100e6, 1),                       # outlier (< half of 330M)
        _buy(284e6, 4), _buy(280e6, 2),
        _buy(270e6, 10, min_volume=20),        # batch of 10 cannot fill it
    ]
    return ha.analyse_hub(days, orders, STATION, qty=10, cost=268e6,
                          sales_tax=0.0337, broker_fee=0.015, window_end_exclusive=TODAY)


def test_worked_example_figures():
    r = _worked()
    assert r["demand_30"] == pytest.approx(5.0)
    assert r["lowest_sell"] == 331e6
    assert r["outliers"] == 1
    assert r["stock"] == 14
    assert r["days_of_stock"] == pytest.approx(2.8)
    assert r["list"]["profit_each"] == pytest.approx(46_880_300, rel=1e-6)
    assert r["list"]["days_best"] == pytest.approx(2.0)
    assert r["list"]["days_typical"] == pytest.approx(4.8)
    assert r["now"]["fillable"] == 6
    assert r["now"]["avg_price"] == pytest.approx(282_666_666.67, rel=1e-6)
    assert r["now"]["profit_each"] == pytest.approx(5_140_800, rel=1e-4)
    assert (r["speed"], r["supply"]) == ("fast", "under")
    assert "buys" not in r


def test_worked_example_verdict():
    v = _worked()["verdict"]
    assert v.startswith("Sells about 5 a day with only 14 listed.")
    assert "Your 10 would take roughly 2 – 5 days." in v
    assert v.endswith("Buy orders can only take 6.")


def test_verdict_for_a_thin_market():
    days = _days([1] * 11 + [None] * 19)
    r = ha.analyse_hub(days, [_sell(100.0, 3)], STATION, qty=10, cost=None,
                       sales_tax=0.05, broker_fee=0.01, window_end_exclusive=TODAY)
    assert r["speed"] == "thin"
    assert r["verdict"].startswith(
        "Traded on 11 of the last 30 days. Treat these figures as rough.")
    assert r["verdict"].endswith("No buy orders can take your batch.")


def test_verdict_with_no_sales_at_all():
    r = ha.analyse_hub([], [], STATION, qty=10, cost=None,
                       sales_tax=0.05, broker_fee=0.01, window_end_exclusive=TODAY)
    assert r["verdict"].startswith("No sales here in the last 30 days.")


def test_verdict_flags_a_loss_on_selling_now():
    r = ha.analyse_hub(_days([10] * 30), [_sell(100.0, 50), _buy(70.0, 100)], STATION,
                       qty=10, cost=80.0, sales_tax=0.05, broker_fee=0.01,
                       window_end_exclusive=TODAY)
    assert r["verdict"].endswith("Selling to buy orders loses money against your cost.")


# ── best picks ──────────────────────────────────────────────────────────

def _res(speed, typical, profit):
    return {"speed": speed, "list": {"days_typical": typical, "profit_each": profit}}


ORDER = ("jita", "amarr", "rens")


def test_best_picks_fastest_skips_thin_hubs():
    picks = ha.best_picks({"jita": _res("steady", 8.0, 25.0),
                           "amarr": _res("fast", 4.0, 43.0),
                           "rens": _res("thin", 1.0, 56.0)}, ORDER)
    assert picks == {"fastest": "amarr", "margin": "rens"}


def test_best_picks_when_every_hub_is_thin():
    picks = ha.best_picks({"jita": _res("thin", 8.0, None),
                           "amarr": _res("thin", 4.0, None)}, ORDER)
    assert picks == {"fastest": "amarr", "margin": None}


def test_best_picks_skips_unavailable_hubs_and_breaks_ties_by_order():
    picks = ha.best_picks({"jita": _res("fast", 2.0, 10.0),
                           "amarr": _res("fast", 2.0, 10.0),
                           "rens": None}, ORDER)
    assert picks == {"fastest": "jita", "margin": "jita"}


def test_best_picks_with_nothing_usable():
    assert ha.best_picks({"jita": None}, ORDER) == {"fastest": None, "margin": None}


# ── review fixes ────────────────────────────────────────────────────────

@pytest.mark.parametrize("best, typical, expected", [
    # Sub-day means "rounds half up to 0" (under 0.5), as in the table's "<1".
    (0.4, 0.45, "under a day"),
    (0.4, 1.0, "up to 1 day"),
    (0.4, 3.0, "up to 3 days"),
    (0.5, 0.9, "roughly 1 day"),
    (1.0, 1.2, "roughly 1 day"),
    (3.0, 3.2, "roughly 3 days"),
    (2.0, 4.8, "roughly 2 \u2013 5 days"),
    (2.5, 4.5, "roughly 3 \u2013 5 days"),
])
def test_days_phrase(best, typical, expected):
    assert hf.days_phrase(best, typical) == expected


def test_verdict_for_one_unit_reads_naturally():
    r = ha.analyse_hub(_days([10] * 30), [_sell(100.0, 1)], STATION, qty=1,
                       cost=None, sales_tax=0.05, broker_fee=0.01,
                       window_end_exclusive=TODAY)
    assert "Your 1 would take under a day." in r["verdict"]


def test_sell_order_with_no_units_left_is_ignored():
    fig = _fig(_days([10] * 30), [_sell(50.0, 0)])
    assert fig["nothing_listed"] is True
    assert fig["lowest_sell"] is None
    assert fig["stock"] == 0


def test_orders_with_missing_or_negative_volume_are_ignored():
    neg = _sell(60.0, -5)
    missing = {"is_buy_order": False, "price": 70.0, "location_id": STATION,
               "min_volume": 1}
    fig = _fig(_days([10] * 30), [neg, missing, _sell(100.0, 5)])
    assert fig["lowest_sell"] == 100.0
    assert fig["stock"] == 5


def test_buy_order_with_no_units_left_is_ignored():
    fig = _fig(_days([10] * 30), [_sell(100.0, 5), _buy(95.0, 0), _buy(90.0, 5)])
    assert fig["highest_buy"] == 90.0
    assert [o["price"] for o in fig["buys"]] == [90.0]


def test_verdict_says_both_when_a_partial_fill_also_loses_money():
    r = ha.analyse_hub(_days([10] * 30), [_sell(100.0, 50), _buy(70.0, 4)], STATION,
                       qty=10, cost=80.0, sales_tax=0.05, broker_fee=0.01,
                       window_end_exclusive=TODAY)
    assert r["verdict"].endswith("Buy orders can only take 4, at a loss against your cost.")


# ── edge cases of existing behaviour ────────────────────────────────────

def test_sell_now_partial_fill_profit_is_for_the_fillable_units_only():
    now = ha.sell_now(_BUYS, qty=30, cost=80.0, sales_tax=0.05)
    assert now["fillable"] == 18
    assert now["batch_profit"] == pytest.approx(now["profit_each"] * 18)


@pytest.mark.parametrize("over", [{"trade_days": 15}, {"demand_30": 1.0}])
def test_thin_boundaries_pass_from_the_other_side(over):
    assert ha.tags(_base_fig(**over), 1.0)[0] != "thin"


def test_no_days_estimate_is_thin():
    assert ha.tags(_base_fig(), None)[0] == "thin"


def test_verdict_when_nothing_is_listed():
    r = ha.analyse_hub(_days([10] * 30), [_buy(90.0, 5)], STATION, qty=10, cost=None,
                       sales_tax=0.05, broker_fee=0.01, window_end_exclusive=TODAY)
    assert r["verdict"].startswith("Sells about 10 a day and nothing is listed.")


def test_verdict_when_saturated():
    r = ha.analyse_hub(_days([1] * 30), [_sell(100.0, 41)], STATION, qty=10, cost=None,
                       sales_tax=0.05, broker_fee=0.01, window_end_exclusive=TODAY)
    assert r["supply"] == "saturated"
    assert r["verdict"].startswith(
        "41 listed against about 1 a day: 41 days of stock ahead of you.")


def test_best_picks_margin_with_all_negative_profits_takes_the_least_negative():
    picks = ha.best_picks({"jita": _res("fast", 2.0, -30.0),
                           "amarr": _res("fast", 3.0, -5.0),
                           "rens": _res("fast", 4.0, -12.0)}, ORDER)
    assert picks["margin"] == "amarr"
