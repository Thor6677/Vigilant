"""Hub comparison display strings. Pure functions."""
import pytest

from app.market import hub_view as hv


@pytest.mark.parametrize("v, expected", [
    (None, "—"), (312_000_000, "312.0M"), (1_250_000_000, "1.25B"),
    (45_000, "45.0K"), (950.5, "950.50"),
])
def test_fmt_isk(v, expected):
    assert hv.fmt_isk(v) == expected


@pytest.mark.parametrize("v, expected", [
    (None, "—"), (43_100_000, "+43.1M"), (-3_500_000, "−3.5M"), (0, "+0.00"),
])
def test_fmt_signed(v, expected):
    assert hv.fmt_signed(v) == expected


@pytest.mark.parametrize("best, typical, expected", [
    (None, None, "—"), (0.24, 7.6, "<1 – 8"), (1.7, 4.0, "2 – 4"), (3.0, 3.2, "3"),
])
def test_fmt_days_range(best, typical, expected):
    assert hv.fmt_days_range(best, typical) == expected


@pytest.mark.parametrize("raw, expected", [
    ("180000000", 180e6), ("180,000,000", 180e6), ("180m", 180e6), ("1.2B", 1.2e9),
    ("45k", 45e3), (" 12.5 ", 12.5), ("", None), ("abc", None), ("-5", None),
    ("nan", None), ("inf", None),
])
def test_parse_isk(raw, expected):
    assert hv.parse_isk(raw) == expected


def _result(**over):
    r = {"demand_30": 5.0, "demand_7": 6.5, "trade_days": 30, "trend": "up",
         "reference": 330e6, "lowest_sell": 331e6, "highest_buy": 284e6,
         "outliers": 1, "stock": 14, "days_of_stock": 2.8, "nothing_listed": False,
         "list": {"price": 331e6, "profit_each": 46.9e6, "margin": 0.175,
                  "days_best": 2.0, "days_typical": 4.8, "batch_profit": 469e6},
         "now": {"fillable": 6, "avg_price": 282.7e6, "profit_each": 5.1e6,
                 "batch_profit": 30.6e6},
         "speed": "fast", "supply": "under", "verdict": "Sells about 5 a day."}
    r.update(over)
    return r


def test_hub_row_for_a_normal_hub():
    row = hv.hub_row("amarr", "Amarr", _result(), {"jumps": 11, "highsec": True}, qty=10)
    assert row["unavailable"] is False
    assert row["speed_label"] == "Fast" and row["supply_label"] == "Under-supplied"
    assert row["lowest_sell"] == "331.0M"
    assert row["demand_30"] == "5" and row["demand_7"] == "6.5" and row["trend"] == "up"
    assert row["stock"] == "14" and row["stock_days"] == "2.8 days"
    assert row["lw_profit"] == "+46.9M" and row["lw_sign"] == "pos"
    assert row["lw_margin"] == "17.5%" and row["lw_days"] == "2 – 5"
    assert row["now_avg"] == "282.7M" and row["now_fill"] == "fills 6 of 10"
    assert row["outlier_note"] == "1 outlier ignored"
    assert row["route"] == "11 j" and row["route_note"] == "highsec"


def test_hub_row_for_missing_pieces():
    r = _result(lowest_sell=None, nothing_listed=True, stock=0, days_of_stock=None,
                outliers=0,
                now={"fillable": 0, "avg_price": None, "profit_each": None,
                     "batch_profit": None})
    r["list"].update(profit_each=None, margin=None, batch_profit=None)
    row = hv.hub_row("rens", "Rens", r, None, qty=10)
    assert row["lowest_sell"] == "Nothing listed"
    assert row["stock_days"] == ""
    assert row["lw_profit"] == "—" and row["lw_sign"] == ""
    assert row["now_avg"] == "no buyers" and row["now_fill"] == ""
    assert row["outlier_note"] == ""
    assert row["route"] == "" and row["route_note"] == ""


def test_hub_row_unavailable():
    assert hv.hub_row("hek", "Hek", None, None, qty=10) == {
        "key": "hek", "name": "Hek", "unavailable": True}


def test_route_that_leaves_highsec():
    row = hv.hub_row("hek", "Hek", _result(), {"jumps": 19, "highsec": False}, qty=10)
    assert row["route_note"] == "leaves highsec"


NAMES = {"jita": "Jita", "amarr": "Amarr", "rens": "Rens"}


def test_pick_boxes_two_hubs():
    results = {"amarr": _result(),
               "rens": _result(speed="thin", supply=None,
                               list={"price": 345e6, "profit_each": 56.3e6, "margin": 0.21,
                                     "days_best": 16.7, "days_typical": 21.7,
                                     "batch_profit": 563e6})}
    boxes = hv.pick_boxes({"fastest": "amarr", "fastest_thin": False, "margin": "rens"},
                          results, NAMES)
    assert [b["k"] for b in boxes] == ["Fastest sale", "Best margin"]
    assert boxes[0]["v"] == "Amarr — about 2 – 5 days"
    assert boxes[1]["v"] == "Rens — +56.3M each (21.0%)"
    assert "Thin market" in boxes[1]["d"]


