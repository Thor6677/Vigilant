"""The ESI scope guard and the permission catalog it serves.

The promise these pin: an authenticated ESI call only happens when the
character's token carries the scope for it — checked in one place, before any
cache — and every scope Vigilant can ask for is described in the catalog the
user chooses from.
"""
import ast
import asyncio
import base64
import json
import re
from pathlib import Path

import pytest

from app.auth import scopes as cat
from app.esi import scope_guard
from app.esi.scope_guard import ScopeNotGranted
from app.esi.scope_map import AUTH_OPS

APP = Path("app")


def _jwt(scp) -> str:
    """A token-shaped string whose payload carries ``scp``. Unsigned: the guard
    only ever narrows, so it never verifies."""
    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'none'})}.{b64({'sub': 'CHARACTER:EVE:1', 'scp': scp})}.sig"


# ── granted_scopes ───────────────────────────────────────────────────────────

def test_scp_as_a_list():
    assert scope_guard.granted_scopes(_jwt([cat.WALLET, cat.ASSETS])) == {cat.WALLET, cat.ASSETS}


def test_scp_as_a_single_string():
    """EVE emits a bare string when exactly one scope was granted."""
    assert scope_guard.granted_scopes(_jwt(cat.WALLET)) == {cat.WALLET}


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b.c", _jwt(None)])
def test_anything_unparseable_grants_nothing(token):
    assert scope_guard.granted_scopes(token) == frozenset()


# ── check ────────────────────────────────────────────────────────────────────

def test_a_granted_scope_passes():
    scope_guard.check("GET", "/characters/1/wallet/", frozenset({cat.WALLET}))


def test_a_missing_scope_is_refused_and_named():
    with pytest.raises(ScopeNotGranted) as exc:
        scope_guard.check("GET", "/characters/1/wallet/journal/", frozenset({cat.ASSETS}))
    assert exc.value.scope == cat.WALLET


def test_public_operations_need_nothing():
    for path in ("/alliances/99/", "/universe/stations/60003760/", "/status/"):
        scope_guard.check("GET", path, frozenset())


def test_unknown_operations_fail_closed():
    with pytest.raises(ScopeNotGranted) as exc:
        scope_guard.check("GET", "/characters/1/something-new/", frozenset(cat.ALL_SCOPES))
    assert exc.value.scope is None


def test_the_method_matters():
    """POST /ui/autopilot/waypoint writes to the game; there is no GET form of
    it, and a GET must not be mistaken for it."""
    with pytest.raises(ScopeNotGranted):
        scope_guard.check("GET", "/ui/autopilot/waypoint/", frozenset({cat.WAYPOINT}))
    scope_guard.check("POST", "/ui/autopilot/waypoint/", frozenset({cat.WAYPOINT}))


def test_literal_segments_beat_placeholders():
    known, scope, roles = scope_guard.lookup("GET", "/corporations/9/wallets/1/journal/")
    assert known and scope == cat.CORP_WALLETS
    assert set(roles) == {"Accountant", "Junior_Accountant"}


# ── ESIClient wiring ─────────────────────────────────────────────────────────

def test_client_refuses_before_touching_either_cache(monkeypatch):
    """A withdrawn permission must stop being served from Vigilant's caches the
    moment the new token is in use — so the refusal comes before cache_get and
    before the network, not after."""
    from app.esi import client as client_mod

    async def boom(*a, **k):
        raise AssertionError("cache consulted before the permission check")

    monkeypatch.setattr(client_mod, "cache_get", boom)

    async def no_network(*a, **k):
        raise AssertionError("network used before the permission check")

    c = client_mod.ESIClient(_jwt([cat.ASSETS]), cache_enabled=True)
    monkeypatch.setattr(c, "_raw_get", no_network)
    with pytest.raises(ScopeNotGranted):
        asyncio.run(c.get("/characters/1/wallet/"))


def test_client_post_is_guarded_too():
    from app.esi.client import ESIClient
    c = ESIClient(_jwt([cat.ASSETS]))
    with pytest.raises(ScopeNotGranted):
        asyncio.run(c.post("/ui/autopilot/waypoint/", params={"destination_id": 1}))


# ── Coverage: every ESI path the app calls is known to the table ─────────────

_ESI_ROOTS = ("/characters/", "/corporations/", "/corporation/", "/alliances/", "/universe/",
              "/ui/", "/markets/", "/fleets/", "/status", "/killmails/", "/sovereignty/")


