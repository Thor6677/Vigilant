"""Mobile R6 T3: Image Host, the public image page (ISS-119), Discord Time and
Structure Age on phones (mobile design §4.3, §7; user decisions D6 A, D7 A,
D8 A).

- Your Uploads rows are tap-to-open m-rows: the thumbnail link is the lead,
  key 1 the label (or file name), key 2 the expiry. Name, Size, Views and
  Actions (a 40px View link plus the Delete form) open below.
- The public image page binds its two Copy buttons in its own nonce'd
  script, by id, with a page-local copy function. base.html loads actions.js
  only for a session, so a data-click there was dead for every logged-out
  visitor the link was shared with (ISS-119). The share fields stack, each
  ellipsised, with Copy below.
- Discord Time's seven JS-rendered format rows become two lines on phones
  (label + Copy, then the full preview) through phone CSS alone; the
  <t:…> tag is hidden but is still what Copy copies. The mode tabs are a
  44px bar and Quick Actions keep two columns.
- Structure Age's long in-game link wraps. The shared result card (also
  embedded by the WH Tracker and the wormhole system page) gets a 40px
  System link and 11px date labels and method badge on phones (polish
  pass); its markup is untouched.
- Every button here (Copy, Delete, Back, Estimate, the Discord Time
  buttons) keeps the global 44px phone floor: none carries m-tap, whose
  shared rule forces 40px, and no rule in this section sets a height below
  44px. Only links get m-tap.

Desktop must render exactly as before (D21): every phone-only cell is m-only
and every hook class has rules only inside the phone media block.

Contexts follow the shapes app/routes/images.py, discordtime.py and
structure_age.py build. Names and ids are invented."""
import functools
import re
import types
from datetime import datetime, timezone
from html.parser import HTMLParser

from app.routes import discordtime as dt_mod
from app.routes import images as img_mod
from app.routes import structure_age as sa_mod
from tests._mobile import (VOID, assert_mrow, assert_single_value_child, cells_rows,
                           css_section, phone_block, render_page, row_keys, row_labelled,
                           row_lead, rule_bodies, selectors, source)

_section = functools.partial(css_section, release="R6")
_NS = types.SimpleNamespace
_UTC = timezone.utc

# Every attribute static/js/actions.js dispatches on (its bubble and capture
# events, the data-on-error shortcut and the data-confirm submit guard).
# None of them does anything for a logged-out visitor.
_ACTION_BINDING = re.compile(
    r'\sdata-(?:click|change|input|submit|keydown|mousedown|focus|error|on-error|confirm)=')


def _phone():
    return phone_block(_section("T3"))[0]


def _after_phone():
    return phone_block(_section("T3"))[1]


