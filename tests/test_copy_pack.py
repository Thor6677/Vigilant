"""Copy-pack planning engine (app/industry/copy_pack.py).

Synthetic blueprints for the individual rules, plus an Archon fixture built
from the 2026-10-20 SDE (materials, run times, max runs per copy) whose
expected split was computed independently while the design was written:
components level 30.9h over 23 jobs against a 29.7h lower bound.
"""
import math
import random

from app.industry import copy_pack as cp

ALL_FIVE = {sid: 5 for sid, _label in cp.PLANNER_SKILLS}
AZBEL = cp.Facility(struct_mat=0.99, struct_time=0.80)


def bp(product, bp_id, mats=(), time=100, limit=None, copy=10, qty=1,
       skills=(), invented=False, invented_runs=None):
    return cp.BlueprintData(
        blueprint_type_id=bp_id, product_type_id=product, product_quantity=qty,
        manufacturing_time=time, max_production_limit=limit, copying_time=copy,
        materials=tuple(mats), skills=tuple(skills), invented=invented,
        invented_runs=invented_runs,
    )


def bpo(item_id, bp_id, me=10, te=20, reachable=True):
    return cp.OwnedBlueprint(item_id, bp_id, True, me, te, None,
                             owner_name="Main", reachable=reachable)


def bpc(item_id, bp_id, runs, me=10, te=20):
    return cp.OwnedBlueprint(item_id, bp_id, False, me, te, runs, owner_name="Main")


def inp(product, qty, blueprints, **kw):
    kw.setdefault("skills", ALL_FIVE)
    return cp.PlanInput(product, qty, {b.product_type_id: b for b in blueprints}, **kw)


# ── skills and formulas ──────────────────────────────────────────────────────

def test_slots_come_from_skills():
    assert cp.manufacturing_slots(ALL_FIVE) == 11
    assert cp.lab_slots(ALL_FIVE) == 11
    assert cp.manufacturing_slots({}) == 1
    assert cp.manufacturing_slots({cp.MASS_PRODUCTION: 4}) == 5


def test_copy_seconds_applies_science_adv_industry_and_structure():
    # 12,800s x 6 runs x 2 copies x 0.75 x 0.85 x 0.80
    assert cp.copy_seconds(12800, 6, 2, ALL_FIVE, 0.80) == 78336
    assert cp.copy_seconds(12800, 6, 2, {}, 1.0) == 153600


def test_missing_skills_only_checked_with_trained_levels():
    b = bp(1, 101, skills=[(22242, 3), (3380, 1)])
    assert cp.missing_skills(inp(1, 1, [b], trained={22242: 2, 3380: 5}), b) == [(22242, 3)]
    assert cp.missing_skills(inp(1, 1, [b], trained=None), b) == []


# ── tree ─────────────────────────────────────────────────────────────────────

def test_runs_use_product_quantity():
    # 358 units at 3 per run -> 120 runs (Life Support Backup Unit)
    part = bp(2, 202, qty=3)
    top = bp(1, 101, mats=[(2, 358)])
    p = cp.plan(inp(1, 1, [top, part], bpos=(bpo(1, 101, me=0), bpo(2, 202))))
    assert p.items[2].units == 358
    assert p.items[2].runs == 120


def test_default_build_follows_ownership_and_toggle_wins():
    top = bp(1, 101, mats=[(2, 1), (3, 1)])
    owned, unowned = bp(2, 202), bp(3, 303)
    base = dict(bpos=(bpo(1, 101), bpo(2, 202)))
    i = inp(1, 1, [top, owned, unowned], **base)
    assert cp.is_build(i, 1) and cp.is_build(i, 2) and not cp.is_build(i, 3)
    i = inp(1, 1, [top, owned, unowned], build={2: False, 3: True}, **base)
    assert not cp.is_build(i, 2) and cp.is_build(i, 3)


def test_product_is_always_build_even_when_toggled_off():
    top = bp(1, 101)
    assert cp.is_build(inp(1, 1, [top], build={1: False}), 1)


def test_item_without_blueprint_is_a_buy_leaf():
    top = bp(1, 101, mats=[(34, 100)])
    p = cp.plan(inp(1, 1, [top], bpos=(bpo(1, 101, me=0),), build={34: True}))
    assert 34 not in p.items
    leaf = next(r for r in p.tree if r.product_type_id == 34)
    assert not leaf.buildable and not leaf.build and leaf.units == 100


