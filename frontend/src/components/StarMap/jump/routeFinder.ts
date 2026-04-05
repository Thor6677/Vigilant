import type { SystemData } from '../types';
import { jumpDistanceLY, canLightCyno } from './distance';
import type { JumpSpatialIndex } from './spatialIndex';

export interface JumpRoutePreferences {
  preferStation: boolean;   // prefer systems with NPC stations
  preferHsGate: boolean;    // prefer lowsec systems with highsec stargate connections
}

/**
 * Find the shortest (fewest-hop) jump drive route between two systems.
 * Uses weighted BFS (Dijkstra-like) on the virtual jump-range graph.
 *
 * Rules:
 * - Origin can be any sec status (you jump OUT of it)
 * - All midpoints and destination must be cyno-capable (sec < 0.5)
 * - Each hop must be within the effective jump range
 *
 * Preferences reduce the cost of preferred systems, making the search
 * favor them without excluding non-preferred ones.
 */
export function findJumpRoute(
  origin: SystemData,
  destination: SystemData,
  systems: SystemData[],
  rangeLY: number,
  spatialIndex: JumpSpatialIndex,
  preferences?: JumpRoutePreferences,
  adjacency?: Map<number, Set<number>>,
): SystemData[] | null {
  // Direct jump?
  if (jumpDistanceLY(origin, destination) <= rangeLY && canLightCyno(destination)) {
    return [origin, destination];
  }

  if (!canLightCyno(destination)) return null;

  const prefs = preferences ?? { preferStation: true, preferHsGate: false };

  const systemMap = new Map<number, SystemData>();
  for (const sys of systems) systemMap.set(sys.id, sys);

  // Pre-compute which systems have a highsec gate neighbor
  let hsGateSystems: Set<number> | null = null;
  if (prefs.preferHsGate && adjacency) {
    hsGateSystems = new Set<number>();
    for (const [sysId, neighbors] of adjacency) {
      const sys = systemMap.get(sysId);
      if (!sys || !canLightCyno(sys)) continue;
      for (const nId of neighbors) {
        const neighbor = systemMap.get(nId);
        if (neighbor && neighbor.sec >= 0.45) {
          hsGateSystems.add(sysId);
          break;
        }
      }
    }
  }

  // Weighted BFS (priority queue via sorted insertion)
  const costs = new Map<number, number>();
  const parent = new Map<number, number>();
  costs.set(origin.id, 0);

  // Simple priority queue
  const queue: { sys: SystemData; cost: number }[] = [{ sys: origin, cost: 0 }];

  while (queue.length > 0) {
    // Get lowest cost
    queue.sort((a, b) => a.cost - b.cost);
    const { sys: current, cost: currentCost } = queue.shift()!;

    if (current.id === destination.id) {
      // Reconstruct path
      const path: SystemData[] = [];
      let cur = destination.id;
      while (cur !== origin.id) {
        path.unshift(systemMap.get(cur)!);
        cur = parent.get(cur)!;
      }
      path.unshift(origin);
      return path;
    }

    // Already found a better path?
    if (currentCost > (costs.get(current.id) ?? Infinity)) continue;

    const reachable = spatialIndex.findInRange(current, rangeLY);

    for (const next of reachable) {
      if (!canLightCyno(next) && next.id !== destination.id) continue;

      // Base cost: 1 per hop
      let hopCost = 1;

      // Preferences reduce cost for preferred systems (making them favored)
      if (prefs.preferStation && next.hasStation) hopCost *= 0.5;
      if (prefs.preferHsGate && hsGateSystems?.has(next.id)) hopCost *= 0.6;

      const newCost = currentCost + hopCost;
      if (newCost < (costs.get(next.id) ?? Infinity)) {
        costs.set(next.id, newCost);
        parent.set(next.id, current.id);
        queue.push({ sys: next, cost: newCost });
      }
    }
  }

  return null;
}
