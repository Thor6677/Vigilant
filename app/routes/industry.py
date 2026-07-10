"""Manufacturing calculator — nested build/buy with ME/TE, structure, rig, and security modifiers.

The pure cost/time modifier math (`_calc_material`, `_calc_time`, `_format_time`
and the `STRUCTURES`/`RIGS`/`SEC_STATUS` tables) lives in
`app/industry/manufacturing.py` so the build-profitability finder can reuse the
exact same engine without a circular import — see that module. They are
re-imported here under their original names, so every endpoint below is
unchanged.
"""

import json
import time

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import get_db, Character
from app.db.sde_models import SDEBlueprintInvention
import asyncio
from app.sde import lookup as sde
from app.industry.compression import (
    MINERALS, MINERAL_IDS, MINERAL_NAMES, ORE_GROUP_SKILL,
    SKILL_REPROCESSING, SKILL_REPROCESSING_EFFICIENCY,
    STRUCTURES as REPRO_STRUCTURES, RIGS as REPRO_RIGS,
    SECURITY as REPRO_SECURITY, IMPLANTS as REPRO_IMPLANTS,
    TRADE_HUBS, compute_yield, solve_compression,
)
from app.industry.manufacturing import (
    STRUCTURES, RIGS, SEC_STATUS,
    _calc_material, _calc_time, _format_time,
)
from app.industry import build_finder
from app.industry.invention import (
    DECRYPTORS, attempt_cost, invented_bpc, invention_overhead_per_unit,
    invention_probability,
)
from app.routes.fitting import _character_skills_map
from app.market import lp as market_lp
from app.esi.client import ESIClient
from app.esi import market as esi_market

router = APIRouter(tags=["industry"])
templates = Jinja2Templates(directory="app/templates")


async def _get_price_map(db: AsyncSession, type_ids: set[int]) -> dict[int, float]:
    """Fetch global average prices for a set of type IDs."""
    price_map: dict[int, float] = {}
    try:
        client = ESIClient("", db=db)
        all_prices = await esi_market.get_market_prices(client)
        for p in all_prices:
            tid = p.get("type_id")
            if tid in type_ids:
                price_map[tid] = p.get("average_price") or p.get("adjusted_price") or 0
    except Exception:
        pass
    return price_map



