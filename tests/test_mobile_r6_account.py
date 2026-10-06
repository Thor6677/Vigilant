"""Mobile R6 T7: the Account page and the permission picker on phones (mobile
design §7; ISS-101; user decision D18 A).

- Account: each character card gets a phone-only summary button, "N of 25
  shared", which folds the card's permission chips and legend through the
  existing toggleExpanded. A card starts open (`is-expanded`) when anything
  isn't shared, so problems show straight away. The desktop count is m-hide,
  so phones don't show it twice; desktop keeps every chip.
- Picker: CSS only, because it also serves the logged-out sign-up flow, where
  actions.js isn't loaded. Phones hide the "unlocks" chips (.perm-powers);
  every card keeps its switch, title, summary, role, note and ESI scopes.

Desktop must render exactly as before (D21): the summary button is m-only,
and every new rule sits inside the section's phone media block.

Contexts follow the shapes app/routes/account.py and app/auth/routes.py
build. Names and ids are invented."""
import functools
import re
import types
from html.parser import HTMLParser

import pytest

from app.auth import routes as auth_mod
from app.auth import scopes as perms
from app.notify import user_discord
from app.routes import account as acct_mod
from tests._mobile import VOID, css_section, norm, phone_block, render_page, rule_bodies

_section = functools.partial(css_section, release="R6")
_NS = types.SimpleNamespace
TOTAL = len(perms.PERMISSIONS)
ALL = list(perms.ALL_SCOPES)


def _scopes_of(*keys):
    return [s for p in perms.PERMISSIONS if p.key in keys for s in p.scopes]


def _char(cid, name, scopes, declined=(), is_main=False, warnings=None):
    row = _NS(character_id=cid, character_name=name, corporation_name="Sample Corp",
              alliance_name=None, is_main=is_main, scopes=" ".join(scopes),
              declined_scopes=" ".join(declined))
    return acct_mod._char_view(row, warnings or {})


MAIN = _char(90000101, "Sample Pilot Main", ALL, is_main=True)
# Mail declined, skills never asked for: 23 of 25 shared.
ALT = _char(90000102, "Sample Pilot Alt",
            [s for s in ALL if s not in _scopes_of("mail", "skills")],
            declined=_scopes_of("mail"))
# Everything shared, but EVE rejected the token.
LAPSED = _char(90000103, "Sample Pilot Lapsed", ALL, warnings={"sync": "token_revoked: 400"})

_NOTIFY = {"set": False, "masked": "", "enabled": True, "last_at": None, "last_ok": None,
           "last_error": None, "chosen": set(user_discord.DEFAULT_ALERT_TYPES),
           "groups": user_discord.ALERT_TYPE_GROUPS, "dropped": 0}


def _account(chars):
    return render_page(acct_mod, "account.html", "/account", characters=chars, flash=None,
                       total_permissions=TOTAL, notify=dict(_NOTIFY))


def _picker_signup():
    sel = set(perms.PRESETS[perms.DEFAULT_PRESET])
    return render_page(auth_mod, "permissions_picker.html", "/auth/connect", session=None,
                       intent="signup", character=None, selected=sel,
                       active_preset=perms.DEFAULT_PRESET, **auth_mod.picker_context())


def _picker_update():
    current = perms.keys_for_scopes(ALT["scopes"])
    return render_page(acct_mod, "permissions_picker.html", "/account/permissions/90000102",
                       intent="update", welcome=False, character=ALT, current=set(current),
                       adding=set(), selected=set(current), active_preset=None,
                       **auth_mod.picker_context())


# ── a tree, so a test can ask about ancestors and direct children ─────

class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"tag": "#root", "attrs": {}, "kids": [], "text": "", "parent": None}
        self.cur = self.root
        self.all = []

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": {k: (v or "") for k, v in attrs}, "kids": [], "text": "",
                "parent": self.cur}
        self.cur["kids"].append(node)
        self.all.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n["tag"] != tag:
            n = n["parent"]
        if n is not None and n["parent"] is not None:
            self.cur = n["parent"]

    def handle_data(self, data):
        n = self.cur
        while n is not None:
            n["text"] += data
            n = n["parent"]


def _tree(html):
    t = _Tree()
    t.feed(html)
    t.close()
    return t


def _cls(node):
    return node["attrs"].get("class", "").split()


def _find(tree, cls, tag=None):
    return [n for n in tree.all if cls in _cls(n) and (tag is None or n["tag"] == tag)]


def _matches(node, selector):
    """The simple selectors data-toggle-target uses: `.a`, `.a.b`, `tag.a`."""
    m = re.fullmatch(r"([a-z]*)((?:\.[\w-]+)+)", selector)
    assert m, f"unsupported selector {selector!r}"
    tag, classes = m.group(1), m.group(2).split(".")[1:]
    return (not tag or node["tag"] == tag) and all(c in _cls(node) for c in classes)


