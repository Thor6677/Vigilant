import { useState, useRef, useMemo, useCallback } from 'react';
import type { SystemData, JumpShipClass } from './types';
import { JUMP_SHIPS } from './jump/constants';
import { effectiveRange, canLightCyno } from './jump/distance';
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

  // Lazy spatial index
  const spatialIndexRef = useRef<JumpSpatialIndex | null>(null);
  const getSpatialIndex = useCallback(() => {
    if (!spatialIndexRef.current) {
      spatialIndexRef.current = new JumpSpatialIndex(systems);
    }
    return spatialIndexRef.current;
  }, [systems]);

  // Effective range
  const range = useMemo(() => {
    return effectiveRange(JUMP_SHIPS[shipClass].baseRange, jdcLevel);
  }, [shipClass, jdcLevel]);

  // Reachable systems from origin
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

  // Calculate route
  const calculate = useCallback(() => {
    if (jumpOrigin === null || jumpDest === null) {
      setRouteError('Set both origin and destination');
      return;
    }

    const origin = systemMap.get(jumpOrigin);
    const dest = systemMap.get(jumpDest);
    if (!origin || !dest) {
      setRouteError('Invalid system');
      return;
    }

    if (!canLightCyno(dest)) {
      setRouteError('Destination is highsec — cannot light cyno');
      return;
    }

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
  };
}
