import { Graphics } from 'pixi.js';
import type { SystemData, Edge } from '../types';
import { LODTier } from '../types';
import {
  EDGE_ALPHA, EDGE_WIDTH,
  EDGE_COLOR_SAME_CONSTELLATION,
  EDGE_COLOR_SAME_REGION,
  EDGE_COLOR_CROSS_REGION,
} from '../utils/constants';

export class EdgeRenderer {
  readonly graphics = new Graphics();
  private systems = new Map<number, SystemData>();
  private edges: Edge[] = [];
  private currentLOD: LODTier = LODTier.Galaxy;

  init(systemMap: Map<number, SystemData>, edges: Edge[]) {
    this.systems = systemMap;
    this.edges = edges;
    this.graphics.label = 'edges';
    this.draw(LODTier.Galaxy);
  }

  updateLOD(tier: LODTier) {
    if (tier === this.currentLOD) return;
    this.currentLOD = tier;
    this.draw(tier);
  }

  private draw(tier: LODTier) {
    const g = this.graphics;
    g.clear();

    const alpha = EDGE_ALPHA[tier];
    const width = EDGE_WIDTH[tier];

    for (const [srcId, dstId] of this.edges) {
      const src = this.systems.get(srcId);
      const dst = this.systems.get(dstId);
      if (!src || !dst) continue;

      let color: number;
      if (src.conId === dst.conId) {
        color = EDGE_COLOR_SAME_CONSTELLATION;
      } else if (src.regId === dst.regId) {
        color = EDGE_COLOR_SAME_REGION;
      } else {
        color = EDGE_COLOR_CROSS_REGION;
      }

      g.moveTo(src.x, src.y);
      g.lineTo(dst.x, dst.y);
      g.stroke({ width, color, alpha });
    }
  }

  destroy() {
    this.graphics.destroy();
  }
}
