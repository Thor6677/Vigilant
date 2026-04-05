import { Graphics } from 'pixi.js';
import type { SystemData } from '../types';

const JUMP_LINE_COLOR = 0xff6600;
const JUMP_ROUTE_COLOR = 0xff8800;

/**
 * Renders jump range connections and jump route arcs.
 */
export class JumpRangeRenderer {
  readonly rangeGraphics = new Graphics();
  readonly routeGraphics = new Graphics();
  private phase = 0;
  private routeSystems: SystemData[] = [];

  init() {
    this.rangeGraphics.label = 'jumpRange';
    this.routeGraphics.label = 'jumpRoute';
  }

  /** Draw lines from origin to all reachable systems. */
  setReachable(origin: SystemData | null, reachable: SystemData[]) {
    this.rangeGraphics.clear();
    if (!origin || reachable.length === 0) return;

    for (const sys of reachable) {
      this.rangeGraphics.moveTo(origin.x, origin.y);

      const mx = (origin.x + sys.x) / 2;
      const my = (origin.y + sys.y) / 2;
      const dx = sys.x - origin.x;
      const dy = sys.y - origin.y;
      const len = Math.sqrt(dx * dx + dy * dy) || 1;
      const offset = Math.min(len * 0.1, 30);
      const cx = mx - (dy / len) * offset;
      const cy = my + (dx / len) * offset;

      this.rangeGraphics.quadraticCurveTo(cx, cy, sys.x, sys.y);
    }
    this.rangeGraphics.stroke({ width: 1, color: JUMP_LINE_COLOR, alpha: 0.15 });
  }

  /** Draw the calculated jump route. */
  setRoute(route: SystemData[]) {
    this.routeSystems = route;
    this.redrawRoute();
  }

  private redrawRoute() {
    this.routeGraphics.clear();
    const route = this.routeSystems;
    if (route.length < 2) return;

    // Thick route lines
    for (let i = 0; i < route.length - 1; i++) {
      const a = route[i];
      const b = route[i + 1];

      const mx = (a.x + b.x) / 2;
      const my = (a.y + b.y) / 2;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const len = Math.sqrt(dx * dx + dy * dy) || 1;
      const offset = Math.min(len * 0.12, 50);
      const cx = mx - (dy / len) * offset;
      const cy = my + (dx / len) * offset;

      // Glow line (wider, dimmer)
      this.routeGraphics.moveTo(a.x, a.y);
      this.routeGraphics.quadraticCurveTo(cx, cy, b.x, b.y);
      this.routeGraphics.stroke({ width: 12, color: JUMP_ROUTE_COLOR, alpha: 0.15 });

      // Core line
      this.routeGraphics.moveTo(a.x, a.y);
      this.routeGraphics.quadraticCurveTo(cx, cy, b.x, b.y);
      this.routeGraphics.stroke({ width: 4, color: JUMP_ROUTE_COLOR, alpha: 0.9 });
    }

    // Waypoint markers: larger diamonds
    for (const sys of route) {
      const s = 10;
      this.routeGraphics.moveTo(sys.x, sys.y - s);
      this.routeGraphics.lineTo(sys.x + s, sys.y);
      this.routeGraphics.lineTo(sys.x, sys.y + s);
      this.routeGraphics.lineTo(sys.x - s, sys.y);
      this.routeGraphics.closePath();
      this.routeGraphics.fill({ color: JUMP_ROUTE_COLOR, alpha: 0.9 });
      // Diamond outline
      this.routeGraphics.moveTo(sys.x, sys.y - s);
      this.routeGraphics.lineTo(sys.x + s, sys.y);
      this.routeGraphics.lineTo(sys.x, sys.y + s);
      this.routeGraphics.lineTo(sys.x - s, sys.y);
      this.routeGraphics.closePath();
      this.routeGraphics.stroke({ width: 1.5, color: 0xffffff, alpha: 0.4 });
    }
  }

  clearAll() {
    this.rangeGraphics.clear();
    this.routeGraphics.clear();
    this.routeSystems = [];
  }

  tick(delta: number) {
    if (this.routeSystems.length > 1) {
      this.phase += delta * 0.03;
      this.routeGraphics.alpha = 0.8 + 0.2 * Math.sin(this.phase);
    }
  }

  destroy() {
    this.rangeGraphics.destroy();
    this.routeGraphics.destroy();
  }
}
