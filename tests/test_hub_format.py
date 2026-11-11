"""Hub comparison: the one set of number formats the sentences and the table
share. Pure functions. Names are invented."""
import re

import pytest

from app.market import hub_analysis as ha
from app.market import hub_format as hf
from app.market import hub_view as hv


@pytest.mark.parametrize("v, expected", [
    (2.5, "3"), (3.5, "4"), (4.5, "5"), (0.96, "1"), (0.5, "1"), (0.49, "<1"), (0.4, "<1"),
    (0.0, "<1"),
])
def test_day_rounds_half_up(v, expected):
    assert hf.fmt_day(v) == expected


@pytest.mark.parametrize("best, typical, expected", [
    (None, None, "—"), (0.24, 7.6, "<1 – 8"), (1.7, 4.0, "2 – 4"), (3.0, 3.2, "3"),
    (2.5, 4.5, "3 – 5"), (0.2, 0.4, "<1"),
])
def test_fmt_days_range(best, typical, expected):
    assert hf.fmt_days_range(best, typical) == expected


@pytest.mark.parametrize("best, typical, expected", [
    (0.2, 0.4, "under a day"),
    (0.4, 1.0, "up to 1 day"),
    (0.4, 3.0, "up to 3 days"),
    (0.5, 0.9, "roughly 1 day"),
    (1.0, 1.2, "roughly 1 day"),
    (2.5, 4.5, "roughly 3 – 5 days"),
])
def test_days_phrase(best, typical, expected):
    assert hf.days_phrase(best, typical) == expected


def test_days_phrase_hedge_word():
    assert hf.days_phrase(1.0, 1.2, approx="about") == "about 1 day"
    assert hf.days_phrase(2.0, 4.8, approx="about") == "about 2 – 5 days"
    assert hf.days_phrase(0.2, 0.4, approx="about") == "under a day"


@pytest.mark.parametrize("v, expected", [
    (0.04, "<0.1"), (0.0, "0"), (0.05, "0.1"), (1.25, "1.2"), (6.0, "6"), (42.4, "42"),
])
def test_fmt_rate(v, expected):
    assert hf.fmt_rate(v) == expected


# ── the verdict and the table agree for the same hub ────────────────────

GRID = (0.1, 0.4, 0.49, 0.5, 0.9, 0.96, 1.0, 1.49, 1.5, 2.5, 3.5, 4.5, 4.8, 7.6, 14.5)


def _hub(best, typical, demand=5.0, stock=14, days_of_stock=2.8, supply=None):
    result = {"demand_30": demand, "demand_7": demand, "trade_days": 30, "trend": "flat",
              "reference": 100.0, "lowest_sell": 100.0, "highest_buy": 90.0,
              "outliers": 0, "stock": stock, "days_of_stock": days_of_stock,
              "nothing_listed": False, "speed": "steady", "supply": supply,
              "list": {"price": 100.0, "profit_each": None, "margin": None,
                       "days_best": best, "days_typical": typical, "batch_profit": None},
              "now": {"fillable": 10, "avg_price": 90.0, "profit_each": None,
                      "batch_profit": None}}
    result["verdict"] = ha.verdict(result, qty=10, has_cost=False)
    return result, hv.hub_row("amarr", "Amarr", result, None, qty=10)


@pytest.mark.parametrize("best, typical",
                         [(b, t) for b in GRID for t in GRID if t >= b])
def test_verdict_days_match_the_days_column(best, typical):
    result, row = _hub(best, typical)
    clause = result["verdict"].split("would take ")[1].split(".")[0]
    column = row["lw_days"]
    assert re.findall(r"\d+", clause) == re.findall(r"(?<!<)\b\d+", column), (clause, column)
    sub_day = clause == "under a day" or clause.startswith("up to ")
    assert sub_day == column.startswith("<1"), (clause, column)


@pytest.mark.parametrize("demand", [1.0, 1.25, 2.45, 6.0, 9.96, 10.5, 42.5])
def test_verdict_rate_matches_the_sold_per_day_column(demand):
    result, row = _hub(1.0, 2.0, demand=demand)
    assert result["verdict"].startswith(f"Sells about {row['demand_30']} a day")


@pytest.mark.parametrize("days_of_stock", [30.5, 41.0, 99.5, 120.25])
def test_saturated_verdict_matches_the_stock_days_column(days_of_stock):
    result, row = _hub(1.0, 2.0, demand=1.0, stock=41, days_of_stock=days_of_stock,
                       supply="saturated")
    assert f": {row['stock_days']} of stock ahead of you." in result["verdict"]
