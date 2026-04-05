import { Container, Graphics } from 'pixi.js';
import type { SystemData, Edge } from '../types';
import { LODTier } from '../types';
import {
  EDGE_ALPHA,
  EDGE_COLOR_SAME_CONSTELLATION,
  EDGE_COLOR_SAME_REGION,
  EDGE_COLOR_CROSS_REGION,
} from '../utils/constants';

/**
 * Renders all ~7K stargate edges using 3 batched Graphics objects
 * (one per edge type). Edges are drawn once at init; LOD changes
 * only update alpha and are fast (no redraw).
 */
export class EdgeRenderer {
  readonly container = new Container();
  private constellationGfx = new Graphics();
  private regionGfx = new Graphics();
  private crossRegionGfx = new Graphics();
  private currentLOD: LODTier = LODTier.Galaxy;

  init(systemMap: Map<number, SystemData>, edges: Edge[]) {
    this.container.label = 'edges';

    // Sort edges into 3 groups
    const conEdges: [SystemData, SystemData][] = [];
    const regEdges: [SystemData, SystemData][] = [];
    const crossEdges: [SystemData, SystemData][] = [];

    for (const [srcId, dstId] of edges) {
      const src = systemMap.get(srcId);
      const dst = systemMap.get(dstId);
      if (!src || !dst) continue;

      if (src.conId === dst.conId) {
        conEdges.push([src, dst]);
      } else if (src.regId === dst.regId) {
        regEdges.push([src, dst]);
      } else {
        crossEdges.push([src, dst]);
      }
    }

    // Draw each group once with a single stroke call
    this.drawGroup(this.constellationGfx, conEdges, EDGE_COLOR_SAME_CONSTELLATION, 0.8);
    this.drawGroup(this.regionGfx, regEdges, EDGE_COLOR_SAME_REGION, 0.8);
    this.drawGroup(this.crossRegionGfx, crossEdges, EDGE_COLOR_CROSS_REGION, 0.8);

    this.container.addChild(this.constellationGfx);
    this.container.addChild(this.regionGfx);
    this.container.addChild(this.crossRegionGfx);

    // Set initial LOD alpha
    this.applyLOD(LODTier.Galaxy);
  }

  private drawGroup(g: Graphics, edges: [SystemData, SystemData][], color: number, width: number) {
    for (const [src, dst] of edges) {
      g.moveTo(src.x, src.y);
      g.lineTo(dst.x, dst.y);
    }
    g.stroke({ width, color, alpha: 1 });
  }

  updateLOD(tier: LODTier) {
    if (tier === this.currentLOD) return;
    this.currentLOD = tier;
    this.applyLOD(tier);
  }

  private applyLOD(tier: LODTier) {
    const alpha = EDGE_ALPHA[tier];
    this.constellationGfx.alpha = alpha;
    this.regionGfx.alpha = alpha * 0.8; // slightly dimmer for same-region
    this.crossRegionGfx.alpha = Math.min(1, alpha * 1.5); // brighter for cross-region
  }

  destroy() {
    this.container.destroy({ children: true });
  }
}
