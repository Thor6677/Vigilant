import { useCallback, useEffect, useRef, useState } from 'react';

export interface MapBookmark {
  id: number;
  kind: 'system' | 'constellation' | 'region';
  entity_id: number;
  label: string | null;
  color: string | null;
  notes: string | null;
  created_at: string | null;
}

/** DB-backed personal bookmarks for the star map.
 *  Shared between the system info panel (toggle), search (list),
 *  context menu (toggle), and the overlay renderer (pinned marker). */
export function useBookmarks() {
  const [bookmarks, setBookmarks] = useState<MapBookmark[]>([]);
  const [loading, setLoading] = useState(true);
  const tempIdCounter = useRef(0);

  const reload = useCallback(async () => {
    try {
      const resp = await fetch('/api/map/bookmarks');
      if (!resp.ok) return;
      const data: MapBookmark[] = await resp.json();
      setBookmarks(data);
    } catch {
      // Silent — bookmarks are non-critical
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { reload(); }, [reload]);

  const isBookmarked = useCallback((kind: MapBookmark['kind'], entityId: number) => {
    return bookmarks.some(b => b.kind === kind && b.entity_id === entityId);
  }, [bookmarks]);

  const addBookmark = useCallback(async (
    kind: MapBookmark['kind'], entityId: number, label?: string,
  ) => {
    tempIdCounter.current -= 1;
    const tempId = tempIdCounter.current;
    const optimistic: MapBookmark = {
      id: tempId, kind, entity_id: entityId,
      label: label ?? null, color: null, notes: null, created_at: null,
    };
    setBookmarks(prev => {
      if (prev.some(b => b.kind === kind && b.entity_id === entityId)) return prev;
      return [optimistic, ...prev];
    });
    try {
      const resp = await fetch('/api/map/bookmarks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind, entity_id: entityId, label: label ?? null }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const saved: MapBookmark = await resp.json();
      setBookmarks(prev => prev.map(b => (b.id === tempId ? saved : b)));
    } catch {
      setBookmarks(prev => prev.filter(b => b.id !== tempId));
    }
  }, []);

  const removeBookmark = useCallback(async (kind: MapBookmark['kind'], entityId: number) => {
    const existing = bookmarks.find(b => b.kind === kind && b.entity_id === entityId);
    if (!existing) return;
    setBookmarks(prev => prev.filter(b => !(b.kind === kind && b.entity_id === entityId)));
    try {
      await fetch(`/api/map/bookmarks/${existing.id}`, { method: 'DELETE' });
    } catch {
      setBookmarks(prev => [existing, ...prev]);
    }
  }, [bookmarks]);

  const toggleBookmark = useCallback(async (kind: MapBookmark['kind'], entityId: number) => {
    if (isBookmarked(kind, entityId)) await removeBookmark(kind, entityId);
    else await addBookmark(kind, entityId);
  }, [isBookmarked, addBookmark, removeBookmark]);

  return { bookmarks, loading, isBookmarked, addBookmark, removeBookmark, toggleBookmark, reload };
}
