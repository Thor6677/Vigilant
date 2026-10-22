"""Parse the /industry/copy-packs/plan form into validated planner inputs.

Everything a browser sends is clamped or dropped here, so the route and the
engine only ever see sane values: quantity 1-1000, ME 0-10, TE 0-20, skill
levels 0-5, facility keys from the existing whitelists, and at most
MAX_OVERRIDES entries per override map. `state_json` (the page's per-product
toggles) and `skills_json` (the character's manual skill levels) are JSON;
anything unparseable in them is ignored, never an error.
"""
from __future__ import annotations

import json
from itertools import islice
from dataclasses import dataclass, field

from app.industry.copy_pack import PLANNER_SKILLS
from app.industry.manufacturing import RIGS, SEC_STATUS, STRUCTURES

MAX_QUANTITY = 1000
MAX_JSON_CHARS = 20_000
MAX_OVERRIDES = 500
MAX_ID = 2**62  # inside SQLite's signed 64-bit INTEGER

_PLANNER_SKILL_IDS = {sid for sid, _label in PLANNER_SKILLS}


@dataclass
class PlanForm:
    type_id: int = 0
    quantity: int = 1
    character_id: int = 0
    structure: str = "npc_station"
    rig: str = "none"
    security: str = "highsec"
    skill_overrides: dict = field(default_factory=dict)  # skill_id -> level 0-5
    build: dict = field(default_factory=dict)            # product_type_id -> bool
    bpo_choice: dict = field(default_factory=dict)       # product_type_id -> item_id
    me_override: dict = field(default_factory=dict)      # product_type_id -> ME, no-BPO items
    te_override: dict = field(default_factory=dict)      # product_type_id -> TE, no-BPO items
    ignore_bpcs: frozenset = frozenset()                 # product type ids whose owned BPCs to leave out


def _int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _id(value) -> int:
    """A positive id within SQLite's INTEGER range, else 0."""
    n = _int(value, 0)
    return n if 0 < n <= MAX_ID else 0


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def _json_object(raw) -> dict:
    if not isinstance(raw, str) or not raw or len(raw) > MAX_JSON_CHARS:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _id_map(raw, cast) -> dict:
    """{positive int key: cast(value)} from a JSON object; junk keys and
    values that `cast` rejects are skipped."""
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    for key, value in islice(raw.items(), MAX_OVERRIDES):
        k = _id(key)
        if not k:
            continue
        try:
            out[k] = cast(value)
        except (TypeError, ValueError):
            continue
    return out


def _level(lo: int, hi: int):
    def cast(value) -> int:
        n = _int(value)
        if n is None:
            raise ValueError(value)
        return _clamp(n, lo, hi)
    return cast


def _item_id(value) -> int:
    n = _id(value)
    if not n:
        raise ValueError(value)
    return n


def _flag(value) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(value)


def parse_plan_form(form) -> PlanForm:
    state = _json_object(form.get("state_json"))
    skills = _id_map(_json_object(form.get("skills_json")), _level(0, 5))
    ignore = state.get("ignore_bpc")
    ignore_ids = {_id(x) for x in islice(ignore if isinstance(ignore, list) else [], MAX_OVERRIDES)}

    structure = form.get("structure") or "npc_station"
    rig = form.get("rig") or "none"
    security = form.get("security") or "highsec"
    return PlanForm(
        type_id=_id(form.get("type_id")),
        quantity=_clamp(_int(form.get("quantity"), 1), 1, MAX_QUANTITY),
        character_id=_id(form.get("character_id")),
        structure=structure if structure in STRUCTURES else "npc_station",
        rig=rig if rig in RIGS else "none",
        security=security if security in SEC_STATUS else "highsec",
        skill_overrides={k: v for k, v in skills.items() if k in _PLANNER_SKILL_IDS},
        build=_id_map(state.get("build"), _flag),
        bpo_choice=_id_map(state.get("bpo"), _item_id),
        me_override=_id_map(state.get("me"), _level(0, 10)),
        te_override=_id_map(state.get("te"), _level(0, 20)),
        ignore_bpcs=frozenset(i for i in ignore_ids if i > 0),
    )
