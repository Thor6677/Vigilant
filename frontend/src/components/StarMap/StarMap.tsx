import {
  useRef,
  useEffect,
  useCallback,
  useState,
  useMemo,
  useImperativeHandle,
  forwardRef,
} from 'react';
import { Application } from 'pixi.js';
import { Viewport } from 'pixi-viewport';
import { quadtree, type Quadtree } from 'd3-quadtree';
import Graph from 'graphology';

import type { SystemData, MapData, RoutePreference, OverlayType, GroupMode } from './types';
import { LODTier } from './types';
import { LOD_THRESHOLDS, MIN_ZOOM, MAX_ZOOM, CANVAS_SIZE, BG_COLOR } from './utils/constants';
import { heatmapColor, allianceColor, FACTION_COLORS } from './utils/colors';
import { SystemRenderer } from './renderer/SystemRenderer';
import { EdgeRenderer } from './renderer/EdgeRenderer';
import { LabelRenderer } from './renderer/LabelRenderer';
import { RouteRenderer } from './renderer/RouteRenderer';
import { buildGraph } from './graph/buildGraph';
import { findRoute } from './graph/pathfinding';
import { useOverlayData } from './useOverlayData';
import { useCharacterLocations } from './useCharacterLocations';

import { SystemInfoPanel } from './ui/SystemInfoPanel';
import { SystemSearch } from './ui/SystemSearch';
import { MapToolbar } from './ui/MapToolbar';
import { OverlayControls } from './ui/OverlayControls';
import { GroupModeControls } from './ui/GroupModeControls';
import type { GroupData } from './renderer/LabelRenderer';

export interface StarMapHandle {
  focusSystem: (id: number) => void;
  setRoute: (systemIds: number[]) => void;
  getViewport: () => Viewport | null;
}

interface StarMapProps {
  data: MapData;
  onSystemClick?: (system: SystemData) => void;
}

