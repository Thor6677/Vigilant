"""Kill Activity's 30D / 60D / 90D buttons work wherever the partial is
embedded (ISS-124).

partials/wormhole_kills.html is loaded by the wormhole system page (into
#wh-kills) and by the WH Tracker (into an anonymous div with no id). The
range buttons used to target "#wh-kills", which only the system page has:
on the tracker htmx raised htmx:targetError and the buttons did nothing.
They now replace the partial's own root (closest .whk, outerHTML), which
the response brings back as the same .b-panel.whk element, so both embeds
work and the .whk phone rules keep applying. They also name their own
hx-indicator, so on the system page they no longer inherit #wh-kills's
"#kills-loading" (gone after the first load; htmx logged an error per click).

Contexts follow the shapes app/routes/wormholes.py builds; every name is
invented."""
import re
from datetime import datetime, timezone
from html.parser import HTMLParser

from app.routes import wormholes as wh_mod
from tests._mobile import VOID, render_page, source


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags, self.depth = [], 0

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, {k: (v if v is not None else "") for k, v in attrs}, self.depth))
        if tag not in VOID:
            self.depth += 1

    def handle_endtag(self, tag):
        self.depth -= 1


def _tags(html):
    p = _Tags()
    p.feed(html)
    p.close()
    return p.tags


def _kills(days=30, **over):
    heatmap = [[0] * 24 for _ in range(7)]
    heatmap[2][9] = 1
    ctx = dict(kill_count=1, days=days, heatmap=heatmap, heatmap_ids={"2,9": [113]},
               heatmap_npc={}, heatmap_age={"2,9": 0}, recent_kills=[],
               day_names=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], max_kills=1,
               most_recent=datetime(2026, 10, 1, 20, 15, tzinfo=timezone.utc), days_ago=2.5,
               age_labels=["<10d", "10-20d", "20-30d"], system_name="J900101",
               top_corps=[], top_alliances=[])
    ctx.update(over)
    return render_page(wh_mod, "partials/wormhole_kills.html", "/wormholes/system/J900101/kills",
                       session=None, **ctx)


def _root_classes(html):
    tag, attrs, depth = _tags(html)[0]
    assert tag == "div" and depth == 0
    return attrs.get("class", "").split()


def _range_buttons(html):
    return [a for t, a, _ in _tags(html) if t == "button" and a.get("hx-get")]


def test_range_buttons_replace_the_partials_own_root():
    for days in (30, 60, 90):
        html = _kills(days=days)
        buttons = _range_buttons(html)
        assert len(buttons) == 2
        for b in buttons:
            assert b["hx-target"] == "closest .whk", b
            assert b["hx-swap"] == "outerHTML", b
            # Not the system page's inherited "#kills-loading", which the
            # first load removes ("selector returned no matches" per click).
            assert b["hx-indicator"] == "this", b
            assert re.fullmatch(r"/wormholes/system/J900101/kills\?days=(30|60|90)", b["hx-get"])
        # `closest .whk` resolves to the partial's root, which the response
        # brings back with the same classes.
        assert _root_classes(html)[:2] == ["b-panel", "whk"]


def test_no_target_in_the_partial_names_a_page_element():
    """Whatever page embeds the partial, every hx-target it carries must
    resolve inside the partial itself (never an id on the host page)."""
    for html in (_kills(), _kills(kill_count=0)):
        targets = [a["hx-target"] for _, a, _ in _tags(html) if "hx-target" in a]
        assert targets
        for t in targets:
            assert not t.startswith("#"), t
            assert t == "this" or t.startswith(("closest ", "find ")), t


def test_both_embeds_load_the_partial_into_a_container_that_is_not_the_panel():
    """outerHTML on .whk swaps the panel inside its container; the system
    page's #wh-kills and the tracker's polling div stay in place."""
    page = source("wormhole_system.html")
    assert re.search(r'<div id="wh-kills"\s+hx-get="/wormholes/system/\{\{ system\[\'system_name\'\] \}\}/kills"', page)
    tracker = source("partials/wh_tracker_panel.html")
    m = re.search(r'<div hx-get="/wormholes/system/\{\{ system\[\'system_name\'\] \}\}/kills"([^>]*)>', tracker)
    assert m and 'hx-swap="innerHTML"' in m.group(1)
    for src in (page, tracker):
        assert "whk" not in re.findall(r"<div[^>]*hx-get=\"/wormholes/system/[^\"]*/kills\"[^>]*>", src)[0]
