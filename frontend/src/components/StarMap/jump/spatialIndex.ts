import type { SystemData } from '../types';
import { jumpDistanceLY } from './distance';

/**
 * 3D grid spatial hash for fast jump range queries.
 * Divides 3D space into cubic cells and allows O(k) range queries
 * instead of O(n) brute force.
 */
export class JumpSpatialIndex {
  private cells = new Map<string, SystemData[]>();
  private cellSize: number;

  constructor(systems: SystemData[], cellSize = 10) {
    this.cellSize = cellSize;
    for (const sys of systems) {
      const key = this.cellKey(sys.x3, sys.y3, sys.z3);
      let cell = this.cells.get(key);
      if (!cell) {
        cell = [];
        this.cells.set(key, cell);
      }
      cell.push(sys);
    }
  }

  private cellKey(x: number, y: number, z: number): string {
    const cx = Math.floor(x / this.cellSize);
    const cy = Math.floor(y / this.cellSize);
    const cz = Math.floor(z / this.cellSize);
    return `${cx},${cy},${cz}`;
  }

  /** Find all systems within `range` LY of `origin`. */
  findInRange(origin: SystemData, range: number): SystemData[] {
    const results: SystemData[] = [];
    const cx = Math.floor(origin.x3 / this.cellSize);
    const cy = Math.floor(origin.y3 / this.cellSize);
    const cz = Math.floor(origin.z3 / this.cellSize);

    // Check 3x3x3 neighborhood of cells
    const r = Math.ceil(range / this.cellSize);
    for (let dx = -r; dx <= r; dx++) {
      for (let dy = -r; dy <= r; dy++) {
        for (let dz = -r; dz <= r; dz++) {
          const key = `${cx + dx},${cy + dy},${cz + dz}`;
          const cell = this.cells.get(key);
          if (!cell) continue;
          for (const sys of cell) {
            if (sys.id === origin.id) continue;
            if (jumpDistanceLY(origin, sys) <= range) {
              results.push(sys);
            }
          }
        }
      }
    }

    return results;
  }
}
