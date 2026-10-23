"""Pack and Collect presentation data (app/industry/copy_pack_view.py)."""
from app.industry import copy_pack as cp
from app.industry import copy_pack_view as view


def _bpc(item_id, runs, me=10, te=20, loc=None, label="Personal Hangar"):
    return cp.OwnedBlueprint(item_id, 1, False, me, te, runs, owner_name="Builder",
                             location_id=loc, location_label=label)


def _item(t, group=cp.GROUP_COPY, copies=0, rpc=0, me=10, te=20, used=()):
    return cp.ItemPlan(product_type_id=t, level=0, units=1, runs=1, group=group,
                       me=me, te=te, copy_runs=copies * rpc, bpcs_used=list(used),
                       copies=copies, runs_per_copy=rpc)


def _plan(items, tree_ids):
    tree = [cp.TreeRow(t, 0 if i == 0 else 1, True, True, 1, True, True)
            for i, t in enumerate(tree_ids)]
    return cp.Plan(items={i.product_type_id: i for i in items}, tree=tree,
                   level_seconds=[], build_seconds=0, mfg_slots=1, lab_slots=1,
                   copy_jobs=0, longest_copy=None)


# ── Pack ─────────────────────────────────────────────────────────────────────

def test_pack_lists_new_copies_then_existing_bpcs_in_build_order():
    hull = _item(1, copies=2, rpc=1, me=6, te=0)
    plates = _item(2, copies=3, rpc=10, me=10, te=16, used=[(_bpc(9, 40, 9, 14), 5)])
    pack = view.build_pack(_plan([plates, hull], [1, 2]))   # dict order is not tree order
    assert [e.product_type_id for e in pack.entries] == [1, 2]
    assert pack.entries[0].lines == [view.PackLine("copy", 2, 1, 6, 0)]
    assert pack.entries[1].lines == [view.PackLine("copy", 3, 10, 10, 16),
                                     view.PackLine("bpc", 1, 40, 9, 14, uses=5)]
    assert (pack.total, pack.new, pack.existing) == (6, 5, 1)


def test_pack_with_no_copies_or_no_existing_bpcs():
    covered = _item(1, copies=0, rpc=0, used=[(_bpc(9, 20), 12), (_bpc(10, 5), 5)])
    pack = view.build_pack(_plan([covered], [1]))
    assert pack.entries[0].lines == [view.PackLine("bpc", 1, 20, 10, 20, uses=12),
                                     view.PackLine("bpc", 1, 5, 10, 20, uses=5)]
    assert (pack.total, pack.new, pack.existing) == (2, 0, 2)
    fresh = view.build_pack(_plan([_item(1, copies=4, rpc=3)], [1]))
    assert (fresh.total, fresh.new, fresh.existing) == (4, 4, 0)


def test_pack_leaves_out_items_with_nothing_to_hand_over():
    elsewhere = _item(2, group=cp.GROUP_ELSEWHERE, copies=2, rpc=5)      # no BPO owned
    nocopy_none = _item(3, group=cp.GROUP_NOCOPY, copies=1, rpc=4)       # T2, nothing held
    nocopy_held = _item(4, group=cp.GROUP_NOCOPY, used=[(_bpc(9, 8), 8)])
    pack = view.build_pack(_plan([_item(1, copies=1, rpc=1), elsewhere, nocopy_none,
                                  nocopy_held], [1, 2, 3, 4]))
    assert [e.product_type_id for e in pack.entries] == [1, 4]
    assert pack.entries[1].lines == [view.PackLine("bpc", 1, 8, 10, 20, uses=8)]


def test_pack_follows_the_tree_and_lists_a_shared_item_once():
    items = [_item(2, copies=1, rpc=1), _item(3, copies=1, rpc=1), _item(1, copies=1, rpc=1)]
    pack = view.build_pack(_plan(items, [1, 3, 2, 3]))
    assert [e.product_type_id for e in pack.entries] == [1, 3, 2]


def test_empty_pack():
    pack = view.build_pack(_plan([_item(1, group=cp.GROUP_ELSEWHERE, copies=1, rpc=1)], [1]))
    assert pack.entries == [] and (pack.total, pack.new, pack.existing) == (0, 0, 0)


