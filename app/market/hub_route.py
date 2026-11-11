"""Jump counts from a starting system to the trade hubs.

A highsec-only path is preferred and labelled as such; when none exists the
shortest path is returned and flagged as leaving highsec. Jump counts only —
no hauling cost."""
from __future__ import annotations

from collections import deque

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.sde_models import SDESystem
from app.sde import lookup as sde

# The client shows security rounded to one decimal, so 0.45 displays as 0.5.
HIGHSEC_MIN = 0.45


def is_highsec(security: float | None) -> bool:
    return security is not None and security >= HIGHSEC_MIN


def bfs_distances(graph: dict[int, list[int]], origin: int, allowed=None) -> dict[int, int]:
    """{system_id: jumps} for every system reachable from `origin`. With
    `allowed`, only systems for which allowed(system_id) is true are entered."""
    distances = {origin: 0}
    queue = deque([origin])
    while queue:
        current = queue.popleft()
        for neighbour in graph.get(current, []):
            if neighbour in distances:
                continue
            if allowed is not None and not allowed(neighbour):
                continue
            distances[neighbour] = distances[current] + 1
            queue.append(neighbour)
    return distances


def routes_from(graph: dict[int, list[int]], security: dict[int, float | None],
                origin: int, hub_systems: dict[str, int]) -> dict[str, dict | None]:
    """{hub_key: {"jumps": int, "highsec": bool} | None}."""
    def safe(system_id: int) -> bool:
        return is_highsec(security.get(system_id))

    safe_dist = bfs_distances(graph, origin, safe) if safe(origin) else {}
    any_dist = bfs_distances(graph, origin)
    out: dict[str, dict | None] = {}
    for key, system_id in hub_systems.items():
        if system_id in safe_dist:
            out[key] = {"jumps": safe_dist[system_id], "highsec": True}
        elif system_id in any_dist:
            out[key] = {"jumps": any_dist[system_id], "highsec": False}
        else:
            out[key] = None
    return out


async def hub_routes(db: AsyncSession, origin: int,
                     hub_systems: dict[str, int]) -> dict[str, dict | None]:
    graph = (await sde._ensure_jump_graph(db))["graph"]
    rows = (await db.execute(select(SDESystem.system_id, SDESystem.security))).all()
    return routes_from(graph, {sid: sec for sid, sec in rows}, origin, hub_systems)