class _Tags(HTMLParser):
    """Every start tag as (name, attrs)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, {k: (v if v is not None else "") for k, v in attrs}))


def _tags(html):
    p = _Tags()
    p.feed(html)
    p.close()
    return p.tags


class _Inside(HTMLParser):
    """For each element whose (tag, attrs) passes `pick`: every start tag
    nested inside it, as (name, attrs)."""

    def __init__(self, pick):
        super().__init__(convert_charrefs=True)
        self.pick, self.stack, self.found = pick, [], []

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        for _, ref in self.stack:
            if ref is not None:
                ref.append((tag, a))
        ref = None
        if self.pick(tag, a):
            ref = []
            self.found.append(ref)
        if tag not in VOID:
            self.stack.append((tag, ref))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return


def _inside(html, pick):
    p = _Inside(pick)
    p.feed(html)
    p.close()
    return p.found


def _cls(attrs):
    return attrs.get("class", "").split()


# ── Image Host: Your Uploads (D6 A) ──────────────────────────────────────

def _img(image_id, label, filename, w, h, size, views, expires):
    return _NS(id=image_id, user_id=1, label=label, original_filename=filename, width=w,
               height=h, size_bytes=size, view_count=views, expires_at=expires,
               created_at=datetime(2026, 9, 1, 12, 0, tzinfo=_UTC))


_UPLOADS = [
    _img("aB3dE5f", "Sample fleet comp", "sample-fleet.png", 1280, 720, 188_416, 42,
         datetime(2026, 11, 1, 18, 30, tzinfo=_UTC)),
    _img("Zx9Yw8v", None, "sample-scan-with-a-very-long-file-name.png", 1920, 1080, 421_888, 1,
         None),
    _img("Qr7St6u", "Sample kill", "sample-kill.jpg", 800, 600, 98_304, 0,
         datetime(2026, 10, 5, 12, 0, tzinfo=_UTC)),
]


def _render_uploads(images=_UPLOADS):
    return render_page(img_mod, "tools_images.html", "/tools/images", images=list(images),
                       max_mb=10, expiry_options=img_mod.EXPIRY_LABELS)


def _upload_rows():
    rows = cells_rows(_render_uploads())
    assert len(rows) == len(_UPLOADS), f"expected {len(_UPLOADS)} upload m-rows, got {len(rows)}"
    return rows


def test_uploads_are_tap_to_open_rows():
    html = _render_uploads()
    rows = assert_mrow(html, 3)
    assert len(rows) == 3
    for r in rows:
        assert r["attrs"].get("data-click") == "toggleMRow"
        assert "m-row--link" not in _cls(r["attrs"])
        assert "b-table-row" in _cls(r["attrs"]), "the desktop row class stays"


def test_upload_lead_is_the_48px_thumbnail_link():
    """The existing thumbnail link is the lead. Its img keeps the inline
    48x48 box, so a landscape upload can't stretch the lead, and it carries
    matching width/height attributes."""
    for img, row in zip(_UPLOADS, _upload_rows()):
        lead = row_lead(row)
        assert len(lead) == 1
        assert lead[0]["tag"] == "a" and lead[0]["attrs"].get("href") == f"/i/{img.id}"
        # Its only content is an alt="" image, so the link carries the name.
        name = img.label or img.original_filename or img.id
        assert lead[0]["attrs"].get("aria-label") == f"View {name}"
        kids = lead[0]["kids"]
        assert len(kids) == 1
        thumb = kids[0]
        assert thumb.get("src") == f"/i/{img.id}.jpg"
        assert thumb.get("width") == "48" and thumb.get("height") == "48"
        assert "width:48px" in thumb.get("style", "") and "height:48px" in thumb.get("style", "")
        assert "data-on-error" not in thumb


def test_upload_keys_are_the_name_and_the_expiry():
    rows = _upload_rows()
    want = [("Sample fleet comp", "2026-11-01"),
            ("sample-scan-with-a-very-long-file-name.png", "permanent"),
            ("Sample kill", "2026-10-05")]
    for (name, expiry), row in zip(want, rows):
        keys = row_keys(row)
        assert [k["text"] for k in keys] == [name, expiry]
        assert "img-up-exp" in _cls(keys[1]["attrs"]), "key 2 is muted through its hook class"


def test_upload_rows_open_to_name_size_views_and_actions():
    rows = _upload_rows()
    sizes = ["1280×720 · 184 KB", "1920×1080 · 412 KB", "800×600 · 96 KB"]
    for img, size, row in zip(_UPLOADS, sizes, rows):
        labelled = row_labelled(row)
        assert list(labelled) == ["Name", "Size", "Views", "Actions"]
        assert labelled["Name"]["text"] == (img.label or img.original_filename)
        assert labelled["Size"]["text"] == size
        assert labelled["Views"]["text"] == str(img.view_count)
        assert_single_value_child(row)


def test_upload_actions_hold_view_and_the_confirmed_delete_form():
    html = _render_uploads()
    cells = _inside(html, lambda t, a: a.get("data-m-label") == "Actions")
    assert len(cells) == len(_UPLOADS)
    for img, inner in zip(_UPLOADS, cells):
        links = [a for t, a in inner if t == "a"]
        forms = [a for t, a in inner if t == "form"]
        buttons = [a for t, a in inner if t == "button"]
        assert [a.get("href") for a in links] == [f"/i/{img.id}"]
        assert "m-tap" in _cls(links[0])
        assert len(forms) == 1
        assert forms[0].get("method", "").upper() == "POST"
        assert forms[0].get("action") == f"/tools/images/{img.id}/delete"
        assert forms[0].get("data-confirm") == "Delete this image?"
        assert len(buttons) == 1 and buttons[0].get("type") == "submit"
        assert "m-tap" not in _cls(buttons[0]), "a button keeps the 44px floor"


def test_upload_phone_cells_are_phone_only():
    """Desktop stays as it was (D21): the keys and labelled cells are new
    m-only copies; the desktop text block, View link and Delete form are
    still there, untagged."""
    for row in _upload_rows():
        for c in row["cells"]:
            a = c["attrs"]
            if a.get("data-m") == "key" or "data-m-label" in a:
                assert "m-only" in _cls(a), f"phone cell {a} must be m-only"
            elif a.get("data-m") != "lead":
                assert "m-only" not in _cls(a)
        untagged = [c for c in row["cells"]
                    if "data-m" not in c["attrs"] and "data-m-label" not in c["attrs"]]
        assert [c["tag"] for c in untagged] == ["div", "a", "form"]
        assert untagged[2]["attrs"].get("data-confirm") == "Delete this image?"


def test_upload_rows_css():
    phone = _phone()
    assert re.search(r"font-size:\s*12px", rule_bodies(phone, '.img-up-row > [data-m="key"]'))
    exp = rule_bodies(phone, ".img-up-row > .img-up-exp")
    assert "var(--muted)" in exp
    actions = rule_bodies(phone, ".img-up-actions")
    assert "display: flex" in actions
    assert ".img-up" not in _after_phone(), "no desktop rules for the phone hooks"


# ── The public image page (ISS-119, D7 A) ────────────────────────────────

_VIEW = _UPLOADS[0]
_COPY_IDS = {"copy-share-url": "share-url", "copy-raw-url": "raw-url"}


def _render_view(*, session=None, is_owner=False):
    kw = {} if session == "logged-in" else {"session": session}
    return render_page(img_mod, "tools_image_view.html", f"/i/{_VIEW.id}", img=_VIEW,
                       is_owner=is_owner, **kw)


def _copy_buttons(html):
    return [a for t, a in _tags(html) if t == "button" and a.get("id") in _COPY_IDS]


def _page_script(html):
    scripts = re.findall(r'<script nonce="test-nonce">(.*?)</script>', html, flags=re.S)
    mine = [s for s in scripts if "copy-share-url" in s]
    assert len(mine) == 1, "exactly one nonce'd page script binds the Copy buttons"
    return mine[0]


def test_anonymous_image_page_needs_no_actions_js():
    html = _render_view()
    assert "/static/js/actions.js" not in html, "the anonymous render really is anonymous"
    assert 'id="share-url"' in html, "the full page rendered, not the error state"
    found = _ACTION_BINDING.search(html)
    assert not found, f"{found.group(0).strip()} is dead without actions.js (ISS-119)"


def test_copy_buttons_have_stable_hooks():
    for html in (_render_view(), _render_view(session="logged-in", is_owner=True)):
        buttons = _copy_buttons(html)
        assert sorted(b["id"] for b in buttons) == sorted(_COPY_IDS)
        for b in buttons:
            assert "data-click" not in b, "bound by the page script, never through actions.js"
            assert b.get("type") == "button"
            assert b.get("data-field-id") == _COPY_IDS[b["id"]]
            assert "m-tap" not in _cls(b), "a button keeps the 44px floor"


def test_page_script_binds_both_copy_buttons():
    script = _page_script(_render_view())
    body = re.sub(r"/\*.*?\*/", "", script, flags=re.S).strip()
    assert body.startswith("(function") and body.rstrip(";").endswith(")()"), (
        "an IIFE, so the page defines no global copy handler")
    for bid in _COPY_IDS:
        assert re.search(rf"""['"]{bid}['"]""", script), f"#{bid} is bound by id"
    assert "addEventListener('click'" in script
    # Gesture-safe: select the field, then write synchronously in the tap,
    # falling back to execCommand('copy') on the selection.
    # Checked on the comment-stripped body: a comment naming the fallback
    # must not stand in for the code.
    assert ".select()" in body and "setSelectionRange" in body
    assert "navigator.clipboard.writeText" in body
    assert "execCommand('copy')" in body
    # When both paths fail the button says so, rather than nothing.
    assert "'Select & copy'" in body
    # A second tap inside the "Copied" window must not make "Copied" the
    # label it restores: the label is read once, at bind time.
    assert "clearTimeout" in script