# ── places and Collect ───────────────────────────────────────────────────────

def test_place_label():
    o = _bpc(1, 5, loc=10, label="Corp Hangar 3")
    assert view.place_label(o, {10: "Azbel One"}) == "Azbel One, Corp Hangar 3"
    p = _bpc(1, 5, loc=10, label="Personal Hangar")
    assert view.place_label(p, {10: "Azbel One"}) == "Azbel One"
    assert view.place_label(p, {}) == "Personal Hangar"
    assert view.place_label(_bpc(1, 5, label=""), {}) == "Unknown location"


def test_collect_groups_by_place_biggest_first_then_by_name():
    names = {1: "Archon", 2: "Armor Plates", 3: "Jump Drive", 4: "Computer"}
    plan = _plan([
        _item(1, used=[(_bpc(1, 6, 9, 14, loc=111), 6)]),
        _item(2, used=[(_bpc(2, 40, loc=111), 5), (_bpc(3, 10, loc=None, label="Cargo"), 5)]),
        _item(3, used=[(_bpc(4, 8, loc=111), 4)]),
        _item(4, used=[(_bpc(5, 3, loc=222), 3)]),
    ], [1, 2, 3, 4])
    groups = view.group_bpcs(plan, {111: "Faurent I", 222: "Hadonoo III"}, names)
    assert [(g.place, len(g.rows)) for g in groups] == [
        ("Faurent I", 3), ("Cargo", 1), ("Hadonoo III", 1)]       # 1-row groups by name
    assert [names[r.product_type_id] for r in groups[0].rows] == [
        "Archon", "Armor Plates", "Jump Drive"]
    first = groups[0].rows[0]
    assert (first.me, first.te, first.uses, first.runs) == (9, 14, 6, 6)
    assert view.group_bpcs(_plan([_item(1)], [1]), {}, names) == []


# ── owner and ship in Collect ────────────────────────────────────────────────

def _held(item_id, runs, owner, label="Personal Hangar", loc=None, holder="", in_space=False):
    return cp.OwnedBlueprint(item_id, 1, False, 10, 20, runs, owner_name=owner,
                             location_id=loc, location_label=label, holder_label=holder,
                             holder_in_space=in_space)


def test_place_label_names_the_ship_and_where_it_is():
    cargo = _held(1, 5, "Builder", label="Cargo", loc=111, holder="Archon")
    assert view.place_label(cargo, {111: "Faurent I"}) == "Archon in Faurent I"
    # A ship whose place could not be read still says it is a ship.
    lost = _held(2, 5, "Builder", label="Cargo", holder="Archon")
    assert view.place_label(lost, {}) == "Archon (location unknown)"
    # With no holder resolved the flag label is unchanged.
    assert view.place_label(_held(3, 5, "Builder", label="Cargo"), {}) == "Cargo"


def test_collect_separates_owners_sharing_a_place():
    names = {1: "Archon", 2: "Armor Plates", 3: "Jump Drive"}
    plan = _plan([
        _item(1, used=[(_held(1, 6, "Builder", loc=111), 6)]),
        _item(2, used=[(_held(2, 40, "Alpha Corp", label="Corp Hangar 3", loc=111), 5)]),
        _item(3, used=[(_held(3, 8, "Builder", label="Cargo", loc=111, holder="Archon"), 4)]),
    ], [1, 2, 3])
    groups = view.group_bpcs(plan, {111: "Faurent I"}, names)
    assert sorted((g.owner, g.place, len(g.rows)) for g in groups) == [
        ("Alpha Corp", "Faurent I, Corp Hangar 3", 1),
        ("Builder", "Archon in Faurent I", 1),
        ("Builder", "Faurent I", 1),
    ]


def test_place_label_for_a_ship_in_space_names_its_system():
    space = _held(1, 5, "Builder", label="Cargo", loc=30000142, holder="Archon", in_space=True)
    assert view.place_label(space, {30000142: "Jita"}) == "Archon (in space, Jita)"
    assert view.place_label(space, {}) == "Archon (in space)"
