import { useEffect, useState, useCallback } from 'react';

export interface KillData {
  ship: number;
  npc: number;
  pod: number;
}

export interface SovData {
  alliance_id: number | null;
  corporation_id: number | null;
  faction_id: number | null;
}

export interface FWData {
  owner: number | null;
  occupier: number | null;
  contested: string;
  vp: number;
  vp_threshold: number;
  vp_pct: number;
}

export interface IncursionData {
  constellation_id: number;
  staging_system_id: number;
  type: string;
  state: string;
  influence?: number;
  has_boss?: boolean;
  systems: number[];
}

export interface IndustryIndices {
  manufacturing: number;
  me: number;
  te: number;
  copying: number;
  invention: number;
  reaction: number;
}

export interface TheraConnection {
  src: number;
  dst: number;
  src_name: string;
  dst_name: string;
  type: string;
  mass_status: string;
  life_hours: number | null;
  sig: string;
  created_at?: string | null;
}

export interface MapStats {
  kills: Record<string, KillData>;
  jumps: Record<string, number>;
  sovereignty: Record<string, SovData>;
  fw: Record<string, FWData>;
  incursions: IncursionData[];
  indices: Record<string, IndustryIndices>;
  adm: Record<string, number>;
  thera: TheraConnection[];
  _freshness: Record<string, string | null>;
}

export function useOverlayData() {
  const [stats, setStats] = useState<MapStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchStats = useCallback(async () => {
    try {
      const resp = await fetch('/api/map/stats');
      if (!resp.ok) {
        if (resp.status === 401) {
          setError('Not authenticated');
          return;
        }
        throw new Error(`HTTP ${resp.status}`);
      }
      const data: MapStats = await resp.json();
      setStats(data);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to fetch stats');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchStats();
    // Refresh every 5 minutes, but only while the tab is visible — saves
    // battery on mobile and cuts useless ESI load when the user is away.
    let interval: number | null = null;
    const start = () => {
      if (interval != null) return;
      interval = window.setInterval(fetchStats, 5 * 60 * 1000);
    };
    const stop = () => {
      if (interval != null) {
        clearInterval(interval);
        interval = null;
      }
    };
    const onVis = () => {
      if (document.visibilityState === 'visible') {
        fetchStats();  // immediate refresh on focus
        start();
      } else {
        stop();
      }
    };
    start();
    document.addEventListener('visibilitychange', onVis);
    return () => {
      stop();
      document.removeEventListener('visibilitychange', onVis);
    };
  }, [fetchStats]);

  return { stats, loading, error, refetch: fetchStats };
}

// ── Planet-type counts per system (static SDE data) ────────────────────

export interface PlanetTypesData {
  types: Record<string, string>;     // "11": "Temperate", etc.
  systems: Record<string, Record<string, number>>; // system_id → {type_id → count}
}

export function usePlanetTypes(enabled: boolean) {
  const [data, setData] = useState<PlanetTypesData | null>(null);

  useEffect(() => {
    if (!enabled || data) return;
    fetch('/api/map/planet-types')
      .then(r => r.ok ? r.json() : null)
      .then(setData)
      .catch(() => {});
  }, [enabled, data]);

  return data;
}
