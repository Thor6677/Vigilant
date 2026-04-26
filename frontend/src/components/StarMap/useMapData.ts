import { useEffect, useState } from 'react';
import type { SystemData, RegionData, Edge, MapData } from './types';

export function useMapData(space: 'k' | 'w' = 'k') {
  const [data, setData] = useState<MapData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const ac = new AbortController();

    // Reset to a loading state on space switch so App unmounts the
    // existing StarMap and remounts a fresh one against the new dataset.
    // Otherwise the previous space's data + viewport + renderer state
    // sticks around between the click and the fetch resolving — that
    // window is where W→K showed a black screen.
    setData(null);
    setLoading(true);
    setError(null);

    async function load() {
      try {
        const base = import.meta.env.BASE_URL;
        // K-space: served as static Vite-built bundles under /map/data/.
        // W-space: synthetic layout served at runtime by FastAPI under
        // /api/map/wormholes-data/. No service-worker caching for W-space
        // (the layout can change on deploy if SDE updates).
        const v = '2';
        const [systemsURL, edgesURL, regionsURL] = space === 'w'
          ? [
              `/api/map/wormholes-data/systems.json`,
              `/api/map/wormholes-data/edges.json`,
              `/api/map/wormholes-data/regions.json`,
            ]
          : [
              `${base}data/systems.json?v=${v}`,
              `${base}data/edges.json?v=${v}`,
              `${base}data/regions.json?v=${v}`,
            ];
        const [systemsRes, edgesRes, regionsRes] = await Promise.all([
          fetch(systemsURL, { signal: ac.signal }),
          fetch(edgesURL, { signal: ac.signal }),
          fetch(regionsURL, { signal: ac.signal }),
        ]);

        if (!systemsRes.ok || !edgesRes.ok || !regionsRes.ok) {
          throw new Error('Failed to load map data files');
        }

        const [systems, edges, regions] = await Promise.all([
          systemsRes.json() as Promise<SystemData[]>,
          edgesRes.json() as Promise<Edge[]>,
          regionsRes.json() as Promise<RegionData[]>,
        ]);

        if (ac.signal.aborted) return;

        const systemMap = new Map<number, SystemData>();
        for (const sys of systems) {
          systemMap.set(sys.id, sys);
        }

        setData({ systems, edges, regions, systemMap });
      } catch (e) {
        if (ac.signal.aborted) return;
        setError(e instanceof Error ? e.message : 'Unknown error');
      } finally {
        if (!ac.signal.aborted) setLoading(false);
      }
    }

    load();
    return () => { ac.abort(); };
  }, [space]);

  return { data, loading, error };
}
