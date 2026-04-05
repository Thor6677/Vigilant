import { Container, BitmapText, BitmapFont } from 'pixi.js';
import type { Viewport } from 'pixi-viewport';
import type { SystemData, RegionData } from '../types';
import { LODTier } from '../types';
import { LOD_THRESHOLDS } from '../utils/constants';
import { securityColor } from '../utils/colors';

interface SystemLabel {
  container: Container;
  nameText: BitmapText;
  secText: BitmapText;
}

export type GroupMode = 'systems' | 'constellation' | 'region';

export interface GroupData {
  id: number;
  name: string;
  cx: number;
  cy: number;
  count: number;
  systemIds: number[];
  // Bounding box for zoom-to-fit
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

export class LabelRenderer {
  readonly systemLabels = new Container();
  readonly regionLabels = new Container();
  readonly groupLabels = new Container();
  private systemLabelMap = new Map<number, SystemLabel>();
  private systems: SystemData[] = [];
  private _tier: LODTier = LODTier.Galaxy;
  private groupMode: GroupMode = 'systems';

  // Pre-computed group data
  constellationGroups: GroupData[] = [];
  regionGroups: GroupData[] = [];

  init(systems: SystemData[], regions: RegionData[]) {
    this.systems = systems;
    this.systemLabels.label = 'systemLabels';
    this.systemLabels.cullable = true;
    this.regionLabels.label = 'regionLabels';
    this.regionLabels.cullable = true;
    this.groupLabels.label = 'groupLabels';
    this.groupLabels.cullable = true;

    // Bitmap fonts
    BitmapFont.install({
      name: 'MapFont',
      style: {
        fontFamily: 'JetBrains Mono, monospace',
        fontSize: 14,
        fill: '#9a9a9a',
      },
    });

    BitmapFont.install({
      name: 'SecFont',
      style: {
        fontFamily: 'JetBrains Mono, monospace',
        fontSize: 14,
        fill: '#ffffff', // White base — tinted per-system
      },
    });

    BitmapFont.install({
      name: 'RegionFont',
      style: {
        fontFamily: 'JetBrains Mono, monospace',
        fontSize: 22,
        fill: '#3a3a3a',
        fontWeight: 'bold',
        letterSpacing: 2,
      },
    });

    BitmapFont.install({
      name: 'GroupFont',
      style: {
        fontFamily: 'JetBrains Mono, monospace',
        fontSize: 16,
        fill: '#7a7a7a',
      },
    });

    // System labels: name + sec value in a container
    for (const sys of systems) {
      const cont = new Container();
      cont.position.set(sys.x + 8, sys.y + 2);
      cont.visible = false;

      const nameText = new BitmapText({
        text: sys.name,
        style: { fontFamily: 'MapFont', fontSize: 14 },
      });

      const secText = new BitmapText({
        text: ` ${sys.sec.toFixed(1)}`,
        style: { fontFamily: 'SecFont', fontSize: 14 },
      });
      secText.tint = securityColor(sys.sec);
      secText.position.x = nameText.width + 2;

      cont.addChild(nameText);
      cont.addChild(secText);

      this.systemLabels.addChild(cont);
      this.systemLabelMap.set(sys.id, { container: cont, nameText, secText });
    }

    // Region labels
    for (const reg of regions) {
      const label = new BitmapText({
        text: reg.name.toUpperCase(),
        style: { fontFamily: 'RegionFont', fontSize: 22 },
      });
      label.anchor.set(0.5);
      label.position.set(reg.cx, reg.cy);
      label.alpha = 0.5;
      this.regionLabels.addChild(label);
    }

    // Pre-compute constellation and region groups
    this.computeGroups(systems);
  }

  private computeGroups(systems: SystemData[]) {
    // Constellation groups
    const conMap = new Map<number, { name: string; systems: SystemData[] }>();
    for (const sys of systems) {
      let group = conMap.get(sys.conId);
      if (!group) {
        group = { name: sys.conName, systems: [] };
        conMap.set(sys.conId, group);
      }
      group.systems.push(sys);
    }
    this.constellationGroups = Array.from(conMap.entries()).map(([id, g]) => ({
      id,
      name: g.name,
      count: g.systems.length,
      systemIds: g.systems.map(s => s.id),
      cx: g.systems.reduce((s, sys) => s + sys.x, 0) / g.systems.length,
      cy: g.systems.reduce((s, sys) => s + sys.y, 0) / g.systems.length,
      minX: Math.min(...g.systems.map(s => s.x)),
      minY: Math.min(...g.systems.map(s => s.y)),
      maxX: Math.max(...g.systems.map(s => s.x)),
      maxY: Math.max(...g.systems.map(s => s.y)),
    }));

    // Region groups
    const regMap = new Map<number, { name: string; systems: SystemData[] }>();
    for (const sys of systems) {
      let group = regMap.get(sys.regId);
      if (!group) {
        group = { name: sys.regName, systems: [] };
        regMap.set(sys.regId, group);
      }
      group.systems.push(sys);
    }
    this.regionGroups = Array.from(regMap.entries()).map(([id, g]) => ({
      id,
      name: g.name,
      count: g.systems.length,
      systemIds: g.systems.map(s => s.id),
      cx: g.systems.reduce((s, sys) => s + sys.x, 0) / g.systems.length,
      cy: g.systems.reduce((s, sys) => s + sys.y, 0) / g.systems.length,
      minX: Math.min(...g.systems.map(s => s.x)),
      minY: Math.min(...g.systems.map(s => s.y)),
      maxX: Math.max(...g.systems.map(s => s.x)),
      maxY: Math.max(...g.systems.map(s => s.y)),
    }));
  }

