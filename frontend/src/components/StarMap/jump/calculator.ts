import type { SystemData, JumpShipClass } from '../types';
import { jumpDistanceLY, fuelCost } from './distance';
import { JUMP_SHIPS, MAX_FATIGUE, MAX_COOLDOWN } from './constants';

export interface JumpWaypoint {
  system: SystemData;
  distanceLY: number;       // distance from previous hop (0 for origin)
  fuelThisHop: number;      // isotopes for this jump
  cumulativeFuel: number;   // total fuel so far
  orangeTimer: number;      // cooldown minutes after this jump
  blueFatigue: number;      // fatigue minutes after this jump
  waitMinutes: number;      // mandatory wait before next jump
  cumulativeMinutes: number; // total travel time including waits
}

/**
 * Calculate fuel consumption and fatigue for a complete jump route.
 */
export function calculateJumpRoute(
  route: SystemData[],
  shipClass: JumpShipClass,
  _jdcLevel: number,
  jfcLevel: number,
): JumpWaypoint[] {
  const config = JUMP_SHIPS[shipClass];
  const waypoints: JumpWaypoint[] = [];
  let blueFatigue = 0;
  let totalFuel = 0;
  let totalTime = 0;

  for (let i = 0; i < route.length; i++) {
    if (i === 0) {
      // Origin — no jump, no fatigue
      waypoints.push({
        system: route[0],
        distanceLY: 0,
        fuelThisHop: 0,
        cumulativeFuel: 0,
        orangeTimer: 0,
        blueFatigue: 0,
        waitMinutes: 0,
        cumulativeMinutes: 0,
      });
      continue;
    }

    const prev = route[i - 1];
    const curr = route[i];
    const dist = jumpDistanceLY(prev, curr);

    // Effective distance for fatigue (reduced for JF/Rorqual/BlackOps)
    const effectiveDist = dist * (1 - config.fatigueReduction);

    // Orange timer (cooldown): must wait before this jump
    const cooldown = Math.min(MAX_COOLDOWN, Math.max(blueFatigue / 10, 1 + effectiveDist));
    const waitMin = i === 1 ? 0 : cooldown; // No wait for first jump

    // During wait, fatigue decays 1:1
    blueFatigue = Math.max(0, blueFatigue - waitMin);
    totalTime += waitMin;

    // Fuel for this hop
    const fuel = fuelCost(dist, config.baseFuelPerLY, jfcLevel);
    totalFuel += fuel;

    // Update blue fatigue after jump
    blueFatigue = Math.min(MAX_FATIGUE, Math.max(blueFatigue, 10) * (1 + effectiveDist));

    // New orange timer after this jump
    const orangeAfter = Math.min(MAX_COOLDOWN, Math.max(blueFatigue / 10, 1 + effectiveDist));

    waypoints.push({
      system: curr,
      distanceLY: Math.round(dist * 100) / 100,
      fuelThisHop: fuel,
      cumulativeFuel: totalFuel,
      orangeTimer: Math.round(orangeAfter * 10) / 10,
      blueFatigue: Math.round(blueFatigue * 10) / 10,
      waitMinutes: Math.round(waitMin * 10) / 10,
      cumulativeMinutes: Math.round(totalTime * 10) / 10,
    });
  }

  return waypoints;
}