def test_shared_item_lands_at_deepest_level_with_summed_demand():
    # 1 -> 2 -> 3 and 1 -> 3: item 3 is level 2, demand from both parents.
    top = bp(1, 101, mats=[(2, 1), (3, 5)])
    mid = bp(2, 202, mats=[(3, 7)])
    leaf = bp(3, 303)
    owned = (bpo(1, 101, me=0), bpo(2, 202, me=0), bpo(3, 303, me=0))
    p = cp.plan(inp(1, 1, [top, mid, leaf], bpos=owned))
    assert p.items[3].level == 2
    assert p.items[3].units == 12
    # shown under both parents, expanded once
    assert [r.depth for r in p.tree if r.product_type_id == 3] == [2, 1]


def test_per_job_rounding_cascades_from_parent_copies():
    # Archon hull: max 1 run per copy, ME10, Azbel 0.99. Each 1-run job
    # needs ceil(6 x 0.9 x 0.99) = 6 plates, so 2 hulls need 12, not 11.
    hull = bp(1, 101, mats=[(2, 6)], limit=1)
    plates = bp(2, 202, limit=40)
    p = cp.plan(inp(1, 2, [hull, plates], bpos=(bpo(1, 101), bpo(2, 202)),
                    facility=AZBEL))
    assert p.items[1].copies == 2 and p.items[1].runs_per_copy == 1
    assert p.items[2].units == 12


# ── sources ──────────────────────────────────────────────────────────────────

def test_owned_bpcs_are_used_first_partially():
    top = bp(1, 101, mats=[(2, 8)])
    part = bp(2, 202)
    p = cp.plan(inp(1, 1, [top, part],
                    bpos=(bpo(1, 101, me=0), bpo(2, 202)),
                    bpcs=(bpc(9, 202, runs=10, me=8),)))
    ip = p.items[2]
    assert [(o.item_id, n) for o, n in ip.bpcs_used] == [(9, 8)]
    assert ip.copy_runs == 0 and ip.has_bpcs
    assert ip not in p.group(cp.GROUP_COPY)
    assert ip in p.bpc_items()


def test_ignoring_owned_bpcs_copies_everything():
    top = bp(1, 101, mats=[(2, 8)])
    part = bp(2, 202)
    p = cp.plan(inp(1, 1, [top, part],
                    bpos=(bpo(1, 101, me=0), bpo(2, 202)),
                    bpcs=(bpc(9, 202, runs=10),), ignore_bpcs=frozenset({2})))
    assert p.items[2].bpcs_used == []
    assert p.items[2].copy_runs == 8
    assert p.items[2].has_bpcs   # the switch still shows


def test_best_bpo_by_me_then_reachable_then_te():
    # A reachable ME10/TE10 beats an unreachable ME10/TE20: TE only changes
    # build time, an unreachable BPO has to be moved before copying.
    b = bp(1, 101)
    opts = (bpo(1, 101, me=8, te=20), bpo(2, 101, me=10, te=10),
            bpo(3, 101, me=10, te=20, reachable=False),
            bpo(4, 101, me=10, te=20, reachable=True))
    chosen, ranked = cp.choose_bpo(inp(1, 1, [b], bpos=opts), 1, b)
    assert chosen.item_id == 4
    assert [o.item_id for o in ranked] == [4, 2, 3, 1]


def test_bpo_override_respected_and_stale_override_ignored():
    b = bp(1, 101)
    opts = (bpo(1, 101, me=10), bpo(2, 101, me=5))
    assert cp.choose_bpo(inp(1, 1, [b], bpos=opts, bpo_choice={1: 2}), 1, b)[0].item_id == 2
    assert cp.choose_bpo(inp(1, 1, [b], bpos=opts, bpo_choice={1: 99}), 1, b)[0].item_id == 1


def test_no_bpo_goes_to_source_elsewhere_with_assumed_me():
    top = bp(1, 101, mats=[(2, 4)])
    part = bp(2, 202)
    p = cp.plan(inp(1, 1, [top, part], bpos=(bpo(1, 101),), build={2: True},
                    me_override={2: 7}, te_override={2: 14}))
    ip = p.items[2]
    assert ip.group == cp.GROUP_ELSEWHERE
    assert (ip.me, ip.te) == (7, 14)
    assert ip.copy_seconds is None