def _closest(node, selector):
    while node is not None and node["tag"] != "#root":
        if _matches(node, selector):
            return node
        node = node["parent"]
    return None


def _cards(html):
    return _find(_tree(html), "acct-char")


def _toggles(card):
    return [k for k in card["kids"] if "acct-perms-toggle" in _cls(k)]


# ── Account: ISS-101 ──────────────────────────────────────────────────

def test_each_card_has_one_phone_summary_with_its_shared_count():
    cards = _cards(_account([MAIN, ALT, LAPSED]))
    assert len(cards) == 3
    # A rejected token's head shows only "authorization expired", so its
    # button names the chips instead of a count nothing can read.
    for card, want in zip(cards, (f"{TOTAL} of {TOTAL} shared", f"{TOTAL - 2} of {TOTAL} shared",
                                  "Permissions")):
        toggles = _toggles(card)
        assert len(toggles) == 1, "one summary button, a direct child of the card"
        btn = toggles[0]
        assert btn["tag"] == "button" and btn["attrs"].get("type") == "button"
        assert "m-only" in _cls(btn), "desktop keeps the chips and never shows the button"
        assert norm(btn["text"]) == want


def test_the_summary_sits_between_the_head_and_the_chips():
    for card in _cards(_account([MAIN, ALT])):
        order = [c for k in card["kids"] for c in _cls(k)
                 if c in ("acct-char-head", "acct-perms-toggle", "acct-perms", "acct-legend")]
        assert order == ["acct-char-head", "acct-perms-toggle", "acct-perms", "acct-legend"]


def test_a_card_starts_open_only_when_something_isnt_shared():
    main, alt, lapsed = _cards(_account([MAIN, ALT, LAPSED]))
    assert "is-expanded" not in _cls(main)
    assert "is-expanded" in _cls(alt)
    assert "is-expanded" not in _cls(lapsed), "a rejected token alone doesn't open the chips"


@pytest.mark.parametrize("scopes, declined", [
    pytest.param([s for s in ALL if s not in _scopes_of("wallet")], _scopes_of("wallet"), id="declined"),
    pytest.param([s for s in ALL if s not in _scopes_of("industry")], (), id="new"),
    pytest.param([s for s in ALL if s not in _scopes_of("location")[1:]], (), id="partial"),
])
def test_any_unshared_state_opens_the_card(scopes, declined):
    char = _char(90000104, "Sample Pilot Four", scopes, declined=declined)
    assert any(st != perms.GRANTED for st in char["states"].values())
    (card,) = _cards(_account([char]))
    assert "is-expanded" in _cls(card)
    assert norm(_toggles(card)[0]["text"]) == f"{TOTAL - 1} of {TOTAL} shared"


def test_the_summary_toggles_its_own_card_with_toggle_expanded():
    for card in _cards(_account([MAIN, ALT, LAPSED])):
        btn = _toggles(card)[0]
        assert btn["attrs"].get("data-click") == "toggleExpanded"
        target = btn["attrs"].get("data-toggle-target", "")
        assert target, "toggleExpanded with no target would toggle the button itself"
        assert _closest(btn, target) is card, f"{target!r} must resolve to the button's own card"


def test_the_chips_and_legend_stay_direct_children_of_the_card():
    """The phone rules hide them with `.acct-char:not(.is-expanded) > …`."""
    for card in _cards(_account([MAIN, ALT])):
        kids = [c for k in card["kids"] for c in _cls(k)]
        assert "acct-perms" in kids and "acct-legend" in kids
        chips = [n for k in card["kids"] if "acct-perms" in _cls(k) for n in k["kids"]]
        assert len(chips) == TOTAL, "desktop still lists every permission"


def test_the_desktop_count_is_hidden_on_phones_only():
    tree = _tree(_account([MAIN, ALT]))
    counts = _find(tree, "acct-count")
    assert len(counts) == 2
    for c in counts:
        assert "m-hide" in _cls(c)
        assert "m-only" not in _cls(c)


def test_the_page_keeps_its_links_and_confirmed_forms():
    html = _account([MAIN, ALT, LAPSED])
    tree = _tree(html)
    links = [n for n in tree.all if n["tag"] == "a" and n["attrs"].get("href", "").startswith("/account/permissions/")]
    assert [norm(a["text"]) for a in links if "b-btn" in _cls(a)] == [
        "Change permissions", "Change permissions", "Renew"]
    for a in links:
        assert "m-hide" not in _cls(a)
    removes = [n for n in tree.all if n["tag"] == "form" and n["attrs"].get("action", "").startswith("/auth/remove/")]
    assert len(removes) == 2 and all(f["attrs"].get("data-confirm") for f in removes)
    (out,) = [n for n in tree.all if n["tag"] == "form" and n["attrs"].get("action") == "/auth/logout-everywhere"]
    assert out["attrs"].get("data-confirm")


# ── Picker: D18 A, CSS only ──────────────────────────────────────────

