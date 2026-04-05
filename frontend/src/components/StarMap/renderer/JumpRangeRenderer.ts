import { Graphics } from 'pixi.js';
import type { SystemData } from '../types';

const JUMP_LINE_COLOR = 0xff6600;
const JUMP_ROUTE_COLOR = 0xff8800;

/**
 * Renders jump range connections and jump route arcs.
 * Separate from gate route rendering (RouteRenderer).
 */
export class JumpRangeRenderer {
  readonly rangeGraphics = new Graphics();
  readonly routeGraphics = new Graphics();
  private phase = 0;

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

      // Slight bezier curve to distinguish from gate edges
      const mx = (origin.x + sys.x) / 2;
      const my = (origin.y + sys.y) / 2;
      const dx = sys.x - origin.x;
      const dy = sys.y - origin.y;
      const len = Math.sqrt(dx * dx + dy * dy);
      // Perpendicular offset proportional to distance
      const offset = Math.min(len * 0.1, 30);
      const cx = mx - (dy / len) * offset;
      const cy = my + (dx / len) * offset;

      this.rangeGraphics.quadraticCurveTo(cx, cy, sys.x, sys.y);
    }
    this.rangeGraphics.stroke({ width: 0.8, color: JUMP_LINE_COLOR, alpha: 0.2 });
  }

  /** Draw the calculated jump route as arced lines with waypoint markers. */
  setRoute(route: SystemData[]) {
    this.routeGraphics.clear();
    if (route.length < 2) return;

    // Draw arced lines between each hop
    for (let i = 0; i < route.length - 1; i++) {
      const a = route[i];
      const b = route[i + 1];

      const mx = (a.x + b.x) / 2;
      const my = (a.y + b.y) / 2;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const len = Math.sqrt(dx * dx + dy * dy) || 1;
      const offset = Math.min(len * 0.15, 40);
      const cx = mx - (dy / len) * offset;
      const cy = my + (dx / len) * offset;

      this.routeGraphics.moveTo(a.x, a.y);
      this.routeGraphics.quadraticCurveTo(cx, cy, b.x, b.y);
    }
    this.routeGraphics.stroke({ width: 2.5, color: JUMP_ROUTE_COLOR, alpha: 0.8 });

    // Waypoint markers: triangles
    for (const sys of route) {
      const s = 7;
      this.routeGraphics.moveTo(sys.x, sys.y - s);
      this.routeGraphics.lineTo(sys.x + s * 0.866, sys.y + s * 0.5);
      this.routeGraphics.lineTo(sys.x - s * 0.866, sys.y + s * 0.5);
      this.routeGraphics.closePath();
      this.routeGraphics.fill({ color: JUMP_ROUTE_COLOR, alpha: 0.9 });
    }
  }

  clearAll() {
    this.rangeGraphics.clear();
    this.routeGraphics.clear();
  }

  tick(delta: number) {
    // Subtle pulse on route
    if (this.routeGraphics.visible) {
      this.phase += delta * 0.03;
      this.routeGraphics.alpha = 0.7 + 0.3 * Math.sin(this.phase);
    }
  }

  destroy() {
    this.rangeGraphics.destroy();
    this.routeGraphics.destroy();
  }
}
