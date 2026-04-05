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

export type RoutePreference = 'shortest' | 'highsec' | 'lowsec' | 'nullsec';

export const LODTier = {
  Galaxy: 0,
  Region: 1,
  Constellation: 2,
  System: 3,
} as const;

export type LODTier = (typeof LODTier)[keyof typeof LODTier];
