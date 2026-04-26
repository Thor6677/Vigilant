import { useCallback, useEffect, useState } from 'react';
import type { KillHeatmapWindow } from './types';

interface KillHeatmapResponseRaw {
  window: KillHeatmapWindow;
  space: 'k' | 'w';
  bucket_seconds: number;
  buckets: string[];
  max_value: number;
  sparse: boolean;
  // sparse=false → per-system dense int array (length = num_buckets)
  // sparse=true  → per-system list of [bucket_idx, count] pairs
  data: Record<string, number[] | number[][]>;
}

export interface KillHeatmapData {
  window: KillHeatmapWindow;
  space: 'k' | 'w';
  bucket_seconds: number;
  buckets: string[];
  max_value: number;
  /** Per-system dense lookup array. Length = buckets.length. */
  lookup: Map<number, Uint16Array>;
}

function materialize(raw: KillHeatmapResponseRaw): KillHeatmapData {
  const numBuckets = raw.buckets.length;
  const lookup = new Map<number, Uint16Array>();
  if (raw.sparse) {
    for (const [sid, pairs] of Object.entries(raw.data)) {
      const arr = new Uint16Array(numBuckets);
      for (const pair of pairs as number[][]) {
        const [bi, cnt] = pair;
        if (bi >= 0 && bi < numBuckets) arr[bi] = cnt;
      }
      lookup.set(parseInt(sid, 10), arr);
    }
  } else {
    for (const [sid, dense] of Object.entries(raw.data)) {
      const src = dense as number[];
      const arr = new Uint16Array(numBuckets);
      for (let i = 0; i < numBuckets; i++) arr[i] = src[i] ?? 0;
      lookup.set(parseInt(sid, 10), arr);
    }
  }
  return {
    window: raw.window,
    space: raw.space,
    bucket_seconds: raw.bucket_seconds,
    buckets: raw.buckets,
    max_value: raw.max_value,
    lookup,
  };
}

/**
 * Bulk-fetches the per-system kill heatmap dataset for the selected window.
 * Materializes the response into per-system Uint16Array lookups for fast
 * indexed access in the render loop. Refetches on (window, space) change.
 */
export function useKillHeatmap(window: KillHeatmapWindow, space: 'k' | 'w', enabled: boolean) {
  const [data, setData] = useState<KillHeatmapData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async (w: KillHeatmapWindow, s: 'k' | 'w') => {
    setLoading(true);
    setError(null);
    try {
      const resp = await fetch(`/api/map/kill-heatmap?window=${w}&space=${s}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const raw: KillHeatmapResponseRaw = await resp.json();
      setData(materialize(raw));
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
