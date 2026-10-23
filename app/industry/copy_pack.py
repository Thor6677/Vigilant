"""Blueprint copy packs — which copies to make, and how big (/industry/copy-packs).

Given a product, a quantity and the one character who will both copy and
build, work out the blueprint copies to make from the BPOs you own: how many
copies of each, how many runs per copy, and which BPO they come from. Copies
are split so the character's manufacturing slots build the tree in parallel.

No DB or ESI access: the route gathers SDE rows, owned blueprints and skills
and passes them in, so every rule here is unit-testable. (`split_level`
reports its choice by setting `.copies` on the Splittables it is handed.)

Quantities follow the real jobs: each copy (and each owned BPC used) is one
manufacturing job, and a job rounds its materials up on its own, exactly as
the game does. The parent's copies therefore decide how many children are
needed — two 1-run Archon jobs need 12 Capital Armor Plates where one 2-run
job would need 11.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.industry.manufacturing import calc_material, calc_time

MAX_DEPTH = 10
REFINE_LIMIT = 500

MASS_PRODUCTION = 3387
ADV_MASS_PRODUCTION = 24625
INDUSTRY = 3380
ADV_INDUSTRY = 3388
SCIENCE = 3402
LAB_OPERATION = 3406
ADV_LAB_OPERATION = 24624

# (skill_id, label) in the order the Skills panel shows them.
PLANNER_SKILLS = (
    (MASS_PRODUCTION, "Mass Production"),
    (ADV_MASS_PRODUCTION, "Advanced Mass Production"),
    (INDUSTRY, "Industry"),
    (ADV_INDUSTRY, "Advanced Industry"),
    (SCIENCE, "Science"),
    (LAB_OPERATION, "Laboratory Operation"),
    (ADV_LAB_OPERATION, "Advanced Laboratory Operation"),
)

DEFAULT_ME_TE = (10, 20)
INVENTED_ME_TE = (2, 4)    # an invented T2 BPC with no decryptor

GROUP_COPY = "copy"            # copy it from an owned BPO
GROUP_ELSEWHERE = "elsewhere"  # copyable T1-style blueprint, but no BPO is owned
GROUP_NOCOPY = "nocopy"        # no copying activity, or invented with no BPO


@dataclass(frozen=True)
class BlueprintData:
    """One manufacturing blueprint, keyed in PlanInput.blueprints by the
    product it makes. `materials` are (type_id, quantity per run)."""
    blueprint_type_id: int
    product_type_id: int
    product_quantity: int
    manufacturing_time: int
    max_production_limit: int | None
    copying_time: int | None
    materials: tuple[tuple[int, int], ...] = ()
    skills: tuple[tuple[int, int], ...] = ()   # (skill_id, level) needed to build
    invented: bool = False      # this blueprint is an invention product
    invented_runs: int | None = None   # runs on an invented BPC (no decryptor)

    @property
    def copyable(self) -> bool:
        return self.copying_time is not None


def blueprint_from_row(row: dict) -> BlueprintData:
    """A `sde.get_blueprint_tree` row as a BlueprintData."""
    return BlueprintData(
        blueprint_type_id=row["blueprint_type_id"],
        product_type_id=row["product_type_id"],
        product_quantity=row["product_quantity"],
        manufacturing_time=row["manufacturing_time"],
        max_production_limit=row["max_production_limit"],
        copying_time=row["copying_time"],
        materials=tuple(tuple(m) for m in row["materials"]),
        skills=tuple(tuple(s) for s in row["skills"]),
        invented=row["invented"],
        invented_runs=row.get("invented_runs"),
    )


@dataclass(frozen=True)
class OwnedBlueprint:
    item_id: int
    blueprint_type_id: int
    is_bpo: bool
    me: int
    te: int
    runs: int | None            # remaining runs on a BPC; None for a BPO
    owner_name: str = ""
    location_id: int | None = None
    location_label: str = ""
    reachable: bool = True      # the selected character can copy from it
    # Set when the blueprint sits inside a ship or container: the item's own
    # id (ESI's location_id is not a place then) and, once the owner's assets
    # have been read, what it is and the place it is in (location_id above).
    holder_id: int | None = None
    holder_type_id: int | None = None
    holder_label: str = ""
    holder_in_space: bool = False   # the ship is out in space; location_id is its system


@dataclass(frozen=True)
class Facility:
    struct_mat: float = 1.0
    struct_time: float = 1.0
    rig_mat: float = 0.0
    rig_time: float = 0.0
    sec_mult: float = 1.0


@dataclass(frozen=True)
class PlanInput:
    product_type_id: int
    quantity: int
    blueprints: dict[int, BlueprintData]  # keyed by product_type_id
    bpos: tuple[OwnedBlueprint, ...] = ()  # every source
    bpcs: tuple[OwnedBlueprint, ...] = ()  # the ones the character can use
    build: dict = field(default_factory=dict)        # product_type_id -> bool
    bpo_choice: dict = field(default_factory=dict)   # product_type_id -> item_id
    me_override: dict = field(default_factory=dict)  # product_type_id -> ME, no-BPO items
    te_override: dict = field(default_factory=dict)  # product_type_id -> TE, no-BPO items
    ignore_bpcs: frozenset = frozenset()
    skills: dict = field(default_factory=dict)       # planner skill levels
    trained: dict | None = None           # every trained level; None skips the check
    facility: Facility = Facility()


@dataclass
class Job:
    runs: int
    me: int
    te: int
    seconds: int
    bpc: OwnedBlueprint | None = None     # set when an owned BPC runs this job


@dataclass
class ItemPlan:
    product_type_id: int
    level: int
    units: int
    runs: int
    group: str
    me: int
    te: int
    copy_runs: int = 0                    # runs left after owned BPCs
    has_bpcs: bool = False                # the character holds BPCs of it
    bpo: OwnedBlueprint | None = None
    bpo_options: list = field(default_factory=list)
    bpcs_used: list = field(default_factory=list)    # (OwnedBlueprint, runs used)
    copies: int = 0                       # copies to make; BPCs to source if uncopyable
    runs_per_copy: int = 0
    spare_runs: int = 0
    copy_seconds: int | None = None
    missing_skills: list = field(default_factory=list)   # (skill_id, level)
    jobs: list = field(default_factory=list)


@dataclass
class TreeRow:
    product_type_id: int
    depth: int
    build: bool
    buildable: bool
    units: int
    owned: bool          # a BPO or usable BPC exists
    expanded: bool       # its children follow (first appearance of a Build item)
    parent: int | None = None   # index in Plan.tree of the row it is listed under


@dataclass
class Plan:
    items: dict                           # product_type_id -> ItemPlan
    tree: list                            # TreeRow, display order
    level_seconds: list                   # (level, seconds), deepest first
    build_seconds: int
    mfg_slots: int
    lab_slots: int
    copy_jobs: int
    longest_copy: tuple | None            # (product_type_id, seconds)

    def group(self, name: str) -> list:
        return [ip for ip in self.items.values()
                if ip.group == name and ip.copy_runs > 0]

    def bpc_items(self) -> list:
        return [ip for ip in self.items.values() if ip.bpcs_used]


# ── Skills ────────────────────────────────────────────────────────────────────

def manufacturing_slots(skills: dict) -> int:
    return 1 + skills.get(MASS_PRODUCTION, 0) + skills.get(ADV_MASS_PRODUCTION, 0)


def lab_slots(skills: dict) -> int:
    return 1 + skills.get(LAB_OPERATION, 0) + skills.get(ADV_LAB_OPERATION, 0)


def copy_seconds(copying_time: int, runs_per_copy: int, copies: int,
                 skills: dict, struct_time: float) -> int:
    """One copy job making `copies` copies of `runs_per_copy` runs. Science
    is -5% per level, Advanced Industry -3% per level, and the structure's
    time bonus applies to science jobs too. Lab rigs are not modelled."""
    t = copying_time * runs_per_copy * copies
    t *= 1 - 0.05 * skills.get(SCIENCE, 0)
    t *= 1 - 0.03 * skills.get(ADV_INDUSTRY, 0)
    t *= struct_time
    return max(1, round(t))


def run_seconds(inp: PlanInput, bp: BlueprintData, te: int) -> int:
    f = inp.facility
    return calc_time(bp.manufacturing_time, te, f.struct_time, f.rig_time,
                     f.sec_mult, industry=inp.skills.get(INDUSTRY, 0),
                     adv_industry=inp.skills.get(ADV_INDUSTRY, 0))


def missing_skills(inp: PlanInput, bp: BlueprintData) -> list:
    if inp.trained is None:
        return []
    return [(sid, lvl) for sid, lvl in bp.skills if inp.trained.get(sid, 0) < lvl]


# ── Tree ──────────────────────────────────────────────────────────────────────

def owns_blueprint(inp: PlanInput, product_type_id: int) -> bool:
    bp = inp.blueprints.get(product_type_id)
    if bp is None:
        return False
    return any(o.blueprint_type_id == bp.blueprint_type_id
               for o in (*inp.bpos, *inp.bpcs))


def is_build(inp: PlanInput, product_type_id: int) -> bool:
    """The product is always built. Below it, the user's toggle wins;
    untouched items default to Build when a blueprint for them is owned."""
    if product_type_id not in inp.blueprints:
        return False
    if product_type_id == inp.product_type_id:
        return True
    if product_type_id in inp.build:
        return bool(inp.build[product_type_id])
    return owns_blueprint(inp, product_type_id)


def item_levels(inp: PlanInput) -> dict:
    """{product_type_id: level} for everything under Build items. An item's
    level is the DEEPEST depth it appears at, so every parent sits at a
    lower level. Dict order is first-visit order, which keeps the per-level
    processing (and its tie-breaks) in tree order."""
    levels: dict[int, int] = {}

    def visit(t: int, depth: int, path: frozenset) -> None:
        # `path` stops a manufacturing cycle from re-levelling an ancestor.
        if depth > MAX_DEPTH or t in path or levels.get(t, -1) >= depth:
            return
        levels[t] = depth
        if is_build(inp, t):
            for m, _q in inp.blueprints[t].materials:
                visit(m, depth + 1, path | {t})

    visit(inp.product_type_id, 0, frozenset())
    return levels


def tree_rows(inp: PlanInput, demand: dict) -> list:
    """Display order: depth-first under Build items. A shared item shows
    under every parent, but its children only under the first one."""
    rows: list[TreeRow] = []
    expanded: set[int] = set()

    def walk(t: int, depth: int, parent: int | None) -> None:
        build = is_build(inp, t)
        expand = build and t not in expanded and depth < MAX_DEPTH
        idx = len(rows)
        rows.append(TreeRow(t, depth, build, t in inp.blueprints,
                            demand.get(t, 0), owns_blueprint(inp, t), expand, parent))
        if expand:
            expanded.add(t)
            for m, _q in inp.blueprints[t].materials:
                walk(m, depth + 1, idx)

    walk(inp.product_type_id, 0, None)
    return rows


# ── Sources ───────────────────────────────────────────────────────────────────

def bpo_options(inp: PlanInput, bp: BlueprintData) -> list:
    """Owned BPOs of this blueprint, best first: ME, then one the character
    can reach, then TE. Reachability beats TE because TE only changes build
    time, while an unreachable BPO has to be moved before it can be copied."""
    opts = [o for o in inp.bpos
            if o.is_bpo and o.blueprint_type_id == bp.blueprint_type_id]
    return sorted(opts, key=lambda o: (-o.me, not o.reachable, -o.te, o.item_id))


def choose_bpo(inp: PlanInput, product_type_id: int, bp: BlueprintData):
    """(chosen BPO or None, all options). An override naming a BPO that
    isn't owned any more falls back to the best one."""
    opts = bpo_options(inp, bp)
    wanted = inp.bpo_choice.get(product_type_id)
    for o in opts:
        if o.item_id == wanted:
            return o, opts
    return (opts[0] if opts else None), opts


