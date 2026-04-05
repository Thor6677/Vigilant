import type { SystemData } from '../types';

/** 3D Euclidean distance in light-years between two systems. */
export function jumpDistanceLY(a: SystemData, b: SystemData): number {
  const dx = a.x3 - b.x3;
  const dy = a.y3 - b.y3;
  const dz = a.z3 - b.z3;
  return Math.sqrt(dx * dx + dy * dy + dz * dz);
}

/** Effective jump range with Jump Drive Calibration skill (20% per level). */
export function effectiveRange(baseRange: number, jdcLevel: number): number {
  return baseRange * (1 + 0.20 * jdcLevel);
}

/** Fuel cost for a single jump with Jump Fuel Conservation skill (10% per level). */
export function fuelCost(distanceLY: number, baseFuelPerLY: number, jfcLevel: number): number {
  return Math.ceil(distanceLY * baseFuelPerLY * (1 - 0.10 * jfcLevel));
}

/** Can a cynosural field be lit in this system? (lowsec + nullsec only) */
export function canLightCyno(system: SystemData): boolean {
  return system.sec < 0.45; // Systems round to 0.4 or below = lowsec/null
}
