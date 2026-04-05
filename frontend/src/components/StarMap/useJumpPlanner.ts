import { useState, useRef, useMemo, useCallback } from 'react';
import type { SystemData, JumpShipClass } from './types';
import { JUMP_SHIPS } from './jump/constants';
import { effectiveRange, canLightCyno, jumpDistanceLY } from './jump/distance';
import { JumpSpatialIndex } from './jump/spatialIndex';
import { findJumpRoute } from './jump/routeFinder';
import { calculateJumpRoute } from './jump/calculator';
import type { JumpWaypoint } from './jump/calculator';

export interface JumpPlannerState {
  active: boolean;
  setActive: (v: boolean) => void;
  shipClass: JumpShipClass;
  setShipClass: (v: JumpShipClass) => void;
  jdcLevel: number;
  setJdcLevel: (v: number) => void;
  jfcLevel: number;
  setJfcLevel: (v: number) => void;
  jumpOrigin: number | null;
  setJumpOrigin: (id: number | null) => void;
  jumpDest: number | null;
  setJumpDest: (id: number | null) => void;
  range: number;
  reachableSystems: SystemData[];
  reachableIds: Set<number>;
  jumpRoute: JumpWaypoint[] | null;
  routeError: string | null;
  calculate: () => void;
  /** Replace a midpoint at route index with a different system, then recalculate fatigue/fuel. */
  replaceMidpoint: (index: number, newSystemId: number) => void;
  /** Remove a midpoint and recalculate. */
  removeMidpoint: (index: number) => void;
  /** Get alternative systems reachable from a specific waypoint (for midpoint swapping). */
  getAlternatives: (fromIndex: number) => SystemData[];
  /** Insert a new midpoint between two existing hops. */
  insertMidpoint: (afterIndex: number, systemId: number) => void;
  /** Reset the planner to initial state. */
  reset: () => void;
}

