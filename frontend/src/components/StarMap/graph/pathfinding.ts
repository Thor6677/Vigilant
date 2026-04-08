import Graph from 'graphology';
import { bidirectional as dijkstraBidirectional } from 'graphology-shortest-path/dijkstra';
import type { RoutePreference } from '../types';

/** Per-system kill counts used by the 'safest' preference. */
export type KillWeights = Map<number, number>;

/** Multiplier applied to the kill count when computing 'safest' edge weight. */
const SAFEST_KILL_MULTIPLIER = 5;

/**
 * Systems excluded from routing by default. The Glorification gates that
 * connect Zarzakh (the Deathless's home in Yasna Zakh) to Curse, Venal,
 * Metropolis, and Placid produce unrealistic shortcuts between high-sec
 * trade hubs and dangerous null-sec pockets — players don't normally
 * transit there. Excluded unless the user explicitly sets it as origin,
 * destination, or a waypoint (each routing segment computes its own
 * bypass list from its endpoints).
 */
const DEFAULT_EXCLUDED_SYSTEMS: ReadonlySet<number> = new Set([
  30100000, // Zarzakh — Yasna Zakh, sec -1
]);

/** Edge weight for default-excluded systems. Finite so a path is still
 *  found if the user's avoid list / preferences leave no alternative. */
const EXCLUDED_SYSTEM_WEIGHT = 100000;

/**
 * Apply routing preference weights to the graph edges.
 * Returns a cleanup function to restore original weights.
 */
function applyWeights(
  graph: Graph,
  preference: RoutePreference,
  avoidSystems?: Set<number>,
  killWeights?: KillWeights,
  /** Systems for which the default exclusion is bypassed (origin/dest/waypoints
   *  of the current routing segment). */
  bypassExclusion?: Set<number>,
): () => void {
  const originalWeights = new Map<string, number>();

  graph.forEachEdge((edge, _attrs, src, dst) => {
    const srcSec = graph.getNodeAttribute(src, 'sec') as number;
    const dstSec = graph.getNodeAttribute(dst, 'sec') as number;
    const minSec = Math.min(srcSec, dstSec);

    let weight = 1;

    switch (preference) {
      case 'highsec':
        // Make non-highsec edges essentially impassable so the planner
        // detours through long highsec routes (e.g. Jita→Amarr via Khanid)
        // rather than dipping through a single lowsec system. The previous
        // weights (10 for lowsec / 50 for null) were small enough that any
        // direct lowsec route still beat the highsec alternative on cost.
        if (minSec < 0.5) weight = minSec < 0 ? 5000 : 1000;
        break;
      case 'lowsec':
        if (minSec >= 0.5) weight = 100;
        break;
      case 'nullsec':
        if (minSec >= 0.5) weight = 1000;
        else if (minSec > 0) weight = 100;
        break;
      case 'safest': {
        // Mild highsec preference as a baseline (we want safety, and high
        // sec is a reasonable prior). Then add a heavy per-edge surcharge
        // proportional to the kill count of either endpoint, if known.
        if (minSec < 0.5) weight = 3;
        if (killWeights) {
          const srcId = Number(src);
          const dstId = Number(dst);
          const srcKills = killWeights.get(srcId) ?? 0;
          const dstKills = killWeights.get(dstId) ?? 0;
          weight += (srcKills + dstKills) * SAFEST_KILL_MULTIPLIER;
        }
        break;
      }
    }

    const srcId = Number(src);
    const dstId = Number(dst);

    // Heavily penalize avoided systems
    if (avoidSystems && (avoidSystems.has(srcId) || avoidSystems.has(dstId))) {
      weight = 10000;
    }

    // Default-excluded systems (e.g. Zarzakh). Bypassed when the system is
    // explicitly an endpoint of the current routing segment.
    const srcExcluded = DEFAULT_EXCLUDED_SYSTEMS.has(srcId) && !(bypassExclusion?.has(srcId));
    const dstExcluded = DEFAULT_EXCLUDED_SYSTEMS.has(dstId) && !(bypassExclusion?.has(dstId));
    if (srcExcluded || dstExcluded) {
      weight = EXCLUDED_SYSTEM_WEIGHT;
    }

    if (weight !== 1) {
      originalWeights.set(edge, graph.getEdgeAttribute(edge, 'weight') ?? 1);
      graph.setEdgeAttribute(edge, 'weight', weight);
    }
  });

  return () => {
    for (const [edge, w] of originalWeights) {
      if (graph.hasEdge(edge)) {
        graph.setEdgeAttribute(edge, 'weight', w);
      }
    }
  };
}

export function findRoute(
  graph: Graph,
  originId: number,
  destId: number,
  preference: RoutePreference = 'shortest',
  avoidSystems?: Set<number>,
  killWeights?: KillWeights,
): number[] | null {
  const srcKey = String(originId);
  const dstKey = String(destId);

  if (!graph.hasNode(srcKey) || !graph.hasNode(dstKey)) return null;

  // If origin or dest IS a default-excluded system, allow routing through it.
  const bypassExclusion = new Set<number>();
  if (DEFAULT_EXCLUDED_SYSTEMS.has(originId)) bypassExclusion.add(originId);
  if (DEFAULT_EXCLUDED_SYSTEMS.has(destId)) bypassExclusion.add(destId);

  const restore = applyWeights(graph, preference, avoidSystems, killWeights, bypassExclusion);

  try {
    // Use Dijkstra (weighted) so the per-edge weights set by applyWeights
    // actually influence the path. The unweighted bidirectional BFS that
    // used to be imported here ignored edge weights entirely, which silently
    // disabled the highsec / lowsec / nullsec preferences.
    const path = dijkstraBidirectional(graph, srcKey, dstKey, 'weight');
    if (!path) return null;
    return path.map(Number);
  } catch {
    return null;
  } finally {
    restore();
  }
}
