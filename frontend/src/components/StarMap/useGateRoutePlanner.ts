import { useCallback, useEffect, useMemo, useState } from 'react';
import type Graph from 'graphology';
import type { RoutePreference } from './types';
import { findRoute } from './graph/pathfinding';
import type { TheraConnection } from './useOverlayData';

export interface AvoidEntry {
  id: number;
  kind: 'system' | 'constellation' | 'region';
  entity_id: number;
}

export interface SavedRoute {
  id: number;
  name: string;
  origin_system_id: number;
  dest_system_id: number;
  waypoints: number[];
  preference: RoutePreference;
  avoid: number[];
  share_token: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export type ThreatLevel = 'safe' | 'caution' | 'dangerous' | 'smartbomb';

export interface HopKill {
  killmail_id: number;
  time_str: string;
  victim_ship: string;
  victim_ship_id: number;
  victim_char_id: number | null;
  attacker_count: number;
  attacker_ships: { name: string; type_id: number; count: number }[];
  attacker_weapons: Record<string, number>;
  value: number;
  value_str: string;
  is_npc: boolean;
}

export interface HopIntel {
  kills: number;
  pvp_kills: number;
  threat: ThreatLevel;
  has_smartbombs: boolean;
  has_dictors: boolean;
  has_hics: boolean;
  total_value: number;
  total_value_str: string;
  top_kills: HopKill[];
}

export interface GateRoutePlannerState {
  /** Whether the planner panel is visible. */
  active: boolean;
  setActive: (v: boolean) => void;

  /** Route inputs. */
  origin: number | null;
  setOrigin: (id: number | null) => void;
  dest: number | null;
  setDest: (id: number | null) => void;
  swapEndpoints: () => void;

  waypoints: number[];
  addWaypoint: (id: number) => void;
  removeWaypoint: (id: number) => void;
  moveWaypoint: (index: number, direction: 'up' | 'down') => void;
  reorderWaypoint: (fromIndex: number, toIndex: number) => void;
  insertWaypointAt: (index: number, systemId: number) => void;
  clearWaypoints: () => void;

  preference: RoutePreference;
  setPreference: (p: RoutePreference) => void;

  /** DB-backed avoid list. avoidSystems is derived from entries with kind='system'. */
  avoidEntries: AvoidEntry[];
  avoidSystems: Set<number>;
  addAvoid: (systemId: number) => Promise<void>;
  removeAvoid: (entryId: number) => Promise<void>;
  clearAvoid: () => Promise<void>;
  reloadAvoid: () => Promise<void>;

  /** DB-backed saved routes. */
  savedRoutes: SavedRoute[];
  saveCurrentRoute: (name: string) => Promise<SavedRoute | null>;
  deleteSavedRoute: (id: number) => Promise<void>;
  loadSavedRoute: (id: number) => void;
  toggleShareSavedRoute: (id: number) => Promise<SavedRoute | null>;
  reloadSavedRoutes: () => Promise<void>;

  /** Load a route directly from a saved-route payload (e.g. from a share link). */
  loadRouteData: (data: {
    origin_system_id: number;
    dest_system_id: number;
    waypoints: number[];
    preference: RoutePreference;
  }) => void;

  /** Computed route — null when origin/dest not both set or no path exists. */
  activeRoute: number[] | null;

  /** Which hops of activeRoute are Thera/Turnur wormhole legs rather than
   *  stargate jumps, keyed by the *destination* system id of that hop —
   *  e.g. activeRouteVia.get(activeRoute[i]) === 'Thera' means the jump
   *  INTO activeRoute[i] used the Thera hole. Only populated when useThera
   *  is on and the computed route actually used a spliced-in edge. */
  activeRouteVia: Map<number, string>;

  /** Per-hop intel from /api/map/route-safety, keyed by system_id. */
  hopIntel: Map<number, HopIntel>;
  hopIntelLoading: boolean;

  /** Active character — used by auto-trim and the Set Destination button. */
  activeCharacterId: number | null;
  setActiveCharacterId: (id: number | null) => void;
  followCharacter: boolean;
  setFollowCharacter: (v: boolean) => void;

  /** Push the route's destination + waypoints to the active character's
   *  in-game autopilot. Returns null on success, an error message on failure. */
  pushRouteToAutopilot: () => Promise<string | null>;
  /** Push a single system as the next waypoint (additive). */
  pushWaypointToAutopilot: (systemId: number) => Promise<string | null>;

  /** Last error from a route compute or API call (for UI display). */
  errorMessage: string | null;
  clearError: () => void;

