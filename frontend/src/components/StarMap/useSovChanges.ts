import { useEffect, useState, useCallback } from 'react';

export type SovTimeRange = '24h' | '7d' | '1m' | '6m' | '1y';

export interface SovChangeData {
  old_alliance_id: number | null;
  new_alliance_id: number | null;
  old_faction_id: number | null;
  new_faction_id: number | null;
  first_change: string;
  last_change: string;
  change_count: number;
}

export interface SovChangesResult {
  changes: Record<string, SovChangeData>;
  range: SovTimeRange;
  since: string;
}

export function useSovChanges(active: boolean, range: SovTimeRange | null) {
  const [data, setData] = useState<SovChangesResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchChanges = useCallback(async (r: SovTimeRange) => {
    setLoading(true);
    try {
      const resp = await fetch(`/api/map/sov-changes?range=${r}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const result: SovChangesResult = await resp.json();
      setData(result);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to fetch sov changes');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!active || !range) {
      setData(null);
      return;
    }
    fetchChanges(range);
    const interval = setInterval(() => fetchChanges(range), 5 * 60 * 1000);
    return () => clearInterval(interval);
  }, [active, range, fetchChanges]);

  return { data, loading, error };
}