export const StarMap = forwardRef<StarMapHandle, StarMapProps>(({ data, onSystemClick }, ref) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const appRef = useRef<Application | null>(null);
  const viewportRef = useRef<Viewport | null>(null);
  const graphRef = useRef<Graph | null>(null);
  const adjacencyRef = useRef<Map<number, Set<number>> | null>(null);
  const tooltipTimerRef = useRef<number | null>(null);
  const [tooltipVisible, setTooltipVisible] = useState(false);
  const qtRef = useRef<Quadtree<SystemData> | null>(null);

  const systemRendererRef = useRef<SystemRenderer | null>(null);
  const edgeRendererRef = useRef<EdgeRenderer | null>(null);
  const labelRendererRef = useRef<LabelRenderer | null>(null);
  const routeRendererRef = useRef<RouteRenderer | null>(null);

  const [selectedSystem, setSelectedSystem] = useState<SystemData | null>(null);
  const [hoveredSystem, setHoveredSystem] = useState<SystemData | null>(null);
  const [tooltipPos, setTooltipPos] = useState<{ x: number; y: number } | null>(null);
  const [panelPos, setPanelPos] = useState<{ x: number; y: number } | null>(null);
  const [routeOrigin, setRouteOrigin] = useState<number | null>(null);
  const [routeDest, setRouteDest] = useState<number | null>(null);
  const [activeRoute, setActiveRoute] = useState<number[] | null>(null);
  const [routePreference, setRoutePreference] = useState<RoutePreference>('shortest');
  const [activeOverlay, setActiveOverlay] = useState<OverlayType>('security');
  const [groupMode, setGroupMode] = useState<GroupMode>('systems');
  const [overlayBarHeight, setOverlayBarHeight] = useState(36);
  const overlayBarRef = useRef<HTMLDivElement>(null);
  const currentLODRef = useRef<LODTier>(LODTier.Galaxy);
  const selectedSystemRef = useRef<SystemData | null>(null);

  // Keep ref in sync for use in Pixi callbacks
  selectedSystemRef.current = selectedSystem;

  // Fetch ESI overlay stats
  const { stats, loading: statsLoading } = useOverlayData();
  const { characters } = useCharacterLocations();

  // Compute overlay tints when overlay or stats change
  const overlayTints = useMemo(() => {
    if (activeOverlay === 'security' || !stats) return null;

    const tints = new Map<number, number>();

    if (activeOverlay === 'jumps') {
      const values = Object.values(stats.jumps);
      const max = Math.max(1, ...values);
      for (const sys of data.systems) {
        const v = stats.jumps[String(sys.id)] ?? 0;
        tints.set(sys.id, heatmapColor(Math.sqrt(v / max)));
      }
    } else if (activeOverlay === 'shipKills') {
      const values = Object.values(stats.kills).map(k => k.ship);
      const max = Math.max(1, ...values);
      for (const sys of data.systems) {
        const k = stats.kills[String(sys.id)];
        tints.set(sys.id, heatmapColor(Math.sqrt((k?.ship ?? 0) / max)));
      }
    } else if (activeOverlay === 'podKills') {
      const values = Object.values(stats.kills).map(k => k.pod);
      const max = Math.max(1, ...values);
      for (const sys of data.systems) {
        const k = stats.kills[String(sys.id)];
        tints.set(sys.id, heatmapColor(Math.sqrt((k?.pod ?? 0) / max)));
      }
    } else if (activeOverlay === 'npcKills') {
      const values = Object.values(stats.kills).map(k => k.npc);
      const max = Math.max(1, ...values);
      for (const sys of data.systems) {
        const k = stats.kills[String(sys.id)];
        tints.set(sys.id, heatmapColor(Math.sqrt((k?.npc ?? 0) / max)));
      }
    } else if (activeOverlay === 'sovereignty') {
      for (const sys of data.systems) {
        const sov = stats.sovereignty[String(sys.id)];
        if (sov?.alliance_id) {
          tints.set(sys.id, allianceColor(sov.alliance_id));
        } else if (sov?.faction_id) {
          tints.set(sys.id, FACTION_COLORS[sov.faction_id] ?? 0x555577);
        } else {
          tints.set(sys.id, 0x222233);
        }
      }
    } else if (activeOverlay === 'incursions') {
      // Build set of infested systems
      const infested = new Set<number>();
      const staging = new Set<number>();
      for (const inc of stats.incursions) {
        for (const sid of inc.systems) infested.add(sid);
        if (inc.staging_system_id) staging.add(inc.staging_system_id);
      }
      for (const sys of data.systems) {
        if (staging.has(sys.id)) {
          tints.set(sys.id, 0xff8800);
        } else if (infested.has(sys.id)) {
          tints.set(sys.id, 0xff4444);
        } else {
          tints.set(sys.id, 0x1a1a2a);
        }
      }
    } else if (activeOverlay === 'factionWarfare') {
      for (const sys of data.systems) {
        const fw = stats.fw[String(sys.id)];
        if (fw?.occupier) {
          const base = FACTION_COLORS[fw.occupier] ?? 0x555577;
          tints.set(sys.id, fw.contested !== 'uncontested' ? brighten(base, 1.4) : base);
        } else {
          tints.set(sys.id, 0x1a1a2a);
        }
      }
    }

    return tints;
  }, [activeOverlay, stats, data.systems]);

  // Apply overlay tints to renderer
  useEffect(() => {
    systemRendererRef.current?.setOverlayTint(overlayTints);
  }, [overlayTints]);

  // Apply group mode changes
  useEffect(() => {
    const lr = labelRendererRef.current;
    const sr = systemRendererRef.current;
    const er = edgeRendererRef.current;
    if (!lr || !sr || !er) return;

    lr.setGroupMode(groupMode);

    const visibleIds = lr.getVisibleSystemIds();
    sr.container.visible = true;
    sr.setVisibleSystems(visibleIds);
    er.setVisibleSystems(visibleIds);

    // Re-trigger LOD update
    if (viewportRef.current) {
      lr.updateLOD(currentLODRef.current, viewportRef.current.scaled);
      lr.updateViewport(viewportRef.current);
    }
  }, [groupMode]);

  // Handle group expansion — called when a group is clicked
  const handleExpandGroup = useCallback((groupId: number) => {
    const lr = labelRendererRef.current;
    const sr = systemRendererRef.current;
    const er = edgeRendererRef.current;
    if (!lr || !sr || !er) return;

    // Toggle: clicking the already-expanded group collapses it
    const newId = lr.getExpandedGroupId() === groupId ? null : groupId;
    lr.setExpandedGroup(newId);

    // Update system and edge visibility to match
    const visibleIds = lr.getVisibleSystemIds();
    sr.setVisibleSystems(visibleIds);
    er.setVisibleSystems(visibleIds);

    // Re-trigger viewport update for labels
    if (viewportRef.current) {
      lr.updateLOD(currentLODRef.current, viewportRef.current.scaled);
      lr.updateViewport(viewportRef.current);
    }

    // If expanding, zoom to fit the group
    if (newId !== null && viewportRef.current) {
      const groups = lr.getCurrentGroupMode() === 'constellation'
        ? lr.constellationGroups : lr.regionGroups;
      const group = groups.find(g => g.id === newId);
      if (group) {
        const pad = 80;
        const gw = group.maxX - group.minX + pad * 2;
        const gh = group.maxY - group.minY + pad * 2;
        const cx = (group.minX + group.maxX) / 2;
        const cy = (group.minY + group.maxY) / 2;
        const sx = viewportRef.current.screenWidth / gw;
        const sy = viewportRef.current.screenHeight / gh;
        const targetScale = Math.min(sx, sy, MAX_ZOOM);

        viewportRef.current.animate({
          position: { x: cx, y: cy },
          scale: targetScale,
          time: 600,
          ease: 'easeInOutCubic',
        });
      }
    }
  }, []);

  // Measure overlay bar height
  useEffect(() => {
    if (!overlayBarRef.current) return;
    const obs = new ResizeObserver(([entry]) => {
      setOverlayBarHeight(entry.contentRect.height);
    });
    obs.observe(overlayBarRef.current);
    return () => obs.disconnect();
  }, []);

  // Expose imperative API
  useImperativeHandle(ref, () => ({
    focusSystem: (id: number) => {
      const sys = data.systemMap.get(id);
      if (sys && viewportRef.current) {
        viewportRef.current.animate({
          position: { x: sys.x, y: sys.y },
          scale: 2,
          time: 600,
          ease: 'easeInOutCubic',
        });
        setSelectedSystem(sys);
        systemRendererRef.current?.setSelected(id);
      }
    },
    setRoute: (systemIds: number[]) => {
      routeRendererRef.current?.setRoute(systemIds);
      setActiveRoute(systemIds);
    },
    getViewport: () => viewportRef.current,
  }));

  // LOD detection + scale update
  const updateView = useCallback((vp: Viewport) => {
    const scale = vp.scaled;
    let tier: LODTier = LODTier.Galaxy;
    if (scale >= LOD_THRESHOLDS[LODTier.System]) tier = LODTier.System;
    else if (scale >= LOD_THRESHOLDS[LODTier.Constellation]) tier = LODTier.Constellation;
    else if (scale >= LOD_THRESHOLDS[LODTier.Region]) tier = LODTier.Region;

    if (tier !== currentLODRef.current) {
      currentLODRef.current = tier;
      systemRendererRef.current?.updateLOD(tier);
      edgeRendererRef.current?.updateLOD(tier);
      labelRendererRef.current?.updateLOD(tier, scale);
    }

    // Always update node scale for screen-space consistency
    systemRendererRef.current?.updateScale(scale);

    // Update label viewport culling
    labelRendererRef.current?.updateViewport(vp);

    // Update panel position if a system is selected
    const sel = selectedSystemRef.current;
    if (sel) {
      const sp = vp.toScreen(sel.x, sel.y);
      setPanelPos({ x: sp.x, y: sp.y });
    }
  }, []);

  // Initialize Pixi
  useEffect(() => {
    if (!containerRef.current) return;

    const el = containerRef.current;
    let destroyed = false;

    async function init() {
      const app = new Application();
      await app.init({
        background: BG_COLOR,
        resizeTo: el,
        antialias: true,
        autoDensity: true,
        resolution: window.devicePixelRatio || 1,
      });

      if (destroyed) {
        app.destroy(true);
        return;
      }

      el.appendChild(app.canvas as HTMLCanvasElement);
      appRef.current = app;

      const vp = new Viewport({
        screenWidth: el.clientWidth,
        screenHeight: el.clientHeight,
        worldWidth: CANVAS_SIZE,
        worldHeight: CANVAS_SIZE,
        events: app.renderer.events,
      });

      vp.drag({ mouseButtons: 'left' })
        .pinch()
        .wheel({ smooth: 10 })
        .decelerate({ friction: 0.92 })
        .clampZoom({ minScale: MIN_ZOOM, maxScale: MAX_ZOOM })
        .clamp({ direction: 'all', underflow: 'center' });

      app.stage.addChild(vp);
      viewportRef.current = vp;

      // Build renderers
      const systemRenderer = new SystemRenderer();
      const edgeRenderer = new EdgeRenderer();
      const labelRenderer = new LabelRenderer();
      const routeRenderer = new RouteRenderer();

      systemRendererRef.current = systemRenderer;
      edgeRendererRef.current = edgeRenderer;
      labelRendererRef.current = labelRenderer;
      routeRendererRef.current = routeRenderer;

      // Init renderers
      edgeRenderer.init(data.systemMap, data.edges);
      edgeRenderer.initHoverLayer();
      systemRenderer.init(app, data.systems);
      labelRenderer.init(data.systems, data.regions);
      routeRenderer.init(data.systemMap);

      // Add to viewport in draw order: edges → region labels → group labels → systems → system labels → route
      vp.addChild(edgeRenderer.container);
      vp.addChild(labelRenderer.regionLabels);
      vp.addChild(labelRenderer.groupLabels);
      vp.addChild(systemRenderer.container);
      vp.addChild(labelRenderer.systemLabels);
      vp.addChild(routeRenderer.graphics);

      // Build quadtree
      const qt = quadtree<SystemData>()
        .x(d => d.x)
        .y(d => d.y)
        .addAll(data.systems);
      qtRef.current = qt;

      // Build graph + adjacency map
      const { graph, adjacency } = buildGraph(data.systems, data.edges);
      graphRef.current = graph;
      adjacencyRef.current = adjacency;

      // Initial view setup
      vp.fit(true);
      vp.moveCenter(CANVAS_SIZE / 2, CANVAS_SIZE / 2);
      updateView(vp);

      // Update on zoom/pan
      vp.on('zoomed', () => updateView(vp));
      vp.on('moved', () => {
        // Update labels and panel on pan
        labelRendererRef.current?.updateViewport(vp);
        const sel = selectedSystemRef.current;
        if (sel) {
          const sp = vp.toScreen(sel.x, sel.y);
          setPanelPos({ x: sp.x, y: sp.y });
        }
      });

      // Close panel on drag
      vp.on('drag-start', () => {
        setSelectedSystem(null);
        setPanelPos(null);
        systemRenderer.setSelected(null);
      });

      // Pointer events
      let hoverThrottleId: number | null = null;

      vp.on('pointermove', (e) => {
        if (hoverThrottleId !== null) return;
        hoverThrottleId = window.setTimeout(() => {
          hoverThrottleId = null;
        }, 16);

        const worldPos = vp.toWorld(e.global);
        const hitRadius = 30 / vp.scaled;
        const found = qt.find(worldPos.x, worldPos.y, hitRadius);

        if (found) {
          setHoveredSystem(prev => {
            // Start tooltip delay when hovering a new system
            if (!prev || prev.id !== found.id) {
              setTooltipVisible(false);
              if (tooltipTimerRef.current) clearTimeout(tooltipTimerRef.current);
              tooltipTimerRef.current = window.setTimeout(() => setTooltipVisible(true), 150);
            }
            return found;
          });
          const screenPos = vp.toScreen(found.x, found.y);
          setTooltipPos({ x: screenPos.x, y: screenPos.y });
          systemRenderer.setHovered(found.id);
          el.style.cursor = 'pointer';

          // Neighbor highlighting
          const neighbors = adjacencyRef.current?.get(found.id);
          if (neighbors) {
            systemRenderer.setHoverHighlight(found.id, neighbors);
            edgeRenderer.setHoverHighlight(found.id, neighbors);
          }
        } else {
          if (tooltipTimerRef.current) clearTimeout(tooltipTimerRef.current);
          setTooltipVisible(false);
          setHoveredSystem(null);
          setTooltipPos(null);
          systemRenderer.setHovered(null);
          systemRenderer.setHoverHighlight(null, null);
          edgeRenderer.setHoverHighlight(null, null);
          el.style.cursor = 'grab';
        }
      });

      vp.on('clicked', (e: any) => {
        const worldPos = e.world;
        const hitRadius = 30 / vp.scaled;

        // In group mode, check for group clicks
        const currentGroupMode = labelRenderer.getCurrentGroupMode();
        if (currentGroupMode !== 'systems') {
          const groups = currentGroupMode === 'constellation'
            ? labelRenderer.constellationGroups
            : labelRenderer.regionGroups;

          // Find nearest group centroid
          let nearestGroup: GroupData | null = null;
          let nearestDist = Infinity;
          for (const g of groups) {
            // Skip the expanded group — its systems are clickable individually
            if (g.id === labelRenderer.getExpandedGroupId()) continue;
            const dx = worldPos.x - g.cx;
            const dy = worldPos.y - g.cy;
            const dist = Math.sqrt(dx * dx + dy * dy);
            if (dist < nearestDist && dist < hitRadius * 3) {
              nearestDist = dist;
              nearestGroup = g;
            }
          }

          if (nearestGroup) {
            handleExpandGroup(nearestGroup.id);
            return;
          }

          // If inside the expanded group, allow normal system clicks
          // (fall through to the system click logic below)
        }

        // Normal system click
        const found = qt.find(worldPos.x, worldPos.y, hitRadius);

        if (found) {
          setSelectedSystem(found);
          const screenPos = vp.toScreen(found.x, found.y);
          setPanelPos({ x: screenPos.x, y: screenPos.y });
          systemRenderer.setSelected(found.id);
          onSystemClick?.(found);
        } else {
          setSelectedSystem(null);
          setPanelPos(null);
          systemRenderer.setSelected(null);
        }
      });

      // Animation loop (route + selection ring + smooth alpha transitions)
      app.ticker.add((ticker) => {
        routeRenderer.tick(ticker.deltaTime);
        systemRenderer.tick(ticker.deltaTime);
      });

      // ResizeObserver
      const resizeObserver = new ResizeObserver(() => {
        if (!destroyed) {
          app.renderer.resize(el.clientWidth, el.clientHeight);
          vp.resize(el.clientWidth, el.clientHeight);
        }
      });
      resizeObserver.observe(el);

      (el as any).__mapCleanup = () => {
        resizeObserver.disconnect();
        if (hoverThrottleId !== null) clearTimeout(hoverThrottleId);
        if (tooltipTimerRef.current) clearTimeout(tooltipTimerRef.current);
      };
    }

    init();

    return () => {
      destroyed = true;
      (el as any).__mapCleanup?.();
      systemRendererRef.current?.destroy();
      edgeRendererRef.current?.destroy();
      labelRendererRef.current?.destroy();
      routeRendererRef.current?.destroy();
      if (viewportRef.current) {
        viewportRef.current.destroy();
        viewportRef.current = null;
      }
      if (appRef.current) {
        appRef.current.destroy(true, { children: true });
        appRef.current = null;
      }
    };
  }, [data, updateView, onSystemClick]);

  // Route calculation
  useEffect(() => {
    if (routeOrigin === null || routeDest === null || !graphRef.current) {
      routeRendererRef.current?.clearRoute();
      setActiveRoute(null);
      return;
    }
    const path = findRoute(graphRef.current, routeOrigin, routeDest, routePreference);
    if (path) {
      routeRendererRef.current?.setRoute(path);
      setActiveRoute(path);
    } else {
      routeRendererRef.current?.clearRoute();
      setActiveRoute(null);
    }
  }, [routeOrigin, routeDest, routePreference]);

  // Keyboard shortcuts
  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement) return;

      switch (e.key) {
        case 'Escape':
          setSelectedSystem(null);
          setPanelPos(null);
          systemRendererRef.current?.setSelected(null);
          break;
        case 'Home':
          if (viewportRef.current) {
            viewportRef.current.fit(true);
            viewportRef.current.moveCenter(CANVAS_SIZE / 2, CANVAS_SIZE / 2);
          }
          break;
        case '+':
        case '=':
          viewportRef.current?.zoomPercent(0.3, true);
          break;
        case '-':
          viewportRef.current?.zoomPercent(-0.3, true);
          break;
      }
    }

    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, []);

  const handleSetOrigin = useCallback((id: number) => setRouteOrigin(id), []);
  const handleSetDest = useCallback((id: number) => setRouteDest(id), []);

  const handleSearchSelectSystem = useCallback((system: SystemData) => {
    if (viewportRef.current) {
      viewportRef.current.animate({
        position: { x: system.x, y: system.y },
        scale: 2,
        time: 600,
        ease: 'easeInOutCubic',
      });
    }
    setSelectedSystem(system);
    systemRendererRef.current?.setSelected(system.id);
    const vp = viewportRef.current;
    if (vp) {
      const sp = vp.toScreen(system.x, system.y);
      setPanelPos({ x: sp.x, y: sp.y });
    }
  }, []);

  const handleSearchSelectArea = useCallback((type: 'constellation' | 'region', id: number) => {
    const lr = labelRendererRef.current;
    if (!lr || !viewportRef.current) return;

    const groups = type === 'constellation' ? lr.constellationGroups : lr.regionGroups;
    const group = groups.find(g => g.id === id);
    if (!group) return;

    // Zoom to fit the area
    const pad = 80;
    const gw = group.maxX - group.minX + pad * 2;
    const gh = group.maxY - group.minY + pad * 2;
    const cx = (group.minX + group.maxX) / 2;
    const cy = (group.minY + group.maxY) / 2;
    const sx = viewportRef.current.screenWidth / gw;
    const sy = viewportRef.current.screenHeight / gh;
    const targetScale = Math.min(sx, sy, MAX_ZOOM);

    viewportRef.current.animate({
      position: { x: cx, y: cy },
      scale: targetScale,
      time: 600,
      ease: 'easeInOutCubic',
    });

    // Clear any system selection
    setSelectedSystem(null);
    setPanelPos(null);
    systemRendererRef.current?.setSelected(null);
  }, []);

  // Tooltip stat info
  const hoveredStatInfo = useMemo(() => {
    if (!hoveredSystem || !stats) return null;
    const sid = String(hoveredSystem.id);
    const k = stats.kills[sid];
    const j = stats.jumps[sid];
    return { jumps: j ?? 0, shipKills: k?.ship ?? 0, npcKills: k?.npc ?? 0, podKills: k?.pod ?? 0 };
  }, [hoveredSystem, stats]);

  // Clamp tooltip to container bounds
  const clampedTooltipPos = useMemo(() => {
    if (!tooltipPos || !containerRef.current) return tooltipPos;
    const cw = containerRef.current.clientWidth;
    const ch = containerRef.current.clientHeight;
    let x = tooltipPos.x + 16;
    let y = tooltipPos.y - 12;
    // Approximate tooltip size
    if (x + 220 > cw) x = tooltipPos.x - 230;
    if (y + 60 > ch) y = tooltipPos.y - 60;
    if (x < 4) x = 4;
    if (y < 4) y = 4;
    return { x, y };
  }, [tooltipPos]);

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%' }}>
      {/* Canvas container — height adjusts for overlay bar */}
      <div ref={containerRef} style={{ width: '100%', height: `calc(100% - ${overlayBarHeight}px)` }} />

      {/* Search bar */}
      <SystemSearch
        systems={data.systems}
        onSelectSystem={handleSearchSelectSystem}
        onSelectArea={handleSearchSelectArea}
      />

      {/* Group mode selector */}
      <GroupModeControls mode={groupMode} onModeChange={setGroupMode} />

      {/* Character location markers */}
      {characters.map(char => {
        if (!char.system_id) return null;
        const sys = data.systemMap.get(char.system_id);
        if (!sys || !viewportRef.current) return null;
        const sp = viewportRef.current.toScreen(sys.x, sys.y);
        return (
          <div
            key={char.character_id}
            style={{
              position: 'absolute',
              left: sp.x,
              top: sp.y,
              transform: 'translate(-50%, -50%)',
              pointerEvents: 'none',
              zIndex: 5,
            }}
          >
            <div style={{
              width: 16, height: 16,
              border: '2px solid #c8a951',
              borderRadius: '50%',
              animation: 'charPulse 2s ease-in-out infinite',
            }} />
            <div style={{
              position: 'absolute', top: -14, left: '50%', transform: 'translateX(-50%)',
              fontSize: 8, fontFamily: "'JetBrains Mono', monospace",
              color: '#c8a951', whiteSpace: 'nowrap', letterSpacing: '0.05em',
            }}>
              {char.character_name}
            </div>
            <style>{`@keyframes charPulse { 0%,100% { opacity: 1; transform: translate(-50%,-50%) scale(1); } 50% { opacity: 0.5; transform: translate(-50%,-50%) scale(1.3); } }`}</style>
          </div>
        );
      })}

      {/* Zoom toolbar */}
      <MapToolbar
        onZoomIn={() => viewportRef.current?.zoomPercent(0.3, true)}
        onZoomOut={() => viewportRef.current?.zoomPercent(-0.3, true)}
        onFitAll={() => {
          viewportRef.current?.fit(true);
          viewportRef.current?.moveCenter(CANVAS_SIZE / 2, CANVAS_SIZE / 2);
        }}
        onLocate={() => {
          const main = characters.find(c => c.is_main) || characters[0];
          if (main?.system_id) {
            const sys = data.systemMap.get(main.system_id);
            if (sys && viewportRef.current) {
              viewportRef.current.animate({
                position: { x: sys.x, y: sys.y },
                scale: 2,
                time: 600,
                ease: 'easeInOutCubic',
              });
            }
          }
        }}
        hasCharacterLocation={characters.some(c => c.system_id !== null)}
      />

      {/* Hover tooltip */}
      {hoveredSystem && clampedTooltipPos && !selectedSystem && (
        <div
          style={{
            position: 'absolute',
            left: clampedTooltipPos.x,
            top: clampedTooltipPos.y,
            pointerEvents: 'none',
            background: 'rgba(8, 8, 8, 0.95)',
            border: '1px solid #191919',
            padding: '5px 10px',
            fontSize: 11,
            fontFamily: "'JetBrains Mono', monospace",
            color: '#dedede',
            whiteSpace: 'nowrap',
            zIndex: 10,
            opacity: tooltipVisible ? 1 : 0,
            transform: tooltipVisible ? 'translateY(0)' : 'translateY(-4px)',
            transition: 'opacity 120ms ease-out, transform 120ms ease-out',
          }}
        >
          <strong>{hoveredSystem.name}</strong>
          <span style={{ marginLeft: 8, color: '#474747' }}>
            {hoveredSystem.sec.toFixed(1)} · {hoveredSystem.regName}
          </span>
          {hoveredStatInfo && (hoveredStatInfo.jumps > 0 || hoveredStatInfo.shipKills > 0 || hoveredStatInfo.npcKills > 0) && (
            <div style={{ fontSize: 9, color: '#5a5a5a', marginTop: 3, display: 'flex', gap: 8 }}>
              {hoveredStatInfo.jumps > 0 && <span>JUMPS {hoveredStatInfo.jumps.toLocaleString()}</span>}
              {hoveredStatInfo.shipKills > 0 && <span style={{ color: '#cc3333' }}>SK {hoveredStatInfo.shipKills.toLocaleString()}</span>}
              {hoveredStatInfo.npcKills > 0 && <span>NK {hoveredStatInfo.npcKills.toLocaleString()}</span>}
              {hoveredStatInfo.podKills > 0 && <span style={{ color: '#cc3333' }}>PK {hoveredStatInfo.podKills.toLocaleString()}</span>}
            </div>
          )}
        </div>
      )}

      {/* System info panel */}
      {selectedSystem && panelPos && (
        <SystemInfoPanel
          system={selectedSystem}
          position={panelPos}
          stats={stats}
          routeOrigin={routeOrigin}
          routeDest={routeDest}
          activeRoute={activeRoute}
          routePreference={routePreference}
          onSetOrigin={handleSetOrigin}
          onSetDestination={handleSetDest}
          onSetRoutePreference={setRoutePreference}
          onClose={() => {
            setSelectedSystem(null);
            setPanelPos(null);
            systemRendererRef.current?.setSelected(null);
          }}
        />
      )}

      {/* Bottom overlay controls */}
      <div ref={overlayBarRef}>
        <OverlayControls
          activeOverlay={activeOverlay}
          onOverlayChange={(o) => setActiveOverlay(o === activeOverlay ? 'security' : o)}
          statsLoaded={!statsLoading && stats !== null}
        />
      </div>
    </div>
  );
});

StarMap.displayName = 'StarMap';

/** Brighten a hex color by a factor */
function brighten(hex: number, factor: number): number {
  const r = Math.min(255, Math.round(((hex >> 16) & 0xff) * factor));
  const g = Math.min(255, Math.round(((hex >> 8) & 0xff) * factor));
  const b = Math.min(255, Math.round((hex & 0xff) * factor));
  return (r << 16) | (g << 8) | b;
}
