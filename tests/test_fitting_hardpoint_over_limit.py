"""The stats panel marks a hardpoint count that is over the hull's limit.

An imported or saved fit can carry more turrets or launchers than the hull has
hardpoints. It loads as-is; the count turns red instead of modules being
stripped.
"""
import re

import pytest

import app.main  # noqa: F401 — populates every router's templates.env.globals
from app.routes import fitting as fitting_mod


class _Zero(dict):
    def __missing__(self, key):
        return 0


def _render_stats(**stats):
    # A missing stat renders as 0, as in test_fighter_stats_partial.
    base = {k: 0 for k in ("total_dps", "spool_time_s", "total_dps_max_spool", "total_ehp")}
    base.update(stats)
    try:
        return fitting_mod.templates.env.get_template("partials/fitting_stats.html").render(stats=_Zero(base))
    except Exception as exc:  # pragma: no cover - surfaced as a clear failure
        pytest.fail(f"stats partial did not render: {exc}")


def _count(html, kind):
    m = re.search(r'<span data-hardpoint="' + kind + r'"([^>]*)>([^<]*)</span>', html)
    assert m, f"no {kind} hardpoint count in the stats panel"
    return m.group(1), m.group(2).strip()


def test_turrets_over_the_limit_are_red():
    html = _render_stats(turrets_used=5, turrets_total=4, launchers_used=1, launchers_total=2)
    attrs, text = _count(html, "turret")
    assert text == "5/4"
    assert "color:var(--danger)" in attrs and 'data-over="1"' in attrs
    assert 'title="More turrets fitted than this hull has hardpoints"' in attrs
    l_attrs, l_text = _count(html, "launcher")
    assert l_text == "1/2" and "danger" not in l_attrs and "data-over" not in l_attrs


def test_launchers_over_the_limit_are_red():
    html = _render_stats(turrets_used=0, turrets_total=0, launchers_used=3, launchers_total=2)
    attrs, text = _count(html, "launcher")
    assert text == "3/2"
    assert "color:var(--danger)" in attrs and 'data-over="1"' in attrs
    assert 'title="More launchers fitted than this hull has hardpoints"' in attrs
    assert "danger" not in _count(html, "turret")[0]


def test_at_or_under_the_limit_is_plain():
    html = _render_stats(turrets_used=4, turrets_total=4, launchers_used=0, launchers_total=2)
    for kind, text in (("turret", "4/4"), ("launcher", "0/2")):
        attrs, got = _count(html, kind)
        assert got == text and attrs == ""
