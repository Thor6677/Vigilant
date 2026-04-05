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
  systemData: SystemData;
}

export type GroupMode = 'systems' | 'constellation' | 'region';

export interface GroupData {
  id: number;
  name: string;
  cx: number;
  cy: number;
  count: number;
  systemIds: Set<number>;
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
  private expandedGroupId: number | null = null;

  // Pre-computed group data
  constellationGroups: GroupData[] = [];
  regionGroups: GroupData[] = [];
  // Quick lookup: system ID → which group it belongs to (by current mode)
  private systemToConGroup = new Map<number, number>();
  private systemToRegGroup = new Map<number, number>();

  init(systems: SystemData[], regions: RegionData[]) {
    this.systems = systems;
    this.systemLabels.label = 'systemLabels';
    this.systemLabels.cullable = true;
    this.regionLabels.label = 'regionLabels';
    this.regionLabels.cullable = true;
    this.groupLabels.label = 'groupLabels';
    this.groupLabels.cullable = true;

    BitmapFont.install({
      name: 'MapFont',
      style: { fontFamily: 'JetBrains Mono, monospace', fontSize: 14, fill: '#9a9a9a' },
    });
    BitmapFont.install({
      name: 'SecFont',
      style: { fontFamily: 'JetBrains Mono, monospace', fontSize: 14, fill: '#ffffff' },
    });
    BitmapFont.install({
      name: 'RegionFont',
      style: { fontFamily: 'JetBrains Mono, monospace', fontSize: 22, fill: '#3a3a3a', fontWeight: 'bold', letterSpacing: 2 },
    });
    BitmapFont.install({
      name: 'GroupFont',
      style: { fontFamily: 'JetBrains Mono, monospace', fontSize: 16, fill: '#7a7a7a' },
    });

    // System labels: name + color-coded sec value
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
      this.systemLabelMap.set(sys.id, { container: cont, nameText, secText, systemData: sys });
    }

    // Region labels (for systems mode)
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

    this.computeGroups(systems);
  }

  private computeGroups(systems: SystemData[]) {
    const conMap = new Map<number, { name: string; systems: SystemData[] }>();
    const regMap = new Map<number, { name: string; systems: SystemData[] }>();

    for (const sys of systems) {
      // Constellation
      let cg = conMap.get(sys.conId);
      if (!cg) { cg = { name: sys.conName, systems: [] }; conMap.set(sys.conId, cg); }
      cg.systems.push(sys);
      this.systemToConGroup.set(sys.id, sys.conId);

      // Region
      let rg = regMap.get(sys.regId);
      if (!rg) { rg = { name: sys.regName, systems: [] }; regMap.set(sys.regId, rg); }
      rg.systems.push(sys);
      this.systemToRegGroup.set(sys.id, sys.regId);
    }

    const toGroupData = (id: number, g: { name: string; systems: SystemData[] }): GroupData => ({
      id,
      name: g.name,
      count: g.systems.length,
      systemIds: new Set(g.systems.map(s => s.id)),
      cx: g.systems.reduce((s, sys) => s + sys.x, 0) / g.systems.length,
      cy: g.systems.reduce((s, sys) => s + sys.y, 0) / g.systems.length,
      minX: Math.min(...g.systems.map(s => s.x)),
      minY: Math.min(...g.systems.map(s => s.y)),
      maxX: Math.max(...g.systems.map(s => s.x)),
      maxY: Math.max(...g.systems.map(s => s.y)),
    });

    this.constellationGroups = Array.from(conMap.entries()).map(([id, g]) => toGroupData(id, g));
    this.regionGroups = Array.from(regMap.entries()).map(([id, g]) => toGroupData(id, g));
  }

  setGroupMode(mode: GroupMode) {
    this.groupMode = mode;
    this.expandedGroupId = null;
    this.rebuildGroupLabels();
  }

  /** Expand a specific group — show its systems, collapse everything else. */
  setExpandedGroup(groupId: number | null) {
    this.expandedGroupId = groupId;
    this.rebuildGroupLabels();
  }

  getExpandedGroupId(): number | null {
    return this.expandedGroupId;
  }

  /** Returns the set of system IDs that should be visible (for SystemRenderer/EdgeRenderer). */
  getVisibleSystemIds(): Set<number> | null {
    if (this.groupMode === 'systems') return null; // all visible
    if (this.expandedGroupId === null) return new Set(); // none visible (all collapsed)

    const groups = this.groupMode === 'constellation' ? this.constellationGroups : this.regionGroups;
    const expanded = groups.find(g => g.id === this.expandedGroupId);
    return expanded ? expanded.systemIds : new Set();
  }

  private rebuildGroupLabels() {
    this.groupLabels.removeChildren();

    if (this.groupMode === 'systems') {
      this.groupLabels.visible = false;
      return;
    }

    this.groupLabels.visible = true;
    const groups = this.groupMode === 'constellation' ? this.constellationGroups : this.regionGroups;

    for (const group of groups) {
      // Skip the expanded group — its systems are shown individually
      if (group.id === this.expandedGroupId) continue;

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

    if (this.groupMode !== 'systems') {
      // Group mode: show group labels, system labels only for expanded group
      this.regionLabels.visible = false;
      this.groupLabels.visible = true;
      this.systemLabels.visible = this.expandedGroupId !== null;
      return;
    }

    // Systems mode
    this.groupLabels.visible = false;
    this.systemLabels.visible = tier >= LODTier.Region;

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

  updateViewport(vp: Viewport) {
    const scale = vp.scaled;
    const bounds = vp.getVisibleBounds();
    const pad = 200 / scale;
    const minX = bounds.x - pad;
    const maxX = bounds.x + bounds.width + pad;
    const minY = bounds.y - pad;
    const maxY = bounds.y + bounds.height + pad;

    // System labels: scale + viewport cull + group filter
    if (this.systemLabels.visible && this._tier >= LODTier.Region) {
      const targetScreenPx = 10 + Math.min(4, scale * 2);
      const labelScale = targetScreenPx / (14 * scale);

      const expandedSystems = this.groupMode !== 'systems' ? this.getVisibleSystemIds() : null;

      for (const sys of this.systems) {
        const sl = this.systemLabelMap.get(sys.id);
        if (!sl) continue;

        // In group mode, only show labels for expanded group's systems
        if (expandedSystems !== null && !expandedSystems.has(sys.id)) {
          sl.container.visible = false;
          continue;
        }

        const inView = sys.x >= minX && sys.x <= maxX && sys.y >= minY && sys.y <= maxY;
        sl.container.visible = inView;
        if (inView) {
          sl.container.scale.set(labelScale);
        }
      }
    }

    // Group labels: scale + viewport cull
    if (this.groupLabels.visible) {
      const targetScreenPx = 12 + Math.min(6, scale * 3);
      const groupScale = targetScreenPx / (16 * scale);

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
