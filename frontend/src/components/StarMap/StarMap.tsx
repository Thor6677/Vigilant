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

import type { SystemData, MapData, OverlayType, GroupMode } from './types';
import { LODTier } from './types';
import { LOD_THRESHOLDS, MIN_ZOOM, MAX_ZOOM, CANVAS_SIZE, BG_COLOR } from './utils/constants';
import { heatmapColor, allianceColor, FACTION_COLORS } from './utils/colors';
import { SystemRenderer } from './renderer/SystemRenderer';
import { EdgeRenderer } from './renderer/EdgeRenderer';
import { LabelRenderer } from './renderer/LabelRenderer';
import { RouteRenderer } from './renderer/RouteRenderer';
import { JumpRangeRenderer } from './renderer/JumpRangeRenderer';
import { AllianceTerritoryRenderer } from './renderer/AllianceTerritoryRenderer';
import { buildGraph } from './graph/buildGraph';
import { useOverlayData } from './useOverlayData';
import { useSovChanges, type SovTimeRange } from './useSovChanges';
import { useCharacterLocations } from './useCharacterLocations';
import { useJumpPlanner } from './useJumpPlanner';
import { useGateRoutePlanner } from './useGateRoutePlanner';

import { SystemInfoPanel } from './ui/SystemInfoPanel';
import { SystemSearch } from './ui/SystemSearch';
import { MapToolbar } from './ui/MapToolbar';
import { OverlayControls } from './ui/OverlayControls';
import { GroupModeControls } from './ui/GroupModeControls';
import { JumpPlannerPanel } from './ui/JumpPlannerPanel';
import { GateRoutePlannerPanel } from './ui/GateRoutePlannerPanel';
import { SystemContextMenu } from './ui/SystemContextMenu';
import { useIsMobile } from './useIsMobile';
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
  const jumpRangeRendererRef = useRef<JumpRangeRenderer | null>(null);
  const territoryRendererRef = useRef<AllianceTerritoryRenderer | null>(null);

  const [selectedSystem, setSelectedSystem] = useState<SystemData | null>(null);
  const [hoveredSystem, setHoveredSystem] = useState<SystemData | null>(null);
  const [tooltipPos, setTooltipPos] = useState<{ x: number; y: number } | null>(null);
  const [panelPos, setPanelPos] = useState<{ x: number; y: number } | null>(null);
  const [contextMenu, setContextMenu] = useState<{
    system: SystemData;
    position: { x: number; y: number };
  } | null>(null);
  const [activeOverlay, setActiveOverlay] = useState<OverlayType>('security');
  const [groupMode, setGroupMode] = useState<GroupMode>('systems');
  const [overlayBarHeight, setOverlayBarHeight] = useState(36);
  const overlayBarRef = useRef<HTMLDivElement>(null);
  const currentLODRef = useRef<LODTier>(LODTier.Galaxy);
  const panelHoverRef = useRef(false); // true when mouse is over an HTML panel
  const selectedSystemRef = useRef<SystemData | null>(null);

  // Keep ref in sync for use in Pixi callbacks
  selectedSystemRef.current = selectedSystem;

  const isMobile = useIsMobile();

  // Fetch ESI overlay stats
  const { stats, loading: statsLoading } = useOverlayData();
  const { characters } = useCharacterLocations();
  const jumpPlanner = useJumpPlanner(data.systemMap, data.systems, adjacencyRef.current ?? undefined);
  const getGraph = useCallback(() => graphRef.current, []);
  const gateRoutePlanner = useGateRoutePlanner(getGraph);
  const [allianceNames, setAllianceNames] = useState<Map<string, string>>(new Map());
  const [sovTimeRange, setSovTimeRange] = useState<SovTimeRange | null>(null);
  const sovChanges = useSovChanges(activeOverlay === 'sovereignty', sovTimeRange);

  // Fetch alliance names for sovereignty data + sov changes
  useEffect(() => {
    const ids = new Set<number>();
    if (stats?.sovereignty) {
      for (const sov of Object.values(stats.sovereignty)) {
        if (sov.alliance_id) ids.add(sov.alliance_id);
      }
    }
    if (sovChanges.data?.changes) {
      for (const sc of Object.values(sovChanges.data.changes)) {
        if (sc.old_alliance_id) ids.add(sc.old_alliance_id);
        if (sc.new_alliance_id) ids.add(sc.new_alliance_id);
      }
    }
    if (ids.size === 0) return;
    // Filter out already-resolved
    const missing = [...ids].filter(id => !allianceNames.has(String(id)));
    if (missing.length === 0) return;

    // Batch fetch in groups of 50
    const batch = missing.slice(0, 50);
    fetch(`/api/map/alliances?ids=${batch.join(',')}`)
      .then(r => r.ok ? r.json() : {})
      .then((names: Record<string, string>) => {
        setAllianceNames(prev => {
          const next = new Map(prev);
          for (const [id, name] of Object.entries(names)) next.set(id, name);
          return next;
        });
      })
      .catch(() => {});
  }, [stats?.sovereignty, sovChanges.data]);

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
      const changedIds = sovChanges.data ? new Set(Object.keys(sovChanges.data.changes)) : null;
      const hasChanges = changedIds && changedIds.size > 0;
      for (const sys of data.systems) {
        const sov = stats.sovereignty[String(sys.id)];
        let color = 0x222233;
        if (sov?.alliance_id) {
          color = allianceColor(sov.alliance_id);
        } else if (sov?.faction_id) {
          color = FACTION_COLORS[sov.faction_id] ?? 0x555577;
        }
        if (hasChanges) {
          tints.set(sys.id, changedIds.has(String(sys.id)) ? brighten(color, 1.6) : brighten(color, 0.35));
        } else {
          tints.set(sys.id, color);
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
  }, [activeOverlay, stats, data.systems, sovChanges.data]);

  // Apply overlay tints to renderer
  useEffect(() => {
    systemRendererRef.current?.setOverlayTint(overlayTints);
  }, [overlayTints]);

  // Update alliance territory shading when sovereignty overlay is active
  useEffect(() => {
    const tr = territoryRendererRef.current;
    if (!tr) return;
    if (activeOverlay === 'sovereignty' && stats?.sovereignty) {
      // When sov changes active, dim unchanged territory and brighten changed
      const changedIds = sovChanges.data
        ? new Set(Object.keys(sovChanges.data.changes).map(Number))
        : null;
      const dimSet = changedIds && changedIds.size > 0
        ? new Set(data.systems.filter(s => !changedIds.has(s.id)).map(s => s.id))
        : null;
      tr.update(data.systems, stats.sovereignty, dimSet);
    } else {
      tr.clear();
    }
  }, [activeOverlay, stats, data.systems, sovChanges.data]);

  // Apply jump planner highlight: route systems stay bright, others dimmed
  useEffect(() => {
    const sr = systemRendererRef.current;
    const jr = jumpRangeRendererRef.current;
    if (!sr || !jr) return;

    if (!jumpPlanner.active) {
      sr.setJumpRangeHighlight(null, null);
      jr.setReachable(null, []);
      // Same panelHoverRef fix as the gate planner — onPointerLeave won't
      // fire when the panel DOM element is removed while the cursor is over it.
      panelHoverRef.current = false;
      return;
    }

    // If a route exists, highlight the route systems
    if (jumpPlanner.jumpRoute && jumpPlanner.jumpRoute.length > 1) {
      const routeIds = new Set(jumpPlanner.jumpRoute.map(wp => wp.system.id));
      sr.setJumpRangeHighlight(jumpPlanner.jumpOrigin, routeIds);
      jr.setReachable(null, []); // range lines not needed when route is shown
    } else if (jumpPlanner.jumpOrigin !== null) {
      // No route yet — show reachable systems from origin
      const origin = data.systemMap.get(jumpPlanner.jumpOrigin);
      if (origin) {
        sr.setJumpRangeHighlight(jumpPlanner.jumpOrigin, jumpPlanner.reachableIds);
        jr.setReachable(origin, jumpPlanner.reachableSystems);
      }
    } else {
      sr.setJumpRangeHighlight(null, null);
      jr.setReachable(null, []);
    }
  }, [jumpPlanner.active, jumpPlanner.jumpOrigin, jumpPlanner.jumpRoute, jumpPlanner.reachableIds, jumpPlanner.reachableSystems, data.systemMap]);

  // Apply jump route visualization (only when planner is active)
  useEffect(() => {
    const jr = jumpRangeRendererRef.current;
    if (!jr) return;
    if (jumpPlanner.active && jumpPlanner.jumpRoute && jumpPlanner.jumpRoute.length > 1) {
      jr.setRoute(jumpPlanner.jumpRoute.map(wp => wp.system));
    } else {
      jr.clearAll();
    }
  }, [jumpPlanner.active, jumpPlanner.jumpRoute]);

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
      // Imperative escape hatch: push a path directly to the renderer
      // without going through the gate route planner state. Used by
      // external callers that want to display a specific pre-computed path.
      routeRendererRef.current?.setRoute(systemIds);
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
    territoryRendererRef.current?.setLODAlpha(scale);

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

      // Resize viewport when container changes (orientation flip, window resize)
      const vpResizeObserver = new ResizeObserver(() => {
        if (viewportRef.current && el) {
          const { clientWidth, clientHeight } = el;
          viewportRef.current.resize(clientWidth, clientHeight);
          app.renderer.resize(clientWidth, clientHeight);
        }
      });
      vpResizeObserver.observe(el);
      (el as any).__vpResizeObserver = vpResizeObserver;

      // Build renderers
      const systemRenderer = new SystemRenderer();
      const edgeRenderer = new EdgeRenderer();
      const labelRenderer = new LabelRenderer();
      const routeRenderer = new RouteRenderer();
      const jumpRangeRenderer = new JumpRangeRenderer();
      const territoryRenderer = new AllianceTerritoryRenderer();

      systemRendererRef.current = systemRenderer;
      edgeRendererRef.current = edgeRenderer;
      labelRendererRef.current = labelRenderer;
      routeRendererRef.current = routeRenderer;
      jumpRangeRendererRef.current = jumpRangeRenderer;
      territoryRendererRef.current = territoryRenderer;

      // Init renderers
      edgeRenderer.init(data.systemMap, data.edges);
      edgeRenderer.initHoverLayer();
      systemRenderer.init(app, data.systems);
      labelRenderer.init(data.systems, data.regions);
      routeRenderer.init(data.systemMap);
      jumpRangeRenderer.init();

      // Add to viewport in draw order: territory → edges → region labels → group labels → systems → system labels → route
      vp.addChild(territoryRenderer.container);
      vp.addChild(edgeRenderer.container);
      vp.addChild(labelRenderer.regionLabels);
      vp.addChild(labelRenderer.groupLabels);
      vp.addChild(systemRenderer.container);
      vp.addChild(labelRenderer.systemLabels);
      vp.addChild(jumpRangeRenderer.rangeGraphics);
      vp.addChild(jumpRangeRenderer.routeGraphics);
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

        // Suppress hover when mouse is over an HTML panel
        if (panelHoverRef.current) return;

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

      // Right-click context menu on systems
      const handleContextMenu = (e: MouseEvent) => {
        e.preventDefault();
        const rect = el.getBoundingClientRect();
        const localX = e.clientX - rect.left;
        const localY = e.clientY - rect.top;
        const worldPos = vp.toWorld(localX, localY);
        const hitRadius = 30 / vp.scaled;
        const found = qt.find(worldPos.x, worldPos.y, hitRadius);
        if (found) {
          setContextMenu({
            system: found,
            position: { x: e.clientX, y: e.clientY },
          });
        } else {
          setContextMenu(null);
        }
      };
      el.addEventListener('contextmenu', handleContextMenu);

      // Animation loop
      app.ticker.add((ticker) => {
        routeRenderer.tick(ticker.deltaTime);
        systemRenderer.tick(ticker.deltaTime);
        jumpRangeRenderer.tick(ticker.deltaTime);
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
        el.removeEventListener('contextmenu', handleContextMenu);
        if (hoverThrottleId !== null) clearTimeout(hoverThrottleId);
        if (tooltipTimerRef.current) clearTimeout(tooltipTimerRef.current);
      };
    }

    init();

    return () => {
      destroyed = true;
      (el as any).__mapCleanup?.();
      (el as any).__vpResizeObserver?.disconnect();
      systemRendererRef.current?.destroy();
      edgeRendererRef.current?.destroy();
      labelRendererRef.current?.destroy();
      routeRendererRef.current?.destroy();
      jumpRangeRendererRef.current?.destroy();
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

  // Sync the gate route planner's computed activeRoute → the Pixi renderer.
  useEffect(() => {
    const r = routeRendererRef.current;
    const route = gateRoutePlanner.activeRoute;
    if (route && route.length >= 2) {
      r?.setRoute(route);
    } else {
      r?.clearRoute();
    }
  }, [gateRoutePlanner.activeRoute]);

  // Single consolidated highlight effect for the gate route planner.
  // Derives the correct persistent highlight from the current state so
  // competing effects can't step on each other:
  //   panel closed → clear
  //   route active → highlight route systems
  //   neither → clear (unless jump planner owns the highlight)
  useEffect(() => {
    const sr = systemRendererRef.current;
    if (!sr) return;

    if (!gateRoutePlanner.active) {
      // Panel closed — clear gate-planner highlight (don't touch jump planner)
      if (!jumpPlanner.active || (!jumpPlanner.jumpRoute && jumpPlanner.reachableIds.size === 0)) {
        sr.setJumpRangeHighlight(null, null);
      }
      // CRITICAL: reset panelHoverRef. When the user clicks × with their
      // cursor over the panel, onPointerLeave never fires (the DOM element
      // is removed before the event can propagate). This leaves the ref
      // stuck at true, which blocks ALL map hover processing in the
      // viewport pointermove handler (if (panelHoverRef.current) return).
      panelHoverRef.current = false;
      return;
    }

    // Panel is open — derive highlight from state
    const route = gateRoutePlanner.activeRoute;
    if (route && route.length >= 2) {
      sr.setJumpRangeHighlight(gateRoutePlanner.origin, new Set(route));
    } else {
      // No active route — clear unless jump planner owns highlight
      if (!jumpPlanner.active || (!jumpPlanner.jumpRoute && jumpPlanner.reachableIds.size === 0)) {
        sr.setJumpRangeHighlight(null, null);
      }
    }
  }, [
    gateRoutePlanner.active,
    gateRoutePlanner.activeRoute,
    gateRoutePlanner.origin,
    jumpPlanner.active,
    jumpPlanner.jumpRoute,
    jumpPlanner.reachableIds,
  ]);

  // Push the avoid set to the renderer so the red ❌ overlays appear/update.
  useEffect(() => {
    routeRendererRef.current?.setAvoidSystems(gateRoutePlanner.avoidSystems);
  }, [gateRoutePlanner.avoidSystems]);

  // Push per-hop threat levels to the renderer so the diamonds get tinted.
  useEffect(() => {
    const threats = new Map<number, string>();
    for (const [sid, intel] of gateRoutePlanner.hopIntel) {
      threats.set(sid, intel.threat);
    }
    routeRendererRef.current?.setHopThreats(threats);
  }, [gateRoutePlanner.hopIntel]);

  // Auto-trim: when the active character is in a system on the route,
  // advance the route's origin to that system so the panel shows
  // "remaining hops" rather than the full original route.
  useEffect(() => {
    if (!gateRoutePlanner.followCharacter) return;
    if (gateRoutePlanner.activeCharacterId === null) return;
    if (!gateRoutePlanner.activeRoute || gateRoutePlanner.activeRoute.length < 2) return;

    const char = characters.find(c => c.character_id === gateRoutePlanner.activeCharacterId);
    if (!char || char.system_id === null) return;

    // If the active character is in a system on the route AFTER the
    // current origin, advance the origin to it.
    const idx = gateRoutePlanner.activeRoute.indexOf(char.system_id);
    if (idx > 0 && char.system_id !== gateRoutePlanner.origin) {
      // The character has reached a hop further along the route.
      // Trim by setting the origin to the character's current system.
      gateRoutePlanner.setOrigin(char.system_id);
    }
  }, [
    characters,
    gateRoutePlanner.followCharacter,
    gateRoutePlanner.activeCharacterId,
    gateRoutePlanner.activeRoute,
    gateRoutePlanner.origin,
    gateRoutePlanner,
  ]);

  // Default the active character to the user's main (or first) online
  // character. If the current selection isn't online anymore, switch to
  // whoever IS online so the dropdown isn't pointing at a stale entry.
  useEffect(() => {
    const online = characters.filter(c => c.system_id !== null);
    if (online.length === 0) {
      if (gateRoutePlanner.activeCharacterId !== null) {
        gateRoutePlanner.setActiveCharacterId(null);
      }
      return;
    }
    const currentIsOnline = online.some(c => c.character_id === gateRoutePlanner.activeCharacterId);
    if (!currentIsOnline) {
      const main = online.find(c => c.is_main) || online[0];
      gateRoutePlanner.setActiveCharacterId(main.character_id);
    }
  }, [characters, gateRoutePlanner.activeCharacterId, gateRoutePlanner]);

  // Parse URL params on mount and pre-populate the gate route planner.
  // Supports two forms:
  //   /map?route=<share_token>          → fetch shared SavedGateRoute and load
  //   /map?origin=X&dest=Y&waypoints=A,B&prefs=highsec → direct deep link
  // After loading, clean the URL via history.replaceState so a refresh
  // doesn't reload the same route.
  const urlHandledRef = useRef(false);
  useEffect(() => {
    if (urlHandledRef.current) return;
    urlHandledRef.current = true;

    const params = new URLSearchParams(window.location.search);
    const shareToken = params.get('route');
    const originParam = params.get('origin');
    const destParam = params.get('dest');
    const waypointsParam = params.get('waypoints');
    const prefsParam = params.get('prefs');

    const cleanUrl = () => {
      window.history.replaceState({}, '', window.location.pathname);
    };

    if (shareToken) {
      // Fetch the shared route from the public endpoint
      fetch(`/api/map/routes/shared/${encodeURIComponent(shareToken)}`)
        .then(resp => {
          if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
          return resp.json();
        })
        .then((route: {
          origin_system_id: number;
          dest_system_id: number;
          waypoints: number[];
          preference: 'shortest' | 'highsec' | 'lowsec' | 'nullsec';
        }) => {
          gateRoutePlanner.loadRouteData({
            origin_system_id: route.origin_system_id,
            dest_system_id: route.dest_system_id,
            waypoints: route.waypoints || [],
            preference: route.preference || 'shortest',
          });
          gateRoutePlanner.setActive(true);
          cleanUrl();
        })
        .catch(() => {
          // Silent — user can still use the planner manually
          cleanUrl();
        });
    } else if (originParam && destParam) {
      const origin = Number(originParam);
      const dest = Number(destParam);
      if (!Number.isNaN(origin) && !Number.isNaN(dest)) {
        const waypoints = waypointsParam
          ? waypointsParam.split(',').map(Number).filter(n => !Number.isNaN(n))
          : [];
        const validPrefs = new Set(['shortest', 'highsec', 'lowsec', 'nullsec']);
        const preference = (prefsParam && validPrefs.has(prefsParam))
          ? (prefsParam as 'shortest' | 'highsec' | 'lowsec' | 'nullsec')
          : 'shortest';
        gateRoutePlanner.loadRouteData({
          origin_system_id: origin,
          dest_system_id: dest,
          waypoints,
          preference,
        });
        gateRoutePlanner.setActive(true);
        cleanUrl();
      }
    }
  }, [gateRoutePlanner]);

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

  const handleSetOrigin = useCallback((id: number) => {
    gateRoutePlanner.setActive(true);
    gateRoutePlanner.setOrigin(id);
  }, [gateRoutePlanner]);
  const handleSetDest = useCallback((id: number) => {
    gateRoutePlanner.setActive(true);
    gateRoutePlanner.setDest(id);
  }, [gateRoutePlanner]);
  const handleAddWaypoint = useCallback((id: number) => {
    gateRoutePlanner.setActive(true);
    gateRoutePlanner.addWaypoint(id);
  }, [gateRoutePlanner]);
  const handleAvoidSystem = useCallback((id: number) => {
    // Don't auto-open the panel for avoid — user might just be marking a no-go
    gateRoutePlanner.addAvoid(id);
  }, [gateRoutePlanner]);

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

  const mobilePlannerOpen = isMobile && (gateRoutePlanner.active || jumpPlanner.active);

  return (
    <div style={{
      position: 'relative',
      width: '100%',
      height: '100%',
      ...(mobilePlannerOpen ? { display: 'flex', flexDirection: 'column', overflow: 'hidden' } : {}),
    }}>
      {/* Canvas container — height adjusts for overlay bar */}
      <div ref={containerRef} style={{
        width: '100%',
        ...(mobilePlannerOpen
          ? { height: '45%', flexShrink: 0 }
          : { height: `calc(100% - ${overlayBarHeight}px)` }),
      }} />

      {/* Search bar */}
      <SystemSearch
        systems={data.systems}
        characters={characters}
        onSelectSystem={handleSearchSelectSystem}
        onSelectArea={handleSearchSelectArea}
        onSetRouteOrigin={handleSetOrigin}
        onSetRouteDest={handleSetDest}
        onAddRouteWaypoint={handleAddWaypoint}
        onAvoidSystem={handleAvoidSystem}
        isMobile={isMobile}
      />

      {/* Group mode selector */}
      <GroupModeControls mode={groupMode} onModeChange={setGroupMode} isMobile={isMobile} />

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
        jumpPlannerActive={jumpPlanner.active}
        onToggleJumpPlanner={() => jumpPlanner.setActive(!jumpPlanner.active)}
        gatePlannerActive={gateRoutePlanner.active}
        onToggleGatePlanner={() => gateRoutePlanner.setActive(!gateRoutePlanner.active)}
        isMobile={isMobile}
      />

      {/* Gate route planner panel */}
      {gateRoutePlanner.active && (
        <div
          onPointerEnter={() => { panelHoverRef.current = true; }}
          onPointerLeave={() => { panelHoverRef.current = false; }}
          {...(isMobile ? { style: { flex: 1, minHeight: 0, overflow: 'hidden' } } : {})}
        >
          <GateRoutePlannerPanel
            planner={gateRoutePlanner}
            systems={data.systems}
            systemMap={data.systemMap}
            systemName={(id) => data.systemMap.get(id)?.name ?? `System ${id}`}
            characters={characters}
            isMobile={isMobile}
            onFocusSystem={(sys) => {
              if (viewportRef.current) {
                viewportRef.current.animate({
                  position: { x: sys.x, y: sys.y },
                  scale: 2,
                  time: 600,
                  ease: 'easeInOutCubic',
                });
              }
            }}
            onHighlightSystems={(ids) => {
              const sr = systemRendererRef.current;
              if (!sr) return;
              if (ids && ids.size > 0) {
                sr.setJumpRangeHighlight(null, ids);
              } else if (gateRoutePlanner.activeRoute && gateRoutePlanner.activeRoute.length >= 2) {
                // Restore route highlight
                sr.setJumpRangeHighlight(
                  gateRoutePlanner.origin,
                  new Set(gateRoutePlanner.activeRoute),
                );
              } else {
                sr.setJumpRangeHighlight(null, null);
              }
            }}
          />
        </div>
      )}

      {/* Jump planner panel */}
      {jumpPlanner.active && (
        <div
          onPointerEnter={() => { panelHoverRef.current = true; }}
          onPointerLeave={() => { panelHoverRef.current = false; }}
          {...(isMobile ? { style: { flex: 1, minHeight: 0, overflow: 'hidden' } } : {})}
        >
          <JumpPlannerPanel
            planner={jumpPlanner}
            systems={data.systems}
            systemName={(id) => data.systemMap.get(id)?.name ?? `System ${id}`}
            characters={characters}
            stats={stats}
            isMobile={isMobile}
            onFocusSystem={(sys) => {
              if (viewportRef.current) {
                viewportRef.current.animate({
                  position: { x: sys.x, y: sys.y },
                  scale: 2,
                  time: 600,
                  ease: 'easeInOutCubic',
                });
              }
            }}
            onHighlightSystems={(ids) => {
              const sr = systemRendererRef.current;
              if (!sr) return;
              if (ids && ids.size > 0) {
                // Temporarily highlight these alternatives
                sr.setJumpRangeHighlight(null, ids);
              } else if (jumpPlanner.jumpRoute && jumpPlanner.jumpRoute.length > 1) {
                // Restore route highlight
                const routeIds = new Set(jumpPlanner.jumpRoute.map(wp => wp.system.id));
                sr.setJumpRangeHighlight(jumpPlanner.jumpOrigin, routeIds);
              } else {
                sr.setJumpRangeHighlight(jumpPlanner.jumpOrigin, jumpPlanner.reachableIds);
              }
            }}
          />
        </div>
      )}

      {/* Right-click context menu */}
      {contextMenu && (
        <SystemContextMenu
          system={contextMenu.system}
          position={contextMenu.position}
          onClose={() => setContextMenu(null)}
          onSetOrigin={handleSetOrigin}
          onSetDestination={handleSetDest}
          onAddWaypoint={handleAddWaypoint}
          onAvoidSystem={handleAvoidSystem}
        />
      )}

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
          {sovChanges.data?.changes[String(hoveredSystem.id)] && (() => {
            const sc = sovChanges.data!.changes[String(hoveredSystem.id)];
            const oldName = sc.old_alliance_id ? (allianceNames.get(String(sc.old_alliance_id)) ?? `Alliance ${sc.old_alliance_id}`) : 'Unclaimed';
            return (
              <div style={{ fontSize: 9, color: '#c8a951', marginTop: 3 }}>
                SOV CHANGED · was {oldName}{sc.change_count > 1 ? ` · ${sc.change_count} flips` : ''}
              </div>
            );
          })()}
        </div>
      )}

      {/* System info panel */}
      {selectedSystem && panelPos && (
        <SystemInfoPanel
          system={selectedSystem}
          position={panelPos}
          stats={stats}
          allianceNames={allianceNames}
          routeOrigin={gateRoutePlanner.origin}
          routeDest={gateRoutePlanner.dest}
          activeRoute={gateRoutePlanner.activeRoute}
          routePreference={gateRoutePlanner.preference}
          onSetOrigin={handleSetOrigin}
          onSetDestination={handleSetDest}
          onSetRoutePreference={gateRoutePlanner.setPreference}
          onClose={() => {
            setSelectedSystem(null);
            setPanelPos(null);
            systemRendererRef.current?.setSelected(null);
          }}
          onSetJumpOrigin={(id) => { jumpPlanner.setActive(true); jumpPlanner.setJumpOrigin(id); }}
          onSetJumpDest={(id) => { jumpPlanner.setActive(true); jumpPlanner.setJumpDest(id); }}
          sovChange={sovChanges.data?.changes[String(selectedSystem.id)] ?? null}
        />
      )}

      {/* Bottom overlay controls */}
      <div ref={overlayBarRef}>
        <OverlayControls
          activeOverlay={activeOverlay}
          onOverlayChange={(o) => {
            setActiveOverlay(o === activeOverlay ? 'security' : o);
            if (o !== 'sovereignty') setSovTimeRange(null);
          }}
          statsLoaded={!statsLoading && stats !== null}
          sovTimeRange={sovTimeRange}
          onSovTimeRangeChange={(r) => setSovTimeRange(r === sovTimeRange ? null : r)}
          sovChangesCount={sovChanges.data ? Object.keys(sovChanges.data.changes).length : 0}
          sovChangesLoading={sovChanges.loading}
          isMobile={isMobile}
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
