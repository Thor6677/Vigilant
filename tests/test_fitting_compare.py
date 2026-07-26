"""Tests for fit comparison + damage profiles (Phase 5 Task 4).

Pure-math tests hit ``resolve_damage_profile`` / ``_calc_ehp`` (the display
seam: profile weighting of already-computed layer HP/resonances) and
``build_compare_sections`` (delta direction per stat — the sig-radius /
align-time class of "lower is better" stats must color correctly).

Route tests use the signed-session-cookie idiom from tests/test_networth.py
plus a get_db dependency override onto a temp-file sqlite DB, so ownership
scoping is proven against the real route + real SQL without touching the
app's production database. SDE tables exist but are empty — the engine
returns all-zero stats for a bare unknown hull, which is fine: these tests
assert access control and page shape, not stat values.
"""
import asyncio
import base64
import json
import tempfile

import itsdangerous
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, UserFitting, get_db
from app.fitting.compare import COMPARE_STAT_SECTIONS, build_compare_sections
from app.fitting.engine import DAMAGE_PROFILES, _calc_ehp, resolve_damage_profile

USER_A = 71
USER_B = 72


# ── resolve_damage_profile ──────────────────────────────────────────────────

def test_presets_resolve_to_expected_weights():
    assert resolve_damage_profile("uniform") == (0.25, 0.25, 0.25, 0.25)
    assert resolve_damage_profile("em") == (1.0, 0.0, 0.0, 0.0)
    assert resolve_damage_profile("thermal") == (0.0, 1.0, 0.0, 0.0)
    assert resolve_damage_profile("kinetic") == (0.0, 0.0, 1.0, 0.0)
    assert resolve_damage_profile("explosive") == (0.0, 0.0, 0.0, 1.0)


def test_unknown_preset_falls_back_to_uniform():
    assert resolve_damage_profile("nonsense") == DAMAGE_PROFILES["uniform"]
    # "custom" with no weights list is a named-preset miss -> uniform too.
    assert resolve_damage_profile("custom") == DAMAGE_PROFILES["uniform"]


def test_custom_weights_normalized_to_sum_one():
    prof = resolve_damage_profile("uniform", custom=[50, 30, 15, 5])
    assert prof == (0.50, 0.30, 0.15, 0.05)
    # Raw non-percentage input normalizes the same way.
    prof = resolve_damage_profile("uniform", custom=[2, 1, 1, 0])
    assert prof == (0.5, 0.25, 0.25, 0.0)
    assert abs(sum(prof) - 1.0) < 1e-12


def test_custom_overrides_named_preset():
    prof = resolve_damage_profile("em", custom=[0, 0, 0, 10])
    assert prof == (0.0, 0.0, 0.0, 1.0)


def test_degenerate_custom_falls_back_to_preset():
    # All-zero weights: undefined profile -> preset wins.
    assert resolve_damage_profile("em", custom=[0, 0, 0, 0]) == (1.0, 0.0, 0.0, 0.0)
    # Wrong length / junk types -> preset wins.
    assert resolve_damage_profile("em", custom=[1, 2]) == (1.0, 0.0, 0.0, 0.0)
    assert resolve_damage_profile("em", custom=["x", 1, 1, 1]) == (1.0, 0.0, 0.0, 0.0)
    # Negative components clamp to zero before normalizing.
    assert resolve_damage_profile("uniform", custom=[-5, 0, 0, 10]) == (0.0, 0.0, 0.0, 1.0)


# ── profile-weighted EHP math ───────────────────────────────────────────────

# Fixture layer: 1000 HP with resonances em=0.5, therm=0.8, kin=0.6, expl=0.9
# (i.e. resists 50/20/40/10).
HP = 1000.0
RES = (0.5, 0.8, 0.6, 0.9)


def _ehp(profile):
    return _calc_ehp(HP, *RES, profile)


def test_ehp_uniform_profile():
    # weighted resonance = (0.5+0.8+0.6+0.9)/4 = 0.7
    assert abs(_ehp(resolve_damage_profile("uniform")) - HP / 0.7) < 1e-9


