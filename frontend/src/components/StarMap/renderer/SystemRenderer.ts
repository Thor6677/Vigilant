import { Application, Container, Sprite, Texture, Graphics } from 'pixi.js';
import type { SystemData } from '../types';
import { LODTier } from '../types';
import { securityColor } from '../utils/colors';
import { NODE_SIZES } from '../utils/constants';

export class SystemRenderer {
  readonly container = new Container();
  private sprites = new Map<number, Sprite>();
  private systems: SystemData[] = [];
  private circleTexture: Texture | null = null;
  private currentLOD: LODTier = LODTier.Galaxy;

  // Selection / hover state
  private hoveredId: number | null = null;
  private selectedId: number | null = null;

  init(app: Application, systems: SystemData[]) {
    this.systems = systems;
    this.container.label = 'systems';
    this.container.cullable = true;

    // Generate shared circle texture (32x32 soft circle)
    const g = new Graphics();
    g.circle(0, 0, 16);
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
      sprite.scale.set(NODE_SIZES[LODTier.Galaxy] / 16);
      sprite.label = String(sys.id);
      this.container.addChild(sprite);
      this.sprites.set(sys.id, sprite);
    }
  }

  updateLOD(tier: LODTier) {
    if (tier === this.currentLOD) return;
    this.currentLOD = tier;

    const size = NODE_SIZES[tier];
    const scale = size / 16;

    for (const sprite of this.sprites.values()) {
      sprite.scale.set(scale);
    }
  }

  setHovered(systemId: number | null) {
    // Restore previous hovered
    if (this.hoveredId !== null && this.hoveredId !== this.selectedId) {
      const prev = this.sprites.get(this.hoveredId);
      if (prev) {
        const size = NODE_SIZES[this.currentLOD];
        prev.scale.set(size / 16);
        prev.alpha = 1;
      }
    }

    this.hoveredId = systemId;

    if (systemId !== null) {
      const sprite = this.sprites.get(systemId);
      if (sprite) {
        const size = NODE_SIZES[this.currentLOD];
        sprite.scale.set((size * 1.5) / 16);
      }
    }
  }

  setSelected(systemId: number | null) {
    // Restore previous selected
    if (this.selectedId !== null) {
      const prev = this.sprites.get(this.selectedId);
      if (prev) {
        const size = NODE_SIZES[this.currentLOD];
        prev.scale.set(size / 16);
        prev.alpha = 1;
      }
    }

    this.selectedId = systemId;

    if (systemId !== null) {
      const sprite = this.sprites.get(systemId);
      if (sprite) {
        const size = NODE_SIZES[this.currentLOD];
        sprite.scale.set((size * 2) / 16);
      }
    }
  }

  setOverlayTint(tints: Map<number, number> | null) {
    if (!tints) {
      // Restore security tints
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

  getSprite(systemId: number): Sprite | undefined {
    return this.sprites.get(systemId);
  }

  destroy() {
    this.container.destroy({ children: true });
    this.circleTexture?.destroy(true);
  }
}