def test_invented_item_is_can_t_copy_with_one_job_per_bpc():
    # Each invented BPC carries `invented_runs` runs and is its own job.
    top = bp(1, 101, mats=[(2, 30)])
    t2 = bp(2, 202, copy=None, invented=True, limit=10, invented_runs=1)
    p = cp.plan(inp(1, 1, [top, t2], bpos=(bpo(1, 101, me=0),), build={2: True}))
    ip = p.items[2]
    assert ip.group == cp.GROUP_NOCOPY
    assert (ip.me, ip.te) == cp.INVENTED_ME_TE
    assert (ip.copies, ip.runs_per_copy) == (30, 1)
    assert [j.runs for j in ip.jobs] == [1] * 30


def test_invented_item_with_a_copy_time_but_no_bpo_is_sourced_not_copied():
    # Real T2 blueprints carry copy times (T2 BPOs exist), so "invented and
    # no BPO" must land in Can't copy, at invented ME/TE, one job per BPC.
    top = bp(1, 101, mats=[(2, 3)])
    t2 = bp(2, 202, copy=96000, invented=True, invented_runs=1, limit=10)
    p = cp.plan(inp(1, 1, [top, t2], bpos=(bpo(1, 101, me=0),), build={2: True}))
    ip = p.items[2]
    assert ip.group == cp.GROUP_NOCOPY
    assert (ip.me, ip.te) == cp.INVENTED_ME_TE
    assert [j.runs for j in ip.jobs] == [1, 1, 1]


def test_invented_item_with_an_owned_bpo_is_copied():
    top = bp(1, 101, mats=[(2, 3)])
    t2 = bp(2, 202, copy=96000, invented=True, invented_runs=1, limit=10)
    p = cp.plan(inp(1, 1, [top, t2], bpos=(bpo(1, 101, me=0), bpo(2, 202, me=8, te=16))))
    ip = p.items[2]
    assert ip.group == cp.GROUP_COPY and (ip.me, ip.te) == (8, 16)


def test_uncopyable_item_jobs_capped_at_max_runs_per_copy():
    top = bp(1, 101, mats=[(2, 25)])
    faction = bp(2, 202, copy=None, limit=10)
    p = cp.plan(inp(1, 1, [top, faction], bpos=(bpo(1, 101, me=0),), build={2: True}))
    ip = p.items[2]
    assert (ip.copies, ip.runs_per_copy, ip.spare_runs) == (3, 10, 5)
    assert [j.runs for j in ip.jobs] == [10, 10, 5]


def test_uncopyable_item_with_an_owned_bpo_is_one_job():
    # Nothing to source, and a BPO can only be in one job at a time.
    top = bp(1, 101, mats=[(2, 25)])
    faction = bp(2, 202, copy=None, limit=10)
    p = cp.plan(inp(1, 1, [top, faction],
                    bpos=(bpo(1, 101, me=0), bpo(2, 202, me=5, te=10)), build={2: True}))
    ip = p.items[2]
    assert ip.group == cp.GROUP_NOCOPY and (ip.me, ip.te) == (5, 10)
    assert [j.runs for j in ip.jobs] == [25]
    assert (ip.copies, ip.spare_runs) == (1, 0)


def test_uncopyable_parent_rounds_children_per_bpc():
    # A T2 hull from ten 1-run invented BPCs at ME2 in an Azbel: each job
    # needs ceil(6 x 0.98 x 0.99) = 6 parts, 60 in all. One 10-run job
    # would need 59 and leave the copy pack a run short.
    hull = bp(1, 101, mats=[(2, 6)], copy=None, invented=True, invented_runs=1, limit=10)
    part = bp(2, 202, limit=40)
    p = cp.plan(inp(1, 10, [hull, part], bpos=(bpo(2, 202),), facility=AZBEL))
    assert len(p.items[1].jobs) == 10
    assert p.items[2].units == 60


def test_me_override_alone_keeps_the_default_te():
    top = bp(1, 101, mats=[(2, 30)])
    t2 = bp(2, 202, copy=None, invented=True)
    p = cp.plan(inp(1, 1, [top, t2], bpos=(bpo(1, 101, me=0),), build={2: True},
                    me_override={2: 4}))
    assert (p.items[2].me, p.items[2].te) == (4, 4)