def test_share_fields_stack_on_phones():
    tags = _tags(_render_view())
    stacks = [a for t, a in tags if t == "div" and "m-stack" in _cls(a)]
    assert len(stacks) == 2
    found = _inside(_render_view(), lambda t, a: t == "div" and "m-stack" in _cls(a))
    for inner, (bid, field) in zip(found, _COPY_IDS.items()):
        assert [(t, a.get("id")) for t, a in inner] == [("input", field), ("button", bid)]


def test_share_fields_ellipsise_on_phones():
    """A live share URL is wider than a 360px phone at the 16px input size,
    so each field ends in an ellipsis instead of being silently cut. The
    hook is a class on these two inputs only: the D-Scan page has its own
    #share-url."""
    inputs = [a for t, a in _tags(_render_view()) if t == "input"]
    assert [a.get("id") for a in inputs if "img-share-url" in _cls(a)] == ["share-url", "raw-url"]
    body = rule_bodies(_phone(), ".img-share-url")
    assert "text-overflow: ellipsis" in body and "white-space: nowrap" in body
    assert "#share-url" not in _section("T3") and "#raw-url" not in _section("T3")


def test_owner_back_and_delete_keep_the_44px_floor():
    """Back is an a.b-btn and Delete a button.b-btn, so the global phone
    rule already makes both 44px; m-tap would cut them to 40px."""
    html = _render_view(session="logged-in", is_owner=True)
    tags = _tags(html)
    back = [a for t, a in tags
            if t == "a" and a.get("href") == "/tools/images" and "b-btn" in _cls(a)]
    assert len(back) == 1 and "m-tap" not in _cls(back[0])
    forms = [a for t, a in tags if t == "form" and a.get("action") == f"/tools/images/{_VIEW.id}/delete"]
    assert len(forms) == 1 and forms[0].get("data-confirm") == "Delete this image?"
    delete = _inside(html, lambda t, a: t == "form" and a.get("action", "").endswith("/delete"))
    buttons = [a for t, a in delete[0] if t == "button"]
    assert len(buttons) == 1 and "m-tap" not in _cls(buttons[0])
    # When Back wraps to two lines, Delete keeps its height, and Back's
    # block link centres its text in the taller box.
    owner = _inside(html, lambda t, a: "img-owner-actions" in _cls(a))
    assert len(owner) == 1 and [t for t, _ in owner[0]][:2] == ["a", "form"]
    phone = _phone()
    assert "display: flex" in rule_bodies(phone, ".img-owner-actions > form")
    back_rule = rule_bodies(phone, ".img-owner-actions > a")
    assert "display: flex" in back_rule and "align-items: center" in back_rule


