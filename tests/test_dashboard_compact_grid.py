"""T-076 Part A item 2 / T-078: the compact row grid.

One shared CSS grid template per breakpoint — no row carries its own inline
`grid-template-columns`, and the class-level rules define the tracks (dot,
portrait, name, location, ship, wallet, training, flags) once each. Flags
render on one line at the base and <=1000px tiers (`flex-wrap: nowrap`), but
wrap onto a second line at the <=760px tier, where location/ship/training
are gone and there's a fixed-width flags column to wrap inside instead of
overflowing it. The wallet cell is right-aligned with tabular figures at
every tier.

T-078 also fixed a row-to-row column-drift bug: each `.dash-compact-row` is
its own independent grid container, so a bare `auto` or bare `Nfr` track
resolves its base size from THAT row's own content, shifting every track
after it. Every flexible track is now `minmax(0, Nfr)` (explicit zero
minimum) and flags is a fixed px width instead of `auto` — see the CSS
comment in dashboard.html and test_grid_tracks_by_breakpoint below.

Row markup (content block) is checked via the render_full harness; the
shared CSS rules themselves live in dashboard.html's `head` block, which
that harness never renders (it calls the `content` block directly) — those
assertions go through the real route instead, mirroring
tests/test_dashboard_route_render.py's client fixture.
"""
import asyncio
import base64
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone

import itsdangerous
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.main as main
from app.auth import scopes as perms
from app.db.models import Base, Character, CharacterDashboardCache, User, get_db
from tests._dashboard_fixture import CHARACTERS, render_full

CSRF = "test-csrf-token"
USER_ID = 821
CHAR_ID = 90201001


def _cookie(secret_key: str, user_id: int) -> str:
    signer = itsdangerous.TimestampSigner(secret_key)
    data = base64.b64encode(json.dumps({"user_id": user_id, "csrf_token": CSRF}).encode())
    return signer.sign(data).decode()


@pytest.fixture
def client():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(User(id=USER_ID, role="user"))
            db.add(Character(
                character_id=CHAR_ID, character_name="Pilot One", user_id=USER_ID,
                is_main=True, account_group="Sample Corp", sort_order=0,
                scopes=" ".join(perms.ALL_SCOPES), declined_scopes="",
                access_token="x", refresh_token="x",
                token_expiry=datetime.now(timezone.utc) + timedelta(days=1),
            ))
            db.add(CharacterDashboardCache(character_id=CHAR_ID, sync_status="idle"))
            await db.commit()
    asyncio.run(seed())

    async def _override():
        async with SessionLocal() as s:
            yield s
    main.app.dependency_overrides[get_db] = _override
    c = TestClient(main.app, base_url="https://testserver")
    c.cookies.set("vigilant_session", _cookie(main.settings.secret_key, USER_ID))
    c.headers.update({"X-CSRF-Token": CSRF})
    yield c
    main.app.dependency_overrides.pop(get_db, None)


def test_no_compact_row_carries_an_inline_grid_template():
    html = render_full("custom", dash_mode="compact")
    for m in re.finditer(r'<a[^>]*class="dash-compact-row"[^>]*style="([^"]*)"', html):
        assert "grid-template-columns" not in m.group(1), (
            "a compact row must not override the shared grid template inline"
        )


def test_every_row_shares_the_same_class_and_no_row_count_mismatch():
    html = render_full("custom", dash_mode="compact")
    rows = re.findall(r'class="dash-compact-row"', html)
    assert len(rows) == len(CHARACTERS)


def _tracks(raw):
    # minmax(0, 1.2fr) has an internal space — collapse it before splitting
    # on whitespace so it counts as one track, not two.
    return re.sub(r"\(([^)]*)\)", lambda m: m.group(0).replace(" ", ""), raw).strip().split()


