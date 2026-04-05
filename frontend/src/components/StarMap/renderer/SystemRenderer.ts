import { Application, Container, Sprite, Texture, Graphics } from 'pixi.js';
import type { SystemData } from '../types';
import { LODTier } from '../types';
import { securityColor } from '../utils/colors';
import { NODE_SCREEN_PX, NODE_TEXTURE_RADIUS } from '../utils/constants';

export class SystemRenderer {
  readonly container = new Container();
  private sprites = new Map<number, Sprite>();
  private systems: SystemData[] = [];
  private circleTexture: Texture | null = null;
  private currentLOD: LODTier = LODTier.Galaxy;
  private currentBaseScale = 0.1;

  // Selection / hover state
  private hoveredId: number | null = null;
  private selectedId: number | null = null;

  init(app: Application, systems: SystemData[]) {
    this.systems = systems;
    this.container.label = 'systems';
    this.container.cullable = true;

    // Generate shared circle texture
    const g = new Graphics();
    g.circle(0, 0, NODE_TEXTURE_RADIUS);
    g.fill({ color: 0xffffff });
    this.circleTexture = app.renderer.generateTexture({
      target: g,
      resolution: 2,
    });
    g.destroy();

    // Create sprites
    for (const sys of systems) {
      const sprite = new Sprite(this.circleTexture);
      sprite.anchor.set(0.5);
      sprite.position.set(sys.x, sys.y);
      sprite.tint = securityColor(sys.sec);
      sprite.scale.set(0.1); // will be set properly by updateScale()
      sprite.label = String(sys.id);
      this.container.addChild(sprite);
      this.sprites.set(sys.id, sprite);
    }
  }

  /** Recompute sprite scale for screen-space consistency. Call on every zoom change. */
  updateScale(viewportScale: number) {
    const targetPx = NODE_SCREEN_PX[this.currentLOD];
    const baseScale = targetPx / (NODE_TEXTURE_RADIUS * viewportScale);
    this.currentBaseScale = baseScale;

    for (const [id, sprite] of this.sprites) {
      let s = baseScale;
      if (id === this.hoveredId) s *= 1.5;
      if (id === this.selectedId) s *= 2;
      sprite.scale.set(s);
    }
  }

  updateLOD(tier: LODTier) {
    this.currentLOD = tier;
    // Scale will be recalculated by updateScale() called from StarMap
  }

  setHovered(systemId: number | null) {
    // Restore previous
    if (this.hoveredId !== null && this.hoveredId !== this.selectedId) {
      const prev = this.sprites.get(this.hoveredId);
      if (prev) prev.scale.set(this.currentBaseScale);
    }

    this.hoveredId = systemId;

    if (systemId !== null) {
      const sprite = this.sprites.get(systemId);
      if (sprite) sprite.scale.set(this.currentBaseScale * 1.5);
    }
  }

  setSelected(systemId: number | null) {
    // Restore previous
    if (this.selectedId !== null) {
      const prev = this.sprites.get(this.selectedId);
      if (prev) prev.scale.set(this.currentBaseScale);
    }

    this.selectedId = systemId;

    if (systemId !== null) {
      const sprite = this.sprites.get(systemId);
      if (sprite) sprite.scale.set(this.currentBaseScale * 2);
    }
  }

  setOverlayTint(tints: Map<number, number> | null) {
    if (!tints) {
      for (const sys of this.systems) {
        const sprite = this.sprites.get(sys.id);
        if (sprite) sprite.tint = securityColor(sys.sec);
      }
      return;
    }

    for (const sys of this.systems) {
      const sprite = this.sprites.get(sys.id);
      if (!sprite) continue;
      sprite.tint = tints.get(sys.id) ?? 0x222233;
    }
  }

  /** Show only a subset of systems (null = show all). */
  setVisibleSystems(ids: Set<number> | null) {
    for (const [id, sprite] of this.sprites) {
      sprite.visible = ids === null || ids.has(id);
    }
  }

  getSprite(systemId: number): Sprite | undefined {
    return this.sprites.get(systemId);
  }

  destroy() {
    this.container.destroy({ children: true });
    this.circleTexture?.destroy(true);
  }
}
