import { Graphics } from 'pixi.js';
import type { SystemData } from '../types';
import { ROUTE_COLOR, ROUTE_WIDTH } from '../utils/constants';

const ORIGIN_RING_COLOR = 0x33aa55; // green
const DEST_RING_COLOR = 0xcc5533;   // orange-red
const AVOID_COLOR = 0xcc3333;       // red

export class RouteRenderer {
  readonly graphics = new Graphics();
  private route: number[] = [];
  private avoid: Set<number> = new Set();
  private systemMap = new Map<number, SystemData>();
  private dashOffset = 0;

  init(systemMap: Map<number, SystemData>) {
    this.systemMap = systemMap;
    this.graphics.label = 'route';
  }

  setRoute(systemIds: number[]) {
    this.route = systemIds;
    this.draw();
  }

  /** Update the avoid-system overlay set. Drawn even when there's no active route. */
  setAvoidSystems(avoid: Set<number>) {
    this.avoid = avoid;
    this.draw();
  }

  clearRoute() {
    this.route = [];
    // Re-draw so that any avoid-system overlays remain visible.
    this.draw();
  }

  /** Call each frame for marching-ants animation */
  tick(delta: number) {
    if (this.route.length < 2) return;
    this.dashOffset += delta * 2;
    if (this.dashOffset > 20) this.dashOffset -= 20;
    this.draw();
  }

  private draw() {
    const g = this.graphics;
    g.clear();

    // Route polyline + per-hop markers
    if (this.route.length >= 2) {
      const first = this.systemMap.get(this.route[0]);
      if (first) {
        g.moveTo(first.x, first.y);
        for (let i = 1; i < this.route.length; i++) {
          const sys = this.systemMap.get(this.route[i]);
          if (!sys) continue;
          g.lineTo(sys.x, sys.y);
        }
        g.stroke({ width: ROUTE_WIDTH, color: ROUTE_COLOR, alpha: 0.7 });
      }

      // Per-waypoint markers
      for (let i = 0; i < this.route.length; i++) {
        const id = this.route[i];
        const sys = this.systemMap.get(id);
        if (!sys) continue;

        const isOrigin = i === 0;
        const isDest = i === this.route.length - 1;

        // Diamond marker — slightly larger for endpoints
        const s = isOrigin || isDest ? 8 : 6;
        g.moveTo(sys.x, sys.y - s);
        g.lineTo(sys.x + s, sys.y);
        g.lineTo(sys.x, sys.y + s);
        g.lineTo(sys.x - s, sys.y);
        g.closePath();
        g.fill({ color: ROUTE_COLOR, alpha: 0.9 });

        // Distinct origin / destination ring
        if (isOrigin) {
          g.circle(sys.x, sys.y, 12);
          g.stroke({ width: 2, color: ORIGIN_RING_COLOR, alpha: 0.9 });
        } else if (isDest) {
          g.circle(sys.x, sys.y, 12);
          g.stroke({ width: 2, color: DEST_RING_COLOR, alpha: 0.9 });
        }
      }
    }

    // Avoid-system X overlay (rendered independently of the route so the
    // user can see their avoid list even before plotting a route).
    if (this.avoid.size > 0) {
      const r = 5;
      for (const id of this.avoid) {
        const sys = this.systemMap.get(id);
        if (!sys) continue;
        g.moveTo(sys.x - r, sys.y - r);
        g.lineTo(sys.x + r, sys.y + r);
        g.moveTo(sys.x + r, sys.y - r);
        g.lineTo(sys.x - r, sys.y + r);
      }
      g.stroke({ width: 2, color: AVOID_COLOR, alpha: 0.85 });
    }
  }

  destroy() {
    this.graphics.destroy();
  }
}