def usable_bpcs(inp: PlanInput, product_type_id: int, bp: BlueprintData) -> list:
    if product_type_id in inp.ignore_bpcs:
        return []
    found = [o for o in inp.bpcs
             if not o.is_bpo and o.blueprint_type_id == bp.blueprint_type_id
             and (o.runs or 0) > 0]
    return sorted(found, key=lambda o: (-o.runs, -o.me, o.item_id))


def assumed_me_te(inp: PlanInput, product_type_id: int, bp: BlueprintData) -> tuple:
    """ME/TE for runs with no owned blueprint behind them. Each field
    falls back on its own, so overriding only ME keeps an invented item's
    TE 4."""
    me, te = INVENTED_ME_TE if bp.invented else DEFAULT_ME_TE
    return (inp.me_override.get(product_type_id, me),
            inp.te_override.get(product_type_id, te))


def bpc_run_cap(bp: BlueprintData) -> int | None:
    """Most runs one BPC of an uncopyable blueprint carries: an invented
    BPC's runs, else the blueprint's max runs per copy (None = no cap)."""
    if bp.invented and bp.invented_runs:
        return bp.invented_runs
    return bp.max_production_limit or None


def cover_item(inp: PlanInput, product_type_id: int, level: int, units: int) -> ItemPlan:
    """Owned BPCs first, then decide where the remaining runs come from."""
    bp = inp.blueprints[product_type_id]
    runs = math.ceil(units / max(1, bp.product_quantity)) if units > 0 else 0
    bpo, opts = choose_bpo(inp, product_type_id, bp)

    held = [o for o in inp.bpcs
            if not o.is_bpo and o.blueprint_type_id == bp.blueprint_type_id]
    used, remaining = [], runs
    for o in usable_bpcs(inp, product_type_id, bp):
        if remaining <= 0:
            break
        n = min(o.runs, remaining)
        used.append((o, n))
        remaining -= n

    if bp.copyable and bpo is not None:
        group, me, te = GROUP_COPY, bpo.me, bpo.te
    elif bp.copyable and not bp.invented:
        group = GROUP_ELSEWHERE
        me, te = assumed_me_te(inp, product_type_id, bp)
    else:
        # No copying activity, or an invented (T2) blueprint with no BPO to
        # copy from (the SDE gives T2 blueprints copy times because T2 BPOs
        # exist): its BPCs come from invention or the market.
        group = GROUP_NOCOPY
        me, te = (bpo.me, bpo.te) if bpo else assumed_me_te(inp, product_type_id, bp)

    return ItemPlan(
        product_type_id=product_type_id, level=level, units=units, runs=runs,
        group=group, me=me, te=te, copy_runs=remaining, bpo=bpo,
        bpo_options=opts, bpcs_used=used, has_bpcs=bool(held),
        missing_skills=missing_skills(inp, bp),
    )