@router.get("/industry/manufacturing", response_class=HTMLResponse)
async def industry_manufacturing_page(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")
    return templates.TemplateResponse(request, "industry.html", {"structures": STRUCTURES,
        "rigs": RIGS,
        "sec_statuses": SEC_STATUS})


@router.get("/industry/search", response_class=HTMLResponse)
async def industry_search(request: Request, q: str = Query(""), db: AsyncSession = Depends(get_db)):
    if not request.session.get("user_id") or len(q) < 2:
        return HTMLResponse("")

    bp_results = await sde.search_types(db, q + " Blueprint", limit=10)
    raw_results = await sde.search_types(db, q, limit=10)

    seen = set()
    merged = []
    for r in bp_results + raw_results:
        if r["type_id"] not in seen:
            seen.add(r["type_id"])
            merged.append(r)

    valid = []
    for r in merged[:15]:
        mats = await sde.get_blueprint_materials(db, r["type_id"])
        if mats:
            valid.append(r)
        if len(valid) >= 8:
            break

    if not valid:
        return HTMLResponse('<div class="b-empty">No blueprints found</div>')

    html_parts = []
    for r in valid:
        html_parts.append(
            f'<div class="b-table-row" style="cursor:pointer;" '
            f'onclick="selectBlueprint({r["type_id"]})">'
            f'<img src="https://images.evetech.net/types/{r["type_id"]}/icon?size=32" '
            f'style="width:24px;height:24px;border:1px solid var(--border);flex-shrink:0;" '
            f'onerror="this.style.display=\'none\'">'
            f'<span style="font-size:11px;color:var(--text);">{r["type_name"]}</span>'
            f'</div>'
        )
    return HTMLResponse("\n".join(html_parts))


@router.get("/industry/calculate", response_class=HTMLResponse)
async def industry_calculate(
    request: Request,
    type_id: int = Query(...),
    me: int = Query(0),
    te: int = Query(0),
    runs: int = Query(1),
    structure: str = Query("npc_station"),
    rig: str = Query("none"),
    security: str = Query("highsec"),
    db: AsyncSession = Depends(get_db),
):
    if not request.session.get("user_id"):
        return HTMLResponse("")

    me = max(0, min(10, me))
    te = max(0, min(20, te))
    runs = max(1, min(1000, runs))

    struct_info = STRUCTURES.get(structure, STRUCTURES["npc_station"])
    rig_info = RIGS.get(rig, RIGS["none"])
    sec_info = SEC_STATUS.get(security, SEC_STATUS["highsec"])

    materials = await sde.get_blueprint_materials(db, type_id)
    bp_name = await sde.type_id_to_name(db, type_id) or f"Type {type_id}"

    if not materials:
        return HTMLResponse(f'<div class="b-empty">No manufacturing data for {bp_name}</div>')

    # Check which materials have blueprints (buildable)
    all_type_ids = {m["type_id"] for m in materials}
    price_map = await _get_price_map(db, all_type_ids)

    total_cost = 0.0
    total_base_cost = 0.0
    rows = []
    for m in materials:
        base_qty = m["quantity"]
        adjusted_qty = _calc_material(
            base_qty, runs, me,
            struct_info["mat"], rig_info["mat"], sec_info["mult"],
        )
        base_total = base_qty * runs
        saved = base_total - adjusted_qty

        price = price_map.get(m["type_id"], 0)
        line_cost = price * adjusted_qty
        total_cost += line_cost
        total_base_cost += price * base_total

        # Check if this material is buildable and get its build time
        sub_bp = await sde.find_blueprint_for_product(db, m["type_id"])
        build_time_str = None
        build_time_secs = 0
        if sub_bp:
            base_time = await sde.get_blueprint_time(db, sub_bp)
            if base_time:
                build_time_secs = _calc_time(
                    base_time, te, struct_info["time"],
                    rig_info.get("time", 0), sec_info["mult"],
                ) * adjusted_qty  # time per run * number of runs
                build_time_str = _format_time(build_time_secs)

        rows.append({
            "type_id": m["type_id"],
            "name": m["name"],
            "base_qty": base_qty,
            "adjusted_qty": adjusted_qty,
            "saved": saved,
            "unit_price": price,
            "line_cost": line_cost,
            "buildable": sub_bp is not None,
            "sub_bp_id": sub_bp,
            "build_time_str": build_time_str,
            "build_time_secs": build_time_secs,
        })

    total_saved_cost = total_base_cost - total_cost

    modifiers = []
    if me > 0:
        modifiers.append(f"ME {me}")
    if struct_info["mat"] < 1.0:
        modifiers.append(f'{struct_info["label"]} -{(1 - struct_info["mat"]) * 100:.0f}%')
    if rig_info["mat"] > 0:
        eff_rig = rig_info["mat"] * sec_info["mult"] * 100
        modifiers.append(f'{rig_info["label"]} rig -{eff_rig:.1f}% ({sec_info["label"]})')

    # Calculate main blueprint build time
    main_bp_time = await sde.get_blueprint_time(db, type_id)
    main_time_secs = 0
    if main_bp_time:
        main_time_secs = _calc_time(
            main_bp_time, te, struct_info["time"],
            rig_info.get("time", 0), sec_info["mult"],
        ) * runs

    # Calculate total build time: components in parallel + main build
    component_times = [r["build_time_secs"] for r in rows if r.get("build_time_secs")]
    max_component_time = max(component_times) if component_times else 0
    total_time_parallel = max_component_time + main_time_secs  # parallel components, then main
    total_time_sequential = sum(component_times) + main_time_secs  # all sequential

    return templates.TemplateResponse(request, "partials/calc_results.html", {"bp_name": bp_name,
        "rows": rows,
        "runs": runs,
        "me": me,
        "te": te,
        "total_cost": total_cost,
        "total_saved_cost": total_saved_cost,
        "modifiers": modifiers,
        "structures": STRUCTURES,
        "rigs": RIGS,
        "sec_statuses": SEC_STATUS,
        "main_time_str": _format_time(main_time_secs),
        "parallel_time_str": _format_time(total_time_parallel),
        "sequential_time_str": _format_time(total_time_sequential),
        "main_time_secs": main_time_secs})


@router.get("/industry/component", response_class=HTMLResponse)
async def industry_component(
    request: Request,
    type_id: int = Query(..., description="Product type_id"),
    needed: int = Query(1, description="Quantity needed from parent"),
    me: int = Query(0),
    structure: str = Query("npc_station"),
    rig: str = Query("none"),
    security: str = Query("highsec"),
    db: AsyncSession = Depends(get_db),
):
    """Calculate sub-component BOM for a buildable material."""
    if not request.session.get("user_id"):
        return HTMLResponse("")

    me = max(0, min(10, me))
    needed = max(1, needed)

    struct_info = STRUCTURES.get(structure, STRUCTURES["npc_station"])
    rig_info = RIGS.get(rig, RIGS["none"])
    sec_info = SEC_STATUS.get(security, SEC_STATUS["highsec"])

    sub_bp = await sde.find_blueprint_for_product(db, type_id)
    product_name = await sde.type_id_to_name(db, type_id) or f"Type {type_id}"

    if not sub_bp:
        return HTMLResponse(f'<div class="b-empty">{product_name} has no blueprint</div>')

    materials = await sde.get_blueprint_materials(db, sub_bp)
    if not materials:
        return HTMLResponse(f'<div class="b-empty">No materials for {product_name}</div>')

    all_type_ids = {m["type_id"] for m in materials}
    price_map = await _get_price_map(db, all_type_ids)

    # Calculate: we need `needed` units, each run produces 1 unit
    runs = needed
    total_build_cost = 0.0
    sub_rows = []
    for m in materials:
        adjusted_qty = _calc_material(
            m["quantity"], runs, me,
            struct_info["mat"], rig_info["mat"], sec_info["mult"],
        )
        price = price_map.get(m["type_id"], 0)
        line_cost = price * adjusted_qty

        total_build_cost += line_cost
        sub_bp = await sde.find_blueprint_for_product(db, m["type_id"])
        sub_rows.append({
            "type_id": m["type_id"],
            "name": m["name"],
            "base_qty": m["quantity"] * runs,
            "adjusted_qty": adjusted_qty,
            "unit_price": price,
            "line_cost": line_cost,
            "buildable": sub_bp is not None,
        })

    # Buy cost for comparison
    buy_price = price_map.get(type_id, 0)
    # We need to fetch the product price too
    if type_id not in price_map:
        prod_prices = await _get_price_map(db, {type_id})
        buy_price = prod_prices.get(type_id, 0)
    total_buy_cost = buy_price * needed

    return templates.TemplateResponse(request, "partials/component_panel.html", {"product_name": product_name,
        "type_id": type_id,
        "needed": needed,
        "me": me,
        "structure": structure,
        "rig": rig,
        "security": security,
        "sub_rows": sub_rows,
        "total_build_cost": total_build_cost,
        "total_buy_cost": total_buy_cost,
        "buy_unit_price": buy_price,
        "structures": STRUCTURES,
        "rigs": RIGS,
        "sec_statuses": SEC_STATUS})



@router.get("/industry/shopping-list", response_class=HTMLResponse)
async def industry_shopping_list(
    request: Request,
    type_id: int = Query(...),
    me: int = Query(0),
    runs: int = Query(1),
    structure: str = Query("npc_station"),
    rig: str = Query("none"),
    security: str = Query("highsec"),
    build_json: str = Query("{}"),
    db: AsyncSession = Depends(get_db),
):
    """Aggregate all materials into a shopping list.

    build_json is a JSON dict mapping type_id (str) -> {me, structure, rig, security}
    for components the user chose to Build. Everything else is Buy.
    """
    if not request.session.get("user_id"):
        return HTMLResponse("")

    me = max(0, min(10, me))
    runs = max(1, min(1000, runs))

    try:
        build_components = json.loads(build_json)
    except (json.JSONDecodeError, TypeError):
        build_components = {}

    struct_info = STRUCTURES.get(structure, STRUCTURES["npc_station"])
    rig_info = RIGS.get(rig, RIGS["none"])
    sec_info = SEC_STATUS.get(security, SEC_STATUS["highsec"])

    materials = await sde.get_blueprint_materials(db, type_id)
    if not materials:
        return HTMLResponse('<div class="b-empty">No materials</div>')

    # Aggregate: {type_id: {"name": str, "qty": int}}
    shopping: dict[int, dict] = {}

    def _add(tid: int, name: str, qty: int):
        if tid in shopping:
            shopping[tid]["qty"] += qty
        else:
            shopping[tid] = {"name": name, "qty": qty}

    for m in materials:
        adjusted_qty = _calc_material(
            m["quantity"], runs, me,
            struct_info["mat"], rig_info["mat"], sec_info["mult"],
        )

        tid_str = str(m["type_id"])
        if tid_str in build_components:
            # This component is being built — resolve its sub-materials
            comp = build_components[tid_str]
            comp_me = max(0, min(10, int(comp.get("me", 0))))
            comp_struct = STRUCTURES.get(comp.get("structure", "npc_station"), STRUCTURES["npc_station"])
            comp_rig = RIGS.get(comp.get("rig", "none"), RIGS["none"])
            comp_sec = SEC_STATUS.get(comp.get("security", "highsec"), SEC_STATUS["highsec"])

            sub_bp = await sde.find_blueprint_for_product(db, m["type_id"])
            if sub_bp:
                sub_mats = await sde.get_blueprint_materials(db, sub_bp)
                for sm in sub_mats:
                    sub_qty = _calc_material(
                        sm["quantity"], adjusted_qty, comp_me,
                        comp_struct["mat"], comp_rig["mat"], comp_sec["mult"],
                    )
                    _add(sm["type_id"], sm["name"], sub_qty)
            else:
                # Fallback: can't find blueprint, treat as buy
                _add(m["type_id"], m["name"], adjusted_qty)
        else:
            # Buy this material directly
            _add(m["type_id"], m["name"], adjusted_qty)

    # Sort alphabetically by name
    sorted_items = sorted(shopping.values(), key=lambda x: x["name"])

    # Extract minerals (type_ids 34-40) for compression calculator link
    MINERAL_TYPE_IDS = {34, 35, 36, 37, 38, 39, 40}
    mineral_items = {tid: info["qty"] for tid, info in shopping.items() if tid in MINERAL_TYPE_IDS}

    # Get prices for total cost estimate
    all_ids = set(shopping.keys())
    price_map = await _get_price_map(db, all_ids)
    total_cost = sum(price_map.get(tid, 0) * info["qty"] for tid, info in shopping.items())

    # Get volumes for hauling calculator
    volumes = await sde.get_type_volumes(db, list(all_ids))
    total_volume = 0.0
    for tid, info in shopping.items():
        vol = volumes.get(tid, 0.0)
        info["volume"] = vol
        info["total_volume"] = round(vol * info["qty"], 2)
        info["type_id"] = tid
        total_volume += info["total_volume"]

    # Build multibuy-compatible text
    multibuy_lines = [f'{item["name"]} x{item["qty"]}' for item in sorted_items]
    multibuy_text = "\n".join(multibuy_lines)

    return templates.TemplateResponse(request, "partials/shopping_list.html", {"items": sorted_items,
        "item_count": len(sorted_items),
        "total_cost": total_cost,
        "total_volume": total_volume,
        "multibuy_text": multibuy_text,
        "price_map": {shopping[tid]["name"]: price_map.get(tid, 0) for tid in shopping},
        "mineral_items": mineral_items})



# ── Build-Profitability Finder (Phase 4 Task 4) ───────────────────────────────

BUILD_FINDER_CAP = 200


def _fmt_isk(v: float | None) -> str:
    """Compact ISK for the finder table (B/M/K, signed for margins)."""
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1_000_000_000:
        return f"{v / 1_000_000_000:.2f}B"
    if a >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    if a >= 1_000:
        return f"{v / 1_000:.2f}K"
    return f"{v:,.2f}"


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:,.1f}%"


