import { Container, BitmapText, BitmapFont } from 'pixi.js';
import type { Viewport } from 'pixi-viewport';
import type { SystemData, RegionData } from '../types';
import { LODTier } from '../types';
import { LOD_THRESHOLDS } from '../utils/constants';

export class LabelRenderer {
  readonly systemLabels = new Container();
  readonly regionLabels = new Container();
  private systemTexts = new Map<number, BitmapText>();
  private systems: SystemData[] = [];
  private currentTier: LODTier = LODTier.Galaxy;

  init(systems: SystemData[], regions: RegionData[]) {
    this.systems = systems;
    this.systemLabels.label = 'systemLabels';
    this.systemLabels.cullable = true;
    this.regionLabels.label = 'regionLabels';
    this.regionLabels.cullable = true;

    // Bitmap fonts matching Vigilant's design system
    BitmapFont.install({
      name: 'MapFont',
      style: {
        fontFamily: 'JetBrains Mono, monospace',
        fontSize: 12,
        fill: '#9a9a9a',
      },
    });

    BitmapFont.install({
      name: 'RegionFont',
      style: {
        fontFamily: 'JetBrains Mono, monospace',
        fontSize: 18,
        fill: '#3a3a3a',
        fontWeight: 'bold',
        letterSpacing: 2,
      },
    });

    // System name labels — all created, visibility managed by updateViewport
    for (const sys of systems) {
      const label = new BitmapText({
        text: sys.name,
        style: { fontFamily: 'MapFont', fontSize: 12 },
      });
      label.position.set(sys.x + 8, sys.y + 4);
      label.visible = false;
      this.systemLabels.addChild(label);
      this.systemTexts.set(sys.id, label);
    }

    // Region labels
    for (const reg of regions) {
      const label = new BitmapText({
        text: reg.name.toUpperCase(),
        style: { fontFamily: 'RegionFont', fontSize: 18 },
      });
      label.anchor.set(0.5);
      label.position.set(reg.cx, reg.cy);
      label.alpha = 0.5;
      this.regionLabels.addChild(label);
    }
  }

  updateLOD(tier: LODTier, scale?: number) {
    this.currentTier = tier;

    // System labels: visible at Constellation and System tiers
    this.systemLabels.visible = tier >= LODTier.Constellation;

    // Region labels: smooth alpha fade
    if (scale !== undefined) {
      const fadeStart = LOD_THRESHOLDS[LODTier.Region]; // 0.15
      const fadeEnd = LOD_THRESHOLDS[LODTier.Constellation]; // 0.5
      if (scale < fadeStart) {
        this.regionLabels.visible = true;
        this.regionLabels.alpha = 0.4;
      } else if (scale < fadeEnd) {
        this.regionLabels.visible = true;
        // Smooth fade from 0.6 down to 0 as we approach constellation zoom
        const t = (scale - fadeStart) / (fadeEnd - fadeStart);
        this.regionLabels.alpha = 0.6 * (1 - t);
      } else {
        this.regionLabels.visible = false;
      }
    } else {
      this.regionLabels.visible = tier <= LODTier.Region;
      this.regionLabels.alpha = tier === LODTier.Galaxy ? 0.4 : 0.6;
    }
  }

  /** Show/hide individual system labels based on viewport bounds. */
  updateViewport(vp: Viewport) {
    if (this.currentTier < LODTier.Constellation) return; // labels not visible anyway

    const bounds = vp.getVisibleBounds();
    // Add padding to avoid pop-in at edges
    const pad = 100 / vp.scaled;
    const minX = bounds.x - pad;
    const maxX = bounds.x + bounds.width + pad;
    const minY = bounds.y - pad;
    const maxY = bounds.y + bounds.height + pad;

    for (const sys of this.systems) {
      const label = this.systemTexts.get(sys.id);
      if (!label) continue;
      label.visible = sys.x >= minX && sys.x <= maxX && sys.y >= minY && sys.y <= maxY;
    }
  }

  getSystemLabel(systemId: number): BitmapText | undefined {
    return this.systemTexts.get(systemId);
  }

  destroy() {
    this.systemLabels.destroy({ children: true });
    this.regionLabels.destroy({ children: true });
  }
}
