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
}

export interface IncursionData {
  constellation_id: number;
  staging_system_id: number;
  type: string;
  state: string;
  systems: number[];
}

export interface MapStats {
  kills: Record<string, KillData>;
  jumps: Record<string, number>;
  sovereignty: Record<string, SovData>;
  fw: Record<string, FWData>;
  incursions: IncursionData[];
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
    // Refresh every 5 minutes
    const interval = setInterval(fetchStats, 5 * 60 * 1000);
    return () => clearInterval(interval);
  }, [fetchStats]);

  return { stats, loading, error, refetch: fetchStats };
}