def _media_block(html, px):
    """Balanced-brace extraction of one `@media (max-width: Npx) { ... }`
    block, so its `.dash-compact-row` rule (and any sibling rules, e.g.
    `.dash-compact-ship { display: none; }`) is tied to ITS OWN breakpoint —
    not just matched by source order against some other regex, which would
    stay green even if a breakpoint's rules were reordered or duplicated."""
    marker = f"@media (max-width: {px}px)"
    start = html.index(marker)
    brace_start = html.index("{", start)
    depth = 0
    for i in range(brace_start, len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                return html[start:i + 1]
    raise AssertionError(f"unbalanced {marker} block")


def _row_tracks(css_block):
    m = re.search(r"\.dash-compact-row\s*\{[^}]*grid-template-columns:([^;]+);", css_block)
    assert m is not None, f"no .dash-compact-row rule in block: {css_block!r}"
    return _tracks(m.group(1))


# A content-dependent track: a bare `auto` (not `minmax(0, auto)`/inside a
# minmax call) or a bare `Nfr` with no explicit zero minimum. Either one
# resolves its base size from that particular row's own content — see the
# CSS comment in dashboard.html for why that causes row-to-row column drift.
_BARE_AUTO_OR_FR = re.compile(r"(?<!minmax\()\bauto\b|^\d*\.?\d*fr$")


def test_grid_tracks_by_breakpoint(client):
    """T-078: pins the exact track list at each breakpoint (not just a
    count), and enforces the anti-drift rule the CSS comment documents —
    every track in the 8-track and 7-track tiers is either `Npx` or
    `minmax(0, Nfr)`, never a bare `auto` or a bare `Nfr` (both of which
    resolve their size from that one row's own content — see
    test_grid_tracks_by_breakpoint)."""
    client.post("/dashboard/prefs", json={"mode": "compact"})
    html = client.get("/dashboard").text

    base_m = re.search(r"\.dash-compact-row\s*\{[^}]*grid-template-columns:([^;]+);", html)
    assert base_m is not None
    base_tracks = _tracks(base_m.group(1))
    assert len(base_tracks) == 8, f"expected 8 grid tracks (dot/portrait/name/loc/ship/wallet/training/flags), got {base_tracks}"

    mid_block = _media_block(html, 1000)
    assert ".dash-compact-ship" in mid_block and "display: none" in mid_block.replace(";", "")
    mid_tracks = _row_tracks(mid_block)
    assert len(mid_tracks) == 7, f"expected 7 tracks (dot/portrait/name/loc/wallet/training/flags) under <=1000px, got {mid_tracks}"

    small_block = _media_block(html, 760)
    for cls in (".dash-compact-loc", ".dash-compact-ship", ".dash-compact-training"):
        assert cls in small_block, f"{cls} must be hidden under <=760px"
    small_tracks = _row_tracks(small_block)
    assert len(small_tracks) == 5, f"expected 5 tracks (dot/portrait/name/wallet/flags) under <=760px, got {small_tracks}"

    # No content-dependent track in the base or 7-track tier.
    for tier_name, tracks in (("base", base_tracks), ("<=1000px", mid_tracks)):
        for t in tracks:
            assert not _BARE_AUTO_OR_FR.search(t), (
                f"{tier_name} tier track {t!r} is content-dependent (bare auto/fr) "
                "and will drift row to row"
            )
    # The 5-track tier keeps `auto` deliberately for wallet (brief: "wallet
    # auto, right-aligned"), which is safe there ONLY because the track
    # after it (flags) is a fixed px width — assert that explicitly.
    assert small_tracks[3] == "auto", f"expected wallet (4th track) to stay auto, got {small_tracks}"
    assert re.fullmatch(r"\d+px", small_tracks[4]), (
        f"flags (last track) must be a fixed px width for wallet's right edge to stay constant, got {small_tracks[4]!r}"
    )


def test_responsive_breakpoints_are_1000_and_760px():
    """T-078: the exact breakpoints the fix documents — a >760px scroll gap
    between 481px and ~760px was the bug (fixed tracks totalling >380px with
    no override in that range), so these two values are load-bearing, not
    arbitrary."""
    content_html = render_full("custom", dash_mode="compact")
    assert "@media (max-width: 1000px)" not in content_html  # head block, not content

    with open(
        os.path.join(os.path.dirname(__file__), "..", "app", "templates", "dashboard.html")
    ) as f:
        source = f.read()
    assert "@media (max-width: 1000px)" in source
    assert "@media (max-width: 760px)" in source
    assert "@media (max-width: 480px)" not in source, "old phone-only breakpoint should be gone, replaced by 760px"


def test_every_compact_row_carries_a_left_border():
    """T-078: every row gets a 3px left border — transparent when there's no
    warning state, coloured as before when there is one — so columns line
    up across warned and unwarned rows alike (previously unwarned rows had
    no border-left at all and sat 3px left of the others)."""
    html = render_full("custom", dash_mode="compact")
    rows = re.findall(r'<a[^>]*class="dash-compact-row"[^>]*style="([^"]*)"', html)
    assert len(rows) == len(CHARACTERS)
    saw_transparent = False
    saw_colored = False
    for style in rows:
        assert re.search(r"border-left:3px solid", style), f"row missing a left border: {style!r}"
        if "border-left:3px solid transparent" in style:
            saw_transparent = True
        else:
            saw_colored = True
    # The fixture set has both warned and unwarned pilots — prove both
    # branches of the new else clause actually render, not just one.
    assert saw_transparent, "expected at least one row with no warning (transparent border)"
    assert saw_colored, "expected at least one row with a warning (coloured border)"


def test_flags_render_on_one_line_not_wrapped(client):
    client.post("/dashboard/prefs", json={"mode": "compact"})
    html = client.get("/dashboard").text
    m = re.search(r"\.dash-compact-flags\s*\{([^}]*)\}", html)
    assert m is not None
    assert "flex-wrap:nowrap" in m.group(1).replace(" ", "").replace("\n", "")


def test_wallet_column_is_right_aligned_and_tabular(client):
    client.post("/dashboard/prefs", json={"mode": "compact"})
    html = client.get("/dashboard").text
    m = re.search(r"\.dash-compact-wallet\s*\{([^}]*)\}", html)
    assert m is not None
    body = m.group(1).replace(" ", "").replace("\n", "")
    assert "text-align:right" in body
    assert "font-variant-numeric:tabular-nums" in body


def test_compact_grid_css_absent_outside_compact_mode(client):
    for mode in ("cards", "detailed", "table"):
        client.post("/dashboard/prefs", json={"mode": mode})
        html = client.get("/dashboard").text
        assert ".dash-compact-row {" not in html, f"compact grid CSS leaked into {mode} mode"