  /** Whether Thera/Turnur wormhole connections should be treated as edges
   *  in the routing graph. Default false — wormhole chains are ephemeral. */
  useThera: boolean;
  setUseThera: (v: boolean) => void;

  reset: () => void;
}

/**
 * Owns all gate-route planning state and computes the active route whenever
 * inputs change. The graph is provided via a stable accessor so the hook
 * can read the latest graph reference at compute time without forcing the
 * parent to convert its graph ref to state.
 */
export function useGateRoutePlanner(
  getGraph: () => Graph | null,
  getTheraConnections?: () => TheraConnection[] | null,
): GateRoutePlannerState {
  const [active, setActive] = useState(false);
  const [origin, setOrigin] = useState<number | null>(null);
  const [dest, setDest] = useState<number | null>(null);
  const [waypoints, setWaypoints] = useState<number[]>([]);
  const [preference, setPreference] = useState<RoutePreference>('shortest');
  const [avoidEntries, setAvoidEntries] = useState<AvoidEntry[]>([]);
  const [savedRoutes, setSavedRoutes] = useState<SavedRoute[]>([]);
  const [activeRoute, setActiveRoute] = useState<number[] | null>(null);
  const [activeRouteVia, setActiveRouteVia] = useState<Map<number, string>>(() => new Map());
  const [hopIntel, setHopIntel] = useState<Map<number, HopIntel>>(() => new Map());
  const [hopIntelLoading, setHopIntelLoading] = useState(false);
  const [activeCharacterId, setActiveCharacterId] = useState<number | null>(null);
  const [followCharacter, setFollowCharacter] = useState(true);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [useThera, setUseThera] = useState(false);

  // Derived: only system-kind entries contribute to routing avoidance for now.
  // Constellation/region expansion will come later (needs SDE lookup).
  const avoidSystems = useMemo(() => {
    const set = new Set<number>();
    for (const entry of avoidEntries) {
      if (entry.kind === 'system') set.add(entry.entity_id);
    }
    return set;
  }, [avoidEntries]);

  // ── Avoid list API ───────────────────────────────────────────────────────

  const reloadAvoid = useCallback(async () => {
    try {
      const resp = await fetch('/api/map/avoid');
      if (!resp.ok) return;
      const data: AvoidEntry[] = await resp.json();
      setAvoidEntries(data);
    } catch {
      // Silent — avoid list is non-critical to map rendering
    }
  }, []);

  const addAvoid = useCallback(async (systemId: number) => {
    // Optimistic insertion with a temporary negative id, replaced on response.
    const tempId = -Date.now();
    const optimistic: AvoidEntry = { id: tempId, kind: 'system', entity_id: systemId };
    setAvoidEntries(prev => {
      if (prev.some(e => e.kind === 'system' && e.entity_id === systemId)) return prev;
      return [...prev, optimistic];
    });
    try {
      const resp = await fetch('/api/map/avoid', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind: 'system', entity_id: systemId }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const saved: AvoidEntry = await resp.json();
      // Replace the temp entry with the persisted one
      setAvoidEntries(prev => prev.map(e => (e.id === tempId ? saved : e)));
    } catch (e) {
      // Roll back the optimistic insert
      setAvoidEntries(prev => prev.filter(e => e.id !== tempId));
      setErrorMessage(`Could not save avoid entry: ${String(e)}`);
    }
  }, []);

  const removeAvoid = useCallback(async (entryId: number) => {
    let removed: AvoidEntry | undefined;
    setAvoidEntries(prev => {
      removed = prev.find(e => e.id === entryId);
      return prev.filter(e => e.id !== entryId);
    });
    try {
      const resp = await fetch(`/api/map/avoid/${entryId}`, { method: 'DELETE' });
      if (!resp.ok && resp.status !== 404) throw new Error(`HTTP ${resp.status}`);
    } catch (e) {
      // Roll back
      if (removed) setAvoidEntries(prev => [...prev, removed!]);
      setErrorMessage(`Could not remove avoid entry: ${String(e)}`);
    }
  }, []);

  const clearAvoid = useCallback(async () => {
    const previous = avoidEntries;
    setAvoidEntries([]);
    try {
      await Promise.all(previous.map(e =>
        fetch(`/api/map/avoid/${e.id}`, { method: 'DELETE' })
      ));
    } catch (e) {
      setAvoidEntries(previous);
      setErrorMessage(`Could not clear avoid list: ${String(e)}`);
    }
  }, [avoidEntries]);

  // ── Saved routes API ─────────────────────────────────────────────────────

  const reloadSavedRoutes = useCallback(async () => {
    try {
      const resp = await fetch('/api/map/routes');
      if (!resp.ok) return;
      const data: SavedRoute[] = await resp.json();
      setSavedRoutes(data);
    } catch {
      // Silent
    }
  }, []);

  const saveCurrentRoute = useCallback(async (name: string): Promise<SavedRoute | null> => {
    if (origin === null || dest === null) {
      setErrorMessage('Set origin and destination before saving');
      return null;
    }
    try {
      const resp = await fetch('/api/map/routes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name,
          origin_system_id: origin,
          dest_system_id: dest,
          waypoints,
          preference,
          avoid: Array.from(avoidSystems),
        }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.error || `HTTP ${resp.status}`);
      }
      const saved: SavedRoute = await resp.json();
      setSavedRoutes(prev => [saved, ...prev]);
      return saved;
    } catch (e) {
      setErrorMessage(`Could not save route: ${String(e)}`);
      return null;
    }
  }, [origin, dest, waypoints, preference, avoidSystems]);

  const deleteSavedRoute = useCallback(async (id: number) => {
    let removed: SavedRoute | undefined;
    setSavedRoutes(prev => {
      removed = prev.find(r => r.id === id);
      return prev.filter(r => r.id !== id);
    });
    try {
      const resp = await fetch(`/api/map/routes/${id}`, { method: 'DELETE' });
      if (!resp.ok && resp.status !== 404) throw new Error(`HTTP ${resp.status}`);
    } catch (e) {
      if (removed) setSavedRoutes(prev => [...prev, removed!]);
      setErrorMessage(`Could not delete saved route: ${String(e)}`);
    }
  }, []);

  const loadSavedRoute = useCallback((id: number) => {
    const route = savedRoutes.find(r => r.id === id);
    if (!route) return;
    setOrigin(route.origin_system_id);
    setDest(route.dest_system_id);
    setWaypoints(route.waypoints);
    setPreference(route.preference);
    // Note: we do NOT overwrite the user's current avoid list with the
    // saved route's avoid snapshot — the avoid list is per-user, not per-route.
    setErrorMessage(null);
  }, [savedRoutes]);

  const toggleShareSavedRoute = useCallback(async (id: number): Promise<SavedRoute | null> => {
    try {
      const resp = await fetch(`/api/map/routes/${id}/share`, { method: 'POST' });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const updated: SavedRoute = await resp.json();
      setSavedRoutes(prev => prev.map(r => (r.id === id ? updated : r)));
      return updated;
    } catch (e) {
      setErrorMessage(`Could not toggle sharing: ${String(e)}`);
      return null;
    }
  }, []);

  // ── Initial hydration on mount ───────────────────────────────────────────

  useEffect(() => {
    reloadAvoid();
    reloadSavedRoutes();
  }, [reloadAvoid, reloadSavedRoutes]);

  // ── Auto-fetch per-hop intel whenever the active route changes ──────────
  //
  // The dep is the comma-joined system-id string (NOT the array reference)
  // so that re-computes producing an identical set of hops don't refetch.
  // Otherwise: route compute → setActiveRoute(newArray) → fetch intel →
  // setHopIntel → killWeights changes → compute re-runs → setActiveRoute
  // (new array, same content) → fetch intel → … infinite loop.
  const routeKey = activeRoute ? activeRoute.join(',') : '';

  useEffect(() => {
    if (!activeRoute || activeRoute.length < 2) {
      setHopIntel(prev => (prev.size === 0 ? prev : new Map()));
      setHopIntelLoading(false);
      return;
    }

    let cancelled = false;
    setHopIntelLoading(true);

    const systemIds = activeRoute;
    fetch('/api/map/route-safety', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ system_ids: systemIds }),
    })
      .then(resp => {
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        return resp.json();
      })
      .then((data: Record<string, HopIntel>) => {
        if (cancelled) return;
        // MERGE new intel into existing rather than replacing. This prevents
        // the "safest" oscillation: route A → intel for A → route B (avoids A)
        // → intel for B (loses A's data) → route A again. By accumulating,
        // the router eventually sees danger on BOTH paths and converges.
        setHopIntel(prev => {
          const merged = new Map(prev);
          for (const [sidStr, intel] of Object.entries(data)) {
            merged.set(Number(sidStr), intel);
          }
          return merged;
        });
        setHopIntelLoading(false);
      })
      .catch(() => {
        if (cancelled) return;
        // Silent — intel is non-critical
        setHopIntelLoading(false);
      });

    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routeKey]);

  // Build per-system kill weights for the 'safest' preference from the
  // accumulated hopIntel. Uses a string key for stability: the compute
  // effect only re-runs when the actual PvP values change, not on every
  // merge that adds 0-kill systems.
  const killWeightsKey = useMemo(() => {
    const parts: string[] = [];
    for (const [sid, intel] of hopIntel) {
      if (intel.pvp_kills > 0) parts.push(`${sid}:${intel.pvp_kills}`);
    }
    return parts.sort().join(',');
  }, [hopIntel]);

  const stableKillWeights = useMemo(() => {
    const w = new Map<number, number>();
    for (const [sid, intel] of hopIntel) {
      if (intel.pvp_kills > 0) w.set(sid, intel.pvp_kills);
    }
    return w;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [killWeightsKey]);

  // ── Route compute ────────────────────────────────────────────────────────

  useEffect(() => {
    const graph = getGraph();
    if (!graph || origin === null || dest === null) {
      setActiveRoute(prev => (prev === null ? prev : null));
      setActiveRouteVia(prev => (prev.size === 0 ? prev : new Map()));
      return;
    }

    // Optionally splice Thera/Turnur wormhole edges into the graph for this
    // compute only. Restored in `finally` so the gate-edge graph stays clean.
    // Also index src/dst -> anchor label so we can tell pathfinding's flat
    // system-id list which hops were wormhole legs (for "via Thera" labels).
    const theraEdgeKeys: string[] = [];
    const theraViaByPair = new Map<string, string>();
    const theraConns = useThera && getTheraConnections ? getTheraConnections() : null;
    if (theraConns && theraConns.length) {
      for (const c of theraConns) {
        // Only add edges where both endpoints are in the K-space graph
        const a = String(c.src);
        const b = String(c.dst);
        if (!graph.hasNode(a) || !graph.hasNode(b)) continue;
        if (c.via) {
          const pairKey = c.src < c.dst ? `${c.src}-${c.dst}` : `${c.dst}-${c.src}`;
          theraViaByPair.set(pairKey, c.via);
        }
        if (graph.hasEdge(a, b)) continue;
        const key = graph.addEdge(a, b, { weight: 1 });
        theraEdgeKeys.push(key);
      }
    }

    // Chain pathfinding through any intermediate waypoints.
    const stops = [origin, ...waypoints, dest];
    const fullPath: number[] = [];
    const cleanup = () => {
      for (const k of theraEdgeKeys) {
        if (graph.hasEdge(k)) graph.dropEdge(k);
      }
    };
    for (let i = 0; i < stops.length - 1; i++) {
      const seg = findRoute(
        graph, stops[i], stops[i + 1], preference, avoidSystems, stableKillWeights,
      );
      if (!seg) {
        cleanup();
        setActiveRoute(null);
        setActiveRouteVia(new Map());
        setErrorMessage(`No path between system ${stops[i]} and ${stops[i + 1]}`);
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
    cleanup();

    // Label any hop of the final path that matches a spliced Thera/Turnur
    // edge, keyed by the hop's destination system id.
    const viaMap = new Map<number, string>();
    if (theraViaByPair.size > 0) {
      for (let i = 0; i < fullPath.length - 1; i++) {
        const a = fullPath[i];
        const b = fullPath[i + 1];
        const pairKey = a < b ? `${a}-${b}` : `${b}-${a}`;
        const via = theraViaByPair.get(pairKey);
        if (via) viaMap.set(b, via);
      }
    }
    setActiveRouteVia(viaMap);

    // Bail out of state updates if the new path is identical to the previous
    // one (same systems in the same order). This prevents the infinite loop:
    // compute → setActiveRoute(newArr) → intel re-fetch → killWeights changes
    // → compute again → setActiveRoute(newArr) → ...
    setActiveRoute(prev => {
      if (prev && prev.length === fullPath.length) {
        let same = true;
        for (let i = 0; i < prev.length; i++) {
          if (prev[i] !== fullPath[i]) { same = false; break; }
        }
        if (same) return prev;
      }
      return fullPath;
    });
    setErrorMessage(null);
  }, [origin, dest, waypoints, preference, avoidSystems, stableKillWeights, getGraph, useThera, getTheraConnections]);

  // ── Waypoint helpers ─────────────────────────────────────────────────────

  const addWaypoint = useCallback((id: number) => {
    setWaypoints(prev => (prev.includes(id) ? prev : [...prev, id]));
  }, []);

  const removeWaypoint = useCallback((id: number) => {
    setWaypoints(prev => prev.filter(w => w !== id));
  }, []);

  const moveWaypoint = useCallback((index: number, direction: 'up' | 'down') => {
    setWaypoints(prev => {
      if (index < 0 || index >= prev.length) return prev;
      const target = direction === 'up' ? index - 1 : index + 1;
      if (target < 0 || target >= prev.length) return prev;
      const next = [...prev];
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  }, []);

  const reorderWaypoint = useCallback((fromIndex: number, toIndex: number) => {
    setWaypoints(prev => {
      if (
        fromIndex < 0 || fromIndex >= prev.length ||
        toIndex < 0 || toIndex >= prev.length ||
        fromIndex === toIndex
      ) return prev;
      const next = [...prev];
      const [moved] = next.splice(fromIndex, 1);
      next.splice(toIndex, 0, moved);
      return next;
    });
  }, []);

  const insertWaypointAt = useCallback((index: number, systemId: number) => {
    setWaypoints(prev => {
      if (prev.includes(systemId)) return prev;
      const clamped = Math.max(0, Math.min(index, prev.length));
      const next = [...prev];
      next.splice(clamped, 0, systemId);
      return next;
    });
  }, []);

  const clearWaypoints = useCallback(() => setWaypoints([]), []);

  const loadRouteData = useCallback((data: {
    origin_system_id: number;
    dest_system_id: number;
    waypoints: number[];
    preference: RoutePreference;
  }) => {
    setOrigin(data.origin_system_id);
    setDest(data.dest_system_id);
    setWaypoints(data.waypoints);
    setPreference(data.preference);
    setErrorMessage(null);
  }, []);

  const swapEndpoints = useCallback(() => {
    setOrigin(dest);
    setDest(origin);
  }, [origin, dest]);

  // ── ESI autopilot push ───────────────────────────────────────────────────

  const pushRouteToAutopilot = useCallback(async (): Promise<string | null> => {
    if (activeCharacterId === null) {
      return 'Pick a character first.';
    }
    if (origin === null || dest === null) {
      return 'Set origin and destination first.';
    }
    const stops = [origin, ...waypoints, dest];
    try {
      // First call: clear existing route, set the FINAL destination as the
      // primary destination. Then add each intermediate stop as a waypoint.
      // Note: ESI's add_to_beginning lets us insert before the destination,
      // so we add waypoints in reverse from dest backwards to maintain order.
      const finalDest = stops[stops.length - 1];
      const intermediateStops = stops.slice(0, stops.length - 1);

      let resp = await fetch(`/api/character/${activeCharacterId}/autopilot/waypoint`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          system_id: finalDest,
          clear: true,
          add_to_beginning: false,
        }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        return err.message || err.error || `HTTP ${resp.status}`;
      }
      // Add intermediate stops in reverse order with add_to_beginning so they
      // queue up in the right sequence between origin and dest in-game
      for (let i = intermediateStops.length - 1; i >= 0; i--) {
        resp = await fetch(`/api/character/${activeCharacterId}/autopilot/waypoint`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            system_id: intermediateStops[i],
            clear: false,
            add_to_beginning: true,
          }),
        });
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          return err.message || err.error || `HTTP ${resp.status}`;
        }
      }
      return null;
    } catch (e) {
      return String(e);
    }
  }, [activeCharacterId, origin, dest, waypoints]);

  const pushWaypointToAutopilot = useCallback(async (systemId: number): Promise<string | null> => {
    if (activeCharacterId === null) {
      return 'Pick a character first.';
    }
    try {
      const resp = await fetch(`/api/character/${activeCharacterId}/autopilot/waypoint`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          system_id: systemId,
          clear: false,
          add_to_beginning: false,
        }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        return err.message || err.error || `HTTP ${resp.status}`;
      }
      return null;
    } catch (e) {
      return String(e);
    }
  }, [activeCharacterId]);

  const reset = useCallback(() => {
    setOrigin(null);
    setDest(null);
    setWaypoints([]);
    setActiveRoute(null);
    setActiveRouteVia(new Map());
    setErrorMessage(null);
  }, []);

  const clearError = useCallback(() => setErrorMessage(null), []);

  return {
    active, setActive,
    origin, setOrigin,
    dest, setDest,
    swapEndpoints,
    waypoints, addWaypoint, removeWaypoint, moveWaypoint, reorderWaypoint, insertWaypointAt, clearWaypoints,
    preference, setPreference,
    avoidEntries, avoidSystems, addAvoid, removeAvoid, clearAvoid, reloadAvoid,
    savedRoutes, saveCurrentRoute, deleteSavedRoute, loadSavedRoute, toggleShareSavedRoute, reloadSavedRoutes,
    loadRouteData,
    activeRoute,
    activeRouteVia,
    hopIntel, hopIntelLoading,
    activeCharacterId, setActiveCharacterId,
    followCharacter, setFollowCharacter,
    pushRouteToAutopilot, pushWaypointToAutopilot,
    errorMessage, clearError,
    useThera, setUseThera,
    reset,
  };
}
