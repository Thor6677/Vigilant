export interface SystemData {
  id: number;
  name: string;
  x: number;
  y: number;
  sec: number;
  conId: number;
  conName: string;
  regId: number;
  regName: string;
  hasStation: boolean;
  stns: number;    // NPC station count
  svcs: string[];  // Available services: cloning, factory, lab, market, refinery, repair, reprocessing, jumpClone
  x3: number;  // 3D position in light-years (for jump drive calculations)
  y3: number;
  z3: number;
}

export interface RegionData {
  id: number;
  name: string;
  cx: number;
  cy: number;
}

export type Edge = [number, number];

export interface MapData {
  systems: SystemData[];
  edges: Edge[];
  regions: RegionData[];
  systemMap: Map<number, SystemData>;
}

export type OverlayType =
  | 'security'
  | 'sovereignty'
  | 'shipKills'
  | 'podKills'
  | 'npcKills'
  | 'jumps'
  | 'incursions'
  | 'factionWarfare';

export type RoutePreference = 'shortest' | 'highsec' | 'lowsec' | 'nullsec' | 'safest';

export type GroupMode = 'systems' | 'constellation' | 'region';

export type JumpShipClass =
  | 'carrier'
  | 'dreadnought'
  | 'fax'
  | 'supercarrier'
  | 'titan'
  | 'jumpFreighter'
  | 'rorqual'
  | 'blackOps';

export const LODTier = {
  Galaxy: 0,
  Region: 1,
  Constellation: 2,
  System: 3,
} as const;

export type LODTier = (typeof LODTier)[keyof typeof LODTier];
