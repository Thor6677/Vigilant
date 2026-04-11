import { Container, Graphics } from 'pixi.js';
import type { SystemData } from '../types';
import type { SovData } from '../useOverlayData';
import { allianceColor, FACTION_COLORS } from '../utils/colors';

const BLOB_RADIUS = 70;
const BLOB_ALPHA = 0.09;

/**
 * Renders alliance territory shading as alpha-blended filled circles
 * behind each sovereignty-held system. Overlapping circles from the
 * same alliance merge into organic "territory blob" regions.
 *
 * Inserted into the viewport before EdgeRenderer so it renders
 * behind edges and system dots.
 */
export class AllianceTerritoryRenderer {
  readonly container = new Container();
  private gfx = new Graphics();
  private visible = false;

  constructor() {
    this.container.label = 'allianceTerritory';
    this.container.addChild(this.gfx);
    this.container.visible = false;
  }

  /**
   * Redraw territory blobs for all sovereignty-held systems.
   * @param dimSet Optional set of system IDs to dim (for sov change mode).
   *              Systems NOT in dimSet are drawn brighter; systems in dimSet are dimmed.
   *              If null, all systems drawn at normal alpha.
   */
  update(
    systems: SystemData[],
    sovData: Record<string, SovData>,
    dimSet: Set<number> | null = null,
  ) {
    this.gfx.clear();

    // Group systems by holder (alliance_id or faction_id)
    const groups = new Map<number, { color: number; systems: SystemData[]; isFaction: boolean }>();

    for (const sys of systems) {
      const sov = sovData[String(sys.id)];
      if (!sov) continue;

      let holderId: number | null = null;
      let color = 0;
      let isFaction = false;

      if (sov.alliance_id) {
        holderId = sov.alliance_id;
        color = allianceColor(sov.alliance_id);
      } else if (sov.faction_id) {
        holderId = sov.faction_id + 1_000_000_000; // offset to avoid alliance_id collision
        color = FACTION_COLORS[sov.faction_id] ?? 0x555577;
        isFaction = true;
      }

      if (holderId === null) continue;

      let group = groups.get(holderId);
      if (!group) {
        group = { color, systems: [], isFaction };
        groups.set(holderId, group);
      }
      group.systems.push(sys);
    }

    // Draw one batch of circles per alliance/faction
    for (const [, group] of groups) {
      for (const sys of group.systems) {
        const alpha = dimSet
          ? (dimSet.has(sys.id) ? BLOB_ALPHA * 0.3 : BLOB_ALPHA * 1.8)
          : BLOB_ALPHA;
        this.gfx.circle(sys.x, sys.y, BLOB_RADIUS);
        this.gfx.fill({ color: group.color, alpha });
      }
    }

    this.container.visible = true;
    this.visible = true;
  }

  clear() {
    this.gfx.clear();
    this.container.visible = false;
    this.visible = false;
  }

  /** Adjust alpha based on zoom level — fade at galaxy zoom */
  setLODAlpha(zoom: number) {
    if (!this.visible) return;
    if (zoom < 0.08) {
      this.container.alpha = 0.3;
    } else if (zoom < 0.15) {
      this.container.alpha = 0.6;
    } else {
      this.container.alpha = 1.0;
    }
  }
}
