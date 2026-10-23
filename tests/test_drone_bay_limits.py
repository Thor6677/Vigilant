"""The fitting tool must respect the drone bay (ISS-135) and accept fighters (ISS-136).

The page used to accept any number of drones on any ship: 13 light drones in a
40 m3 bay, and drones on a carrier whose bay is 0. A fighter picked in the
module browser added nothing and said nothing (it now adds a squadron; the
fighter rules are in test_fighter_page_logic.py). Bandwidth is not a carry limit
(it only caps how many drones launch, which the engine already applies to DPS),
so it is flagged in the stats panel instead of refused.

Server side: a temp SQLite file, a private event loop per test. Page logic:
the page's own functions run in node against stubs.
"""
import asyncio
import json
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.main  # noqa: F401 — populates every router's templates.env.globals
from app.db.models import Base
from app.db.sde_models import SDEGroup, SDETypeDogmaAttribute, SDEType
from app.routes import fitting as fitting_mod
from app.sde import lookup as sde
from tests._mobile import render_page

MARKET = 500
DRONE_CAT, FIGHTER_CAT, MODULE_CAT = 18, 87, 7
DRONE_GROUP, FIGHTER_GROUP, MODULE_GROUP = 100, 200, 101
HORNET, SCOUT, FIGHTER, MODULE = 1001, 1002, 5001, 2001
TRISTAN, ARCHON = 593, 23757


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
            db.add_all([
                SDEGroup(group_id=DRONE_GROUP, category_id=DRONE_CAT, group_name="Combat Drone"),
                SDEGroup(group_id=FIGHTER_GROUP, category_id=FIGHTER_CAT, group_name="Light Fighter"),
                SDEGroup(group_id=MODULE_GROUP, category_id=MODULE_CAT, group_name="Gun"),
            ])
            for tid, name, grp, vol in [(HORNET, "Hornet I", DRONE_GROUP, 5.0),
                                        (SCOUT, "Scout I", DRONE_GROUP, 25.0),
                                        (FIGHTER, "Test Fighter", FIGHTER_GROUP, 3000.0),
                                        (MODULE, "Test Gun", MODULE_GROUP, 5.0)]:
                db.add(SDEType(type_id=tid, type_name=name, group_id=grp, market_group_id=MARKET,
                               published=True, volume=vol))
            db.add_all([
                SDETypeDogmaAttribute(type_id=TRISTAN, attribute_id=283, value=40.0),
                SDETypeDogmaAttribute(type_id=TRISTAN, attribute_id=1271, value=25.0),
                SDETypeDogmaAttribute(type_id=ARCHON, attribute_id=283, value=0.0),
                SDETypeDogmaAttribute(type_id=ARCHON, attribute_id=1271, value=0.0),
            ])
            await db.commit()
            return await fn(db)
    finally:
        await engine.dispose()


# ── server side ─────────────────────────────────────────────────────

def test_browse_items_carry_volume_and_mark_fighters():
    async def go(db):
        return {i["type_id"]: i for i in await sde.get_market_group_items(db, MARKET)}
    items = _run(_with_db(go))
    assert items[HORNET]["volume"] == 5.0 and items[SCOUT]["volume"] == 25.0
    assert items[HORNET]["slot_type"] == "drone"
    assert items[FIGHTER]["slot_type"] == "fighter"
    # A plain unslotted type outside both categories is untouched.
    assert items[MODULE]["slot_type"] is None


def test_drone_search_results_carry_volume():
    async def go(db):
        return await sde.search_drones(db, "scout")
    assert _run(_with_db(go))[0]["volume"] == 25.0


def test_ship_slots_reports_the_drone_bay_and_bandwidth():
    async def go(db):
        req = SimpleNamespace(session={"user_id": 1})
        return (await fitting_mod.ship_slots(req, TRISTAN, "", db),
                await fitting_mod.ship_slots(req, ARCHON, "", db))
    tristan, archon = _run(_with_db(go))
    assert (tristan["drone_bay"], tristan["drone_bandwidth"]) == (40.0, 25.0)
    assert (archon["drone_bay"], archon["drone_bandwidth"]) == (0.0, 0.0)