  setGroupMode(mode: GroupMode) {
    this.groupMode = mode;
    this.rebuildGroupLabels();
  }

  private rebuildGroupLabels() {
    // Clear existing group labels
    this.groupLabels.removeChildren();

    if (this.groupMode === 'systems') {
      this.groupLabels.visible = false;
      return;
    }

    this.groupLabels.visible = true;
    const groups = this.groupMode === 'constellation' ? this.constellationGroups : this.regionGroups;

    for (const group of groups) {
      const cont = new Container();
      cont.position.set(group.cx, group.cy);
      cont.label = String(group.id);

      const nameText = new BitmapText({
        text: group.name.toUpperCase(),
        style: { fontFamily: 'GroupFont', fontSize: 16 },
      });
      nameText.anchor.set(0.5, 1);
      nameText.position.y = -4;

      const countText = new BitmapText({
        text: `${group.count} SYSTEMS`,
        style: { fontFamily: 'MapFont', fontSize: 10 },
      });
      countText.anchor.set(0.5, 0);
      countText.position.y = 4;
      countText.alpha = 0.5;

      cont.addChild(nameText);
      cont.addChild(countText);
      this.groupLabels.addChild(cont);
    }
  }

  updateLOD(tier: LODTier, scale?: number) {
    this._tier = tier;
    // scale is used locally below

    if (this.groupMode !== 'systems') {
      // In group mode: hide system labels and region labels, show group labels
      this.systemLabels.visible = false;
      this.regionLabels.visible = false;
      this.groupLabels.visible = true;
      return;
    }

    // Normal system mode
    this.groupLabels.visible = false;

    // System labels: visible from Region tier onwards (was Constellation)
    this.systemLabels.visible = tier >= LODTier.Region;

    // Region labels: smooth alpha fade
    if (scale !== undefined) {
      const fadeStart = LOD_THRESHOLDS[LODTier.Region];
      const fadeEnd = LOD_THRESHOLDS[LODTier.Constellation];
      if (scale < fadeStart) {
        this.regionLabels.visible = true;
        this.regionLabels.alpha = 0.4;
      } else if (scale < fadeEnd) {
        this.regionLabels.visible = true;
        this.regionLabels.alpha = 0.6 * (1 - (scale - fadeStart) / (fadeEnd - fadeStart));
      } else {
        this.regionLabels.visible = false;
      }
    } else {
      this.regionLabels.visible = tier <= LODTier.Region;
      this.regionLabels.alpha = tier === LODTier.Galaxy ? 0.4 : 0.6;
    }
  }

  /** Update label scale and visibility based on viewport. */
  updateViewport(vp: Viewport) {
    const scale = vp.scaled;

    // Scale system labels inversely with zoom for consistent screen size
    if (this.systemLabels.visible && this._tier >= LODTier.Region) {
      const targetScreenPx = 10 + Math.min(4, scale * 2);
      const labelScale = targetScreenPx / (14 * scale); // 14 = base font size

      const bounds = vp.getVisibleBounds();
      const pad = 200 / scale;
      const minX = bounds.x - pad;
      const maxX = bounds.x + bounds.width + pad;
      const minY = bounds.y - pad;
      const maxY = bounds.y + bounds.height + pad;

      for (const sys of this.systems) {
        const sl = this.systemLabelMap.get(sys.id);
        if (!sl) continue;
        const inView = sys.x >= minX && sys.x <= maxX && sys.y >= minY && sys.y <= maxY;
        sl.container.visible = inView;
        if (inView) {
          sl.container.scale.set(labelScale);
        }
      }
    }

    // Scale group labels similarly
    if (this.groupLabels.visible) {
      const targetScreenPx = 12 + Math.min(6, scale * 3);
      const groupScale = targetScreenPx / (16 * scale);

      const bounds = vp.getVisibleBounds();
      const pad = 300 / scale;
      const minX = bounds.x - pad;
      const maxX = bounds.x + bounds.width + pad;
      const minY = bounds.y - pad;
      const maxY = bounds.y + bounds.height + pad;

      for (const child of this.groupLabels.children) {
        const gx = child.position.x;
        const gy = child.position.y;
        child.visible = gx >= minX && gx <= maxX && gy >= minY && gy <= maxY;
        if (child.visible) {
          child.scale.set(groupScale);
        }
      }
    }
  }

  getCurrentGroupMode(): GroupMode {
    return this.groupMode;
  }

  getSystemLabel(systemId: number): BitmapText | undefined {
    return this.systemLabelMap.get(systemId)?.nameText;
  }

  destroy() {
    this.systemLabels.destroy({ children: true });
    this.regionLabels.destroy({ children: true });
    this.groupLabels.destroy({ children: true });
  }
}