def test_ehp_single_type_profiles():
    assert abs(_ehp(resolve_damage_profile("em")) - HP / 0.5) < 1e-9          # 2000
    assert abs(_ehp(resolve_damage_profile("thermal")) - HP / 0.8) < 1e-9     # 1250
    assert abs(_ehp(resolve_damage_profile("kinetic")) - HP / 0.6) < 1e-9
    assert abs(_ehp(resolve_damage_profile("explosive")) - HP / 0.9) < 1e-9


def test_ehp_custom_profile():
    # 50% EM / 50% Thermal -> weighted resonance 0.65
    prof = resolve_damage_profile("uniform", custom=[50, 50, 0, 0])
    assert abs(_ehp(prof) - HP / 0.65) < 1e-9
    # Best-resisted type gives the highest EHP; worst gives the lowest.
    assert _ehp(resolve_damage_profile("em")) > _ehp(resolve_damage_profile("explosive"))


def test_ehp_guards():
    assert _calc_ehp(0, *RES, (0.25, 0.25, 0.25, 0.25)) == 0
    # 100% resist across the board -> near-infinite sentinel, not div-by-zero.
    assert _calc_ehp(HP, 0, 0, 0, 0, (0.25, 0.25, 0.25, 0.25)) == HP * 1000


# ── delta-direction table ───────────────────────────────────────────────────

def _all_rows():
    for section in COMPARE_STAT_SECTIONS:
        for row in section["rows"]:
            yield row  # (label, key, direction, fmt, guard)


def test_direction_table_lower_is_better_stats():
    directions = {key: direction for _, key, direction, _, _ in _all_rows()}
    # The inverted stats — a hard-coded "higher is better" here would
    # green a fatter, slower, easier-to-lock fit.
    for key in ("sig_radius", "align_time", "lock_time", "cap_drain_rate"):
        assert directions[key] == "lower", key
    for key in ("total_ehp", "total_dps", "max_velocity", "weapon_volley",
                "cap_stable_pct", "warp_speed_au_s"):
        assert directions[key] == "higher", key
    assert directions["cap_stable"] == "bool"


def _stats(**over):
    base = {
        "shield_ehp": 1000, "armor_ehp": 2000, "hull_ehp": 500, "total_ehp": 3500,
        "shield_rep_rate": 0, "armor_rep_rate": 10, "peak_shield_recharge": 5,
        "shield_em_resist": 12.5, "shield_therm_resist": 40, "shield_kin_resist": 50,
        "shield_expl_resist": 60, "armor_em_resist": 60, "armor_therm_resist": 45,
        "armor_kin_resist": 30, "armor_expl_resist": 20,
        "weapon_dps": 300, "drone_dps": 50, "total_dps": 350,
        "total_dps_max_spool": 350, "weapon_volley": 1200,
        "max_velocity": 1500, "align_time": 4.0, "sig_radius": 40,
        "lock_time": 3.0, "warp_speed_au_s": 5.0,
        "cap_stable": True, "cap_stable_pct": 55, "cap_lasts_s": 0,
        "cap_drain_rate": 20,
    }
    base.update(over)
    return base


def _find_row(sections, label):
    for s in sections:
        for r in s["rows"]:
            if r["label"] == label:
                return r
    raise AssertionError(f"row {label!r} not found")


def test_lower_sig_radius_in_b_is_better():
    sections = build_compare_sections(_stats(sig_radius=120), _stats(sig_radius=35))
    row = _find_row(sections, "Sig radius")
    assert row["cls"] == "better" and row["delta"].startswith("-")


def test_higher_sig_radius_in_b_is_worse():
    row = _find_row(
        build_compare_sections(_stats(sig_radius=35), _stats(sig_radius=120)),
        "Sig radius")
    assert row["cls"] == "worse" and row["delta"].startswith("+")


