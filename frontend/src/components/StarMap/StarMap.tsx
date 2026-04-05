import {
  useRef,
  useEffect,
  useCallback,
  useState,
  useImperativeHandle,
  forwardRef,
} from 'react';
import { Application } from 'pixi.js';
import { Viewport } from 'pixi-viewport';
import { quadtree, type Quadtree } from 'd3-quadtree';
import Graph from 'graphology';

import type { SystemData, MapData, RoutePreference } from './types';
import { LODTier } from './types';
import { LOD_THRESHOLDS, MIN_ZOOM, MAX_ZOOM, CANVAS_SIZE, BG_COLOR } from './utils/constants';
import { SystemRenderer } from './renderer/SystemRenderer';
import { EdgeRenderer } from './renderer/EdgeRenderer';
import { LabelRenderer } from './renderer/LabelRenderer';
import { RouteRenderer } from './renderer/RouteRenderer';
import { buildGraph } from './graph/buildGraph';
import { findRoute } from './graph/pathfinding';

import { SystemInfoPanel } from './ui/SystemInfoPanel';
import { SystemSearch } from './ui/SystemSearch';
import { MapToolbar } from './ui/MapToolbar';

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
  const currentLODRef = useRef<LODTier>(LODTier.Galaxy);

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

  // LOD detection
  const updateLOD = useCallback((scale: number) => {
    let tier: LODTier = LODTier.Galaxy;
    if (scale >= LOD_THRESHOLDS[LODTier.System]) tier = LODTier.System;
    else if (scale >= LOD_THRESHOLDS[LODTier.Constellation]) tier = LODTier.Constellation;
    else if (scale >= LOD_THRESHOLDS[LODTier.Region]) tier = LODTier.Region;

    if (tier !== currentLODRef.current) {
      currentLODRef.current = tier;
      systemRendererRef.current?.updateLOD(tier);
      edgeRendererRef.current?.updateLOD(tier);
      labelRendererRef.current?.updateLOD(tier);
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

      // Viewport
      const vp = new Viewport({
        screenWidth: el.clientWidth,
        screenHeight: el.clientHeight,
        worldWidth: CANVAS_SIZE,
        worldHeight: CANVAS_SIZE,
        events: app.renderer.events,
      });

      vp.drag({ mouseButtons: 'left' })
        .pinch()
        .wheel({ smooth: 5 })
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
      systemRenderer.init(app, data.systems);
      labelRenderer.init(data.systems, data.regions);
      routeRenderer.init(data.systemMap);

      // Add to viewport in draw order: edges → region labels → systems → system labels → route
      vp.addChild(edgeRenderer.graphics);
      vp.addChild(labelRenderer.regionLabels);
      vp.addChild(systemRenderer.container);
      vp.addChild(labelRenderer.systemLabels);
      vp.addChild(routeRenderer.graphics);

      // Build quadtree for spatial hit testing
      const qt = quadtree<SystemData>()
        .x(d => d.x)
        .y(d => d.y)
        .addAll(data.systems);
      qtRef.current = qt;

      // Build graph
      const graph = buildGraph(data.systems, data.edges);
      graphRef.current = graph;

      // Set initial LOD
      updateLOD(vp.scaled);
      labelRenderer.updateLOD(LODTier.Galaxy);

      // Zoom to fit
      vp.fit(true);
      vp.moveCenter(CANVAS_SIZE / 2, CANVAS_SIZE / 2);

      // LOD updates on zoom
      vp.on('zoomed', () => updateLOD(vp.scaled));

      // Close panel on drag
      vp.on('drag-start', () => {
        setSelectedSystem(null);
        setPanelPos(null);
        systemRenderer.setSelected(null);
      });

      // Pointer events for hover / click
      let hoverThrottleId: number | null = null;

      vp.on('pointermove', (e) => {
        if (hoverThrottleId !== null) return;
        hoverThrottleId = window.setTimeout(() => {
          hoverThrottleId = null;
        }, 16);

        const worldPos = vp.toWorld(e.global);
        const hitRadius = 30 / vp.scaled; // Adjust hit radius by zoom
        const found = qt.find(worldPos.x, worldPos.y, hitRadius);

        if (found) {
          setHoveredSystem(found);
          const screenPos = vp.toScreen(found.x, found.y);
          setTooltipPos({ x: screenPos.x, y: screenPos.y });
          systemRenderer.setHovered(found.id);
          el.style.cursor = 'pointer';
        } else {
          setHoveredSystem(null);
          setTooltipPos(null);
          systemRenderer.setHovered(null);
          el.style.cursor = 'grab';
        }
      });

      vp.on('clicked', (e: any) => {
        const worldPos = e.world;
        const hitRadius = 30 / vp.scaled;
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

      // Animation loop for route
      app.ticker.add((ticker) => {
        routeRenderer.tick(ticker.deltaTime);
      });

      // ResizeObserver
      const resizeObserver = new ResizeObserver(() => {
        if (!destroyed) {
          app.renderer.resize(el.clientWidth, el.clientHeight);
          vp.resize(el.clientWidth, el.clientHeight);
        }
      });
      resizeObserver.observe(el);

      // Store cleanup ref
      (el as any).__mapCleanup = () => {
        resizeObserver.disconnect();
        if (hoverThrottleId !== null) clearTimeout(hoverThrottleId);
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
  }, [data, updateLOD, onSystemClick]);

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
      if (e.target instanceof HTMLInputElement) return;

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

  const handleSearchSelect = useCallback((system: SystemData) => {
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

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%' }}>
      <div ref={containerRef} style={{ width: '100%', height: '100%' }} />

      {/* Search bar */}
      <SystemSearch systems={data.systems} onSelect={handleSearchSelect} />

      {/* Toolbar */}
      <MapToolbar
        onZoomIn={() => viewportRef.current?.zoomPercent(0.3, true)}
        onZoomOut={() => viewportRef.current?.zoomPercent(-0.3, true)}
        onFitAll={() => {
          viewportRef.current?.fit(true);
          viewportRef.current?.moveCenter(CANVAS_SIZE / 2, CANVAS_SIZE / 2);
        }}
      />

      {/* Hover tooltip */}
      {hoveredSystem && tooltipPos && !selectedSystem && (
        <div
          style={{
            position: 'absolute',
            left: tooltipPos.x + 16,
            top: tooltipPos.y - 12,
            pointerEvents: 'none',
            background: 'rgba(10, 14, 30, 0.92)',
            border: '1px solid #2a3a5a',
            borderRadius: 6,
            padding: '6px 10px',
            fontSize: 13,
            fontFamily: 'DM Sans, sans-serif',
            color: '#C8D8E8',
            whiteSpace: 'nowrap',
            zIndex: 10,
          }}
        >
          <strong>{hoveredSystem.name}</strong>
          <span style={{ marginLeft: 8, color: '#8a9ab0' }}>
            {hoveredSystem.sec.toFixed(1)} &middot; {hoveredSystem.regName}
          </span>
        </div>
      )}

      {/* System info panel */}
      {selectedSystem && panelPos && (
        <SystemInfoPanel
          system={selectedSystem}
          position={panelPos}
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
    </div>
  );
});

StarMap.displayName = 'StarMap';
