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

/** Wormhole class → fill color. Picks visually distinct hues across
 *  the C1–C6 ladder, with separate shades for the specials.
 *  Returns the dim baseline if the class is unknown. */
export function wormholeClassColor(cls: number | undefined): number {
  if (cls == null) return 0x141414;
  switch (cls) {
    case 1: return 0x4f86ee;  // C1 — soft blue
    case 2: return 0x5ed4d4;  // C2 — cyan
    case 3: return 0x57c873;  // C3 — green
    case 4: return 0xe6cd55;  // C4 — yellow
    case 5: return 0xe78b3a;  // C5 — orange
    case 6: return 0xe05050;  // C6 — red
    case 12: return 0xc8a951; // Thera — gold
    case 13: return 0x8a8a8a; // Drifter shattered — gray
    case 14: case 15: case 16: case 17: case 18:
      return 0xa97bd0;        // Drifter complexes — purple
    case 19: return 0x6e6e6e; // Other shattered — slightly darker gray
    case 25: return 0xd03ed0; // Pochven — magenta
    default: return 0x474747;
  }
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

/** Industry cost-index color ramp. 0 = dim blue (cheap), 1 = bright red (busy).
 *  ESI reports indices as decimals ≥ 0, usually 0.0 – 0.2 for quiet systems,
 *  0.3 – 0.8 for industrial hubs, and 1.0+ for saturated markets (Jita/Amarr).
 *  We normalize on a reference max so one Jita doesn't wash out the scale. */
export function industryColor(value: number, maxSeen: number): number {
  if (!isFinite(value) || value <= 0) return 0x1a1a1a;
  const cap = Math.max(0.1, maxSeen);
  // Use sqrt so the low end has more visible differentiation
  const t = Math.sqrt(Math.min(value / cap, 1));
  return heatmapColor(t);
}

/** ADM (Activity Defense Multiplier) color — inverted from industry since
 *  HIGH ADM = strongly defended = friendly for the sov holder.
 *  1.0 ≈ undefended (red), 6.0 ≈ maxed (green). */
export function admColor(value: number): number {
  if (!isFinite(value) || value <= 0) return 0x1a1a1a;
  // Clamp to 1.0 – 6.0, normalize to 0..1
  const t = Math.max(0, Math.min(1, (value - 1) / 5));
  // Red (low ADM) → yellow → green (high ADM)
  if (t < 0.5) {
    const s = t * 2;
    const r = 0xcc;
    const g = Math.round(s * 0xaa);
    const b = Math.round(0x33 - s * 0x33);
    return (r << 16) | (g << 8) | b;
  }
  const s = (t - 0.5) * 2;
  const r = Math.round((1 - s) * 0xcc + s * 0x33);
  const g = 0xaa;
  const b = Math.round(s * 0x66);
  return (r << 16) | (g << 8) | b;
}

/** Per-planet-type color palette (matches typical EVE community references). */
export const PLANET_TYPE_COLORS: Record<number, number> = {
  11:    0x33aa66, // Temperate — green
  12:    0x88ccee, // Ice — pale blue
  13:    0x66aa99, // Gas — teal
  2014:  0x3366cc, // Oceanic — deep blue
  2015:  0xee3300, // Lava — red/orange
  2016:  0xa08060, // Barren — tan
  2017:  0xbb66cc, // Storm — purple
  2063:  0xee8844, // Plasma — orange
  30889: 0x888888, // Shattered — grey
};

/** Pick the dominant planet-type color for a system, based on count. */
export function dominantPlanetColor(counts: Record<string, number> | undefined): number {
  if (!counts) return 0x1a1a1a;
  let bestId = 0;
  let bestN = 0;
  for (const [tid, n] of Object.entries(counts)) {
    if (n > bestN) { bestN = n; bestId = Number(tid); }
  }
  return PLANET_TYPE_COLORS[bestId] ?? 0x1a1a1a;
}
