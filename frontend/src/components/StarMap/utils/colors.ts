/**
 * EVE Online security status color scale.
 * Returns a hex number suitable for Pixi.js tinting.
 *
 * Raw sec values from the SDE (e.g. 0.46) are rounded to one decimal
 * to match the in-game display. Without rounding, a 0.46 system would
 * show as "0.5" in the UI but get the orange 0.4 color — confusing.
 */
export function securityColor(sec: number): number {
  // Round to 1 decimal to match EVE's displayed sec value
  const s = Math.round(sec * 10) / 10;
  if (s >= 1.0) return 0x2fefef;
  if (s >= 0.9) return 0x48f148;
  if (s >= 0.8) return 0x48f148;
  if (s >= 0.7) return 0x00ef47;
  if (s >= 0.6) return 0x00ef47;
  if (s >= 0.5) return 0xefef00;
  if (s >= 0.4) return 0xd77700;
  if (s >= 0.3) return 0xef6f00;
  if (s >= 0.2) return 0xef0000;
  if (s >= 0.1) return 0xd73000;
  if (s > 0.0) return 0xf05050;
  // 0.0 and below: gradient toward deep red
  const t = Math.min(1, Math.abs(sec));
  const r = Math.round(0xf0 - t * 0x70);
  const g = Math.round(0x50 - t * 0x50);
  const b = Math.round(0x50 - t * 0x50);
  return (r << 16) | (g << 8) | b;
}

/**
 * CSS color string for security status (used in HTML overlays).
 */
export function securityColorCSS(sec: number): string {
  const hex = securityColor(sec);
  return `#${hex.toString(16).padStart(6, '0')}`;
}

/**
 * Heatmap color ramp: 0 = dim blue/gray, 1 = bright yellow/red.
 */
export function heatmapColor(t: number): number {
  t = Math.max(0, Math.min(1, t));
  if (t === 0) return 0x1a1a40; // No activity — dim
  if (t < 0.5) {
    const s = t * 2;
    const r = Math.round(s * 0xef);
    const g = Math.round(s * 0xef);
    const b = Math.round((1 - s) * 0x60 + 0x20);
    return (r << 16) | (g << 8) | b;
  }
  const s = (t - 0.5) * 2;
  const r = 0xef;
  const g = Math.round((1 - s) * 0xef);
  const b = Math.round((1 - s) * 0x20);
  return (r << 16) | (g << 8) | b;
}

/**
 * Deterministic color from an alliance/corp ID.
 * Produces consistent, visually distinct colors.
 */
export function allianceColor(id: number): number {
  // Simple hash to spread IDs across hue space
  let h = ((id * 2654435761) >>> 0) % 360;
  const s = 0.55 + (((id * 31) % 100) / 100) * 0.3; // 0.55-0.85
  const l = 0.45 + (((id * 17) % 100) / 100) * 0.2; // 0.45-0.65
  return hslToHex(h, s, l);
}

function hslToHex(h: number, s: number, l: number): number {
  const a = s * Math.min(l, 1 - l);
  const f = (n: number) => {
    const k = (n + h / 30) % 12;
    const color = l - a * Math.max(Math.min(k - 3, 9 - k, 1), -1);
    return Math.round(255 * color);
  };
  return (f(0) << 16) | (f(8) << 8) | f(4);
}

/** Faction warfare faction colors */
export const FACTION_COLORS: Record<number, number> = {
  500001: 0x4488cc, // Caldari — blue
  500002: 0xcc6633, // Minmatar — rust
  500003: 0xccaa33, // Amarr — gold
  500004: 0x33aa66, // Gallente — green
};