def test_pick_boxes_same_hub_collapse_to_one():
    boxes = hv.pick_boxes({"fastest": "amarr", "fastest_thin": False, "margin": "amarr"},
                          {"amarr": _result()}, NAMES)
    assert [b["k"] for b in boxes] == ["Fastest sale and best margin"]


def test_pick_boxes_without_a_cost_or_any_data():
    boxes = hv.pick_boxes({"fastest": "amarr", "fastest_thin": False, "margin": None},
                          {"amarr": _result()}, NAMES)
    assert [b["k"] for b in boxes] == ["Fastest sale"]
    assert hv.pick_boxes({"fastest": None, "fastest_thin": False, "margin": None},
                         {}, NAMES) == []


@pytest.mark.parametrize("v, expected", [
    (999_950, "1.0M"), (999_999_999, "1.00B"), (999.999, "1.0K"), (999_949, "999.9K"),
])
def test_fmt_isk_unit_boundaries(v, expected):
    assert hv.fmt_isk(v) == expected


def test_signed_zero_rounding():
    assert hv.fmt_signed(-0.001) == "+0.00"
    assert hv._sign(-0.001) == "pos"
    assert hv.fmt_signed(-3_500_000) == "\u22123.5M"
    assert hv._sign(-3_500_000) == "neg"


@pytest.mark.parametrize("v, expected", [(0.04, "<0.1"), (0.0, "0"), (0.05, "0.1")])
def test_fmt_rate_small(v, expected):
    assert hv.fmt_rate(v) == expected


@pytest.mark.parametrize("v, expected", [(2.5, "3"), (3.5, "4"), (0.96, "1"), (0.4, "<1")])
def test_day_rounds_half_up(v, expected):
    assert hv._day(v) == expected


def _subday():
    return _result(list={"price": 331e6, "profit_each": 46.9e6, "margin": 0.175,
                         "days_best": 0.2, "days_typical": 0.4, "batch_profit": 469e6})


def test_pick_boxes_sub_day_fastest_and_margin():
    results = {"amarr": _subday(), "rens": _subday()}
    boxes = hv.pick_boxes({"fastest": "amarr", "fastest_thin": False, "margin": "rens"},
                          results, NAMES)
    assert boxes[0]["v"] == "Amarr \u2014 under a day"
    assert boxes[1]["d"].endswith("Under a day.")
    assert "<1" not in boxes[0]["v"] + boxes[1]["d"]
    one = hv.pick_boxes({"fastest": "amarr", "fastest_thin": False, "margin": "amarr"},
                        {"amarr": _subday()}, NAMES)
    assert one[0]["v"] == "Amarr \u2014 under a day"


def _mixed(best, typical):
    return _result(list={"price": 331e6, "profit_each": 46.9e6, "margin": 0.175,
                         "days_best": best, "days_typical": typical,
                         "batch_profit": 469e6})


def test_pick_boxes_mixed_sub_day_range_reads_up_to():
    results = {"amarr": _mixed(0.2, 7.6), "rens": _mixed(0.3, 4.4)}
    boxes = hv.pick_boxes({"fastest": "amarr", "fastest_thin": False, "margin": "rens"},
                          results, NAMES)
    assert boxes[0]["v"] == "Amarr — up to 8 days"
    assert boxes[1]["d"].endswith("Up to 4 days.")
    assert "<1" not in boxes[0]["v"] + boxes[1]["d"]
    assert "about" not in boxes[0]["v"] and "About" not in boxes[1]["d"]


def test_pick_boxes_mixed_sub_day_range_up_to_one_day():
    results = {"amarr": _mixed(0.2, 0.6), "rens": _mixed(0.3, 0.7)}
    boxes = hv.pick_boxes({"fastest": "amarr", "fastest_thin": False, "margin": "rens"},
                          results, NAMES)
    assert boxes[0]["v"] == "Amarr — up to 1 day"
    assert boxes[1]["d"].endswith("Up to 1 day.")


def test_pick_boxes_combined_mixed_sub_day_range():
    one = hv.pick_boxes({"fastest": "amarr", "fastest_thin": False, "margin": "amarr"},
                        {"amarr": _mixed(0.2, 7.6)}, NAMES)
    assert one[0]["v"] == "Amarr — up to 8 days"


def test_pick_boxes_margin_hub_without_a_days_estimate_has_no_days_sentence():
    results = {"amarr": _result(),
               "rens": _result(speed="thin", supply=None, list=_mixed(None, None)["list"])}
    boxes = hv.pick_boxes({"fastest": "amarr", "fastest_thin": False, "margin": "rens"},
                          results, NAMES)
    assert boxes[1]["d"] == "Thin market."
    assert "days" not in boxes[1]["d"]
    assert "— days" not in boxes[0]["v"] + boxes[1]["v"] + boxes[1]["d"]