@router.get("/industry/build-finder", response_class=HTMLResponse)
async def build_finder_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Landing page — an EVE-style market-group tree picker (htmx-loaded via
    the /tree fragments below) + ME/structure/rig/security controls, plus
    invention controls (character/skills/decryptor). The ranked table loads
    via htmx on submit (see below)."""
    if not request.session.get("user_id"):
        return RedirectResponse("/")
    return templates.TemplateResponse(request, "build_finder.html", {
        "structures": STRUCTURES,
        "rigs": RIGS,
        "sec_statuses": SEC_STATUS,
        "cap": BUILD_FINDER_CAP,
        "decryptors": DECRYPTORS,
    })


@router.get("/industry/build-finder/tree", response_class=HTMLResponse)
async def build_finder_tree(
    request: Request,
    parent: int = Query(0),
    db: AsyncSession = Depends(get_db),
):
    """htmx fragment: one level of the market-group tree.

    `parent=0` → root groups (parent_group_id IS NULL), rendered as a bare
    list for the tree container. `parent=<id>` → that node's children,
    wrapped in the node's own `#bft-kids-<id>` slot so the expand arrow's
    `hx-swap="outerHTML"` replaces the empty placeholder in place (the same
    per-level lazy idiom as fitting's /tools/fitting/browse/groups)."""
    if not request.session.get("user_id"):
        return HTMLResponse("", status_code=401)
    nodes = await sde.get_market_group_children(db, parent or None)
    return templates.TemplateResponse(request, "partials/build_finder_tree.html", {
        "nodes": nodes,
        "parent": parent,
        "mode": "tree",
    })


@router.get("/industry/build-finder/tree/search", response_class=HTMLResponse)
async def build_finder_tree_search(
    request: Request,
    q: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """htmx fragment: market-group search results, path-labeled ("Ships >
    Frigates > Assault Frigates"), each selectable like a tree node.

    Queries under 2 chars (including a cleared box) return the ROOT tree
    fragment instead — the search input targets the same container as the
    tree, so this is what makes clearing the box restore the browser."""
    if not request.session.get("user_id"):
        return HTMLResponse("", status_code=401)
    q = (q or "").strip()
    if len(q) < 2:
        nodes = await sde.get_market_group_children(db, None)
        return templates.TemplateResponse(request, "partials/build_finder_tree.html", {
            "nodes": nodes,
            "parent": 0,
            "mode": "tree",
        })
    rows = await sde.search_market_groups(db, q)
    return templates.TemplateResponse(request, "partials/build_finder_tree.html", {
        "nodes": rows,
        "parent": 0,
        "mode": "search",
    })


@router.get("/industry/build-finder/results", response_class=HTMLResponse)
async def build_finder_results(
    request: Request,
    market_group_id: int = Query(0),
    me: int = Query(10),
    structure: str = Query("npc_station"),
    rig: str = Query("none"),
    security: str = Query("highsec"),
    character_id: int = Query(0),
    encryption: int = Query(4, ge=0, le=5),
    science: int = Query(4, ge=0, le=5),
    decryptor: str = Query("none"),
    db: AsyncSession = Depends(get_db),
):
    """htmx partial: buildable products in a market-group SUBTREE (the picked
    tree node + all its descendants), ranked by margin %.

    Bounded work: at most `BUILD_FINDER_CAP` blueprints priced per request (the
    subtree's full count is reported for a "showing N of M" footer). TE is
    intentionally not a control — it only affects build *time*, and the ranking
    is by ISK margin, so it can't change the result. Sell value + material costs
    come from the global `/markets/prices/` map (`market_lp.get_price_map`), the
    same source the LP tool uses.

    Invention: `sde.get_invention_data` resolves which of this group's
    products are T2-inventable. When any are, per-product probability comes
    from either the selected character's ESI skills (ownership + scope
    checked, same pattern as `/market/pnl`) or the manual `encryption`/
    `science` selects, and expected per-unit overhead
    (`app.industry.invention`) is folded into the ranking via
    `build_finder.rank_builds(..., invention=...)`. An unpriced decryptor
    (or any un-inventable product) yields `overhead_per_unit=None`, which
    `rank_builds` treats as unpriced — never silently zero-costed."""
    if not request.session.get("user_id"):
        return HTMLResponse("", status_code=401)
    if not market_group_id:
        return HTMLResponse('<div class="b-empty">Pick a group to rank.</div>')

    user_id = request.session.get("user_id")
    me = max(0, min(10, me))
    struct_info = STRUCTURES.get(structure, STRUCTURES["npc_station"])
    rig_info = RIGS.get(rig, RIGS["none"])
    sec_info = SEC_STATUS.get(security, SEC_STATUS["highsec"])

    start = time.perf_counter()
    total_n, products = await sde.get_market_group_subtree_products(
        db, market_group_id, cap=BUILD_FINDER_CAP)

    ranked = []
    invention_active = False
    tables_empty = False
    selected_character_id = 0
    if products:
        from sqlalchemy import select

        # One global price fetch covers products + every material + decryptors.
        price_map = await market_lp.get_price_map(db)

        invention_data = await sde.get_invention_data(
            db, [p["product_type_id"] for p in products],
        )
        invention_active = bool(invention_data)
        if not invention_active:
            # Distinguish "this group has no T2/inventable items" (normal,
            # no message needed) from "invention tables were never imported"
            # (SDE reload pending — surfaced in the footnote).
            exists_row = (await db.execute(
                select(SDEBlueprintInvention.blueprint_type_id).limit(1)
            )).first()
            tables_empty = exists_row is None

        invention_map: dict[int, dict] = {}
        if invention_data:
            dec = DECRYPTORS.get(decryptor)

            char_skills = None
            if character_id:
                char_result = await db.execute(
                    select(Character).where(
                        Character.character_id == character_id,
                        Character.user_id == user_id,
                    )
                )
                char = char_result.scalar_one_or_none()
                if char and _SKILLS_SCOPE in (char.scopes or ""):
                    selected_character_id = character_id
                    try:
                        char_skills = await _character_skills_map(db, char)
                    except Exception:
                        char_skills = None

            all_skill_ids = sorted({
                sid for inv in invention_data.values() for sid in inv["skill_ids"]
            })
            skill_names = (
                await sde.type_ids_to_names(db, all_skill_ids) if all_skill_ids else {}
            )

            decryptor_price = price_map.get(dec.type_id) if dec else None

            for product_id, inv in invention_data.items():
                e, s1, s2, missing = _resolve_invention_skills(
                    char_skills, inv["skill_ids"], skill_names, encryption, science,
                )
                p_final = invention_probability(
                    inv["probability"], e, s1, s2,
                    dec.prob_mult if dec else 1.0,
                )
                if dec is not None and decryptor_price is None:
                    attempt = None
                else:
                    attempt = attempt_cost(
                        inv["datacores"], price_map, decryptor_price or 0.0,
                    )
                runs, ime = invented_bpc(inv["base_runs"], dec)
                overhead = (
                    invention_overhead_per_unit(
                        attempt, p_final, runs, inv["per_run_output_qty"],
                    ) if attempt is not None else None
                )
                invention_map[product_id] = {
                    "overhead_per_unit": overhead,
                    "invented_me": ime,
                    "skill_missing": missing,
                }

        ranked = build_finder.rank_builds(
            products, me, struct_info["mat"], rig_info["mat"], sec_info["mult"],
            price_map, invention=invention_map or None,
        )
    compute_ms = round((time.perf_counter() - start) * 1000, 1)

    rows = [{
        "product_type_id": r["product_type_id"],
        "product_name": r["product_name"],
        "cost_str": _fmt_isk(r["cost_per_unit"]),
        "sell_str": _fmt_isk(r["sell_per_unit"]),
        "margin_isk_str": _fmt_isk(r["margin_isk"]),
        "margin_pct_str": _fmt_pct(r["margin_pct"]),
        "margin_positive": (r["margin_isk"] is not None and r["margin_isk"] > 0),
        "priced": r["priced"],
        "inv_str": _fmt_isk(r["invention_overhead"]) if r["invention_overhead"] is not None else None,
        "skill_missing": r["skill_missing"],
    } for r in ranked]

    return templates.TemplateResponse(request, "partials/build_finder_results.html", {
        "rows": rows,
        "total_n": total_n,
        "shown": len(products),
        "capped": total_n > BUILD_FINDER_CAP,
        "me": me,
        "compute_ms": compute_ms,
        "invention_active": invention_active,
        "tables_empty": tables_empty,
        "selected_character_id": selected_character_id,
        "encryption": encryption,
        "science": science,
        "decryptor": decryptor,
    })


# ── Invention: characters + skill resolution ─────────────────────────────

_SKILLS_SCOPE = "esi-skills.read_skills.v1"


@router.get("/industry/build-finder/characters")
async def build_finder_characters(request: Request, db: AsyncSession = Depends(get_db)):
    """Dropdown source — characters the user owns that have the skills scope
    (copy of `list_fitting_characters` in app/routes/fitting.py)."""
    user_id = request.session.get("user_id")
    if not user_id:
        return {"characters": []}
    from sqlalchemy import select
    r = await db.execute(
        select(Character)
        .where(Character.user_id == user_id)
        .order_by(Character.character_name)
    )
    return {
        "characters": [
            {"id": c.character_id, "name": c.character_name}
            for c in r.scalars().all()
            if _SKILLS_SCOPE in (c.scopes or "")
        ],
    }


def _resolve_invention_skills(
    char_skills: dict[int, int] | None,
    skill_ids: list[int],
    skill_names: dict[int, str],
    encryption_manual: int,
    science_manual: int,
) -> tuple[int, int, int, bool]:
    """Resolve (E, S1, S2, missing_flag) feeding `invention_probability`.

    Character mode (`char_skills` is not None): the encryption skill is the
    one whose SDE type name ends with "Encryption Methods" — identified by
    NAME, never by position, since `skill_ids` order from the SDE is not
    guaranteed. The other two entries are the sciences. E = the character's
    level of the encryption skill (0 if untrained); S1/S2 = levels of the two
    sciences. `missing_flag` is True if ANY of the three resolves to 0
    (untrained or not owned).

    A degenerate `skill_ids` list — not exactly 3 entries, or no entry whose
    name resolves to an "Encryption Methods" skill — can't be safely split
    into E vs S, so it falls back to the manual values with
    `missing_flag=False`.

    Manual mode (`char_skills` is None): `(encryption_manual, science_manual,
    science_manual, False)` — no character data, so nothing can be "missing"."""
    if char_skills is None:
        return encryption_manual, science_manual, science_manual, False

    encryption_id = None
    if len(skill_ids) == 3:
        for sid in skill_ids:
            if skill_names.get(sid, "").endswith("Encryption Methods"):
                encryption_id = sid
                break

    if encryption_id is None:
        return encryption_manual, science_manual, science_manual, False

    science_ids = [sid for sid in skill_ids if sid != encryption_id]
    e_level = char_skills.get(encryption_id, 0)
    s1 = char_skills.get(science_ids[0], 0)
    s2 = char_skills.get(science_ids[1], 0)
    missing = e_level == 0 or s1 == 0 or s2 == 0
    return e_level, s1, s2, missing


# ── Compression Calculator ────────────────────────────────────────────────────

@router.get("/industry/compression", response_class=HTMLResponse)
async def compression_page(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")

    from sqlalchemy import select
    result = await db.execute(select(Character).where(Character.user_id == user_id))
    characters = [
        {"character_id": c.character_id, "character_name": c.character_name}
        for c in result.scalars().all()
    ]

    # Pre-fill mineral values from query params (from manufacturing shopping list)
    prefill = {}
    for name, mid in MINERALS.items():
        val = request.query_params.get(f"mineral_{mid}", "0")
        try:
            prefill[mid] = int(val)
        except (ValueError, TypeError):
            prefill[mid] = 0

    return templates.TemplateResponse(request, "compression.html", {"characters": characters,
        "minerals": MINERALS,
        "structures": REPRO_STRUCTURES,
        "rigs": REPRO_RIGS,
        "security": REPRO_SECURITY,
        "implants": REPRO_IMPLANTS,
        "trade_hubs": TRADE_HUBS,
        "prefill": prefill})


@router.get("/industry/compression/skills/{character_id}", response_class=HTMLResponse)
async def compression_skills(
    request: Request, character_id: int, db: AsyncSession = Depends(get_db),
):
    """Htmx partial: fetch character reprocessing skill levels."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("")

    from sqlalchemy import select
    from app.esi.client import refresh_token
    from app.esi import character as esi_char

    char_result = await db.execute(
        select(Character).where(Character.character_id == character_id, Character.user_id == user_id)
    )
    char = char_result.scalar_one_or_none()
    if not char or "esi-skills.read_skills.v1" not in (char.scopes or ""):
        return HTMLResponse('<span style="font-size:10px;color:var(--muted);">Skills scope not available</span>')

    try:
        token = await refresh_token(char, db)
        client = ESIClient(token, db=db)
        raw = await esi_char.get_skills(client, character_id)
        skills_by_id = {s["skill_id"]: s.get("active_skill_level", 0) for s in raw.get("skills", [])}

        repro = skills_by_id.get(SKILL_REPROCESSING, 0)
        eff = skills_by_id.get(SKILL_REPROCESSING_EFFICIENCY, 0)

        # Build skill display with new tier-based skills
        SKILL_LABELS = {
            60377: "Simple", 60378: "Coherent", 60379: "Variegated",
            60380: "Complex", 60381: "Abyssal", 12189: "Mercoxit",
        }
        html = '<div style="display:flex;gap:0.75rem;flex-wrap:wrap;align-items:center;">'
        html += f'<div style="display:flex;flex-direction:column;gap:3px;"><label style="font-size:9px;letter-spacing:0.12em;text-transform:uppercase;color:var(--muted);">Reprocessing</label><input type="number" name="repro_level" value="{repro}" min="0" max="5" style="width:45px;padding:0.25rem 0.4rem;background:var(--bg);border:1px solid var(--border);color:var(--text);font-family:inherit;font-size:11px;text-align:center;"></div>'
        html += f'<div style="display:flex;flex-direction:column;gap:3px;"><label style="font-size:9px;letter-spacing:0.12em;text-transform:uppercase;color:var(--muted);">Efficiency</label><input type="number" name="eff_level" value="{eff}" min="0" max="5" style="width:45px;padding:0.25rem 0.4rem;background:var(--bg);border:1px solid var(--border);color:var(--text);font-family:inherit;font-size:11px;text-align:center;"></div>'
        for sid, label in SKILL_LABELS.items():
            lv = skills_by_id.get(sid, 0)
            html += f'<span style="font-size:10px;color:var(--text);border:1px solid var(--border);padding:2px 6px;">{label} <strong>{lv}</strong></span>'
        html += f'<span style="font-size:9px;color:var(--success);align-self:flex-end;padding-bottom:4px;">Loaded</span>'
        html += '</div>'

        # Store ore-specific skill levels as JSON for the form
        # Map each group_id to the character's level in its processing skill
        ore_skills = {}
        for group_id, skill_id in ORE_GROUP_SKILL.items():
            ore_skills[str(group_id)] = skills_by_id.get(skill_id, 0)
        import json
        html += f'<input type="hidden" id="skill-ore-json" value=\'{json.dumps(ore_skills)}\'>'

        return HTMLResponse(html)
    except Exception:
        return HTMLResponse('<span style="font-size:10px;color:var(--danger);">Failed to load skills</span>')


@router.post("/industry/compression/calculate", response_class=HTMLResponse)
async def compression_calculate(
    request: Request, db: AsyncSession = Depends(get_db),
):
    """Main compression calculation endpoint."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("")

    form = await request.form()

    # Parse mineral targets
    target = {}
    for name, mid in MINERALS.items():
        val = form.get(f"mineral_{mid}", "0")
        try:
            target[mid] = max(0, int(val.replace(",", "")))
        except (ValueError, AttributeError):
            target[mid] = 0

    if sum(target.values()) == 0:
        return HTMLResponse('<div class="b-empty">Enter at least one mineral amount.</div>')

    structure = form.get("structure", "npc_station")
    rig = form.get("rig", "none")
    security = form.get("security", "highsec")
    implant = form.get("implant", "none")
    mode = form.get("mode", "isk")
    hub = form.get("trade_hub", "jita")
    repro_level = int(form.get("repro_level", "0"))
    eff_level = int(form.get("eff_level", "0"))
    ore_skills_json = form.get("ore_skills", "{}")

    import json as _json
    try:
        ore_skills = _json.loads(ore_skills_json)
    except Exception:
        ore_skills = {}

    # Load compressed ore data from SDE
    ore_data = await sde.get_ore_reprocessing_map(db)
    if not ore_data:
        return HTMLResponse('<div class="b-empty">No ore data available. SDE may need to reload.</div>')

    # Filter to only ores that produce standard minerals (34-40)
    mineral_set = set(MINERAL_IDS)
    filtered_ore_data = {}
    for oid, data in ore_data.items():
        ore_minerals = {mid: qty for mid, qty in data["minerals"].items() if mid in mineral_set}
        if ore_minerals:
            filtered_ore_data[oid] = {**data, "minerals": ore_minerals}

    # Compute per-ore yield (varies by ore-specific skill)
    yield_per_ore = {}
    for oid, data in filtered_ore_data.items():
        group_id = data.get("group_id", 0)
        ore_skill = int(ore_skills.get(str(group_id), 0))
        yield_per_ore[oid] = compute_yield(
            structure=structure, rig=rig, security=security,
            repro_level=repro_level, efficiency_level=eff_level,
            ore_skill_level=ore_skill, implant=implant,
        )

    # Fetch global average prices (single ESI call, covers all types)
    hub_info = TRADE_HUBS.get(hub, TRADE_HUBS["jita"])
    ore_prices = {}
    mineral_prices = {}
    try:
        global_prices = await esi_market.get_market_prices(ESIClient("", db=db))
        for p in global_prices:
            tid = p.get("type_id")
            price = p.get("average_price") or p.get("adjusted_price") or 0
            if tid in filtered_ore_data:
                ore_prices[tid] = price
            if mode == "waste" and tid in (34, 35, 36, 37, 38, 39, 40):
                mineral_prices[tid] = price or 1.0
    except Exception:
        pass

    # Remove ores with zero or missing prices
    ore_prices = {k: v for k, v in ore_prices.items() if v > 0}

    # Run solver
    result = solve_compression(target, filtered_ore_data, ore_prices, yield_per_ore, mode, mineral_prices)

    if result.get("error"):
        return HTMLResponse(f'<div class="b-empty" style="color:var(--danger);">{result["error"]}</div>')

    # Build multibuy text
    multibuy_lines = [f'{ore["name"]} x{ore["quantity"]}' for ore in result["ores"]]
    multibuy_text = "\n".join(multibuy_lines)

    target_named = {MINERAL_NAMES.get(mid, str(mid)): qty for mid, qty in target.items() if qty > 0}

    return templates.TemplateResponse(request, "partials/compression_results.html", {"ores": result["ores"],
        "total_isk": result["total_isk"],
        "total_volume": result["total_volume"],
        "minerals_produced": result["minerals_produced"],
        "minerals_surplus": result["minerals_surplus"],
        "target_minerals": target_named,
        "multibuy_text": multibuy_text,
        "mode": mode,
        "hub_label": hub_info["label"]})


# ── Hauling Calculator ────────────────────────────────────────────────────────

from app.industry.hauling import (
    HAULING_SHIPS, CARGO_MODULES, CARGO_RIGS, BAY_LABELS,
    get_ships_by_group, calculate_effective_capacity,
    parse_eve_paste, categorize_item, recommend_ships,
)


@router.get("/industry/hauling", response_class=HTMLResponse)
async def industry_hauling(request: Request):
    if not request.session.get("user_id"):
        return RedirectResponse("/")
    return templates.TemplateResponse(request, "hauling.html", {"ships_by_group": get_ships_by_group(),
        "all_ships": HAULING_SHIPS,
        "cargo_modules": CARGO_MODULES,
        "cargo_rigs": CARGO_RIGS,
        "bay_labels": BAY_LABELS})


@router.post("/industry/hauling/resolve", response_class=HTMLResponse)
async def hauling_resolve(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Parse pasted item list, resolve names, look up volumes, categorize."""
    form = await request.form()
    paste_text = form.get("paste_text", "")

    if not paste_text.strip():
        return HTMLResponse('<div class="b-empty">Paste items above to resolve</div>')

    parsed = parse_eve_paste(paste_text)
    if not parsed:
        return HTMLResponse('<div class="b-empty" style="color:var(--danger);">Could not parse any items</div>')

    # Resolve names to type_ids
    resolved = []
    unresolved = []
    for item in parsed:
        type_id = await sde.type_name_to_id(db, item["name"])
        if type_id:
            resolved.append({"type_id": type_id, "name": item["name"], "qty": item["qty"]})
        else:
            unresolved.append(item["name"])

    if not resolved:
        return HTMLResponse('<div class="b-empty" style="color:var(--danger);">No items could be resolved</div>')

    # Get volumes and group_ids
    type_ids = [r["type_id"] for r in resolved]
    volumes = await sde.get_type_volumes(db, type_ids)
    group_ids = await sde.get_type_group_ids(db, type_ids)

    # Enrich items
    items_by_bay: dict[str, float] = {}
    for item in resolved:
        tid = item["type_id"]
        vol = volumes.get(tid, 0.0)
        gid = group_ids.get(tid)
        bay = categorize_item(gid)
        item["volume"] = vol
        item["total_volume"] = round(vol * item["qty"], 2)
        item["bay"] = bay
        items_by_bay[bay] = items_by_bay.get(bay, 0) + item["total_volume"]

    total_volume = sum(item["total_volume"] for item in resolved)

    # Get recommendations
    recommendations = recommend_ships(items_by_bay)

    return templates.TemplateResponse(request, "partials/hauling_resolved.html", {"items": resolved,
        "unresolved": unresolved,
        "items_by_bay": items_by_bay,
        "total_volume": total_volume,
        "recommendations": recommendations,
        "bay_labels": BAY_LABELS})


# ── Appraisal Calculator ─────────────────────────────────────────────────────

from app.esi.market import APPRAISAL_HUBS, get_hub_prices_batch
from app.industry.hauling import parse_eve_paste


@router.get("/industry/appraisal", response_class=HTMLResponse)
async def industry_appraisal(request: Request):
    if not request.session.get("user_id"):
        return RedirectResponse("/")
    return templates.TemplateResponse(request, "appraisal.html", {"hubs": APPRAISAL_HUBS})


@router.post("/industry/appraisal/calculate", response_class=HTMLResponse)
async def appraisal_calculate(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Parse pasted items, resolve via SDE, fetch live sell prices from trade hub."""
    form = await request.form()
    paste_text = form.get("paste_text", "")
    hub_key = form.get("hub", "jita")

    if not paste_text.strip():
        return HTMLResponse('<div class="b-empty">Paste items above to appraise</div>')

    if hub_key not in APPRAISAL_HUBS:
        hub_key = "jita"

    parsed = parse_eve_paste(paste_text)
    if not parsed:
        return HTMLResponse('<div class="b-empty" style="color:var(--danger);">Could not parse any items</div>')

    # Resolve names to type_ids
    resolved = []
    unresolved = []
    for item in parsed:
        type_id = await sde.type_name_to_id(db, item["name"])
        if type_id:
            resolved.append({"type_id": type_id, "name": item["name"], "qty": item["qty"]})
        else:
            unresolved.append(item["name"])

    if not resolved:
        return HTMLResponse('<div class="b-empty" style="color:var(--danger);">No items could be resolved</div>')

    # Get volumes
    type_ids = [r["type_id"] for r in resolved]
    volumes = await sde.get_type_volumes(db, type_ids)

    # Get live sell prices from trade hub
    client = ESIClient("", db=db)
    prices = await get_hub_prices_batch(client, hub_key, type_ids)

    # Build results
    items = []
    total_isk = 0.0
    total_volume = 0.0
    for r in resolved:
        tid = r["type_id"]
        price = prices.get(tid)
        vol = volumes.get(tid, 0.0)
        item_total = (price or 0) * r["qty"]
        item_vol = vol * r["qty"]
        total_isk += item_total
        total_volume += item_vol
        items.append({
            "type_id": tid,
            "name": r["name"],
            "qty": r["qty"],
            "unit_price": price,
            "total_price": item_total,
            "unit_volume": vol,
            "total_volume": round(item_vol, 2),
        })

    # Sort by total price descending (most valuable first)
    items.sort(key=lambda x: x["total_price"], reverse=True)

    hub = APPRAISAL_HUBS[hub_key]

    return templates.TemplateResponse(request, "partials/appraisal_results.html", {"items": items,
        "unresolved": unresolved,
        "total_isk": total_isk,
        "total_volume": total_volume,
        "hub_label": hub["label"],
        "item_count": len(items)})
