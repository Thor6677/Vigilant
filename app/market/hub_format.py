"""Number formatting shared by the hub comparison's sentences and its table.

`hub_analysis` (the verdict sentence) and `hub_view` (the columns and the
pick boxes) both format through here, so one figure is never rounded two ways
on the same page.

Days are rounded half up (2.5 → 3, 4.5 → 5; never banker's rounding). A value
that rounds to 0 — under half a day — is "sub-day": the table shows "<1" and
the sentences say "under a day". So 0.4 days reads "<1" / "under a day", and
0.5 reads "1" / "roughly 1 day".

Rates (units sold a day, days of stock) are whole numbers from 10 up and one
decimal below that, with a trailing ".0" dropped; a rate above zero that would
show as "0" shows as "<0.1".
"""
from __future__ import annotations

import math


def day_number(v: float) -> int:
    """Days, rounded half up. 0 means sub-day (under half a day)."""
    return math.floor(v + 0.5)


def fmt_day(v: float) -> str:
    """One day count for the table: '<1', '1', '12'."""
    n = day_number(v)
    return "<1" if n < 1 else str(n)


def fmt_days_range(best: float | None, typical: float | None) -> str:
    """The table's days-to-sell cell: '—', '<1', '3', '<1 – 8', '2 – 5'."""
    if best is None or typical is None:
        return "—"
    lo, hi = fmt_day(best), fmt_day(typical)
    return lo if lo == hi else f"{lo} – {hi}"


def days_phrase(best: float, typical: float, approx: str = "roughly") -> str:
    """The same range in words, hedge word included: 'under a day',
    'up to 1 day', 'up to 8 days', 'roughly 1 day', 'roughly 2 – 5 days'
    (`approx` replaces 'roughly'). Uses the same numbers as fmt_days_range."""
    lo, hi = day_number(best), day_number(typical)
    if lo < 1 and hi < 1:
        return "under a day"
    if lo < 1:
        return "up to 1 day" if hi == 1 else f"up to {hi} days"
    if lo == hi:
        return f"{approx} 1 day" if lo == 1 else f"{approx} {lo} days"
    return f"{approx} {lo} – {hi} days"


def fmt_rate(v: float) -> str:
    """A rate: '42', '6', '1.2', '0.6', '<0.1', '0'."""
    if v >= 10:
        return f"{v:.0f}"
    if 0 < v < 0.05:
        return "<0.1"
    return f"{v:.1f}".rstrip("0").rstrip(".")