def test_anonymous_viewer_gets_no_owner_controls():
    html = _render_view()
    assert "/delete" not in html


# ── Discord Time (D8 A) ──────────────────────────────────────────────────

def test_format_row_template_keeps_its_lookups():
    """dtCopy finds a row's button through #dt-tag-X's parent, so the rows
    keep their markup; the phone layout is CSS alone (no tap-to-open)."""
    src = source("discordtime.html")
    render = src[src.index("function dtRenderOutput"):src.index("function dtUpdate")]
    assert "'<div class=\"dt-format-row\">'" in render
    assert "id=\"dt-tag-' + f.key + '\"" in render
    assert "'<button class=\"dt-copy-btn\" data-click=\"dtCopy\" data-fmt=\"' + f.key + '\">Copy</button>'" in render
    assert "m-row" not in render and "toggleMRow" not in render
    copy = src[src.index("function dtCopy"):src.index("function dtCopyAll")]
    assert "getElementById('dt-tag-' + fmt)" in copy
    assert "parentElement.querySelector('.dt-copy-btn')" in copy


def test_format_rows_are_two_lines_on_phones():
    phone = _phone()
    row = rule_bodies(phone, "#dt-output > .dt-format-row")
    assert "display: grid" in row
    areas = re.search(r"grid-template-areas:\s*([^;]+);", row)
    assert areas and re.findall(r'"([^"]+)"', areas.group(1)) == ["label copy", "preview preview"]
    label = rule_bodies(phone, ".dt-format-row > .dt-fmt-label")
    assert "grid-area: label" in label and "width: auto" in label
    assert "display: none" in rule_bodies(phone, ".dt-format-row > .dt-tag")
    preview = rule_bodies(phone, ".dt-format-row > .dt-preview")
    assert "grid-area: preview" in preview and "white-space: normal" in preview
    assert "grid-area: copy" in rule_bodies(phone, ".dt-format-row > .dt-copy-btn")


