import { useState } from 'react';
import { useMapData } from './components/StarMap/useMapData';
import { StarMap } from './components/StarMap/StarMap';

// Initial space ('k' | 'w') comes from the #map-root data attribute set by
// the page template. /map → 'k', /map/wormholes → 'w'. After load the user
// can toggle live within the StarMap UI without navigating.
function readInitialSpace(): 'k' | 'w' {
  const root = document.getElementById('map-root');
  return root?.dataset.space === 'w' ? 'w' : 'k';
}

export default function App() {
  const [space, setSpace] = useState<'k' | 'w'>(readInitialSpace);
  const { data, loading, error } = useMapData(space);

  if (error) {
    return (
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        width: '100%', height: '100%', color: '#cc3333',
        fontFamily: "'JetBrains Mono', monospace", fontSize: 12,
      }}>
        Failed to load map data: {error}
      </div>
    );
  }

  if (loading || !data) {
    return null; // Template shows the loading spinner
  }

  return <StarMap data={data} space={space} onSpaceChange={setSpace} />;
}
