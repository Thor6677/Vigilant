import { useCallback, useEffect, useState } from 'react';
import type Graph from 'graphology';
import type { RoutePreference } from './types';
import { findRoute } from './graph/pathfinding';

export interface GateRoutePlannerState {
  /** Whether the planner panel is visible (used by Phase 1C panel toggle). */
  active: boolean;
  setActive: (v: boolean) => void;

  /** Route inputs. */
  origin: number | null;
  setOrigin: (id: number | null) => void;
  dest: number | null;
  setDest: (id: number | null) => void;
  waypoints: number[];
  setWaypoints: (ids: number[]) => void;
  addWaypoint: (id: number) => void;
  removeWaypoint: (id: number) => void;
  clearWaypoints: () => void;

  preference: RoutePreference;
  setPreference: (p: RoutePreference) => void;

  /** Avoid list (in-memory for Phase 1A; DB-backed in Phase 1B). */
  avoidSystems: Set<number>;
  addAvoid: (id: number) => void;
  removeAvoid: (id: number) => void;
  clearAvoid: () => void;

  /** Computed route — null when origin/dest not both set or no path exists. */
  activeRoute: number[] | null;

  reset: () => void;
}

/**
 * Owns all gate-route planning state and computes the active route whenever
 * inputs change. The graph is provided via a stable accessor so that the
 * hook can read the latest graph reference at compute time without forcing
 * the parent to convert its graph ref to state.
 */
export function useGateRoutePlanner(getGraph: () => Graph | null): GateRoutePlannerState {
  const [active, setActive] = useState(false);
  const [origin, setOrigin] = useState<number | null>(null);
  const [dest, setDest] = useState<number | null>(null);
  const [waypoints, setWaypoints] = useState<number[]>([]);
  const [preference, setPreference] = useState<RoutePreference>('shortest');
  const [avoidSystems, setAvoidSystems] = useState<Set<number>>(new Set());
  const [activeRoute, setActiveRoute] = useState<number[] | null>(null);

  // Recompute the active route whenever inputs change.
  useEffect(() => {
    const graph = getGraph();
    if (!graph || origin === null || dest === null) {
      setActiveRoute(null);
      return;
    }

    // Chain pathfinding through any intermediate waypoints.
    const stops = [origin, ...waypoints, dest];
    const fullPath: number[] = [];
    for (let i = 0; i < stops.length - 1; i++) {
      const seg = findRoute(graph, stops[i], stops[i + 1], preference, avoidSystems);
      if (!seg) {
        setActiveRoute(null);
        return;
      }
      if (i === 0) {
        fullPath.push(...seg);
      } else {
        // Skip the first node of each subsequent segment to avoid duplicating
        // the join point with the previous segment's tail.
        fullPath.push(...seg.slice(1));
      }
    }
    setActiveRoute(fullPath);
  }, [origin, dest, waypoints, preference, avoidSystems, getGraph]);

  const addWaypoint = useCallback((id: number) => {
    setWaypoints(prev => (prev.includes(id) ? prev : [...prev, id]));
  }, []);

  const removeWaypoint = useCallback((id: number) => {
    setWaypoints(prev => prev.filter(w => w !== id));
  }, []);

  const clearWaypoints = useCallback(() => setWaypoints([]), []);

  const addAvoid = useCallback((id: number) => {
    setAvoidSystems(prev => {
      if (prev.has(id)) return prev;
      const next = new Set(prev);
      next.add(id);
      return next;
    });
  }, []);

  const removeAvoid = useCallback((id: number) => {
    setAvoidSystems(prev => {
      if (!prev.has(id)) return prev;
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
  }, []);

  const clearAvoid = useCallback(() => setAvoidSystems(new Set()), []);

  const reset = useCallback(() => {
    setOrigin(null);
    setDest(null);
    setWaypoints([]);
    setActiveRoute(null);
  }, []);

  return {
    active, setActive,
    origin, setOrigin,
    dest, setDest,
    waypoints, setWaypoints, addWaypoint, removeWaypoint, clearWaypoints,
    preference, setPreference,
    avoidSystems, addAvoid, removeAvoid, clearAvoid,
    activeRoute,
    reset,
  };
}
