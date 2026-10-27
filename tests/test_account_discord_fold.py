"""On phones the Discord alert groups on /account fold behind their legend (ISS-146).

Every alert checkbox label is held at the 44px tap floor, so the five groups
made the panel about 1.5 screens tall. Each group is now one row on a phone:
its name and an m-only "on/total" count button (toggleExpanded on the group)
that opens the checkboxes. Groups always start closed; a closed checkbox still
submits. Desktop renders every group open and is unchanged.

Names and ids are invented."""
import re

from app.notify import user_discord
from tests._mobile import css_section, phone_block
from tests.test_mobile_r6_account import MAIN, _account, _NOTIFY

def _section():
    return css_section("T7", release="R6")


def _html(chosen=None):
    from tests._mobile import render_page
    from tests.test_mobile_r6_account import TOTAL, acct_mod
    notify = dict(_NOTIFY)
    if chosen is not None:
        notify["chosen"] = set(chosen)
    return render_page(acct_mod, "account.html", "/account", characters=[MAIN], flash=None,
                       total_permissions=TOTAL, notify=notify)


def _groups(html):
    return re.findall(r'<fieldset class="acct-discord-group">(.*?)</fieldset>', html, flags=re.S)


def test_every_group_has_one_phone_toggle_on_the_group():
    html = _html()
    groups = _groups(html)
    assert len(groups) == len(user_discord.ALERT_TYPE_GROUPS)
    for g in groups:
        toggles = re.findall(r'<button type="button" class="m-only acct-discord-toggle"[^>]*>', g)
        assert len(toggles) == 1
        assert 'data-click="toggleExpanded"' in toggles[0]
        assert 'data-toggle-target=".acct-discord-group"' in toggles[0]


def test_the_count_matches_the_ticked_alerts_of_each_group():
    first_group, items = user_discord.ALERT_TYPE_GROUPS[0]
    keys = [k for k, _ in items]
    html = _html(chosen=keys[:2])
    g = _groups(html)[0]
    assert re.search(r"<b>2</b><span aria-hidden=\"true\">/%d</span>" % len(keys), g)
    other = _groups(html)[1]
    assert "<b>0</b>" in other


def test_no_group_starts_open_and_every_checkbox_is_still_rendered():
    html = _html(chosen=[k for _, items in user_discord.ALERT_TYPE_GROUPS for k, _ in items])
    total = sum(len(items) for _, items in user_discord.ALERT_TYPE_GROUPS)
    assert "is-expanded" not in "".join(_groups(html))
    assert html.count('name="alert_types"') == total


def test_phone_css_folds_the_groups_and_keeps_a_44px_row():
    body, tail = phone_block(_section())
    assert re.search(r"\.acct-discord-group:not\(\.is-expanded\)\s*>\s*\.acct-discord-check\s*\{\s*display:\s*none;", body)
    assert re.search(r"\.acct-discord-group\s*>\s*legend\s*\{[^}]*min-height:\s*44px", body)
    assert re.search(r"\.acct-discord-toggle::before\s*\{[^}]*inset:\s*0", body), "whole row is the tap target"
    assert tail.strip() == "", "desktop stays identical: no rule outside the phone block"


def test_desktop_never_sees_the_toggle():
    css = open("static/css/site.css", encoding="utf-8").read()
    # the only mentions of the toggle are inside the phone block above
    assert css.count(".acct-discord-toggle") == css_section("T7", release="R6").count(".acct-discord-toggle")
