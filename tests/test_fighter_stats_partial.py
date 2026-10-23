"""Fighter data for the page: ship slot limits, the stats panel and the compare row (ISS-136).

Server side only: a temp SQLite file with a private event loop per test, and
the stats partial rendered through the route module's own templates.env. The
page logic that reads these is in test_fighter_page_logic.py.
"""
import asyncio
import json
import tempfile
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.main  # noqa: F401 — populates every router's templates.env.globals
from app.db.models import Base
from app.db.sde_models import SDETypeDogmaAttribute
from app.fitting.compare import build_compare_sections
from app.routes import fitting as fitting_mod

CARRIER, TRISTAN = 9001, 9002


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


async def _with_db(fn):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as db:
            # A carrier-like hull: 4 tubes, 3 light, 2 support, no heavy attribute.
            for attr, val in ((2216, 4.0), (2217, 3.0), (2218, 2.0), (2055, 65000.0)):
                db.add(SDETypeDogmaAttribute(type_id=CARRIER, attribute_id=attr, value=val))
            # A frigate-like hull: none of the fighter attributes at all.
            db.add(SDETypeDogmaAttribute(type_id=TRISTAN, attribute_id=283, value=40.0))
            await db.commit()
            return await fn(db)
    finally:
        await engine.dispose()


def test_ship_slots_reports_fighter_limits_with_missing_attributes_as_zero():
    async def go(db):
        req = SimpleNamespace(session={"user_id": 1})
        return (await fitting_mod.ship_slots(req, CARRIER, "", db),
                await fitting_mod.ship_slots(req, TRISTAN, "", db))
    carrier, tristan = _run(_with_db(go))
    assert (carrier["fighter_tubes"], carrier["fighter_light"],
            carrier["fighter_support"], carrier["fighter_heavy"]) == (4, 3, 2, 0)
    assert carrier["fighter_hangar_base"] == 65000.0
    assert (tristan["fighter_tubes"], tristan["fighter_light"],
            tristan["fighter_support"], tristan["fighter_heavy"]) == (0, 0, 0, 0)
    assert tristan["fighter_hangar_base"] == 0.0


class _Zero(dict):
    def __missing__(self, key):
        return 0


def _render_stats(**stats):
    # A missing stat renders as 0, as in test_drone_bay_limits.
    base = {k: 0 for k in ("total_dps", "spool_time_s", "total_dps_max_spool", "total_ehp")}
    base.update(stats)
    try:
        return fitting_mod.templates.env.get_template("partials/fitting_stats.html").render(stats=_Zero(base))
    except Exception as exc:  # pragma: no cover - surfaced as a clear failure
        pytest.fail(f"stats partial did not render: {exc}")


class _Attrs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sum_attrs = {}

    def handle_starttag(self, tag, attrs):
        if "data-fit-sum" in dict(attrs):
            self.sum_attrs = dict(attrs)


def _sum_attrs(html):
    p = _Attrs()
    p.feed(html)
    return p.sum_attrs


CARRIER_STATS = dict(
    fighter_tubes_used=3, fighter_tubes_total=4,
    fighter_light_used=2, fighter_light_total=3,
    fighter_support_used=1, fighter_support_total=2,
    fighter_heavy_used=0, fighter_heavy_total=0,
    fighter_hangar_used=24000.0, fighter_hangar_total=81250.0,
    fighter_squadrons=3,
)


def test_fighter_data_attributes_ride_on_the_summary_element():
    info = {"7001": {"class": "light", "max_size": 6, "volume": 1000.0, "primary": "attack"}}
    html = _render_stats(fighter_info=info, fighter_hangar_total=81250.0, drone_volumes={"1001": 5.0})
    attrs = _sum_attrs(html)
    # tojson leaves its quotes bare, so a double-quoted attribute would end at
    # the first one and the page would read nothing; both must parse as JSON.
    assert json.loads(attrs["data-drone-vols"]) == {"1001": 5.0}
    assert json.loads(attrs["data-fighter-info"]) == info
    assert float(attrs["data-fighter-hangar"]) == 81250.0