# ── Splitting ─────────────────────────────────────────────────────────────────

def schedule(durations: list, slots: int) -> list:
    """Longest job first, each onto the slot that frees up earliest.
    Returns every slot's end time."""
    ends = [0] * max(1, slots)
    for d in sorted(durations, reverse=True):
        i = ends.index(min(ends))
        ends[i] += d
    return ends


def _score(ends: list) -> tuple:
    finish = max(ends)
    return finish, ends.count(finish)


@dataclass
class Splittable:
    key: int
    runs: int
    run_seconds: int
    max_per_copy: int | None
    copies: int = 1

    @property
    def per_copy(self) -> int:
        return math.ceil(self.runs / self.copies)

    @property
    def min_copies(self) -> int:
        if not self.max_per_copy:
            return 1
        return max(1, math.ceil(self.runs / self.max_per_copy))

    def durations(self) -> list:
        return [self.per_copy * self.run_seconds] * self.copies


def _normalised(runs: int, copies: int) -> int:
    """The fewest copies giving the same runs per copy, so no copy is pure
    spare (5 runs as 4 copies of 2 is really 3 copies)."""
    return math.ceil(runs / math.ceil(runs / copies))


def split_level(splittable: list, fixed: list, slots: int) -> int:
    """Choose copies per item so the level finishes soonest; sets each
    item's `.copies` and returns the level's finish time in seconds.

    Start from the fewest copies. Then repeatedly give one more copy to an
    item — longest job first — keeping the change only if the score
    (finish time, slots ending at it) drops. The second term stops ties
    stalling the search: with two equal longest jobs, splitting either alone
    leaves the finish time unchanged. Finally prune copies that were only
    there to break a tie and don't lower the finish time. `fixed` holds the
    durations of jobs that can't be split (owned BPCs, uncopyable items)."""
    for s in splittable:
        s.copies = s.min_copies

    def ends() -> list:
        return schedule(fixed + [d for s in splittable for d in s.durations()], slots)

    best = _score(ends())
    accepted = 0
    while accepted < REFINE_LIMIT:
        improved = False
        for s in sorted(splittable, key=lambda s: -s.per_copy * s.run_seconds):
            if s.per_copy <= 1:
                continue
            before = s.copies
            s.copies = math.ceil(s.runs / (s.per_copy - 1))
            score = _score(ends())
            if score < best:
                best, accepted, improved = score, accepted + 1, True
                break
            s.copies = before
        if not improved:
            break

    changed = True
    while changed:
        changed = False
        for s in sorted(splittable, key=lambda s: s.per_copy * s.run_seconds):
            if s.copies <= s.min_copies:
                continue
            before = s.copies
            s.copies = _normalised(s.runs, s.copies - 1)
            e = ends()
            if max(e) <= best[0]:
                best, changed = _score(e), True
            else:
                s.copies = before
    return max(ends())


