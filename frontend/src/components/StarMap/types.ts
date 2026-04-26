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
  | 'factionWarfare'
  | 'industry'        // Tier 1.1 — manufacturing/ME/TE/copying/invention/reaction cost index
  | 'adm'             // Tier 2.7 — sov Activity Defense Multiplier (1.0 – 6.0)
  | 'planetType'      // Tier 2.6 — dominant planet type in system (PI site selection)
  | 'radar'           // Tier 1.2 — N-jump reach from a chosen pivot system
  | 'killHeatmap';    // Per-system kill heatmap with time-scrubber slider

export type KillHeatmapWindow = '1d' | '7d' | '30d';

/** Activity index subfield selected inside the 'industry' overlay. */
export type IndustryIndexKind =
  | 'manufacturing'
  | 'me'
  | 'te'
  | 'copying'
  | 'invention'
  | 'reaction';

/** Planet-type IDs we track. Keys match SDE invType IDs. */
export type PlanetTypeId = 11 | 12 | 13 | 2014 | 2015 | 2016 | 2017 | 2063 | 30889;

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
