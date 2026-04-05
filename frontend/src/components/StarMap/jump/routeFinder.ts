import type { SystemData } from '../types';
import { jumpDistanceLY, canLightCyno } from './distance';
import type { JumpSpatialIndex } from './spatialIndex';

/**
 * Find the shortest (fewest-hop) jump drive route between two systems.
 * Uses BFS on the virtual jump-range graph with spatial indexing.
 *
 * Rules:
 * - Origin can be any sec status (you jump OUT of it)
 * - All midpoints and destination must be cyno-capable (sec < 0.5)
 * - Each hop must be within the effective jump range
 */
export function findJumpRoute(
  origin: SystemData,
  destination: SystemData,
  systems: SystemData[],
  rangeLY: number,
  spatialIndex: JumpSpatialIndex,
): SystemData[] | null {
  // Direct jump?
  if (jumpDistanceLY(origin, destination) <= rangeLY && canLightCyno(destination)) {
    return [origin, destination];
  }

  // Destination must be cyno-capable
  if (!canLightCyno(destination)) return null;

  // BFS
  const visited = new Set<number>();
  const parent = new Map<number, number>(); // system ID → parent system ID
  const queue: SystemData[] = [origin];
  visited.add(origin.id);

  const systemMap = new Map<number, SystemData>();
  for (const sys of systems) systemMap.set(sys.id, sys);

  while (queue.length > 0) {
    const current = queue.shift()!;

    // Find all systems within jump range
    const reachable = spatialIndex.findInRange(current, rangeLY);

    for (const next of reachable) {
      if (visited.has(next.id)) continue;

      // Midpoints must be cyno-capable (can jump TO them)
      if (!canLightCyno(next) && next.id !== destination.id) continue;

      visited.add(next.id);
      parent.set(next.id, current.id);

      // Found destination?
      if (next.id === destination.id) {
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

      queue.push(next);
    }
  }

  return null; // No route found
}