def test_fighter_dps_row_appears_only_above_zero_and_carries_the_effective_arrow():
    stats = dict(total_dps=300.0, fighter_dps=267.3, fighter_primary_dps=267.3,
                 effective_total_dps=200.0, effective_fighter_dps=180.0,
                 target_resist_profile="test_profile")
    html = _render_stats(**stats)
    assert "Fighter DPS" in html and "267.3" in html and "180.0" in html
    assert "Missiles" not in html          # no salvo, so no split
    assert "Fighter DPS" not in _render_stats(**{**stats, "fighter_dps": 0})
    # Fighters alone are enough to open the Offense section.
    assert "Fighter DPS" in _render_stats(total_dps=0, fighter_dps=50.0)


def test_attack_and_missile_split_is_shown_when_there_is_a_salvo():
    html = _render_stats(total_dps=441.0, fighter_dps=441.0, fighter_primary_dps=267.3,
                         fighter_salvo_dps=173.7)
    assert "Attack" in html and "267.3" in html
    assert "Missiles" in html and "173.7" in html


def test_fighter_block_shows_tubes_classes_and_hangar_and_skips_unused_classes():
    html = _render_stats(**CARRIER_STATS)
    assert 'id="fighter-stats"' in html
    assert "Launch Tubes" in html and "3 / 4" in html
    assert "Light Squadrons" in html and "2 / 3" in html
    assert "Support Squadrons" in html and "1 / 2" in html
    assert "Heavy Squadrons" not in html          # the hull has no heavy slots
    assert "24,000 / 81,250" in html
    assert "Over the fighter hangar" not in html


def test_over_hangar_and_squadrons_without_a_tube_are_red():
    html = _render_stats(**{**CARRIER_STATS, "fighter_hangar_used": 90000.0, "fighter_not_counted": 2})
    assert "Over the fighter hangar" in html
    assert "2 squadrons" in html and "Not counted" in html
    assert html.count("var(--danger)") >= 2
    ok = _render_stats(**CARRIER_STATS)
    assert "Not counted" not in ok
    assert "1 squadron<" in _render_stats(**{**CARRIER_STATS, "fighter_not_counted": 1})


def test_fighter_block_is_hidden_without_tubes_or_fighters_but_not_with_fighters_alone():
    assert 'id="fighter-stats"' not in _render_stats()
    # A fit holding fighters on a tubeless hull must still say so.
    html = _render_stats(fighter_squadrons=2, fighter_tubes_total=0, fighter_hangar_total=0,
                         fighter_hangar_used=3000.0)
    assert 'id="fighter-stats"' in html and "0 / 0" in html
    assert "Over the fighter hangar" in html


def test_compare_has_a_fighter_dps_row_after_drone_dps():
    sections = build_compare_sections({"fighter_dps": 100.0, "drone_dps": 5.0},
                                      {"fighter_dps": 150.0, "drone_dps": 5.0})
    offense = next(s for s in sections if s["name"] == "Offense")
    labels = [r["label"] for r in offense["rows"]]
    assert labels.index("Fighter DPS") == labels.index("Drone DPS") + 1
    row = offense["rows"][labels.index("Fighter DPS")]
    assert row["cls"] == "better" and row["a"] == "100.0" and row["b"] == "150.0"


def test_stats_route_survives_hostile_fighter_quantities():
    """A raw Infinity/NaN/1e999 in the body parses as JSON; it used to be a 500."""
    from tests.test_fitting_boosters_routes import _CSRF, _csrf_client, _seeded_db
    teardown = _seeded_db()
    try:
        client = _csrf_client()
        for raw in ("Infinity", "-Infinity", "NaN", "1e999", "1e300", "true", '"7"'):
            body = ('{"ship_type_id": 587, "items": [{"type_id": 7001, "type_name": "Test Light", '
                    '"slot": "fighter", "quantity": %s, "online": true}]}' % raw)
            r = client.post("/tools/fitting/stats", content=body,
                            headers={"X-CSRF-Token": _CSRF, "Content-Type": "application/json"})
            assert r.status_code == 200, (raw, r.text[:200])
    finally:
        teardown()
