"""v1.9.0 polish A: central fixes to the shared phone layer (mobile design §4).

All of it lives in the R1 layer of static/css/site.css (before the first
`/* ── R2 T1` section), static/js/actions.js, tests/_mobile.py and
scripts/mobile-audit.js. Every CSS rule here sits inside a
`@media (max-width: 640px)` block, so desktop is unchanged by construction.

1. m-tap no longer shrinks buttons: the shared .m-tap rule forces
   min-height 40px !important, but every <button> and .b-btn already has the
   44px phone floor. `button.m-tap, .b-btn.m-tap` keeps them at 44px.
2. A non-button .b-btn (mostly <a class="b-btn">) centres its label in the
   44px phone box with `align-content: center`, which leaves the box's
   display, and so its width, exactly as it was."""
import re

from tests._mobile import SITE_CSS, selectors

PHONE = "max-width: 640px"
_R2_FIRST = "/* ── R2 T1 · character overview ── */"


def _raw() -> str:
    with open(SITE_CSS, encoding="utf-8") as fh:
        return fh.read()


def _strip(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _media_bodies(css: str, query: str) -> list[str]:
    """Bodies of every @media block whose prelude contains `query`, brace-matched."""
    out = []
    for m in re.finditer(r"@media([^{]*)\{", css):
        if query not in m.group(1):
            continue
        depth, i = 1, m.end()
        while depth and i < len(css):
            depth += (css[i] == "{") - (css[i] == "}")
            i += 1
        out.append(css[m.end():i - 1])
    return out


def _r1_layer() -> str:
    """The R1 shared layer: everything before the first R2 page section."""
    raw = _raw()
    assert raw.count(_R2_FIRST) == 1
    return _strip(raw[:raw.index(_R2_FIRST)])


def _rules(css: str):
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        yield selectors(m.group(1)), m.group(2)


def _r1_phone_rule(sel_list: list[str]) -> str:
    """The body of the R1 phone-layer rule whose selector list is exactly
    `sel_list` (there must be exactly one)."""
    bodies = [body for block in _media_bodies(_r1_layer(), PHONE)
              for sels, body in _rules(block) if sels == sel_list]
    assert len(bodies) == 1, f"expected one R1 phone rule for {sel_list}, found {len(bodies)}"
    return bodies[0]


def _decl(body: str, prop: str) -> str | None:
    m = re.search(rf"(?:^|[;\s]){re.escape(prop)}\s*:\s*([^;]+?)\s*(?:;|$)", body)
    return m.group(1) if m else None


def _outside_phone(css: str) -> str:
    """`css` with every phone @media block removed."""
    out, i = [], 0
    for m in re.finditer(r"@media([^{]*)\{", css):
        if m.start() < i or PHONE not in m.group(1):
            continue
        depth, j = 1, m.end()
        while depth and j < len(css):
            depth += (css[j] == "{") - (css[j] == "}")
            j += 1
        out.append(css[i:m.start()])
        i = j
    out.append(css[i:])
    return "".join(out)


# ── 1. m-tap keeps buttons at the 44px floor ─────────────────────────

def test_m_tap_buttons_keep_the_44px_floor():
    body = _r1_phone_rule(["button.m-tap", ".b-btn.m-tap"])
    assert _decl(body, "min-height") == "44px !important"
    # Width is still m-tap's 40px minimum: the rule only restores the height.
    assert _decl(body, "min-width") is None


def test_the_shared_m_tap_rule_still_gives_links_40px():
    """Links and other non-button targets keep the R1 contract (40x40,
    12px glyph); test_dashboard_modes_render pins the same body."""
    body = _r1_phone_rule([".m-tap", ".dash-sec-toggle", ".dash-sec-hide"])
    assert _decl(body, "min-height") == "40px !important"
    assert _decl(body, "min-width") == "40px !important"


def test_the_button_rule_is_phone_only():
    assert "button.m-tap" not in _outside_phone(_strip(_raw()))
    assert ".b-btn.m-tap" not in _outside_phone(_strip(_raw()))


# ── 2. a.b-btn labels sit in the middle of their 44px box ─────────────

_POLISH_HEAD = "/* ═══ v1.9.0 polish A — shared phone-layer fixes"


def _polish_block() -> str:
    """The polish-A phone block: after its header, before R2 T1."""
    raw = _raw()
    assert raw.count(_POLISH_HEAD) == 1
    start, stop = raw.index(_POLISH_HEAD), raw.index(_R2_FIRST)
    assert start < stop, "the polish-A block belongs to the R1 layer, before R2 T1"
    blocks = _media_bodies(_strip(raw[start:stop]), PHONE)
    assert len(blocks) == 1, "one phone @media block"
    return blocks[0]


def _polish_rule(sel_list: list[str]) -> str:
    bodies = [body for sels, body in _rules(_polish_block()) if sels == sel_list]
    assert len(bodies) == 1, f"expected one polish-A rule for {sel_list}, found {len(bodies)}"
    return bodies[0]


def test_non_button_b_btn_labels_are_centred_without_a_display_change():
    body = _polish_rule([".b-btn:not(button)"])
    assert _decl(body, "align-content") == "center"
    # No display (block stays full width, inline-block keeps its width), no
    # sizes, no !important: a page section that makes one flex still wins.
    for prop in ("display", "width", "min-width", "height", "min-height", "justify-content"):
        assert _decl(body, prop) is None, prop
    assert "!important" not in body


def test_the_polish_block_is_phone_only():
    raw = _raw()
    block = _strip(raw[raw.index(_POLISH_HEAD):raw.index(_R2_FIRST)])
    assert _outside_phone(block).strip() == "", "every polish-A rule sits in the phone block"
