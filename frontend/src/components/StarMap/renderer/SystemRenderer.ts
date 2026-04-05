import { Application, Container, Sprite, Texture, Graphics } from 'pixi.js';
import type { SystemData } from '../types';
import { LODTier } from '../types';
import { securityColor } from '../utils/colors';
import { NODE_SCREEN_PX, NODE_TEXTURE_RADIUS, GLOW_TEXTURE_RADIUS } from '../utils/constants';

export class SystemRenderer {
  readonly container = new Container();
  private sprites = new Map<number, Sprite>();
  private glowSprites = new Map<number, Sprite>();
  private systems: SystemData[] = [];
  private circleTexture: Texture | null = null;
  private glowTexture: Texture | null = null;
  private currentLOD: LODTier = LODTier.Galaxy;
  private currentBaseScale = 0.1;
  // Selection / hover state
  private hoveredId: number | null = null;
  private selectedId: number | null = null;

  // Neighbor highlight state
  private highlightedNeighbors: Set<number> | null = null;
  private targetAlphas = new Map<number, number>(); // for smooth transitions

  // Selection ring
  private selectionRing = new Graphics();
  private selectionPhase = 0;

  init(app: Application, systems: SystemData[]) {
    this.systems = systems;
    this.container.label = 'systems';
    this.container.cullable = true;

    // Core circle texture (sharp dot)
    const g = new Graphics();
    g.circle(0, 0, NODE_TEXTURE_RADIUS);
    g.fill({ color: 0xffffff });
    this.circleTexture = app.renderer.generateTexture({ target: g, resolution: 2 });
    g.destroy();

    // Glow texture (soft falloff)
    const gg = new Graphics();
    // Center bright core
    gg.circle(0, 0, GLOW_TEXTURE_RADIUS * 0.3);
    gg.fill({ color: 0xffffff, alpha: 0.2 });
    // Outer soft glow
    gg.circle(0, 0, GLOW_TEXTURE_RADIUS);
    gg.fill({ color: 0xffffff, alpha: 0.06 });
    this.glowTexture = app.renderer.generateTexture({ target: gg, resolution: 1 });
    gg.destroy();

    // Create sprites (glow behind core)
    for (const sys of systems) {
      const glow = new Sprite(this.glowTexture);
      glow.anchor.set(0.5);
      glow.position.set(sys.x, sys.y);
      glow.tint = securityColor(sys.sec);
      glow.scale.set(0.1);
      this.container.addChild(glow);
      this.glowSprites.set(sys.id, glow);

      const sprite = new Sprite(this.circleTexture);
      sprite.anchor.set(0.5);
      sprite.position.set(sys.x, sys.y);
      sprite.tint = securityColor(sys.sec);
      sprite.scale.set(0.1);
      sprite.label = String(sys.id);
      this.container.addChild(sprite);
      this.sprites.set(sys.id, sprite);
    }

    // Selection ring (added on top, repositioned when active)
    this.selectionRing.visible = false;
    this.container.addChild(this.selectionRing);
  }

  updateScale(viewportScale: number) {
    const targetPx = NODE_SCREEN_PX[this.currentLOD];
    const baseScale = targetPx / (NODE_TEXTURE_RADIUS * viewportScale);
    this.currentBaseScale = baseScale;
    const glowScale = (targetPx * 2.5) / (GLOW_TEXTURE_RADIUS * viewportScale);

    for (const [id, sprite] of this.sprites) {
      let s = baseScale;
      if (id === this.hoveredId) s *= 1.5;
      sprite.scale.set(s);

      const glow = this.glowSprites.get(id);
      if (glow) glow.scale.set(glowScale);
    }
  }

  updateLOD(tier: LODTier) {
    this.currentLOD = tier;
  }

