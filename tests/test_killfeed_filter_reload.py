"""Kill Feed: a filter change (or "Back to live") reloads the feed at once
(ISS-122).

The page cleared #kf-feed to "Loading…" and called
htmx.trigger(feed, 'load'). htmx 1.9 runs a `load` trigger once, when it
processes the element; it never listens for a `load` event, so that call
did nothing and the feed stayed on "Loading…" until the next `every 15s`
poll. The feed now declares a custom event in its hx-trigger and the page
triggers that, which goes through the same request path as the poll (same
hx-get, swap, and the htmx:configRequest handler that adds the filters).
"""
import re

from app.routes import intel_kills as kills_mod
from tests._mobile import render_page


def _page():
    return render_page(kills_mod, "intel_kills.html", "/intel/kills")


def _feed_trigger(html):
    m = re.search(r'<div id="kf-feed"([^>]*)>', html)
    assert m, "no #kf-feed"
    t = re.search(r'hx-trigger="([^"]*)"', m.group(1))
    assert t, "#kf-feed has no hx-trigger"
    return [part.strip() for part in t.group(1).split(",")]


def _scripts(html):
    """The page's inline scripts with `//` comments dropped (the comments
    talk about htmx.trigger calls too)."""
    js = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.S))
    return re.sub(r"(?m)^\s*//[^\n]*$", "", js)


def test_feed_keeps_its_first_load_and_poll_and_declares_a_reload_event():
    triggers = _feed_trigger(_page())
    assert triggers[0] == "load"
    assert "every 15s" in triggers
    custom = [t for t in triggers if t != "load" and not t.startswith("every ")]
    assert custom == ["kf-reload"]


def test_every_feed_reload_triggers_an_event_the_feed_listens_for():
    html = _page()
    triggers = _feed_trigger(html)
    listened = {t.split()[0] for t in triggers if t != "load" and not t.startswith("every ")}
    calls = re.findall(r"htmx\.trigger\(\s*(\w+)\s*,\s*'([^']*)'\s*\)", _scripts(html))
    # persist() (filter change) and resumeLive() (Back to live).
    assert len(calls) == 2, calls
    for var, event in calls:
        assert event != "load", f"htmx.trigger({var}, 'load') never reaches a load trigger"
        assert event in listened, (var, event)


def test_reload_sites_still_reset_the_cursor_and_the_row_cap_first():
    """The fresh request must ask for the full top-100 under the new
    filter: the `since` cursor and the visible-row cap reset before the
    trigger."""
    js = _scripts(_page())
    sites = list(re.finditer(r"htmx\.trigger\(\s*\w+\s*,\s*'kf-reload'\s*\)", js))
    assert len(sites) == 2
    for m in sites:
        before = js[max(0, m.start() - 600):m.start()]
        assert "kfSinceCursor = null;" in before
        assert "KF_VISIBLE_CAP = 100;" in before
        assert "Loading…" in before
