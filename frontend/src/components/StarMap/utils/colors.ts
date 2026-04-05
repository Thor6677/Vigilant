/**
 * EVE Online security status color scale.
 * Returns a hex number suitable for Pixi.js tinting.
 */
export function securityColor(sec: number): number {
  if (sec >= 1.0) return 0x2fefef;
  if (sec >= 0.9) return 0x48f148;
  if (sec >= 0.8) return 0x48f148;
  if (sec >= 0.7) return 0x00ef47;
  if (sec >= 0.6) return 0x00ef47;
  if (sec >= 0.5) return 0xefef00;
  if (sec >= 0.4) return 0xd77700;
  if (sec >= 0.3) return 0xef6f00;
  if (sec >= 0.2) return 0xef0000;
  if (sec >= 0.1) return 0xd73000;
  if (sec > 0.0) return 0xf05050;
  // 0.0 and below: gradient toward deep red
  const t = Math.min(1, Math.abs(sec)); // 0..1
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
 * Heatmap color ramp: 0 = dim blue, 1 = bright yellow/red.
 */
export function heatmapColor(t: number): number {
  t = Math.max(0, Math.min(1, t));
  if (t < 0.5) {
    // Blue to yellow
    const s = t * 2;
    const r = Math.round(s * 0xef);
    const g = Math.round(s * 0xef);
    const b = Math.round((1 - s) * 0x80 + 0x20);
    return (r << 16) | (g << 8) | b;
  }
  // Yellow to red
  const s = (t - 0.5) * 2;
  const r = 0xef;
  const g = Math.round((1 - s) * 0xef);
  const b = Math.round((1 - s) * 0x20);
  return (r << 16) | (g << 8) | b;
}
