"""Page scripts must be cache-busted by content, not by a hand-bumped number.

The edge serves /static/ as `Cache-Control: public, immutable, max-age=604800`.
base.html used to load `actions.js?v=1` and `notifications.js?v=6`, and nobody
bumped those: actions.js changed in every release from v1.3.3 on — banner
dismiss, then eighteen fragments' worth of handlers — under a URL that never
did. A returning browser kept the old file for up to a week, and `immutable`
means a plain reload would not even revalidate it, so for that user every
handler living only in actions.js was undefined: the banner × did nothing
(ISS-039). Stylesheets had been hashed since the restyle; scripts now are.
"""
import glob
import hashlib
import os
import re

import app.main as main
from tests.test_route_auth_gating import _client, admin_user_id  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_js_version_is_a_content_hash_of_every_page_script():
    h = hashlib.md5()
    for p in sorted(glob.glob(os.path.join(ROOT, "static/js/*.js"))):
        with open(p, "rb") as fh:
            h.update(fh.read())
    assert main.JS_V == h.hexdigest()[:8]
    assert re.fullmatch(r"[0-9a-f]{8}", main.JS_V)


def test_no_template_pins_a_static_script_to_a_literal_version():
    offenders = []
    for path in glob.glob(os.path.join(ROOT, "app/templates/**/*.html"), recursive=True):
        with open(path, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                for m in re.finditer(r'src="(/static/js/[^"]+)"', line):
                    src = m.group(1)
                    if "?v={{ js_v }}" not in src:
                        offenders.append(f"{os.path.relpath(path, ROOT)}:{n}: {src}")
    assert not offenders, (
        "Static script(s) not cache-busted by js_v — a returning browser will "
        "keep the old file for 7 days:\n  " + "\n  ".join(offenders)
    )


def test_rendered_page_carries_the_hash(admin_user_id):
    # A page that renders base.html's authed chrome without touching ESI or
    # the sync tables — the script tags are behind base.html's user_id gate.
    r = _client(user_id=admin_user_id, role="admin").get("/tools/discordtime")
    assert r.status_code == 200
    assert f"/static/js/actions.js?v={main.JS_V}" in r.text
    assert f"/static/js/notifications.js?v={main.JS_V}" in r.text
