import type { JumpShipClass } from '../types';

export interface JumpShipConfig {
  label: string;
  baseRange: number;         // LY
  baseFuelPerLY: number;     // isotopes per LY
  fatigueReduction: number;  // 0 = none, 0.5 = 50%, 0.9 = 90%
}

export const JUMP_SHIPS: Record<JumpShipClass, JumpShipConfig> = {
  carrier:       { label: 'Carrier',        baseRange: 3.5, baseFuelPerLY: 1000, fatigueReduction: 0 },
  dreadnought:   { label: 'Dreadnought',    baseRange: 3.5, baseFuelPerLY: 1000, fatigueReduction: 0 },
  fax:           { label: 'Force Auxiliary', baseRange: 3.5, baseFuelPerLY: 1000, fatigueReduction: 0 },
  supercarrier:  { label: 'Supercarrier',   baseRange: 3.0, baseFuelPerLY: 1000, fatigueReduction: 0 },
  titan:         { label: 'Titan',          baseRange: 3.0, baseFuelPerLY: 1000, fatigueReduction: 0 },
  jumpFreighter: { label: 'Jump Freighter', baseRange: 5.0, baseFuelPerLY: 3000, fatigueReduction: 0.9 },
  rorqual:       { label: 'Rorqual',        baseRange: 5.0, baseFuelPerLY: 4000, fatigueReduction: 0.9 },
  blackOps:      { label: 'Black Ops',      baseRange: 4.0, baseFuelPerLY: 750,  fatigueReduction: 0.5 },
};

export const FUEL_TYPES = [
  { typeId: 16274, name: 'Helium Isotopes',   faction: 'Amarr' },
  { typeId: 17888, name: 'Nitrogen Isotopes', faction: 'Caldari' },
  { typeId: 17887, name: 'Oxygen Isotopes',   faction: 'Gallente' },
  { typeId: 17889, name: 'Hydrogen Isotopes', faction: 'Minmatar' },
] as const;

// Max fatigue cap (minutes)
export const MAX_FATIGUE = 300;
// Max orange timer cap (minutes)
export const MAX_COOLDOWN = 30;