def test_stats_panel_flags_bay_and_bandwidth_overuse_and_carries_volumes():
    stats = {k: 0 for k in ("total_dps", "spool_time_s", "total_dps_max_spool", "total_ehp")}
    stats.update(drone_bay_used=50.0, drone_bay_total=40.0, drone_bw_used=50.0, drone_bw_total=25.0,
                 drone_volumes={"1001": 5.0})
    html = _render_stats(stats)
    assert html.count("var(--danger)") >= 2
    assert 'data-drone-vols="{&#34;1001&#34;: 5.0}"' in html or "data-drone-vols=" in html
    ok = _render_stats({**stats, "drone_bay_used": 10.0, "drone_bw_used": 10.0})
    assert "Over the drone bay" not in ok and "Over bandwidth" not in ok


def _render_stats(stats):
    # Only the sections under test are needed; a missing stat renders empty
    # under the page's lenient undefined, so feed a mapping that answers 0.
    class Zero(dict):
        def __missing__(self, key):
            return 0
    try:
        return fitting_mod.templates.env.get_template("partials/fitting_stats.html").render(stats=Zero(stats))
    except Exception as exc:  # pragma: no cover - surfaced as a clear failure
        pytest.fail(f"stats partial did not render: {exc}")


# ── page logic, run in node ─────────────────────────────────────────

@pytest.fixture(scope="module")
def script():
    page = render_page(fitting_mod, "fitting_tool.html", "/tools/fitting", folder_paths=[])
    blocks = re.findall(r"<script nonce=[^>]*>(.*?)</script>", page, re.S)
    body = [b for b in blocks if "function renderSlots" in b]
    assert len(body) == 1
    return body[0]


def _fn(script, name):
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", script)
    assert m, f"no function {name}() in the page script"
    depth, i = 1, m.end()
    while depth:
        depth += (script[i] == "{") - (script[i] == "}")
        i += 1
    return script[m.start():i]


HARNESS = r"""
const names = %(names)s;
const src = %(src)s;
const notes = {};
const mk = (id) => notes[id] || (notes[id] = {id, textContent: '', hidden: true, getAttribute: () => statsAttr});
let statsAttr = '{}';
global.document = {
  getElementById: mk,
  querySelector: () => ({getAttribute: () => statsAttr}),
  querySelectorAll: () => [],
};
global.state = {shipTypeId: 593, items: []};
global.shipSlots = {drone_bay: 40, drone_bandwidth: 25};
global.saveState = () => {}; global.renderSlots = () => {}; global.recalcStats = () => {};
global.isSlotFull = () => false; global.setTimeout = () => 0; global.clearTimeout = () => {};
global._noteBrowserAdd = () => {}; global._browserAddNoteText = () => 'ok';
global._origAddModule = () => { state.items.push({slot: 'other'}); };
// Test-only: evaluates the page's own function source, extracted from this repo's template.
eval(src + '\n' + names.map(n => 'global.' + n + ' = ' + n + ';').join('\n'));
global.droneVolumes = droneVolumes;
const out = {};
const note = () => (notes['fit-limit-note'] || {}).textContent || '';
const reset = (slots, vols) => { state.items = []; shipSlots = slots; global.shipSlots = slots;
  Object.keys(droneVolumes).forEach(k => delete droneVolumes[k]); Object.assign(droneVolumes, vols || {});
  notes['fit-limit-note'] && (notes['fit-limit-note'].textContent = ''); };
%(scenarios)s
console.log(JSON.stringify(out));
"""

