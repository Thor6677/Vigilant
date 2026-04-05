import { useEffect, useState, useCallback } from 'react';

export interface CharacterLocation {
  character_id: number;
  character_name: string;
  system_id: number | null;
  system_name: string | null;
  is_main: boolean;
}

export function useCharacterLocations() {
  const [characters, setCharacters] = useState<CharacterLocation[]>([]);

  const fetchLocations = useCallback(async () => {
    try {
      const resp = await fetch('/api/map/characters');
      if (!resp.ok) return;
      const data: CharacterLocation[] = await resp.json();
      setCharacters(data);
    } catch {
      // Silently fail — character locations are non-critical
    }
  }, []);

  useEffect(() => {
    fetchLocations();
    const interval = setInterval(fetchLocations, 60_000);
    return () => clearInterval(interval);
  }, [fetchLocations]);

  return { characters };
}
