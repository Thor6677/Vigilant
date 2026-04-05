import { LODTier } from '../types';
import type { LODTier as LODTierType } from '../types';

// Zoom scale thresholds for LOD tiers
export const LOD_THRESHOLDS: Record<LODTierType, number> = {
  [LODTier.Galaxy]: 0,
  [LODTier.Region]: 0.15,
  [LODTier.Constellation]: 0.5,
  [LODTier.System]: 2.0,
};

// Target screen-pixel size for system nodes at each LOD tier.
// Nodes scale inversely with zoom to maintain consistent screen size.
export const NODE_SCREEN_PX: Record<LODTierType, number> = {
  [LODTier.Galaxy]: 3,
  [LODTier.Region]: 4,
  [LODTier.Constellation]: 6,
  [LODTier.System]: 8,
};

// Circle texture radius (must match the radius in SystemRenderer.init)
export const NODE_TEXTURE_RADIUS = 16;

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
export const EDGE_COLOR_CROSS_REGION = 0x7755bb;

// Label fade thresholds (system labels fade in between these zoom scales)
export const LABEL_FADE_START = 0.5;
export const LABEL_FADE_END = 0.8;

// Glow texture radius (larger than node for ambient glow)
export const GLOW_TEXTURE_RADIUS = 32;

// Viewport
export const MIN_ZOOM = 0.02;
export const MAX_ZOOM = 8;
export const CANVAS_SIZE = 10000;

// Route
export const ROUTE_COLOR = 0x00d4ff;
export const ROUTE_WIDTH = 3;

// Map background
export const BG_COLOR = 0x080808;
