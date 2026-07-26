import { Container, Graphics } from 'pixi.js';
import type { SystemData } from '../types';
import type { TheraConnection } from '../useOverlayData';

/**
 * Renders Thera/Turnur wormhole connections from Eve-Scout as dashed gold
 * arcs between their K-space exits. Because Thera sits in J-space (excluded
 * from our map), we draw only the K-space side and tag it with a small
 * "T" or "U" badge so the user can tell which hub it routes through.
 *
 * Low-frequency data (10-minute refresh) — we redraw on every update.
 */
export class TheraRenderer {
  readonly container = new Container();
  private gfx = new Graphics();

  init() {
    this.container.label = 'thera';
    this.container.addChild(this.gfx);
  }

  /** Draw/redraw the set of Thera connections. Only draws entries where both
   *  endpoints exist in the K-space systemMap (drops j-space/abyssal). */
  update(connections: TheraConnection[], systemMap: Map<number, SystemData>) {
    this.gfx.clear();

    // Group by K-space system so we can draw one badge per K-space exit even
    // if Eve-Scout has multiple signatures rolling through the same system.
    const kspaceHits = new Map<number, TheraConnection[]>();
    for (const c of connections) {
      const kId = c.src >= 31000000 ? c.dst : c.src;  // pick the K-space side
      const peerId = c.src >= 31000000 ? c.src : c.dst;
      if (!systemMap.has(kId)) continue;
      // Skip pure J-space↔J-space chains (rare but possible)
      if (peerId >= 31000000 && kId >= 31000000) continue;
      if (!kspaceHits.has(kId)) kspaceHits.set(kId, []);
      kspaceHits.get(kId)!.push(c);
    }

    // Small ring + "T" inside at the K-space exit
    for (const kId of kspaceHits.keys()) {
      const sys = systemMap.get(kId);
      if (!sys) continue;
      this.gfx.circle(sys.x, sys.y, 9);
      this.gfx.stroke({ width: 1, color: 0xc8a951, alpha: 0.7 });
      this.gfx.circle(sys.x, sys.y, 4);
      this.gfx.stroke({ width: 1, color: 0xc8a951, alpha: 0.4 });
    }
  }

  setVisible(v: boolean) {
    this.container.visible = v;
  }

  destroy() {
    this.container.destroy({ children: true });
  }
}