def test_lower_speed_in_b_is_worse():
    row = _find_row(
        build_compare_sections(_stats(max_velocity=2000), _stats(max_velocity=900)),
        "Max velocity")
    assert row["cls"] == "worse" and row["delta"].startswith("-")


def test_higher_align_time_in_b_is_worse():
    row = _find_row(
        build_compare_sections(_stats(align_time=3.0), _stats(align_time=9.0)),
        "Align time")
    assert row["cls"] == "worse"


def test_equal_values_marked_same():
    row = _find_row(build_compare_sections(_stats(), _stats()), "Total EHP")
    assert row["cls"] == "same" and row["delta"] == "—"


def test_cap_stable_rendered_as_bool_row():
    row = _find_row(
        build_compare_sections(_stats(cap_stable=True), _stats(cap_stable=False)),
        "Cap stable")
    assert row["a"] == "Stable" and row["b"] == "Unstable"
    assert row["cls"] == "bool" and row["delta"] == ""


def test_spool_row_hidden_when_neither_fit_spools():
    sections = build_compare_sections(_stats(), _stats())
    labels = [r["label"] for s in sections for r in s["rows"]]
    assert "Total DPS (max spool)" not in labels


def test_spool_row_shown_when_one_fit_spools():
    spooler = _stats(total_dps=400, total_dps_max_spool=700)
    sections = build_compare_sections(_stats(), spooler)
    row = _find_row(sections, "Total DPS (max spool)")
    assert row["cls"] == "better"  # 700 > 350


# ── compare route: gating + ownership ───────────────────────────────────────

def _client():
    import app.main as main
    return TestClient(main.app)


def _authed_client(user_id=USER_A):
    """TestClient carrying a signed session cookie for `user_id`. Uses an https
    base_url because the session cookie is Secure outside debug mode."""
    import app.main as main

    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    data = base64.b64encode(json.dumps({"user_id": user_id}).encode())
    cookie = signer.sign(data).decode()
    client = TestClient(main.app, base_url="https://testserver")
    client.cookies.set("vigilant_session", cookie)
    return client


def test_compare_redirects_when_unauthenticated():
    r = _client().get("/tools/fitting/compare?a=1&b=2", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/"


def _seeded_app_db():
    """Temp sqlite DB seeded with fits for two users; returns (fit ids, teardown)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            mine_1 = UserFitting(user_id=USER_A, name="Alpha", ship_type_id=587,
                                 items_json="[]")
            mine_2 = UserFitting(user_id=USER_A, name="Bravo", ship_type_id=602,
                                 items_json="[]")
            theirs = UserFitting(user_id=USER_B, name="Hostile", ship_type_id=587,
                                 items_json="[]")
            db.add_all([mine_1, mine_2, theirs])
            await db.commit()
            return mine_1.id, mine_2.id, theirs.id

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        ids = loop.run_until_complete(seed())
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    import app.main as main
    main.app.dependency_overrides[get_db] = override_get_db

    def teardown():
        main.app.dependency_overrides.pop(get_db, None)

    return ids, teardown


def test_compare_renders_own_fits_and_404s_foreign_fit():
    (mine_1, mine_2, theirs), teardown = _seeded_app_db()
    try:
        client = _authed_client(USER_A)

        # Both fits owned -> 200, names on the page, implants deferral noted.
        r = client.get(f"/tools/fitting/compare?a={mine_1}&b={mine_2}")
        assert r.status_code == 200
        assert "Alpha" in r.text and "Bravo" in r.text
        assert "implants not modeled" in r.text

        # Another user's fit in either slot -> 404 (not 403 — never leak
        # that the id exists).
        assert client.get(
            f"/tools/fitting/compare?a={mine_1}&b={theirs}").status_code == 404
        assert client.get(
            f"/tools/fitting/compare?a={theirs}&b={mine_1}").status_code == 404

        # Nonexistent id -> 404 too.
        assert client.get(
            f"/tools/fitting/compare?a={mine_1}&b=999999").status_code == 404
    finally:
        teardown()