def _desc(node):
    for k in node["kids"]:
        yield k
        yield from _desc(k)


def _assert_cards_complete(html):
    tree = _tree(html)
    cards = [n for n in tree.all if n["tag"] == "label" and "perm-card" in _cls(n)]
    assert len(cards) == TOTAL
    by_key = {p.key: p for p in perms.PERMISSIONS}
    for card in cards:
        p = by_key[card["attrs"]["data-key"]]
        inner = list(_desc(card))

        def has(cls, tag=None):
            return [n for n in inner if cls in _cls(n) and (tag is None or n["tag"] == tag)]
        assert has("perm-toggle", "input"), p.key
        assert has("perm-title") and has("perm-summary"), p.key
        assert bool(has("perm-role")) == bool(p.in_game_roles), p.key
        assert bool(has("perm-note")) == bool(p.note), p.key
        assert has("perm-scopes", "details"), p.key
        assert bool(has("perm-powers")) == bool(p.powers), "the chips stay in the markup; CSS hides them"
        for n in inner:
            assert "m-hide" not in _cls(n) and "m-only" not in _cls(n), p.key


def test_the_anonymous_signup_picker_binds_nothing_through_actions_js():
    html = _picker_signup()
    assert "data-click" not in html and "data-input" not in html and "data-change" not in html
    assert not re.search(r"<script[^>]*\bsrc=\"[^\"]*actions\.js", html), "actions.js is logged-in only"
    _assert_cards_complete(html)
    assert re.search(r'<script nonce="test-nonce">\s*\(function \(\) \{\s*var form = document\.getElementById\(\'perm-form\'\)', html), (
        "the picker's own script still runs under the page nonce")


def test_the_logged_in_picker_keeps_every_card_and_the_purge_box():
    html = _picker_update()
    _assert_cards_complete(html)
    assert re.search(r"<script[^>]*\bsrc=\"[^\"]*actions\.js", html), "logged in, actions.js loads"
    tree = _tree(html)
    (page,) = _find(tree, "perm-page")
    inner = list(_desc(page))
    assert not [n for n in inner if any(a in ("data-click", "data-input", "data-change")
                                         for a in n["attrs"])], "the picker binds in its own script"
    assert [n for n in inner if n["attrs"].get("id") == "perm-purge"]
    assert [n for n in inner if "perm-footer" in _cls(n)]


# ── site.css: the R6 T7 section ──────────────────────────────────────

def _phone():
    body, tail = phone_block(_section("T7"))
    return body, tail


def test_the_section_is_phone_only():
    body, tail = _phone()
    assert tail.strip() == "", "desktop stays identical: no rule outside the phone block"


def test_folded_cards_hide_their_chips_and_legend_on_phones():
    body, _ = _phone()
    for part in ("acct-perms", "acct-legend"):
        rules = rule_bodies(body, f".acct-char:not(.is-expanded) > .{part}")
        assert re.search(r"display:\s*none", rules), part


def test_the_summary_chevron_follows_the_card_state():
    body, _ = _phone()
    closed = rule_bodies(body, ".acct-perms-toggle::after")
    opened = rule_bodies(body, ".acct-char.is-expanded > .acct-perms-toggle::after")
    assert "▸" in closed and "▾" in opened
    assert re.search(r'content:\s*"▸"\s*/\s*""', closed), "the chevron is decoration for screen readers"
    assert re.search(r'content:\s*"▾"\s*/\s*""', opened)


def test_the_picker_hides_the_unlocks_chips_on_phones():
    body, _ = _phone()
    assert re.search(r"display:\s*none", rule_bodies(body, ".perm-card .perm-powers"))


def test_picker_controls_reach_the_44px_floor():
    body, _ = _phone()
    assert re.search(r"line-height:\s*44px", rule_bodies(body, ".perm-scopes > summary"))
    assert re.search(r"min-height:\s*44px", rule_bodies(body, ".perm-footer > a"))
    assert re.search(r"min-width:\s*44px", rule_bodies(body, ".perm-group-actions > button")), (
        "All / None are 44px tall from the global rule; this makes them 44px wide too")


def test_the_footer_button_may_shrink_so_the_count_stays_on_screen():
    """.b-btn.is-primary is flex:none; beside Cancel at 360px that pushed the
    count off the left edge (justify-content:flex-end)."""
    body, _ = _phone()
    assert re.search(r"flex:\s*0 1 auto", rule_bodies(body, ".perm-footer > .b-btn"))


def test_the_section_never_shrinks_a_control_below_the_global_floor():
    body, _ = _phone()
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", body):
        sizes = re.findall(r"(?:min-)?height:\s*(\d+)px", m.group(2))
        assert all(int(s) >= 44 for s in sizes), f"{m.group(1).strip()}: {sizes}"
        if re.search(r"min-height:\s*(0|auto)\b", m.group(2)):
            raise AssertionError(f"{m.group(1).strip()} resets a min-height")