def test_discord_buttons_keep_the_floor_and_are_never_forced_visible():
    """Copy All and Share Link start with an inline display:none until a time
    exists, so no rule here may set display on a .dt-copy-btn. Every Copy is
    a button, so the global 44px floor sizes it; this rule only widens it
    and sets no height of its own."""
    phone = _phone()
    btn = rule_bodies(phone, "button.dt-copy-btn")
    assert "min-width: 40px" in btn
    assert "height" not in btn, "the global button rule gives Copy its 44px"
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", phone):
        if "dt-copy-btn" in m.group(1):
            assert not re.search(r"(?<![-\w])display\s*:", m.group(2)), m.group(1).strip()
    html = render_page(dt_mod, "discordtime.html", "/tools/discordtime")
    for bid in ("dt-copy-all-btn", "dt-share-btn"):
        tag = [a for t, a in _tags(html) if a.get("id") == bid]
        assert len(tag) == 1
        assert "display:none" in tag[0].get("style", "")
        assert "m-tap" not in _cls(tag[0])


def test_mode_tabs_are_a_44px_bar():
    tab = rule_bodies(_phone(), ".dt-tabs > .dt-tab")
    assert "min-height: 44px" in tab
    assert re.search(r"flex:\s*1 1 0", tab)
    html = render_page(dt_mod, "discordtime.html", "/tools/discordtime")
    tabs = [a for t, a in _tags(html) if t == "button" and "dt-tab" in _cls(a)]
    assert [a.get("data-mode") for a in tabs] == ["utc", "local", "offset"]


def test_quick_actions_keep_two_columns_on_phones():
    src = source("discordtime.html")
    style = src[src.index("<style"):src.index("</style>")]
    assert re.search(r"\.dt-quick-grid\s*\{[^}]*grid-template-columns:\s*1fr 1fr", style)
    assert not re.search(r"@media[^{]*max-width:\s*640px", style), (
        "the template's own phone rule made Quick Actions one column")
    assert "dt-quick-grid" not in _phone()


# ── Structure Age ─────────────────────────────────────────────────────────

_PASTE = "<url=showinfo:35833//1234567890123>J123456 - Sample Citadel (Sample Corp)</url>"


