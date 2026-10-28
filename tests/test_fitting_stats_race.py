"""A slow stats response must not overwrite a newer one (ISS-138).

recalcStats() posts on every change and used to write whichever response
arrived last, so an older, slower answer could replace the current numbers (and
the drone volumes the bay check reads from the panel). The page's own function
runs in node against stubs, with fetches resolved out of order.
"""
import json
import re
import shutil
import subprocess

import pytest

import app.main  # noqa: F401 — populates every router's templates.env.globals
from app.routes import fitting as fitting_mod
from tests._mobile import render_page

HARNESS = r"""
const src = %(src)s;
const panel = {innerHTML: ''};
const pending = [];
global.document = {getElementById: (id) => id === 'stats-panel' ? panel : null};
global.state = {shipTypeId: 593, items: []};
global.getSelectedSkillCharId = () => null; global.getSelectedSkillPreset = () => null; global.getSelectedProfile = () => 'uniform';
global.getCustomWeights = () => null; global.getImplantTypeIds = () => []; global.getBoosterPayload = () => [];
global._fitCheckOk = (r) => r;
global._fitErrorHtml = (s) => 'ERROR ' + s;
global._syncDroneVolumes = () => {}; global._syncFighterInfo = () => {}; global._fitSumBarUpdate = () => {}; global.refreshSkillWarnings = () => {};
// each fetch parks until the scenario settles it, in whatever order it likes
global.fetch = () => new Promise((resolve, reject) => pending.push({resolve, reject}));
const ok = (html) => ({text: () => Promise.resolve(html)});
// Test-only: evaluates the page's own function source, extracted from this repo's template.
eval(src + '\nglobal.recalcStats = recalcStats;');
const tick = () => new Promise((r) => setImmediate(r));
(async () => {
  const out = {};
  // A: the second answer lands first, the first (older) one lands last.
  recalcStats(); recalcStats();
  pending[1].resolve(ok('new')); await tick();
  pending[0].resolve(ok('old')); await tick();
  out.A = panel.innerHTML;
  // B: in order, each one still applies.
  pending.length = 0; panel.innerHTML = '';
  recalcStats(); pending[0].resolve(ok('first')); await tick();
  recalcStats(); pending[1].resolve(ok('second')); await tick();
  out.B = panel.innerHTML;
  // C: an older failure arriving after a newer success must not show an error.
  pending.length = 0; panel.innerHTML = '';
  recalcStats(); recalcStats();
  pending[1].resolve(ok('good')); await tick();
  pending[0].reject({status: 500}); await tick();
  out.C = panel.innerHTML;
  // D: the latest request failing still shows the error.
  pending.length = 0; panel.innerHTML = '';
  recalcStats(); pending[0].reject({status: 500}); await tick();
  out.D = panel.innerHTML;
  console.log(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    page = render_page(fitting_mod, "fitting_tool.html", "/tools/fitting", folder_paths=[])
    blocks = [b for b in re.findall(r"<script nonce=[^>]*>(.*?)</script>", page, re.S)
              if "function renderSlots" in b]
    assert len(blocks) == 1
    script = blocks[0]
    m = re.search(r"function\s+recalcStats\s*\([^)]*\)\s*\{", script)
    assert m, "no recalcStats() in the page script"
    depth, i = 1, m.end()
    while depth:
        depth += (script[i] == "{") - (script[i] == "}")
        i += 1
    seq = re.search(r"var\s+_statsSeq\s*=\s*0;", script)
    src = (seq.group(0) if seq else "") + "\n" + script[m.start():i]
    path = tmp_path_factory.mktemp("node") / "harness.js"
    path.write_text(HARNESS % {"src": json.dumps(src)})
    run = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_an_older_response_arriving_last_is_ignored(result):
    assert result["A"] == "new"


def test_responses_in_order_each_apply(result):
    assert result["B"] == "second"


def test_an_older_failure_does_not_replace_a_newer_success(result):
    assert result["C"] == "good"


def test_the_latest_request_failing_still_shows_the_error(result):
    assert result["D"] == "ERROR 500"
