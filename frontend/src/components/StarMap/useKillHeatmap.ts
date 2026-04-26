import { useCallback, useEffect, useState } from 'react';
import type { KillHeatmapWindow } from './types';

export interface KillHeatmapResponse {
  window: KillHeatmapWindow;
  space: 'k' | 'w';
  bucket_seconds: number;
  buckets: string[];          // ISO timestamps, one per bucket
  max_value: number;          // global max across all (system, bucket) cells
  data: Record<string, number[]>; // system_id (string) → [v0, v1, ...]
}

/**
 * Bulk-fetches the per-system kill heatmap dataset for the selected window.
 * Refetches when window or space changes; caches per (window, space) so
 * toggling back doesn't re-hit the server.
 */
export function useKillHeatmap(window: KillHeatmapWindow, space: 'k' | 'w', enabled: boolean) {
  const [data, setData] = useState<KillHeatmapResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async (w: KillHeatmapWindow, s: 'k' | 'w') => {
    setLoading(true);
    setError(null);
    try {
      const resp = await fetch(`/api/map/kill-heatmap?window=${w}&space=${s}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const body: KillHeatmapResponse = await resp.json();
      setData(body);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to fetch heatmap');
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!enabled) return;
    fetchData(window, space);
  }, [window, space, enabled, fetchData]);

  return { data, loading, error, refetch: () => fetchData(window, space) };
}
