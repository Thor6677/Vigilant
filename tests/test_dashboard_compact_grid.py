"""T-076 Part A item 2: the compact row grid.

One shared CSS grid template — no row carries its own inline
`grid-template-columns`, and the class-level rule defines the fixed tracks
(dot, portrait, name, location, ship, wallet, training, flags) once. Flags
render on one line (`flex-wrap: nowrap`, not `wrap`), and the wallet cell is
right-aligned with tabular figures.

Row markup (content block) is checked via the render_full harness; the
shared CSS rule itself lives in dashboard.html's `head` block, which that
harness never renders (it calls the `content` block directly) — those
assertions go through the real route instead, mirroring
tests/test_dashboard_route_render.py's client fixture.
"""
import asyncio
import base64
import json
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


def test_shared_grid_template_definition_has_eight_tracks(client):
    """Two CSS rules define `.dash-compact-row`'s grid-template-columns —
    the base 8-track layout and the <=480px responsive override (5 tracks,
    checked separately below) — both class-level, never per-row inline
    (see test_no_compact_row_carries_an_inline_grid_template)."""
    client.post("/dashboard/prefs", json={"mode": "compact"})
    html = client.get("/dashboard").text
    matches = re.findall(r"\.dash-compact-row\s*\{[^}]*grid-template-columns:([^;]+);", html)
    assert len(matches) == 2, f"expected base + responsive .dash-compact-row rules, found {len(matches)}"
    # minmax(140px, 1.2fr) has an internal space — collapse it before
    # splitting on whitespace so it counts as one track, not two.
    base_tracks = re.sub(r"\(([^)]*)\)", lambda m: m.group(0).replace(" ", ""), matches[0]).strip().split()
    assert len(base_tracks) == 8, f"expected 8 grid tracks (dot/portrait/name/loc/ship/wallet/training/flags), got {base_tracks}"
    mobile_tracks = matches[1].strip().split()
    assert len(mobile_tracks) == 5, f"expected 5 tracks (dot/portrait/name/wallet/flags) under the phone breakpoint, got {mobile_tracks}"


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
