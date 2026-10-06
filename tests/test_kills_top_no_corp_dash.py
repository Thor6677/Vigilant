"""Kill Feed "Most Valuable" cards: a victim with no corporation and no
alliance shows an em dash, not the text "&mdash;".

The fallback used to be the string '&mdash;' inside a Jinja expression,
which autoescaping turned into "&amp;mdash;", so the card read "&mdash;".

Names and ids are invented."""
import re

from app.routes import intel_kills as kills_mod
from tests._mobile import render_page


def _card(i, **over):
    c = {"killmail_id": 120000200 + i, "type_id": 35832, "type_name": f"Sample Hull {i}",
         "isk_fmt": "12.3B", "victim_corp": "Sample Corp", "victim_alliance": "Sample Alliance",
         "system_id": 30000001, "system_name": "Sample System", "system_band": "ns"}
    c.update(over)
    return c


def _corp_lines(html):
    return re.findall(r'<div class="kf-top-corp">([^<]*)</div>', html)


def _top(structures, ships):
    return render_page(kills_mod, "partials/intel_kills_top.html", "/intel/kills/top",
                       structures=structures, ships=ships)


def test_card_without_corp_or_alliance_shows_an_em_dash():
    html = _top([_card(0, victim_corp="", victim_alliance="")],
                [_card(1, victim_corp=None, victim_alliance=None)])
    assert _corp_lines(html) == ["—", "—"]
    assert "&amp;mdash;" not in html
    assert "mdash" not in html.split('id="kf-top-body"', 1)[1]


def test_card_with_a_corp_or_an_alliance_still_shows_it():
    html = _top([_card(0), _card(1, victim_corp="")],
                [_card(2, victim_alliance=""), _card(3, victim_corp="A & B <Holdings>")])
    assert _corp_lines(html) == ["Sample Corp", "Sample Alliance", "Sample Corp",
                                 "A &amp; B &lt;Holdings&gt;"]