def _sa_ctx():
    return {"paste": _PASTE,
            "parsed": {"structure_id": 1234567890123, "jcode": "J123456", "corp_name": "Sample Corp"},
            "estimate": {"method": "interpolate", "age_str": "≈ 2y 3m", "days_wide": 12.5,
                         "mid_str": "04 Jul 2024", "low_str": "28 Jun 2024", "high_str": "10 Jul 2024",
                         "note": "Sample note"},
            "error": None}


def test_structure_age_page_is_anonymous_safe():
    html = render_page(sa_mod, "structure_age.html", "/tools/structure-age", session=None, **_sa_ctx())
    assert "/static/js/actions.js" not in html
    assert "sa-age" in html and "J123456" in html, "the result card rendered"
    found = _ACTION_BINDING.search(html)
    assert not found, f"{found.group(0).strip()} is dead without actions.js"
    codes = [a for t, a in _tags(html) if t == "code"]
    assert len(codes) == 1 and "sa-showinfo" in _cls(codes[0]), "the in-game link has its wrap hook"
    submit = [a for t, a in _tags(html) if t == "button" and "sa-submit" in _cls(a)]
    assert len(submit) == 1 and "m-tap" not in _cls(submit[0]), "Estimate keeps the 44px floor"


def test_structure_age_link_wraps_on_phones():
    assert "overflow-wrap: anywhere" in rule_bodies(_phone(), ".sa-showinfo")


def test_shared_result_card_changes_only_on_phones():
    """The WH Tracker panel and the wormhole system page embed the result
    card and share the .sa-* rules. This section restyles only the tool
    page's .sa-showinfo hook and, for all three pages, the card's System
    link (44px) and its 9px labels (11px), all inside the phone block, so
    desktop is unchanged; the partial renders as before, anonymous-safe."""
    sels = [s for m in re.finditer(r"([^{}]+)\{", _section("T3")) for s in selectors(m.group(1))]
    sa = {s for s in sels if ".sa-" in s}
    assert sa == {".sa-showinfo", ".sa-meta-item > a", ".sa-date-lbl", ".sa-method"}, sa
    assert phone_block(_section("T3"))[1].strip() == "", "no desktop rule"
    link = rule_bodies(_phone(), ".sa-meta-item > a")
    assert "display: inline-flex" in link and "min-height: 44px" in link
    for sel in (".sa-date-lbl", ".sa-method"):
        assert "font-size: 11px" in rule_bodies(_phone(), sel), sel
    html = render_page(sa_mod, "structure_age_result.html", "/tools/structure-age/partial",
                       session=None, **_sa_ctx())
    assert not _ACTION_BINDING.search(html)
    assert "m-tap" not in html and "m-row" not in html


# ── The 44px floor (every page in this task) ─────────────────────────────

def _all_renders():
    sa = _sa_ctx()
    return {
        "uploads": _render_uploads(),
        "view-anon": _render_view(),
        "view-owner": _render_view(session="logged-in", is_owner=True),
        "discordtime": render_page(dt_mod, "discordtime.html", "/tools/discordtime"),
        "structure-age": render_page(sa_mod, "structure_age.html", "/tools/structure-age",
                                     session=None, **sa),
        "structure-age-empty": render_page(sa_mod, "structure_age.html", "/tools/structure-age",
                                           session=None, paste="", parsed=None, estimate=None,
                                           error=None),
    }


def test_no_button_carries_m_tap():
    """The shared .m-tap rule forces 40px with !important, below the 44px
    every button and .b-btn already gets on phones. Only links may use it."""
    for name, html in _all_renders().items():
        for t, a in _tags(html):
            if "m-tap" in _cls(a):
                assert t == "a" and "b-btn" not in _cls(a), f"{name}: m-tap on <{t} {a}>"


def test_section_sets_no_height_below_the_floor():
    for m in re.finditer(r"(?<![-\w])(min-height|height)\s*:\s*(\d+(?:\.\d+)?)px", _section("T3")):
        assert float(m.group(2)) >= 44, f"{m.group(0)} is below the 44px phone floor"