def _template(node) -> str | None:
    """Best-effort string template of a path argument: f-strings and '+'
    concatenations become '/characters/{x}/assets/'."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = ""
        for v in node.values:
            out += v.value if isinstance(v, ast.Constant) else "{x}"
        return out
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _template(node.left), _template(node.right)
        return (left if left is not None else "{x}") + (right if right is not None else "{x}")
    return None


def _esi_calls():
    """(file:line, method, path) for every .get()/.post() whose first argument
    looks like an ESI path. Route decorators (@router.get("/characters")) are
    excluded — they declare Vigilant's own URLs."""
    found = []
    for py in APP.rglob("*.py"):
        tree = ast.parse(py.read_text())
        decorators = {id(d) for n in ast.walk(tree)
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                      for d in n.decorator_list}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if id(node) in decorators or node.func.attr not in ("get", "post") or not node.args:
                continue
            path = _template(node.args[0])
            if path and path.startswith(_ESI_ROOTS):
                found.append((f"{py}:{node.lineno}", node.func.attr.upper(), path))
    return found


def test_the_scan_actually_finds_calls():
    calls = _esi_calls()
    assert len(calls) > 30, calls
    assert any("/wallet/" in p for _, _, p in calls)


def test_every_esi_call_in_the_app_is_in_the_table():
    unknown = [(where, m, p) for where, m, p in _esi_calls()
               if not scope_guard.lookup(m, p.replace("{x}", "1"))[0]]
    assert not unknown, f"ESI paths the scope map does not know (regenerate it?): {unknown}"


def test_every_authenticated_call_needs_a_catalog_scope():
    """If code calls an endpoint whose scope is not in the catalog, users can
    never grant it — the feature would silently never work."""
    missing = []
    for where, m, p in _esi_calls():
        _known, scope, _roles = scope_guard.lookup(m, p.replace("{x}", "1"))
        if scope and scope not in cat.ALL_SCOPES:
            missing.append((where, scope))
    assert not missing, missing


# ── The catalog ──────────────────────────────────────────────────────────────

_SPEC_SCOPES = {scope for _m, _p, scope, _r in AUTH_OPS}


def test_every_catalog_scope_exists_in_the_spec():
    assert set(cat.ALL_SCOPES) <= _SPEC_SCOPES, set(cat.ALL_SCOPES) - _SPEC_SCOPES


def test_catalog_in_game_roles_come_from_the_spec():
    """The roles shown next to a corp permission must be what ESI enforces,
    not what someone remembered."""
    for perm in cat.PERMISSIONS:
        if not perm.in_game_roles:
            continue
        spec_roles = {tuple(sorted(r)) for _m, _p, s, r in AUTH_OPS if s in perm.scopes and r}
        assert tuple(sorted(perm.in_game_roles)) in spec_roles, (perm.key, spec_roles)


def test_every_scope_string_in_the_app_is_in_the_catalog():
    """Scope strings outside the catalog would be requested nowhere and could
    never be granted. Allowed exception: the catalog's own docstring naming the
    retired corp-mining scope."""
    rx = re.compile(r"esi-[a-z_]+\.[a-z_]+\.v1")
    stray = set()
    for f in list(APP.rglob("*.py")) + list(APP.rglob("*.html")):
        if f.name == "scope_map.py":
            continue
        for s in rx.findall(f.read_text()):
            if s not in cat.ALL_SCOPES and not (f.name == "scopes.py" and s == "esi-industry.read_corporation_mining.v1"):
                stray.add((str(f), s))
    assert not stray, stray


def test_writes_are_flagged():
    """The picker marks game-changing permissions; the waypoint scope is the
    only write Vigilant asks for."""
    assert [p.key for p in cat.PERMISSIONS if p.writes] == ["waypoints"]
    assert all(".write_" not in s for p in cat.PERMISSIONS if not p.writes for s in p.scopes)


def test_corp_permissions_pull_in_corp_roles():
    assert "corp_roles" in cat.normalize_keys(["corp_wallets"])
    assert cat.normalize_keys(["wallet", "bogus"]) == ["wallet"]


def test_presets():
    assert set(cat.scopes_for(cat.PRESETS["everything"])) == set(cat.ALL_SCOPES)
    assert cat.scopes_for(cat.PRESETS["minimal"]) == []
    personal = set(cat.scopes_for(cat.PRESETS["personal"]))
    assert cat.CORP_WALLETS not in personal and cat.WALLET in personal


def test_states():
    wallet = cat.BY_KEY["wallet"]
    loc = cat.BY_KEY["location"]
    assert cat.state_of(wallet, cat.WALLET, "") == cat.GRANTED
    assert cat.state_of(wallet, "", cat.WALLET) == cat.DECLINED
    assert cat.state_of(wallet, "", "") == cat.NEW
    assert cat.state_of(loc, cat.LOCATION, "") == cat.PARTIAL


def test_retired_scopes_are_reported_as_extra():
    assert cat.extra_scopes(f"{cat.WALLET} esi-industry.read_corporation_mining.v1") == \
        {"esi-industry.read_corporation_mining.v1"}
