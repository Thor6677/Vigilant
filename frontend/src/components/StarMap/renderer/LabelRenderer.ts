import { Container, BitmapText, BitmapFont } from 'pixi.js';
import type { SystemData, RegionData } from '../types';
import { LODTier } from '../types';

export class LabelRenderer {
  readonly systemLabels = new Container();
  readonly regionLabels = new Container();
  private systemTexts = new Map<number, BitmapText>();
  // font install is synchronous; no tracking needed

  init(systems: SystemData[], regions: RegionData[]) {
    this.systemLabels.label = 'systemLabels';
    this.systemLabels.cullable = true;
    this.regionLabels.label = 'regionLabels';
    this.regionLabels.cullable = true;

    // Install bitmap fonts
    BitmapFont.install({
      name: 'MapFont',
      style: {
        fontFamily: 'DM Sans, sans-serif',
        fontSize: 14,
        fill: '#C8D8E8',
      },
    });

    BitmapFont.install({
      name: 'RegionFont',
      style: {
        fontFamily: 'Rajdhani, sans-serif',
        fontSize: 22,
        fill: '#5A7A9A',
        fontWeight: 'bold',
      },
    });

    // System name labels (hidden by default — shown at Constellation+ zoom)
    for (const sys of systems) {
      const label = new BitmapText({
        text: sys.name,
        style: { fontFamily: 'MapFont', fontSize: 14 },
      });
      label.position.set(sys.x + 8, sys.y + 4);
      label.visible = false;
      label.cullable = true;
      this.systemLabels.addChild(label);
      this.systemTexts.set(sys.id, label);
    }

    // Region labels (visible at Galaxy/Region zoom)
    for (const reg of regions) {
      const label = new BitmapText({
        text: reg.name.toUpperCase(),
        style: { fontFamily: 'RegionFont', fontSize: 22 },
      });
      label.anchor.set(0.5);
      label.position.set(reg.cx, reg.cy);
      label.alpha = 0.6;
      label.cullable = true;
      this.regionLabels.addChild(label);
    }
  }

  updateLOD(tier: LODTier) {
    // System labels: visible at Constellation and System tiers
    const showSystemLabels = tier >= LODTier.Constellation;
    this.systemLabels.visible = showSystemLabels;

    // Region labels: visible at Galaxy and Region tiers
    const showRegionLabels = tier <= LODTier.Region;
    this.regionLabels.visible = showRegionLabels;

    // Adjust region label alpha based on tier
    if (showRegionLabels) {
      this.regionLabels.alpha = tier === LODTier.Galaxy ? 0.4 : 0.7;
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