  /** Call each frame to animate selection ring + smooth alpha transitions. */
  tick(delta: number) {
    // Selection ring pulse
    if (this.selectedId !== null && this.selectionRing.visible) {
      this.selectionPhase += delta * 0.04;
      const pulse = 0.5 + 0.5 * Math.sin(this.selectionPhase);
      const ringScale = (1.0 + pulse * 0.4) * this.currentBaseScale * 3;
      this.selectionRing.scale.set(ringScale);
      this.selectionRing.alpha = 0.3 + pulse * 0.5;
    }

    // Smooth alpha transitions for neighbor highlighting
    if (this.targetAlphas.size > 0) {
      const speed = delta * 0.15; // ~200ms to full transition
      for (const [id, target] of this.targetAlphas) {
        const sprite = this.sprites.get(id);
        const glow = this.glowSprites.get(id);
        if (!sprite) continue;
        const current = sprite.alpha;
        const diff = target - current;
        if (Math.abs(diff) < 0.01) {
          sprite.alpha = target;
          if (glow) glow.alpha = target;
          if (target === 1) this.targetAlphas.delete(id);
        } else {
          const next = current + diff * speed;
          sprite.alpha = next;
          if (glow) glow.alpha = next;
        }
      }
    }
  }

  setHovered(systemId: number | null) {
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

  /** Highlight hovered system + its neighbors, dim everything else. */
  setHoverHighlight(systemId: number | null, neighborIds: Set<number> | null) {
    if (systemId === null || neighborIds === null) {
      // Restore all to full alpha (smoothly)
      if (this.highlightedNeighbors !== null) {
        for (const sys of this.systems) {
          this.targetAlphas.set(sys.id, 1);
        }
      }
      this.highlightedNeighbors = null;
      return;
    }

    this.highlightedNeighbors = neighborIds;

    for (const sys of this.systems) {
      if (sys.id === systemId || neighborIds.has(sys.id)) {
        this.targetAlphas.set(sys.id, 1);
      } else {
        this.targetAlphas.set(sys.id, 0.12);
      }
    }
  }

  /** Highlight a set of systems (for jump planner), dim everything else. */
  setJumpRangeHighlight(originId: number | null, highlightIds: Set<number> | null) {
    if (highlightIds === null || highlightIds.size === 0) {
      // Restore alphas smoothly
      for (const sys of this.systems) {
        this.targetAlphas.set(sys.id, 1);
      }
      return;
    }

    for (const sys of this.systems) {
      if (sys.id === originId || highlightIds.has(sys.id)) {
        this.targetAlphas.set(sys.id, 1);
      } else {
        // Keep systems visible enough to see sec colors
        this.targetAlphas.set(sys.id, 0.25);
      }
    }
  }

  setSelected(systemId: number | null) {
    // Restore previous
    if (this.selectedId !== null) {
      const prev = this.sprites.get(this.selectedId);
      if (prev) prev.scale.set(this.currentBaseScale);
    }

    this.selectedId = systemId;
    this.selectionPhase = 0;

    if (systemId !== null) {
      const sys = this.systems.find(s => s.id === systemId);
      if (sys) {
        // Position and show selection ring
        this.selectionRing.clear();
        this.selectionRing.circle(0, 0, 6);
        this.selectionRing.stroke({ width: 1.5, color: 0xc8a951, alpha: 0.8 });
        this.selectionRing.position.set(sys.x, sys.y);
        this.selectionRing.visible = true;
      }
    } else {
      this.selectionRing.visible = false;
    }
  }

  setOverlayTint(tints: Map<number, number> | null) {
    if (!tints) {
      for (const sys of this.systems) {
        const sprite = this.sprites.get(sys.id);
        const glow = this.glowSprites.get(sys.id);
        const color = securityColor(sys.sec);
        if (sprite) sprite.tint = color;
        if (glow) glow.tint = color;
      }
      return;
    }
    for (const sys of this.systems) {
      const sprite = this.sprites.get(sys.id);
      const glow = this.glowSprites.get(sys.id);
      const color = tints.get(sys.id) ?? 0x222233;
      if (sprite) sprite.tint = color;
      if (glow) glow.tint = color;
    }
  }

  setVisibleSystems(ids: Set<number> | null) {
    for (const [id, sprite] of this.sprites) {
      const vis = ids === null || ids.has(id);
      sprite.visible = vis;
      const glow = this.glowSprites.get(id);
      if (glow) glow.visible = vis;
    }
  }

  getSprite(systemId: number): Sprite | undefined {
    return this.sprites.get(systemId);
  }

  destroy() {
    this.container.destroy({ children: true });
    this.circleTexture?.destroy(true);
    this.glowTexture?.destroy(true);
  }
}
