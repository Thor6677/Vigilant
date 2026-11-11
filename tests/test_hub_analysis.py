"""Hub comparison maths (spec section 4). Pure functions, no I/O.
Names and prices are invented."""
from datetime import date, timedelta

import pytest

from app.market import hub_analysis as ha

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


def test_nothing_listed():
    fig = _fig(_days([10] * 30), [_buy(90.0, 5)])
    assert fig["lowest_sell"] is None
    assert fig["stock"] == 0
    assert fig["days_of_stock"] is None
    assert fig["nothing_listed"] is True


def test_days_of_stock_absent_when_demand_is_zero():
    assert _fig([], [_sell(100.0, 5)])["days_of_stock"] is None
