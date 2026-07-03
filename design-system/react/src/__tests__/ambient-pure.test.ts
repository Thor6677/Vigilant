// Reaches outside the package root by design: this package hosts the only vitest setup in design-system/.
import { normalizeSystems, allianceColor } from '../../../ambient/vigilant-ambient.js';

test('allianceColor returns stable rgb triple', () => {
  const c = allianceColor(99003581);
  expect(c).toHaveLength(3);
  c.forEach((v) => { expect(v).toBeGreaterThanOrEqual(0); expect(v).toBeLessThanOrEqual(255); });
  expect(allianceColor(99003581)).toEqual(c);
});

test('normalizeSystems centers and scales to ~1000 radius', () => {
  const out = normalizeSystems([
    { id: 1, name: 'A', x3: -10, y3: 0, z3: 0 },
    { id: 2, name: 'B', x3: 10, y3: 2, z3: 4 },
  ]);
  expect(out).toHaveLength(2);
  const maxExt = Math.max(...out.map((s) => Math.max(Math.abs(s.x), Math.abs(s.z))));
  expect(maxExt).toBeCloseTo(1000, 0);
  expect(out[0].id).toBe(1);
});

test('normalizeSystems handles empty array without throwing', () => {
  expect(normalizeSystems([])).toEqual([]);
});
