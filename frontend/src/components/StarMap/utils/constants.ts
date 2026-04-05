import { LODTier } from '../types';
import type { LODTier as LODTierType } from '../types';

// Zoom scale thresholds for LOD tiers
export const LOD_THRESHOLDS: Record<LODTierType, number> = {
  [LODTier.Galaxy]: 0,
  [LODTier.Region]: 0.15,
  [LODTier.Constellation]: 0.5,
  [LODTier.System]: 2.0,
};

// System node sizes at each LOD tier
export const NODE_SIZES: Record<LODTierType, number> = {
  [LODTier.Galaxy]: 1.5,
  [LODTier.Region]: 3,
  [LODTier.Constellation]: 5,
  [LODTier.System]: 8,
};

// Edge rendering
export const EDGE_ALPHA: Record<LODTierType, number> = {
  [LODTier.Galaxy]: 0.15,
  [LODTier.Region]: 0.25,
  [LODTier.Constellation]: 0.4,
  [LODTier.System]: 0.6,
};

export const EDGE_WIDTH: Record<LODTierType, number> = {
  [LODTier.Galaxy]: 0.5,
  [LODTier.Region]: 0.8,
  [LODTier.Constellation]: 1.0,
  [LODTier.System]: 1.5,
};

// Edge colors by type
export const EDGE_COLOR_SAME_CONSTELLATION = 0x2a2a3a;
export const EDGE_COLOR_SAME_REGION = 0x3a3a5a;
export const EDGE_COLOR_CROSS_REGION = 0x6644aa;

// Viewport
export const MIN_ZOOM = 0.02;
export const MAX_ZOOM = 8;
export const CANVAS_SIZE = 10000;

// Route
export const ROUTE_COLOR = 0x00d4ff;
export const ROUTE_WIDTH = 3;

// Map background
export const BG_COLOR = 0x080808;