SCENARIOS = r"""
// A: bay 40, 5 m3 drones: eight fit, the ninth is refused.
reset({drone_bay: 40, drone_bandwidth: 25});
let results = [];
for (let i = 0; i < 9; i++) results.push(addDrone(1001, 'Hornet I', 5));
out.A = {results, qty: state.items[0].quantity, note: note()};

// B: a ship with no drone bay refuses outright.
reset({drone_bay: 0, drone_bandwidth: 0});
out.B = {result: addDrone(1001, 'Hornet I', 5), n: state.items.length, note: note()};

// C: slot data not loaded yet (null): nothing is refused.
reset({drone_bay: null, drone_bandwidth: null});
out.C = {result: addDrone(1001, 'Hornet I', 5), n: state.items.length};

// D: the + button obeys the same limit; - always works.
reset({drone_bay: 10, drone_bandwidth: 25}, {1001: 5});
addDrone(1001, 'Hornet I', 5); addDrone(1001, 'Hornet I', 5);
changeDroneQty(0, 1);
const afterPlus = state.items[0].quantity;
changeDroneQty(0, -1);
out.D = {afterPlus, afterMinus: state.items[0].quantity, note: note()};

// E: a fighter from the browser adds a squadron of its own.
reset({drone_bay: 40, drone_bandwidth: 25});
addModuleFromBrowser(5001, 'Test Fighter', 'fighter');
out.E = {n: state.items.length, slot: state.items[0] && state.items[0].slot,
         online: state.items[0] && state.items[0].online, note: note()};

// F: volumes learned from the stats panel cover drones that came from an import.
reset({drone_bay: 10, drone_bandwidth: 25});
state.items.push({type_id: 1002, type_name: 'Scout I', slot: 'drone', quantity: 1});
statsAttr = JSON.stringify({1002: 8, 1001: 5});
_syncDroneVolumes();
out.F = {tooBig: addDrone(1001, 'Hornet I'), note: note()};

// G: a drone of unknown volume is let through (the stats panel flags it).
reset({drone_bay: 10, drone_bandwidth: 25});
out.G = {result: addDrone(7777, 'Mystery Drone'), n: state.items.length};

// H: no ship picked yet: no limit applies.
reset({drone_bay: 0, drone_bandwidth: 0});
state.shipTypeId = null;
out.H = {result: addDrone(1001, 'Hornet I', 5)};
state.shipTypeId = 593;

// I: a normal drone from the browser still reaches the bay.
reset({drone_bay: 40, drone_bandwidth: 25});
addModuleFromBrowser(1001, 'Hornet I', 'drone', 5);
out.I = {n: state.items.length, slot: state.items[0] && state.items[0].slot};
"""

NAMES = ["_fmtM3", "_limitNote", "_droneBayUsed", "droneRoomProblem", "_syncDroneVolumes",
         "changeDroneQty", "addDrone", "_fighterClass", "_fighterHardProblem", "_fighterFit",
         "_fighterWanted", "_fighterWalk", "_fighterHangarUsed", "fighterRoomProblem", "addFighter",
         "addModuleFromBrowser"]


@pytest.fixture(scope="module")
def result(script, tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    parts = ["var droneVolumes = {};", "var _limitNoteTimer = null;", "var fighterInfo = {};",
             "var fighterHangarTotal = null;", "var FIGHTER_CLASSES = ['light', 'support', 'heavy'];", "var NOT_LAUNCHABLE = 'not a launchable fighter';"]
    parts += [_fn(script, n) for n in NAMES]
    harness = HARNESS % {"names": json.dumps(NAMES), "src": json.dumps("\n".join(parts)),
                         "scenarios": SCENARIOS}
    path = tmp_path_factory.mktemp("node") / "harness.js"
    path.write_text(harness)
    run = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_bay_fills_to_the_limit_and_refuses_the_next_drone(result):
    r = result["A"]
    assert r["results"] == [True] * 8 + [False]
    assert r["qty"] == 8
    assert "drone bay full (40 / 40" in r["note"]


def test_a_ship_with_no_drone_bay_takes_no_drones(result):
    r = result["B"]
    assert r["result"] is False and r["n"] == 0
    assert "no drone bay" in r["note"]


def test_nothing_is_refused_before_the_ship_data_arrives_or_without_a_ship(result):
    assert result["C"] == {"result": True, "n": 1}
    assert result["H"]["result"] is True


def test_plus_button_obeys_the_bay_and_minus_always_works(result):
    r = result["D"]
    assert r["afterPlus"] == 2 and r["afterMinus"] == 1
    assert "drone bay full" in r["note"]


def test_fighter_from_the_browser_adds_a_squadron(result):
    r = result["E"]
    assert (r["n"], r["slot"], r["online"]) == (1, "fighter", True)
    assert "not supported" not in r["note"]


def test_imported_drones_count_toward_the_bay_via_the_stats_volumes(result):
    r = result["F"]
    assert r["tooBig"] is False and "drone bay full" in r["note"]


def test_unknown_volume_is_let_through(result):
    assert result["G"] == {"result": True, "n": 1}


def test_a_normal_drone_from_the_browser_still_lands_in_the_bay(result):
    assert result["I"] == {"n": 1, "slot": "drone"}
