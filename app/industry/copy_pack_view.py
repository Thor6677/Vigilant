"""Copy-pack results as the user reads them: the Pack to hand over and the
owned BPCs grouped by where they sit (/industry/copy-packs).

Pure functions over a finished `Plan`; no DB or ESI access.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.industry import copy_pack as cp


@dataclass(frozen=True)
class PackLine:
    kind: str                # "copy" (made by this plan) or "bpc" (already owned)
    count: int
    runs: int                # runs on each BPC
    me: int
    te: int
    uses: int | None = None  # runs this plan uses; owned BPCs only


@dataclass(frozen=True)
class PackEntry:
    product_type_id: int
    lines: list


@dataclass(frozen=True)
class Pack:
    entries: list
    total: int
    new: int
    existing: int


@dataclass(frozen=True)
class CollectRow:
    product_type_id: int
    me: int
    te: int
    uses: int
    runs: int


@dataclass(frozen=True)
class CollectGroup:
    owner: str
    place: str
    rows: list


def place_label(o: cp.OwnedBlueprint, location_names: dict) -> str:
    """Where a blueprint sits. A corp hangar keeps its division next to the
    structure name; a blueprint inside a ship or container says which one,
    then where that is; anything else is the name alone, then the flag label."""
    named = location_names.get(o.location_id) if o.location_id else None
    label = o.location_label or ""
    if o.holder_label:
        if o.holder_in_space:
            return f"{o.holder_label} (in space, {named})" if named else f"{o.holder_label} (in space)"
        return f"{o.holder_label} in {named}" if named else f"{o.holder_label} (location unknown)"
    if named and label.startswith("Corp Hangar"):
        return f"{named}, {label}"
    return named or label or "Unknown location"


def build_pack(plan: cp.Plan) -> Pack:
    """What the builder is handed: per built blueprint, the copies this plan
    makes and the owned BPCs it uses. Items with nothing in hand (no BPO and
    no BPC) are left out; the page lists them elsewhere."""
    order: list[int] = []
    for r in plan.tree:
        if r.build and r.product_type_id in plan.items and r.product_type_id not in order:
            order.append(r.product_type_id)
    entries, new, existing = [], 0, 0
    for t in order:
        ip = plan.items[t]
        lines = []
        if ip.group == cp.GROUP_COPY and ip.copies > 0:
            lines.append(PackLine("copy", ip.copies, ip.runs_per_copy, ip.me, ip.te))
            new += ip.copies
        for o, n in ip.bpcs_used:
            lines.append(PackLine("bpc", 1, o.runs, o.me, o.te, uses=n))
            existing += 1
        if lines:
            entries.append(PackEntry(t, lines))
    return Pack(entries, new + existing, new, existing)


def group_bpcs(plan: cp.Plan, location_names: dict, names: dict) -> list:
    """The BPCs the plan uses, grouped by owner and place: biggest group
    first, then by owner and place name; rows within a group by item name."""
    groups: dict[tuple[str, str], list] = {}
    for ip in plan.bpc_items():
        for o, n in ip.bpcs_used:
            groups.setdefault((o.owner_name, place_label(o, location_names)), []).append(
                CollectRow(ip.product_type_id, o.me, o.te, n, o.runs))
    out = []
    for (owner, place), rows in groups.items():
        rows.sort(key=lambda r: (names.get(r.product_type_id, ""), -r.runs))
        out.append(CollectGroup(owner, place, rows))
    out.sort(key=lambda g: (-len(g.rows), g.owner, g.place))
    return out