export function useJumpPlanner(
  systemMap: Map<number, SystemData>,
  systems: SystemData[],
): JumpPlannerState {
  const [active, setActive] = useState(false);
  const [shipClass, setShipClass] = useState<JumpShipClass>('carrier');
  const [jdcLevel, setJdcLevel] = useState(5);
  const [jfcLevel, setJfcLevel] = useState(4);
  const [jumpOrigin, setJumpOrigin] = useState<number | null>(null);
  const [jumpDest, setJumpDest] = useState<number | null>(null);
  const [jumpRoute, setJumpRoute] = useState<JumpWaypoint[] | null>(null);
  const [routeError, setRouteError] = useState<string | null>(null);

  const spatialIndexRef = useRef<JumpSpatialIndex | null>(null);
  const getSpatialIndex = useCallback(() => {
    if (!spatialIndexRef.current) {
      spatialIndexRef.current = new JumpSpatialIndex(systems);
    }
    return spatialIndexRef.current;
  }, [systems]);

  const range = useMemo(() => {
    return effectiveRange(JUMP_SHIPS[shipClass].baseRange, jdcLevel);
  }, [shipClass, jdcLevel]);

  const { reachableSystems, reachableIds } = useMemo(() => {
    if (!active || jumpOrigin === null) {
      return { reachableSystems: [] as SystemData[], reachableIds: new Set<number>() };
    }
    const origin = systemMap.get(jumpOrigin);
    if (!origin) return { reachableSystems: [] as SystemData[], reachableIds: new Set<number>() };
    const index = getSpatialIndex();
    const reachable = index.findInRange(origin, range).filter(canLightCyno);
    const ids = new Set(reachable.map(s => s.id));
    return { reachableSystems: reachable, reachableIds: ids };
  }, [active, jumpOrigin, range, systemMap, getSpatialIndex]);

  const calculate = useCallback(() => {
    if (jumpOrigin === null || jumpDest === null) {
      setRouteError('Set both origin and destination');
      return;
    }
    const origin = systemMap.get(jumpOrigin);
    const dest = systemMap.get(jumpDest);
    if (!origin || !dest) { setRouteError('Invalid system'); return; }
    if (!canLightCyno(dest)) { setRouteError('Destination is highsec — cannot light cyno'); return; }

    const index = getSpatialIndex();
    const route = findJumpRoute(origin, dest, systems, range, index);
    if (!route) {
      setRouteError(`No route found within ${range.toFixed(1)} LY range`);
      setJumpRoute(null);
      return;
    }
    const waypoints = calculateJumpRoute(route, shipClass, jdcLevel, jfcLevel);
    setJumpRoute(waypoints);
    setRouteError(null);
  }, [jumpOrigin, jumpDest, range, shipClass, jdcLevel, jfcLevel, systems, systemMap, getSpatialIndex]);

  /** Replace a midpoint and recalculate fatigue/fuel for the modified route. */
  const replaceMidpoint = useCallback((index: number, newSystemId: number) => {
    if (!jumpRoute || index <= 0 || index >= jumpRoute.length - 1) return;
    const newSys = systemMap.get(newSystemId);
    if (!newSys) return;

    // Validate: new system must be within range of both prev and next hop
    const prev = jumpRoute[index - 1].system;
    const next = jumpRoute[index + 1].system;
    if (jumpDistanceLY(prev, newSys) > range || jumpDistanceLY(newSys, next) > range) {
      setRouteError(`${newSys.name} is out of range from adjacent hops`);
      return;
    }

    // Build new route array
    const newRoute = jumpRoute.map(wp => wp.system);
    newRoute[index] = newSys;

    // Recalculate fatigue/fuel
    const waypoints = calculateJumpRoute(newRoute, shipClass, jdcLevel, jfcLevel);
    setJumpRoute(waypoints);
    setRouteError(null);
  }, [jumpRoute, range, shipClass, jdcLevel, jfcLevel, systemMap]);

  /** Remove a midpoint if the adjacent hops can still reach each other. */
  const removeMidpoint = useCallback((index: number) => {
    if (!jumpRoute || index <= 0 || index >= jumpRoute.length - 1) return;

    const prev = jumpRoute[index - 1].system;
    const next = jumpRoute[index + 1].system;
    if (jumpDistanceLY(prev, next) > range) {
      setRouteError(`Cannot remove — ${prev.name} to ${next.name} exceeds ${range.toFixed(1)} LY range`);
      return;
    }

    const newRoute = jumpRoute.filter((_, i) => i !== index).map(wp => wp.system);
    const waypoints = calculateJumpRoute(newRoute, shipClass, jdcLevel, jfcLevel);
    setJumpRoute(waypoints);
    setRouteError(null);
  }, [jumpRoute, range, shipClass, jdcLevel, jfcLevel]);

  /** Get cyno-capable systems reachable from the previous waypoint AND that can reach the next waypoint. */
  const getAlternatives = useCallback((fromIndex: number): SystemData[] => {
    if (!jumpRoute || fromIndex <= 0 || fromIndex >= jumpRoute.length - 1) return [];

    const prev = jumpRoute[fromIndex - 1].system;
    const next = jumpRoute[fromIndex + 1].system;
    const index = getSpatialIndex();

    // Systems reachable from the previous hop
    const fromPrev = index.findInRange(prev, range).filter(canLightCyno);

    // Filter to those that can also reach the next hop
    return fromPrev.filter(sys =>
      sys.id !== prev.id &&
      sys.id !== next.id &&
      jumpDistanceLY(sys, next) <= range
    );
  }, [jumpRoute, range, getSpatialIndex]);

  /** Insert a new midpoint between hops at afterIndex and afterIndex+1. */
  const insertMidpoint = useCallback((afterIndex: number, systemId: number) => {
    if (!jumpRoute || afterIndex < 0 || afterIndex >= jumpRoute.length - 1) return;
    const newSys = systemMap.get(systemId);
    if (!newSys) return;

    const prev = jumpRoute[afterIndex].system;
    const next = jumpRoute[afterIndex + 1].system;
    if (jumpDistanceLY(prev, newSys) > range || jumpDistanceLY(newSys, next) > range) {
      setRouteError(`${newSys.name} is out of range from adjacent hops`);
      return;
    }

    const newRoute = jumpRoute.map(wp => wp.system);
    newRoute.splice(afterIndex + 1, 0, newSys);
    const waypoints = calculateJumpRoute(newRoute, shipClass, jdcLevel, jfcLevel);
    setJumpRoute(waypoints);
    setRouteError(null);
  }, [jumpRoute, range, shipClass, jdcLevel, jfcLevel, systemMap]);

  const reset = useCallback(() => {
    setJumpOrigin(null);
    setJumpDest(null);
    setJumpRoute(null);
    setRouteError(null);
  }, []);

  return {
    active, setActive,
    shipClass, setShipClass,
    jdcLevel, setJdcLevel,
    jfcLevel, setJfcLevel,
    jumpOrigin, setJumpOrigin,
    jumpDest, setJumpDest,
    range,
    reachableSystems,
    reachableIds,
    jumpRoute,
    routeError,
    calculate,
    replaceMidpoint,
    removeMidpoint,
    getAlternatives,
    insertMidpoint,
    reset,
  };
}