def test_blueprint_from_row():
    row = {"blueprint_type_id": 101, "product_type_id": 1, "product_quantity": 3,
           "manufacturing_time": 600, "max_production_limit": 40,
           "copying_time": 480, "invented": False,
           "materials": [(34, 10)], "skills": [(3380, 1)]}
    b = cp.blueprint_from_row(row)
    assert b.materials == ((34, 10),) and b.skills == ((3380, 1),)
    assert b.copyable and b.product_quantity == 3


# ── splitting ────────────────────────────────────────────────────────────────

def test_schedule_is_longest_first_onto_earliest_free_slot():
    assert sorted(cp.schedule([5, 4, 3, 3], 2)) == [7, 8]


def test_split_respects_max_runs_per_copy():
    s = cp.Splittable(key=1, runs=100, run_seconds=1, max_per_copy=40)
    assert s.min_copies == 3
    cp.split_level([s], [], slots=1)
    # One slot, so splitting can't run anything in parallel, but 4 x 25
    # wastes no runs where the minimum 3 x 34 wastes 2.
    assert (s.copies, s.per_copy) == (4, 25)


def test_split_fills_slots():
    # One copy of 12 runs would take 1200s; four copies of 3 use 4 slots.
    s = cp.Splittable(key=1, runs=12, run_seconds=100, max_per_copy=None)
    finish = cp.split_level([s], [], slots=4)
    assert s.copies == 4 and s.per_copy == 3 and finish == 300


def test_tied_longest_items_both_split():
    # Two items tie for the longest job. Splitting either one alone leaves
    # the finish time at 1000, so an M-only rule stalls; the tie-aware
    # score splits both.
    a = cp.Splittable(key=1, runs=10, run_seconds=100, max_per_copy=None)
    b = cp.Splittable(key=2, runs=10, run_seconds=100, max_per_copy=None)
    finish = cp.split_level([a, b], [], slots=4)
    assert a.copies == 2 and b.copies == 2 and finish == 500


def test_prune_removes_copies_that_do_not_help():
    # A fixed 1000s job sets the finish; a short item never needs splitting.
    s = cp.Splittable(key=1, runs=4, run_seconds=100, max_per_copy=None)
    finish = cp.split_level([s], [1000], slots=3)
    assert s.copies == 1 and finish == 1000


def test_spare_runs_reported_for_equal_copies():
    # 22 runs over 3 slots: three copies of 8, two runs spare.
    hull = bp(1, 101, mats=[(2, 22)], limit=1)
    bay = bp(2, 202, time=16000, limit=40)
    p = cp.plan(inp(1, 1, [hull, bay], bpos=(bpo(1, 101, me=0), bpo(2, 202)),
                    skills={**ALL_FIVE, cp.MASS_PRODUCTION: 0,
                            cp.ADV_MASS_PRODUCTION: 2}))
    ip = p.items[2]
    assert (ip.copies, ip.runs_per_copy, ip.spare_runs) == (3, 8, 2)


def test_owned_bpc_jobs_are_fixed_and_count_toward_the_level():
    # A 10-run owned BPC is one unsplit job; the copied remainder is split
    # around it and the level can't finish before the BPC job does.
    top = bp(1, 101, mats=[(2, 30)])
    part = bp(2, 202, time=100)
    p = cp.plan(inp(1, 1, [top, part], bpos=(bpo(1, 101, me=0), bpo(2, 202)),
                    bpcs=(bpc(9, 202, runs=10),)))
    ip = p.items[2]
    bpc_jobs = [j for j in ip.jobs if j.bpc is not None]
    assert [j.runs for j in bpc_jobs] == [10]
    assert ip.copy_runs == 20
    assert dict(p.level_seconds)[1] >= bpc_jobs[0].seconds


def test_split_invariants_hold_on_random_levels():
    rng = random.Random(20261021)
    for _ in range(300):
        items = [cp.Splittable(key=i, runs=rng.randint(1, 400),
                               run_seconds=rng.randint(1, 5000),
                               max_per_copy=rng.choice([None, 1, 5, 40, 200]))
                 for i in range(rng.randint(1, 8))]
        fixed = [rng.randint(1, 50000) for _ in range(rng.randint(0, 3))]
        slots = rng.randint(1, 11)
        for s in items:
            s.copies = s.min_copies
        start = max(cp.schedule(fixed + [d for s in items for d in s.durations()], slots))
        finish = cp.split_level(items, fixed, slots)
        assert finish <= start
        for s in items:
            assert s.copies <= s.runs
            assert s.max_per_copy is None or s.per_copy <= s.max_per_copy
            assert math.ceil(s.runs / s.per_copy) == s.copies   # no pure-spare copy


