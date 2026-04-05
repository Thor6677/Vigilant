import { Graphics } from 'pixi.js';
import type { SystemData } from '../types';
import { ROUTE_COLOR, ROUTE_WIDTH } from '../utils/constants';

export class RouteRenderer {
  readonly graphics = new Graphics();
  private route: number[] = [];
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

  clearRoute() {
    this.route = [];
    this.graphics.clear();
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

    if (this.route.length < 2) return;

    // Draw solid route line
    const first = this.systemMap.get(this.route[0]);
    if (!first) return;

    g.moveTo(first.x, first.y);
    for (let i = 1; i < this.route.length; i++) {
      const sys = this.systemMap.get(this.route[i]);
      if (!sys) continue;
      g.lineTo(sys.x, sys.y);
    }
    g.stroke({ width: ROUTE_WIDTH, color: ROUTE_COLOR, alpha: 0.7 });

    // Draw waypoint diamonds
    for (const id of this.route) {
      const sys = this.systemMap.get(id);
      if (!sys) continue;
      const s = 6;
      g.moveTo(sys.x, sys.y - s);
      g.lineTo(sys.x + s, sys.y);
      g.lineTo(sys.x, sys.y + s);
      g.lineTo(sys.x - s, sys.y);
      g.closePath();
      g.fill({ color: ROUTE_COLOR, alpha: 0.9 });
    }
  }

  destroy() {
    this.graphics.destroy();
  }
}