# ── Plan ──────────────────────────────────────────────────────────────────────

def plan(inp: PlanInput) -> Plan:
    f = inp.facility
    slots = manufacturing_slots(inp.skills)
    levels = item_levels(inp)
    demand: dict[int, int] = {inp.product_type_id: inp.quantity}
    items: dict[int, ItemPlan] = {}
    level_seconds: list[tuple[int, int]] = []

    by_level: dict[int, list[int]] = {}
    for t, lvl in levels.items():
        if is_build(inp, t):
            by_level.setdefault(lvl, []).append(t)

    for lvl in sorted(by_level):
        splittable, fixed = [], []
        for t in by_level[lvl]:
            bp = inp.blueprints[t]
            ip = cover_item(inp, t, lvl, demand.get(t, 0))
            items[t] = ip
            for o, n in ip.bpcs_used:
                secs = n * run_seconds(inp, bp, o.te)
                ip.jobs.append(Job(n, o.me, o.te, secs, bpc=o))
                fixed.append(secs)
            if ip.copy_runs <= 0:
                continue
            per_run = run_seconds(inp, bp, ip.te)
            if ip.group == GROUP_NOCOPY:
                # Each sourced BPC is its own job, capped at its runs, so
                # its materials round per job like everything else. An owned
                # BPO needs nothing sourced, and a blueprint can only be in
                # one job at a time, so it builds everything in one job.
                cap = ip.copy_runs if ip.bpo else (bpc_run_cap(bp) or ip.copy_runs)
                ip.copies = math.ceil(ip.copy_runs / cap)
                ip.runs_per_copy = min(cap, ip.copy_runs)
                ip.spare_runs = ip.copies * ip.runs_per_copy - ip.copy_runs
                left = ip.copy_runs
                while left > 0:
                    n = min(cap, left)
                    ip.jobs.append(Job(n, ip.me, ip.te, n * per_run))
                    fixed.append(n * per_run)
                    left -= n
            else:
                splittable.append(Splittable(t, ip.copy_runs, per_run,
                                             bp.max_production_limit))

        level_seconds.append((lvl, split_level(splittable, fixed, slots)))

        for s in splittable:
            ip = items[s.key]
            bp = inp.blueprints[s.key]
            ip.copies = s.copies
            ip.runs_per_copy = s.per_copy
            ip.spare_runs = s.per_copy * s.copies - s.runs
            ip.jobs.extend(Job(s.per_copy, ip.me, ip.te, s.per_copy * s.run_seconds)
                           for _ in range(s.copies))
            if ip.group == GROUP_COPY:
                ip.copy_seconds = copy_seconds(bp.copying_time, s.per_copy,
                                               s.copies, inp.skills, f.struct_time)

        for t in by_level[lvl]:
            bp = inp.blueprints[t]
            for job in items[t].jobs:
                for m, q in bp.materials:
                    if m in items:
                        # Only past the depth cut-off or on a manufacturing
                        # cycle: otherwise an item is planned after all of
                        # its parents, never before.
                        continue
                    demand[m] = demand.get(m, 0) + calc_material(
                        q, job.runs, job.me, f.struct_mat, f.rig_mat, f.sec_mult)

    copies = [ip for ip in items.values()
              if ip.group == GROUP_COPY and ip.copy_seconds is not None]
    longest = max(copies, key=lambda ip: ip.copy_seconds, default=None)
    level_seconds.sort(key=lambda x: -x[0])
    return Plan(
        items=items,
        tree=tree_rows(inp, demand),
        level_seconds=level_seconds,
        build_seconds=sum(secs for _lvl, secs in level_seconds),
        mfg_slots=slots,
        lab_slots=lab_slots(inp.skills),
        copy_jobs=len(copies),
        longest_copy=(longest.product_type_id, longest.copy_seconds) if longest else None,
    )