def test_manufacturing_cycle_terminates_with_product_at_level_zero():
    a = bp(1, 101, mats=[(2, 1)])
    b = bp(2, 202, mats=[(1, 1)])
    p = cp.plan(inp(1, 1, [a, b], bpos=(bpo(1, 101), bpo(2, 202))))
    assert p.items[1].level == 0 and p.items[2].level == 1
    assert p.items[2].units == 1


# ── Archon fixture (2026-10-20 SDE) ──────────────────────────────────────────

ARCHON = 23757
ARCHON_MATERIALS = (
    (57474, 1), (57487, 1), (57488, 1), (21019, 3), (21021, 3), (21023, 3),
    (21035, 3), (24560, 3), (21009, 4), (21013, 4), (21025, 4), (21037, 4),
    (21017, 6), (24558, 6), (21029, 12), (57470, 12), (57486, 200), (57478, 400),
)
# product: (blueprint, product qty, manufacturing time, max runs per copy, copy time)
ARCHON_COMPONENTS = {
    57474: (57511, 1, 16000, 40, 12800),   # Radar-FTL Interlink Communicator
    57487: (57524, 1, 16000, 40, 12800),   # Capital Core Temperature Regulator
    57488: (57525, 1, 16000, 20, 12800),   # Neurolink Protection Cell
    21019: (21020, 1, 16000, 40, 12800),   # Capital Capacitor Battery
    21021: (21022, 1, 16000, 40, 12800),   # Capital Power Generator
    21023: (21024, 1, 16000, 40, 12800),   # Capital Shield Emitter
    21035: (21036, 1, 16000, 40, 12800),   # Capital Computer System
    24560: (24561, 1, 16000, 40, 12800),   # Capital Corporate Hangar Bay
    21009: (21010, 1, 16000, 40, 12800),   # Capital Propulsion Engine
    21013: (21014, 1, 16000, 40, 12800),   # Capital Sensor Cluster
    21025: (21026, 1, 16000, 40, 12800),   # Capital Jump Drive
    21037: (21038, 1, 16000, 40, 12800),   # Capital Construction Parts
    21017: (21018, 1, 16000, 40, 12800),   # Capital Armor Plates
    24558: (24559, 1, 16000, 40, 12800),   # Capital Ship Maintenance Bay
    21029: (21030, 1, 16000, 40, 12800),   # Capital Drone Bay
    57470: (57507, 1, 10800, 200, 1440),   # U-C Trigger Neurolink Conduit
    57486: (57523, 3, 1800, 200, 1440),    # Life Support Backup Unit
    57478: (57515, 3, 1800, 200, 1440),    # Auto-Integrity Preservation Seal
}


def _archon_plan(quantity):
    blueprints = [bp(ARCHON, 23758, mats=ARCHON_MATERIALS, time=1800000,
                     limit=1, copy=1600000, skills=[(22242, 3)])]
    blueprints += [bp(t, b, time=tm, limit=lim, copy=ct, qty=q)
                   for t, (b, q, tm, lim, ct) in ARCHON_COMPONENTS.items()]
    owned = tuple(bpo(1000 + i, b.blueprint_type_id) for i, b in enumerate(blueprints))
    return cp.plan(inp(ARCHON, quantity, blueprints, bpos=owned, facility=AZBEL))


def test_archon_x2_matches_the_design_simulation():
    p = _archon_plan(2)
    components = dict(p.level_seconds)[1]
    assert round(components / 3600, 1) == 30.9
    assert sum(len(ip.jobs) for ip in p.items.values() if ip.level == 1) == 23
    split = {t: (ip.copies, ip.runs_per_copy, ip.spare_runs)
             for t, ip in p.items.items() if ip.copies > 1 and t != ARCHON}
    assert split == {21009: (2, 4, 0), 21029: (3, 8, 2), 57478: (3, 80, 2)}
    assert p.items[21017].units == 12          # per-job rounding, not 11
    assert p.items[57486].runs == 120          # 358 units at 3 per run
    assert (p.items[ARCHON].copies, p.items[ARCHON].runs_per_copy) == (2, 1)
    assert p.longest_copy == (ARCHON, 1632000)  # 18d 21h 20m


def test_archon_x1_beats_the_m_only_stall():
    p = _archon_plan(1)
    components = dict(p.level_seconds)[1]
    assert round(components / 3600, 1) == 15.5
