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
 * Renders stargate edges. The main container has 3 batched Graphics
 * (constellation/region/cross-region) drawn once at init.
 * A separate filteredGfx Graphics is used in group mode to show
 * only edges connected to the expanded group's systems.
 */
export class EdgeRenderer {
  readonly container = new Container();
  private constellationGfx = new Graphics();
  private regionGfx = new Graphics();
  private crossRegionGfx = new Graphics();
  private filteredGfx = new Graphics();
  private currentLOD: LODTier = LODTier.Galaxy;

  // Store raw data for filtered redraw
  private systemMap = new Map<number, SystemData>();
  private edges: Edge[] = [];

  init(systemMap: Map<number, SystemData>, edges: Edge[]) {
    this.systemMap = systemMap;
    this.edges = edges;
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

    this.drawGroup(this.constellationGfx, conEdges, EDGE_COLOR_SAME_CONSTELLATION, 0.8);
    this.drawGroup(this.regionGfx, regEdges, EDGE_COLOR_SAME_REGION, 0.8);
    this.drawGroup(this.crossRegionGfx, crossEdges, EDGE_COLOR_CROSS_REGION, 0.8);

    this.container.addChild(this.constellationGfx);
    this.container.addChild(this.regionGfx);
    this.container.addChild(this.crossRegionGfx);
    this.container.addChild(this.filteredGfx);

    this.filteredGfx.visible = false;
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
    this.regionGfx.alpha = alpha * 0.8;
    this.crossRegionGfx.alpha = Math.min(1, alpha * 1.5);
    this.filteredGfx.alpha = Math.max(alpha, 0.35);
  }

  /**
   * Show only edges where at least one endpoint is in the given set.
   * Pass null to show all edges (normal mode).
   */
  setVisibleSystems(ids: Set<number> | null) {
    if (ids === null) {
      // Normal mode: show batched edges, hide filtered
      this.constellationGfx.visible = true;
      this.regionGfx.visible = true;
      this.crossRegionGfx.visible = true;
      this.filteredGfx.visible = false;
      return;
    }

    // Group mode: hide batched edges, draw filtered edges
    this.constellationGfx.visible = false;
    this.regionGfx.visible = false;
    this.crossRegionGfx.visible = false;

    this.filteredGfx.clear();

    if (ids.size === 0) {
      this.filteredGfx.visible = false;
      return;
    }

    this.filteredGfx.visible = true;

    // Draw edges where at least one endpoint is in the expanded group
    for (const [srcId, dstId] of this.edges) {
      if (!ids.has(srcId) && !ids.has(dstId)) continue;

      const src = this.systemMap.get(srcId);
      const dst = this.systemMap.get(dstId);
      if (!src || !dst) continue;

      // Color: internal edges vs cross-group edges
      let color: number;
      if (ids.has(srcId) && ids.has(dstId)) {
        // Both in group — use constellation/region coloring
        if (src.conId === dst.conId) color = EDGE_COLOR_SAME_CONSTELLATION;
        else if (src.regId === dst.regId) color = EDGE_COLOR_SAME_REGION;
        else color = EDGE_COLOR_CROSS_REGION;
      } else {
        // One endpoint outside — cross-group connection (brighter)
        color = EDGE_COLOR_CROSS_REGION;
      }

      this.filteredGfx.moveTo(src.x, src.y);
      this.filteredGfx.lineTo(dst.x, dst.y);
      this.filteredGfx.stroke({ width: 1, color, alpha: 0.5 });
    }
  }

  // ── Hover highlight ──────────────────────────────────────────────

  private hoverGfx = new Graphics();
  private hoverActive = false;

  initHoverLayer() {
    this.hoverGfx.visible = false;
    this.container.addChild(this.hoverGfx);
  }

  /** Highlight edges connected to the hovered system, dim the rest. */
  setHoverHighlight(systemId: number | null, neighborIds: Set<number> | null) {
    if (systemId === null || neighborIds === null) {
      if (this.hoverActive) {
        this.hoverGfx.visible = false;
        this.hoverGfx.clear();
        // Restore batch alpha
        this.applyLOD(this.currentLOD);
        this.hoverActive = false;
      }
      return;
    }

    this.hoverActive = true;

    // Dim the batched edges
    this.constellationGfx.alpha = 0.04;
    this.regionGfx.alpha = 0.04;
    this.crossRegionGfx.alpha = 0.06;

    // Draw highlighted edges
    this.hoverGfx.clear();
    this.hoverGfx.visible = true;

    const connected = new Set(neighborIds);
    connected.add(systemId);

    for (const [srcId, dstId] of this.edges) {
      if (!connected.has(srcId) && !connected.has(dstId)) continue;
      // Must have at least one endpoint as the hovered system
      if (srcId !== systemId && dstId !== systemId) continue;

      const src = this.systemMap.get(srcId);
      const dst = this.systemMap.get(dstId);
      if (!src || !dst) continue;

      this.hoverGfx.moveTo(src.x, src.y);
      this.hoverGfx.lineTo(dst.x, dst.y);
    }
    this.hoverGfx.stroke({ width: 1.5, color: 0x88aacc, alpha: 0.7 });
  }

  destroy() {
    this.container.destroy({ children: true });
  }
}
