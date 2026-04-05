import { useEffect, useState } from 'react';
import type { SystemData, RegionData, Edge, MapData } from './types';

export function useMapData() {
  const [data, setData] = useState<MapData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const base = import.meta.env.BASE_URL;
        const [systemsRes, edgesRes, regionsRes] = await Promise.all([
          fetch(`${base}data/systems.json`),
          fetch(`${base}data/edges.json`),
          fetch(`${base}data/regions.json`),
        ]);

        if (!systemsRes.ok || !edgesRes.ok || !regionsRes.ok) {
          throw new Error('Failed to load map data files');
        }

        const [systems, edges, regions] = await Promise.all([
          systemsRes.json() as Promise<SystemData[]>,
          edgesRes.json() as Promise<Edge[]>,
          regionsRes.json() as Promise<RegionData[]>,
        ]);

        if (cancelled) return;

        const systemMap = new Map<number, SystemData>();
        for (const sys of systems) {
          systemMap.set(sys.id, sys);
        }

        setData({ systems, edges, regions, systemMap });
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : 'Unknown error');
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    load();
    return () => { cancelled = true; };
  }, []);

  return { data, loading, error };
}
